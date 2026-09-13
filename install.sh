#!/usr/bin/env bash
# Installs FaceGate without modifying Howdy itself.
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

install -d -m 0755 /usr/local/lib/facegate /usr/local/lib/security/facegate /usr/local/lib/systemd/user
install -d -m 0700 /etc/facegate
cp -a "${project_dir}/facegate" /usr/local/lib/facegate/
install -m 0755 "${project_dir}/bin/facegate-hud" /usr/local/bin/facegate-hud
install -m 0700 "${project_dir}/pam/facegate-pam.py" /usr/local/lib/security/facegate/pam.py
install -m 0644 "${project_dir}/packaging/facegate.service" /usr/local/lib/systemd/user/facegate.service

sudo_pam=/etc/pam.d/sudo
howdy_line='pam_python.so /lib/security/howdy/pam.py'
facegate_line='pam_python.so /usr/local/lib/security/facegate/pam.py'
if grep -Fq "${facegate_line}" "${sudo_pam}"; then
  echo "FaceGate is already active in ${sudo_pam}."
elif grep -Fq "${howdy_line}" "${sudo_pam}"; then
  backup="/etc/facegate/sudo.pam.$(date +%Y%m%d-%H%M%S).bak"
  cp -a "${sudo_pam}" "${backup}"
  sed -i "s#${howdy_line}#${facegate_line}#" "${sudo_pam}"
  echo "Updated ${sudo_pam}; backup: ${backup}"
else
  echo "Refusing to change ${sudo_pam}: expected Howdy rule was not found." >&2
  exit 1
fi

# Graphical admin prompts (pkexec, KDE "Authentication Required") use polkit's
# PAM service. Its rule ships in /usr/lib/pam.d; an /etc/pam.d copy overrides
# it and survives package updates.
polkit_vendor=/usr/lib/pam.d/polkit-1
polkit_pam=/etc/pam.d/polkit-1
polkit_marker='# Managed by FaceGate: copy of /usr/lib/pam.d/polkit-1 with Howdy gated.'
if [[ -f ${polkit_pam} ]] && grep -Fq "${facegate_line}" "${polkit_pam}"; then
  echo "FaceGate is already active in ${polkit_pam}."
elif [[ -f ${polkit_pam} ]] && grep -Fq "${howdy_line}" "${polkit_pam}"; then
  backup="/etc/facegate/polkit-1.pam.$(date +%Y%m%d-%H%M%S).bak"
  cp -a "${polkit_pam}" "${backup}"
  sed -i "s#${howdy_line}#${facegate_line}#" "${polkit_pam}"
  echo "Updated ${polkit_pam}; backup: ${backup}"
elif [[ ! -e ${polkit_pam} ]] && [[ -f ${polkit_vendor} ]] && grep -Fq "${howdy_line}" "${polkit_vendor}"; then
  { echo "${polkit_marker}"; sed "s#${howdy_line}#${facegate_line}#" "${polkit_vendor}"; } > "${polkit_pam}"
  chmod 0644 "${polkit_pam}"
  echo "Created ${polkit_pam} to gate Howdy for polkit prompts."
else
  echo "Leaving polkit unchanged: no Howdy rule found for it."
fi

target_uid=$(id -u "${target_user}")
runuser -u "${target_user}" -- env \
  XDG_RUNTIME_DIR="/run/user/${target_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${target_uid}/bus" \
  systemctl --user daemon-reload
runuser -u "${target_user}" -- env \
  XDG_RUNTIME_DIR="/run/user/${target_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${target_uid}/bus" \
  systemctl --user enable --now facegate.service
runuser -u "${target_user}" -- env \
  XDG_RUNTIME_DIR="/run/user/${target_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${target_uid}/bus" \
  systemctl --user restart facegate.service

echo "FaceGate installed. Test with: sudo -k; sudo -v"
