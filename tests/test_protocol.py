import unittest

from facegate.protocol import MAX_MESSAGE_BYTES, decode, encode


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self):
        message = {"id": "test", "phase": "approval", "timeout_ms": 12_000}
        self.assertEqual(decode(encode(message)), message)

    def test_rejects_a_non_object(self):
        with self.assertRaises(ValueError):
            decode(b"[]")

    def test_rejects_large_payload(self):
        with self.assertRaises(ValueError):
            decode(b"{" + b"x" * MAX_MESSAGE_BYTES + b"}")
