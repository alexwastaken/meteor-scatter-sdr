from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Event, RLock, Thread
from typing import Any
import hashlib
import json
import math
import subprocess
import time

import numpy as np

from .config import CaptureConfig, ConfigurationError
from .rtl import RtlSdr, RtlSdrError


BUFFER_SECONDS = 30.0
FFT_SIZE = 4096
DISPLAY_BINS = 512
READ_BYTES = 262_144


@dataclass(frozen=True, slots=True)
class LiveSettings:
    config: CaptureConfig
    detection_frequency_hz: int
    threshold_db: float = 8.0

    def validated(self) -> "LiveSettings":
        config = self.config.validated()
        if not config.lower_edge_hz <= self.detection_frequency_hz <= config.upper_edge_hz:
            raise ConfigurationError("detection frequency must be inside the displayed span")
        if not math.isfinite(self.threshold_db) or not 3 <= self.threshold_db <= 30:
            raise ConfigurationError("detector threshold must be between 3 and 30 dB")
        return replace(self, config=config)


class RollingIqBuffer:
    """A byte-accurate FIFO that never retains more than its configured duration."""

    def __init__(self, sample_rate_hz: int, seconds: float = BUFFER_SECONDS) -> None:
        self.capacity = round(sample_rate_hz * seconds * 2)
        self._chunks: deque[bytes] = deque()
        self.byte_count = 0

    def append(self, data: bytes) -> None:
        if not data:
            return
        self._chunks.append(data)
        self.byte_count += len(data)
        overflow = self.byte_count - self.capacity
        while overflow > 0 and self._chunks:
            first = self._chunks[0]
            if len(first) <= overflow:
                self._chunks.popleft()
                self.byte_count -= len(first)
                overflow -= len(first)
            else:
                self._chunks[0] = first[overflow:]
                self.byte_count -= overflow
                overflow = 0

    @property
    def fill_ratio(self) -> float:
        return min(1.0, self.byte_count / self.capacity)

    def snapshot(self) -> bytes:
        return b"".join(self._chunks)


class SpectrumAnalyzer:
    def __init__(self, fft_size: int = FFT_SIZE, display_bins: int = DISPLAY_BINS) -> None:
        if fft_size % display_bins:
            raise ValueError("fft size must be divisible by display bins")
        self.fft_size = fft_size
        self.display_bins = display_bins
        self.window = np.hanning(fft_size).astype(np.float32)
        self.window_scale = float(self.window.sum() * 127.5)

    def analyze(self, iq_bytes: bytes) -> np.ndarray:
        needed = self.fft_size * 2
        if len(iq_bytes) < needed:
            raise ValueError(f"at least {needed} I/Q bytes are required")
        raw = np.frombuffer(iq_bytes[-needed:], dtype=np.uint8).astype(np.float32)
        samples = (raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)
        fft = np.fft.fftshift(np.fft.fft(samples * self.window))
        power = 20 * np.log10(np.maximum(np.abs(fft) / self.window_scale, 1e-8))
        grouped = power.reshape(self.display_bins, -1).max(axis=1)
        return grouped.astype(np.float32)


class CandidateDetector:
    """Adaptive narrowband level detector; its output is a candidate, not proof."""

    def __init__(self, threshold_db: float, warmup_frames: int = 40, cooldown: float = 15.0) -> None:
        self.threshold_db = threshold_db
        self.warmup_frames = warmup_frames
        self.cooldown = cooldown
        self.history: deque[float] = deque(maxlen=240)
        self.consecutive = 0
        self.last_trigger = -math.inf

    def update(self, level_db: float, now: float | None = None) -> tuple[bool, float, float | None]:
        now = time.monotonic() if now is None else now
        baseline = float(np.median(self.history)) if self.history else None
        excess = 0.0 if baseline is None else level_db - baseline
        ready = len(self.history) >= self.warmup_frames
        if ready and excess >= self.threshold_db:
            self.consecutive += 1
        else:
            self.consecutive = 0
        triggered = self.consecutive >= 2 and now - self.last_trigger >= self.cooldown
        if triggered:
            self.last_trigger = now
            self.consecutive = 0
        if not ready or excess < self.threshold_db / 2:
            self.history.append(level_db)
        return triggered, excess, baseline


