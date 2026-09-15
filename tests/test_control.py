import socket
import tempfile
import threading
import unittest
from pathlib import Path

from facegate.control import request_config
from facegate.protocol import decode, encode


class ConfigClientTests(unittest.TestCase):
    def test_launcher_asks_the_resident_island_to_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facegate.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listener.listen(1)
            received = {}

            def serve():
                connection, _ = listener.accept()
                with connection:
                    received.update(decode(connection.recv(4096).split(b"\n", 1)[0]))
                    connection.sendall(encode({"ok": True}))
                listener.close()

            thread = threading.Thread(target=serve)
            thread.start()
            self.assertEqual(request_config(True, path), {"ok": True})
            thread.join(timeout=1)
            self.assertEqual(received, {"phase": "config", "welcome": True})


if __name__ == "__main__":
    unittest.main()
