from pathlib import Path
from tempfile import TemporaryDirectory
import os
import time
import unittest

from meteor_sdr.config import CaptureConfig
from meteor_sdr.rtl import RtlTcpStream


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


if __name__ == "__main__":
    unittest.main()
