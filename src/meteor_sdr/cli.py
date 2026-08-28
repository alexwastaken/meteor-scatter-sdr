from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import shutil
import sys

from .capture import capture_once
from .config import ConfigurationError, load_config, parse_frequency, parse_gain
from .rtl import RtlSdrError, device_diagnostic


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meteor-sdr",
        description="Monitor RTL-SDR radio data for meteor-scatter experiments.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.5.0")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check rtl-sdr tools and the USB device")
    doctor.set_defaults(handler=_doctor)

    capture = commands.add_parser("capture", help="record one time-bounded .cu8 file")
    capture.add_argument("--config", type=Path, help="TOML configuration file")
    capture.add_argument(
        "--center-frequency",
        type=parse_frequency,
        metavar="HZ",
        help="center frequency, for example 100M or 100000000",
    )
    capture.add_argument(
        "--sample-rate",
        type=parse_frequency,
        metavar="SPS",
        help="complex samples/second, for example 1.024M",
    )
    capture.add_argument(
        "--gain",
        type=parse_gain,
        default=argparse.SUPPRESS,
        metavar="DB|auto",
        help="tuner gain in dB or 'auto'",
    )
    capture.add_argument("--ppm", type=int, help="frequency correction in parts per million")
    capture.add_argument("--device", help="rtl_sdr device index or serial number")
    capture.add_argument("--duration", type=float, help="capture duration in seconds")
    capture.add_argument("--output-dir", type=Path, help="capture directory")
    capture.set_defaults(handler=_capture)

    serve = commands.add_parser("serve", help="open the live spectrum and waterfall")
    serve.add_argument("--config", type=Path, help="TOML configuration file")
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to listen on (default: 127.0.0.1)",
    )
    serve.add_argument("--port", type=int, default=8787, help="port (default: 8787)")
    serve.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the dashboard in the Pi's browser",
    )
    serve.set_defaults(handler=_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (ConfigurationError, RtlSdrError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ncapture interrupted; partial data was retained", file=sys.stderr)
        return 130


def _doctor(_: argparse.Namespace) -> int:
    rtl_sdr = shutil.which("rtl_sdr")
    rtl_tcp = shutil.which("rtl_tcp")
    rtl_test = shutil.which("rtl_test")
    print(f"rtl_sdr: {rtl_sdr or 'not found'}")
    print(f"rtl_tcp: {rtl_tcp or 'not found'}")
    print(f"rtl_test: {rtl_test or 'not found'}")
    if rtl_sdr is None or rtl_tcp is None or rtl_test is None:
        print("Install them with: sudo apt update && sudo apt install rtl-sdr")
        return 1

    healthy, detail = device_diagnostic()
    print(detail)
    if not healthy:
        print(
            "The tools are installed, but the receiver did not pass its device check. "
            "See the README troubleshooting section.",
            file=sys.stderr,
        )
        return 1
    print("RTL-SDR device check passed.")
    return 0


def _capture(args: argparse.Namespace) -> int:
    config = load_config(args.config).with_overrides(
        center_frequency_hz=args.center_frequency,
        sample_rate_hz=args.sample_rate,
        ppm=args.ppm,
        device=args.device,
        duration_seconds=args.duration,
        output_dir=args.output_dir,
    )
    if hasattr(args, "gain"):
        # None deliberately means rtl_sdr automatic gain, so it cannot use the
        # generic override helper (which ignores absent/None values).
        config = replace(config, gain_db=args.gain).validated()
    estimated_mib = config.expected_bytes / (1024 * 1024)
    print(
        f"Capturing {config.duration_seconds:g}s at "
        f"{config.center_frequency_hz / 1_000_000:g} MHz / "
        f"{config.sample_rate_hz / 1_000_000:g} MS/s "
        f"(~{estimated_mib:.1f} MiB)..."
    )
    result = capture_once(config)
    print(f"data:     {result.data_path}")
    print(f"metadata: {result.metadata_path}")
    print(f"sha256:   {result.sha256}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    from .web import serve_dashboard

    if not 0 <= args.port <= 65_535:
        raise ConfigurationError("port must be between 0 and 65535")
    config = load_config(args.config)
    serve_dashboard(
        config=config,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
