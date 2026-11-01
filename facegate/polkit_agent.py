"""FaceGate as the session's polkit authentication agent.

Graphical admin prompts (pkexec, Discover, System Settings, ...) go through
polkit, whose desktop agent normally opens its own password dialog the moment
authentication starts. Registering FaceGate as the agent means the island is
the only prompt: PAM asks for face approval first, and only after Deny, a
timeout, or no face match does the island turn into a password field.

The agent speaks polkit's D-Bus agent interface directly and lets
PolkitAgent.Session run the setuid helper, which performs the PAM conversation.
Nothing here decides authorization: polkitd does, from the helper's result.
"""

from __future__ import annotations

import locale
import os
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Polkit", "1.0")
gi.require_version("PolkitAgent", "1.0")
from gi.repository import Gio, GLib, Polkit, PolkitAgent  # noqa: E402

from . import requester as requesters

OBJECT_PATH = "/org/facegate/PolkitAgent"
AUTHORITY = ("org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority", "org.freedesktop.PolicyKit1.Authority")
EXEC_ACTION = "org.freedesktop.policykit.exec"
# Gaze owns this action and checks it before changing enrolled faces.
MANAGE_FACES_ACTION = "com.gundulabs.gaze.manage-faces"
MAX_PASSWORD_ATTEMPTS = 3
RETRY_REGISTER_SECONDS = 5
LOCKED_PASSWORD = "Password sign-in is temporarily locked. Try again later."
AGENT_XML = """
<node>
  <interface name="org.freedesktop.PolicyKit1.AuthenticationAgent">
    <method name="BeginAuthentication">
      <arg type="s" name="action_id" direction="in"/>
      <arg type="s" name="message" direction="in"/>
      <arg type="s" name="icon_name" direction="in"/>
      <arg type="a{ss}" name="details" direction="in"/>
      <arg type="s" name="cookie" direction="in"/>
      <arg type="a(sa{sv})" name="identities" direction="in"/>
    </method>
    <method name="CancelAuthentication">
      <arg type="s" name="cookie" direction="in"/>
    </method>
  </interface>
</node>
"""


def log(message: str) -> None:
    print("[facegate] polkit: %s" % message, flush=True)


def requester_for(action_id: str, message: str, details: dict[str, str]) -> dict[str, Any]:
    """Who asked, from polkit's own caller and subject pids.

    For pkexec the caller is pkexec itself, so show its command and launcher.
    Otherwise the subject is the app that asked (Discover, System Settings, ...).
    """
    found: dict[str, Any] = {}
    try:
        if action_id == EXEC_ACTION and details.get("polkit.caller-pid"):
            found = requesters.describe_command(int(details["polkit.caller-pid"]))
        elif details.get("polkit.subject-pid"):
            found = requesters.describe_app(int(details["polkit.subject-pid"]))
    except (OSError, ValueError, IndexError):
        found = {}
    found["service"] = "polkit-1"
    found["message"] = message
    return found


def choose_identity(identities: list[tuple[str, dict[str, Any]]]) -> Polkit.Identity | None:
    """Prefer authenticating as the person at the desk."""
    uids = [details.get("uid") for kind, details in identities if kind == "unix-user"]
    if os.getuid() in uids:
        return Polkit.UnixUser.new(os.getuid())
    return Polkit.UnixUser.new(uids[0]) if uids else None


def friendly_pam_message(text: object) -> str:
    """Keep actionable PAM state while hiding low-level helper chatter."""
    clean = " ".join(str(text or "").split())
    return LOCKED_PASSWORD if "locked" in clean.lower() else ""


@dataclass
class Authentication:
    cookie: str
    identity: Polkit.Identity
    invocation: Gio.DBusMethodInvocation
    requester: dict[str, Any]
    action_id: str = ""
    session: Any = None
    password_mode: bool = False
    failed_passwords: int = 0
    pam_message: str = ""
    cancelled: bool = False
    finished: bool = False


