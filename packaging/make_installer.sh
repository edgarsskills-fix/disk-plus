#!/usr/bin/env bash
# make_installer.sh <версия> <пакет.pkg.tar.zst> <исходники.tar.gz> <выход.run>
set -euo pipefail
ver="$1"; pkg="$2"; srctar="$3"; out="$4"
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"; trap 'rm -rf "${work:?}"' EXIT
mkdir "$work/payload"
cp "$pkg" "$work/payload/"; cp "$srctar" "$work/payload/"
tar -C "$work/payload" -czf "$work/payload.tgz" .
sed "s/@VER@/$ver/g" "$here/installer-header.sh" > "$out"
cat "$work/payload.tgz" >> "$out"
chmod +x "$out"
bash -n "$here/installer-header.sh" || { echo "ошибка синтаксиса в установщике" >&2; exit 1; }
