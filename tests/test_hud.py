import socket
import unittest
from unittest import mock

import cairo

from facegate.edge_hud import APPROVAL, ISLAND_WIDTH, approval_view, face_scan_available, MARK_PROFILES, MarkModel, Spring, Tween, island_path, mark_values, paint_mark, ring_head
from facegate.hud import approval_timeout_ms


class HudRequestTests(unittest.TestCase):
    def test_capture_feedback_does_not_flash_face_exposure(self):
        from facegate.edge_hud import PreviewModel, preview_look

        for progress in (0.0, 0.2, 0.8, 1.0):
            model = PreviewModel(progress=progress)
            steady = preview_look(model)
            for pulse in (0.25, 0.5, 1.0):
                model.pulse = pulse
                self.assertEqual(preview_look(model), steady)

    def test_no_gaze_enrollment_skips_straight_to_password(self):
        import os
        import pwd

        username = pwd.getpwuid(os.getuid()).pw_name

        class Faces:
            def __init__(self, records=None, error=None):
                self.records = records or []
                self.error = error

            def list_faces(self, requested):
                if self.error:
                    raise self.error
                return self.records

        self.assertFalse(face_scan_available(username, Faces()))
        self.assertTrue(face_scan_available(username, Faces([("Face", 5, True, True)])))
        self.assertFalse(face_scan_available("a-different-user", Faces([("Face", 5, True, True)])))

    def test_approval_timeout_is_clamped_without_an_int_base_error(self):
        self.assertEqual(approval_timeout_ms(12_000), 12_000)
        self.assertEqual(approval_timeout_ms(80_000), 30_000)
        self.assertEqual(approval_timeout_ms(1), 1_000)

    def test_presence_mark_draws_all_interaction_states_and_blends(self):
        """The animated mark must never disappear because one state raises."""
        states = tuple(MARK_PROFILES)
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

    def test_account_lock_message_is_preserved_without_helper_chatter(self):
        from facegate.polkit_agent import LOCKED_PASSWORD, friendly_pam_message

        self.assertEqual(friendly_pam_message("The account is locked due to 3 failed logins."), LOCKED_PASSWORD)
        self.assertEqual(friendly_pam_message("Authentication failure"), "")


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


