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
from typing import Any

import numpy as np

from .config import CaptureConfig


class RtlSdrError(RuntimeError):
    """Raised when rtl_sdr is missing or a capture fails."""

    def __init__(
        self,
        message: str,
        *,
        receiver_info: dict[str, str] | None = None,
        error_stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.receiver_info = dict(receiver_info) if receiver_info else None
        self.error_stage = error_stage


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


class RtlPyStream:
    """Low-latency direct USB stream powered by the maintained pyrtlsdr binding."""

    def __init__(self, config: CaptureConfig) -> None:
        try:
            from rtlsdr import RtlSdr as PyRtlSdr
        except (ImportError, OSError) as exc:
            raise RtlSdrError(
                "PyRTLSDR could not load. Activate the project virtual environment, "
                "run 'python -m pip install -e .', and confirm the rtl-sdr system "
                f"package is installed. Detail: {exc}"
            ) from exc

        device = config.device.strip()
        arguments: dict[str, Any]
        if device.isdigit():
            device_index = int(device)
            arguments = {"device_index": device_index}
        else:
            device_index = PyRtlSdr.get_device_index_by_serial(device)
            arguments = {"serial_number": device}
        try:
            self._sdr = PyRtlSdr(**arguments)
        except Exception as exc:
            raise RtlSdrError(
                f"PyRTLSDR could not open receiver {device}: {exc}",
                error_stage="open",
            ) from exc

        self._lock = threading.RLock()
        self._closed = False
        self._config: CaptureConfig | None = None
        self._last_error = ""
        try:
            self._identity = self._read_identity(device_index)
        except (AttributeError, ImportError, OSError, TypeError, ValueError):
            self._identity = {
                "display_name": "RTL-SDR receiver",
                "manufacturer": "",
                "product": "",
                "serial": device if not device.isdigit() else "",
                "driver_name": "RTL-SDR",
            }
        try:
            self.tune(config)
        except Exception:
            self.close()
            raise

    def read(self, size: int) -> bytes:
        with self._lock:
            if self._closed:
                return b""
            try:
                return bytes(self._sdr.read_bytes(size))
            except Exception as exc:
                if self._closed:
                    return b""
                self._last_error = str(exc)
                raise RtlSdrError(f"PyRTLSDR sample read failed: {exc}") from exc

    def tune(self, config: CaptureConfig) -> None:
        config = config.validated()
        with self._lock:
            if self._closed:
                raise RtlSdrError("PyRTLSDR receiver is closed")
            previous = self._config
            try:
                if previous is None or previous.sample_rate_hz != config.sample_rate_hz:
                    self._sdr.sample_rate = config.sample_rate_hz
                if (previous is None or previous.ppm != config.ppm) and (
                    config.ppm != 0 or previous is not None
                ):
                    try:
                        self._sdr.freq_correction = config.ppm
                    except Exception:
                        # Some R820T2 macOS builds reject an explicit zero even
                        # though zero is already the device default.
                        if config.ppm != 0:
                            raise
                if (
                    previous is None
                    or previous.center_frequency_hz != config.center_frequency_hz
                ):
                    self._sdr.center_freq = config.center_frequency_hz
                if previous is None or previous.gain_db != config.gain_db:
                    self._sdr.gain = "auto" if config.gain_db is None else config.gain_db
            except Exception as exc:
                self._last_error = str(exc)
                # A setter may have succeeded before a later one failed. The
                # cached configuration is no longer trustworthy, so a caller's
                # rollback must reapply every hardware setting.
                self._config = None
                raise RtlSdrError(
                    f"PyRTLSDR tuning failed: {exc}",
                    receiver_info=self._identity,
                    error_stage="tuning",
                ) from exc
            self._config = config

    def stderr_detail(self) -> str:
        return self._last_error

    @staticmethod
    def _read_identity(device_index: int) -> dict[str, str]:
        from ctypes import c_ubyte
        from rtlsdr.librtlsdr import librtlsdr

        manufacturer_buffer = (c_ubyte * 256)()
        product_buffer = (c_ubyte * 256)()
        serial_buffer = (c_ubyte * 256)()
        result = librtlsdr.rtlsdr_get_device_usb_strings(
            device_index,
            manufacturer_buffer,
            product_buffer,
            serial_buffer,
        )

        def decoded(buffer: object) -> str:
            return bytes(buffer).split(b"\0", 1)[0].decode(
                "utf-8", errors="replace"
            ).strip()

        manufacturer = decoded(manufacturer_buffer) if result == 0 else ""
        product = decoded(product_buffer) if result == 0 else ""
        serial = decoded(serial_buffer) if result == 0 else ""
        raw_name = librtlsdr.rtlsdr_get_device_name(device_index)
        driver_name = (
            raw_name.decode("utf-8", errors="replace").strip()
            if isinstance(raw_name, bytes)
            else str(raw_name or "").strip()
        )
        if manufacturer and product:
            display_name = (
                product
                if manufacturer.casefold() in product.casefold()
                else f"{manufacturer} {product}"
            )
        else:
            display_name = product or manufacturer or driver_name or "RTL-SDR receiver"
        return {
            "display_name": display_name,
            "manufacturer": manufacturer,
            "product": product,
            "serial": serial,
            "driver_name": driver_name,
        }

    def identity(self) -> dict[str, str]:
        return dict(self._identity)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._sdr.close()
            except Exception as exc:
                self._last_error = str(exc)


class SimulatedRadioStream:
    """Real-time synthetic I/Q source for learning the dashboard without hardware."""

    _STATIONS = (99_720_000, 100_180_000, 100_365_000)
    _PING_PERIOD_SECONDS = 14.0
    _PING_START_SECONDS = 5.0
    _PING_DURATION_SECONDS = 1.2

    def __init__(self, config: CaptureConfig) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._config = config.validated()
        self._sample_index = 0
        self._started = time.monotonic()
        self._rng = np.random.default_rng(20260827)

    def read(self, size: int) -> bytes:
        started = time.monotonic()
        with self._lock:
            if self._closed:
                return b""
            config = self._config
            sample_index = self._sample_index
            sample_count = max(1, size // 2)
            self._sample_index += sample_count

        sample_rate = config.sample_rate_hz
        sample_numbers = sample_index + np.arange(sample_count, dtype=np.float64)
        signal = (
            self._rng.normal(0.0, 0.038, sample_count)
            + 1j * self._rng.normal(0.0, 0.038, sample_count)
        )
        half_span = sample_rate / 2
        for index, station_hz in enumerate(self._STATIONS):
            offset = station_hz - config.center_frequency_hz
            if abs(offset) < half_span * 0.94:
                amplitude = 0.08 + index * 0.018
                signal += amplitude * np.exp(
                    2j * np.pi * offset * sample_numbers / sample_rate
                )

        cycle = (time.monotonic() - self._started) % self._PING_PERIOD_SECONDS
        ping_age = cycle - self._PING_START_SECONDS
        if 0 <= ping_age < self._PING_DURATION_SECONDS:
            envelope = np.sin(np.pi * ping_age / self._PING_DURATION_SECONDS) ** 2
            signal += 0.62 * envelope

        interleaved = np.empty(sample_count * 2, dtype=np.uint8)
        interleaved[0::2] = np.clip(signal.real * 127.5 + 127.5, 0, 255)
        interleaved[1::2] = np.clip(signal.imag * 127.5 + 127.5, 0, 255)

        remaining = sample_count / sample_rate - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        return interleaved.tobytes()[:size]

    def tune(self, config: CaptureConfig) -> None:
        with self._lock:
            if self._closed:
                raise RtlSdrError("simulated receiver is closed")
            self._config = config.validated()
            self._sample_index = 0

    def stderr_detail(self) -> str:
        return ""

    def identity(self) -> dict[str, str]:
        return {
            "display_name": "Meteor SDR simulated receiver",
            "manufacturer": "Meteor SDR",
            "product": "Synthetic RF generator",
            "serial": "DEMO",
            "driver_name": "Simulation",
        }

    def close(self) -> None:
        with self._lock:
            self._closed = True


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
