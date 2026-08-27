from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import unittest

import numpy as np

from meteor_sdr.config import CaptureConfig, ConfigurationError
from meteor_sdr.live import (
    CandidateDetector,
    LiveMonitor,
    LiveSettings,
    RollingIqBuffer,
    SpectrumAnalyzer,
    render_waterfall_png,
    settings_from_payload,
)
from meteor_sdr.rtl import RtlSdr


class LivePipelineTests(unittest.TestCase):
    def test_rolling_buffer_keeps_only_newest_bytes(self) -> None:
        ring = RollingIqBuffer(sample_rate_hz=2, seconds=2)
        ring.append(b"12345")
        ring.append(b"67890")
        self.assertEqual(ring.capacity, 8)
        self.assertEqual(ring.snapshot(), b"34567890")
        self.assertEqual(ring.fill_ratio, 1.0)

    def test_spectrum_finds_a_complex_tone(self) -> None:
        analyzer = SpectrumAnalyzer(fft_size=4096, display_bins=512)
        sample_rate = 1_024_000
        offset_hz = 128_000
        time_axis = np.arange(4096)
        samples = 0.7 * np.exp(2j * np.pi * offset_hz * time_axis / sample_rate)
        interleaved = np.empty(8192, dtype=np.uint8)
        interleaved[0::2] = np.clip(samples.real * 127.5 + 127.5, 0, 255)
        interleaved[1::2] = np.clip(samples.imag * 127.5 + 127.5, 0, 255)
        spectrum = analyzer.analyze(interleaved.tobytes())
        peak = int(np.argmax(spectrum))
        expected = round((offset_hz + sample_rate / 2) / (sample_rate / 512))
        self.assertLessEqual(abs(peak - expected), 1)

    def test_detector_requires_warmup_and_two_high_frames(self) -> None:
        detector = CandidateDetector(threshold_db=8, warmup_frames=3, cooldown=10)
        for index in range(3):
            triggered, _, _ = detector.update(-60, now=float(index))
            self.assertFalse(triggered)
        self.assertFalse(detector.update(-50, now=3)[0])
        triggered, excess, baseline = detector.update(-50, now=4)
        self.assertTrue(triggered)
        self.assertEqual(excess, 10)
        self.assertEqual(baseline, -60)

    def test_live_settings_validate_target_and_unknown_fields(self) -> None:
        base = CaptureConfig(center_frequency_hz=100_000_000, sample_rate_hz=1_024_000)
        settings = settings_from_payload(base, {"detection_frequency_hz": 100_100_000})
        self.assertEqual(settings.detection_frequency_hz, 100_100_000)
        with self.assertRaises(ConfigurationError):
            settings_from_payload(base, {"detection_frequency_hz": 101_000_000})
        with self.assertRaises(ConfigurationError):
            settings_from_payload(base, {"download": True})

    def test_candidate_archive_writes_raw_iq_and_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config = CaptureConfig(
                center_frequency_hz=100_000_000,
                sample_rate_hz=250_000,
                output_dir=Path(temporary_directory),
            )
            monitor = LiveMonitor(config)
            settings = LiveSettings(config, 100_000_000, 8).validated()
            data = bytes([127, 128]) * 4096
            try:
                monitor._archive(data, settings, -35.0, 10.0)
                metadata_files = list((config.output_dir / "events").glob("*.cu8.json"))
                self.assertEqual(len(metadata_files), 1)
                metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
                data_path = metadata_files[0].parent / metadata["data_file"]
                self.assertEqual(data_path.read_bytes(), data)
                self.assertEqual(metadata["classification"], "meteor_candidate_unconfirmed")
                self.assertEqual(metadata["trigger_excess_db"], 10.0)
                preview_path = metadata_files[0].parent / metadata["preview_file"]
                self.assertTrue(preview_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            finally:
                monitor.close()

    def test_waterfall_preview_is_a_512_pixel_png(self) -> None:
        data = bytes([127, 128]) * 8192
        preview = render_waterfall_png(data, height=24)
        self.assertEqual(preview[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(preview[16:20], "big"), 512)
        self.assertEqual(int.from_bytes(preview[20:24], "big"), 24)

    def test_monitor_publishes_a_frame_from_streamed_iq(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "fake_rtl_sdr"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdout.buffer.write(bytes([127, 128]) * 131072)\n",
                encoding="utf-8",
            )
            os.chmod(executable, 0o755)
            monitor = LiveMonitor(
                CaptureConfig(sample_rate_hz=250_000),
                rtl=RtlSdr(str(executable)),
            )
            try:
                monitor.start({})
                frame = monitor.wait_for_frame(0, timeout=2)
                self.assertIsNotNone(frame)
                assert frame is not None
                self.assertEqual(len(frame["spectrum_db"]), 512)
                self.assertGreater(frame["buffer_seconds"], 0)
            finally:
                monitor.close()

    def test_live_tuning_restarts_stream_at_new_center(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "fake_continuous_rtl_sdr"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys, time\n"
                "while True:\n"
                "    sys.stdout.buffer.write(bytes([127, 128]) * 32768)\n"
                "    sys.stdout.buffer.flush()\n"
                "    time.sleep(0.01)\n",
                encoding="utf-8",
            )
            os.chmod(executable, 0o755)
            monitor = LiveMonitor(
                CaptureConfig(sample_rate_hz=250_000),
                rtl=RtlSdr(str(executable)),
            )
            try:
                monitor.start({})
                first = monitor.wait_for_frame(0, timeout=2)
                self.assertIsNotNone(first)
                assert first is not None
                monitor.tune(
                    {
                        "center_frequency_hz": 100_100_000,
                        "detection_frequency_hz": 100_100_000,
                        "sample_rate_hz": 250_000,
                    }
                )
                second = monitor.wait_for_frame(first["sequence"], timeout=2)
                self.assertIsNotNone(second)
                assert second is not None
                self.assertEqual(
                    (second["lower_edge_hz"] + second["upper_edge_hz"]) // 2,
                    100_100_000,
                )
            finally:
                monitor.close()


if __name__ == "__main__":
    unittest.main()
