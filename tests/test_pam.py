import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PAM_PATH = Path(__file__).parents[1] / "pam" / "facegate-pam.py"
SPEC = importlib.util.spec_from_file_location("facegate_pam_test", PAM_PATH)
pam = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pam)


class FakePam:
    PAM_AUTHINFO_UNAVAIL = 9
    PAM_SUCCESS = 0
    PAM_AUTH_ERR = 7
    service = "sudo"

    def get_user(self):
        return "sammy"


class FaceModelTests(unittest.TestCase):
    def test_missing_empty_and_malformed_models_use_password(self):
        with tempfile.TemporaryDirectory() as directory:
            models = Path(directory) / "models"
            models.mkdir()
            self.assertFalse(pam.has_face_models("sammy", directory))
            (models / "sammy.dat").write_text("[]")
            self.assertFalse(pam.has_face_models("sammy", directory))
            (models / "sammy.dat").write_text("not json")
            self.assertFalse(pam.has_face_models("sammy", directory))
            (models / "sammy.dat").write_text(json.dumps([{"id": 0, "data": []}]))
            self.assertFalse(pam.has_face_models("sammy", directory))

    def test_nonempty_encoding_is_an_enrolled_face(self):
        with tempfile.TemporaryDirectory() as directory:
            models = Path(directory) / "models"
            models.mkdir()
            (models / "sammy.dat").write_text(json.dumps([{"id": 0, "data": [[0.1, 0.2]]}]))
            self.assertTrue(pam.has_face_models("sammy", directory))

    def test_no_face_returns_before_the_hud_is_contacted(self):
        with (
            mock.patch.object(pam, "should_skip", return_value=False),
            mock.patch.object(pam, "has_face_models", return_value=False),
            mock.patch.object(pam, "request_hud") as request_hud,
        ):
            self.assertEqual(pam.do_auth(FakePam()), FakePam.PAM_AUTHINFO_UNAVAIL)
        request_hud.assert_not_called()

    def test_successful_face_match_clears_password_failures(self):
        with (
            mock.patch.object(pam, "should_skip", return_value=False),
            mock.patch.object(pam, "has_face_models", return_value=True),
            mock.patch.object(pam, "request_hud", return_value={"approved": True}),
            mock.patch.object(pam, "emit_hud"),
            mock.patch.object(pam.requesters, "describe_command", return_value={}),
            mock.patch.object(pam.subprocess, "call", return_value=0),
            mock.patch.object(pam, "reset_password_failures") as reset,
        ):
            self.assertEqual(pam.do_auth(FakePam()), FakePam.PAM_SUCCESS)
        reset.assert_called_once_with("sammy")


if __name__ == "__main__":
    unittest.main()