def settings_from_payload(base: CaptureConfig, payload: dict[str, Any]) -> LiveSettings:
    allowed = {
        "center_frequency_hz",
        "sample_rate_hz",
        "gain_db",
        "detection_frequency_hz",
        "threshold_db",
    }
    unexpected = set(payload) - allowed
    if unexpected:
        raise ConfigurationError(f"unsupported live setting: {sorted(unexpected)[0]}")

    def integer(name: str, default: int) -> int:
        value = payload.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError(f"{name} must be an integer")
        return value

    gain_value = payload.get("gain_db", base.gain_db)
    if gain_value is None or (isinstance(gain_value, str) and gain_value.lower() == "auto"):
        gain = None
    elif isinstance(gain_value, bool) or not isinstance(gain_value, (int, float)):
        raise ConfigurationError("gain must be 'auto' or a number")
    else:
        gain = float(gain_value)
    threshold = payload.get("threshold_db", 8.0)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ConfigurationError("detector threshold must be a number")

    config = replace(
        base,
        center_frequency_hz=integer("center_frequency_hz", base.center_frequency_hz),
        sample_rate_hz=integer("sample_rate_hz", base.sample_rate_hz),
        gain_db=gain,
    ).validated()
    detection = integer("detection_frequency_hz", config.center_frequency_hz)
    return LiveSettings(config, detection, float(threshold)).validated()


