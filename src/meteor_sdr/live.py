from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Condition, Event, Lock, RLock, Thread
from typing import Any, Callable, Protocol
import hashlib
import json
import math
import shutil
import time

import numpy as np

from .config import CaptureConfig, ConfigurationError
from .rtl import RtlPyStream, RtlSdrError


BUFFER_SECONDS = 30.0
FFT_SIZE = 4096
DISPLAY_BINS = 512
MIN_READ_BYTES = 8_192
DETECTOR_RATE_HZ = 50.0
DISPLAY_FRAME_RATE_HZ = 12.5
RETUNE_FLUSH_SECONDS = 0.08
AUDIO_SAMPLE_RATE_HZ = 32_000
AUDIO_RF_RATE_HZ = 256_000
AUDIO_DEEMPHASIS_SECONDS = 75e-6
AUDIO_LOWPASS_HZ = 12_500
AUDIO_FILTER_TAPS = 129


def stream_read_size(sample_rate_hz: int) -> int:
    """Read about 10 ms in power-of-two blocks favored by librtlsdr/libusb."""
    target = max(MIN_READ_BYTES, sample_rate_hz * 2 / 100)
    return 1 << round(math.log2(target))


class RadioStream(Protocol):
    def read(self, size: int) -> bytes | None: ...

    def tune(self, config: CaptureConfig) -> None: ...

    def stderr_detail(self) -> str: ...

    def close(self) -> None: ...


class FmAudioDemodulator:
    """Low-overhead mono wide-FM demodulator for the watched frequency."""

    def __init__(
        self,
        sample_rate_hz: int,
        frequency_offset_hz: int = 0,
        output_rate_hz: int = AUDIO_SAMPLE_RATE_HZ,
    ) -> None:
        self.sample_rate_hz = sample_rate_hz
        self.frequency_offset_hz = frequency_offset_hz
        self.output_rate_hz = output_rate_hz
        self.rf_decimation = max(1, round(sample_rate_hz / AUDIO_RF_RATE_HZ))
        self.rf_rate_hz = sample_rate_hz / self.rf_decimation
        self.audio_decimation = max(1, round(self.rf_rate_hz / output_rate_hz))
        self.intermediate_audio_rate_hz = self.rf_rate_hz / self.audio_decimation
        positions = np.arange(AUDIO_FILTER_TAPS) - (AUDIO_FILTER_TAPS - 1) / 2
        normalized_cutoff = AUDIO_LOWPASS_HZ / self.rf_rate_hz
        taps = (
            2.0
            * normalized_cutoff
            * np.sinc(2.0 * normalized_cutoff * positions)
            * np.hamming(AUDIO_FILTER_TAPS)
        )
        self._audio_filter_taps = (taps / taps.sum()).astype(np.float32)
        self._audio_filter_state = np.zeros(AUDIO_FILTER_TAPS - 1, dtype=np.float32)
        self._audio_decimation_position = 0
        self._mixer_phase = 0.0
        self._previous_iq: complex | None = None
        self._deemphasis = 0.0
        self._highpass_previous_input = 0.0
        self._highpass_previous_output = 0.0

    def process(self, iq_bytes: bytes) -> bytes:
        raw = np.frombuffer(iq_bytes, dtype=np.uint8)
        if raw.size < self.rf_decimation * 4:
            return b""
        if raw.size % 2:
            raw = raw[:-1]
        values = raw.astype(np.float32)
        samples = (values[0::2] - 127.5) + 1j * (values[1::2] - 127.5)

        if self.frequency_offset_hz:
            phase_step = -2.0 * math.pi * self.frequency_offset_hz / self.sample_rate_hz
            phases = self._mixer_phase + phase_step * np.arange(samples.size)
            samples *= np.exp(1j * phases)
            self._mixer_phase = float(
                (self._mixer_phase + phase_step * samples.size) % (2.0 * math.pi)
            )

        usable = samples.size - samples.size % self.rf_decimation
        baseband = samples[:usable].reshape(-1, self.rf_decimation).mean(axis=1)
        if self._previous_iq is not None:
            baseband = np.concatenate(
                (np.asarray([self._previous_iq], dtype=np.complex64), baseband)
            )
        self._previous_iq = complex(baseband[-1])
        discriminator = np.angle(baseband[1:] * np.conj(baseband[:-1]))
        if discriminator.size <= 0:
            return b""
        filter_input = np.concatenate(
            (self._audio_filter_state, discriminator.astype(np.float32, copy=False))
        )
        filtered_discriminator = np.convolve(
            filter_input, self._audio_filter_taps, mode="valid"
        )
        self._audio_filter_state = filter_input[-(AUDIO_FILTER_TAPS - 1) :]
        first_output = (-self._audio_decimation_position) % self.audio_decimation
        audio = filtered_discriminator[first_output :: self.audio_decimation]
        self._audio_decimation_position = (
            self._audio_decimation_position + filtered_discriminator.size
        ) % self.audio_decimation
        if audio.size <= 0:
            return b""

        if not math.isclose(self.intermediate_audio_rate_hz, self.output_rate_hz):
            output_count = max(
                1,
                round(
                    audio.size
                    * self.output_rate_hz
                    / self.intermediate_audio_rate_hz
                ),
            )
            source_positions = np.linspace(0.0, 1.0, audio.size, endpoint=False)
            output_positions = np.linspace(0.0, 1.0, output_count, endpoint=False)
            audio = np.interp(output_positions, source_positions, audio)

        deviation_scale = max(
            1e-6, 2.0 * math.pi * 75_000.0 / self.rf_rate_hz
        )
        audio = audio / deviation_scale
        deemphasis_alpha = 1.0 / (
            1.0 + AUDIO_DEEMPHASIS_SECONDS * self.output_rate_hz
        )
        highpass_decay = math.exp(-2.0 * math.pi * 30.0 / self.output_rate_hz)
        filtered = np.empty(audio.size, dtype=np.float32)
        for index, value in enumerate(audio):
            self._deemphasis += deemphasis_alpha * (float(value) - self._deemphasis)
            highpass = (
                self._deemphasis
                - self._highpass_previous_input
                + highpass_decay * self._highpass_previous_output
            )
            self._highpass_previous_input = self._deemphasis
            self._highpass_previous_output = highpass
            filtered[index] = highpass
        pcm = np.clip(filtered * 0.8, -1.0, 1.0)
        return (pcm * 32767.0).astype("<i2").tobytes()


