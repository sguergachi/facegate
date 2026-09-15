import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SecurityBoundaryTests(unittest.TestCase):
    def test_installed_pam_block_has_one_gaze_owned_success_path(self):
        installer = (ROOT / "scripts/refresh-pam").read_text()
        match = re.search(
            r"pam_block=\$\(cat <<EOF\n(?P<block>.*?)\nEOF\n\)",
            installer,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        lines = [
            line.split()
            for line in match.group("block").splitlines()
            if line.startswith("auth")
        ]
        self.assertEqual(
            [tokens[-1] for tokens in lines],
            [
                "consent",
                "${gaze_pam}",
                "notify-failure",
                "pam_deny.so",
                "notify-success",
                "pam_permit.so",
            ],
        )
        self.assertIn("[success=ignore", " ".join(lines[0]))
        self.assertIn("[success=2", " ".join(lines[1]))
        self.assertNotIn("sufficient", " ".join(lines[0]))
        self.assertNotIn("sufficient", " ".join(lines[1]))
        self.assertEqual(lines[-1][1], "sufficient")

    def test_refresh_removes_obsolete_modules_in_non_auth_stacks(self):
        installer = (ROOT / "scripts/refresh-pam").read_text()
        self.assertIn(
            r"($0 ~ /\/lib\/security\/howdy\/pam[.]py",
            installer,
        )
        self.assertIn(r"$0 ~ /\/usr\/local\/lib\/security\/facegate\/pam[.]py", installer)
        self.assertIn("/^# Managed by FaceGate:.*Howdy gated[.]$/", installer)

    def test_obsolete_privileged_python_paths_are_deleted(self):
        self.assertFalse((ROOT / "pam/facegate-pam.py").exists())
        self.assertFalse((ROOT / "bin/facegate-faces-list").exists())
        self.assertFalse((ROOT / "packaging/org.facegate.faces.policy").exists())

    def test_gaze_package_upgrades_reapply_the_managed_block(self):
        installer = (ROOT / "install.sh").read_text()
        hook = (ROOT / "packaging/facegate-gaze.hook").read_text()
        self.assertIn("facegate-gaze.hook", installer)
        self.assertIn("Operation = Upgrade", hook)
        self.assertIn("Target = gaze-bin", hook)
        self.assertIn("Exec = /usr/local/lib/facegate/refresh-pam", hook)

    def test_no_face_path_does_not_report_a_false_biometric_failure(self):
        source = (ROOT / "pam/facegate-consent.c").read_text()
        self.assertIn('"facegate.password-only"', source)
        self.assertIn('"\\\"reason\\\":\\\"password-only\\\""', source)

    def test_settings_never_launches_its_client_through_pkexec(self):
        settings = (ROOT / "facegate/settings_app.py").read_text()
        helper = (ROOT / "facegate/faces_helper.py").read_text()
        self.assertNotIn('"pkexec"', settings)
        self.assertIn("must run as your normal user", helper)

    def test_consent_module_never_calls_the_gaze_daemon_itself(self):
        source = (ROOT / "pam/facegate-consent.c").read_text()
        self.assertIn(r'\"username\"', source)
        self.assertNotIn("com.gundulabs.Gaze", source)
        self.assertNotIn("pam_gaze", source)
        self.assertNotIn("pam_permit", source)


if __name__ == "__main__":
    unittest.main()
