# FaceGate security model

FaceGate is an explicit-consent interface around Gaze. It is designed so the
desktop UI cannot manufacture biometric authentication success. This document
states the boundary precisely; it is not a claim that either project has had an
independent security audit.

## Trust boundary

| Component | Privilege | Security role |
| --- | --- | --- |
| `gazed` | Root, system service | Owns cameras, liveness, recognition, templates, and enrollment writes |
| Official `pam_gaze.so` | Loaded by PAM | Returns the biometric authentication verdict |
| `facegate-consent.so` | Loaded by PAM | Asks whether a scan may start and reports UI state; never returns authentication success to the application |
| FaceGate HUD | Logged-in user | Displays consent, progress, results, and the polkit password fallback |
| FaceGate Settings/helper | Logged-in user | Calls Gaze's D-Bus API; cannot access template files and cannot waive Gaze's polkit checks |
| `polkitd` and its helper | System | Select the permitted identity and validate the password/PAM result |

FaceGate's PAM control block is intentionally fail-closed around the biometric
branch:

1. No approval, malformed approval, missing HUD, remote request, or timeout skips
   Gaze and continues to the existing password stack.
2. Approval can only advance to the official Gaze module.
3. Every non-success return from Gaze continues to the password stack.
4. Only Gaze success reaches the isolated `pam_permit.so` success branch.
5. UI result notifications use ignored PAM return values and cannot affect the
   verdict.
6. If Gaze reports no enrollment (or the preflight cannot be completed), the
   HUD declines the scan and PAM goes directly to password authentication.
   This check can only remove the biometric path; it cannot grant access.

The compiled consent module and Gaze's PAM module are root-owned. The managed
PAM block references Gaze's official installed module path directly.
The installer refuses a Gaze module that is not root-owned or is writable by
group/other. It writes PAM files atomically and keeps timestamped backups under
root-only `/etc/facegate`. A root-owned pacman hook rebuilds the managed block
after Gaze package upgrades, because the upstream Arch package may rewrite its
own PAM entries during that transaction.

## What a compromised UI can and cannot do

A process running as the desktop user can talk to or imitate the `0600` consent
socket. It may approve a camera attempt, decline one, suppress the presentation,
or display a false result. It still cannot make `pam_gaze.so` return success,
change Gaze templates without passing Gaze's polkit authorization, or replace
the root-owned installed code.

FaceGate is the polkit authentication agent in order to preserve the existing
password-in-island fallback. Consequently, the FaceGate UI process receives a
password typed into that field before forwarding it to `PolkitAgent.Session`.
A compromised UI could capture that password. The stricter alternative is to
leave Plasma's stock polkit agent enabled; doing that changes the fallback UI.
`sudo` passwords stay in the terminal and never pass through FaceGate.

Malware with root access, write access to PAM configuration or installed
modules, control of Gaze, or the ability to tamper with the user's whole desktop
session is outside this boundary.

## Scope

FaceGate changes only the `sudo` and `polkit-1` PAM services. It does not alter
SSH or Gaze's integrations with other authentication surfaces. If you accept the
optional setup step, `kde-fingerprint` (the lock screen's biometric slot) and
`plasmalogin` get FaceGate blocks. On the lock screen, the consent module only
announces the scan (`scanning` mode, always `PAM_IGNORE`) before
`pam_gaze.so`. In that block, the
consent module's `greeter` mode reads the password the greeter already holds:
a typed password goes to the normal stack, and an empty field goes to
`pam_gaze.so`, whose verdict alone authenticates. The module sends only
display updates to the greeter user's island socket, and it checks that
socket's owner and peer uid the same way it does for sudo. Both blocks keep
`pam_nologin` and `pam_faillock preauth` ahead of the face match. There's no
consent step on either screen.

To appear over the lock screen, the island's native launcher
(`/usr/local/bin/facegate-hud`) is granted KWin's `kde_lockscreen_overlay_v1`
through `org.facegate.Island.desktop`. KWin identifies the client by its
executable, so the launcher is root-owned and runs only FaceGate's installed
island in isolated mode. Malware running as the same user could still inject
into that process and draw over the lock screen, for example to phish the
password. That is the same exposure as any other trusted Plasma component, and
it's within the same-user limit stated above. The lock screen itself still
verifies the password. Uninstalling FaceGate leaves Gaze installed and replaces the managed
block with Gaze's normal upstream PAM rule.

Biometrics are a convenience credential, not a substitute for a strong
password. A password fallback remains available whenever FaceGate or Gaze fails.

## Upstream considerations

Gaze provides the stronger separation used here, including a sandboxed daemon,
system D-Bus caller checks, polkit authorization for template changes, liveness
processing, and root-only template storage. It is nevertheless young and should
not be described as proven or independently audited without evidence.

At the version reviewed for this migration (0.2.7), Gaze enrollment captures
several pose samples but does not appear to reject a session merely because
different people supplied different samples. Enroll only in a private setting,
cancel if anyone else enters the camera view, and follow upstream fixes for this
case. FaceGate deliberately does not duplicate template validation because doing
so would move biometric security back into the UI project.

## Reporting a vulnerability

When reporting a security problem, include the FaceGate version or commit, Gaze
version, distribution, affected PAM service, and the relevant sanitized PAM
block. Do not include face templates, camera frames, passwords, or other
credentials in a report.
