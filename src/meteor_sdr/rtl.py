from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time

from .config import CaptureConfig


class RtlSdrError(RuntimeError):
    """Raised when rtl_sdr is missing or a capture fails."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    command: tuple[str, ...]
    stderr: str
    returncode: int


class RtlSdr:
    def __init__(self, executable: str = "rtl_sdr") -> None:
        self.executable = executable

    def resolved_executable(self) -> str:
        if os.sep in self.executable:
            path = Path(self.executable)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        resolved = shutil.which(self.executable)
        if resolved is None:
            raise RtlSdrError(
                "rtl_sdr was not found. On Raspberry Pi OS run: "
                "sudo apt update && sudo apt install rtl-sdr"
            )
        return resolved

    def command(self, config: CaptureConfig, output_path: Path) -> list[str]:
        command = [
            self.resolved_executable(),
            "-d",
            config.device,
            "-f",
            str(config.center_frequency_hz),
            "-s",
            str(config.sample_rate_hz),
            "-p",
            str(config.ppm),
            "-n",
            str(config.sample_count),
        ]
        if config.gain_db is not None:
            command.extend(["-g", str(config.gain_db)])
        command.append(str(output_path))
        return command

    def stream_command(self, config: CaptureConfig) -> list[str]:
        """Build a continuous rtl_sdr command that writes unsigned I/Q to stdout."""
        command = [
            self.resolved_executable(),
            "-d",
            config.device,
            "-f",
            str(config.center_frequency_hz),
            "-s",
            str(config.sample_rate_hz),
            "-p",
            str(config.ppm),
        ]
        if config.gain_db is not None:
            command.extend(["-g", str(config.gain_db)])
        command.append("-")
        return command

    def capture(self, config: CaptureConfig, output_path: Path) -> ProcessResult:
        command = self.command(config, output_path)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _, stderr = process.communicate()
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
            try:
                _, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                _, stderr = process.communicate()
            raise

        result = ProcessResult(tuple(command), stderr.strip(), process.returncode)
        if result.returncode != 0:
            detail = result.stderr or f"rtl_sdr exited with status {result.returncode}"
            raise RtlSdrError(detail)
        return result


class RtlTcpStream:
    """One persistent local rtl_tcp stream with in-place tuner control."""

    _SET_FREQUENCY = 0x01
    _SET_SAMPLE_RATE = 0x02
    _SET_GAIN_MODE = 0x03
    _SET_GAIN = 0x04
    _SET_PPM = 0x05

    def __init__(
        self,
        config: CaptureConfig,
        executable: str = "rtl_tcp",
        connect_timeout: float = 4.0,
    ) -> None:
        self.executable = executable
        self._socket: socket.socket | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._command_lock = threading.Lock()
        self._stderr_parts: list[bytes] = []
        self._stderr_thread: threading.Thread | None = None
        self._closed = False
        self._open(config.validated(), connect_timeout)

    def resolved_executable(self) -> str:
        if os.sep in self.executable:
            path = Path(self.executable)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        resolved = shutil.which(self.executable)
        if resolved is None:
            raise RtlSdrError(
                "rtl_tcp was not found. On Raspberry Pi OS run: "
                "sudo apt update && sudo apt install rtl-sdr"
            )
        return resolved

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def _open(self, config: CaptureConfig, connect_timeout: float) -> None:
        port = self._available_port()
        command = [
            self.resolved_executable(),
            "-a",
            "127.0.0.1",
            "-p",
            str(port),
            "-d",
            config.device,
            "-f",
            str(config.center_frequency_hz),
            "-s",
            str(config.sample_rate_hz),
            "-P",
            str(config.ppm),
            "-n",
            "16",
        ]
        if config.gain_db is not None:
            command.extend(["-g", str(config.gain_db)])
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._process = process
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            daemon=True,
            name="rtl-tcp-stderr",
        )
        self._stderr_thread.start()

        deadline = time.monotonic() + connect_timeout
        connection: socket.socket | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            candidate.settimeout(0.2)
            try:
                candidate.connect(("127.0.0.1", port))
                connection = candidate
                break
            except OSError:
                candidate.close()
                time.sleep(0.04)
        if connection is None:
            detail = self.stderr_detail()
            self.close()
            raise RtlSdrError(detail or "rtl_tcp did not open its local stream")

        connection.settimeout(1.0)
        self._socket = connection
        try:
            header = self._receive_exact(12)
        except (OSError, TimeoutError) as exc:
            detail = self.stderr_detail()
            self.close()
            raise RtlSdrError(detail or f"rtl_tcp handshake failed: {exc}") from exc
        if len(header) != 12 or header[:4] != b"RTL0":
            self.close()
            raise RtlSdrError("rtl_tcp returned an invalid tuner handshake")

    def _receive_exact(self, size: int) -> bytes:
        assert self._socket is not None
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            data = self._socket.recv(remaining)
            if not data:
                raise ConnectionError("rtl_tcp closed the stream")
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def read(self, size: int) -> bytes | None:
        """Return up to size bytes; None means no samples arrived before timeout."""
        connection = self._socket
        if connection is None:
            return b""
        chunks: list[bytes] = []
        remaining = size
        try:
            while remaining:
                data = connection.recv(remaining)
                if not data:
                    return b"" if not chunks else b"".join(chunks)
                chunks.append(data)
                remaining -= len(data)
        except socket.timeout:
            return None if not chunks else b"".join(chunks)
        except OSError:
            if self._closed:
                return b""
            raise
        return b"".join(chunks)

    def tune(self, config: CaptureConfig) -> None:
        config = config.validated()
        with self._command_lock:
            self._send_command(self._SET_FREQUENCY, config.center_frequency_hz)
            self._send_command(self._SET_SAMPLE_RATE, config.sample_rate_hz)
            self._send_command(self._SET_PPM, config.ppm)
            if config.gain_db is None:
                self._send_command(self._SET_GAIN_MODE, 0)
            else:
                self._send_command(self._SET_GAIN_MODE, 1)
                self._send_command(self._SET_GAIN, round(config.gain_db * 10))

    def _send_command(self, command: int, value: int) -> None:
        connection = self._socket
        if connection is None:
            raise RtlSdrError("rtl_tcp stream is not connected")
        try:
            connection.sendall(struct.pack(">BI", command, value & 0xFFFFFFFF))
        except OSError as exc:
            raise RtlSdrError(f"rtl_tcp tuning command failed: {exc}") from exc

    def stderr_detail(self) -> str:
        return b"".join(self._stderr_parts).decode("utf-8", errors="replace").strip()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process is not None and process.stderr is not None:
            process.stderr.close()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.5)

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while data := process.stderr.read(4096):
                self._stderr_parts.append(data)
                if sum(map(len, self._stderr_parts)) > 32_768:
                    del self._stderr_parts[:-4]
        except (OSError, ValueError):
            return


def device_diagnostic(timeout_seconds: float = 20.0) -> tuple[bool, str]:
    executable = shutil.which("rtl_test")
    if executable is None:
        return False, "rtl_test was not found; install the rtl-sdr package"
    try:
        result = subprocess.run(
            [executable, "-t"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "").strip()
        return False, output or "rtl_test did not finish within 20 seconds"
    output = result.stdout.strip()
    return result.returncode == 0, output
