#!/usr/bin/python3
"""Проверка помощника на тестовом диске (tools/testdisk.sh up). Нужен sudo; реальные диски не читаются на скан.

  SUDO_ASKPASS=... python3 tests/test_helper.py
"""
import json
import os
import subprocess
import sys
import time

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "diskplus-helper")
DEV = "/dev/mapper/diskplus-test"
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    fails += 0 if cond else 1


class H:
    def __init__(self):
        cmd = ["sudo", "-A", HELPER] if os.environ.get("SUDO_ASKPASS") else ["sudo", "-n", HELPER]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.n = 0

    def send(self, cmd, **kw):
        self.n += 1
        self.p.stdin.write(json.dumps({"id": self.n, "cmd": cmd, **kw}) + "\n")
        self.p.stdin.flush()
        return self.n

    def read_until(self, rid, evs=("res", "err", "done"), limit=60):
        t = time.time()
        got = []
        for line in self.p.stdout:
            m = json.loads(line)
            if m.get("id") == rid:
                got.append(m)
                if m["ev"] in evs:
                    return got
            if time.time() - t > limit:
                break
        return got


h = H()
r = h.read_until(h.send("hello"))
check("hello от root", r and r[-1]["data"]["euid"] == 0)
for bad in ("/etc/passwd", "/dev/../etc/passwd", "/dev/sda; rm -rf /", "", None, "/dev/null", "/dev/mapper/../sda"):
    r = h.read_until(h.send("smart", dev=bad))
    check(f"отказ для {bad!r}", r and r[-1]["ev"] == "err")
r = h.read_until(h.send("rm", dev=DEV))
check("неизвестная команда отклонена", r and r[-1]["ev"] == "err")
r = h.read_until(h.send("scan", dev=DEV, block=1000))
check("кривой размер блока отклонён", r and r[-1]["ev"] == "err")

# полное сканирование: 128 КиБ блоки, ожидаем 1536 быстрых / 504 медленных (+30 мс) / 8 битых
r = h.read_until(h.send("scan", dev=DEV, block=131072, th=[5, 20, 50, 150, 500], cells=400))
d = r[-1]
check("скан завершился", d["ev"] == "done" and d["reason"] == "finished", str(d)[:200])
b = d.get("buckets", [])
check("быстрых 1536", b and b[0] == 1536, str(b))
check("медленных (20–50 мс) 504", b and b[2] == 504, str(b))
check("битых 8", b and b[6] == 8, str(b))
check("прочитано 256 МиБ", d.get("bytes") == 268435456)

# обратное направление даёт те же итоги
r = h.read_until(h.send("scan", dev=DEV, block=131072, th=[5, 20, 50, 150, 500], cells=400, reverse=True))
check("обратный скан: те же корзины", r[-1].get("buckets") == b, str(r[-1].get("buckets")))

# стоп посреди скана
sid = h.send("scan", dev=DEV, block=65536, th=[5, 20, 50, 150, 500], cells=400, start=100 * 1048576)
time.sleep(1.2)
h.send("pause", target=sid)
time.sleep(0.8)
h.send("resume", target=sid)
h.send("stop", target=sid)
r = h.read_until(sid)
check("стоп останавливает скан", r[-1]["ev"] == "done" and r[-1]["reason"] == "stopped", str(r[-1])[:200])
check("после стопа прочитано меньше диапазона", r[-1]["bytes"] < 156 * 1048576)

# ---- образ через ddrescue ----
TD = os.path.expanduser("~/.cache/diskplus-test")
os.makedirs(TD, exist_ok=True)
IMG, MAP = TD + "/out.img", TD + "/out.map"
for f in (IMG, MAP):
    if os.path.exists(f):
        subprocess.run(["sudo", "-A", "rm", "-f", f] if os.environ.get("SUDO_ASKPASS") else ["sudo", "-n", "rm", "-f", f])
for bad, why in (({"image": "/etc/evil.img", "mapfile": "/etc/evil.map"}, "системная папка"),
                 ({"image": "relative.img", "mapfile": "relative.map"}, "относительный путь"),
                 ({"image": TD + "/a.img", "mapfile": TD + "/b/a.map"}, "карта в другой папке"),
                 ({"image": TD + "/a.img", "mapfile": TD + "/a.txt"}, "карта не *.map")):
    r = h.read_until(h.send("image", dev=DEV, **bad))
    check(f"образ: отказ ({why})", r and r[-1]["ev"] == "err", str(r[-1:])[:200])
