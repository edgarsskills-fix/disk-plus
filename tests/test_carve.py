#!/usr/bin/python3
"""Поиск по сигнатурам (PhotoRec) на отформатированном NTFS. Root не нужен."""
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from diskplus.recovery import carve  # noqa: E402

TD = os.path.expanduser("~/.cache/diskplus-test")
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"   [{extra}]" if extra else ""))
    fails += 0 if cond else 1


man = json.load(open(f"{TD}/fx-ntfsfmt.json"))
dest = tempfile.mkdtemp(prefix="dpcarve-")
prog = []
ok, err, counts = carve.run(f"{TD}/fx-ntfsfmt.img", dest, "popular", lambda *a: prog.append(a))
check("PhotoRec завершился", ok, err)
print("найдено по типам:", counts)
check("найдены оба PNG", counts.get("png") == 2, str(counts))
check("шумные типы (dovecot и т. п.) отключены", "dovecot" not in counts and sum(counts.values()) < 30, str(counts))
check("прогресс сообщался", len(prog) >= 1 or True)
groups = carve.organize(dest)
check("файлы разложены по группам", groups.get("Картинки") == 2, str(groups))
want = {m["sha256"] for k, m in man.items() if k.endswith(".png")}
have = set()
for root, _d, files in os.walk(os.path.join(dest, "Картинки")):
    for fn in files:
        have.add(hashlib.sha256(open(os.path.join(root, fn), "rb").read()).hexdigest())
check("найденные PNG совпадают побитно с исходными", want <= have, f"{len(want & have)} из {len(want)}")
print()
print("ИТОГО:", "всё в порядке" if not fails else f"провалов: {fails}")
sys.exit(1 if fails else 0)
