from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
import time
import types
import unittest
from unittest.mock import patch

from meteor_sdr.config import CaptureConfig
from meteor_sdr.rtl import RtlPyStream, RtlSdrError, RtlTcpStream


class RtlTcpStreamTests(unittest.TestCase):
    def test_stream_handshake_samples_and_live_tuning_commands(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "fake_rtl_tcp"
            command_log = Path(f"{executable}.commands")
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, socket, struct, sys\n"
                "args=sys.argv[1:]\n"
                "port=int(args[args.index('-p')+1])\n"
                "log=pathlib.Path(sys.argv[0]+'.commands')\n"
                "server=socket.socket()\n"
                "server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
                "server.bind(('127.0.0.1',port))\n"
                "server.listen(1)\n"
                "connection,_=server.accept()\n"
                "connection.sendall(b'RTL0'+bytes(8))\n"
                "connection.sendall(bytes([127,128])*32768)\n"
                "pending=b''\n"
                "while True:\n"
                "    data=connection.recv(4096)\n"
                "    if not data: break\n"
                "    pending+=data\n"
                "    while len(pending)>=5:\n"
                "        command,value=struct.unpack('>BI',pending[:5])\n"
                "        pending=pending[5:]\n"
                "        with log.open('a') as output: output.write(f'{command}:{value}\\n')\n",
                encoding="utf-8",
            )
            os.chmod(executable, 0o755)
            stream = RtlTcpStream(
                CaptureConfig(sample_rate_hz=250_000),
                executable=str(executable),
            )
            try:
                samples = stream.read(32_768)
                self.assertIsNotNone(samples)
                assert samples is not None
                self.assertEqual(len(samples), 32_768)
                stream.tune(
                    CaptureConfig(
                        center_frequency_hz=101_100_000,
                        sample_rate_hz=250_000,
                        gain_db=12.3,
                    )
                )
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    if command_log.exists() and len(command_log.read_text().splitlines()) >= 5:
                        break
                    time.sleep(0.01)
                self.assertEqual(
                    command_log.read_text(encoding="utf-8").splitlines(),
                    ["1:101100000", "2:250000", "5:0", "3:1", "4:123"],
                )
            finally:
                stream.close()


class RtlPyStreamTests(unittest.TestCase):
    def test_direct_binding_reads_raw_bytes_and_retunes_open_device(self) -> None:
        instances = []

        class FakePyRtlSdr:
            def __init__(self, **arguments) -> None:
                self.arguments = arguments
                self.sample_rate = None
                self.freq_correction = None
                self.center_freq = None
                self.gain = None
                self.closed = False
                instances.append(self)

            def read_bytes(self, size: int) -> bytearray:
                return bytearray([127, 128]) * (size // 2)

            def close(self) -> None:
                self.closed = True

        module = types.ModuleType("rtlsdr")
        module.RtlSdr = FakePyRtlSdr
        with patch.dict(sys.modules, {"rtlsdr": module}):
            stream = RtlPyStream(
                CaptureConfig(
                    center_frequency_hz=100_000_000,
                    sample_rate_hz=1_024_000,
                )
            )
            try:
                self.assertEqual(instances[0].arguments, {"device_index": 0})
                self.assertEqual(len(stream.read(8_192)), 8_192)
                stream.tune(
                    CaptureConfig(
                        center_frequency_hz=100_250_000,
                        sample_rate_hz=1_024_000,
                        gain_db=28.0,
                    )
                )
                self.assertEqual(instances[0].center_freq, 100_250_000)
                self.assertEqual(instances[0].gain, 28.0)
            finally:
                stream.close()
        self.assertTrue(instances[0].closed)

    def test_failed_setter_invalidates_cache_so_rollback_reapplies_hardware(self) -> None:
        instances = []

        class FakePyRtlSdr:
            def __init__(self, **arguments) -> None:
                self.arguments = arguments
                self.sample_rates: list[int] = []
                self.center_frequencies: list[int] = []
                self.gains: list[float | str] = []
                self.freq_correction = None
                self.closed = False
                instances.append(self)

            @property
            def sample_rate(self):
                return self.sample_rates[-1] if self.sample_rates else None

            @sample_rate.setter
            def sample_rate(self, value: int) -> None:
                self.sample_rates.append(value)

            @property
            def center_freq(self):
                return self.center_frequencies[-1] if self.center_frequencies else None

            @center_freq.setter
            def center_freq(self, value: int) -> None:
                self.center_frequencies.append(value)

            @property
            def gain(self):
                return self.gains[-1] if self.gains else None

            @gain.setter
            def gain(self, value: float | str) -> None:
                self.gains.append(value)
                if value == 12.3:
                    raise ValueError("gain setter failed after center frequency changed")

            def read_bytes(self, size: int) -> bytearray:
                return bytearray([127, 128]) * (size // 2)

            def close(self) -> None:
                self.closed = True

        original = CaptureConfig(
            center_frequency_hz=100_000_000,
            sample_rate_hz=1_024_000,
            gain_db=28,
        )
        failed = CaptureConfig(
            center_frequency_hz=100_250_000,
            sample_rate_hz=1_024_000,
            gain_db=12.3,
        )
        module = types.ModuleType("rtlsdr")
        module.RtlSdr = FakePyRtlSdr
        with patch.dict(sys.modules, {"rtlsdr": module}):
            stream = RtlPyStream(original)
            try:
                with self.assertRaisesRegex(RtlSdrError, "gain setter failed"):
                    stream.tune(failed)
                self.assertIsNone(stream._config)

                stream.tune(original)
                self.assertEqual(stream._config, original.validated())
                self.assertEqual(instances[0].sample_rates, [1_024_000, 1_024_000])
                self.assertEqual(
                    instances[0].center_frequencies,
                    [100_000_000, 100_250_000, 100_000_000],
                )
                self.assertEqual(instances[0].gains, [28, 12.3, 28])
            finally:
                stream.close()

    def test_tuning_error_preserves_the_opened_receiver_identity(self) -> None:
        class FakePyRtlSdr:
            def __init__(self, **arguments) -> None:
                self.arguments = arguments
                self.freq_correction = None
                self.center_freq = None
                self.gain = None

            @property
            def sample_rate(self):
                return None

            @sample_rate.setter
            def sample_rate(self, value) -> None:
                raise ValueError(f"invalid rate {value}")

            def close(self) -> None:
                pass

        identity = {
            "display_name": "Nooelec NESDR SMArt v5",
            "manufacturer": "Nooelec",
            "product": "NESDR SMArt v5",
            "serial": "33864718",
            "driver_name": "RTL-SDR",
        }
        module = types.ModuleType("rtlsdr")
        module.RtlSdr = FakePyRtlSdr
        with (
            patch.dict(sys.modules, {"rtlsdr": module}),
            patch.object(RtlPyStream, "_read_identity", return_value=identity),
            self.assertRaises(RtlSdrError) as raised,
        ):
            RtlPyStream(CaptureConfig(sample_rate_hz=1_024_000))

        self.assertIn("PyRTLSDR tuning failed", str(raised.exception))
        self.assertEqual(raised.exception.receiver_info, identity)
        self.assertEqual(raised.exception.error_stage, "tuning")


if __name__ == "__main__":
    unittest.main()
