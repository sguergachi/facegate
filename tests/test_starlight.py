"""Temporal regressions for preview transitions; no camera or display needed."""
import unittest

import numpy as np

from facegate.edge_hud import STAR_STYLE
from facegate.starlight import Starlight, load_triangles, DISTANCE_SECONDS


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


class StarlightGeometryTests(unittest.TestCase):
    def test_topology_covers_every_landmark_and_forehead_boundary(self):
        triangles = load_triangles()
        self.assertIsNotNone(triangles)
        np.testing.assert_array_equal(np.unique(triangles), np.arange(468))
        # The crown must connect to both sides of the forehead. The previous
        # camera-derived topology omitted this point and produced a notch.
        edges = np.sort(np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]]), axis=1)
        for edge in ([10, 109], [10, 338]):
            self.assertEqual(int(np.sum(np.all(edges == edge, axis=1))), 1)
        _, counts = np.unique(edges, axis=0, return_counts=True)
        self.assertTrue(np.all(counts <= 2), "No overlapping/non-manifold edges")

    def test_distance_cues_grow_for_closer_and_shrink_for_back(self):
        stars = Starlight(312, 208, STAR_STYLE)
        for name, direction in (("closer", 1), ("back", -1)):
            widths = []
            for phase in (0.15, 0.6):
                points, light = stars.arrow_shape(name, phase * DISTANCE_SECONDS)
                self.assertTrue(np.isfinite(points).all())
                self.assertTrue(np.isfinite(light).all())
                outline = stars.guide_lines[1][0]
                widths.append(float(np.ptp(outline[:, 0])))
                # At the right side the tip points out for closer, in for back.
                chevron = stars.guide_lines[-1][0]
                self.assertGreater(direction * float(chevron[1, 0] - chevron[0, 0]), 0)
                for line, _ in stars.guide_lines:
                    self.assertTrue(np.all(line >= [0, 0]))
                    self.assertTrue(np.all(line <= [312, 208]))
            self.assertGreater(direction * (widths[1] - widths[0]), 0)
