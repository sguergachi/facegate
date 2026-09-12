# FaceGate

FaceGate adds an explicit, Face ID-style consent screen before Howdy uses the
camera for `sudo`. On KDE/Wayland it uses a true layer-shell surface, so the
black island is anchored to the monitor's physical top edge rather than placed
as an ordinary desktop window. The visual HUD runs in your normal desktop
session, while a tiny PAM adapter stays root-owned.

## What it changes

Only the Howdy rule in `/etc/pam.d/sudo` is replaced:

```text
sudo → FaceGate consent HUD → existing Howdy compare.py → sudo or password fallback
```

SDDM is deliberately untouched. If FaceGate is closed, rejects the request,
times out, or face recognition fails, PAM continues to the normal password
prompt. The **Approve** button is not authentication; it solely permits the
camera scan to begin.

## HUD states

- **Approve face verification** — camera remains off until approval.
- **Looking for you** — a scanning arc sweeps the mark while Howdy runs.
- **Verified** — the arc closes into a green ring before sudo continues.
- **Not recognized** — a brief red shake, then the normal password prompt.

The spacing and hierarchy are governed by the measured [layout contract](DESIGN.md),
including the grid, concentric radii, and spring-based motion choreography.

## Install

Requirements: an active KDE/Wayland graphical session, Howdy already working
for `sudo`, `pam-python`, Python 3, GTK 4, and `gtk4-layer-shell`.

On Arch/KDE, the supplied user service preloads the installed layer-shell
library so the GTK introspection binding receives the layer role before GTK
connects to Wayland.

```bash
cd ~/Dev/facegate
sudo ./install.sh
sudo -k
sudo -v
```

The installer creates a timestamped backup in `/etc/facegate/` before it edits
the PAM file. To revert:

```bash
sudo ./uninstall.sh
```

## Development checks

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
python3 -m py_compile facegate/*.py
python2 -m py_compile pam/facegate-pam.py
shellcheck install.sh uninstall.sh
```

## Security model

The Unix socket is mode `0600` in `/run/user/$UID`, and the HUD lives entirely
in the user's desktop session so it can render under Wayland. Its response only
starts Howdy. A successful Howdy comparison remains the sole successful PAM
result; the HUD cannot grant sudo on its own. FaceGate never stores camera
frames, biometric templates, passwords, or sudo credentials.
