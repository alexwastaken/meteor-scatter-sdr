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
from .rtl import RtlPyStream, RtlSdrError


BUFFER_SECONDS = 30.0
FFT_SIZE = 4096
DISPLAY_BINS = 512
MIN_READ_BYTES = 8_192
MAX_FRAME_RATE_HZ = 25.0
RETUNE_FLUSH_SECONDS = 0.08


def stream_read_size(sample_rate_hz: int) -> int:
    """Read about 10 ms in power-of-two blocks favored by librtlsdr/libusb."""
    target = max(MIN_READ_BYTES, sample_rate_hz * 2 / 100)
    return 1 << round(math.log2(target))


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


def mean_power_db(values_db: np.ndarray) -> float:
    """Average logarithmic spectrum bins in linear power space."""
    if values_db.size == 0:
        raise ValueError("at least one spectrum bin is required")
    return float(10.0 * np.log10(np.mean(np.power(10.0, values_db / 10.0))))


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


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """State from one detector update; `completed` still means unconfirmed."""

    completed: bool
    active: bool
    excess_db: float
    baseline_db: float | None
    reference_excess_db: float
    effective_threshold_db: float
    duration_seconds: float | None = None
    peak_excess_db: float | None = None
    peak_level_db: float | None = None
    profile: str | None = None


