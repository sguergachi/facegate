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

install -d -m 0755 /usr/local/lib/facegate /usr/local/lib/security/facegate /usr/local/lib/systemd/user \
  /usr/local/share/applications /usr/share/polkit-1/actions
install -d -m 0700 /etc/facegate
# The faces helper runs this package as root, so it must be root's alone:
# a copy owned by the user would let any of their processes edit root's code.
rm -rf /usr/local/lib/facegate/facegate
cp -r --no-preserve=mode,ownership "${project_dir}/facegate" /usr/local/lib/facegate/
find /usr/local/lib/facegate/facegate -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root /usr/local/lib/facegate
chmod -R u=rwX,go=rX /usr/local/lib/facegate
install -m 0755 "${project_dir}/bin/facegate-hud" /usr/local/bin/facegate-hud
install -m 0755 "${project_dir}/bin/facegate-settings" /usr/local/bin/facegate-settings
install -m 0755 "${project_dir}/bin/facegate-faces" /usr/local/lib/facegate/facegate-faces
install -m 0755 "${project_dir}/bin/facegate-faces-list" /usr/local/lib/facegate/facegate-faces-list
install -m 0700 "${project_dir}/pam/facegate-pam.py" /usr/local/lib/security/facegate/pam.py
install -m 0644 "${project_dir}/packaging/facegate.service" /usr/local/lib/systemd/user/facegate.service
install -m 0644 "${project_dir}/packaging/org.facegate.faces.policy" /usr/share/polkit-1/actions/org.facegate.faces.policy
install -m 0644 "${project_dir}/packaging/org.facegate.Settings.desktop" /usr/local/share/applications/org.facegate.Settings.desktop

sudo_pam=/etc/pam.d/sudo
howdy_line='pam_python.so /lib/security/howdy/pam.py'
facegate_line='pam_python.so /usr/local/lib/security/facegate/pam.py'
facegate_rule="auth       sufficient  ${facegate_line}"
# Prints a PAM file with FaceGate's rule ahead of its first auth rule, so a
# face match is enough and every other rule (the password) still follows.
with_facegate_first() {
  awk -v rule="${facegate_rule}" '!added && $1 == "auth" { print rule; added = 1 } { print } END { if (!added) exit 1 }' "$1"
}
if grep -Fq "${facegate_line}" "${sudo_pam}"; then
  echo "FaceGate is already active in ${sudo_pam}."
else
  backup="/etc/facegate/sudo.pam.$(date +%Y%m%d-%H%M%S).bak"
  cp -a "${sudo_pam}" "${backup}"
  if grep -Fq "${howdy_line}" "${sudo_pam}"; then
    sed -i "s#${howdy_line}#${facegate_line}#" "${sudo_pam}"
  elif updated=$(with_facegate_first "${sudo_pam}"); then
    printf '%s\n' "${updated}" > "${sudo_pam}"
  else
    echo "Refusing to change ${sudo_pam}: it has no auth rules." >&2
    exit 1
  fi
  echo "Updated ${sudo_pam}; backup: ${backup}"
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
elif [[ ! -e ${polkit_pam} ]] && [[ -f ${polkit_vendor} ]] && updated=$(with_facegate_first "${polkit_vendor}"); then
  { echo "${polkit_marker}"; printf '%s\n' "${updated}"; } > "${polkit_pam}"
  chmod 0644 "${polkit_pam}"
  echo "Created ${polkit_pam} so polkit prompts use FaceGate."
else
  echo "Leaving polkit unchanged: ${polkit_pam} is customised without a Howdy rule."
fi

target_uid=$(id -u "${target_user}")
runuser -u "${target_user}" -- env \
  XDG_RUNTIME_DIR="/run/user/${target_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${target_uid}/bus" \
  systemctl --user daemon-reload
# FaceGate is the session's polkit agent, so the island replaces KDE's password
# dialog. Only one agent can hold a session: retire KDE's (uninstall restores it).
runuser -u "${target_user}" -- env \
  XDG_RUNTIME_DIR="/run/user/${target_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${target_uid}/bus" \
  sh -c 'systemctl --user stop plasma-polkit-agent.service 2>/dev/null; systemctl --user mask plasma-polkit-agent.service >/dev/null 2>&1 || true'
runuser -u "${target_user}" -- env \
  XDG_RUNTIME_DIR="/run/user/${target_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${target_uid}/bus" \
  systemctl --user enable --now facegate.service
runuser -u "${target_user}" -- env \
  XDG_RUNTIME_DIR="/run/user/${target_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${target_uid}/bus" \
  systemctl --user restart facegate.service

echo "FaceGate installed. Test with: sudo -k; sudo -v"
echo "Manage faces with FaceGate in your app menu, or: facegate-settings"
