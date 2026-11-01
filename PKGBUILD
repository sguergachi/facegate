# Maintainer: sguergachi <sguergachi@gmail.com>
# Contributor: Community
pkgname=facegate
pkgver=1.0.0
pkgrel=1
pkgdesc="Face ID-style consent interface for Gaze — sudo/polkit approval via face"
arch=(any)
url="https://github.com/sguergachi/facegate"
license=(MIT)
depends=(
  python python-gobject python-cairo python-numpy
  gtk4 gtk4-layer-shell libappindicator polkit
  python-onnxruntime-cpu
  gaze-bin
)
makedepends=(
  base-devel gcc pam
)
source=("$pkgname-$pkgver.tar.gz::https://github.com/sguergachi/facegate/archive/refs/tags/v$pkgver.tar.gz")
# Checksum will be updated after tagging; for development builds, use SKIP
sha256sums=('SKIP')

build() {
  cd "$pkgname-$pkgver"
  # Build the native PAM consent module
  cc -std=c11 -fPIC -shared -O2 -Wall -Wextra -Werror \
     -Wl,-z,relro,-z,now -o pam/facegate-consent.so \
     pam/facegate-consent.c -lpam
}

package() {
  cd "$pkgname-$pkgver"
  # User-session binaries
  install -Dm755 bin/facegate-hud "$pkgdir/usr/bin/facegate-hud"
  install -Dm755 bin/facegate-settings "$pkgdir/usr/bin/facegate-settings"
  install -Dm755 bin/facegate-tray "$pkgdir/usr/bin/facegate-tray"
  install -Dm755 bin/facegate-faces "$pkgdir/usr/lib/facegate/facegate-faces"

  # Python module
  install -Dm755 -d "$pkgdir/usr/lib/facegate/facegate"
  cp -r facegate/* "$pkgdir/usr/lib/facegate/facegate/"
  find "$pkgdir/usr/lib/facegate/facegate" -type d -name __pycache__ -prune -o -name '*.py' -print0 2>/dev/null | xargs -0 chmod 644
  chmod -R u=rwX,go=rX "$pkgdir/usr/lib/facegate"

  # Native consent module
  install -Dm755 pam/facegate-consent.so "$pkgdir/usr/lib/security/facegate/consent.so"

  # Scripts
  install -Dm755 scripts/refresh-pam "$pkgdir/usr/lib/facegate/refresh-pam"

  # Systemd user services
  install -Dm644 packaging/facegate.service "$pkgdir/usr/lib/systemd/user/facegate.service"
  install -Dm644 packaging/facegate-tray.service "$pkgdir/usr/lib/systemd/user/facegate-tray.service"

  # Desktop entry
  install -Dm644 packaging/org.facegate.Settings.desktop "$pkgdir/usr/share/applications/org.facegate.Settings.desktop"

  # Icon
  install -Dm644 packaging/facegate-symbolic.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/facegate-symbolic.svg"

  # Pacman hook for Gaze upgrades
  install -Dm644 packaging/facegate-gaze.hook "$pkgdir/etc/pacman.d/hooks/facegate-gaze.hook"

  # PAM setup scripts (run as root after install)
  install -Dm755 install.sh "$pkgdir/usr/share/facegate/install.sh"
  install -Dm755 uninstall.sh "$pkgdir/usr/share/facegate/uninstall.sh"
  install -Dm755 pam/facegate-consent.c "$pkgdir/usr/share/facegate/pam/facegate-consent.c"
}

post_install() {
  echo "FaceGate installed. Run the setup script to configure PAM and polkit:"
  echo "  sudo /usr/share/facegate/install.sh"
  echo "Or, if Gaze is already configured:"
  echo "  sudo /usr/share/facegate/setup.sh"
}