class CandidateDetector:
    """Profile-aware detector that rejects single-frame and broadband flashes."""

    def __init__(
        self,
        threshold_db: float,
        warmup_frames: int = 125,
        cooldown: float = 15.0,
        start_frames: int = 2,
        release_frames: int = 3,
    ) -> None:
        self.threshold_db = threshold_db
        self.warmup_frames = warmup_frames
        self.cooldown = cooldown
        self.start_frames = start_frames
        self.release_frames = release_frames
        self.history: deque[float] = deque(maxlen=375)
        self.reference_history: deque[float] = deque(maxlen=375)
        self.consecutive = 0
        self.quiet_frames = 0
        self.first_high_at: float | None = None
        self.event_started_at: float | None = None
        self.peak_excess_db = -math.inf
        self.peak_level_db = -math.inf
        self.peak_reference_excess_db = -math.inf
        self.active = False
        self.last_completed = -math.inf

    @property
    def ready(self) -> bool:
        return (
            len(self.history) >= self.warmup_frames
            and len(self.reference_history) >= self.warmup_frames
        )

    def update(
        self,
        level_db: float,
        reference_level_db: float,
        now: float | None = None,
    ) -> DetectionResult:
        now = time.monotonic() if now is None else now
        baseline = float(np.median(self.history)) if self.history else None
        reference_baseline = (
            float(np.median(self.reference_history)) if self.reference_history else None
        )
        excess = 0.0 if baseline is None else level_db - baseline
        reference_excess = (
            0.0 if reference_baseline is None else reference_level_db - reference_baseline
        )
        mad = (
            float(np.median(np.abs(np.asarray(self.history) - baseline)))
            if baseline is not None and self.history
            else 0.0
        )
        effective_threshold = max(self.threshold_db, 6.0 * 1.4826 * mad)
        broadband_limit = max(3.0, self.threshold_db * 0.5)
        discriminated_rise = excess - max(0.0, reference_excess)
        high = (
            self.ready
            and excess >= effective_threshold
            and reference_excess < broadband_limit
            and discriminated_rise >= max(4.0, self.threshold_db * 0.6)
        )

        if not self.active:
            if high and now - self.last_completed >= self.cooldown:
                if self.consecutive == 0:
                    self.first_high_at = now
                self.consecutive += 1
            else:
                self.consecutive = 0
                self.first_high_at = None
            if self.consecutive >= self.start_frames:
                self.active = True
                self.event_started_at = self.first_high_at or now
                self.peak_excess_db = excess
                self.peak_level_db = level_db
                self.peak_reference_excess_db = reference_excess
                self.quiet_frames = 0
                self.consecutive = 0

        completed = False
        duration: float | None = None
        profile: str | None = None
        peak_excess: float | None = None
        peak_level: float | None = None
        if self.active:
            self.peak_excess_db = max(self.peak_excess_db, excess)
            self.peak_level_db = max(self.peak_level_db, level_db)
            self.peak_reference_excess_db = max(
                self.peak_reference_excess_db, reference_excess
            )
            release_level = max(2.0, effective_threshold * 0.35)
            self.quiet_frames = self.quiet_frames + 1 if excess <= release_level else 0
            if self.quiet_frames >= self.release_frames:
                duration = max(0.0, now - (self.event_started_at or now))
                peak_excess = self.peak_excess_db
                peak_level = self.peak_level_db
                profile = "underdense_like" if duration <= 0.7 else "overdense_like"
                completed = (
                    0.08 <= duration <= 60.0
                    and self.peak_reference_excess_db < broadband_limit
                )
                if completed:
                    self.last_completed = now
                self.active = False
                self.event_started_at = None
                self.first_high_at = None
                self.quiet_frames = 0
                self.peak_excess_db = -math.inf
                self.peak_level_db = -math.inf
                self.peak_reference_excess_db = -math.inf

        if not self.active and (not self.ready or excess < self.threshold_db / 2):
            self.history.append(level_db)
            self.reference_history.append(reference_level_db)

        return DetectionResult(
            completed=completed,
            active=self.active,
            excess_db=excess,
            baseline_db=baseline,
            reference_excess_db=reference_excess,
            effective_threshold_db=effective_threshold,
            duration_seconds=duration,
            peak_excess_db=peak_excess,
            peak_level_db=peak_level,
            profile=profile,
        )


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
        source_label: str = "hardware",
    ) -> None:
        self.base_config = base_config.validated()
        self._source_factory = source_factory or RtlPyStream
        self._source_label = source_label
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
        self._receiver_info: dict[str, Any] = {
            "connected": False,
            "display_name": (
                "Meteor SDR simulated receiver"
                if source_label == "simulation"
                else "RTL-SDR not connected"
            ),
            "manufacturer": "",
            "product": "",
            "serial": "",
            "driver_name": "",
        }
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
            "source": self._source_label,
            "receiver": dict(self._receiver_info),
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
        read_size = stream_read_size(config.sample_rate_hz)
        stream: RadioStream | None = None
        discard_bytes = 0
        try:
            stream = self._source_factory(config)
            with self._condition:
                self._stream = stream
                identity_reader = getattr(stream, "identity", None)
                identity = identity_reader() if callable(identity_reader) else {}
                self._receiver_info = {
                    "connected": True,
                    "display_name": identity.get(
                        "display_name",
                        "RTL-SDR receiver"
                        if self._source_label == "hardware"
                        else "Meteor SDR simulated receiver",
                    ),
                    "manufacturer": identity.get("manufacturer", ""),
                    "product": identity.get("product", ""),
                    "serial": identity.get("serial", ""),
                    "driver_name": identity.get("driver_name", ""),
                }
                assert self._settings is not None
                if self._radio_revision != radio_revision:
                    settings = self._settings
                    settings_revision = self._settings_revision
                    radio_revision = self._radio_revision
                    config = settings.config
                    stream.tune(config)
                    ring = RollingIqBuffer(config.sample_rate_hz)
                    read_size = stream_read_size(config.sample_rate_hz)
                    detector = CandidateDetector(settings.threshold_db)
                    discard_bytes = round(
                        config.sample_rate_hz * 2 * RETUNE_FLUSH_SECONDS
                    )
                self._state = "running"
                self._condition.notify_all()
            last_frame_time = 0.0
            while not self._stop.is_set():
                block = stream.read(read_size)
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
                        read_size = stream_read_size(config.sample_rate_hz)
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
                frequencies = config.lower_edge_hz + (
                    np.arange(analyzer.display_bins) + 0.5
                ) * bin_width
                distance = np.abs(frequencies - settings.detection_frequency_hz)
                target_bins = spectrum[distance <= 80_000]
                reference_bins = spectrum[distance >= 120_000]
                if reference_bins.size == 0:
                    reference_bins = spectrum[distance > 80_000]
                level = mean_power_db(target_bins)
                reference_level = mean_power_db(reference_bins)
                detection = detector.update(level, reference_level)
                buffer_seconds = ring.byte_count / (config.sample_rate_hz * 2)
                archived = False
                if detection.completed and ring.fill_ratio >= 0.999:
                    archived = True
                    self._candidate_count += 1
                    self._writer.submit(
                        self._archive,
                        ring.snapshot(),
                        settings,
                        detection,
                    )
                frame = {
                    "sequence": self._sequence + 1,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "spectrum_db": [round(float(value), 1) for value in spectrum],
                    "lower_edge_hz": config.lower_edge_hz,
                    "upper_edge_hz": config.upper_edge_hz,
                    "detection_frequency_hz": settings.detection_frequency_hz,
                    "target_level_db": round(level, 1),
                    "reference_level_db": round(reference_level, 1),
                    "reference_excess_db": round(detection.reference_excess_db, 1),
                    "baseline_db": (
                        None
                        if detection.baseline_db is None
                        else round(detection.baseline_db, 1)
                    ),
                    "excess_db": round(detection.excess_db, 1),
                    "threshold_db": round(detection.effective_threshold_db, 1),
                    "buffer_seconds": round(buffer_seconds, 2),
                    "detector_ready": detector.ready,
                    "detector_active": detection.active,
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
                self._receiver_info["connected"] = False
                if self._source_label == "hardware":
                    self._receiver_info["display_name"] = "RTL-SDR not connected"
                self._condition.notify_all()
        finally:
            if stream is not None:
                stream.close()
            self._stream = None
            with self._condition:
                if self._state != "error":
                    self._state = "stopped"
                self._receiver_info["connected"] = False
                self._condition.notify_all()

    def _archive(
        self,
        data: bytes,
        settings: LiveSettings,
        detection: DetectionResult,
    ) -> None:
        event_dir = self._event_dir()
        event_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        prefix = (
            "simulation_candidate"
            if self._source_label == "simulation"
            else "meteor_candidate"
        )
        stem = f"{prefix}_{now.strftime('%Y%m%dT%H%M%S_%fZ')}_{settings.detection_frequency_hz}Hz"
        data_path = event_dir / f"{stem}.cu8"
        metadata_path = event_dir / f"{stem}.cu8.json"
        preview_path = event_dir / f"{stem}.waterfall.png"
        temporary_path = event_dir / f".{stem}.part"
        temporary_path.write_bytes(data)
        temporary_path.replace(data_path)
        preview_path.write_bytes(render_waterfall_png(data))
        metadata = {
            "classification": (
                "simulated_meteor_candidate"
                if self._source_label == "simulation"
                else "meteor_candidate_unconfirmed"
            ),
            "radio_source": self._source_label,
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
            "trigger_level_db": round(detection.peak_level_db or 0.0, 2),
            "trigger_excess_db": round(detection.peak_excess_db or 0.0, 2),
            "reference_excess_db": round(detection.reference_excess_db, 2),
            "echo_duration_seconds": detection.duration_seconds,
            "echo_profile": detection.profile,
            "sha256": hashlib.sha256(data).hexdigest(),
            "note": (
                "Synthetic training event; this data did not come from an antenna."
                if self._source_label == "simulation"
                else "Automatic candidate only; inspect the waterfall before classifying it as a meteor."
            ),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
