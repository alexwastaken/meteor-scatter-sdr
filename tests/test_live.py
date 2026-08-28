from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest

import numpy as np

from meteor_sdr.config import CaptureConfig, ConfigurationError
from meteor_sdr.live import (
    CandidateDetector,
    DetectionResult,
    LiveMonitor,
    LiveSettings,
    RollingIqBuffer,
    SpectrumAnalyzer,
    render_waterfall_png,
    settings_from_payload,
    stream_read_size,
)
from meteor_sdr.rtl import SimulatedRadioStream


class FakeRadioStream:
    def __init__(self, config: CaptureConfig) -> None:
        self.config = config
        self.tuned: list[CaptureConfig] = []
        self.closed = False

    def read(self, size: int) -> bytes:
        time.sleep(0.004)
        if self.closed:
            return b""
        return (bytes([127, 128]) * (size // 2 + 1))[:size]

    def tune(self, config: CaptureConfig) -> None:
        self.config = config
        self.tuned.append(config)

    def stderr_detail(self) -> str:
        return ""

    def close(self) -> None:
        self.closed = True


class LivePipelineTests(unittest.TestCase):
    def test_simulated_receiver_streams_and_retunes_without_hardware(self) -> None:
        config = CaptureConfig(sample_rate_hz=250_000)
        stream = SimulatedRadioStream(config)
        try:
            block = stream.read(8192)
            self.assertEqual(len(block), 8192)
            self.assertGreater(len(set(block)), 20)
            stream.tune(
                CaptureConfig(
                    center_frequency_hz=100_100_000,
                    sample_rate_hz=250_000,
                )
            )
            self.assertEqual(len(stream.read(8192)), 8192)
        finally:
            stream.close()
        self.assertEqual(stream.read(8192), b"")

    def test_rolling_buffer_keeps_only_newest_bytes(self) -> None:
        ring = RollingIqBuffer(sample_rate_hz=2, seconds=2)
        ring.append(b"12345")
        ring.append(b"67890")
        self.assertEqual(ring.capacity, 8)
        self.assertEqual(ring.snapshot(), b"34567890")
        self.assertEqual(ring.fill_ratio, 1.0)

    def test_stream_reads_are_short_and_usb_aligned(self) -> None:
        self.assertEqual(stream_read_size(250_000), 8_192)
        self.assertEqual(stream_read_size(1_024_000) % 512, 0)
        self.assertLessEqual(stream_read_size(1_024_000), 21_000)

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

    def test_detector_requires_a_rise_and_completed_decay(self) -> None:
        detector = CandidateDetector(threshold_db=8, warmup_frames=3, cooldown=10)
        for index in range(3):
            result = detector.update(-60, -70, now=index * 0.04)
            self.assertFalse(result.completed)
        self.assertFalse(detector.update(-50, -70, now=0.12).active)
        result = detector.update(-50, -70, now=0.16)
        self.assertTrue(result.active)
        self.assertFalse(result.completed)
        detector.update(-60, -70, now=0.20)
        detector.update(-60, -70, now=0.24)
        result = detector.update(-60, -70, now=0.28)
        self.assertTrue(result.completed)
        self.assertEqual(result.peak_excess_db, 10)
        self.assertEqual(result.baseline_db, -60)
        self.assertEqual(result.profile, "underdense_like")

    def test_detector_rejects_single_frame_and_broadband_flashes(self) -> None:
        detector = CandidateDetector(threshold_db=8, warmup_frames=3)
        for index in range(3):
            detector.update(-60, -70, now=index * 0.04)
        self.assertFalse(detector.update(-48, -70, now=0.12).active)
        self.assertFalse(detector.update(-60, -70, now=0.16).active)
        self.assertFalse(detector.update(-48, -58, now=0.20).active)
        self.assertFalse(detector.update(-48, -58, now=0.24).active)

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
                monitor._archive(
                    data,
                    settings,
                    DetectionResult(
                        completed=True,
                        active=False,
                        excess_db=2.0,
                        baseline_db=-45.0,
                        reference_excess_db=0.5,
                        effective_threshold_db=8.0,
                        duration_seconds=0.24,
                        peak_excess_db=10.0,
                        peak_level_db=-35.0,
                        profile="underdense_like",
                    ),
                )
                metadata_files = list((config.output_dir / "events").glob("*.cu8.json"))
                self.assertEqual(len(metadata_files), 1)
                metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
                data_path = metadata_files[0].parent / metadata["data_file"]
                self.assertEqual(data_path.read_bytes(), data)
                self.assertEqual(metadata["classification"], "meteor_candidate_unconfirmed")
                self.assertEqual(metadata["trigger_excess_db"], 10.0)
                self.assertEqual(metadata["echo_profile"], "underdense_like")
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
        monitor = LiveMonitor(
            CaptureConfig(sample_rate_hz=250_000),
            source_factory=FakeRadioStream,
        )
        try:
            monitor.start({})
            frame = monitor.wait_for_frame(0, timeout=2)
            self.assertIsNotNone(frame)
            assert frame is not None
            self.assertEqual(len(frame["spectrum_db"]), 512)
            self.assertGreater(frame["buffer_seconds"], 0)
            status = monitor.status()
            self.assertEqual(status["source"], "hardware")
            self.assertTrue(status["receiver"]["connected"])
            self.assertEqual(status["receiver"]["display_name"], "RTL-SDR receiver")
        finally:
            monitor.close()

    def test_live_tuning_reuses_stream_at_new_center(self) -> None:
        sources: list[FakeRadioStream] = []

        def factory(config: CaptureConfig) -> FakeRadioStream:
            source = FakeRadioStream(config)
            sources.append(source)
            return source

        monitor = LiveMonitor(
            CaptureConfig(sample_rate_hz=250_000),
            source_factory=factory,
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
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].tuned[-1].center_frequency_hz, 100_100_000)
            self.assertEqual(
                (second["lower_edge_hz"] + second["upper_edge_hz"]) // 2,
                100_100_000,
            )
            status = monitor.watch({"detection_frequency_hz": 100_110_000})
            self.assertEqual(status["settings"]["detection_frequency_hz"], 100_110_000)
        finally:
            monitor.close()


if __name__ == "__main__":
    unittest.main()