class SudoPasswordTests(unittest.TestCase):
    def setUp(self):
        from facegate import edge_hud

        self.edge_hud = edge_hud
        hud = edge_hud.EdgeHud.__new__(edge_hud.EdgeHud)
        hud.pending = hud.enrollment = hud.expected_enrollment = hud.polkit = hud.view = None
        hud.sudo_password = hud.sudo_password_watch = hud.sudo_lock_timer = hud.dismiss_timeout = hud.enrollment_start = None
        hud.silenced = False
        hud.config_active = False
        hud.enabled = True
        hud.present_password = mock.Mock()
        hud.dismiss = mock.Mock(return_value=False)
        self.hud = hud
        self.pam_side, self.hud_side = socket.socketpair()

    def tearDown(self):
        if self.hud.sudo_password_watch:
            self.edge_hud.GLib.source_remove(self.hud.sudo_password_watch)
        self.pam_side.close()
        self.hud_side.close()

    def test_typed_password_goes_back_to_pam_and_waits_for_its_verdict(self):
        self.hud.handle_request({"phase": "password", "requester": {"app": "Konsole"}}, self.hud_side)
        on_password = self.hud.present_password.call_args[0][1]
        on_password("hunter2")
        self.assertEqual(self.pam_side.recv(4096), b'{"password":"hunter2"}\n')
        self.assertIsNone(self.hud.sudo_password)
        self.edge_hud.GLib.source_remove(self.hud.dismiss_timeout)

    def test_escape_hands_sudo_back_to_its_own_prompt(self):
        self.hud.handle_request({"phase": "password"}, self.hud_side)
        self.edge_hud.EdgeHud.cancel(self.hud)
        self.assertEqual(self.pam_side.recv(4096), b'{"password":null}\n')
        self.hud.dismiss.assert_called_once()

    def test_a_locked_sudo_says_when_instead_of_taking_a_password(self):
        import time

        self.hud._show_sudo_lock = mock.Mock()
        until = time.time() + 300
        self.hud.handle_request({"phase": "password", "locked_until": until}, self.hud_side)
        self.hud._show_sudo_lock.assert_called_once_with(None, False, until)
        self.hud.present_password.assert_not_called()
        # An expired lock just asks for the password.
        self.hud.sudo_password = None
        self.edge_hud.GLib.source_remove(self.hud.sudo_password_watch)
        self.hud.handle_request({"phase": "password", "locked_until": time.time() - 5}, socket.socketpair()[1])
        self.hud.present_password.assert_called_once()

    def test_a_retry_always_starts_with_an_empty_field(self):
        hud = self.edge_hud.EdgeHud.__new__(self.edge_hud.EdgeHud)
        hud.silenced = False
        hud.entry = mock.Mock()
        hud.mark = mock.Mock()
        hud._present = mock.Mock()
        hud.password_busy = mock.Mock()
        with mock.patch.object(self.edge_hud.GLib, "timeout_add"):
            self.edge_hud.EdgeHud.present_password(hud, {"app": "paru"}, lambda _p: None, lambda: None, error=True)
        hud.entry.set_text.assert_called_once_with("")
        hud.mark.set_state.assert_called_once_with("denied")

    def test_approval_waits_until_answered(self):
        self.hud.approval_timeout = None
        self.hud.present_approval = mock.Mock()
        self.hud.handle_request({"id": "a", "phase": "approval", "requester": {}}, self.hud_side)
        self.hud.present_approval.assert_called_once()
        self.assertIsNone(self.hud.approval_timeout)
        # Ctrl+C on sudo closes its end: the island goes, rather than waiting forever.
        self.pam_side.close()
        self.hud._approval_hung_up()
        self.assertIsNone(self.hud.pending)
        self.hud.dismiss.assert_called_once()

    def test_turned_off_never_shows_and_lets_pam_use_the_usual_prompt(self):
        self.hud.enabled = False
        self.hud.present_approval = mock.Mock()
        self.hud.handle_request({"phase": "password"}, self.hud_side)
        self.assertEqual(self.pam_side.recv(4096), b'{"password":null,"reason":"disabled"}\n')
        pam_side, hud_side = socket.socketpair()
        self.hud.handle_request({"phase": "approval"}, hud_side)
        self.assertEqual(pam_side.recv(4096), b'{"approved":false,"reason":"disabled"}\n')
        pam_side.close()
        self.hud.present_password.assert_not_called()
        self.hud.present_approval.assert_not_called()


    def test_the_login_screen_island_only_reports_scans(self):
        self.hud.greeter = True
        self.hud.present_approval = mock.Mock()
        self.hud.handle_request({"phase": "approval"}, self.hud_side)
        self.assertEqual(self.pam_side.recv(4096), b'{"approved":false,"reason":"greeter"}\n')
        pam_side, hud_side = socket.socketpair()
        self.hud.handle_request({"phase": "password"}, hud_side)
        self.assertEqual(pam_side.recv(4096), b'{"password":null,"reason":"greeter"}\n')
        pam_side.close()
        self.hud.present_approval.assert_not_called()
        self.hud.present_password.assert_not_called()

    def test_the_login_screen_welcomes_you_back(self):
        self.hud.greeter = True
        self.hud._present = mock.Mock()
        with mock.patch.object(self.edge_hud.GLib, "timeout_add", return_value=0):
            self.hud.present_result(True)
            self.assertEqual(self.hud._present.call_args[0][0].title, "Welcome back")
            # Escape silenced the last login attempt, not the next one.
            self.hud.silenced = True
            self.hud.present_scanning()
        self.assertEqual(self.hud._present.call_args[0][0].title, "Looking for you")


class PreferencesTests(unittest.TestCase):
    def test_placement_and_enabled_defaults_and_updates_keep_other_keys(self):
        import tempfile
        from pathlib import Path

        from facegate import preferences

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertTrue(preferences.enabled(preferences.load(path)))
            self.assertEqual(preferences.placement({}), ("follow", ""))
            preferences.save({"onboarded": True}, path)
            preferences.update(path, enabled=False, placement="fixed", monitor="DP-1")
            settings = preferences.load(path)
            self.assertTrue(settings["onboarded"])
            self.assertFalse(preferences.enabled(settings))
            self.assertEqual(preferences.placement(settings), ("fixed", "DP-1"))
            # A fixed choice without a monitor still follows the pointer.
            self.assertEqual(preferences.placement({"placement": "fixed"}), ("follow", ""))
