from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
import time
import types
import unittest
from unittest.mock import patch

from meteor_sdr.config import CaptureConfig
from meteor_sdr.rtl import RtlPyStream, RtlTcpStream


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


if __name__ == "__main__":
    unittest.main()
