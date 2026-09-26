"""The PAM module must read back every password exactly as the island sent it."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
#include "%s"

int main(void)
{
    static char json[8192], out[PAM_MAX_RESP_SIZE];
    size_t length = fread(json, 1, sizeof(json) - 1, stdin);

    json[length] = '\0';
    if (!json_string(json, "password", out, sizeof(out)))
        return 3;
    fputs(out, stdout);
    return 0;
}
"""


@unittest.skipUnless(shutil.which("cc") and Path("/usr/include/security/pam_modules.h").exists(), "needs cc and PAM headers")
class ConsentJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        source = Path(cls.directory.name) / "harness.c"
        cls.binary = Path(cls.directory.name) / "harness"
        source.write_text(HARNESS % (ROOT / "pam/facegate-consent.c"))
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-o", str(cls.binary), str(source), "-lpam"], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def decode(self, reply: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.binary)], input=reply.encode("utf-8"), capture_output=True)

    def test_every_password_round_trips_as_the_island_encodes_it(self):
        for password in ("hunter2", "héllo wörld", "パスワード", "emoji🔒key", 'quote"back\\slash/', "tab\tcr\rff\fbs\b"):
            for ascii_only in (True, False):
                reply = json.dumps({"id": "x", "password": password}, separators=(",", ":"), ensure_ascii=ascii_only) + "\n"
                result = self.decode(reply)
                self.assertEqual(result.returncode, 0, (password, ascii_only))
                self.assertEqual(result.stdout.decode("utf-8"), password)

    def test_malformed_or_missing_passwords_fail(self):
        for reply in ('{"id":"x","password":null}', '{"id":"x","password":"a\\u00"}', '{"id":"x","password":"a\\ud800"}',
                      '{"id":"x","password":"a\\u0000b"}', '{"id":"x","password":"a\\q"}', '{"id":"x","password":"unterminated'):
            self.assertEqual(self.decode(reply).returncode, 3, reply)

    def test_an_overlong_password_fails_instead_of_being_cut(self):
        self.assertEqual(self.decode(json.dumps({"password": "x" * 600})).returncode, 3)


if __name__ == "__main__":
    unittest.main()
