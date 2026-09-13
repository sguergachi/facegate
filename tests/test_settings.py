import unittest
from unittest import mock

from facegate import settings_app
from facegate import edge_hud


class HelperCommandTests(unittest.TestCase):
    def test_listing_uses_the_prompt_free_helper(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("FACEGATE_FACES_HELPER", None)
            self.assertEqual(settings_app.helper_command(["list"]), ["pkexec", settings_app.LIST_HELPER])
            self.assertEqual(settings_app.helper_command(["remove", "3"]), ["pkexec", settings_app.CHANGE_HELPER, "remove", "3"])
            self.assertEqual(settings_app.helper_command(["enroll"]), ["pkexec", settings_app.CHANGE_HELPER, "enroll"])

    def test_development_override_receives_the_same_arguments(self):
        with mock.patch.dict("os.environ", {"FACEGATE_FACES_HELPER": "python3 /tmp/fake.py"}):
            self.assertEqual(settings_app.helper_command(["enroll"]), ["python3", "/tmp/fake.py", "enroll"])


def bare_hud():
    """An EdgeHud with its state but no layer-shell window."""
    hud = edge_hud.EdgeHud.__new__(edge_hud.EdgeHud)
    hud.pending = hud.enrollment = hud.expected_enrollment = hud.polkit = hud.view = None
    hud.dismiss_timeout = hud.enrollment_start = None
    hud.silenced = False
    hud.preview = mock.Mock()
    hud._present = mock.Mock()
    hud.dismiss = mock.Mock(return_value=False)
    return hud


class IslandBridgeTests(unittest.TestCase):
    def test_approval_turns_straight_into_the_preview_before_the_first_frame(self):
        hud = bare_hud()
        cancel = mock.Mock()
        bridge = settings_app.IslandBridge(hud, cancel)
        bridge.expect()
        hud._present.assert_not_called()

        # The agent reports the password approval: no socket, no waiting.
        hud.enrollment_authorized()
        hud._present.assert_called_once_with(edge_hud.ENROLLING)
        self.assertTrue(hud.enrolling)

        # The helper's first frame adopts the scan already on screen.
        self.assertTrue(bridge.open())
        hud._present.assert_called_once()
        bridge.send({"progress": 0.4})
        hud.preview.set_progress.assert_called_once_with(0.4)
        frame = {"frame": "AAAA", "w": 312, "h": 208}
        bridge.send(frame)
        hud.preview.take_frame.assert_called_once_with(frame)

        # Escape in the island reaches the panel so the helper stops.
        hud.cancel = edge_hud.EdgeHud.cancel.__get__(hud)
        hud.cancel()
        cancel.assert_called_once()

    def test_a_face_approval_lets_verified_land_and_escape_returns_to_configuration(self):
        hud = bare_hud()
        cancel = mock.Mock()
        bridge = settings_app.IslandBridge(hud, cancel)
        bridge.expect()
        hud.view = edge_hud.SUCCESS
        self.assertTrue(hud.enrollment_authorized())
        hud._present.assert_not_called()
        self.assertIsNotNone(hud.enrollment_start)

        edge_hud.EdgeHud.cancel(hud)
        cancel.assert_called_once()
        hud.dismiss.assert_called_once()
        self.assertIsNone(hud.enrollment_start)
        self.assertFalse(bridge.open())

    def test_a_first_frame_during_verified_starts_the_scan_early(self):
        hud = bare_hud()
        bridge = settings_app.IslandBridge(hud, lambda: None)
        bridge.expect()
        hud.view = edge_hud.SUCCESS
        hud.enrollment_authorized()
        self.assertTrue(bridge.open())
        hud._present.assert_called_once_with(edge_hud.ENROLLING)
        self.assertIsNone(hud.enrollment_start)

    def test_a_first_frame_without_the_agent_still_starts_the_scan(self):
        hud = bare_hud()
        bridge = settings_app.IslandBridge(hud, lambda: None)
        bridge.expect()
        self.assertTrue(bridge.open())
        hud._present.assert_called_once_with(edge_hud.ENROLLING)

    def test_a_late_face_result_never_replaces_the_preview(self):
        hud = bare_hud()
        bridge = settings_app.IslandBridge(hud, lambda: None)
        bridge.expect()
        bridge.open()
        hud.present_result(True)
        hud._present.assert_called_once_with(edge_hud.ENROLLING)

    def test_closing_before_approval_forgets_the_scan(self):
        hud = bare_hud()
        bridge = settings_app.IslandBridge(hud, lambda: None)
        bridge.expect()
        bridge.close()
        hud.enrollment_authorized()
        hud._present.assert_not_called()
        self.assertFalse(bridge.open())


class PreviewFrameTests(unittest.TestCase):
    def test_a_streamed_frame_becomes_the_latest_preview_image(self):
        import base64
        from types import SimpleNamespace

        preview = SimpleNamespace(latest=None)
        pixels = bytes(range(256)) * (312 * 208 // 256) + bytes(312 * 208 % 256)
        edge_hud.PreviewArea.take_frame(preview, {"frame": base64.b64encode(pixels).decode(), "w": 312, "h": 208})
        self.assertEqual((preview.latest.get_width(), preview.latest.get_height()), (312, 208))
        edge_hud.PreviewArea.take_frame(preview, {"frame": "AAAA", "w": 10, "h": 10})
        self.assertEqual(preview.latest.get_width(), 312)


class GeometryTests(unittest.TestCase):
    def test_layout_is_on_the_spacing_scale(self):
        """Every length is on the 4-point grid; structure is on the 8-point rhythm."""
        grid = [
            "PAGE_MARGIN", "BUTTON_HEIGHT", "BUTTON_GAP", "PANEL_RADIUS", "CLOSE_SIZE", "CLOSE_INSET", "CONTENT_WIDTH",
            "HERO_SIZE", "HERO_TOP", "MARK_TO_TITLE", "TITLE_TO_DETAIL", "HERO_BOTTOM", "BAR_HEIGHT", "BAR_MARK", "BAR_GAP",
            "HEADER_FEATHER", "SECTION_TO_ROWS", "BOTTOM_FEATHER", "BUTTON_CLEARANCE", "BOTTOM_BAR",
            "ROW_PADDING", "AVATAR_SIZE", "ROW_HEIGHT", "ROW_RADIUS", "ROW_GAP",
            "FLOW_TOP", "ART_HEIGHT", "ART_TO_TITLE", "TITLE_TO_BODY", "COPY_TO_EXTRA", "LIST_GAP", "ICON_SIZE",
            "ICON_TO_TEXT", "DOTS_TO_BUTTON", "ART_MARK", "PANEL_WIDTH", "PANEL_HEIGHT", "STORY_WIDTH", "STORY_HEIGHT",
        ]
        for name in grid:
            self.assertEqual(getattr(settings_app, name) % 4, 0, name)
        structure = ["PAGE_MARGIN", "BUTTON_HEIGHT", "BUTTON_GAP", "PANEL_RADIUS", "CLOSE_SIZE", "BAR_HEIGHT", "ROW_HEIGHT", "ROW_RADIUS", "ROW_GAP", "ART_TO_TITLE", "COPY_TO_EXTRA", "DOTS_TO_BUTTON"]
        for name in structure:
            self.assertEqual(getattr(settings_app, name) % 8, 0, name)
        self.assertEqual(settings_app.PageDots.DOT % 4, 0)
        self.assertEqual(settings_app.ROW_RADIUS, settings_app.AVATAR_SIZE // 2 + settings_app.ROW_PADDING)
        self.assertEqual(settings_app.ROW_HEIGHT, 2 * settings_app.ROW_RADIUS)

    def test_corners_are_concentric_with_the_primary_button(self):
        self.assertEqual(settings_app.PANEL_RADIUS, settings_app.BUTTON_HEIGHT // 2 + settings_app.PAGE_MARGIN)
        # The close button's centre sits on the corner radius's centre.
        self.assertEqual(settings_app.CLOSE_INSET + settings_app.CLOSE_SIZE // 2, settings_app.PANEL_RADIUS)
        # Collapsed, the header's content shares the close button's centre line.
        self.assertEqual(settings_app.BAR_HEIGHT / 2, settings_app.CLOSE_INSET + settings_app.CLOSE_SIZE / 2)

    def test_configuration_is_the_same_composition_inside_the_island(self):
        self.assertEqual(edge_hud.CONFIG_WIDTH, settings_app.PANEL_WIDTH)
        self.assertEqual(edge_hud.CONFIG_HEIGHT, settings_app.PANEL_HEIGHT)
        self.assertEqual(edge_hud.CONFIG_RADIUS, settings_app.PANEL_RADIUS)
        self.assertLess(edge_hud.ISLAND_WIDTH, edge_hud.CONFIG_WIDTH)

    def test_each_later_onboarding_story_step_has_a_back_target(self):
        self.assertEqual(settings_app.BACK_TARGETS, {"how": "welcome", "privacy": "how", "added": "privacy"})

    def test_first_face_explains_the_password_bootstrap(self):
        title, body, detail = settings_app.setup_auth_copy(True, False)
        self.assertEqual(title, "Enter your sudo password")
        self.assertIn("approve itself", body)
        self.assertIn("sudo password", detail)
        self.assertNotIn("password", settings_app.setup_auth_copy(True, True)[0].lower())


if __name__ == "__main__":
    unittest.main()
