from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import signal
import subprocess

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
