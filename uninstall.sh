#!/usr/bin/env bash
# Restores the original sudo PAM configuration from FaceGate's latest backup.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this uninstaller with sudo: sudo ./uninstall.sh" >&2
  exit 1
fi

backup=$(find /etc/facegate -maxdepth 1 -name 'sudo.pam.*.bak' -type f -printf '%T@ %p\n' | sort -nr | head -n 1 | cut -d' ' -f2-)
if [[ -z ${backup} ]]; then
  echo "No FaceGate sudo PAM backup found; no PAM file was changed." >&2
  exit 1
fi
install -m 0644 "${backup}" /etc/pam.d/sudo
polkit_pam=/etc/pam.d/polkit-1
if [[ -f ${polkit_pam} ]] && head -n 1 "${polkit_pam}" | grep -Fq '# Managed by FaceGate'; then
  rm -f "${polkit_pam}"
  echo "Removed ${polkit_pam}; polkit uses /usr/lib/pam.d/polkit-1 again."
else
  polkit_backup=$(find /etc/facegate -maxdepth 1 -name 'polkit-1.pam.*.bak' -type f -printf '%T@ %p\n' | sort -nr | head -n 1 | cut -d' ' -f2-)
  if [[ -n ${polkit_backup} ]]; then
    install -m 0644 "${polkit_backup}" "${polkit_pam}"
    echo "Restored ${polkit_pam} from ${polkit_backup}."
  fi
fi
rm -f /usr/local/bin/facegate-hud /usr/local/lib/security/facegate/pam.py /usr/local/lib/systemd/user/facegate.service
rm -rf /usr/local/lib/facegate /usr/local/lib/security/facegate
echo "Restored /etc/pam.d/sudo from ${backup}. Disable the user service with: systemctl --user disable --now facegate.service"
