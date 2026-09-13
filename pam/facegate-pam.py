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
import sys
import syslog
import time
import uuid

import ConfigParser

# The shared requester module lives in the installed FaceGate package (or the
# checkout this file sits in, during development).
for package_root in ("/usr/local/lib/facegate", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(package_root, "facegate")) and package_root not in sys.path:
        sys.path.insert(0, package_root)
from facegate import requester as requesters  # noqa: E402

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

    service = getattr(pamh, "service", None) or "sudo"
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
