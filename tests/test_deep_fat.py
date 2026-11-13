#!/usr/bin/python3
"""Глубокий поиск FAT32/exFAT после быстрого форматирования. Root не нужен."""
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from diskplus.recovery import extract, fat_scan, index, volumes  # noqa: E402

TD = os.path.expanduser("~/.cache/diskplus-test")
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"   [{extra}]" if extra else ""))
    fails += 0 if cond else 1


for fs in ("fat32fmt", "exfatfmt"):
    print(f"\n=== {fs} ===")
    img = f"{TD}/fx-{fs}.img"
    man = json.load(open(f"{TD}/fx-{fs}.json"))
    v = volumes.list_volumes(img)[0]
    db = index.Index(tempfile.mktemp(suffix=".sqlite"))
    ok, err, info = fat_scan.deep_scan(img, v.offset, v.length, v.sector, db)
    check("глубокий поиск завершён", ok, err)
    print("info:", info)
    total, _ = db.counts()
    names = {n.name for n in db.search("")}
    top = [n.name for n in db.children(0)]
    print("корень:", top)
    # верхний уровень (корневой каталог после форматирования потерян) – ожидаем найти файлы во вложенных папках
    dst = tempfile.mkdtemp(prefix="dpfat-")
    r = extract.recover(img, v.offset, v.sector, db, [n.id for n in db.search("")], dst)
    hashes = {}
    for root, _d, files in os.walk(dst):
        for fn in files:
            hashes.setdefault(hashlib.sha256(open(os.path.join(root, fn), "rb").read()).hexdigest(), []).append(os.path.join(root, fn))
    good = [rel for rel, m in man.items() if m["sha256"] in hashes and m["size"] > 0]
    lost = [rel for rel, m in man.items() if m["sha256"] not in hashes and m["size"] > 0]
    print(f"восстановлено побитно (по контрольным суммам): {len(good)} из {sum(1 for m in man.values() if m['size'] > 0)}; не найдены: {lost}")
    check("найдено не меньше половины файлов", len(good) >= 5, str(lost))
    # файлы во вложенных папках (Documents/notes, Documents/old/sub...) должны сохранить имена
    nested = [n for n in db.search("") if n.path.count("/") >= 2]
    check("имена и пути вложенных файлов сохранены", len(nested) >= 3, str([n.path for n in db.search("")][:6]))
print()
print("ИТОГО:", "всё в порядке" if not fails else f"провалов: {fails}")
sys.exit(1 if fails else 0)
