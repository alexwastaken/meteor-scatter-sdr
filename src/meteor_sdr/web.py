from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from threading import Timer
from typing import Any
from urllib.parse import unquote, urlparse
import json
import webbrowser

from .config import CaptureConfig, ConfigurationError
from .live import LiveMonitor
from .rtl import RtlSdrError


MAX_REQUEST_BYTES = 16 * 1024


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, monitor: LiveMonitor):
        self.monitor = monitor
        super().__init__(address, DashboardHandler)

    def server_close(self) -> None:
        self.monitor.close()
        super().server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/live/status":
            self._json(HTTPStatus.OK, self.server.monitor.status())
            return
        if path == "/api/live/events":
            self._event_stream()
            return
        if path == "/api/events":
            self._json(HTTPStatus.OK, {"events": self.server.monitor.events()})
            return
        if path.startswith("/api/events/previews/"):
            name = unquote(path.removeprefix("/api/events/previews/"))
            preview = self.server.monitor.preview_path(name)
            if preview is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "preview not found"})
                return
            body = preview.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.css": ("app.css", "text/css; charset=utf-8"),
            "/display.css": ("display.css", "text/css; charset=utf-8"),
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
        path = urlparse(self.path).path
        if path not in {"/api/live/start", "/api/live/stop", "/api/live/tune"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = self._payload() if path in {"/api/live/start", "/api/live/tune"} else {}
            if path == "/api/live/start":
                status = self.server.monitor.start(payload)
            elif path == "/api/live/tune":
                status = self.server.monitor.tune(payload)
            else:
                status = self.server.monitor.stop()
        except RtlSdrError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        except (ConfigurationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.ACCEPTED, status)

    def log_message(self, format: str, *args: object) -> None:
        if args and str(args[1]).startswith("5"):
            super().log_message(format, *args)

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise ConfigurationError("invalid request size")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ConfigurationError("live settings must be an object")
        return payload

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _event_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        sequence = 0
        try:
            while True:
                frame = self.server.monitor.wait_for_frame(sequence, timeout=8)
                if frame is None:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    sequence = int(frame["sequence"])
                    body = json.dumps(frame, separators=(",", ":")).encode("utf-8")
                    self.wfile.write(b"event: frame\ndata: " + body + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def serve_dashboard(
    config: CaptureConfig,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    monitor = LiveMonitor(config)
    server = DashboardServer((host, port), monitor)
    actual_port = server.server_address[1]
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{browser_host}:{actual_port}"
    print(f"Meteor SDR live monitor: {url}")
    if host in {"0.0.0.0", "::"}:
        print("Network access is enabled; use this only on a trusted local network.")
    print("Press Ctrl+C to stop.")
    if open_browser:
        Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nLive monitor stopped.")
    finally:
        server.server_close()
