# -*- coding: utf-8 -*-
"""Who is asking for privilege, read from /proc.

Shared by the Python 2 PAM adapter and the Python 3 HUD/polkit agent, so it
stays compatible with both. Everything here is for display only: a process can
rename itself, so it must never influence an authorization decision.
"""

import os

# Processes that only relay a command; the requester is whoever launched them.
RELAYS = frozenset([
    "sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "nu", "xonsh",
    "sudo", "su", "doas", "pkexec", "env", "timeout", "nohup", "script", "xargs", "setsid",
    "time", "watch", "make", "just", "tmux", "tmux: server", "screen",
])
# Friendly names for apps that commonly ask for, or host things that ask for, privilege.
APP_NAMES = {
    "konsole": "Konsole", "yakuake": "Yakuake", "kitty": "kitty", "alacritty": "Alacritty",
    "foot": "foot", "wezterm-gui": "WezTerm", "ghostty": "Ghostty", "gnome-terminal-": "GNOME Terminal",
    "gnome-terminal-server": "GNOME Terminal", "xterm": "XTerm", "tilix": "Tilix", "terminator": "Terminator",
    "code": "VS Code", "codium": "VSCodium", "cursor": "Cursor", "zed": "Zed", "zeditor": "Zed",
    "t3code": "T3 Code", "kate": "Kate", "dolphin": "Dolphin", "idea": "IntelliJ IDEA",
    "claude": "Claude Code", "codex": "Codex", "aider": "Aider", "sshd": "SSH",
    "plasma-discover": "Discover", "systemsettings": "System Settings", "partitionmanager": "Partition Manager",
    "pamac-manager": "Pamac", "octopi": "Octopi", "gparted": "GParted",
    # The kernel truncates comm to 15 characters.
    "facegate-settin": "FaceGate Settings", "facegate-settings": "FaceGate Settings",
}
STOP_AT = frozenset(["systemd", "init", "kwin_wayland", "plasmashell", "sddm", "login", "gdm", "startplasma-wayland"])
MAX_COMMAND_CHARS = 160
ELLIPSIS = u"…"
TEXT = type(u"")


def read_stat(pid):
    with open("/proc/%d/stat" % pid) as stat:
        raw = stat.read()
    # comm may contain spaces or parentheses; the other fields follow the last ')'.
    return raw[raw.index("(") + 1:raw.rindex(")")], raw[raw.rindex(")") + 2:].split()


def read_process(pid):
    comm, fields = read_stat(pid)
    with open("/proc/%d/cmdline" % pid) as cmdline:
        argv = [arg for arg in cmdline.read().split("\0") if arg]
    return comm, int(fields[1]), argv


def real_uid(pid):
    with open("/proc/%d/status" % pid) as status:
        for line in status:
            if line.startswith("Uid:"):
                return int(line.split()[1])
    return -1


def newest_process(name, uid):
    """The most recently started process called name run by uid, if any.

    Matches the real uid: setuid programs such as pkexec run as root.
    """
    newest, newest_start = None, -1
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            comm, fields = read_stat(int(entry))
            if comm != name or real_uid(int(entry)) != uid:
                continue
            start = int(fields[19])
        except (IOError, OSError, ValueError, IndexError):
            continue
        if start > newest_start:
            newest, newest_start = int(entry), start
    return newest


def quote(arg):
    safe = "@%+=:,./-_"
    if arg and all(char.isalnum() or char in safe for char in arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def command_line(argv):
    """argv as one printable, shell-quoted, bounded line."""
    program = os.path.basename(argv[0]) if argv else ""
    command = " ".join([program] + [quote(arg) for arg in argv[1:]])
    if not isinstance(command, TEXT):
        command = command.decode("utf-8", "replace")
    command = u"".join(char if char >= u" " else u" " for char in command)
    if len(command) > MAX_COMMAND_CHARS:
        command = command[:MAX_COMMAND_CHARS - 1] + ELLIPSIS
    return command


def _name(comm, argv):
    for candidate in [os.path.basename(argv[0]) if argv else "", comm]:
        if candidate in APP_NAMES:
            return APP_NAMES[candidate], True
    return comm, False


def apps_from(pid, requester):
    """Walk up from pid, skipping relays, to fill in "app" and "host"."""
    depth = 0
    while pid > 1 and depth < 32:
        depth += 1
        try:
            comm, parent, argv = read_process(pid)
        except (IOError, OSError, ValueError, IndexError):
            break
        if comm in STOP_AT:
            break
        executable = os.path.basename(argv[0]) if argv else ""
        if comm not in RELAYS and executable not in RELAYS:
            name, known = _name(comm, argv)
            if "app" not in requester:
                requester["app"] = name
            elif known and name != requester["app"]:
                requester["host"] = name
                break
        pid = parent
    return requester


def describe_command(pid):
    """A process that runs a command for someone (sudo, pkexec): its command,
    and the app that launched it."""
    _comm, parent, argv = read_process(pid)
    return apps_from(parent, {"command": command_line(argv)})


def describe_app(pid):
    """A process that asked for privilege itself (a polkit D-Bus client)."""
    return apps_from(pid, {})
