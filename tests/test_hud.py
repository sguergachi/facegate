import unittest

import cairo

from facegate.edge_hud import APPROVAL, ISLAND_WIDTH, approval_view, MARK_PROFILES, MarkModel, Spring, Tween, island_path, mark_values, paint_mark, ring_head
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
                    model = MarkModel(state, MARK_PROFILES[previous], transition, 1.2, 0.2, 0.37)
                    paint_mark(cairo.Context(surface), 72, model)

    def test_ring_completion_is_continuous_frame_to_frame(self):
        """Scanning to success must never pop.

        The moving head changes a little every frame, so measure each frame's
        total change: a seam shows up as one frame far above the rest (the old
        comet-to-ring swap measured over 30x the median).
        """
        model = MarkModel("success", MARK_PROFILES["scanning"], 0.0, 0.0, 0.0, 0.3)
        previous = None
        energies = []
        for _ in range(110):
            model.transition = min(1.0, model.transition + 1 / (0.55 * 144))
            model.spin = (model.spin + mark_values(model)[-1] / 144) % 1.0
            model.state_elapsed += 1 / 144
            model.elapsed += 1 / 144
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 72, 72)
            paint_mark(cairo.Context(surface), 72, model)
            alpha = bytes(surface.get_data())[3::4]
            if previous is not None:
                energies.append(sum(abs(a - b) for a, b in zip(alpha, previous)))
            previous = alpha
        median = sorted(energies)[len(energies) // 2]
        self.assertLess(max(energies), 4 * median)

    def test_ring_completes_clockwise(self):
        """In y-down screen space a rising angle is clockwise; the head must never back up."""
        for result in ("success", "failure"):
            model = MarkModel(result, MARK_PROFILES["scanning"], 0.0, 0.0, 0.0, 0.0)
            spin_turns = 0.0
            previous = ring_head(0.0, mark_values(model)[4])
            for _ in range(110):
                model.transition = min(1.0, model.transition + 1 / (0.55 * 144))
                spin_turns += mark_values(model)[-1] / 144
                head = ring_head(spin_turns, mark_values(model)[4])
                self.assertGreaterEqual(head, previous - 1e-9, result)
                previous = head

    def test_state_change_mid_transition_starts_from_what_is_visible(self):
        model = MarkModel("scanning", MARK_PROFILES["approval"], 0.4)
        visible = mark_values(model)
        model.origin, model.state, model.transition = visible, "success", 0.0
        self.assertEqual(mark_values(model), visible)


class RequesterTests(unittest.TestCase):
    def test_names_the_requester_host_and_command(self):
        view = approval_view({"app": "Claude Code", "host": "T3 Code", "command": "sudo pacman -Syu"})
        self.assertEqual(view.title, "Claude Code wants sudo")
        self.assertEqual(view.command, "sudo pacman -Syu")
        self.assertEqual(view.detail, "via T3 Code\nApprove to verify with your face.")
        self.assertEqual(approval_view({"app": "Konsole", "command": "sudo -v"}).detail, "Approve to verify with your face.")
        self.assertTrue(view.actions)

    def test_polkit_requests_ask_for_admin_access(self):
        view = approval_view({"service": "polkit-1", "app": "Dolphin", "command": "pkexec /usr/bin/kate"})
        self.assertEqual(view.title, "Dolphin wants admin access")
        self.assertEqual(approval_view({"service": "polkit-1"}).title, "Admin access requested")

    def test_missing_or_malformed_requester_keeps_the_generic_prompt(self):
        for requester in (None, {}, "Konsole", {"app": "", "command": None}, {"service": "sudo"}):
            self.assertEqual(approval_view(requester), APPROVAL)

    def test_requester_text_is_flattened_and_bounded(self):
        view = approval_view({"app": "Evil\nApp" + "x" * 80, "command": "sudo rm\t-rf /tmp/a\x1b[31m"})
        self.assertNotIn("\n", view.title)
        self.assertLessEqual(len(view.title), len(" wants sudo") + 32)
        self.assertTrue(all(char.isprintable() for char in view.command))


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


class PolkitCopyTests(unittest.TestCase):
    def test_password_view_keeps_the_requester_and_swaps_the_hint(self):
        from facegate.edge_hud import PASSWORD_ERROR, PASSWORD_HINT, password_view

        requester = {"service": "polkit-1", "app": "Konsole", "host": "T3 Code", "command": "pkexec pacman -Rns apollo"}
        view = password_view(requester)
        self.assertTrue(view.password)
        self.assertEqual((view.primary, view.secondary), ("Allow", "Cancel"))
        self.assertEqual(view.title, "Konsole wants admin access")
        self.assertEqual(view.detail, "via T3 Code\n" + PASSWORD_HINT)
        self.assertEqual(password_view(requester, error=True).detail, "via T3 Code\n" + PASSWORD_ERROR)

    def test_polkit_message_explains_requests_without_a_command(self):
        view = approval_view({"service": "polkit-1", "app": "Discover", "message": "Authentication is required to install software"})
        self.assertEqual(view.title, "Discover wants admin access")
        self.assertIn("Authentication is required to install software", view.detail)
        with_command = approval_view({"service": "polkit-1", "app": "Konsole", "command": "pkexec true", "message": "Authentication is needed to run true"})
        self.assertNotIn("Authentication is needed", with_command.detail)


class RequesterModuleTests(unittest.TestCase):
    def test_command_line_is_quoted_flattened_and_bounded(self):
        from facegate.requester import MAX_COMMAND_CHARS, command_line

        self.assertEqual(command_line(["/usr/bin/pkexec", "pacman", "-Rns", "apollo"]), "pkexec pacman -Rns apollo")
        self.assertEqual(command_line(["sudo", "echo", "a b"]), "sudo echo 'a b'")
        self.assertNotIn("\n", command_line(["sudo", "printf", "x\ny"]))
        self.assertEqual(len(command_line(["sudo"] + ["x" * 50] * 10)), MAX_COMMAND_CHARS)

    def test_describes_this_process_tree_without_raising(self):
        import os

        from facegate.requester import describe_app, describe_command

        self.assertIn("command", describe_command(os.getpid()))
        self.assertIsInstance(describe_app(os.getpid()), dict)

    def test_agent_prefers_the_person_at_the_desk(self):
        import os

        from facegate.polkit_agent import choose_identity

        chosen = choose_identity([("unix-user", {"uid": 0}), ("unix-user", {"uid": os.getuid()})])
        self.assertEqual(chosen.get_uid(), os.getuid())
        self.assertEqual(choose_identity([("unix-user", {"uid": 0})]).get_uid(), 0)
        self.assertIsNone(choose_identity([("unix-group", {"gid": 10})]))
