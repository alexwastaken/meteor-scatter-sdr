from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
import math
import tomllib


FM_LOW_HZ = 87_500_000
FM_HIGH_HZ = 108_000_000
MIN_SAMPLE_RATE_HZ = 250_000
MAX_SAMPLE_RATE_HZ = 2_400_000
MAX_RTL_SDR_SAMPLES = 2_000_000_000


class ConfigurationError(ValueError):
    """Raised when capture settings are invalid."""


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    # Keep the 95.3 MHz research target away from the RTL-SDR center/DC bin.
    center_frequency_hz: int = 95_460_000
    sample_rate_hz: int = 512_000
    # Fixed gain keeps AGC changes from resembling transient meteor echoes.
    gain_db: float | None = 28.0
    ppm: int = 0
    device: str = "0"
    duration_seconds: float = 10.0
    output_dir: Path = Path("captures")

    @property
    def sample_count(self) -> int:
        return round(self.sample_rate_hz * self.duration_seconds)

    @property
    def expected_bytes(self) -> int:
        # rtl_sdr emits one unsigned byte for I and one for Q.
        return self.sample_count * 2

    @property
    def lower_edge_hz(self) -> int:
        return self.center_frequency_hz - self.sample_rate_hz // 2

    @property
    def upper_edge_hz(self) -> int:
        return self.center_frequency_hz + self.sample_rate_hz // 2

    def validated(self) -> "CaptureConfig":
        if not FM_LOW_HZ <= self.center_frequency_hz <= FM_HIGH_HZ:
            raise ConfigurationError(
                f"center frequency must be between {FM_LOW_HZ} and {FM_HIGH_HZ} Hz"
            )
        if not MIN_SAMPLE_RATE_HZ <= self.sample_rate_hz <= MAX_SAMPLE_RATE_HZ:
            raise ConfigurationError(
                f"sample rate must be between {MIN_SAMPLE_RATE_HZ} and "
                f"{MAX_SAMPLE_RATE_HZ} samples/second"
            )
        if self.lower_edge_hz < FM_LOW_HZ or self.upper_edge_hz > FM_HIGH_HZ:
            raise ConfigurationError(
                "the selected center frequency and sample rate extend beyond the "
                "87.5-108 MHz antenna range"
            )
        if not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0:
            raise ConfigurationError("duration must be a positive number of seconds")
        if self.sample_count > MAX_RTL_SDR_SAMPLES:
            raise ConfigurationError(
                "capture is too long for one rtl_sdr file; use a shorter duration"
            )
        if self.gain_db is not None and (
            not math.isfinite(self.gain_db) or not 0 <= self.gain_db <= 49.6
        ):
            raise ConfigurationError("gain must be 'auto' or between 0 and 49.6 dB")
        if not -1_000 <= self.ppm <= 1_000:
            raise ConfigurationError("ppm correction must be between -1000 and 1000")
        if not self.device.strip():
            raise ConfigurationError("device must be an rtl_sdr index or serial number")
        return self

    def with_overrides(self, **values: Any) -> "CaptureConfig":
        filtered = {key: value for key, value in values.items() if value is not None}
        return replace(self, **filtered).validated()


def load_config(path: Path | None) -> CaptureConfig:
    if path is None:
        return CaptureConfig().validated()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"config file does not exist: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"invalid TOML in {path}: {exc}") from exc

    radio = _table(raw, "radio")
    capture = _table(raw, "capture")
    gain = _parse_gain(radio.get("gain_db", "auto"))

    config = CaptureConfig(
        center_frequency_hz=_integer(
            radio.get("center_frequency_hz", 98_500_000), "center_frequency_hz"
        ),
        sample_rate_hz=_integer(
            radio.get("sample_rate_hz", 1_024_000), "sample_rate_hz"
        ),
        gain_db=gain,
        ppm=_integer(radio.get("ppm", 0), "ppm"),
        device=str(radio.get("device", "0")),
        duration_seconds=_number(
            capture.get("duration_seconds", 10.0), "duration_seconds"
        ),
        output_dir=Path(capture.get("output_dir", "captures")),
    )
    return config.validated()


def parse_frequency(value: str) -> int:
    """Parse values such as 100000000, 100M, 1024k, or 1.024e6."""
    normalized = value.strip().lower().replace("hz", "")
    multiplier = 1.0
    if normalized.endswith("m"):
        normalized = normalized[:-1]
        multiplier = 1_000_000.0
    elif normalized.endswith("k"):
        normalized = normalized[:-1]
        multiplier = 1_000.0
    try:
        parsed = float(normalized) * multiplier
    except ValueError as exc:
        raise ConfigurationError(f"invalid frequency or sample rate: {value}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigurationError(f"invalid frequency or sample rate: {value}")
    return round(parsed)


def parse_gain(value: str) -> float | None:
    return _parse_gain(value)


def _parse_gain(value: object) -> float | None:
    if isinstance(value, str) and value.strip().lower() == "auto":
        return None
    if isinstance(value, bool):
        raise ConfigurationError("gain_db must be 'auto' or a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("gain_db must be 'auto' or a number") from exc


def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"[{name}] must be a TOML table")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    return float(value)
