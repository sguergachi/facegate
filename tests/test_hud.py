import unittest

from facegate.hud import approval_timeout_ms


class HudRequestTests(unittest.TestCase):
    def test_approval_timeout_is_clamped_without_an_int_base_error(self):
        self.assertEqual(approval_timeout_ms(12_000), 12_000)
        self.assertEqual(approval_timeout_ms(80_000), 30_000)
        self.assertEqual(approval_timeout_ms(1), 1_000)