@dataclass(frozen=True, slots=True)
class LiveSettings:
    config: CaptureConfig
    detection_frequency_hz: int
    threshold_db: float = 8.0
    observation_mode: str = "meteor"

    def validated(self) -> "LiveSettings":
        config = self.config.validated()
        if not config.lower_edge_hz <= self.detection_frequency_hz <= config.upper_edge_hz:
            raise ConfigurationError("detection frequency must be inside the displayed span")
        if not math.isfinite(self.threshold_db) or not 3 <= self.threshold_db <= 30:
            raise ConfigurationError("detector threshold must be between 3 and 30 dB")
        if self.observation_mode not in {"meteor", "radio_check"}:
            raise ConfigurationError("observation mode must be 'meteor' or 'radio_check'")
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

    def snapshot_chunks(self) -> tuple[bytes, ...]:
        """Freeze byte references without copying the full RF buffer on the reader."""
        return tuple(self._chunks)


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
    peak_reference_excess_db: float | None = None
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

    @property
    def readiness(self) -> float:
        return min(
            1.0,
            min(len(self.history), len(self.reference_history)) / self.warmup_frames,
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
        peak_reference_excess: float | None = None
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
                peak_reference_excess = self.peak_reference_excess_db
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
            peak_reference_excess_db=peak_reference_excess,
            profile=profile,
        )


