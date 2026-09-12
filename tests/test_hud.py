import unittest

import cairo

from facegate.edge_hud import LivingFace
from facegate.hud import approval_timeout_ms


class HudRequestTests(unittest.TestCase):
    def test_approval_timeout_is_clamped_without_an_int_base_error(self):
        self.assertEqual(approval_timeout_ms(12_000), 12_000)
        self.assertEqual(approval_timeout_ms(80_000), 30_000)
        self.assertEqual(approval_timeout_ms(1), 1_000)

    def test_presence_mark_draws_all_interaction_states(self):
        """The animated mark must never disappear because one state raises."""
        for state in ("approval", "scanning", "success", "failure"):
            mark = LivingFace.__new__(LivingFace)
            mark.state = state
            mark.previous_state = state
            mark.transition = 1.0
            mark.phase = 0.37
            mark.elapsed = 1.2
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 128, 128)
            mark._draw(None, cairo.Context(surface), 128, 128)
