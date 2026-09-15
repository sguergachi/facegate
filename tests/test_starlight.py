"""Temporal regressions for preview transitions; no camera or display needed."""
import unittest

import numpy as np

from facegate.edge_hud import STAR_STYLE
from facegate.starlight import Starlight


class StarlightMotionTests(unittest.TestCase):
    def make_face(self):
        stars = Starlight(312, 208, STAR_STYLE)
        count = len(stars.face_stars)
        stars.vertices = np.zeros((478, 3), np.float32)
        stars.surface = {"delay": np.linspace(0.05, 0.7, count).astype(np.float32)}
        stars.dust = (np.tile([156, 104], (count, 1)).astype(np.float32), np.ones(count, np.float32) * 0.2)
        stars.face = True
        return stars

    def test_long_tracking_loss_releases_dust_immediately(self):
        stars = self.make_face()
        for _ in range(600):
            stars.advance(1 / 60)
        stars.submit_mesh(np.empty((0, 3)))
        before = stars.position[stars.face_stars].copy()
        for _ in range(15):
            stars.advance(1 / 60)
        moved = np.linalg.norm(stars.position[stars.face_stars] - before, axis=1)
        self.assertGreater(float(np.percentile(moved, 90)), 1)
        for _ in range(60):
            stars.advance(1 / 60)
        self.assertIsNone(stars.vertices)
        self.assertTrue(np.isfinite(stars.position).all())

    def test_arrow_reversal_dissolves_before_switch_and_can_cancel(self):
        stars = Starlight(312, 208, STAR_STYLE)
        stars.set_arrow("turn-left")
        for _ in range(90):
            stars.advance(1 / 60)
        stars.set_arrow("turn-right")
        stars.advance(1 / 60)
        self.assertEqual(stars.arrow, "turn-left")
        for _ in range(120):
            stars.advance(1 / 60)
        self.assertEqual(stars.arrow, "turn-right")
        self.assertGreater(stars.arrow_amount, 0.9)
        stars.set_arrow("tilt-up")
        stars.set_arrow("")
        for _ in range(120):
            stars.advance(1 / 60)
        self.assertEqual(stars.arrow, "")

    def test_result_releases_face_and_guide(self):
        stars = self.make_face()
        stars.set_arrow("closer")
        for _ in range(120):
            stars.advance(1 / 60)
        stars.fade_out()
        self.assertFalse(stars.face)
        for _ in range(90):
            stars.advance(1 / 60)
        self.assertFalse(stars.visible)
        self.assertEqual(stars.arrow, "")
