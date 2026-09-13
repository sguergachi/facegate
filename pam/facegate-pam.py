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
import subprocess
import syslog
import time
import uuid

import ConfigParser

HOWDY_DIR = "/usr/lib/security/howdy"
HOWDY_COMPARE = HOWDY_DIR + "/compare.py"
HOWDY_CONFIG = HOWDY_DIR + "/config.ini"
SOCKET_BASENAME = "facegate.sock"
CONSENT_TIMEOUT_SECONDS = 12


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


# Processes that only relay a command; the requester is whoever launched them.
RELAYS = frozenset([
    "sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "nu", "xonsh",
    "sudo", "su", "doas", "env", "timeout", "nohup", "script", "xargs", "setsid",
    "time", "watch", "make", "just", "tmux", "tmux: server", "screen",
])
# Apps that host other programs: shown as "via ...".
APP_NAMES = {
    "konsole": "Konsole", "yakuake": "Yakuake", "kitty": "kitty", "alacritty": "Alacritty",
    "foot": "foot", "wezterm-gui": "WezTerm", "ghostty": "Ghostty", "gnome-terminal-": "GNOME Terminal",
    "gnome-terminal-server": "GNOME Terminal", "xterm": "XTerm", "tilix": "Tilix", "terminator": "Terminator",
    "code": "VS Code", "codium": "VSCodium", "cursor": "Cursor", "zed": "Zed", "zeditor": "Zed",
    "t3code": "T3 Code", "kate": "Kate", "dolphin": "Dolphin", "idea": "IntelliJ IDEA",
    "claude": "Claude Code", "codex": "Codex", "aider": "Aider", "sshd": "SSH",
}
STOP_AT = frozenset(["systemd", "init", "kwin_wayland", "plasmashell", "sddm", "login", "gdm", "startplasma-wayland"])
MAX_COMMAND_CHARS = 160


def read_process(pid):
    with open("/proc/%d/stat" % pid) as stat:
        raw = stat.read()
    # comm may contain spaces or parentheses; the parent pid follows the last ')'.
    comm = raw[raw.index("(") + 1:raw.rindex(")")]
    parent = int(raw[raw.rindex(")") + 2:].split()[1])
    with open("/proc/%d/cmdline" % pid) as cmdline:
        argv = [arg for arg in cmdline.read().split("\0") if arg]
    return comm, parent, argv


def process_name(comm, argv):
    for candidate in [os.path.basename(argv[0]) if argv else "", comm]:
        if candidate in APP_NAMES:
            return APP_NAMES[candidate]
    return comm


def quote(arg):
    safe = "@%+=:,./-_"
    if arg and all(char.isalnum() or char in safe for char in arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def describe_requester(sudo_pid):
    """Name the app that ran sudo and the exact command, for display only.

    A process can rename itself, so this is informative, never an
    authorization input.
    """
    _comm, parent, argv = read_process(sudo_pid)
    command = " ".join(["sudo"] + [quote(arg) for arg in argv[1:]])
    command = "".join(char if char >= " " else " " for char in command)
    if len(command) > MAX_COMMAND_CHARS:
        command = command[:MAX_COMMAND_CHARS - 1] + u"…".encode("utf-8")
    requester = {"command": command}
    pid, depth = parent, 0
    while pid > 1 and depth < 32:
        depth += 1
        try:
            comm, next_pid, argv = read_process(pid)
        except (IOError, OSError, ValueError):
            break
        if comm in STOP_AT:
            break
        executable = os.path.basename(argv[0]) if argv else ""
        if comm not in RELAYS and executable not in RELAYS:
            name = process_name(comm, argv)
            if "app" not in requester:
                requester["app"] = name
            elif name != requester["app"] and (comm in APP_NAMES or executable in APP_NAMES):
                requester["host"] = name
                break
        pid = next_pid
    return requester


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


def do_auth(pamh):
    try:
        username = pamh.get_user()
    except Exception:
        return pamh.PAM_AUTHINFO_UNAVAIL
    if not username:
        return pamh.PAM_AUTHINFO_UNAVAIL

    config = ConfigParser.ConfigParser()
    config.read(HOWDY_CONFIG)
    try:
        if should_skip(config):
            return pamh.PAM_AUTHINFO_UNAVAIL
    except Exception as error:
        log("Unable to read Howdy configuration: %s" % error)
        return pamh.PAM_AUTHINFO_UNAVAIL

    try:
        requester = describe_requester(os.getpid())
    except Exception as error:
        log("Unable to describe the sudo requester: %s" % error)
        requester = {}
    consent = request_hud(
        username,
        "approval",
        {"service": "sudo", "timeout_ms": CONSENT_TIMEOUT_SECONDS * 1000, "requester": requester},
        timeout=CONSENT_TIMEOUT_SECONDS + 2,
    )
    if not consent or not consent.get("approved"):
        log("Face scan not approved for %s; password fallback" % username)
        return pamh.PAM_AUTHINFO_UNAVAIL

    log("Face scan approved for %s" % username)
    emit_hud(username, "scanning")
    status = subprocess.call(["/usr/bin/python3", HOWDY_COMPARE, username])
    if status == 0:
        emit_hud(username, "result", {"result": "success"})
        log("Face verification approved for %s" % username)
        return pamh.PAM_SUCCESS

    emit_hud(username, "result", {"result": "failure", "status": status})
    log("Face verification failed for %s (Howdy status %d)" % (username, status))
    return pamh.PAM_AUTH_ERR


def pam_sm_authenticate(pamh, flags, args):
    return do_auth(pamh)


def pam_sm_open_session(pamh, flags, args):
    return pamh.PAM_SUCCESS


def pam_sm_close_session(pamh, flags, args):
    return pamh.PAM_SUCCESS


def pam_sm_setcred(pamh, flags, args):
    return pamh.PAM_SUCCESS
