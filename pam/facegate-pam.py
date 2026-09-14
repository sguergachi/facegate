#!/usr/bin/python2
# -*- coding: utf-8 -*-
"""PAM adapter for FaceGate.  Kept Python-2-compatible for pam-python.

This module never treats the HUD response as authentication.  It only obtains
consent to start the camera, then invokes Howdy's existing compare.py process.
Any HUD error, rejection, timeout, or failed face match falls through to the
normal password stack via PAM_AUTH_ERR / PAM_AUTHINFO_UNAVAIL.
"""

import glob
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import syslog
import time
import uuid

try:
    import ConfigParser
except ImportError:  # Python 3, used by the unit tests.
    import configparser as ConfigParser

# The shared requester module lives in the installed FaceGate package (or the
# checkout this file sits in, during development).
for package_root in ("/usr/local/lib/facegate", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(package_root, "facegate")) and package_root not in sys.path:
        sys.path.insert(0, package_root)
from facegate import requester as requesters  # noqa: E402

HOWDY_DIR = "/usr/lib/security/howdy"
HOWDY_COMPARE = HOWDY_DIR + "/compare.py"
HOWDY_CONFIG = HOWDY_DIR + "/config.ini"
FAILLOCK = "/usr/bin/faillock"
FAILLOCK_CONF = "/etc/security/faillock.conf"
SOCKET_BASENAME = "facegate.sock"
# Written by FaceGate's main screen; "enabled": false turns FaceGate off.
SETTINGS_PATH = ".config/facegate/settings.json"
MAX_SETTINGS_BYTES = 64 * 1024
CONSENT_TIMEOUT_SECONDS = 12
# Typing a password takes longer than a glance; sudo's own limit is 5 minutes.
PASSWORD_TIMEOUT_SECONDS = 120
# sudo options that take a value, so the word after them is not the command.
SUDO_VALUE_OPTIONS = frozenset("CDghpRrTtUu")

# Set once the island supplied this sudo's password, so setcred (which sudo
# only reaches after the whole stack accepted it) can report success.
island_password = {"asked": 0, "pending": False}


def log(message):
    syslog.openlog("[FACEGATE]", 0, syslog.LOG_AUTH)
    syslog.syslog(syslog.LOG_INFO, message)
    syslog.closelog()


def request_hud(username, phase, payload=None, timeout=2):
    """Talk to the user's 0600 runtime socket.  Returns None on any failure."""
    try:
        user_id = pwd.getpwnam(username).pw_uid
        path = "/run/user/%d/%s" % (user_id, SOCKET_BASENAME)
        message = {"id": str(uuid.uuid4()), "phase": phase}
        if payload:
            message.update(payload)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(path)
        client.sendall(json.dumps(message, separators=(",", ":")) + "\n")
        response = client.recv(4096)
        client.close()
        return json.loads(response.split("\n", 1)[0])
    except Exception as error:
        log("HUD %s unavailable for %s: %s" % (phase, username, error))
        return None


def emit_hud(username, phase, payload=None):
    # State rendering is best effort; it must never decide authentication.
    request_hud(username, phase, payload, timeout=1)


def should_skip(config):
    if config.getboolean("core", "disabled"):
        return True
    if config.getboolean("core", "ignore_ssh"):
        if "SSH_CONNECTION" in os.environ or "SSH_CLIENT" in os.environ or "SSHD_OPTS" in os.environ:
            return True
    if config.getboolean("core", "ignore_closed_lid"):
        if any("closed" in open(path).read() for path in glob.glob("/proc/acpi/button/lid/*/state")):
            return True
    return False


