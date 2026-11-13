#!/usr/bin/python3
"""Глубокий поиск NTFS на «быстро отформатированном» разделе (tools/make_fixtures.py ntfsfmt). Root не нужен."""
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from diskplus.recovery import extract, index, ntfs_scan, volumes  # noqa: E402

TD = os.path.expanduser("~/.cache/diskplus-test")
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"   [{extra}]" if extra else ""))
    fails += 0 if cond else 1


img = f"{TD}/fx-ntfsfmt.img"
man = json.load(open(f"{TD}/fx-ntfsfmt.json"))
vols = volumes.list_volumes(img)
v = vols[0]
check("том NTFS найден", v.fs == "ntfs", v.fs)

# обычное чтение: диск выглядит пустым
db0 = index.Index(tempfile.mktemp(suffix=".sqlite"))
index.scan_volume(img, v.offset, v.sector, db0)
total0, _ = db0.counts()
check("обычное чтение после форматирования почти ничего не видит", total0 <= 1, f"файлов: {total0}")

# глубокий поиск
db = index.Index(tempfile.mktemp(suffix=".sqlite"))
ok, err, info = ntfs_scan.deep_scan(img, v.offset, v.length, v.sector, db)
check("глубокий поиск завершён", ok, err)
print("info:", info)
total, deleted = db.counts()
check(f"найдено файлов не меньше {len(man) - 1}", total >= len(man) - 1, f"найдено {total}, ожидали {len(man)}")
root = [n.name for n in db.children(0)]
check("дерево восстановлено: в корне Documents, Photos, Music", {"Documents", "Photos", "Music"} <= set(root), str(root))
check("кириллическая папка «Работа» найдена", "Работа" in root, str(root))

dst = tempfile.mkdtemp(prefix="dpdeep-")
allfiles = [n.id for n in db.search("")]
r = extract.recover(img, v.offset, v.sector, db, allfiles, dst)
good, bad, missing = [], [], []
for rel, m in man.items():
    p = os.path.join(dst, rel)
    if not os.path.exists(p):
        missing.append(rel)
    elif hashlib.sha256(open(p, "rb").read()).hexdigest() == m["sha256"]:
        good.append(rel)
    else:
        bad.append(rel)
print(f"восстановлено побитно: {len(good)} из {len(man)}; повреждено: {bad}; не найдено: {missing}")
check("большинство файлов восстановлено побитно", len(good) >= len(man) - 2, f"{len(good)} из {len(man)}")
check("маленькие файлы (резидентные в MFT) и обычные совпадают", all(k in good for k in ("Documents/report.txt", "Photos/pic1.png", "empty.txt")), str(bad + missing))
check("файл с юникодным именем цел", "Работа/файл с пробелами и юникодом.txt" in good)
# потерянная таблица разделов: затираем GPT (начало и конец), поиск должен найти том по загрузочному сектору
import shutil  # noqa: E402
broken = tempfile.mktemp(suffix=".img")
shutil.copyfile(f"{TD}/fx-ntfs.img", broken)
with open(broken, "r+b") as bf:
    bf.write(b"\0" * (34 * 512))
    size = os.path.getsize(broken)
    bf.seek(size - 33 * 512)
    bf.write(b"\0" * (33 * 512))
check("после затирания GPT разделов в таблице нет", volumes.list_volumes(broken) == [])
lost = volumes.scan_lost(broken, os.path.getsize(broken), [], 512)
check("быстрый поиск находит потерянный NTFS-раздел", len(lost) == 1 and lost[0].fs == "ntfs" and lost[0].offset == 2048, str(lost))
dbl = index.Index(tempfile.mktemp(suffix=".sqlite"))
okl, _e = index.scan_volume(broken, lost[0].offset, 512, dbl) if lost else (False, "")
check("и файлы на нём читаются как обычно", okl and dbl.counts()[0] >= 6, str(dbl.counts()) if lost else "")
os.remove(broken)
print()
print("ИТОГО:", "всё в порядке" if not fails else f"провалов: {fails}")
sys.exit(1 if fails else 0)
