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
    PAM_IGNORE = 25
    service = "sudo"

    def get_user(self):
        return "sammy"


class TurnedOffTests(unittest.TestCase):
    def test_only_an_explicit_off_in_the_users_own_file_counts(self):
        import getpass
        import os

        user = getpass.getuser()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertFalse(pam.turned_off(user, str(path)))
            path.write_text(json.dumps({"enabled": True}))
            self.assertFalse(pam.turned_off(user, str(path)))
            path.write_text("not json")
            self.assertFalse(pam.turned_off(user, str(path)))
            path.write_text(json.dumps({"enabled": False}))
            self.assertTrue(pam.turned_off(user, str(path)))
            # A link is never followed, even to a file that says off.
            link = Path(directory) / "link.json"
            os.symlink(path, link)
            self.assertFalse(pam.turned_off(user, str(link)))

    def test_off_skips_the_island_entirely(self):
        with mock.patch.object(pam, "turned_off", return_value=True), mock.patch.object(pam, "request_hud") as hud:
            self.assertEqual(pam.do_auth(FakePam()), FakePam.PAM_AUTHINFO_UNAVAIL)
        hud.assert_not_called()


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

    def test_polkit_without_a_face_leaves_the_password_to_the_agent(self):
        pamh = FakePam()
        pamh.service = "polkit-1"
        with (
            mock.patch.object(pam, "should_skip", return_value=False),
            mock.patch.object(pam, "has_face_models", return_value=False),
            mock.patch.object(pam, "request_hud") as request_hud,
        ):
            self.assertEqual(pam.do_auth(pamh), FakePam.PAM_AUTHINFO_UNAVAIL)
        request_hud.assert_not_called()

    def test_sudo_without_a_face_takes_the_password_from_the_island_for_pam_unix(self):
        pam.island_password.update(asked=0, pending=False)
        pamh = FakePam()
        with (
            mock.patch.object(pam, "should_skip", return_value=False),
            mock.patch.object(pam, "has_face_models", return_value=False),
            mock.patch.object(pam.requesters, "read_process", return_value=("sudo", 1, ["sudo", "pacman", "-Syu"])),
            mock.patch.object(pam.requesters, "describe_command", return_value={"command": "sudo pacman -Syu"}),
            mock.patch.object(pam, "locked_until", return_value=0),
            mock.patch.object(pam, "request_hud", return_value={"password": "hunter2"}) as request_hud,
            mock.patch.object(pam, "emit_hud") as emit_hud,
        ):
            # Never sufficient by itself: pam_unix still has to accept it.
            self.assertEqual(pam.do_auth(pamh), FakePam.PAM_AUTHINFO_UNAVAIL)
            self.assertEqual(pamh.authtok, "hunter2")
            self.assertEqual(request_hud.call_args[0][1], "password")
            self.assertFalse(request_hud.call_args[0][2]["error"])
            # A retry after a rejected password says so.
            pam.do_auth(pamh)
            self.assertTrue(request_hud.call_args[0][2]["error"])
            self.assertEqual(pam.pam_sm_acct_mgmt(pamh, 0, []), FakePam.PAM_IGNORE)
            pam.pam_sm_acct_mgmt(pamh, 0, [])
        emit_hud.assert_called_once_with("sammy", "result", {"result": "success"})

    def test_dismissed_island_leaves_sudo_at_its_terminal_prompt(self):
        pam.island_password.update(asked=0, pending=False)
        pamh = FakePam()
        with (
            mock.patch.object(pam, "should_skip", return_value=False),
            mock.patch.object(pam, "has_face_models", return_value=False),
            mock.patch.object(pam.requesters, "read_process", return_value=("sudo", 1, ["sudo", "true"])),
            mock.patch.object(pam.requesters, "describe_command", return_value={}),
            mock.patch.object(pam, "locked_until", return_value=0),
            mock.patch.object(pam, "request_hud", return_value={"password": None}),
        ):
            self.assertEqual(pam.do_auth(pamh), FakePam.PAM_AUTHINFO_UNAVAIL)
        self.assertFalse(hasattr(pamh, "authtok"))

    def test_faillock_lock_is_detected_so_the_island_can_say_when_to_retry(self):
        import time as clock

        base = clock.mktime(clock.strptime("2026-09-13 22:23:15", "%Y-%m-%d %H:%M:%S"))
        output = """sammy:
When                Type  Source                                           Valid
2026-09-13 22:23:15 SVC   sudo                                                 V
2026-09-13 22:23:19 SVC   sudo                                                 V
2026-09-13 22:23:22 SVC   sudo                                                 V
"""
        settings = {"deny": 3, "fail_interval": 900, "unlock_time": 600}
        self.assertEqual(pam.locked_until("sammy", base + 60, output, settings), base + 7 + 600)
        self.assertEqual(pam.locked_until("sammy", base + 7 + 601, output, settings), 0)
        two = "\n".join(output.splitlines()[:-1])
        self.assertEqual(pam.locked_until("sammy", base + 60, two, settings), 0)
        self.assertEqual(pam.locked_until("sammy", base + 60, output, dict(settings, unlock_time=0)), -1)

    def test_locked_sudo_tells_the_island_when_it_unlocks(self):
        pam.island_password.update(asked=0, pending=False)
        with (
            mock.patch.object(pam, "locked_until", return_value=4102444800.0),
            mock.patch.object(pam, "request_hud", return_value={"password": None}) as request_hud,
        ):
            pam.ask_password_in_island(FakePam(), "sammy", {})
        self.assertEqual(request_hud.call_args[0][2]["locked_until"], 4102444800.0)

    def test_stdin_and_non_interactive_sudo_never_open_the_island(self):
        for argv in (["sudo", "-S", "true"], ["sudo", "-kS", "true"], ["sudo", "--stdin", "true"], ["sudo", "-n", "true"]):
            self.assertTrue(pam.password_comes_from_elsewhere(argv), argv)
        # A graphical askpass (sudo -A, as paru uses) is replaced by the island.
        for argv in (["sudo", "-A", "pacman", "-Sy"], ["sudo", "--askpass", "true"], ["sudo", "true"], ["sudo", "-u", "nobody", "true"], ["sudo", "-uS", "true"], ["sudo", "pacman", "-S", "vim"], ["sudo", "--", "-S"]):
            self.assertFalse(pam.password_comes_from_elsewhere(argv), argv)

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
