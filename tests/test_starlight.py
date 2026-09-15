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


class StarlightShadingTests(unittest.TestCase):
    def canonical(self):
        from facegate.starlight import load_surface
        points = load_surface()
        # Physical XYZ, placed with one uniform scale in all dimensions.
        points *= 8
        points[:, :2] += [156, 104]
        return np.concatenate([points, np.tile(points[1], (10, 1))])

    def test_lighting_is_invariant_to_uniform_scale_and_translation(self):
        stars = Starlight(312, 208, STAR_STYLE)
        points = self.canonical()
        stars.submit_mesh(points)
        first = stars._face_dust()[1]
        stars.vertices = points * 1.25 + [12, -5, 30]
        second = stars._face_dust()[1]
        np.testing.assert_allclose(first, second, atol=1e-5)

    def test_back_facing_surface_does_not_get_flipped_toward_light(self):
        stars = Starlight(312, 208, STAR_STYLE)
        points = self.canonical()
        stars.submit_mesh(points)
        front = stars._face_dust()[1].sum()
        turned = points.copy()
        turned[:, [0, 2]] *= -1  # 180 degree rotation, not a reflection.
        stars.vertices = turned
        back = stars._face_dust()[1].sum()
        self.assertLess(back, front * 0.1)

    def test_surface_sampling_does_not_depend_on_first_capture_pose(self):
        points = self.canonical()
        first = Starlight(312, 208, STAR_STYLE)
        second = Starlight(312, 208, STAR_STYLE)
        first.submit_mesh(points)
        turned = points.copy()
        turned[:, 0] = points[:, 0] * 0.6 + points[:, 2] * 0.8
        second.submit_mesh(turned)
        # The surface samples precede the small feature-stroke allocation.
        from facegate.starlight import FEATURE_SHARE
        count = len(first.face_stars) - int(len(first.face_stars) * FEATURE_SHARE)
        np.testing.assert_array_equal(first.surface["corners"][:count], second.surface["corners"][:count])
        np.testing.assert_array_equal(first.surface["weights"][:count], second.surface["weights"][:count])

    def test_reset_releases_previous_face_samples_and_radiance(self):
        stars = Starlight(312, 208, STAR_STYLE)
        stars.submit_mesh(self.canonical())
        stars.reset()
        self.assertIsNone(stars.surface)
        self.assertIsNone(stars.mesh_target)
        self.assertIsNone(stars.dust)

    def test_iris_catchlights_inherit_valid_surface_orientation(self):
        stars = Starlight(312, 208, STAR_STYLE)
        normal = stars._normals(self.canonical())
        np.testing.assert_allclose(np.linalg.norm(normal[468:478], axis=1), np.ones(10), atol=1e-6)
        self.assertTrue(np.all(normal[468:478, 2] > 0))

    def test_stationary_face_has_no_periodic_light_flicker(self):
        stars = Starlight(312, 208, STAR_STYLE)
        points = self.canonical()
        stars.submit_mesh(points)
        for _ in range(180):
            stars.advance(1 / 60)
        samples = []
        for frame in range(420):
            if frame % 6 == 0:
                stars.submit_mesh(points)
            stars.advance(1 / 60)
            samples.append(stars.light[stars.face_stars].copy())
        samples = np.asarray(samples)
        # Central grains stay clear of the outer feather; this covers a full
        # former six-second scan cycle plus the independent glint pulses.
        central = np.linalg.norm(stars.dust[0] - [156, 104], axis=1) < 45
        self.assertLess(float(np.max(np.ptp(samples[:, central], axis=0))), 0.002)

    def test_depth_noise_is_smoothed_before_it_can_move_the_shading(self):
        stars = Starlight(312, 208, STAR_STYLE)
        points = self.canonical()
        stars.submit_mesh(points)
        noise = np.random.default_rng(42).normal(0, 0.9, len(points))
        errors = []
        for frame in range(30):
            for _ in range(6):
                stars.advance(1 / 60)
            jittered = points.copy()
            jittered[:, 2] += noise * (-1 if frame % 2 else 1)
            stars.submit_mesh(jittered)
            if frame > 10:
                errors.append(np.abs(stars.vertices[:, 2] - points[:, 2]).mean())
        self.assertLess(float(np.mean(errors)), float(np.abs(noise).mean()) * 0.45)

    def test_coaching_does_not_dim_the_formed_face(self):
        stars = Starlight(312, 208, STAR_STYLE)
        stars.submit_mesh(self.canonical())
        for _ in range(180):
            stars.advance(1 / 60)
        before = stars.light[stars.face_stars].copy()
        stars.set_arrow("closer")
        for _ in range(90):
            stars.advance(1 / 60)
        central = np.linalg.norm(stars.dust[0] - [156, 104], axis=1) < 45
        np.testing.assert_allclose(stars.light[stars.face_stars][central], before[central], atol=0.002)

    def test_turning_face_and_its_shading_share_the_displayed_pose(self):
        stars = Starlight(312, 208, STAR_STYLE)
        points = self.canonical()
        stars.submit_mesh(points)
        for _ in range(180):
            stars.advance(1 / 60)
        for frame in range(120):
            # Camera poses arrive at 15 Hz while rendering runs at 60 Hz.
            if frame % 4 == 0:
                angle = np.sin(frame / 60 * np.pi) * 0.5
                turned = points.copy()
                x, z = points[:, 0] - 156, points[:, 2]
                turned[:, 0] = 156 + x * np.cos(angle) + z * np.sin(angle)
                turned[:, 2] = -x * np.sin(angle) + z * np.cos(angle)
                stars.submit_mesh(turned)
            stars.advance(1 / 60)
            rest, expected_light = stars._face_dust()
            np.testing.assert_allclose(stars.dust[1], expected_light, atol=1e-6)
            # Only the intentional subpixel dust drift can offset the surface.
            self.assertLess(float(np.max(np.abs(stars.position[stars.face_stars] - rest))), 0.15)
