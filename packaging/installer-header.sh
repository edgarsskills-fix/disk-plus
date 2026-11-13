#!/usr/bin/env bash
# ============================================================================
#  Disk++ @VER@ — установщик (один файл, внутри пакет и исходники)
#
#  Установить:   bash "Disk++-установщик.run"     (или двойной щелчок по ярлыку «Установить Disk++»)
#  Удалить:      bash "Disk++-установщик.run" --remove
#  Справка:      bash "Disk++-установщик.run" --help
#  (ключ --pause оставляет окно терминала открытым после установки)
#
#  Arch / CachyOS / Manjaro — ставится пакетом через pacman (зависимости подтянутся сами).
#  Debian / Ubuntu / Fedora — зависимости через apt / dnf (НЕ проверено на этих системах;
#  нужна библиотека libadwaita 1.5 или новее).
# ============================================================================
VER="@VER@"
PKG="disk-plus-$VER-1-any.pkg.tar.zst"
SRC="disk-plus-$VER.tar.gz"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
finish() {
  local rc=$1
  if [ "${DP_PAUSE:-}" = 1 ]; then read -r -p "Нажмите Enter, чтобы закрыть окно… " _ || true; fi
  exit "$rc"
}

case "${1:-}" in
  --help|-h)
    sed -n '2,13p' "$0" | sed 's/^# \{0,2\}//'; exit 0 ;;
  --pause) MODE=install; DP_PAUSE=1 ;;
  --remove|--uninstall) MODE=remove ;;
  --worker) ;;       # внутренний режим: уже от имени root
  *) MODE=install ;;
esac

# ---------------------------------------------------------------- распаковка полезной нагрузки
extract_payload() {
  local dest="$1" line
  line=$(awk '/^__DISKPLUS_ARCHIVE__$/ { print NR + 1; exit }' "$0")
  [ -n "$line" ] || { err "Повреждённый установщик: нет архива."; return 1; }
  tail -n +"$line" "$0" | tar -xz -C "$dest"
}

# ---------------------------------------------------------------- работа от имени root
worker() {
  local mode="$1" tmp="$2"
  . /etc/os-release 2>/dev/null || true
  local like=" ${ID:-} ${ID_LIKE:-} "
  local os=other
  case "$like" in *" arch "*|*" cachyos "*|*" manjaro "*) os=arch ;; esac
  case "$like" in *" debian "*|*" ubuntu "*) os=debian ;; esac
  case "$like" in *" fedora "*|*" rhel "*) os=fedora ;; esac

  remove_manual() {
    rm -rf /usr/lib/disk-plus
    rm -f /usr/bin/disk-plus /usr/share/polkit-1/actions/com.local.diskplus.policy \
          /usr/share/applications/com.local.DiskPlus.desktop \
          /usr/share/icons/hicolor/scalable/apps/com.local.DiskPlus.svg \
          /usr/share/metainfo/com.local.DiskPlus.metainfo.xml
  }
  refresh_caches() {
    gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor 2>/dev/null || true
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
  }

  if [ "$mode" = remove ]; then
    if [ "$os" = arch ] && pacman -Q disk-plus >/dev/null 2>&1; then
      bold "Удаляю пакет disk-plus…"; pacman -R --noconfirm disk-plus || return 1
    else
      bold "Удаляю файлы Disk++…"; remove_manual; refresh_caches
    fi
    bold "Disk++ удалён."; return 0
  fi

  case "$os" in
    arch)
      bold "Система: Arch-подобная. Устанавливаю пакет (зависимости подтянутся автоматически)…"
      # старая ручная установка (не из пакета) мешала бы pacman
      if [ -e /usr/bin/disk-plus ] && ! pacman -Qo /usr/bin/disk-plus >/dev/null 2>&1; then
        warn "Нашёл прежнюю установку без пакета, убираю её."; remove_manual
      fi
      pacman -U --noconfirm "$tmp/$PKG" || {
        err "pacman не смог поставить пакет. Если пишет «target not found», обновите систему: sudo pacman -Syu — и запустите установщик снова."
        return 1; }
      ;;
    debian)
      bold "Система: Debian/Ubuntu. Ставлю зависимости (apt)…"
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq || warn "apt update не удался, продолжаю"
      apt-get install -y python3 python3-gi python3-gi-cairo python3-numpy gir1.2-gtk-4.0 gir1.2-adw-1 \
        smartmontools util-linux gdisk parted gddrescue testdisk sleuthkit ntfs-3g partclone zstd acl e2fsprogs \
        exfatprogs dosfstools btrfs-progs xfsprogs || { err "Не удалось поставить зависимости."; return 1; }
      apt-get install -y policykit-1 2>/dev/null || apt-get install -y pkexec polkitd 2>/dev/null || warn "Не удалось поставить polkit: поставьте пакет с pkexec вручную."
      install_files "$tmp" ;;
    fedora)
      bold "Система: Fedora/RHEL. Ставлю зависимости (dnf)…"
      dnf install -y python3 python3-gobject python3-cairo python3-numpy gtk4 libadwaita smartmontools util-linux gdisk \
        parted ddrescue testdisk sleuthkit ntfs-3g partclone zstd acl e2fsprogs exfatprogs dosfstools btrfs-progs \
        xfsprogs polkit || { err "Не удалось поставить зависимости."; return 1; }
      install_files "$tmp" ;;
    *)
      warn "Неизвестная система: ставлю только файлы Disk++. Зависимости установите сами:"
      warn "python3 + PyGObject, GTK 4, libadwaita ≥1.5, smartmontools, gddrescue, testdisk, sleuthkit, ntfs-3g,"
      warn "partclone, zstd, gdisk, parted, acl, e2fsprogs, exfatprogs, dosfstools, btrfs-progs, xfsprogs, polkit."
      install_files "$tmp" ;;
  esac

  # проверка версии libadwaita для не-Arch систем
  if [ "$os" != arch ]; then
    python3 - <<'PY' || warn "ВНИМАНИЕ: не удалось проверить libadwaita. Disk++ нужна версия 1.5 или новее."
