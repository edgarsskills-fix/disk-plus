#!/usr/bin/python3
"""Проверка движка восстановления на тестовых образах (tools/make_fixtures.py). Права root не нужны."""
import hashlib
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from diskplus.recovery import extract, index, volumes  # noqa: E402

TD = os.path.expanduser("~/.cache/diskplus-test")
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))
    fails += 0 if cond else 1


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


for fs, want_deleted in (("ntfs", True), ("fat32", True), ("exfat", False), ("ext4", False)):
    img = f"{TD}/fx-{fs}.img"
    if not os.path.exists(img):
        print("нет образа", img, "(запустите tools/make_fixtures.py)")
        fails += 1
        continue
    man = json.load(open(f"{TD}/fx-{fs}.json"))
    print(f"\n=== {fs} ===")
    vols = volumes.list_volumes(img)
    check(f"{fs}: найден 1 том из таблицы GPT", len(vols) == 1 and vols[0].source == "table", str(vols))
    v = vols[0]
    check(f"{fs}: ФС определена как {fs}", v.fs == fs, v.fs)
    check(f"{fs}: размер тома > 100 МБ", v.size_bytes > 100e6, str(v.size_bytes))
    db = index.Index(tempfile.mktemp(suffix=".sqlite"))
    ok, err = index.scan_volume(img, v.offset, v.sector, db)
    check(f"{fs}: сканирование успешно", ok, err)
    total, deleted = db.counts()
    root = [n.name for n in db.children(0)]
    check(f"{fs}: в корне есть Documents и Photos", "Documents" in root and "Photos" in root, str(root))
    check(f"{fs}: служебные $-файлы скрыты", not any(n.startswith("$") for n in root if n != "$OrphanFiles"), str(root))
    dst = tempfile.mkdtemp(prefix="dprec-")
    # 1) обычные файлы: восстановить всё, что не удалялось
    live = {k: m for k, m in man.items() if not m["deleted"]}
    ids = [n.id for n in db.search("") if n.path in live]
    check(f"{fs}: найдены все {len(live)} живых файлов", len(ids) == len(live), f"{len(ids)} из {len(live)}")
    r = extract.recover(img, v.offset, v.sector, db, ids, dst)
    check(f"{fs}: живые файлы извлечены без ошибок", not r.failed and r.ok == len(live), str(r.failed[:2]))
    bad = [k for k, m in live.items() if not os.path.exists(os.path.join(dst, k)) or sha(os.path.join(dst, k)) != m["sha256"]]
    check(f"{fs}: контрольные суммы живых файлов совпадают", not bad, str(bad))
    # 2) удалённые файлы
    dels = {k: m for k, m in man.items() if m["deleted"]}
    found = [n for n in db.search("", deleted_only=True) if n.path in dels]
    if want_deleted:
        check(f"{fs}: все {len(dels)} удалённых файлов найдены с именами", len(found) == len(dels), f"{len(found)} из {len(dels)}")
        dst2 = tempfile.mkdtemp(prefix="dprec-")
        r2 = extract.recover(img, v.offset, v.sector, db, [n.id for n in found], dst2)
        bad = [k for k, m in dels.items() if not os.path.exists(os.path.join(dst2, k)) or sha(os.path.join(dst2, k)) != m["sha256"]]
        check(f"{fs}: удалённые файлы восстановлены побитно", not bad, str(bad))
    else:
        print(f"info {fs}: удалённых найдено {len(found)} из {len(dels)} (ограничение Sleuth Kit для этой ФС)")
    # 3) восстановление папки целиком
    docs = next(n for n in db.children(0) if n.name == "Documents")
    dst3 = tempfile.mkdtemp(prefix="dprec-")
    r3 = extract.recover(img, v.offset, v.sector, db, [docs.id], dst3)
    check(f"{fs}: папка Documents восстановлена целиком", os.path.exists(os.path.join(dst3, "Documents/report.txt")) and os.path.isdir(os.path.join(dst3, "Documents/notes")))
    # 4) поиск и юникод
    hit = db.search("юникод")
    check(f"{fs}: поиск по юникоду находит файл", len(hit) == 1 and hit[0].name.endswith(".txt"), str(hit))

# защита от выхода за пределы папки назначения
check("безопасные пути: .. отбрасывается", extract.safe_parts("../../etc/passwd") == ["etc", "passwd"])
# том без таблицы разделов: ФС прямо в файле
print()
print("ИТОГО:", "всё в порядке" if not fails else f"провалов: {fails}")
sys.exit(1 if fails else 0)
