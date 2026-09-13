import unittest
from unittest import mock

from facegate import settings_app


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


class GeometryTests(unittest.TestCase):
    def test_layout_is_on_the_spacing_scale(self):
        """Every length is on the 4-point grid; structure is on the 8-point rhythm."""
        grid = [
            "PAGE_MARGIN", "BUTTON_HEIGHT", "BUTTON_GAP", "WINDOW_RADIUS", "CLOSE_SIZE", "CLOSE_INSET", "CONTENT_WIDTH",
            "HERO_SIZE", "HERO_TOP", "MARK_TO_TITLE", "TITLE_TO_DETAIL", "HERO_BOTTOM", "BAR_HEIGHT", "BAR_MARK", "BAR_GAP",
            "HEADER_FEATHER", "SECTION_TO_ROWS", "BOTTOM_FEATHER", "BUTTON_CLEARANCE", "BOTTOM_BAR",
            "ROW_PADDING", "AVATAR_SIZE", "ROW_HEIGHT", "ROW_RADIUS", "ROW_GAP",
            "FLOW_TOP", "ART_HEIGHT", "ART_TO_TITLE", "TITLE_TO_BODY", "COPY_TO_EXTRA", "LIST_GAP", "ICON_SIZE",
            "ICON_TO_TEXT", "DOTS_TO_BUTTON", "ART_MARK", "WINDOW_WIDTH", "WINDOW_HEIGHT", "STORY_WIDTH", "STORY_HEIGHT",
        ]
        for name in grid:
            self.assertEqual(getattr(settings_app, name) % 4, 0, name)
        structure = ["PAGE_MARGIN", "BUTTON_HEIGHT", "BUTTON_GAP", "WINDOW_RADIUS", "CLOSE_SIZE", "BAR_HEIGHT", "ROW_HEIGHT", "ROW_RADIUS", "ROW_GAP", "ART_TO_TITLE", "COPY_TO_EXTRA", "DOTS_TO_BUTTON"]
        for name in structure:
            self.assertEqual(getattr(settings_app, name) % 8, 0, name)
        self.assertEqual(settings_app.PageDots.DOT % 4, 0)
        self.assertEqual(settings_app.ROW_RADIUS, settings_app.AVATAR_SIZE // 2 + settings_app.ROW_PADDING)
        self.assertEqual(settings_app.ROW_HEIGHT, 2 * settings_app.ROW_RADIUS)

    def test_corners_are_concentric_with_the_primary_button(self):
        self.assertEqual(settings_app.WINDOW_RADIUS, settings_app.BUTTON_HEIGHT // 2 + settings_app.PAGE_MARGIN)
        # The close button's centre sits on the corner radius's centre.
        self.assertEqual(settings_app.CLOSE_INSET + settings_app.CLOSE_SIZE // 2, settings_app.WINDOW_RADIUS)
        # Collapsed, the header's content shares the close button's centre line.
        self.assertEqual(settings_app.BAR_HEIGHT / 2, settings_app.CLOSE_INSET + settings_app.CLOSE_SIZE / 2)


if __name__ == "__main__":
    unittest.main()