def settings_from_payload(base: CaptureConfig, payload: dict[str, Any]) -> LiveSettings:
    allowed = {
        "center_frequency_hz",
        "sample_rate_hz",
        "gain_db",
        "detection_frequency_hz",
        "threshold_db",
        "observation_mode",
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
    observation_mode = payload.get("observation_mode", "meteor")
    if not isinstance(observation_mode, str):
        raise ConfigurationError("observation_mode must be a string")

    config = replace(
        base,
        center_frequency_hz=integer("center_frequency_hz", base.center_frequency_hz),
        sample_rate_hz=integer("sample_rate_hz", base.sample_rate_hz),
        gain_db=gain,
    ).validated()
    detection = integer("detection_frequency_hz", config.center_frequency_hz)
    return LiveSettings(
        config,
        detection,
        float(threshold),
        observation_mode,
    ).validated()


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
        self._tune_lock = Lock()
        self._retuning = False
        self._state = "stopped"
        self._error: str | None = None
        self._sequence = 0
        self._frame: dict[str, Any] | None = None
        self._audio_sequence = 0
        self._audio_chunk: bytes | None = None
        self._audio_clients = 0
        self._buffer_seconds = 0.0
        self._candidate_count = 0
        self._started_at: str | None = None
        self._receiver_info: dict[str, Any] = {
            "connected": False,
            "detected": False,
            "error_stage": None,
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
            self._retuning = False
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
            self._retuning = False
            self._condition.notify_all()
            return self.status_locked()

    def tune(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Retune the persistent rtl_tcp source without restarting the receiver."""
        settings = settings_from_payload(self.base_config, payload)
        with self._tune_lock:
            with self._condition:
                if self._state not in {"starting", "running"}:
                    raise RtlSdrError("start monitoring before using live tuning")
                stream = self._stream
                previous_settings = self._settings
                self._retuning = True
                self._condition.notify_all()
            try:
                if stream is not None:
                    stream.tune(settings.config)
            except Exception as exc:
                rollback_error: Exception | None = None
                if stream is not None and previous_settings is not None:
                    try:
                        stream.tune(previous_settings.config)
                    except Exception as rollback_exc:
                        rollback_error = rollback_exc
                with self._condition:
                    self._retuning = False
                    if rollback_error is not None:
                        self._state = "error"
                        self._error = (
                            f"receiver tuning failed ({exc}); restoring the previous "
                            f"settings also failed ({rollback_error})"
                        )
                        self._receiver_info["connected"] = False
                        self._receiver_info["error_stage"] = "tuning"
                        self._stop.set()
                    else:
                        # The hardware crossed a tuning boundary before the
                        # rollback. Rebuild analysis state and discard the
                        # first post-retune bytes even though settings are back
                        # where they started.
                        self._radio_revision += 1
                        self._buffer_seconds = 0.0
                        self._audio_chunk = None
                    self._condition.notify_all()
                if rollback_error is not None:
                    if stream is not None:
                        stream.close()
                    raise RtlSdrError(self._error or "receiver tuning failed") from exc
                raise
            with self._condition:
                if self._state not in {"starting", "running"}:
                    self._retuning = False
                    self._condition.notify_all()
                    raise RtlSdrError("receiver stopped while it was being tuned")
                self._settings = settings
                self._settings_revision += 1
                self._radio_revision += 1
                self._buffer_seconds = 0.0
                self._audio_chunk = None
                self._retuning = False
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
            "audio": {
                "available": self._audio_available_locked(),
                "sample_rate_hz": AUDIO_SAMPLE_RATE_HZ,
                "listeners": self._audio_clients,
            },
            "settings": {
                "center_frequency_hz": config.center_frequency_hz,
                "sample_rate_hz": config.sample_rate_hz,
                "gain_db": "auto" if config.gain_db is None else config.gain_db,
                "detection_frequency_hz": (
                    settings.detection_frequency_hz if settings else config.center_frequency_hz
                ),
                "threshold_db": settings.threshold_db if settings else 8.0,
                "observation_mode": (
                    settings.observation_mode if settings else "meteor"
                ),
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

    def subscribe_audio(self) -> int:
        with self._condition:
            if not self._audio_available_locked():
                raise RtlSdrError(
                    "receiver audio is available only while speaker mode is active"
                )
            self._audio_clients += 1
            return self._audio_sequence

    def unsubscribe_audio(self) -> None:
        with self._condition:
            self._audio_clients = max(0, self._audio_clients - 1)

    def wait_for_audio(
        self, after_sequence: int, timeout: float = 2.0
    ) -> tuple[int, bytes] | None:
        with self._condition:
            self._condition.wait_for(
                lambda: self._audio_sequence > after_sequence
                or not self._audio_available_locked(),
                timeout=timeout,
            )
            if not self._audio_available_locked():
                return None
            if self._audio_sequence > after_sequence and self._audio_chunk is not None:
                return self._audio_sequence, self._audio_chunk
            return None

    def _audio_available_locked(self) -> bool:
        return (
            self._state == "running"
            and not self._retuning
            and self._settings is not None
            and self._settings.observation_mode == "radio_check"
        )

    def _publish_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._condition:
            if not self._audio_available_locked() or not self._audio_clients:
                return
            self._audio_sequence += 1
            self._audio_chunk = pcm
            self._condition.notify_all()

    def events(self, limit_per_source: int = 25) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        event_dir = self._event_dir()
        if not event_dir.exists():
            return result
        for path in event_dir.glob("*.cu8.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                data_path = event_dir / item["data_file"]
                if data_path.is_file():
                    # Older metadata may reference a PNG that this version no
                    # longer creates or serves. Keep the on-disk record intact,
                    # but do not expose the sunset image feature through the API.
                    item.pop("preview_file", None)
                    item["byte_count"] = data_path.stat().st_size
                    result.append(item)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        result.sort(key=lambda item: item.get("triggered_at_utc", ""), reverse=True)
        limit_per_source = max(1, limit_per_source)
        physical = [item for item in result if item.get("radio_source") != "simulation"]
        training = [item for item in result if item.get("radio_source") == "simulation"]
        recent = physical[:limit_per_source] + training[:limit_per_source]
        recent.sort(key=lambda item: item.get("triggered_at_utc", ""), reverse=True)
        return recent

    def storage_status(self) -> dict[str, int]:
        event_dir = self._event_dir()
        try:
            event_dir.mkdir(parents=True, exist_ok=True)
            raw_files = list(event_dir.glob("*.cu8"))
            used_bytes = sum(path.stat().st_size for path in raw_files)
            free_bytes = shutil.disk_usage(event_dir).free
        except OSError:
            return {"event_count": 0, "used_bytes": 0, "free_bytes": 0}
        return {
            "event_count": len(raw_files),
            "used_bytes": used_bytes,
            "free_bytes": free_bytes,
        }

    def _event_dir(self) -> Path:
        return self.base_config.output_dir.expanduser() / "events"

    def _run(self) -> None:
        with self._condition:
            assert self._settings is not None
            settings = self._settings
            settings_revision = self._settings_revision
            radio_revision = self._radio_revision
        config = settings.config
        analyzer = SpectrumAnalyzer()
        detector = CandidateDetector(settings.threshold_db)
        audio_demodulator = FmAudioDemodulator(
            config.sample_rate_hz,
            settings.detection_frequency_hz - config.center_frequency_hz,
        )
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
                    "detected": True,
                    "error_stage": None,
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
                    audio_demodulator = FmAudioDemodulator(
                        config.sample_rate_hz,
                        settings.detection_frequency_hz - config.center_frequency_hz,
                    )
                    discard_bytes = round(
                        config.sample_rate_hz * 2 * RETUNE_FLUSH_SECONDS
                    )
                self._state = "running"
                self._condition.notify_all()
            last_analysis_time = 0.0
            last_publish_time = 0.0
            while not self._stop.is_set():
                block = stream.read(read_size)
                if block is None:
                    continue
                if block == b"":
                    break
                with self._condition:
                    if self._retuning:
                        continue
                    if self._radio_revision != radio_revision:
                        assert self._settings is not None
                        settings = self._settings
                        settings_revision = self._settings_revision
                        radio_revision = self._radio_revision
                        config = settings.config
                        ring = RollingIqBuffer(config.sample_rate_hz)
                        read_size = stream_read_size(config.sample_rate_hz)
                        detector = CandidateDetector(settings.threshold_db)
                        audio_demodulator = FmAudioDemodulator(
                            config.sample_rate_hz,
                            settings.detection_frequency_hz
                            - config.center_frequency_hz,
                        )
                        discard_bytes = round(
                            config.sample_rate_hz * 2 * RETUNE_FLUSH_SECONDS
                        )
                        last_analysis_time = 0.0
                        last_publish_time = 0.0
                    elif self._settings_revision != settings_revision:
                        assert self._settings is not None
                        settings = self._settings
                        settings_revision = self._settings_revision
                        detector = CandidateDetector(settings.threshold_db)
                        audio_demodulator = FmAudioDemodulator(
                            config.sample_rate_hz,
                            settings.detection_frequency_hz
                            - config.center_frequency_hz,
                        )
                    block_radio_revision = radio_revision
                if discard_bytes:
                    discard_bytes = max(0, discard_bytes - len(block))
                    continue
                ring.append(block)
                if settings.observation_mode == "radio_check" and self._audio_clients:
                    self._publish_audio(audio_demodulator.process(block))
                if len(block) < FFT_SIZE * 2:
                    continue
                frame_time = time.monotonic()
                if frame_time - last_analysis_time < 1 / DETECTOR_RATE_HZ:
                    continue
                last_analysis_time = frame_time
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
                if settings.observation_mode == "meteor":
                    detection = detector.update(level, reference_level)
                else:
                    detection = DetectionResult(
                        completed=False,
                        active=False,
                        excess_db=0.0,
                        baseline_db=None,
                        reference_excess_db=0.0,
                        effective_threshold_db=settings.threshold_db,
                    )
                with self._condition:
                    # A retune may finish while spectrum analysis is in flight.
                    # Never publish or archive that now-stale block under the old
                    # observation mode.
                    if (
                        self._retuning
                        or self._radio_revision != block_radio_revision
                    ):
                        continue
                    buffer_seconds = ring.byte_count / (config.sample_rate_hz * 2)
                    archived = False
                    if (
                        settings.observation_mode == "meteor"
                        and detection.completed
                        and ring.fill_ratio >= 0.999
                    ):
                        archived = True
                        self._candidate_count += 1
                        self._writer.submit(
                            self._archive,
                            ring.snapshot_chunks(),
                            settings,
                            detection,
                        )
                    if (
                        not archived
                        and frame_time - last_publish_time
                        < 1 / DISPLAY_FRAME_RATE_HZ
                    ):
                        continue
                    last_publish_time = frame_time
                    frame = {
                        "sequence": self._sequence + 1,
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "spectrum_db": [
                            round(float(value), 1) for value in spectrum
                        ],
                        "lower_edge_hz": config.lower_edge_hz,
                        "upper_edge_hz": config.upper_edge_hz,
                        "detection_frequency_hz": settings.detection_frequency_hz,
                        "target_level_db": round(level, 1),
                        "reference_level_db": round(reference_level, 1),
                        "reference_excess_db": round(
                            detection.reference_excess_db, 1
                        ),
                        "baseline_db": (
                            None
                            if detection.baseline_db is None
                            else round(detection.baseline_db, 1)
                        ),
                        "excess_db": round(detection.excess_db, 1),
                        "threshold_db": round(
                            detection.effective_threshold_db, 1
                        ),
                        "buffer_seconds": round(buffer_seconds, 2),
                        "detector_ready": detector.ready,
                        "detector_readiness": round(detector.readiness, 3),
                        "detector_active": detection.active,
                        "observation_mode": settings.observation_mode,
                        "archived": archived,
                    }
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
                self._retuning = False
                self._state = "error"
                self._error = str(exc)
                detected_receiver = (
                    exc.receiver_info if isinstance(exc, RtlSdrError) else None
                )
                if self._source_label == "hardware" and detected_receiver:
                    self._receiver_info = {
                        **detected_receiver,
                        "connected": False,
                        "detected": True,
                        "error_stage": exc.error_stage,
                    }
                else:
                    self._receiver_info["connected"] = False
                    self._receiver_info["detected"] = False
                    self._receiver_info["error_stage"] = (
                        exc.error_stage if isinstance(exc, RtlSdrError) else None
                    )
                if self._source_label == "hardware" and not detected_receiver:
                    self._receiver_info["display_name"] = "RTL-SDR not connected"
                self._condition.notify_all()
        finally:
            if stream is not None:
                stream.close()
            self._stream = None
            with self._condition:
                self._retuning = False
                if self._state != "error":
                    self._state = "stopped"
                    self._receiver_info["connected"] = False
                self._condition.notify_all()

    def _archive(
        self,
        data: bytes | tuple[bytes, ...],
        settings: LiveSettings,
        detection: DetectionResult,
    ) -> None:
        if not isinstance(data, bytes):
            data = b"".join(data)
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
        temporary_path = event_dir / f".{stem}.part"
        temporary_path.write_bytes(data)
        temporary_path.replace(data_path)
        with self._condition:
            receiver = dict(self._receiver_info)
        metadata = {
            "classification": (
                "simulated_meteor_candidate"
                if self._source_label == "simulation"
                else "meteor_candidate_unconfirmed"
            ),
            "radio_source": self._source_label,
            "data_file": data_path.name,
            "triggered_at_utc": now.isoformat(),
            "echo_end_utc": now.isoformat(),
            "echo_start_utc": (
                now - timedelta(seconds=detection.duration_seconds or 0.0)
            ).isoformat(),
            "buffer_duration_seconds": len(data) / (settings.config.sample_rate_hz * 2),
            "center_frequency_hz": settings.config.center_frequency_hz,
            "sample_rate_hz": settings.config.sample_rate_hz,
            "lower_edge_hz": settings.config.lower_edge_hz,
            "upper_edge_hz": settings.config.upper_edge_hz,
            "detection_frequency_hz": settings.detection_frequency_hz,
            "gain_db": "auto" if settings.config.gain_db is None else settings.config.gain_db,
            "ppm": settings.config.ppm,
            "sample_format": "cu8_interleaved_iq",
            "receiver": receiver,
            "detector_threshold_db": settings.threshold_db,
            "observation_mode": settings.observation_mode,
            "trigger_level_db": round(detection.peak_level_db or 0.0, 2),
            "trigger_excess_db": round(detection.peak_excess_db or 0.0, 2),
            "reference_excess_db": round(
                detection.peak_reference_excess_db
                if detection.peak_reference_excess_db is not None
                else detection.reference_excess_db,
                2,
            ),
            "echo_duration_seconds": detection.duration_seconds,
            "echo_profile": detection.profile,
            "observer": {
                "location_name": "Carmel Valley, San Diego, California",
                "antenna_type": "Yagi",
                "polarization": "horizontal",
                "antenna_gain_db": 7.0,
            },
            "suggested_target": (
                {
                    "call_sign": "KZJH",
                    "frequency_hz": 95_300_000,
                    "transmitter_location": "Jackson, Wyoming",
                    "licensed_erp_kw": 100.0,
                    "approximate_distance_km": 1298,
                    "approximate_bearing_true_degrees": 24,
                    "status": "field_test_required",
                }
                if settings.detection_frequency_hz == 95_300_000
                else None
            ),
            "sha256": hashlib.sha256(data).hexdigest(),
            "note": (
                "Synthetic training event; this data did not come from an antenna."
                if self._source_label == "simulation"
                else "Automatic candidate only; review the raw I/Q and detector metadata before classifying it as a meteor."
            ),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
