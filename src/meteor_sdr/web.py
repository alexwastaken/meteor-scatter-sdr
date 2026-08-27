from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from threading import RLock, Thread, Timer
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse
import json
import tempfile
import time
import traceback
import uuid
import webbrowser
import zipfile

from .capture import CaptureResult, capture_once
from .config import CaptureConfig, ConfigurationError
from .rtl import RtlSdrError


MAX_REQUEST_BYTES = 16 * 1024
CaptureFunction = Callable[[CaptureConfig], CaptureResult]


class CaptureBusyError(RuntimeError):
    """Raised when a second capture is requested while one is running."""


class CaptureNotFoundError(FileNotFoundError):
    """Raised when a requested capture is unavailable."""


@dataclass(slots=True)
class CaptureJob:
    job_id: str
    config: CaptureConfig
    started_at_utc: str
    started_monotonic: float
    state: str = "capturing"
    result: CaptureResult | None = None
    error: str | None = None
    finished_at_utc: str | None = None

    def snapshot(self) -> dict[str, Any]:
        elapsed = max(0.0, time.monotonic() - self.started_monotonic)
        if self.state == "capturing":
            progress = min(0.99, elapsed / self.config.duration_seconds)
        else:
            progress = 1.0
        payload: dict[str, Any] = {
            "id": self.job_id,
            "state": self.state,
            "progress": round(progress, 4),
            "elapsed_seconds": round(elapsed, 2),
            "duration_seconds": self.config.duration_seconds,
            "center_frequency_hz": self.config.center_frequency_hz,
            "sample_rate_hz": self.config.sample_rate_hz,
            "gain_db": "auto" if self.config.gain_db is None else self.config.gain_db,
            "expected_bytes": self.config.expected_bytes,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "error": self.error,
        }
        if self.result is not None:
            payload["result"] = {
                "data_file": self.result.data_path.name,
                "metadata_file": self.result.metadata_path.name,
                "byte_count": self.result.byte_count,
                "sha256": self.result.sha256,
            }
        return payload


