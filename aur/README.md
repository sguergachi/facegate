# AUR packaging

`PKGBUILD` builds from the GitHub tag `v$pkgver`. To publish:

```bash
git tag v0.1.0 && git push origin v0.1.0
cd aur && updpkgsums && makepkg --printsrcinfo > .SRCINFO
git clone ssh://aur@aur.archlinux.org/facegate.git /tmp/facegate-aur
cp PKGBUILD .SRCINFO facegate.install facegate*.hook /tmp/facegate-aur/
cd /tmp/facegate-aur && git add -A && git commit -m "facegate 0.1.0" && git push
```

The AUR needs a maintainer account with an SSH key, so the final push must be
done by the maintainer. The PKGBUILD installs under `/usr` (rewriting the
`/usr/local` paths used by `install.sh`) and `facegate.hook` re-applies the PAM
gate after Gaze upgrades.

## Website

`site/` is a static page. Enable it via GitHub Pages (Settings → Pages), or copy
`aur/pages.yml.example` to `.github/workflows/pages.yml` to deploy it on push.