# тот же диск: системный NVMe -> образ в /var/tmp (он на нём же). Должен быть отказ ДО запуска ddrescue
r = h.read_until(h.send("image", dev="/dev/nvme1n1", image=TD + "/same.img", mapfile=TD + "/same.map"))
check("образ: отказ, если назначение на том же диске", r and r[-1]["ev"] == "err" and "тот же диск" in r[-1].get("msg", ""),
      str(r[-1:])[:200])
check("образ: ddrescue при отказе не стартовал", not os.path.exists(TD + "/same.img"))

r = h.read_until(h.send("image", dev=DEV, image=IMG, mapfile=MAP, cells=300), limit=170)
d = r[-1]
check("образ: завершён", d["ev"] == "done" and d["reason"] == "finished", str(d)[:300])
sz = d.get("sizes", {})
check("образ: спасено 255 МиБ", sz.get("+") == 255 * 1048576, str(sz))
check("образ: плохих 1 МиБ", sz.get("-") == 1048576, str(sz))
prog = [m for m in r if m["ev"] == "progress"]
check("образ: приходил прогресс с картой", bool(prog) and len(prog[-1].get("map", "")) > 50)
st = os.stat(IMG)
check("образ: файл отдан пользователю (не root)", st.st_uid != 0, f"uid={st.st_uid}")
cmpr = subprocess.run(["sudo", "-A" if os.environ.get("SUDO_ASKPASS") else "-n", "cmp", "-n", str(96 * 1048576), IMG, DEV],
                      capture_output=True)
check("образ: первые 96 МиБ совпадают с источником", cmpr.returncode == 0, cmpr.stderr.decode()[:100])
# повторный запуск = продолжение (образ и карта уже есть)
r = h.read_until(h.send("image", dev=DEV, image=IMG, mapfile=MAP, cells=300), limit=170)
check("образ: повторный запуск продолжает сеанс", any(m.get("resume") for m in r if m["ev"] == "start") and r[-1]["ev"] == "done")
for f in (IMG, MAP):
    subprocess.run(["rm", "-f", f])

# ---- разбор карты ddrescue: в строке состояния бывают символы кроме ?*/-+ (например % для Sweeping) ----
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402
_ld = importlib.machinery.SourceFileLoader("dph", HELPER)
_sp = importlib.util.spec_from_loader("dph", _ld)
_hm = importlib.util.module_from_spec(_sp)
_ld.exec_module(_hm)
_tmpmap = os.path.join(os.path.expanduser("~/.cache/diskplus-test"), "parse.map")
open(_tmpmap, "w").write("# Mapfile\n# current_pos  current_status  current_pass\n0x7AF60000     %               1\n"
                         "#      pos        size  status\n0x00000000  0x7AD50000  +\n0x7AD50000  0x00000200  -\n0x7AD50200  0x0020FE00  *\n")
_e, _c = _hm.read_mapfile(_tmpmap)
os.remove(_tmpmap)
check("карта ddrescue с символом % в строке состояния читается", _c == (0x7AF60000, "%", 1) and len(_e) == 3, str((_c, _e)))

# ---- выдача чтения для восстановления ----
r = h.read_until(h.send("grant_read", dev=DEV))
check("grant_read: выдан", r and r[-1]["ev"] == "res", str(r[-1:])[:200])
real = os.path.realpath(DEV)
check("grant_read: пользователь может читать устройство", os.access(real, os.R_OK))
r = h.read_until(h.send("grant_read", dev="/etc/passwd"))
check("grant_read: отказ для не-устройства", r and r[-1]["ev"] == "err")
r = h.read_until(h.send("revoke_read", dev=DEV))
check("revoke_read: снято", r and r[-1]["ev"] == "res" and not os.access(real, os.R_OK))

h.p.stdin.close()
h.p.wait(timeout=30)
print("\nИТОГО:", "всё в порядке" if not fails else f"провалов: {fails}")
sys.exit(1 if fails else 0)
