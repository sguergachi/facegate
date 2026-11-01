#!/usr/bin/env bash
# Remove FaceGate while leaving Gaze installed and directly usable.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this uninstaller with sudo: sudo ./uninstall.sh" >&2
  exit 1
fi

pam_begin='# FaceGate Gaze consent gate - BEGIN'
pam_end='# FaceGate Gaze consent gate - END'
upstream_rule=
for candidate in /usr/lib/security/pam_gaze.so /usr/lib64/security/pam_gaze.so /lib/security/pam_gaze.so /lib/*/security/pam_gaze.so; do
  [[ -f ${candidate} ]] && { upstream_rule='auth        sufficient    pam_gaze.so'; break; }
done
work_dir=$(mktemp -d)
cleanup() { rm -rf -- "${work_dir}"; }
trap cleanup EXIT

# Replace FaceGate's whole managed block with Gaze's normal upstream rule. This
# does not restore old Howdy references from historical backups.
ungate_pam() {
  local target=$1 label=$2 temporary staged
  [[ -f ${target} ]] || return 0
  grep -Fxq "${pam_begin}" "${target}" || return 0
  temporary="${work_dir}/${label}.pam"
  awk -v begin="${pam_begin}" -v end="${pam_end}" -v replacement="${upstream_rule}" '
    $0 == begin {
      if (replacement != "") print replacement
      managed = 1
      found = 1
      next
    }
    $0 == end { managed = 0; next }
    managed { next }
    { print }
    END { if (!found || managed) exit 42 }
  ' "${target}" > "${temporary}" || {
    echo "Refusing to alter malformed FaceGate block in ${target}." >&2
    return 1
  }
  staged=$(mktemp "${target}.facegate.XXXXXX")
  install -o root -g root -m 0644 "${temporary}" "${staged}"
  mv -f -- "${staged}" "${target}"
  if [[ -n ${upstream_rule} ]]; then
    echo "Removed FaceGate's gate from ${target}; Gaze remains enabled."
  else
    echo "Removed FaceGate's gate from ${target}; Gaze is not installed."
  fi
}

ungate_pam /etc/pam.d/sudo sudo
ungate_pam /etc/pam.d/polkit-1 polkit-1

# The login and lock screens go back to their plain password stacks. Gaze's
# own KDE rules stay off; `sudo gaze-kde-pam enable` brings them back.
for service in plasmalogin:greeter kde-fingerprint:'lock screen'; do
  face_pam="/etc/pam.d/${service%%:*}"
  face_begin="# FaceGate ${service#*:} gate - BEGIN"
  face_end="# FaceGate ${service#*:} gate - END"
  [[ -f ${face_pam} ]] && grep -Fxq "${face_begin}" "${face_pam}" || continue
  temporary="${work_dir}/${service%%:*}.pam"
  awk -v begin="${face_begin}" -v end="${face_end}" '
    $0 == begin { managed = 1; next }
    $0 == end { managed = 0; next }
    !managed { print }
  ' "${face_pam}" > "${temporary}"
  staged=$(mktemp "${face_pam}.facegate.XXXXXX")
  install -o root -g root -m 0644 "${temporary}" "${staged}"
  mv -f -- "${staged}" "${face_pam}"
  echo "Removed FaceGate's gate from ${face_pam}."
done
rm -f /etc/facegate/greeter
systemctl --global disable facegate-greeter.service 2>/dev/null || true

target_user=${SUDO_USER:-}
if [[ -n ${target_user} && ${target_user} != root ]]; then
  target_uid=$(id -u "${target_user}")
  user_systemctl=(runuser -u "${target_user}" -- env
    "XDG_RUNTIME_DIR=/run/user/${target_uid}"
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${target_uid}/bus"
    systemctl --user)
  "${user_systemctl[@]}" disable --now facegate.service 2>/dev/null || true
  "${user_systemctl[@]}" disable --now facegate-tray.service 2>/dev/null || true
  for svc in plasma-polkit-agent.service hyprpolkitagent.service polkit-gnome-authentication-agent-1.service; do
    "${user_systemctl[@]}" unmask "${svc}" 2>/dev/null || true
    "${user_systemctl[@]}" start "${svc}" 2>/dev/null || true
  done
fi

rm -f \
  /usr/local/bin/facegate-hud \
  /usr/local/bin/facegate-settings \
  /usr/local/bin/facegate-tray \
  /usr/local/lib/systemd/user/facegate.service \
  /usr/local/lib/systemd/user/facegate-tray.service \
  /usr/local/lib/systemd/user/facegate-greeter.service \
  /usr/local/share/applications/org.facegate.Settings.desktop \
  /usr/local/share/applications/org.facegate.Island.desktop \
  /usr/local/share/icons/hicolor/scalable/apps/facegate-symbolic.svg \
  /etc/pacman.d/hooks/facegate-gaze.hook \
  /usr/share/polkit-1/actions/org.facegate.faces.policy
rm -rf /usr/local/lib/facegate /usr/local/lib/security/facegate
systemctl daemon-reload
systemctl try-restart polkit.service >/dev/null 2>&1 || true

if [[ -n ${upstream_rule} ]]; then
  echo "FaceGate removed. Gaze and its face templates were left intact."
else
  echo "FaceGate removed. The normal password stacks remain active."
fi
