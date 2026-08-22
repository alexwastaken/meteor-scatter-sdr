from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import unittest

from meteor_sdr.capture import capture_once
from meteor_sdr.config import CaptureConfig
from meteor_sdr.rtl import RtlSdr


FAKE_RTL_SDR = """#!/usr/bin/env python3
import pathlib
import sys

sample_count = int(sys.argv[sys.argv.index("-n") + 1])
output = pathlib.Path(sys.argv[-1])
output.write_bytes(bytes([127, 128]) * sample_count)
print("fake rtl_sdr capture complete", file=sys.stderr)
"""


class CaptureTests(unittest.TestCase):
    def test_capture_writes_data_and_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "rtl_sdr"
            executable.write_text(FAKE_RTL_SDR, encoding="utf-8")
            os.chmod(executable, 0o755)
            output_dir = root / "captures"
            config = CaptureConfig(
                center_frequency_hz=100_000_000,
                sample_rate_hz=250_000,
                duration_seconds=0.001,
                output_dir=output_dir,
            ).validated()

            result = capture_once(config, RtlSdr(str(executable)))

            self.assertEqual(result.byte_count, 500)
            self.assertTrue(result.data_path.exists())
            self.assertTrue(result.metadata_path.exists())
            self.assertFalse(list(output_dir.glob("*.part")))
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["format"], "cu8")
            self.assertEqual(metadata["sample_count"], 250)
            self.assertEqual(metadata["gain_db"], "auto")
            self.assertEqual(metadata["sha256"], result.sha256)


if __name__ == "__main__":
    unittest.main()
