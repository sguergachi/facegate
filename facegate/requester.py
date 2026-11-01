"""Who is asking for privilege, read from /proc.

Used by the HUD (for sudo) and the polkit agent. Everything here is for
display only: a process can rename itself, so it must never influence an
authorization decision.
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
    "facegate-settin": "FaceGate", "facegate-settings": "FaceGate",
}
STOP_AT = frozenset(["systemd", "init", "kwin_wayland", "plasmashell", "sddm", "login", "gdm", "startplasma-wayland"])
MAX_COMMAND_CHARS = 160
ELLIPSIS = "…"


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


def quote(arg):
    safe = "@%+=:,./-_"
    if arg and all(char.isalnum() or char in safe for char in arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def command_line(argv):
    """argv as one printable, shell-quoted, bounded line."""
    program = os.path.basename(argv[0]) if argv else ""
    command = " ".join([program] + [quote(arg) for arg in argv[1:]])
    command = "".join(char if char >= " " else " " for char in command)
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
        except (OSError, ValueError, IndexError):
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
