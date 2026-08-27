from http.client import HTTPConnection
from threading import Thread
import json
import unittest

from meteor_sdr.config import CaptureConfig
from meteor_sdr.live import LiveMonitor
from meteor_sdr.web import DashboardServer


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

    def test_status_and_static_page(self) -> None:
        self.connection.request("GET", "/api/live/status")
        response = self.connection.getresponse()
        status = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["buffer_target_seconds"], 30.0)

        self.connection.request("GET", "/")
        response = self.connection.getresponse()
        body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("Wideband receiver", body)
        self.assertNotIn("Download ZIP", body)

        self.connection.request("GET", "/display.css")
        response = self.connection.getresponse()
        stylesheet = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn(".retune-overlay[hidden]", stylesheet)

    def test_old_download_api_is_gone(self) -> None:
        self.connection.request("GET", "/api/captures")
        response = self.connection.getresponse()
        response.read()
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
