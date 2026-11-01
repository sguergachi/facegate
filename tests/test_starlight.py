"""Temporal regressions for preview transitions; no camera or display needed."""
import unittest

import numpy as np

from facegate.edge_hud import STAR_STYLE
from facegate.starlight import Starlight, load_triangles, DISTANCE_SECONDS, SOURCE


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

    def test_a_released_face_condenses_again_when_the_mesh_returns(self):
        stars = Starlight(312, 208, STAR_STYLE)
        points = np.concatenate([
            np.linspace([120, 70, 2], [190, 150, 8], 468),
            np.tile([156, 104, 4], (10, 1)),
        ]).astype(np.float32)
        stars.submit_mesh(points)
        for _ in range(180):
            stars.advance(1 / 60)
        stars.submit_mesh(np.empty((0, 3)))
        for _ in range(30):
            stars.advance(1 / 60)
        self.assertFalse(stars.face)
        self.assertGreater(stars.formed, 0.0)
        stars.submit_mesh(points)
        self.assertTrue(stars.face)
        before = stars.formed
        for _ in range(30):
            stars.advance(1 / 60)
        self.assertGreater(stars.formed, before)

    def test_phase_dust_writes_a_checkmark_then_the_face_returns(self):
        from facegate.starlight import MARK_RING, MARK_FILL, load_surface

        stars = Starlight(312, 208, STAR_STYLE)
        points = load_surface()
        points = points * 8
        points[:, :2] += [156, 104]
        points = np.concatenate([points, np.tile(points[1], (10, 1))])
        stars.submit_mesh(points)
        for _ in range(180):
            stars.advance(1 / 60)
        stars.submit_mesh(np.empty((0, 3)), mark=True)
        self.assertTrue(stars.mark)
        self.assertFalse(stars.face)
        for _ in range(90):
            stars.advance(1 / 60)
        rest, _ = stars._mark_dust()
        pos = stars.position[stars.face_stars]
        self.assertLess(float(np.median(np.linalg.norm(pos - rest, axis=1))), 6)
        on_ring = stars.mark_along < MARK_RING
        on_check = stars.mark_along >= MARK_RING + MARK_FILL
        centre = rest.mean(axis=0)
        ring_r = np.linalg.norm(rest[on_ring] - centre, axis=1)
        check_r = np.linalg.norm(rest[on_check] - centre, axis=1)
        self.assertGreater(float(ring_r.mean()), float(check_r.mean()) * 1.25)
        ring_w = float(np.ptp(rest[on_ring, 0]))
        ring_h = float(np.ptp(rest[on_ring, 1]))
        self.assertGreater(ring_w, 80)
        self.assertGreater(ring_h, 80)
        self.assertLess(abs(ring_w - ring_h) / max(ring_w, ring_h), 0.12)
        start, corner, end = stars._mark_points()
        self.assertGreater(float(end[0] - start[0]), 20)
        self.assertLess(float(end[1]), float(corner[1]))
        stars.submit_mesh(points)
        self.assertFalse(stars.mark)
        self.assertTrue(stars.face)
        for _ in range(180):
            stars.advance(1 / 60)
        face = stars.dust[0]
        self.assertLess(float(np.max(np.abs(stars.position[stars.face_stars] - face))), 3)

    def test_seal_keeps_tilting_until_the_camera_returns(self):
        from facegate.starlight import MARK_TURN_SECONDS, load_surface

        stars = Starlight(312, 208, STAR_STYLE)
        points = load_surface()
        points = points * 8
        points[:, :2] += [156, 104]
        points = np.concatenate([points, np.tile(points[1], (10, 1))])
        stars.submit_mesh(points)
        for _ in range(60):
            stars.advance(1 / 60)
        stars.submit_mesh(np.empty((0, 3)), mark=True, axis="down")
        stars.advance(1 / 60)
        start_tilt = stars._mark_basis()[1]
        self.assertGreater(start_tilt, 0.2)
        for _ in range(int(MARK_TURN_SECONDS * 60)):
            stars.advance(1 / 60)
        mid_tilt = stars._mark_basis()[1]
        self.assertLess(mid_tilt, start_tilt - 0.15)
        for _ in range(24):
            stars.advance(1 / 60)
        later_tilt = stars._mark_basis()[1]
        self.assertLess(later_tilt, mid_tilt - 0.02)
        stars.submit_mesh(points)
        for _ in range(12):
            stars.advance(1 / 60)
        self.assertLess(stars._mark_basis()[1], later_tilt)
        self.assertGreater(stars.formed, 0.0)

    def test_seal_turns_on_the_axis_of_the_pose_just_captured(self):
        from facegate.starlight import MARK_TURN_SECONDS, load_surface

        def formed():
            stars = Starlight(312, 208, STAR_STYLE)
            points = load_surface()
            points = points * 8
            points[:, :2] += [156, 104]
            points = np.concatenate([points, np.tile(points[1], (10, 1))])
            stars.submit_mesh(points)
            for _ in range(30):
                stars.advance(1 / 60)
            return stars

        left = formed()
        left.submit_mesh(np.empty((0, 3)), mark=True, axis="left")
        left.advance(1 / 60)
        yaw, tilt = left._mark_basis()
        self.assertGreater(abs(yaw), abs(tilt) * 4)
        self.assertGreater(yaw, 0)
        left.advance(MARK_TURN_SECONDS)
        later_yaw, _ = left._mark_basis()
        self.assertLess(later_yaw, yaw)
        up = formed()
        up.submit_mesh(np.empty((0, 3)), mark=True, axis="up")
        up.advance(1 / 60)
        yaw, tilt = up._mark_basis()
        self.assertGreater(abs(tilt), abs(yaw) * 4)
        self.assertLess(tilt, 0)
        up.advance(MARK_TURN_SECONDS)
        _, later_tilt = up._mark_basis()
        self.assertGreater(later_tilt, tilt)

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

    def test_guide_dust_is_drawn_apart_in_its_own_colour(self):
        from facegate.starlight import Look

        stars = self.make_face()
        stars.presence_target = 1.0
        stars.set_arrow("turn-left")
        for _ in range(60):
            stars.advance(1 / 60)
        calls = []
        splat = stars._splat
        stars._splat = lambda scale, lights, *rest, **kw: calls.append(lights) or splat(scale, lights, *rest, **kw)
        stars.stars(1.0, Look(guide=(0.39, 0.82, 1.0)))
        (face,), (guide,) = calls[-2:]
        # The guide's few hundred motes are splatted into their own box only.
        (texture,), box = stars.splat_cropped(1.0, guide)
        self.assertLess(box.get_width() * box.get_height(), 0.5 * 312 * 208)
        self.assertEqual(float(face[stars.arrow_stars].max()), 0.0)
        self.assertEqual(float(guide[stars.face_stars].max()), 0.0)
        self.assertGreater(float(guide[stars.arrow_stars].max()), 0.0)

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

    def test_source_sits_behind_the_head_and_scales_with_it(self):
        stars = Starlight(312, 208, STAR_STYLE)
        points = self.canonical()
        stars.submit_mesh(points)
        x, y = stars._source_centre()
        low, high = points[:468, :2].min(axis=0), points[:468, :2].max(axis=0)
        centre = (low + high) / 2
        height = float(high[1] - low[1])
        self.assertAlmostEqual(x, float(centre[0]), delta=0.5)
        # Above the face centre so the core leaks around the crown.
        self.assertLess(y, float(centre[1]) - 0.1 * height)
        self.assertGreater(y, float(low[1]))
        # Core smaller than the face, corona wrapping it, wash filling the scene.
        core, corona, wash = SOURCE
        scale = stars._source_scale()
        self.assertLess(core[1] * scale, height)
        self.assertGreater(corona[1] * scale, height * 0.85)
        self.assertGreater(wash[0] * scale, corona[0] * scale)
        # A closer face gets a larger corona, still wrapping that head.
        closer = points.copy()
        closer[:, :2] = (closer[:, :2] - [156, 104]) * 1.3 + [156, 104]
        stars.vertices = closer
        self.assertGreater(stars._source_scale(), scale)

    def test_source_follows_the_face_and_haze_is_brighter_near_it(self):
        stars = Starlight(312, 208, STAR_STYLE)
        points = self.canonical()
        stars.submit_mesh(points)
        first = stars._source_centre()
        shifted = points.copy()
        shifted[:, 0] += 24
        stars.vertices = shifted
        second = stars._source_centre()
        self.assertAlmostEqual(second[0] - first[0], 24, delta=0.5)
        self.assertAlmostEqual(second[1], first[1], delta=0.5)
        # Haze motes nearer the core carry more light than those at the edge.
        stars.scene = 1.0
        stars.haze_angle[:] = 0
        stars.haze_radius[:] = np.linspace(0.05, 1.25, len(stars.haze_radius))
        stars.haze_light[:] = 0.5
        stars.haze_speed[:] = 0
        stars._haze(1.0)
        centre = np.array(stars._source_centre())
        angle = stars.haze_angle
        positions = centre + np.stack(
            [np.cos(angle) * 78, np.sin(angle) * 96], axis=1
        ) * stars.haze_radius[:, None]
        dist = np.hypot(positions[:, 0] - centre[0], positions[:, 1] - centre[1])
        near, far = dist < 40, dist > 90
        self.assertTrue(near.any() and far.any())
        falloff = 0.40 + 0.60 * np.clip(1 - dist / 130.0, 0, 1) ** 1.4
        self.assertGreater(float(falloff[near].mean()), float(falloff[far].mean()) * 1.4)
