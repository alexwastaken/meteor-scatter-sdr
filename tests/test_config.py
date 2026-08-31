from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from meteor_sdr.config import (
    CaptureConfig,
    ConfigurationError,
    load_config,
    parse_frequency,
)


class ConfigTests(unittest.TestCase):
    def test_frequency_suffixes(self) -> None:
        self.assertEqual(parse_frequency("100M"), 100_000_000)
        self.assertEqual(parse_frequency("1.024MHz"), 1_024_000)
        self.assertEqual(parse_frequency("250k"), 250_000)

    def test_default_capture_size(self) -> None:
        config = CaptureConfig().validated()
        self.assertEqual(config.sample_count, 10_240_000)
        self.assertEqual(config.expected_bytes, 20_480_000)

    def test_rejects_librtlsdr_unsupported_sample_rate_gap(self) -> None:
        for sample_rate_hz in (300_001, 512_000, 900_000):
            with self.subTest(sample_rate_hz=sample_rate_hz):
                with self.assertRaisesRegex(ConfigurationError, "unsupported"):
                    CaptureConfig(sample_rate_hz=sample_rate_hz).validated()
        for sample_rate_hz in (250_000, 300_000, 900_001, 1_024_000):
            with self.subTest(sample_rate_hz=sample_rate_hz):
                self.assertEqual(
                    CaptureConfig(sample_rate_hz=sample_rate_hz).validated().sample_rate_hz,
                    sample_rate_hz,
                )

    def test_default_meteor_profile_avoids_the_center_artifact(self) -> None:
        config = CaptureConfig().validated()
        target_hz = 95_300_000
        self.assertGreaterEqual(abs(config.center_frequency_hz - target_hz), 80_000)
        self.assertLessEqual(config.lower_edge_hz, target_hz - 80_000)
        self.assertGreaterEqual(config.upper_edge_hz, target_hz + 80_000)

    def test_rejects_span_outside_antenna_band(self) -> None:
        with self.assertRaises(ConfigurationError):
            CaptureConfig(
                center_frequency_hz=87_500_000,
                sample_rate_hz=1_024_000,
            ).validated()

    def test_loads_toml(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.toml"
            path.write_text(
                """
[radio]
center_frequency_hz = 99000000
sample_rate_hz = 1000000
gain_db = 28.0
ppm = 1
device = "meteor-receiver"

[capture]
duration_seconds = 2.5
output_dir = "radio-data"
""".strip(),
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual(config.center_frequency_hz, 99_000_000)
        self.assertEqual(config.gain_db, 28.0)
        self.assertEqual(config.device, "meteor-receiver")
        self.assertEqual(config.output_dir, Path("radio-data"))

    def test_partial_config_uses_the_driver_safe_meteor_defaults(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.toml"
            path.write_text("[radio]\nppm = 1\n", encoding="utf-8")
            config = load_config(path)

        self.assertEqual(config.center_frequency_hz, 95_460_000)
        self.assertEqual(config.sample_rate_hz, 1_024_000)
        self.assertEqual(config.gain_db, 28.0)


if __name__ == "__main__":
    unittest.main()