def turned_off(username, path=None):
    """Whether the user switched FaceGate off on its main screen.

    Read here rather than asked of the island, so off never depends on the
    island running: sudo simply gets its usual password prompt. The file is
    the user's own and can only take FaceGate out of the way, never grant
    anything. Still, only a regular file they own is read, without following
    links or blocking.
    """
    try:
        user = pwd.getpwnam(username)
        descriptor = os.open(path or os.path.join(user.pw_dir, SETTINGS_PATH), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != user.pw_uid:
                return False
            settings = json.loads(os.read(descriptor, MAX_SETTINGS_BYTES))
        finally:
            os.close(descriptor)
    except Exception:
        return False
    return isinstance(settings, dict) and settings.get("enabled") is False


def has_face_models(username, howdy_dir=HOWDY_DIR):
    """Return whether Howdy has a usable face model for this user.

    The PAM module runs as root, so it can read Howdy's root-only model store.
    Missing, empty, or malformed files safely fall through to the password
    stack instead of presenting an approval that cannot succeed.
    """
    path = os.path.join(howdy_dir, "models", username + ".dat")
    try:
        with open(path) as model_file:
            models = json.load(model_file)
    except (IOError, OSError, ValueError, TypeError):
        return False
    return isinstance(models, list) and any(
        isinstance(model, dict) and isinstance(model.get("data"), list) and model.get("data")
        for model in models
    )


def password_comes_from_elsewhere(argv):
    """sudo -S reads the password from stdin, and -n must never ask.

    -A (a graphical askpass helper, as Arch Update and paru use) still gets
    the island: it replaces that dialog, and Cancel falls back to it.
    """
    for arg in argv[1:]:
        if arg == "--" or not arg.startswith("-") or arg == "-":
            return False
        if arg.startswith("--"):
            name = arg[2:].split("=", 1)[0]
            if name in ("stdin", "non-interactive"):
                return True
            continue
        for index, flag in enumerate(arg[1:]):
            if flag in "Sn":
                return True
            if flag in SUDO_VALUE_OPTIONS:
                # The rest of this word (or the next word) is the value.
                break
    return False


def ask_password_in_island(pamh, username, requester):
    """Collect the password in the island and leave it for pam_unix to check.

    FaceGate never verifies it: the password becomes PAM_AUTHTOK and the
    normal stack (pam_unix try_first_pass, faillock) decides. Returns False
    when the island is unavailable or dismissed, so sudo asks in the terminal.
    """
    until = locked_until(username)
    payload = {"service": "sudo", "requester": requester, "error": island_password["asked"] > 0}
    if until:
        # The island counts down, then offers the field once sudo can accept it.
        payload["locked_until"] = until
    wait = PASSWORD_TIMEOUT_SECONDS + (max(0, until - time.time()) if until > 0 else 0)
    reply = request_hud(username, "password", payload, timeout=wait)
    password = reply.get("password") if isinstance(reply, dict) else None
    if not password or not isinstance(password, (type(u""), str)):
        return False
    if not isinstance(password, str):
        # Python 2: PAM wants bytes.
        password = password.encode("utf-8")
    pamh.authtok = password
    island_password["asked"] += 1
    island_password["pending"] = True
    return True


def faillock_settings(path=FAILLOCK_CONF):
    """deny, fail_interval and unlock_time, with pam_faillock's defaults."""
    settings = {"deny": 3, "fail_interval": 900, "unlock_time": 600}
    try:
        with open(path) as conf:
            for line in conf:
                key, _, value = line.split("#", 1)[0].partition("=")
                key, value = key.strip(), value.strip()
                if key in settings:
                    settings[key] = 0 if value == "never" else int(value)
    except (IOError, OSError, ValueError):
        pass
    return settings


def locked_until(username, now=None, output=None, settings=None):
    """When pam_faillock will accept a password again, or 0 if it does now.

    While locked, pam_faillock refuses every password, even the right one, so
    asking for one would only look like "Incorrect password".
    """
    settings = settings or faillock_settings()
    now = time.time() if now is None else now
    if output is None:
        if not os.path.exists(FAILLOCK):
            return 0
        try:
            output = subprocess.Popen([FAILLOCK, "--user", username], stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()[0]
        except (IOError, OSError):
            return 0
    if not isinstance(output, str):
        output = output.decode("utf-8", "replace")
    failures = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[-1] == "V":
            try:
                failures.append(time.mktime(time.strptime(fields[0] + " " + fields[1], "%Y-%m-%d %H:%M:%S")))
            except ValueError:
                continue
    recent = [when for when in failures if now - when <= settings["fail_interval"]]
    if not settings["deny"] or len(recent) < settings["deny"]:
        return 0
    if settings["unlock_time"] == 0:
        # Locked until an administrator resets it; nothing to count down to.
        return -1
    until = max(recent) + settings["unlock_time"]
    return until if until > now else 0


def reset_password_failures(username):
    """End a password failure streak after successful face authentication.

    FaceGate's PAM rule is sufficient, so a match returns before the later
    pam_faillock ``authsucc`` rule in system-auth can clear old failures.
    Mirror that bookkeeping when faillock is installed.
    """
    if not os.path.exists(FAILLOCK):
        return
    try:
        with open(os.devnull, "w") as discard:
            subprocess.call([FAILLOCK, "--user", username, "--reset"], stdout=discard, stderr=discard)
    except (IOError, OSError):
        pass


def do_auth(pamh):
    try:
        username = pamh.get_user()
    except Exception:
        return pamh.PAM_AUTHINFO_UNAVAIL
    if not username:
        return pamh.PAM_AUTHINFO_UNAVAIL
    if turned_off(username):
        log("FaceGate is off for %s; usual password prompt" % username)
        return pamh.PAM_AUTHINFO_UNAVAIL

    config = ConfigParser.ConfigParser()
    config.read(HOWDY_CONFIG)
    try:
        if should_skip(config):
            return pamh.PAM_AUTHINFO_UNAVAIL
    except Exception as error:
        log("Unable to read Howdy configuration: %s" % error)
        return pamh.PAM_AUTHINFO_UNAVAIL

    service = getattr(pamh, "service", None) or "sudo"
    if not has_face_models(username):
        # polkit's own agent (FaceGate's island) asks for the password itself.
        if service == "polkit-1":
            log("No face models for %s; password fallback" % username)
            return pamh.PAM_AUTHINFO_UNAVAIL
        return password_fallback(pamh, username)

    try:
        if service == "polkit-1":
            # PAM runs in polkit's agent helper, not in the requester. FaceGate's
            # own polkit agent knows the real caller; this is a fallback guess.
            pkexec = requesters.newest_process("pkexec", pwd.getpwnam(username).pw_uid)
            requester = requesters.describe_command(pkexec) if pkexec else {}
        else:
            requester = requesters.describe_command(os.getpid())
    except Exception as error:
        log("Unable to describe the %s requester: %s" % (service, error))
        requester = {}
    requester["service"] = service
    consent = request_hud(
        username,
        "approval",
        {"service": service, "timeout_ms": CONSENT_TIMEOUT_SECONDS * 1000, "requester": requester},
        timeout=CONSENT_TIMEOUT_SECONDS + 2,
    )
    if not consent or not consent.get("approved"):
        log("Face scan not approved for %s; password fallback" % username)
        return pamh.PAM_AUTHINFO_UNAVAIL

    log("Face scan approved for %s" % username)
    emit_hud(username, "scanning")
    status = subprocess.call(["/usr/bin/python3", HOWDY_COMPARE, username])
    if status == 0:
        reset_password_failures(username)
        emit_hud(username, "result", {"result": "success"})
        log("Face verification approved for %s" % username)
        return pamh.PAM_SUCCESS

    emit_hud(username, "result", {"result": "failure", "status": status})
    log("Face verification failed for %s (Howdy status %d)" % (username, status))
    return pamh.PAM_AUTH_ERR


def password_fallback(pamh, username):
    """No face to try: ask for sudo's password in the island when possible."""
    try:
        argv = requesters.read_process(os.getpid())[2]
    except Exception:
        argv = []
    if password_comes_from_elsewhere(argv):
        return pamh.PAM_AUTHINFO_UNAVAIL
    try:
        requester = requesters.describe_command(os.getpid())
    except Exception as error:
        log("Unable to describe the sudo requester: %s" % error)
        requester = {}
    requester["service"] = "sudo"
    if ask_password_in_island(pamh, username, requester):
        log("Password for %s collected in the island; checked by the PAM stack" % username)
    else:
        log("No face models for %s; terminal password fallback" % username)
    # Not sufficient on its own: pam_unix checks the password next.
    return pamh.PAM_AUTHINFO_UNAVAIL


def pam_sm_authenticate(pamh, flags, args):
    return do_auth(pamh)


def pam_sm_open_session(pamh, flags, args):
    return pamh.PAM_SUCCESS


def pam_sm_close_session(pamh, flags, args):
    return pamh.PAM_SUCCESS


def pam_sm_acct_mgmt(pamh, flags, args):
    """sudo checks the account only after the stack accepted a password.

    Installed as an optional account rule, so it never affects the decision.
    """
    if island_password["pending"]:
        island_password["pending"] = False
        try:
            emit_hud(pamh.get_user(), "result", {"result": "success"})
        except Exception:
            pass
    return pamh.PAM_IGNORE


def pam_sm_setcred(pamh, flags, args):
    return pamh.PAM_SUCCESS
