#!/usr/bin/env bash
# Сборка: dist/disk-plus-<ver>-1-any.pkg.tar.zst и dist/Disk++-установщик.run
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
ver="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$here/diskplus/__init__.py")"
dist="$here/dist"; mkdir -p "$dist"
work="$(mktemp -d)"; trap 'rm -rf "${work:?}"' EXIT
src="$work/disk-plus-$ver"; mkdir -p "$src"
cp -r "$here/diskplus" "$here/lib" "$here/bin" "$here/data" "$here/README.md" "$here/LICENSE" "$src/"
find "$src" -name '__pycache__' -prune -exec rm -rf {} +
tar -C "$work" -czf "$work/disk-plus-$ver.tar.gz" "disk-plus-$ver"
mkdir -p "$work/pkg"; cp "$here/packaging/arch/PKGBUILD" "$here/packaging/arch/disk-plus.install" "$work/pkg/"
cp "$work/disk-plus-$ver.tar.gz" "$work/pkg/"
( cd "$work/pkg" && sed -i "s/^pkgver=.*/pkgver=$ver/" PKGBUILD && makepkg -f --nodeps --skipinteg --nocolor >/dev/null )
pkg="$(ls "$work"/pkg/disk-plus-"$ver"-1-any.pkg.tar.zst)"
cp "$pkg" "$dist/"
bash "$here/packaging/make_installer.sh" "$ver" "$dist/$(basename "$pkg")" "$work/disk-plus-$ver.tar.gz" "$dist/Disk++-установщик.run"
echo "Готово:"; ls -la "$dist"
