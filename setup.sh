#!/usr/bin/env bash
# First-run setup: installs Howdy and FaceGate's dependencies, picks the
# camera, wires FaceGate into sudo and polkit, then opens FaceGate's
# onboarding, which walks you through adding your face.
# Run it as yourself (not with sudo); it asks for sudo when it needs it.
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
howdy_dir=/usr/lib/security/howdy
howdy_config=${howdy_dir}/config.ini

bold=$'\e[1m' dim=$'\e[2m' green=$'\e[32m' red=$'\e[31m' reset=$'\e[0m'
step_number=0
step() { step_number=$((step_number + 1)); printf '\n%s%d. %s%s\n' "${bold}" "${step_number}" "$1" "${reset}"; }
info() { printf '   %s\n' "$1"; }
done_() { printf '   %s✓%s %s\n' "${green}" "${reset}" "$1"; }
fail() { printf '\n%s✗ %s%s\n' "${red}" "$1" "${reset}" >&2; exit 1; }
ask() { local reply; read -r -p "   $1 [y/N] " reply; [[ ${reply} == [yY]* ]]; }

if [[ ${EUID} -eq 0 ]]; then
  fail "Run ./setup.sh as your normal user. It asks for sudo when it needs it."
fi

printf '%sFaceGate setup%s\n' "${bold}" "${reset}"
printf '%sFace approval for sudo and admin prompts, powered by Howdy.%s\n' "${dim}" "${reset}"

step "Checking this computer"
command -v pacman >/dev/null || fail "FaceGate currently supports Arch-based systems (pacman) only."
[[ ${XDG_CURRENT_DESKTOP:-} == *KDE* ]] || fail "FaceGate supports KDE Plasma only."
[[ ${XDG_SESSION_TYPE:-} == wayland ]] || fail "Run this from a Plasma Wayland session."
sudo -v || fail "sudo is needed to install packages and configure PAM."
# Keep the sudo ticket fresh through long AUR builds.
while true; do sudo -n true 2>/dev/null; sleep 50; kill -0 "$$" 2>/dev/null || exit; done &
keepalive=$!
trap 'kill "${keepalive}" 2>/dev/null || true' EXIT
done_ "Arch-based KDE Plasma Wayland session"

step "Installing FaceGate's dependencies"
sudo pacman -S --needed --noconfirm \
  base-devel git python python-gobject python-cairo gtk4 gtk4-layer-shell libappindicator polkit v4l-utils
done_ "Packages from the official repositories"

step "Installing Howdy (face recognition)"
if pacman -Q howdy >/dev/null 2>&1; then
  done_ "Howdy $(pacman -Q howdy | cut -d' ' -f2) is already installed"
else
  aur_helper=
  for helper in paru yay; do
    command -v "${helper}" >/dev/null && { aur_helper=${helper}; break; }
  done
  if [[ -z ${aur_helper} ]]; then
    info "Howdy lives in the AUR, and no AUR helper (paru or yay) was found."
    ask "Install paru now?" || fail "Install paru or yay, then run ./setup.sh again."
    build_dir=$(mktemp -d)
    git clone --depth 1 https://aur.archlinux.org/paru-bin.git "${build_dir}/paru-bin"
    (cd "${build_dir}/paru-bin" && makepkg -si --noconfirm)
    rm -rf "${build_dir}"
    aur_helper=paru
  fi
  info "Building Howdy with ${aur_helper}. Compiling dlib can take 10+ minutes."
  "${aur_helper}" -S --needed howdy
  pacman -Q howdy >/dev/null 2>&1 || fail "Howdy did not install."
  done_ "Howdy installed"
fi
pacman -Q pam-python >/dev/null 2>&1 || fail "pam-python is missing; it should come with Howdy."

step "Choosing a camera"
current_camera=$(sudo sed -n 's/^device_path *= *//p' "${howdy_config}")
if [[ -n ${current_camera} && ${current_camera} != none && -e ${current_camera} ]]; then
  camera=${current_camera}
  done_ "Howdy already uses ${camera}"
else
  # Collect capture devices once each (by-id links are stable across reboots).
  # Metadata nodes list no formats; an IR sensor streams 8-bit greyscale.
  declare -A seen=()
  cameras=() labels=() ir_cameras=()
  for link in /dev/v4l/by-id/* /dev/v4l/by-path/* /dev/video*; do
    [[ -e ${link} ]] || continue
    node=$(readlink -f "${link}")
    [[ -z ${seen[${node}]:-} ]] || continue
    seen[${node}]=1
    formats=$(v4l2-ctl -d "${node}" --list-formats 2>/dev/null | grep -o "'[A-Z0-9 ]*'" || true)
    [[ -n ${formats} ]] || continue
    name=$(cat "/sys/class/video4linux/${node##*/}/name" 2>/dev/null || echo camera)
    kind=color
    if grep -q "'GREY'" <<<"${formats}"; then kind=infrared; ir_cameras+=("${link}"); fi
    cameras+=("${link}")
    labels+=("${name} (${kind}) ${dim}${link}${reset}")
  done
  if ((${#cameras[@]} == 0)); then
    fail "No camera found. Connect one and run ./setup.sh again."
  elif ((${#ir_cameras[@]} == 1)); then
    camera=${ir_cameras[0]}
    info "Using the infrared camera: it works in the dark and resists photos."
  elif ((${#cameras[@]} == 1)); then
    camera=${cameras[0]}
  else
    for i in "${!cameras[@]}"; do printf '   %d) %s\n' "$((i + 1))" "${labels[i]}"; done
    read -r -p "   Which camera should recognise you? [1] " choice
    choice=${choice:-1}
    if [[ ! ${choice} =~ ^[0-9]+$ ]] || ((choice < 1 || choice > ${#cameras[@]})); then
      fail "No camera ${choice}."
    fi
    camera=${cameras[choice - 1]}
  fi
  sudo sed -i "s#^device_path *=.*#device_path = ${camera}#" "${howdy_config}"
  done_ "Howdy will use ${camera}"
fi

step "Connecting FaceGate to sudo and admin prompts"
sudo "${project_dir}/install.sh"
done_ "FaceGate is active"

step "Meeting FaceGate"
info "The FaceGate island opens to show how it works and add your face."
# Onboarding asks for admin approval in the same island before it adds a face.
setsid facegate-settings --welcome >/dev/null 2>&1 < /dev/null &
done_ "Opened FaceGate"

printf '\n%sAll set.%s Finish in the FaceGate island. You can reopen it any time\n' "${bold}" "${reset}"
printf 'from its tray icon or your app menu. Your password always works too.\n'