import gi
gi.require_version("Adw", "1")
from gi.repository import Adw
v = (Adw.get_major_version(), Adw.get_minor_version())
print("libadwaita", "%d.%d" % v)
raise SystemExit(0 if v >= (1, 5) else 1)
PY
  fi
  bold "Готово! Disk++ установлен: «Disk++» в меню приложений или команда disk-plus."
  echo "При первом использовании нажмите «Разрешить» и введите пароль администратора."
}

install_files() {
  local tmp="$1" d
  d=$(mktemp -d); tar -xzf "$tmp/$SRC" -C "$d"
  local root="$d/disk-plus-$VER"
  rm -rf /usr/lib/disk-plus; install -d -m755 /usr/lib/disk-plus
  cp -r "$root/diskplus" /usr/lib/disk-plus/diskplus
  find /usr/lib/disk-plus -type d -exec chmod 755 {} +; find /usr/lib/disk-plus -type f -exec chmod 644 {} +
  chown -R root:root /usr/lib/disk-plus
  install -m755 -o root -g root "$root/lib/diskplus-helper" /usr/lib/disk-plus/diskplus-helper
  install -m755 -o root -g root "$root/bin/disk-plus" /usr/bin/disk-plus
  install -Dm644 "$root/data/com.local.diskplus.policy" /usr/share/polkit-1/actions/com.local.diskplus.policy
  install -Dm644 "$root/data/com.local.DiskPlus.desktop" /usr/share/applications/com.local.DiskPlus.desktop
  install -Dm644 "$root/data/com.local.DiskPlus.svg" /usr/share/icons/hicolor/scalable/apps/com.local.DiskPlus.svg
  install -Dm644 "$root/data/com.local.DiskPlus.metainfo.xml" /usr/share/metainfo/com.local.DiskPlus.metainfo.xml
  rm -rf "$d"
  gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor 2>/dev/null || true
  update-desktop-database -q /usr/share/applications 2>/dev/null || true
}

# ---------------------------------------------------------------- внутренний вызов от root
if [ "${1:-}" = "--worker" ]; then
  tmp="$3"; worker "$2" "$tmp"; exit $?
fi

# ---------------------------------------------------------------- обычный запуск (от пользователя)
[ "$(uname -s)" = Linux ] || { err "Установщик работает только в Linux."; finish 1; }
bold "Disk++ $VER: установка"
TMP=$(mktemp -d /tmp/diskplus-inst.XXXXXX) || finish 1
trap 'rm -rf "$TMP"' EXIT
chmod 755 "$TMP"
extract_payload "$TMP" || finish 1
cp "$0" "$TMP/installer.sh"; chmod 755 "$TMP/installer.sh"

if [ "$(id -u)" -eq 0 ]; then
  bash "$TMP/installer.sh" --worker "$MODE" "$TMP"; rc=$?
elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v pkexec >/dev/null 2>&1; then
  echo "Понадобится пароль администратора."
  pkexec bash "$TMP/installer.sh" --worker "$MODE" "$TMP"; rc=$?
elif command -v sudo >/dev/null 2>&1; then
  echo "Понадобится пароль администратора (sudo)."
  sudo bash "$TMP/installer.sh" --worker "$MODE" "$TMP"; rc=$?
else
  err "Нужны права администратора (pkexec или sudo не найдены)."; rc=1
fi
[ "$rc" -eq 0 ] && { command -v notify-send >/dev/null 2>&1 && notify-send -i com.local.DiskPlus "Disk++" "Готово" 2>/dev/null; true; }
[ "$rc" -eq 0 ] || err "Установка не завершена (код $rc)."
finish "$rc"
__DISKPLUS_ARCHIVE__
