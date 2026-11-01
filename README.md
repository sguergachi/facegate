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

> **KDE Plasma on Wayland only.** FaceGate replaces Plasma's polkit agent and
> asks KWin which monitor is active. GNOME, Hyprland, Sway, X11, and other
> desktops are not currently supported.

## Install (Arch-based, Wayland)

```bash
yay -S facegate        # from the AUR; pulls in Gaze and wires PAM
facegate-enable        # once per user, from your Wayland session
facegate-settings      # add your face
```

Website: <https://sguergachi.github.io/facegate/> · Remove with
`pacman -R facegate` (Gaze and templates stay). Maintainers: see
[aur/README.md](aur/README.md). The source-tree `setup.sh`/`install.sh` remain
available for development.

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

FaceGate gates `sudo` and polkit elevation and, if you opt in, shows the
island for face sign-in on the lock screen and the login screen (see
[Lock screen and login](#lock-screen-and-login)).

## Experience

- **‹App› wants sudo** names the requesting app and command. The camera stays off
  until you approve.
- **Looking for you** animates while Gaze performs liveness and recognition.
- **Verified** closes the arc into a green ring before authentication continues.
- **Not recognized** shakes red, then falls back to the normal password prompt.

Press **Deny** to decline, or Escape to dismiss the island. After denial,
timeout, or a failed match, the island becomes the password field for `sudo`,
`pkexec`, Discover, and System Settings. Escape on the password field hands
`sudo` back to its terminal prompt.

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

## Lock screen and login

Setup offers face sign-in on both screens. Neither one has an **Approve**
step, because sitting down at them is the request.

- **Login (Plasma Login Manager):** press Enter with the password field empty.
  The island drops from the top edge ("Looking for you"), Gaze checks your face,
  and "Welcome back" signs you in. If you typed a password instead, FaceGate
  passes it straight to the normal password stack without scanning, and
  KWallet unlocks as usual. The greeter only calls PAM when the form is
  submitted, so this needs the Enter press.

  The island runs inside the greeter's own session, as its `plasmalogin` user
  (`facegate-greeter.service`, started by `plasma-login-wayland.target`).
  `/etc/pam.d/plasmalogin` gets a FaceGate block like the sudo one. The
  `greeter` mode of the consent module only picks the path (typed password or
  face), and `pam_gaze.so` alone decides. A face mismatch ends that attempt
  without counting as a failed password in `pam_faillock`. A locked-out or
  `nologin` account can't sign in by face.
- **Lock screen:** KScreenLocker starts its biometric slot
  (`/etc/pam.d/kde-fingerprint`) as soon as the screen locks. FaceGate's block
  there tells your island to drop over the lock screen ("Looking for you"), and
  Gaze decides. The island then says "Welcome back" and the screen unlocks.
  You get one scan per lock; if it gives up while you're away, press Enter on
  the empty password field to scan again.

  KWin hides every other surface while the session is locked, unless the
  executable is trusted with `kde_lockscreen_overlay_v1` (plasmashell holds the
  same grant). That's why the island is a small root-owned native launcher,
  `/usr/local/bin/facegate-hud`, that embeds Python and only ever runs
  FaceGate's installed island. The grant lives in
  `org.facegate.Island.desktop`. Trusting `/usr/bin/python3` instead would let
  every Python program draw over the lock screen.

After a face login, KWallet asks for your password once, because no password
was typed for it to reuse.

To turn face sign-in off, run `sudo rm /etc/facegate/greeter`, then restore
`/etc/pam.d/plasmalogin`
and `/etc/pam.d/kde-fingerprint` from their backups in `/etc/facegate/`.

## Install

Requirements: an Arch-based system, KDE Plasma on Wayland, Gaze 0.2.7 or newer,
and a webcam (an IR camera is strongly recommended).

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
5. Optionally, face sign-in with the island at the lock screen and login
   screen (see [Lock screen and login](#lock-screen-and-login)).
6. The onboarding experience in a top-edge island, where you can add a face.

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