class FaceGatePolkitAgent:
    def __init__(self, hud: Any) -> None:
        self.hud = hud
        hud.polkit = self
        self.active: Authentication | None = None
        self.queue: list[Authentication] = []
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.subject: tuple[str, dict[str, GLib.Variant]] | None = None
        self.registered = False
        # Retries registration while KDE's agent still holds the session.
        self.retry: int | None = None
        interface = Gio.DBusNodeInfo.new_for_xml(AGENT_XML).interfaces[0]
        self.registration = self.bus.register_object_with_closures2(OBJECT_PATH, interface, self._on_call, None, None)

    # Registration ---------------------------------------------------------

    def start(self) -> None:
        if not (self.registered or self.retry) and self._register():
            self.retry = GLib.timeout_add_seconds(RETRY_REGISTER_SECONDS, self._retry)

    def _retry(self) -> bool:
        if self._register():
            return True
        self.retry = None
        return False

    def release(self) -> None:
        """Give the session back while FaceGate is off, so KDE's agent can register."""
        if self.retry:
            GLib.source_remove(self.retry)
            self.retry = None
        # Detach the whole queue first. _cancel normally advances to the next
        # request, which is exactly what must not happen while turning off.
        pending = ([self.active] if self.active else []) + list(self.queue)
        self.active = None
        self.queue.clear()
        for auth in pending:
            self._cancel(auth)
        self._unregister()
        log("released the session to the desktop's agent")

    def _session_subject(self) -> tuple[str, dict[str, GLib.Variant]]:
        """The user's graphical login session; this service runs outside it."""
        session_id = os.environ.get("XDG_SESSION_ID", "")
        if not session_id:
            user_path = self.bus.call_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                "GetUser", GLib.Variant("(u)", (os.getuid(),)), GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE, 2_000, None,
            ).unpack()[0]
            display = self.bus.call_sync(
                "org.freedesktop.login1", user_path, "org.freedesktop.DBus.Properties",
                "Get", GLib.Variant("(ss)", ("org.freedesktop.login1.User", "Display")), GLib.VariantType.new("(v)"),
                Gio.DBusCallFlags.NONE, 2_000, None,
            ).unpack()[0]
            session_id = display[0]
        return ("unix-session", {"session-id": GLib.Variant("s", session_id)})

    def _register(self) -> bool:
        """Returns True to keep retrying (for GLib), False once registered."""
        try:
            self.subject = self._session_subject()
            self.bus.call_sync(
                *AUTHORITY, "RegisterAuthenticationAgent",
                GLib.Variant("((sa{sv})ss)", (self.subject, locale.setlocale(locale.LC_MESSAGES) or "C", OBJECT_PATH)),
                None, Gio.DBusCallFlags.NONE, 5_000, None,
            )
        except GLib.Error as error:
            # Usually another agent (KDE's) still holds the session.
            log("not registered yet (%s); retrying" % error.message)
            return True
        self.registered = True
        log("registered as the authentication agent for %s %s" % (self.subject[0], {k: v.unpack() for k, v in self.subject[1].items()}))
        return False

    def _unregister(self) -> None:
        if self.registered and self.subject:
            try:
                self.bus.call_sync(
                    *AUTHORITY, "UnregisterAuthenticationAgent",
                    GLib.Variant("((sa{sv})s)", (self.subject, OBJECT_PATH)),
                    None, Gio.DBusCallFlags.NONE, 2_000, None,
                )
            except GLib.Error:
                pass
        self.registered = False

    def stop(self) -> None:
        self._unregister()
        self.bus.unregister_object(self.registration)

    # D-Bus ------------------------------------------------------------------

    def _from_polkitd(self, sender: str) -> bool:
        """Anyone on the system bus can call this object; only polkitd may."""
        try:
            (owner,) = self.bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "GetNameOwner", GLib.Variant("(s)", (AUTHORITY[0],)), GLib.VariantType.new("(s)"),
                Gio.DBusCallFlags.NONE, 2_000, None,
            ).unpack()
        except GLib.Error:
            return False
        return sender == owner

    def _on_call(self, _connection: Any, sender: str, _path: str, _interface: str, method: str, params: GLib.Variant, invocation: Gio.DBusMethodInvocation) -> None:
        if not self._from_polkitd(sender):
            # Otherwise any local program could put its own words in the island.
            log("refused %s from %s: not polkitd" % (method, sender))
            invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "Only polkit may call the authentication agent")
            return
        if method == "BeginAuthentication":
            action_id, message, _icon, details, cookie, identities = params.unpack()
            identity = choose_identity(identities)
            if identity is None:
                invocation.return_dbus_error("org.freedesktop.PolicyKit1.Error.Failed", "No identity to authenticate as")
                return
            log("begin %s" % action_id)
            self.queue.append(Authentication(cookie, identity, invocation, requester_for(action_id, message, details), action_id))
            if not self.active:
                self._next()
        elif method == "CancelAuthentication":
            (cookie,) = params.unpack()
            for auth in [self.active] + self.queue:
                if auth and auth.cookie == cookie:
                    self._cancel(auth)
            invocation.return_value(None)

    # Flow -------------------------------------------------------------------

    def _next(self) -> None:
        self.active = self.queue.pop(0) if self.queue else None
        if self.active:
            self._begin_session(self.active)

    def _begin_session(self, auth: Authentication) -> None:
        session = PolkitAgent.Session.new(auth.identity, auth.cookie)
        session.connect("request", lambda _s, prompt, echo: self._on_request(auth, prompt, echo))
        session.connect("show-info", lambda _s, text: self._on_pam_message(auth, text, False))
        session.connect("show-error", lambda _s, text: self._on_pam_message(auth, text, True))
        session.connect("completed", lambda _s, gained: self._on_completed(auth, gained))
        auth.session = session
        session.initiate()

    def _on_request(self, auth: Authentication, _prompt: str, _echo_on: bool) -> None:
        """PAM wants a password: the face step was denied, timed out, or failed."""
        auth.password_mode = True
        error = auth.failed_passwords > 0

        def show() -> bool:
            if auth is self.active and not auth.cancelled:
                self.hud.present_password(
                    auth.requester,
                    lambda password: self._respond(auth, password),
                    error,
                    auth.pam_message,
                )
            return False

        # Let a "Not recognized" result finish its moment before the field appears.
        view = self.hud.view
        GLib.timeout_add(900 if (view and view.state == "failure") else 0, show)

    def _on_pam_message(self, auth: Authentication, text: object, error: bool) -> None:
        log("helper %s: %s" % ("error" if error else "info", text))
        message = friendly_pam_message(text)
        if message:
            auth.pam_message = message

    def _respond(self, auth: Authentication, password: str) -> None:
        if auth.session and not auth.finished:
            auth.session.response(password)

    def _on_completed(self, auth: Authentication, gained: bool) -> None:
        auth.session = None
        if auth.cancelled:
            return
        if gained:
            self._finish(auth, None)
            # An approved Add Face turns straight into its scan.
            if auth.action_id == MANAGE_FACES_ACTION and self.hud.enrollment_authorized():
                return
            if self.hud.view and self.hud.view.state == "password":
                self.hud.present_result(True)
            return
        if auth.password_mode:
            if auth.pam_message:
                self._finish(auth, "org.freedesktop.PolicyKit1.Error.Failed")
                if self.hud.view and self.hud.view.state == "password":
                    self.hud.present_result(False, "Password unavailable", auth.pam_message)
                return
            auth.failed_passwords += 1
            if auth.failed_passwords < MAX_PASSWORD_ATTEMPTS:
                # A fresh helper session; PAM will ask again straight away.
                self._begin_session(auth)
                return
        self._finish(auth, "org.freedesktop.PolicyKit1.Error.Failed")
        if self.hud.view and self.hud.view.state == "password":
            self.hud.present_result(False, "Not authorized", "Too many incorrect passwords.")

    def _cancel(self, auth: Authentication) -> None:
        auth.cancelled = True
        if auth.session:
            auth.session.cancel()
            auth.session = None
        self._finish(auth, "org.freedesktop.PolicyKit1.Error.Cancelled")
        if self.hud.pending and self.hud.pending.polkit:
            self.hud.resolve(False)
        self.hud.dismiss()

    def cancel_active(self) -> None:
        if self.active:
            self._cancel(self.active)

    def _finish(self, auth: Authentication, error: str | None) -> None:
        if auth.finished:
            return
        auth.finished = True
        if error:
            auth.invocation.return_dbus_error(error, "Authentication %s" % ("cancelled" if auth.cancelled else "failed"))
        else:
            auth.invocation.return_value(None)
        log("finished %s" % ("ok" if not error else error.rsplit(".", 1)[-1].lower()))
        if auth is self.active:
            self._next()
        elif auth in self.queue:
            self.queue.remove(auth)
