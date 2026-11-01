"""The PAM module's own logic: password decoding and the faillock lockout."""

import json
import struct
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
#include "MODULE"

int main(int argc, char **argv)
{
    static char json[8192], out[PAM_MAX_RESP_SIZE];
    size_t length;

    if (argc == 6) {
        /* tally path, now, deny, fail_interval, unlock_time */
        struct faillock_policy policy = {atoll(argv[3]), atoll(argv[4]), atoll(argv[5]), ""};
        printf("%lld", tally_locked_until(&policy, argv[1], atoll(argv[2])));
        return 0;
    }
    length = fread(json, 1, sizeof(json) - 1, stdin);

    json[length] = '\0';
    if (!json_string(json, "password", out, sizeof(out)))
        return 3;
    fputs(out, stdout);
    return 0;
}
"""


@unittest.skipUnless(shutil.which("cc") and Path("/usr/include/security/pam_modules.h").exists(), "needs cc and PAM headers")
class ConsentNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        source = Path(cls.directory.name) / "harness.c"
        cls.binary = Path(cls.directory.name) / "harness"
        source.write_text(HARNESS.replace("MODULE", str(ROOT / "pam/facegate-consent.c")))
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

    def locked_until(self, failures, now=10_000, deny=3, interval=900, unlock=600) -> int:
        tally = Path(self.directory.name) / "tally"
        # pam_faillock's record: source[52], reserved, status, time.
        tally.write_bytes(b"".join(struct.pack("=52sHHQ", b"/dev/pts/1", 0, status, when) for when, status in failures))
        result = subprocess.run([str(self.binary), str(tally), str(now), str(deny), str(interval), str(unlock)], capture_output=True, check=True)
        return int(result.stdout)

    def test_faillock_lock_matches_pam_faillock(self):
        valid = 0x1
        self.assertEqual(self.locked_until([(9_900, valid), (9_950, valid)]), 0)
        # Three recent failures lock until the latest plus unlock_time.
        self.assertEqual(self.locked_until([(9_900, valid), (9_950, valid), (9_990, valid)]), 10_590)
        # Old, invalid, or expired failures don't count.
        self.assertEqual(self.locked_until([(8_000, valid), (9_950, valid), (9_990, valid)]), 0)
        self.assertEqual(self.locked_until([(9_900, 0), (9_950, valid), (9_990, valid)]), 0)
        self.assertEqual(self.locked_until([(9_000, valid), (9_100, valid), (9_200, valid)]), 0)
        # unlock_time = never: only an administrator can unlock.
        self.assertEqual(self.locked_until([(9_900, valid), (9_950, valid), (9_990, valid)], unlock=0), -1)
        self.assertEqual(self.locked_until([(9_900, valid)] * 5, deny=0), 0)


if __name__ == "__main__":
    unittest.main()
