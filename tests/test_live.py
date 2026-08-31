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
    FmAudioDemodulator,
    LiveMonitor,
    LiveSettings,
    RollingIqBuffer,
    SpectrumAnalyzer,
    settings_from_payload,
    stream_read_size,
)
from meteor_sdr.rtl import RtlSdrError, SimulatedRadioStream


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
    def test_tuning_error_keeps_detected_receiver_separate_from_usb_failure(self) -> None:
        identity = {
            "display_name": "Nooelec NESDR SMArt v5",
            "manufacturer": "Nooelec",
            "product": "NESDR SMArt v5",
            "serial": "33864718",
            "driver_name": "RTL-SDR",
        }

        def failing_factory(config: CaptureConfig):
            raise RtlSdrError(
                "PyRTLSDR tuning failed: invalid sample rate",
                receiver_info=identity,
                error_stage="tuning",
            )

        monitor = LiveMonitor(CaptureConfig(), source_factory=failing_factory)
        try:
            monitor.start({})
            deadline = time.monotonic() + 1
            status = monitor.status()
            while status["state"] != "error" and time.monotonic() < deadline:
                time.sleep(0.01)
                status = monitor.status()

            self.assertEqual(status["state"], "error")
            self.assertFalse(status["receiver"]["connected"])
            self.assertTrue(status["receiver"]["detected"])
            self.assertEqual(status["receiver"]["error_stage"], "tuning")
            self.assertEqual(status["receiver"]["display_name"], identity["display_name"])
            self.assertIn("tuning failed", status["error"])
        finally:
            monitor.close()

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
        self.assertEqual(b"".join(ring.snapshot_chunks()), b"34567890")
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

    def test_fm_audio_demodulates_the_watched_station(self) -> None:
        sample_rate = 1_024_000
        sample_count = 65_536
        time_axis = np.arange(sample_count) / sample_rate
        instantaneous_frequency = 100_000 + 25_000 * np.sin(
            2 * np.pi * 1_000 * time_axis
        )
        phase = np.cumsum(2 * np.pi * instantaneous_frequency / sample_rate)
        samples = 0.8 * np.exp(1j * phase)
        interleaved = np.empty(sample_count * 2, dtype=np.uint8)
        interleaved[0::2] = np.clip(samples.real * 127.5 + 127.5, 0, 255)
        interleaved[1::2] = np.clip(samples.imag * 127.5 + 127.5, 0, 255)

        demodulator = FmAudioDemodulator(sample_rate, frequency_offset_hz=100_000)
        pcm = np.frombuffer(demodulator.process(interleaved.tobytes()), dtype="<i2")
        self.assertGreater(pcm.size, 1_500)
        spectrum = np.abs(np.fft.rfft(pcm[300:] * np.hanning(pcm.size - 300)))
        frequencies = np.fft.rfftfreq(pcm.size - 300, 1 / 32_000)
        peak_frequency = frequencies[int(np.argmax(spectrum[1:])) + 1]
        self.assertLess(abs(peak_frequency - 1_000), 40)
        self.assertGreater(float(np.sqrt(np.mean(pcm.astype(float) ** 2))), 1_000)

    def test_fm_audio_removes_the_stereo_pilot_before_downsampling(self) -> None:
        sample_rate = 250_000
        sample_count = 125_000
        time_axis = np.arange(sample_count) / sample_rate
        instantaneous_frequency = (
            25_000 * np.sin(2 * np.pi * 1_000 * time_axis)
            + 8_000 * np.sin(2 * np.pi * 19_000 * time_axis)
        )
        phase = np.cumsum(2 * np.pi * instantaneous_frequency / sample_rate)
        samples = 0.8 * np.exp(1j * phase)
        interleaved = np.empty(sample_count * 2, dtype=np.uint8)
        interleaved[0::2] = np.clip(samples.real * 127.5 + 127.5, 0, 255)
        interleaved[1::2] = np.clip(samples.imag * 127.5 + 127.5, 0, 255)

        demodulator = FmAudioDemodulator(sample_rate)
        pcm = np.frombuffer(demodulator.process(interleaved.tobytes()), dtype="<i2")
        stable = pcm[500:].astype(float)
        spectrum = np.abs(np.fft.rfft(stable * np.hanning(stable.size)))
        frequencies = np.fft.rfftfreq(stable.size, 1 / 32_000)
        audio_level = spectrum[np.argmin(np.abs(frequencies - 1_000))]
        aliased_pilot_level = spectrum[np.argmin(np.abs(frequencies - 13_000))]

        self.assertGreater(audio_level, aliased_pilot_level * 100)

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
        self.assertEqual(settings.observation_mode, "meteor")
        radio = settings_from_payload(
            base,
            {
                "detection_frequency_hz": 100_100_000,
                "observation_mode": "radio_check",
            },
        )
        self.assertEqual(radio.observation_mode, "radio_check")
        with self.assertRaises(ConfigurationError):
            settings_from_payload(base, {"detection_frequency_hz": 101_000_000})
        with self.assertRaises(ConfigurationError):
            settings_from_payload(base, {"observation_mode": "general_sdr"})
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
                        peak_reference_excess_db=1.25,
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
                self.assertEqual(metadata["reference_excess_db"], 1.25)
                self.assertEqual(metadata["echo_profile"], "underdense_like")
                self.assertEqual(metadata["observation_mode"], "meteor")
                self.assertEqual(metadata["observer"]["antenna_type"], "Yagi")
                self.assertIsNone(metadata["suggested_target"])
                self.assertNotIn("preview_file", metadata)
                self.assertEqual(list(metadata_files[0].parent.glob("*.png")), [])

                legacy_preview = metadata_files[0].parent / "legacy.waterfall.png"
                legacy_preview.write_bytes(b"old image")
                metadata["preview_file"] = legacy_preview.name
                metadata_files[0].write_text(
                    json.dumps(metadata),
                    encoding="utf-8",
                )
                listed = monitor.events()
                self.assertEqual(len(listed), 1)
                self.assertNotIn("preview_file", listed[0])
                self.assertTrue(legacy_preview.exists())
            finally:
                monitor.close()

    def test_radio_check_mode_never_arms_or_archives(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            monitor = LiveMonitor(
                CaptureConfig(
                    center_frequency_hz=94_900_000,
                    sample_rate_hz=250_000,
                    output_dir=Path(temporary_directory),
                ),
                source_factory=FakeRadioStream,
            )
            try:
                monitor.start({"observation_mode": "radio_check"})
                frame = monitor.wait_for_frame(0, timeout=2)
                self.assertIsNotNone(frame)
                assert frame is not None
                self.assertEqual(frame["observation_mode"], "radio_check")
                self.assertFalse(frame["detector_ready"])
                self.assertFalse(frame["detector_active"])
                self.assertFalse(frame["archived"])
                self.assertEqual(monitor.status()["candidate_count"], 0)
                self.assertEqual(monitor.events(), [])
            finally:
                monitor.close()

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
            self.assertTrue(status["audio"]["available"])
            self.assertEqual(status["audio"]["sample_rate_hz"], 32_000)
        finally:
            monitor.close()

    def test_monitor_publishes_audio_only_for_a_listener(self) -> None:
        monitor = LiveMonitor(
            CaptureConfig(sample_rate_hz=250_000),
            source_factory=FakeRadioStream,
        )
        sequence = monitor.subscribe_audio()
        try:
            monitor.start({})
            item = monitor.wait_for_audio(sequence, timeout=2)
            self.assertIsNotNone(item)
            assert item is not None
            next_sequence, pcm = item
            self.assertGreater(next_sequence, sequence)
            self.assertGreater(len(pcm), 0)
            self.assertEqual(len(pcm) % 2, 0)
        finally:
            monitor.unsubscribe_audio()
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
