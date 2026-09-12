import unittest

import cairo

from facegate.edge_hud import ISLAND_WIDTH, MarkModel, Spring, Tween, island_path, paint_mark
from facegate.hud import approval_timeout_ms


class HudRequestTests(unittest.TestCase):
    def test_approval_timeout_is_clamped_without_an_int_base_error(self):
        self.assertEqual(approval_timeout_ms(12_000), 12_000)
        self.assertEqual(approval_timeout_ms(80_000), 30_000)
        self.assertEqual(approval_timeout_ms(1), 1_000)

    def test_presence_mark_draws_all_interaction_states_and_blends(self):
        """The animated mark must never disappear because one state raises."""
        states = ("approval", "scanning", "success", "failure")
        for previous in states:
            for state in states:
                for transition in (0.0, 0.5, 1.0):
                    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 72, 72)
                    model = MarkModel(state, previous, transition, 1.2, 0.2, 0.37)
                    paint_mark(cairo.Context(surface), 72, model)


class MotionTests(unittest.TestCase):
    def test_spring_settles_on_target_with_restrained_overshoot(self):
        spring = Spring(0.0)
        spring.configure(0.56, 0.8)
        spring.target = 100.0
        peak = 0.0
        for _ in range(165):
            spring.step(1 / 165)
            peak = max(peak, spring.value)
        self.assertAlmostEqual(spring.value, 100.0, delta=0.5)
        self.assertLess(peak, 103.0)

    def test_tween_holds_through_delay_then_arrives(self):
        tween = Tween()
        tween.to(1.0, now=10.0, duration=0.4, delay=0.1)
        self.assertEqual(tween.value(10.05), 0.0)
        self.assertGreater(tween.value(10.3), 0.5)
        self.assertEqual(tween.value(10.6), 1.0)

    def test_island_path_handles_collapsed_and_full_sizes(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
        for width, height in ((0, 0), (124, 0.5), (124, 6), (ISLAND_WIDTH, 226)):
            context = cairo.Context(surface)
            island_path(context, 200, width, height)
            context.fill()
