#!/usr/bin/env bash
# Тестовый «больной» диск для Disk++ (только виртуальный, реальные диски не трогает).
#   testdisk.sh up    — создать /dev/mapper/diskplus-test (256 МиБ):
#                       0–96 МиБ норма, 96–97 битые (EIO), 97–160 медленные (+30 мс), 160–256 норма
#   testdisk.sh down  — убрать
# Нужен root (sudo).
set -euo pipefail
IMG="${DISKPLUS_TEST_IMG:-/var/tmp/diskplus-test.img}"
NAME=diskplus-test
MB=2048   # секторов в МиБ
case "${1:-}" in
up)
  [ -e /dev/mapper/$NAME ] && { echo "уже создан: /dev/mapper/$NAME"; exit 0; }
  truncate -s 256M "$IMG"
  LOOP=$(losetup --find --show "$IMG")
  dmsetup create $NAME <<TBL
0 $((96*MB)) linear $LOOP 0
$((96*MB)) $((1*MB)) error
$((97*MB)) $((63*MB)) delay $LOOP $((97*MB)) 30
$((160*MB)) $((96*MB)) linear $LOOP $((160*MB))
TBL
  echo "$LOOP" > "$IMG.loop"
  echo "готово: /dev/mapper/$NAME  (loop: $LOOP)"
  ;;
down)
  dmsetup remove $NAME 2>/dev/null || true
  [ -f "$IMG.loop" ] && losetup -d "$(cat "$IMG.loop")" 2>/dev/null || true
  rm -f "$IMG" "$IMG.loop"
  echo "убрано"
  ;;
*) echo "использование: $0 up|down"; exit 1;;
esac
