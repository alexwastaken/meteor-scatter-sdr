from html.parser import HTMLParser
from http.client import HTTPConnection
from threading import Thread
import json
import unittest

from meteor_sdr.config import CaptureConfig
from meteor_sdr.live import LiveMonitor
from meteor_sdr.web import DashboardServer


class PageInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.canvases: set[str] = set()
        self.tooltip_ids: set[str] = set()
        self.tooltip_targets: set[str] = set()
        self.buttons: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.ids.add(element_id)
        if tag == "canvas" and element_id:
            self.canvases.add(element_id)
        if tag == "button":
            if element_id:
                self.buttons.add(element_id)
            described_by = values.get("aria-describedby")
            if described_by:
                self.tooltip_targets.add(described_by)
        if values.get("role") == "tooltip" and element_id:
            self.tooltip_ids.add(element_id)


class DashboardHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = LiveMonitor(CaptureConfig())
        self.server = DashboardServer(("127.0.0.1", 0), self.monitor)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)

    def tearDown(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get(self, path: str) -> tuple[int, bytes, dict[str, str]]:
        self.connection.request("GET", path)
        response = self.connection.getresponse()
        body = response.read()
        headers = {name.lower(): value for name, value in response.getheaders()}
        return response.status, body, headers

    def test_status_contract(self) -> None:
        status_code, body, _ = self.get("/api/live/status")
        status = json.loads(body)
        self.assertEqual(status_code, 200)
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["buffer_target_seconds"], 30.0)
        self.assertEqual(status["settings"]["observation_mode"], "meteor")
        self.assertEqual(status["settings"]["sample_rate_hz"], 512_000)

    def test_meteor_dashboard_contract(self) -> None:
        status_code, body_bytes, _ = self.get("/")
        body = body_bytes.decode()
        self.assertEqual(status_code, 200)
        inspector = PageInspector()
        inspector.feed(body)

        self.assertIn('aria-label="Meteor observation workspace"', body)
        self.assertIn("PASSIVE RECEIVER", body)
        self.assertIn("UNCONFIRMED", body.upper())
        self.assertIn("WATCH MARKER · NOT A SIGNAL", body)
        self.assertIn("95.3 MHz", body)
        self.assertIn("KZJH", body)
        self.assertIn("024° true", body)
        self.assertEqual(inspector.canvases, {"power-chart", "waterfall"})
        self.assertTrue({"calibrate-button", "watch-button", "retry-button"} <= inspector.buttons)
        self.assertNotIn("center-frequency", inspector.ids)
        self.assertNotIn("tune-down", inspector.ids)
        self.assertNotIn("tune-up", inspector.ids)
        self.assertNotIn("spectrum", inspector.ids)
        self.assertNotIn("Download ZIP", body)
        self.assertNotIn("FM audio ·", body)
        self.assertEqual(inspector.tooltip_targets, inspector.tooltip_ids)
        self.assertGreaterEqual(len(inspector.tooltip_ids), 5)

    def test_radio_check_is_compact_visual_free_and_isolated(self) -> None:
        _, body_bytes, _ = self.get("/")
        body = body_bytes.decode()
        radio_section = body.split('<details class="panel radio-check"', 1)[1].split("</details>", 1)[0]
        self.assertIn('id="audio-button"', radio_section)
        self.assertIn('aria-pressed="false"', radio_section)
        self.assertIn("candidate saving", radio_section)
        self.assertNotIn("<canvas", radio_section)
        self.assertNotIn("waterfall", radio_section.lower())
        self.assertNotIn("spectrum", radio_section.lower())

        _, script_bytes, _ = self.get("/app.js")
        script = script_bytes.decode()
        self.assertIn('observation_mode: "radio_check"', script)
        self.assertIn('observation_mode: "meteor"', script)
        self.assertIn("Stop audio & restore meteor watch", script)
        self.assertIn("queuedBytes < 8192", script)
        self.assertIn("audioNextTime <= audioContext.currentTime + .005", script)

    def test_static_assets_are_served_without_cache(self) -> None:
        for path, content_type in (
            ("/app.css", "text/css; charset=utf-8"),
            ("/app.js", "text/javascript; charset=utf-8"),
            ("/assets/meteor-scatter-path.jpg", "image/jpeg"),
        ):
            status_code, body, headers = self.get(path)
            self.assertEqual(status_code, 200)
            self.assertGreater(len(body), 100)
            self.assertEqual(headers["content-type"], content_type)
            self.assertEqual(headers["cache-control"], "no-store")

    def test_old_download_api_is_gone(self) -> None:
        for path in ("/api/captures", "/api/download", "/api/captures.zip"):
            status_code, _, _ = self.get(path)
            self.assertEqual(status_code, 404)

    def test_audio_stream_requires_a_running_receiver(self) -> None:
        status_code, body, _ = self.get("/api/live/audio")
        payload = json.loads(body)
        self.assertEqual(status_code, 409)
        self.assertEqual(payload["error"], "receiver is not running")


if __name__ == "__main__":
    unittest.main()
