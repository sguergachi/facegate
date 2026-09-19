#!/usr/bin/env bash
# Install FaceGate as an unprivileged consent UI in front of Gaze.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer with sudo: sudo ./install.sh" >&2
  exit 1
fi

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
target_user=${SUDO_USER:-}
if [[ -z ${target_user} || ${target_user} == root ]]; then
  echo "Run this from your graphical user session with sudo, not directly as root." >&2
  exit 1
fi

for command in cc awk install; do
  command -v "${command}" >/dev/null || {
    echo "Missing required command: ${command}" >&2
    exit 1
  }
done

gaze_pam=
gaze_pam_real=
for candidate in \
  /usr/lib/security/pam_gaze.so \
  /usr/lib64/security/pam_gaze.so \
  /lib/security/pam_gaze.so \
  /lib/*/security/pam_gaze.so; do
  if [[ -f ${candidate} ]]; then
    gaze_pam=${candidate}
    gaze_pam_real=$(readlink -f "${candidate}")
    break
  fi
done
if [[ -z ${gaze_pam} ]]; then
  echo "Gaze's official pam_gaze.so was not found. Run ./setup.sh first." >&2
  exit 1
fi
if ! command -v gazed >/dev/null || ! command -v gaze >/dev/null; then
  echo "Gaze's daemon or CLI was not found. Run ./setup.sh first." >&2
  exit 1
fi
minimum_gaze=0.2.7
gaze_version=$(gaze --version 2>/dev/null | grep -Eo '[0-9]+[.][0-9]+[.][0-9]+' | head -n 1 || true)
if [[ -z ${gaze_version} ]] ||
  [[ $(printf '%s\n%s\n' "${minimum_gaze}" "${gaze_version}" | sort -V | head -n 1) != "${minimum_gaze}" ]]; then
  echo "FaceGate requires Gaze ${minimum_gaze} or newer (found: ${gaze_version:-unknown})." >&2
  exit 1
fi
if [[ $(stat -c '%u' "${gaze_pam_real}") != 0 ]] ||
  find "${gaze_pam_real}" -maxdepth 0 -perm /022 -print -quit | grep -q .; then
  echo "Refusing to use ${gaze_pam}: the Gaze PAM module is not securely root-owned." >&2
  exit 1
fi
[[ -f /usr/include/security/pam_modules.h ]] || {
  echo "PAM development headers are missing (on Arch they are provided by pam)." >&2
  exit 1
}

build_dir=$(mktemp -d)
cleanup() { rm -rf -- "${build_dir}"; }
trap cleanup EXIT
cc -std=c11 -fPIC -shared -O2 -Wall -Wextra -Werror \
  -Wl,-z,relro,-z,now -o "${build_dir}/consent.so" \
  "${project_dir}/pam/facegate-consent.c" -lpam

install -d -m 0755 \
  /usr/local/bin \
  /usr/local/lib/facegate \
  /usr/local/lib/security/facegate \
  /usr/local/lib/systemd/user \
  /usr/local/share/applications \
  /usr/local/share/icons/hicolor/scalable/apps \
  /etc/pacman.d/hooks
install -d -m 0700 /etc/facegate

# Every executable in the authentication-facing path is root-owned. The user UI
# can request a scan, but cannot replace the consent module or Gaze PAM module.
rm -rf /usr/local/lib/facegate/facegate
cp -r --no-preserve=mode,ownership "${project_dir}/facegate" /usr/local/lib/facegate/
find /usr/local/lib/facegate/facegate -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root /usr/local/lib/facegate
chmod -R u=rwX,go=rX /usr/local/lib/facegate
install -o root -g root -m 0755 "${project_dir}/bin/facegate-hud" /usr/local/bin/facegate-hud
install -o root -g root -m 0755 "${project_dir}/bin/facegate-settings" /usr/local/bin/facegate-settings
install -o root -g root -m 0755 "${project_dir}/bin/facegate-tray" /usr/local/bin/facegate-tray
install -o root -g root -m 0755 "${project_dir}/bin/facegate-faces" /usr/local/lib/facegate/facegate-faces
install -o root -g root -m 0755 "${project_dir}/scripts/refresh-pam" /usr/local/lib/facegate/refresh-pam
install -o root -g root -m 0755 "${build_dir}/consent.so" /usr/local/lib/security/facegate/consent.so
install -o root -g root -m 0644 "${project_dir}/packaging/facegate.service" /usr/local/lib/systemd/user/facegate.service
install -o root -g root -m 0644 "${project_dir}/packaging/facegate-tray.service" /usr/local/lib/systemd/user/facegate-tray.service
install -o root -g root -m 0644 "${project_dir}/packaging/facegate-greeter.service" /usr/local/lib/systemd/user/facegate-greeter.service
install -o root -g root -m 0644 "${project_dir}/packaging/org.facegate.Settings.desktop" /usr/local/share/applications/org.facegate.Settings.desktop
install -o root -g root -m 0644 "${project_dir}/packaging/facegate-symbolic.svg" /usr/local/share/icons/hicolor/scalable/apps/facegate-symbolic.svg
install -o root -g root -m 0644 "${project_dir}/packaging/facegate-gaze.hook" /etc/pacman.d/hooks/facegate-gaze.hook

# Remove obsolete Howdy-era FaceGate components. Gaze itself owns enrollment,
# policy checks, the camera, templates, recognition, and the biometric verdict.
rm -f \
  /usr/local/lib/facegate/facegate-faces-list \
  /usr/local/lib/security/facegate/pam.py \
  /usr/local/lib/security/facegate/gaze-auth.so \
  /usr/share/polkit-1/actions/org.facegate.faces.policy

# This also removes standalone Howdy/Gaze rules. The pacman hook repeats it
# after Gaze upgrades, whose package scripts may regenerate those rules.
/usr/local/lib/facegate/refresh-pam

systemctl daemon-reload
systemctl enable --now gazed.service
# The login screen's island starts with Plasma Login Manager's greeter session.
if getent passwd plasmalogin >/dev/null; then
  systemctl --global enable facegate-greeter.service
fi
systemctl try-restart polkit.service >/dev/null 2>&1 || true

# Gaze ships colour-only. Hand it the infrared camera, if there is one, through
# its own validated Config API; a camera already chosen for IR is kept.
python3 -I -c 'import sys; sys.path.insert(0, "/usr/local/lib/facegate"); from facegate.cameras import main; raise SystemExit(main())' ||
  echo "Continuing without infrared; run the installer again once gazed is running." >&2

target_uid=$(id -u "${target_user}")
user_systemctl=(runuser -u "${target_user}" -- env
  "XDG_RUNTIME_DIR=/run/user/${target_uid}"
  "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${target_uid}/bus"
  systemctl --user)
"${user_systemctl[@]}" daemon-reload
# Preserve the current FaceGate password-in-island experience by keeping its
# polkit agent as the only session agent. polkitd/PAM still validate passwords.
"${user_systemctl[@]}" stop plasma-polkit-agent.service 2>/dev/null || true
"${user_systemctl[@]}" mask plasma-polkit-agent.service >/dev/null 2>&1 || true
"${user_systemctl[@]}" enable --now facegate.service
"${user_systemctl[@]}" restart facegate.service

echo "FaceGate installed with Gaze as its biometric backend."
echo "Test with: sudo -k; sudo -v"
echo "Manage faces from the FaceGate app, or run: facegate-settings"
