from html.parser import HTMLParser
from http.client import HTTPConnection
from threading import Thread
from unittest.mock import patch
import json
import re
import unittest

from meteor_sdr.config import CaptureConfig
from meteor_sdr.live import LiveMonitor
from meteor_sdr.web import DashboardServer


def javascript_function(script: str, name: str) -> str:
    """Return one classic function declaration without depending on a JS parser."""
    marker = f"function {name}("
    start = script.index(marker)
    opening_brace = script.index("{", start)
    depth = 0
    for index in range(opening_brace, len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[start : index + 1]
    raise AssertionError(f"Unclosed JavaScript function: {name}")


class PageInspector(HTMLParser):
    VOID_ELEMENTS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.canvases: set[str] = set()
        self.tooltip_ids: set[str] = set()
        self.tooltip_targets: set[str] = set()
        self.buttons: set[str] = set()
        self.elements: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.element_text: dict[str, list[str]] = {}
        self._open_elements: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.ids.add(element_id)
            self.elements[element_id] = (tag, values)
            self.element_text[element_id] = []
        if tag == "canvas" and element_id:
            self.canvases.add(element_id)
        if tag == "button":
            if element_id:
                self.buttons.add(element_id)
            described_by = values.get("aria-describedby")
            classes = (values.get("class") or "").split()
            if described_by and "info-trigger" in classes:
                self.tooltip_targets.add(described_by)
        if values.get("role") == "tooltip" and element_id:
            self.tooltip_ids.add(element_id)
        if tag not in self.VOID_ELEMENTS:
            self._open_elements.append((tag, element_id))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open_elements) - 1, -1, -1):
            if self._open_elements[index][0] == tag:
                del self._open_elements[index:]
                return

    def handle_data(self, data: str) -> None:
        for _, element_id in self._open_elements:
            if element_id:
                self.element_text[element_id].append(data)

    def text_for(self, element_id: str) -> str:
        return " ".join("".join(self.element_text[element_id]).split())


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
        self.assertEqual(status["settings"]["sample_rate_hz"], 1_024_000)

    def test_meteor_dashboard_contract(self) -> None:
        status_code, body_bytes, _ = self.get("/")
        body = body_bytes.decode()
        self.assertEqual(status_code, 200)
        inspector = PageInspector()
        inspector.feed(body)

        self.assertIn('aria-label="Meteor observation workspace"', body)
        self.assertIn("PASSIVE RECEIVER", body)
        self.assertIn("Automatic image snapshots are off", body)
        self.assertIn("WATCH MARKER · NOT A SIGNAL", body)
        self.assertIn("95.3 MHz", body)
        self.assertIn("KZJH", body)
        self.assertIn("024° true", body)
        self.assertIn('href="/meteor-guide.md"', body)
        self.assertIn("Open meteor guide", body)
        self.assertEqual(inspector.canvases, {"power-chart", "waterfall"})
        self.assertTrue({"calibrate-button", "watch-button", "retry-button"} <= inspector.buttons)
        self.assertNotIn("center-frequency", inspector.ids)
        self.assertNotIn("tune-down", inspector.ids)
        self.assertNotIn("tune-up", inspector.ids)
        self.assertNotIn("spectrum", inspector.ids)
        self.assertNotIn("Download ZIP", body)
        self.assertNotIn("FM audio ·", body)
        self.assertNotIn('id="event-list"', body)
        self.assertNotIn("Recent unconfirmed candidates", body)
        self.assertEqual(inspector.tooltip_targets, inspector.tooltip_ids)
        self.assertGreaterEqual(len(inspector.tooltip_ids), 5)

    def test_live_display_explanations_are_accessible_and_complete(self) -> None:
        _, body_bytes, _ = self.get("/")
        body = body_bytes.decode("utf-8")
        inspector = PageInspector()
        inspector.feed(body)

        displays = {
            "power": {
                "button": "power-info-button",
                "panel": "power-info-panel",
                "accessible_names": ("echo power", "target-band rise", "target band rise"),
                "required_text": (
                    ("x axis", "horizontal axis"),
                    ("30 seconds", "thirty seconds", "30 s"),
                    ("left",),
                    ("right", "now"),
                    ("y axis", "vertical axis"),
                    ("rise above", "signal rise", "db above baseline", "relative change"),
                    ("db", "decibel"),
                    ("cyan", "blue"),
                    ("watched channel", "target channel", "target window"),
                    ("reference", "surrounding band", "surrounding-band"),
                    ("trigger", "threshold"),
                ),
            },
            "waterfall": {
                "button": "waterfall-info-button",
                "panel": "waterfall-info-panel",
                "accessible_names": ("waterfall",),
                "required_text": (
                    ("x axis", "horizontal axis"),
                    ("frequency",),
                    ("left",),
                    ("right",),
                    ("y axis", "vertical axis"),
                    ("time",),
                    ("newest row at the top", "newest at the top"),
                    ("brightness", "color"),
                    ("received power", "received energy", "signal strength"),
                    ("watch marker",),
                    ("not a signal", "screen overlay"),
                    ("receiver center", "tuning center"),
                    ("dc artifact", "center artifact"),
                ),
            },
        }

        for display, contract in displays.items():
            with self.subTest(display=display):
                button_id = contract["button"]
                panel_id = contract["panel"]
                self.assertIn(button_id, inspector.elements)
                self.assertIn(panel_id, inspector.elements)

                button_tag, button_attributes = inspector.elements[button_id]
                self.assertEqual(button_tag, "button")
                self.assertEqual(button_attributes.get("type"), "button")
                self.assertEqual(button_attributes.get("aria-controls"), panel_id)
                self.assertEqual(button_attributes.get("aria-expanded"), "false")
                accessible_name = (button_attributes.get("aria-label") or "").lower()
                self.assertTrue(
                    any(name in accessible_name for name in contract["accessible_names"]),
                    f"{button_id} needs a specific accessible name",
                )

                panel_tag, panel_attributes = inspector.elements[panel_id]
                self.assertIn(panel_tag, {"aside", "div", "section"})
                self.assertIn("hidden", panel_attributes)
                if panel_tag != "section":
                    self.assertEqual(panel_attributes.get("role"), "region")
                else:
                    self.assertIn(panel_attributes.get("role"), {None, "region"})
                labelled_by = panel_attributes.get("aria-labelledby")
                self.assertIsNotNone(labelled_by)
                self.assertIn(labelled_by, inspector.elements)
                self.assertTrue(inspector.text_for(labelled_by))

                explanation = inspector.text_for(panel_id).lower()
                for alternatives in contract["required_text"]:
                    self.assertTrue(
                        any(term in explanation for term in alternatives),
                        f"{panel_id} must explain one of {alternatives!r}",
                    )

    def test_live_display_explanation_buttons_control_open_state(self) -> None:
        _, script_bytes, _ = self.get("/app.js")
        script = script_bytes.decode("utf-8")

        self.assertIn("elements.visualInfoButtons.forEach", script)
        self.assertIn('getAttribute("aria-controls")', script)
        self.assertRegex(
            script,
            re.compile(
                r'button\.setAttribute\(\s*["\']aria-expanded["\']\s*,'
                r"\s*String\((?:open|opening)\)"
            ),
        )
        self.assertTrue(
            re.search(r"panel\.hidden\s*=\s*!\s*(?:open|opening)", script)
            or re.search(
                r"panel\.toggleAttribute\(\s*[\"']hidden[\"']\s*,\s*!\s*(?:open|opening)",
                script,
            ),
            "Opening an explanation must remove hidden; closing it must restore hidden",
        )
        self.assertIn("closeVisualInfoPanels();", script)
        self.assertIn('"Escape"', script)

    def test_speaker_button_listens_to_selected_meteor_frequency(self) -> None:
        _, body_bytes, _ = self.get("/")
        _, style_bytes, _ = self.get("/app.css")
        body = body_bytes.decode("utf-8")
        styles = style_bytes.decode("utf-8")
        inspector = PageInspector()
        inspector.feed(body)

        self.assertNotIn('id="radio-check"', body)
        self.assertNotIn('<details class="panel radio-check"', body)
        self.assertIn("audio-button", inspector.elements)
        button_tag, button_attributes = inspector.elements["audio-button"]
        self.assertEqual(button_tag, "button")
        self.assertEqual(button_attributes.get("type"), "button")
        self.assertEqual(button_attributes.get("aria-pressed"), "false")
        self.assertIn("disabled", button_attributes)
        accessible_name = (button_attributes.get("aria-label") or "").lower()
        self.assertIn("listen", accessible_name)
        self.assertIn("selected", accessible_name)
        self.assertTrue(
            any(term in accessible_name for term in ("meteor", "target", "frequency"))
        )
        self.assertEqual(button_attributes.get("aria-describedby"), "audio-note")
        self.assertIn("speaker-button", (button_attributes.get("class") or "").split())
        button_start = body.rfind("<button", 0, body.index('id="audio-button"'))
        button_markup = body[button_start : body.index("</button>", button_start)]
        self.assertIn("<svg", button_markup)
        self.assertIn('aria-hidden="true"', button_markup)
        self.assertIn("audio-note", inspector.elements)
        _, note_attributes = inspector.elements["audio-note"]
        self.assertTrue(
            note_attributes.get("role") == "status"
            or note_attributes.get("aria-live") == "polite",
            "The changing listen state must be announced without moving focus",
        )

        fixed_audio_rule = False
        for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", styles):
            if not any(
                selector in selectors
                for selector in ("#audio-button", ".audio-button", ".speaker-button")
            ):
                continue
            if all(
                re.search(rf"\b{property_name}\s*:", declarations)
                for property_name in ("position", "left", "bottom")
            ) and re.search(r"\bposition\s*:\s*fixed\b", declarations):
                fixed_audio_rule = True
                break
        self.assertTrue(fixed_audio_rule, "The speaker control must stay at the lower-left")

        _, script_bytes, _ = self.get("/app.js")
        script = script_bytes.decode("utf-8")
        radio_payload = javascript_function(script, "radioPayload")
        selected_target = re.search(
            r"const\s+([A-Za-z_$][\w$]*)\s*=\s*meteorTargetHz\(\)",
            radio_payload,
        )
        self.assertIsNotNone(selected_target)
        target_variable = selected_target.group(1)
        self.assertRegex(radio_payload, rf"center_frequency_hz\s*:\s*{target_variable}\b")
        self.assertRegex(radio_payload, rf"detection_frequency_hz\s*:\s*{target_variable}\b")
        self.assertRegex(
            radio_payload,
            r"sample_rate_hz\s*:\s*(?:RADIO_CHECK_SPAN_HZ|250_000)",
        )
        self.assertIn('observation_mode: "radio_check"', radio_payload)
        self.assertIn("const RADIO_CHECK_SPAN_HZ = 250_000", script)
        self.assertNotIn("RADIO_CHECK_FREQUENCY_HZ", script)

        toggle = javascript_function(script, "toggleRadioCheck")
        saved_settings = next(
            (
                name
                for name in re.findall(
                    r"let\s+([A-Za-z_$][\w$]*)\s*=\s*null",
                    script,
                )
                if "meteor" in name.lower()
                and any(term in name.lower() for term in ("settings", "payload"))
            ),
            None,
        )
        self.assertIsNotNone(
            saved_settings,
            "Speaker audio must preserve the exact pre-listen meteor settings",
        )
        assert saved_settings is not None
        stop_branch = toggle[: toggle.index("return;")]
        start_branch = toggle[toggle.index("return;") :]
        self.assertIn(saved_settings, stop_branch)
        self.assertNotIn(
            'postSettings("/api/live/tune", meteorPayload())',
            stop_branch,
            "Stopping audio must use the saved settings, not recompute current form values",
        )
        snapshot_assignment = start_branch.index(f"{saved_settings} =")
        radio_tune = start_branch.index('postSettings("/api/live/tune", payload)')
        self.assertLess(snapshot_assignment, radio_tune)

        stop_audio = toggle.index("await stopAudio()")
        restore_meteor = stop_branch.index('postSettings("/api/live/tune"')
        self.assertLess(stop_audio, restore_meteor)
        clear_snapshot = stop_branch.find(f"{saved_settings} = null", restore_meteor)
        self.assertGreater(
            clear_snapshot,
            restore_meteor,
            "Discard the saved settings only after the restore request succeeds",
        )
        self.assertIn("const payload = radioPayload()", toggle)
        self.assertIn('postSettings("/api/live/tune", payload)', toggle)
        self.assertIn("radioCheckActive = false", toggle)
        audio_ui = javascript_function(script, "updateAudioUi")
        self.assertIn('const live = monitorState === "running"', audio_ui)
        self.assertRegex(
            audio_ui,
            r"elements\.audioButton\.disabled\s*=\s*!live\s*\|\|\s*audioTransitionBusy",
        )
        self.assertNotIn(
            "elements.audioButton.textContent",
            audio_ui,
            "Updating listen state must not delete the speaker icon",
        )
        self.assertNotIn("Play local FM 94.9", body)
        self.assertNotIn("Play local FM 94.9", script)

        self.assertIn("const METEOR_SPAN_HZ = 1_024_000", script)
        self.assertIn("status.receiver?.detected", script)
        self.assertIn("queuedBytes < 8192", script)
        self.assertIn("audioNextTime <= audioContext.currentTime + .005", script)

        pagehide = script[
            script.index('window.addEventListener("pagehide"') : script.index(
                "setSensitivity(8);"
            )
        ]
        self.assertIn(saved_settings, pagehide)

        activation_catch = start_branch[start_branch.index("} catch (error) {") :]
        status_request = activation_catch.index('request("/api/live/status")')
        status_apply = activation_catch.index("applyStatus(status)")
        self.assertLess(status_request, status_apply)
        self.assertIn('status.settings?.observation_mode === "radio_check"', activation_catch)
        self.assertIn(
            f'postSettings("/api/live/tune", {saved_settings})',
            activation_catch,
        )
        self.assertIn("if (restored)", activation_catch)
        self.assertGreater(
            activation_catch.index(f"{saved_settings} = null"),
            activation_catch.index("if (restored)"),
        )

    def test_static_assets_are_served_without_cache(self) -> None:
        for path, content_type in (
            ("/app.css", "text/css; charset=utf-8"),
            ("/app.js", "text/javascript; charset=utf-8"),
            ("/meteor-guide.md", "text/markdown; charset=utf-8"),
            ("/assets/meteor-scatter-path.jpg", "image/jpeg"),
        ):
            status_code, body, headers = self.get(path)
            self.assertEqual(status_code, 200)
            self.assertGreater(len(body), 100)
            self.assertEqual(headers["content-type"], content_type)
            self.assertEqual(headers["cache-control"], "no-store")

    def test_meteor_guide_is_plain_language_and_honest_about_evidence(self) -> None:
        status_code, body_bytes, headers = self.get("/meteor-guide.md")
        body = body_bytes.decode("utf-8")

        self.assertEqual(status_code, 200)
        self.assertEqual(headers["content-type"], "text/markdown; charset=utf-8")
        self.assertIn("# Meteor Echo Monitor: Simple Guide", body)
        self.assertIn("How the automatic detector decides to save", body)
        self.assertIn("What gets saved", body)
        self.assertIn("Common false positives", body)
        self.assertIn("Important limits of this version", body)
        self.assertIn("unconfirmed meteor echo candidate", body.lower())
        self.assertIn("does **not** transmit", body)
        self.assertNotIn("confirmed meteor detection", body.lower())

    def test_old_download_api_is_gone(self) -> None:
        for path in (
            "/api/captures",
            "/api/download",
            "/api/captures.zip",
            "/api/events/previews/old.waterfall.png",
        ):
            status_code, _, _ = self.get(path)
            self.assertEqual(status_code, 404)

    def test_saved_image_feature_is_absent_but_live_waterfall_remains(self) -> None:
        _, page_bytes, _ = self.get("/")
        _, script_bytes, _ = self.get("/app.js")
        page = page_bytes.decode("utf-8")
        script = script_bytes.decode("utf-8")

        self.assertIn('id="waterfall"', page)
        self.assertIn("drawWaterfall", script)
        self.assertNotIn("preview_url", script)
        self.assertNotIn("eventCard", script)
        self.assertNotIn("loadEvents", script)

    def test_audio_stream_requires_a_running_receiver(self) -> None:
        status_code, body, _ = self.get("/api/live/audio")
        payload = json.loads(body)
        self.assertEqual(status_code, 409)
        self.assertEqual(payload["error"], "receiver is not running")

    def test_audio_stream_rejects_running_meteor_mode(self) -> None:
        running_meteor = {
            "state": "running",
            "settings": {"observation_mode": "meteor"},
        }
        stopped_meteor = {
            "state": "stopped",
            "settings": {"observation_mode": "meteor"},
        }
        with patch.object(
            self.monitor,
            "status",
            side_effect=(running_meteor, stopped_meteor),
        ):
            status_code, body, _ = self.get("/api/live/audio")

        self.assertEqual(status_code, 409)
        payload = json.loads(body)
        error = payload["error"].lower().replace("_", " ").replace("-", " ")
        self.assertTrue(
            "radio check" in error or "speaker" in error,
            "The response should explain how to enter the permitted audio mode",
        )

    def test_open_audio_stream_ends_when_audio_becomes_unavailable(self) -> None:
        listening = {
            "state": "running",
            "settings": {"observation_mode": "radio_check"},
            "audio": {"available": True},
        }
        retuning = {
            "state": "running",
            "settings": {"observation_mode": "radio_check"},
            "audio": {"available": False},
        }
        with (
            patch.object(
                self.monitor,
                "status",
                side_effect=(listening, retuning),
            ) as status,
            patch.object(self.monitor, "subscribe_audio", return_value=41) as subscribe,
            patch.object(self.monitor, "wait_for_audio", return_value=None) as wait,
            patch.object(self.monitor, "unsubscribe_audio") as unsubscribe,
        ):
            status_code, body, headers = self.get("/api/live/audio")

        self.assertEqual(status_code, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertEqual(status.call_count, 2)
        subscribe.assert_called_once_with()
        wait.assert_called_once_with(41, timeout=2)
        unsubscribe.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
