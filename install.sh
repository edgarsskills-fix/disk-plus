#!/usr/bin/env bash
# Установка Disk++ в систему (нужен sudo). Код помощника обязан принадлежать root.
#   ./install.sh            — установить / обновить
#   ./install.sh --remove   — удалить
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" -ne 0 ]; then exec sudo "$0" "$@"; fi

LIB=/usr/lib/disk-plus
if [ "${1:-}" = "--remove" ]; then
  rm -rf "${LIB:?}"
  rm -f /usr/bin/disk-plus /usr/share/polkit-1/actions/com.local.diskplus.policy \
        /usr/share/applications/com.local.DiskPlus.desktop \
        /usr/share/icons/hicolor/scalable/apps/com.local.DiskPlus.svg \
        /usr/share/icons/hicolor/symbolic/apps/com.local.DiskPlus-symbolic.svg \
        /usr/share/metainfo/com.local.DiskPlus.metainfo.xml
  gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor 2>/dev/null || true
  update-desktop-database -q /usr/share/applications 2>/dev/null || true
  echo "Disk++ удалён"; exit 0
fi

missing=()
for pkg in smartmontools python-gobject python-cairo python-numpy libadwaita polkit util-linux; do
  pacman -Q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "Не хватает пакетов: ${missing[*]}"
  echo "Установите: sudo pacman -S --needed ${missing[*]}"; exit 1
fi

rm -rf "${LIB:?}/diskplus"
install -d -m755 "$LIB"
cp -r "$here/diskplus" "$LIB/diskplus"
find "$LIB/diskplus" -name '__pycache__' -prune -exec rm -rf {} +
find "$LIB/diskplus" -type d -exec chmod 755 {} +
find "$LIB/diskplus" -type f -exec chmod 644 {} +
chown -R root:root "$LIB"
install -m755 -o root -g root "$here/lib/diskplus-helper" "$LIB/diskplus-helper"
install -m755 -o root -g root "$here/bin/disk-plus" /usr/bin/disk-plus
install -Dm644 "$here/data/com.local.diskplus.policy" /usr/share/polkit-1/actions/com.local.diskplus.policy
install -Dm644 "$here/data/com.local.DiskPlus.desktop" /usr/share/applications/com.local.DiskPlus.desktop
install -Dm644 "$here/data/com.local.DiskPlus.svg" /usr/share/icons/hicolor/scalable/apps/com.local.DiskPlus.svg
install -Dm644 "$here/data/com.local.DiskPlus-symbolic.svg" /usr/share/icons/hicolor/symbolic/apps/com.local.DiskPlus-symbolic.svg
install -Dm644 "$here/data/com.local.DiskPlus.metainfo.xml" /usr/share/metainfo/com.local.DiskPlus.metainfo.xml
gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true

# Необязательные движки следующих этапов
for pkg in ddrescue testdisk sleuthkit partclone ntfs-3g; do
  pacman -Q "$pkg" >/dev/null 2>&1 || echo "Подсказка: для следующих функций понадобится $pkg"
done
echo "Disk++ установлен: команда disk-plus и ярлык в меню приложений"
