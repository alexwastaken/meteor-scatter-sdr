from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import contextlib
import io
import unittest

from meteor_sdr.capture import CaptureResult
from meteor_sdr.cli import main


class CliTests(unittest.TestCase):
    @patch("meteor_sdr.cli.capture_once")
    def test_explicit_auto_gain_overrides_config_file(self, capture_mock) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.toml"
            config_path.write_text(
                """
[radio]
center_frequency_hz = 100000000
sample_rate_hz = 250000
gain_db = 28.0

[capture]
duration_seconds = 0.001
output_dir = "captures"
""".strip(),
                encoding="utf-8",
            )
            capture_mock.return_value = CaptureResult(
                root / "capture.cu8",
                root / "capture.cu8.json",
                500,
                "abc123",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(
                    ["capture", "--config", str(config_path), "--gain", "auto"]
                )

        self.assertEqual(exit_code, 0)
        submitted_config = capture_mock.call_args.args[0]
        self.assertIsNone(submitted_config.gain_db)


if __name__ == "__main__":
    unittest.main()