class CaptureManager:
    def __init__(
        self,
        base_config: CaptureConfig,
        capture_function: CaptureFunction = capture_once,
    ) -> None:
        self.base_config = base_config.validated()
        self.capture_function = capture_function
        self._lock = RLock()
        self._job: CaptureJob | None = None

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = config_from_payload(self.base_config, payload)
        with self._lock:
            if self._job is not None and self._job.state == "capturing":
                raise CaptureBusyError("a capture is already running")
            job = CaptureJob(
                job_id=uuid.uuid4().hex,
                config=config,
                started_at_utc=datetime.now(timezone.utc).isoformat(),
                started_monotonic=time.monotonic(),
            )
            self._job = job
            worker = Thread(target=self._run, args=(job,), daemon=True)
            worker.start()
            return job.snapshot()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._job.snapshot() if self._job is not None else None,
                "defaults": public_config(self.base_config),
            }

    def captures(self) -> list[dict[str, Any]]:
        output_dir = self.base_config.output_dir.expanduser().resolve()
        if not output_dir.exists():
            return []
        captures: list[dict[str, Any]] = []
        for metadata_path in output_dir.glob("*.cu8.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                data_file = metadata["data_file"]
                data_path = safe_capture_path(output_dir, data_file, ".cu8")
                if not data_path.is_file():
                    continue
                captures.append(
                    {
                        "id": data_file,
                        "data_file": data_file,
                        "metadata_file": metadata_path.name,
                        "byte_count": data_path.stat().st_size,
                        "center_frequency_hz": metadata.get("center_frequency_hz"),
                        "sample_rate_hz": metadata.get("sample_rate_hz"),
                        "gain_db": metadata.get("gain_db"),
                        "started_at_utc": metadata.get("started_at_utc"),
                        "duration_seconds": metadata.get("requested_duration_seconds"),
                        "download_url": (
                            f"/api/captures/{quote(data_file, safe='')}/download"
                        ),
                    }
                )
            except (KeyError, OSError, ValueError, TypeError):
                continue
        captures.sort(key=lambda item: item.get("started_at_utc") or "", reverse=True)
        return captures

    def _run(self, job: CaptureJob) -> None:
        try:
            result = self.capture_function(job.config)
        except (RtlSdrError, ConfigurationError, OSError) as exc:
            with self._lock:
                job.state = "error"
                job.error = str(exc)
                job.finished_at_utc = datetime.now(timezone.utc).isoformat()
        except Exception:
            traceback.print_exc()
            with self._lock:
                job.state = "error"
                job.error = "capture failed unexpectedly; check the terminal for details"
                job.finished_at_utc = datetime.now(timezone.utc).isoformat()
        else:
            with self._lock:
                job.state = "complete"
                job.result = result
                job.finished_at_utc = datetime.now(timezone.utc).isoformat()


def config_from_payload(base: CaptureConfig, payload: dict[str, Any]) -> CaptureConfig:
    allowed = {
        "center_frequency_hz",
        "sample_rate_hz",
        "gain_db",
        "duration_seconds",
    }
    unexpected = set(payload) - allowed
    if unexpected:
        raise ConfigurationError(f"unsupported capture setting: {sorted(unexpected)[0]}")

    center = _optional_integer(payload, "center_frequency_hz")
    sample_rate = _optional_integer(payload, "sample_rate_hz")
    duration = _optional_number(payload, "duration_seconds")
    gain_value = payload.get("gain_db", base.gain_db)
    if gain_value is None or (
        isinstance(gain_value, str) and gain_value.lower() == "auto"
    ):
        gain: float | None = None
    elif isinstance(gain_value, bool) or not isinstance(gain_value, (int, float)):
        raise ConfigurationError("gain must be 'auto' or a number")
    else:
        gain = float(gain_value)

    return replace(
        base,
        center_frequency_hz=(center if center is not None else base.center_frequency_hz),
        sample_rate_hz=(sample_rate if sample_rate is not None else base.sample_rate_hz),
        duration_seconds=(duration if duration is not None else base.duration_seconds),
        gain_db=gain,
    ).validated()


def public_config(config: CaptureConfig) -> dict[str, Any]:
    return {
        "center_frequency_hz": config.center_frequency_hz,
        "sample_rate_hz": config.sample_rate_hz,
        "gain_db": "auto" if config.gain_db is None else config.gain_db,
        "duration_seconds": config.duration_seconds,
        "expected_bytes": config.expected_bytes,
        "output_dir": str(config.output_dir.expanduser().resolve()),
    }


def build_capture_zip(output_dir: Path, data_file: str):
    output_dir = output_dir.expanduser().resolve()
    data_path = safe_capture_path(output_dir, data_file, ".cu8")
    metadata_path = safe_capture_path(output_dir, f"{data_file}.json", ".cu8.json")
    if not data_path.is_file() or not metadata_path.is_file():
        raise CaptureNotFoundError(data_file)

    archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(data_path, data_path.name, compress_type=zipfile.ZIP_STORED)
        bundle.write(
            metadata_path,
            metadata_path.name,
            compress_type=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        )
    size = archive.tell()
    archive.seek(0)
    archive_name = f"{data_path.stem}.zip"
    return archive, size, archive_name


def safe_capture_path(output_dir: Path, name: str, suffix: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(suffix):
        raise CaptureNotFoundError(str(name))
    candidate = (output_dir / name).resolve()
    if candidate.parent != output_dir:
        raise CaptureNotFoundError(name)
    return candidate


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager: CaptureManager):
        self.manager = manager
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json(HTTPStatus.OK, self.server.manager.status())
            return
        if path == "/api/captures":
            self._json(HTTPStatus.OK, {"captures": self.server.manager.captures()})
            return
        if path.startswith("/api/captures/") and path.endswith("/download"):
            encoded_name = path[len("/api/captures/") : -len("/download")]
            self._download(unquote(encoded_name.rstrip("/")))
            return
        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.css": ("app.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        asset = static_files.get(path)
        if asset is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        name, content_type = asset
        body = files("meteor_sdr").joinpath("web").joinpath(name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/captures":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ConfigurationError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ConfigurationError("capture settings must be an object")
            job = self.server.manager.start(payload)
        except CaptureBusyError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        except (ConfigurationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.ACCEPTED, {"job": job})

    def log_message(self, format: str, *args: object) -> None:
        # Keep routine polling out of the Pi terminal; surface server errors only.
        if args and str(args[1]).startswith("5"):
            super().log_message(format, *args)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _download(self, data_file: str) -> None:
        try:
            archive, size, archive_name = build_capture_zip(
                self.server.manager.base_config.output_dir, data_file
            )
        except CaptureNotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "capture not found"})
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition", f'attachment; filename="{archive_name}"'
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            while block := archive.read(1024 * 1024):
                self.wfile.write(block)
        finally:
            archive.close()


def serve_dashboard(
    config: CaptureConfig,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    manager = CaptureManager(config)
    server = DashboardServer((host, port), manager)
    actual_port = server.server_address[1]
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{browser_host}:{actual_port}"
    print(f"Meteor SDR dashboard: {url}")
    if host in {"0.0.0.0", "::"}:
        print("Network access is enabled; use this only on a trusted local network.")
    print("Press Ctrl+C to stop.")
    if open_browser:
        Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


def _optional_integer(payload: dict[str, Any], name: str) -> int | None:
    if name not in payload:
        return None
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    return value


def _optional_number(payload: dict[str, Any], name: str) -> float | None:
    if name not in payload:
        return None
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    return float(value)
