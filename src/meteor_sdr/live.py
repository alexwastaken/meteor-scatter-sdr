from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Event, RLock, Thread
from typing import Any, Callable, Protocol
import hashlib
import json
import math
import struct
import time
import zlib

import numpy as np

from .config import CaptureConfig, ConfigurationError
from .rtl import RtlSdrError, RtlTcpStream


BUFFER_SECONDS = 30.0
FFT_SIZE = 4096
DISPLAY_BINS = 256
READ_BYTES = 32_768
MAX_FRAME_RATE_HZ = 22.0
RETUNE_FLUSH_SECONDS = 0.15


class RadioStream(Protocol):
    def read(self, size: int) -> bytes | None: ...

    def tune(self, config: CaptureConfig) -> None: ...

    def stderr_detail(self) -> str: ...

    def close(self) -> None: ...


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


def render_waterfall_png(
    iq_bytes: bytes,
    analyzer: SpectrumAnalyzer | None = None,
    height: int = 240,
) -> bytes:
    """Render a compact, newest-first waterfall preview without extra packages."""
    analyzer = analyzer or SpectrumAnalyzer()
    needed = analyzer.fft_size * 2
    if len(iq_bytes) < needed:
        raise ValueError(f"at least {needed} I/Q bytes are required")
    maximum_start = len(iq_bytes) - needed
    starts = np.linspace(0, maximum_start, height, dtype=np.int64)
    rows = np.empty((height, analyzer.display_bins), dtype=np.float32)
    for index, start in enumerate(starts):
        rows[height - index - 1] = analyzer.analyze(
            iq_bytes[int(start) : int(start) + needed]
        )

    level = np.clip((rows + 90.0) / 70.0, 0.0, 1.0)
    stops = np.array(
        [[0, 0, 0], [0, 12, 40], [0, 72, 210], [0, 181, 255], [220, 248, 255]],
        dtype=np.float32,
    )
    scaled = level * (len(stops) - 1)
    lower = np.minimum(len(stops) - 2, scaled.astype(np.int16))
    mix = (scaled - lower)[..., None]
    rgb = (stops[lower] * (1 - mix) + stops[lower + 1] * mix).astype(np.uint8)
    raw = b"".join(b"\x00" + row.tobytes() for row in rgb)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", analyzer.display_bins, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(
        b"IDAT", zlib.compress(raw, level=6)
    ) + chunk(b"IEND", b"")


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
    def __init__(
        self,
        base_config: CaptureConfig,
        source_factory: Callable[[CaptureConfig], RadioStream] | None = None,
    ) -> None:
        self.base_config = base_config.validated()
        self._source_factory = source_factory or RtlTcpStream
        self._condition = Condition(RLock())
        self._stop = Event()
        self._thread: Thread | None = None
        self._stream: RadioStream | None = None
        self._settings: LiveSettings | None = None
        self._settings_revision = 0
        self._radio_revision = 0
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
            self._settings_revision += 1
            self._radio_revision += 1
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
        stream = self._stream
        if stream is not None:
            stream.close()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        with self._condition:
            if self._state != "error":
                self._state = "stopped"
            self._condition.notify_all()
            return self.status_locked()

    def tune(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Retune the persistent rtl_tcp source without restarting the receiver."""
        settings = settings_from_payload(self.base_config, payload)
        with self._condition:
            if self._state not in {"starting", "running"}:
                raise RtlSdrError("start monitoring before using live tuning")
            stream = self._stream
        if stream is not None:
            stream.tune(settings.config)
        with self._condition:
            if self._state not in {"starting", "running"}:
                raise RtlSdrError("receiver stopped while it was being tuned")
            self._settings = settings
            self._settings_revision += 1
            self._radio_revision += 1
            self._buffer_seconds = 0.0
            self._condition.notify_all()
            return self.status_locked()

    def watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"detection_frequency_hz"}:
            raise ConfigurationError("watch update requires detection_frequency_hz")
        frequency = payload["detection_frequency_hz"]
        if isinstance(frequency, bool) or not isinstance(frequency, int):
            raise ConfigurationError("detection_frequency_hz must be an integer")
        with self._condition:
            if self._state not in {"starting", "running"} or self._settings is None:
                raise RtlSdrError("receiver is not running")
            config = self._settings.config
            if not config.lower_edge_hz <= frequency <= config.upper_edge_hz:
                raise ConfigurationError("watch frequency must be inside the displayed span")
            self._settings = replace(self._settings, detection_frequency_hz=frequency)
            self._settings_revision += 1
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
                lambda: self._sequence > after_sequence,
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
                    preview_name = item.get("preview_file")
                    if isinstance(preview_name, str) and (event_dir / preview_name).is_file():
                        item["preview_url"] = f"/api/events/previews/{preview_name}"
                    result.append(item)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        result.sort(key=lambda item: item.get("triggered_at_utc", ""), reverse=True)
        return result

    def _event_dir(self) -> Path:
        return self.base_config.output_dir.expanduser() / "events"

    def preview_path(self, name: str) -> Path | None:
        if Path(name).name != name or not name.endswith(".waterfall.png"):
            return None
        path = (self._event_dir() / name).resolve()
        event_dir = self._event_dir().resolve()
        return path if path.parent == event_dir and path.is_file() else None

    def _run(self) -> None:
        with self._condition:
            assert self._settings is not None
            settings = self._settings
            settings_revision = self._settings_revision
            radio_revision = self._radio_revision
        config = settings.config
        analyzer = SpectrumAnalyzer()
        detector = CandidateDetector(settings.threshold_db)
        ring = RollingIqBuffer(config.sample_rate_hz)
        stream: RadioStream | None = None
        discard_bytes = 0
        try:
            stream = self._source_factory(config)
            with self._condition:
                self._stream = stream
                assert self._settings is not None
                if self._radio_revision != radio_revision:
                    settings = self._settings
                    settings_revision = self._settings_revision
                    radio_revision = self._radio_revision
                    config = settings.config
                    stream.tune(config)
                    ring = RollingIqBuffer(config.sample_rate_hz)
                    detector = CandidateDetector(settings.threshold_db)
                    discard_bytes = round(
                        config.sample_rate_hz * 2 * RETUNE_FLUSH_SECONDS
                    )
                self._state = "running"
                self._condition.notify_all()
            last_frame_time = 0.0
            while not self._stop.is_set():
                block = stream.read(READ_BYTES)
                if block is None:
                    continue
                if block == b"":
                    break
                with self._condition:
                    if self._radio_revision != radio_revision:
                        assert self._settings is not None
                        settings = self._settings
                        settings_revision = self._settings_revision
                        radio_revision = self._radio_revision
                        config = settings.config
                        ring = RollingIqBuffer(config.sample_rate_hz)
                        detector = CandidateDetector(settings.threshold_db)
                        discard_bytes = round(
                            config.sample_rate_hz * 2 * RETUNE_FLUSH_SECONDS
                        )
                        last_frame_time = 0.0
                    elif self._settings_revision != settings_revision:
                        assert self._settings is not None
                        settings = self._settings
                        settings_revision = self._settings_revision
                        detector = CandidateDetector(settings.threshold_db)
                if discard_bytes:
                    discard_bytes = max(0, discard_bytes - len(block))
                    continue
                ring.append(block)
                if len(block) < FFT_SIZE * 2:
                    continue
                frame_time = time.monotonic()
                if frame_time - last_frame_time < 1 / MAX_FRAME_RATE_HZ:
                    continue
                last_frame_time = frame_time
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
            if not self._stop.is_set():
                detail = stream.stderr_detail()
                raise RtlSdrError(detail or "rtl_tcp closed the live stream")
        except (OSError, RtlSdrError, ValueError) as exc:
            with self._condition:
                self._state = "error"
                self._error = str(exc)
                self._condition.notify_all()
        finally:
            if stream is not None:
                stream.close()
            self._stream = None
            with self._condition:
                if self._state != "error":
                    self._state = "stopped"
                self._condition.notify_all()

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
        preview_path = event_dir / f"{stem}.waterfall.png"
        temporary_path = event_dir / f".{stem}.part"
        temporary_path.write_bytes(data)
        temporary_path.replace(data_path)
        preview_path.write_bytes(render_waterfall_png(data))
        metadata = {
            "classification": "meteor_candidate_unconfirmed",
            "data_file": data_path.name,
            "preview_file": preview_path.name,
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
