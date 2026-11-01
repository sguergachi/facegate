# FaceGate

<p align="center">
  <img src="media/facegate-demo.gif" alt="FaceGate island on a KDE desktop: sudo approved by face, then a second attempt where the face isn't recognized and the password is used instead" width="960">
</p>

FaceGate is a Face ID-style consent interface for
[Gaze](https://github.com/GunduLabs/gaze). Gaze is the security backend: its
root-owned daemon controls the camera and templates, performs liveness and face
matching, and its official PAM module returns the biometric verdict. FaceGate
adds the explicit **Approve** step and presents the existing scan, result, and
password-fallback experience.

The island is a layer-shell surface anchored to the monitor's physical top edge.
Its UI runs in the logged-in desktop session; no Python code participates in the
biometric decision or reads Gaze's templates.

> **Arch-based systems with Wayland only.** FaceGate replaces your desktop's
> polkit agent and asks the compositor which monitor is active. KDE Plasma is
> strongly recommended (layer-shell integration, active output tracking, and the
> password-in-island experience); other Wayland desktops work but fall back to
> the compositor's output choice. GNOME, Sway, X11, and other desktops are not
> currently supported.

## Security boundary

```text
sudo / polkit
      │
      ▼
root-owned native FaceGate consent module ── denied/unavailable ──► password
      │ approved (permission to start the camera, not authentication)
      ▼
official pam_gaze.so ─► root-owned gazed ─► liveness + match verdict
      │                                      │
      ├─ failure/unavailable ────────────────► password
      └─ success ────────────────────────────► authenticated
```

The installer puts a small compiled PAM module before Gaze. It can only choose
between skipping the scan and calling Gaze; a FaceGate response is never enough
to authenticate. The success branch is reachable only after the official
`pam_gaze.so` returns success. Missing modules, a missing HUD, malformed replies,
timeouts, denial, daemon failure, and failed recognition all fall back to the
original password stack.

The PAM modules and installed application code are root-owned. FaceGate Settings
is an unprivileged client of Gaze's system D-Bus API. Enrollment and deletion are
authorized independently by Gaze/polkit, and FaceGate neither opens the camera
nor reads or writes biometric templates. It stores only optional display
timestamps in `~/.local/share/facegate/faces.json`.

For graphical admin prompts, FaceGate remains the session's polkit agent so the
password fallback stays inside the island. The password is passed to polkit's
authentication session for validation; FaceGate does not decide whether it is
correct. This does mean the UI process necessarily receives the typed password.
Users who require the UI process never to see passwords should keep Plasma's
stock polkit agent instead, at the cost of changing that fallback UX.

The consent socket is `0600` in `/run/user/$UID`. A malicious process already
running as that same user could imitate an approval, but that can only start a
Gaze attempt—it cannot create a successful biometric verdict. Protecting the
entire graphical session from same-user malware is outside FaceGate's threat
model.

The detailed component boundary, failure invariants, and known limitations are
documented in [SECURITY.md](SECURITY.md).

SDDM and the lock screen are deliberately untouched. FaceGate gates only `sudo`
and polkit elevation; Gaze integrations for other surfaces remain separate.

## Experience

- **‹App› wants sudo** names the requesting app and command. The camera stays off
  until you approve.
- **Looking for you** animates while Gaze performs liveness and recognition.
- **Verified** closes the arc into a green ring before authentication continues.
- **Not recognized** shakes red, then falls back to the normal password prompt.

Press **Deny** to decline, or Escape to dismiss the island. `sudo` uses its
terminal password prompt. For `pkexec`, Discover, and System Settings, the island
becomes the password field after denial, timeout, or a failed match.

The visual behavior and spacing remain governed by [DESIGN.md](DESIGN.md).

## Managing faces

Open **FaceGate** from the app menu or run `facegate-settings`. First-run
onboarding is a top-edge layer-shell island; it yields that position to
approval and enrollment, then returns. The normal face manager remains a
standalone window. **Add a Face** shows enrollment hints,
progress, and Gaze's camera preview as starlight gathered on your face's shape
(MediaPipe Face Mesh, run on the preview only, never stored or sent to Gaze); **Remove** asks for confirmation
before requesting deletion.

Face names and sample counts come from Gaze. Gaze owns all template storage and
authorization, so the same faces appear in `gaze list-faces` and its official
tools.

## Install

Requirements: an Arch-based system with Wayland, Gaze 0.2.7 or newer,
and a webcam (an IR camera is strongly recommended). KDE Plasma is strongly
recommended for the best experience.

### Quick install (AUR)

```bash
paru -S facegate
# or
yay -S facegate
```

The AUR package installs FaceGate, its dependencies (including `gaze-bin`), and
configures PAM/polkit automatically.

### Manual install from source

```bash
git clone https://github.com/sguergachi/facegate.git
cd facegate
./setup.sh
```

Run setup as your normal user. It installs:

1. FaceGate's GTK/layer-shell and build dependencies, and ONNX Runtime for the
   enrollment preview's face mesh.
2. Gaze's prebuilt `gaze-bin` AUR package, including `gazed` and the official PAM
   module.
3. FaceGate's native consent module and UI, then replaces standalone Howdy/Gaze
   rules in the `sudo` and polkit PAM services with one fail-closed managed block.
4. Your infrared camera, if it has one, as Gaze's IR camera. Gaze ships
   colour-only; the installer finds the luma-only V4L2 node and sets it through
   Gaze's own `Config` API, keeping any IR camera you already chose. Gaze then
   matches colour and IR together, and the enrollment preview shows both.
5. The onboarding experience in a top-edge island, where you can add a face.

`setup.sh` no longer installs, calls, or configures Howdy. If Howdy is already
installed, the migration removes its active rules from the two PAM services but
does not automatically uninstall the package or delete its old models. Remove it
after testing Gaze if you no longer need it.

Before each PAM edit, the installer makes a timestamped backup in
`/etc/facegate/`. To remove FaceGate while keeping Gaze and its templates:

```bash
sudo ./uninstall.sh
```

The uninstaller replaces FaceGate's managed block with Gaze's ordinary upstream
PAM rule and restores Plasma's stock polkit agent. A pacman post-transaction
hook reapplies the managed block after `gaze`/`gaze-bin` upgrades so upstream
package scripts cannot accidentally leave a standalone Gaze rule ahead of the
consent step.

## Test mode

Run the HUD from this checkout without PAM, Gaze, or a camera:

```bash
scripts/demo.sh
```

It loops the real interface flow with a simulated alternating result. To preview
the enrollment presentation using a local camera without writing a template:

```bash
scripts/demo.sh enroll
```

The optional live-camera demo needs Arch's `python-opencv` package; production
enrollment does not, because Gaze owns that pipeline.

## AUR upload

To upload FaceGate to the Arch User Repository (AUR):

1. Create a package repository on AUR (or use your personal AUR account)
2. Place the PKGBUILD and source files in the repository
3. Add a post-install hook so users can run `sudo /usr/share/facegate/install.sh` after installing `facegate` from AUR
4. The AUR package depends on `gaze-bin` for the biometric backend and PAM module

See the included `PKGBUILD` for the complete package recipe, or contribute to the
official FaceGate AUR maintenance.

## Development checks

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
python3 -m py_compile facegate/*.py
cc -std=c11 -fPIC -shared -Wall -Wextra -Werror \
  -o /tmp/facegate-consent.so pam/facegate-consent.c -lpam
bash -n install.sh uninstall.sh setup.sh scripts/demo.sh
shellcheck install.sh uninstall.sh setup.sh scripts/demo.sh
```

## Backend maturity

Gaze has a substantially cleaner privilege boundary than FaceGate's former
Howdy integration, but it is still a young project and is not presented here as
independently security-audited. Biometric authentication should remain a
convenience factor with a strong password fallback. Review Gaze's configuration,
use an IR camera, keep both projects updated, and test PAM recovery before relying
on the setup.

## Credits

Face recognition, liveness detection, camera handling, template storage, D-Bus
authorization, and the biometric PAM result are provided by
[Gaze](https://github.com/GunduLabs/gaze) by Gundu Labs and its contributors.
FaceGate is an independent consent/UI layer and is not affiliated with Gaze.

The enrollment preview uses Google's MediaPipe face detector and Face Mesh
models (Apache-2.0), converted to ONNX; see
[facegate/models/NOTICE](facegate/models/NOTICE).
