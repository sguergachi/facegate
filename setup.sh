#!/usr/bin/env bash
# First-run setup for Gaze-backed FaceGate on Arch/KDE Plasma Wayland.
# Run as yourself; the script asks for sudo only for system changes.
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bold=$'\e[1m' dim=$'\e[2m' green=$'\e[32m' red=$'\e[31m' reset=$'\e[0m'
step_number=0
step() { step_number=$((step_number + 1)); printf '\n%s%d. %s%s\n' "${bold}" "${step_number}" "$1" "${reset}"; }
info() { printf '   %s\n' "$1"; }
done_() { printf '   %s✓%s %s\n' "${green}" "${reset}" "$1"; }
fail() { printf '\n%s✗ %s%s\n' "${red}" "$1" "${reset}" >&2; exit 1; }
ask() { local reply; read -r -p "   $1 [y/N] " reply; [[ ${reply} == [yY]* ]]; }

if [[ ${EUID} -eq 0 ]]; then
  fail "Run ./setup.sh as your normal user. It asks for sudo when needed."
fi

printf '%sFaceGate setup%s\n' "${bold}" "${reset}"
printf '%sExplicit face approval for sudo and admin prompts, secured by Gaze.%s\n' "${dim}" "${reset}"

step "Checking this computer"
command -v pacman >/dev/null || fail "FaceGate currently supports Arch-based systems (pacman) only."
[[ ${XDG_CURRENT_DESKTOP:-} == *KDE* ]] || fail "FaceGate supports KDE Plasma only."
[[ ${XDG_SESSION_TYPE:-} == wayland ]] || fail "Run this from a Plasma Wayland session."
sudo -v || fail "sudo is needed to install packages and configure PAM."
# Keep the sudo ticket fresh through an AUR package installation.
while true; do sudo -n true 2>/dev/null; sleep 50; kill -0 "$$" 2>/dev/null || exit; done &
keepalive=$!
cleanup() { kill "${keepalive}" 2>/dev/null || true; }
trap cleanup EXIT
done_ "Arch-based KDE Plasma Wayland session"

step "Installing FaceGate's dependencies"
sudo pacman -S --needed --noconfirm \
  base-devel git pam python python-gobject python-cairo python-numpy \
  gtk4 gtk4-layer-shell libappindicator polkit wayland plasma-wayland-protocols
# The enrollment preview's face mesh; any system ONNX Runtime will do.
if ! python3 -I -c 'import onnxruntime' 2>/dev/null; then
  sudo pacman -S --needed --noconfirm python-onnxruntime-cpu
fi
done_ "Packages from the official repositories"

aur_helper=
find_aur_helper() {
  [[ -n ${aur_helper} ]] && return
  for helper in paru yay; do
    command -v "${helper}" >/dev/null && { aur_helper=${helper}; return; }
  done
  info "Gaze is distributed through the AUR, and no AUR helper was found."
  ask "Install paru now?" || fail "Install paru or yay, then run ./setup.sh again."
  helper_dir=$(mktemp -d)
  git clone --depth 1 https://aur.archlinux.org/paru-bin.git "${helper_dir}/paru-bin"
  # AUR packages are user-submitted: show what will run before building it,
  # and let pacman confirm the install itself.
  info "paru-bin's build script (PKGBUILD), from the AUR:"
  sed 's/^/     /' "${helper_dir}/paru-bin/PKGBUILD"
  ask "Build and install paru-bin from this PKGBUILD?" || {
    rm -rf -- "${helper_dir}"
    fail "Install paru or yay, then run ./setup.sh again."
  }
  (cd "${helper_dir}/paru-bin" && makepkg -si)
  rm -rf -- "${helper_dir}"
  aur_helper=paru
}

step "Installing Gaze (biometric security backend)"
gaze_pam=
for candidate in /usr/lib/security/pam_gaze.so /usr/lib64/security/pam_gaze.so /lib/security/pam_gaze.so /lib/*/security/pam_gaze.so; do
  [[ -f ${candidate} ]] && { gaze_pam=${candidate}; break; }
done
if command -v gazed >/dev/null && command -v gaze >/dev/null && [[ -n ${gaze_pam} ]]; then
  done_ "Gaze's daemon, CLI, and PAM module are already installed"
else
  find_aur_helper
  info "Installing Gaze's prebuilt daemon, CLI, and official PAM module with ${aur_helper}."
  "${aur_helper}" -S --needed gaze-bin
  pacman -Q gaze-bin >/dev/null 2>&1 || fail "Gaze did not install."
  done_ "Gaze installed"
fi
sudo systemctl enable --now gazed.service
systemctl_status=$(sudo systemctl is-active gazed.service 2>/dev/null || true)
[[ ${systemctl_status} == active ]] || fail "gazed.service did not start; run: sudo journalctl -u gazed -n 100"
done_ "Gaze daemon is active"

if pacman -Q howdy >/dev/null 2>&1; then
  info "Howdy is installed but FaceGate will no longer call it or reference its PAM module."
  info "After confirming Gaze works, you may remove it with: paru -Rns howdy pam-python"
fi

step "Checking the Gaze backend"
if gaze doctor; then
  done_ "Gaze health check passed"
else
  info "Gaze reported setup warnings. No face may be enrolled yet; review the output above."
fi

step "Connecting the existing FaceGate experience to Gaze"
sudo "${project_dir}/install.sh"
done_ "FaceGate is active; only Gaze can return biometric success"

step "Face unlock at the lock screen and login (optional)"
info "The lock screen starts looking for you as soon as it locks, with the"
info "island over it. At the login screen, press Enter on an empty password"
info "field and the island looks for you; a typed password signs in as usual."
info "There is no Approve step on either: you asked by sitting down. KWallet"
info "asks for its password once after a face login."
if ask "Use your face at the lock screen and login?"; then
  sudo touch /etc/facegate/greeter
  sudo /usr/local/lib/facegate/refresh-pam
  done_ "Face unlock is on at the lock screen and login"
else
  info "Skipped. Run ./setup.sh again to add it later."
fi

step "Meeting FaceGate"
info "FaceGate opens to show how it works and add your face."
setsid facegate-settings --welcome >/dev/null 2>&1 < /dev/null &
done_ "Opened FaceGate"

printf '\n%sAll set.%s Finish in the FaceGate window. Your password remains the fallback.\n' "${bold}" "${reset}"
