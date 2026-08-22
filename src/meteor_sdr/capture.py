from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import time

from .config import CaptureConfig
from .rtl import RtlSdr, RtlSdrError


@dataclass(frozen=True, slots=True)
class CaptureResult:
    data_path: Path
    metadata_path: Path
    byte_count: int
    sha256: str


def capture_once(config: CaptureConfig, radio: RtlSdr | None = None) -> CaptureResult:
    config = config.validated()
    radio = radio or RtlSdr()
    output_dir = config.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)
    stem = (
        f"{started_at.strftime('%Y%m%dT%H%M%S.%fZ')}_"
        f"{config.center_frequency_hz}Hz_{config.sample_rate_hz}sps"
    )
    final_path = output_dir / f"{stem}.cu8"
    partial_path = output_dir / f"{stem}.part"
    metadata_path = output_dir / f"{stem}.cu8.json"

    monotonic_start = time.monotonic()
    try:
        process = radio.capture(config, partial_path)
    except BaseException:
        # A partial file is intentionally retained for diagnosis and is ignored by Git.
        raise
    elapsed = time.monotonic() - monotonic_start
    ended_at = datetime.now(timezone.utc)

    if not partial_path.exists():
        raise RtlSdrError("rtl_sdr exited successfully but did not create a capture file")
    byte_count = partial_path.stat().st_size
    if byte_count != config.expected_bytes:
        raise RtlSdrError(
            f"capture size mismatch: expected {config.expected_bytes} bytes, "
            f"received {byte_count}; partial data remains at {partial_path}"
        )

    sha256 = _sha256(partial_path)
    os.replace(partial_path, final_path)

    metadata = {
        "schema_version": 1,
        "format": "cu8",
        "data_file": final_path.name,
        "byte_order": "I,Q interleaved unsigned 8-bit offset binary",
        "bytes_per_complex_sample": 2,
        "center_frequency_hz": config.center_frequency_hz,
        "sample_rate_hz": config.sample_rate_hz,
        "band_edges_hz": [config.lower_edge_hz, config.upper_edge_hz],
        "gain_db": "auto" if config.gain_db is None else config.gain_db,
        "ppm_correction": config.ppm,
        "device": config.device,
        "requested_duration_seconds": config.duration_seconds,
        "elapsed_seconds": round(elapsed, 6),
        "sample_count": config.sample_count,
        "byte_count": byte_count,
        "sha256": sha256,
        "started_at_utc": started_at.isoformat(),
        "ended_at_utc": ended_at.isoformat(),
        "rtl_sdr_command": list(process.command),
        "rtl_sdr_stderr": process.stderr,
    }
    temporary_metadata = metadata_path.with_suffix(metadata_path.suffix + ".part")
    temporary_metadata.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary_metadata, metadata_path)
    return CaptureResult(final_path, metadata_path, byte_count, sha256)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
