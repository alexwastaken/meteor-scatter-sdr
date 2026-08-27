from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest
import zipfile

from meteor_sdr.capture import CaptureResult
from meteor_sdr.config import CaptureConfig, ConfigurationError
from meteor_sdr.web import CaptureBusyError, CaptureManager, build_capture_zip


class WebDashboardTests(unittest.TestCase):
    def test_manager_captures_and_bundles_files(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory) / "captures"
            config = CaptureConfig(
                center_frequency_hz=100_000_000,
                sample_rate_hz=250_000,
                duration_seconds=0.001,
                output_dir=output_dir,
            )

            def fake_capture(submitted: CaptureConfig) -> CaptureResult:
                submitted.output_dir.mkdir(parents=True, exist_ok=True)
                data_path = submitted.output_dir / "test_capture.cu8"
                metadata_path = submitted.output_dir / "test_capture.cu8.json"
                data_path.write_bytes(bytes([127, 128]) * submitted.sample_count)
                metadata_path.write_text(
                    json.dumps(
                        {
                            "data_file": data_path.name,
                            "center_frequency_hz": submitted.center_frequency_hz,
                            "sample_rate_hz": submitted.sample_rate_hz,
                            "gain_db": "auto",
                            "started_at_utc": "2026-08-26T12:00:00+00:00",
                            "requested_duration_seconds": submitted.duration_seconds,
                        }
                    ),
                    encoding="utf-8",
                )
                return CaptureResult(data_path, metadata_path, data_path.stat().st_size, "abc")

            manager = CaptureManager(config, fake_capture)
            job = manager.start({"gain_db": "auto"})
            self.assertEqual(job["state"], "capturing")
            for _ in range(100):
                status = manager.status()["active"]
                if status["state"] != "capturing":
                    break
                time.sleep(0.01)
            self.assertEqual(status["state"], "complete")

            captures = manager.captures()
            self.assertEqual(len(captures), 1)
            self.assertEqual(captures[0]["data_file"], "test_capture.cu8")

            archive, _, archive_name = build_capture_zip(output_dir, "test_capture.cu8")
            try:
                with zipfile.ZipFile(archive) as bundle:
                    self.assertEqual(
                        set(bundle.namelist()),
                        {"test_capture.cu8", "test_capture.cu8.json"},
                    )
                self.assertEqual(archive_name, "test_capture.zip")
            finally:
                archive.close()

    def test_rejects_unknown_and_out_of_band_settings(self) -> None:
        manager = CaptureManager(CaptureConfig())
        with self.assertRaises(ConfigurationError):
            manager.start({"output_dir": "/tmp/not-allowed"})
        with self.assertRaises(ConfigurationError):
            manager.start({"center_frequency_hz": 50_000_000})

    def test_rejects_parallel_capture(self) -> None:
        def slow_capture(_: CaptureConfig) -> CaptureResult:
            time.sleep(0.2)
            raise OSError("stopped")

        manager = CaptureManager(CaptureConfig(duration_seconds=0.001), slow_capture)
        manager.start({})
        with self.assertRaises(CaptureBusyError):
            manager.start({})


if __name__ == "__main__":
    unittest.main()