class LiveMonitor:
    def __init__(self, base_config: CaptureConfig, rtl: RtlSdr | None = None) -> None:
        self.base_config = base_config.validated()
        self.rtl = rtl or RtlSdr()
        self._condition = Condition(RLock())
        self._stop = Event()
        self._thread: Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._settings: LiveSettings | None = None
        self._state = "stopped"
        self._error: str | None = None
        self._sequence = 0
        self._frame: dict[str, Any] | None = None
        self._buffer_seconds = 0.0
        self._candidate_count = 0
        self._started_at: str | None = None
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="meteor-archive")

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = settings_from_payload(self.base_config, payload)
        with self._condition:
            if self._state in {"starting", "running"}:
                raise RtlSdrError("live monitoring is already running")
            self._settings = settings
            self._state = "starting"
            self._error = None
            self._frame = None
            self._buffer_seconds = 0.0
            self._candidate_count = 0
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._stop.clear()
            self._thread = Thread(target=self._run, daemon=True, name="rtl-live-reader")
            self._thread.start()
            return self.status_locked()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        with self._condition:
            if self._state != "error":
                self._state = "stopped"
            self._condition.notify_all()
            return self.status_locked()

    def close(self) -> None:
        self.stop()
        self._writer.shutdown(wait=True, cancel_futures=False)

    def status(self) -> dict[str, Any]:
        with self._condition:
            return self.status_locked()

    def status_locked(self) -> dict[str, Any]:
        settings = self._settings
        config = settings.config if settings else self.base_config
        return {
            "state": self._state,
            "error": self._error,
            "started_at_utc": self._started_at,
            "buffer_seconds": round(self._buffer_seconds, 2),
            "buffer_target_seconds": BUFFER_SECONDS,
            "candidate_count": self._candidate_count,
            "settings": {
                "center_frequency_hz": config.center_frequency_hz,
                "sample_rate_hz": config.sample_rate_hz,
                "gain_db": "auto" if config.gain_db is None else config.gain_db,
                "detection_frequency_hz": (
                    settings.detection_frequency_hz if settings else config.center_frequency_hz
                ),
                "threshold_db": settings.threshold_db if settings else 8.0,
                "lower_edge_hz": config.lower_edge_hz,
                "upper_edge_hz": config.upper_edge_hz,
            },
            "event_directory": str(self._event_dir().resolve()),
        }

    def wait_for_frame(self, after_sequence: int, timeout: float = 10.0) -> dict[str, Any] | None:
        with self._condition:
            self._condition.wait_for(
                lambda: self._sequence > after_sequence or self._state in {"stopped", "error"},
                timeout=timeout,
            )
            if self._sequence > after_sequence and self._frame is not None:
                return self._frame
            return None

    def events(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        event_dir = self._event_dir()
        if not event_dir.exists():
            return result
        for path in event_dir.glob("*.cu8.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                data_path = event_dir / item["data_file"]
                if data_path.is_file():
                    item["byte_count"] = data_path.stat().st_size
                    result.append(item)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        result.sort(key=lambda item: item.get("triggered_at_utc", ""), reverse=True)
        return result

    def _event_dir(self) -> Path:
        return self.base_config.output_dir.expanduser() / "events"

    def _run(self) -> None:
        assert self._settings is not None
        settings = self._settings
        config = settings.config
        analyzer = SpectrumAnalyzer()
        detector = CandidateDetector(settings.threshold_db)
        ring = RollingIqBuffer(config.sample_rate_hz)
        stderr_parts: list[bytes] = []
        process: subprocess.Popen[bytes] | None = None
        stderr_thread: Thread | None = None
        try:
            command = self.rtl.stream_command(config)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._process = process
            stderr_thread = Thread(
                target=self._drain_stderr,
                args=(process, stderr_parts),
                daemon=True,
            )
            stderr_thread.start()
            with self._condition:
                self._state = "running"
                self._condition.notify_all()
            assert process.stdout is not None
            while not self._stop.is_set():
                block = process.stdout.read(READ_BYTES)
                if not block:
                    break
                ring.append(block)
                if len(block) < FFT_SIZE * 2:
                    continue
                spectrum = analyzer.analyze(block)
                bin_width = config.sample_rate_hz / analyzer.display_bins
                target_index = round(
                    (settings.detection_frequency_hz - config.lower_edge_hz) / bin_width
                )
                target_index = max(0, min(analyzer.display_bins - 1, target_index))
                radius = max(1, round(8_000 / bin_width))
                low = max(0, target_index - radius)
                high = min(analyzer.display_bins, target_index + radius + 1)
                level = float(np.max(spectrum[low:high]))
                triggered, excess, baseline = detector.update(level)
                buffer_seconds = ring.byte_count / (config.sample_rate_hz * 2)
                archived = False
                if triggered and ring.fill_ratio >= 0.999:
                    archived = True
                    self._candidate_count += 1
                    self._writer.submit(
                        self._archive,
                        ring.snapshot(),
                        settings,
                        level,
                        excess,
                    )
                frame = {
                    "sequence": self._sequence + 1,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "spectrum_db": [round(float(value), 1) for value in spectrum],
                    "lower_edge_hz": config.lower_edge_hz,
                    "upper_edge_hz": config.upper_edge_hz,
                    "detection_frequency_hz": settings.detection_frequency_hz,
                    "target_level_db": round(level, 1),
                    "baseline_db": None if baseline is None else round(baseline, 1),
                    "excess_db": round(excess, 1),
                    "threshold_db": settings.threshold_db,
                    "buffer_seconds": round(buffer_seconds, 2),
                    "detector_ready": len(detector.history) >= detector.warmup_frames,
                    "archived": archived,
                }
                with self._condition:
                    self._sequence += 1
                    frame["sequence"] = self._sequence
                    self._frame = frame
                    self._buffer_seconds = buffer_seconds
                    self._condition.notify_all()
            returncode = process.poll()
            if returncode is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if not self._stop.is_set() and process.returncode not in {0, None}:
                stderr_thread.join(timeout=0.5)
                detail = b"".join(stderr_parts).decode("utf-8", errors="replace").strip()
                raise RtlSdrError(detail or f"rtl_sdr exited with status {process.returncode}")
        except (OSError, RtlSdrError, ValueError) as exc:
            with self._condition:
                self._state = "error"
                self._error = str(exc)
                self._condition.notify_all()
        finally:
            if process is not None:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            if stderr_thread is not None:
                stderr_thread.join(timeout=0.5)
            self._process = None
            with self._condition:
                if self._state != "error":
                    self._state = "stopped"
                self._condition.notify_all()

    @staticmethod
    def _drain_stderr(process: subprocess.Popen[bytes], parts: list[bytes]) -> None:
        if process.stderr is None:
            return
        while data := process.stderr.read(4096):
            parts.append(data)
            if sum(map(len, parts)) > 32_768:
                del parts[:-4]

    def _archive(
        self,
        data: bytes,
        settings: LiveSettings,
        level_db: float,
        excess_db: float,
    ) -> None:
        event_dir = self._event_dir()
        event_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        stem = f"meteor_candidate_{now.strftime('%Y%m%dT%H%M%S_%fZ')}_{settings.detection_frequency_hz}Hz"
        data_path = event_dir / f"{stem}.cu8"
        metadata_path = event_dir / f"{stem}.cu8.json"
        temporary_path = event_dir / f".{stem}.part"
        temporary_path.write_bytes(data)
        temporary_path.replace(data_path)
        metadata = {
            "classification": "meteor_candidate_unconfirmed",
            "data_file": data_path.name,
            "triggered_at_utc": now.isoformat(),
            "buffer_duration_seconds": len(data) / (settings.config.sample_rate_hz * 2),
            "center_frequency_hz": settings.config.center_frequency_hz,
            "sample_rate_hz": settings.config.sample_rate_hz,
            "lower_edge_hz": settings.config.lower_edge_hz,
            "upper_edge_hz": settings.config.upper_edge_hz,
            "detection_frequency_hz": settings.detection_frequency_hz,
            "gain_db": "auto" if settings.config.gain_db is None else settings.config.gain_db,
            "ppm": settings.config.ppm,
            "sample_format": "cu8_interleaved_iq",
            "detector_threshold_db": settings.threshold_db,
            "trigger_level_db": round(level_db, 2),
            "trigger_excess_db": round(excess_db, 2),
            "sha256": hashlib.sha256(data).hexdigest(),
            "note": "Automatic candidate only; inspect the waterfall before classifying it as a meteor.",
        }
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
