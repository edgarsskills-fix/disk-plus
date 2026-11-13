#!/usr/bin/python3
"""Уменьшение разделов при клонировании и восстановлении (ТОЛЬКО loop-устройства).
Источник 300 МБ (FAT 64 + NTFS 100 + ext4 100) -> диск назначения 200 МБ.
  SUDO_ASKPASS=... python3 tests/test_shrink.py"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
HELPER = os.path.join(ROOT, "lib", "diskplus-helper")
WORK = os.path.expanduser("~/.cache/diskplus-test/shrink")
STAGE = os.path.expanduser("~/.cache/diskplus-test/stage")
SUDO = ["sudo", "-A" if os.environ.get("SUDO_ASKPASS") else "-n"]
fails = 0
loops = []


def check(name, cond, extra=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))
    fails += 0 if cond else 1


def sudo(*cmd, **kw):
    return subprocess.run(SUDO + list(cmd), capture_output=True, text=True, **kw)


class H:
    def __init__(self):
        self.p = subprocess.Popen(SUDO + [HELPER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.n = 0

    def call(self, cmd, limit=400, **kw):
        self.n += 1
        rid = self.n
        self.p.stdin.write(json.dumps({"id": rid, "cmd": cmd, **kw}) + "\n")
        self.p.stdin.flush()
        evs, t = [], time.time()
        for line in self.p.stdout:
            m = json.loads(line)
            if m.get("id") != rid:
                continue
            evs.append(m)
            if m["ev"] in ("res", "err", "done"):
                return evs
            if time.time() - t > limit:
                break
        return evs


def attach(img):
    dev = sudo("losetup", "-fP", "--show", img).stdout.strip()
    loops.append(dev)
    sudo("partprobe", dev)
    sudo("udevadm", "settle")
    return dev


def mount_sums(node, fs):
    mnt = tempfile.mkdtemp(prefix="dpsh-")
    sudo("mount", *(["-t", "ntfs-3g"] if fs == "ntfs" else []), "-o", "ro", node, mnt)
    try:
        out = {}
        for dp, _d, fl in os.walk(mnt):
            for f in fl:
                p = os.path.join(dp, f)
                out[os.path.relpath(p, mnt)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
        size = int(subprocess.run(["df", "-B1", "--output=size", mnt], capture_output=True, text=True).stdout.split()[1])
        return {k: v for k, v in out.items() if "System Volume" not in k and "lost+found" not in k}, size
    finally:
        sudo("umount", mnt)
        os.rmdir(mnt)


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(8 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


os.makedirs(WORK, exist_ok=True)
shutil.rmtree(STAGE, ignore_errors=True)
SRC_IMG, DST_IMG, DST2_IMG = (os.path.join(WORK, n) for n in ("src.img", "dst.img", "dst2.img"))
BK = os.path.join(WORK, "bk")
shutil.rmtree(BK, ignore_errors=True)
try:
    for f, sz in ((SRC_IMG, "300M"), (DST_IMG, "200M"), (DST2_IMG, "200M")):
        subprocess.run(["truncate", "-s", sz, f], check=True)
    S = attach(SRC_IMG)
    sudo("sfdisk", "-q", S, input='label: gpt\nstart=2048, size=131072, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI"\n'
         'start=133120, size=204800, type=EBD0A0A2-B9E5-4433-87C7-68D4B1F8A4D3, name="Windows"\n'
         'start=337920, size=204800, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="Linux root"\n')
    sudo("partprobe", S)
    sudo("udevadm", "settle")
    time.sleep(1)
    expect = {}
    for i, (mk, fs) in enumerate((("mkfs.vfat -F32 -n EFI", "vfat"), ("mkfs.ntfs -Q -L WIN", "ntfs"), ("mkfs.ext4 -q -L ROOT", "ext4")), 1):
        sudo(*mk.split(), f"{S}p{i}")
        mnt = tempfile.mkdtemp(prefix="dpts-")
        sudo("mount", *(["-t", "ntfs-3g"] if fs == "ntfs" else []), f"{S}p{i}", mnt)
        sudo("sh", "-c", f"echo 'файл {i}' > {mnt}/hello{i}.txt; head -c 4000000 /dev/urandom > {mnt}/rand{i}.bin; mkdir -p {mnt}/d/s; echo deep > {mnt}/d/s/x.txt; chmod -R a+rX {mnt} 2>/dev/null; true")
        sudo("umount", mnt)
        os.rmdir(mnt)
    for i, fs in ((1, "vfat"), (2, "ntfs"), (3, "ext4")):
        expect[i] = (fs, mount_sums(f"{S}p{i}", fs)[0])
    src_before = sha_file(SRC_IMG)
    check("источник подготовлен", all(len(v[1]) >= 3 for v in expect.values()))
    h = H()
    D = attach(DST_IMG)
    P1, P2, P3 = 131072, 102400, 122880        # сектора: EFI 64 МБ, NTFS 50 МБ (было 100), ext4 60 МБ (было 100)
    plan = [{"src": f"{S}p1", "start": 2048, "size": P1},
            {"src": f"{S}p2", "start": 2048 + P1, "size": P2},
            {"src": f"{S}p3", "start": 2048 + P1 + P2, "size": P3}]
    base = {"src": S, "dst": D, "new_ids": False, "verify": True, "parts": plan, "staging": STAGE}

    print("\n-- минимальные размеры")
    r = h.call("shrink_info", src=S)[-1]
    info = {p["node"]: p for p in r["data"]["parts"]} if r["ev"] == "res" else {}
    check("NTFS: минимум найден и меньше 100 МБ", info.get(f"{S}p2", {}).get("min") and info[f"{S}p2"]["min"] < 204800, str(info.get(f"{S}p2")))
    check("ext4: минимум найден и меньше 100 МБ", info.get(f"{S}p3", {}).get("min") and info[f"{S}p3"]["min"] < 204800, str(info.get(f"{S}p3")))
    check("FAT: уменьшить нельзя (с пояснением)", info.get(f"{S}p1", {}).get("min") is None and info[f"{S}p1"]["reason"])
    check("по умолчанию временная папка определена", bool(r["data"].get("staging")), str(r["data"].get("staging")))

    print("\n-- проверка плана")
    r = h.call("clone_check", **{**base, "parts": [plan[0], {**plan[1], "size": 2048 * 5}, plan[2]]})
    check("слишком маленький NTFS отклонён", r[-1]["ev"] == "res" and any("слишком мал" in p for p in r[-1]["data"]["problems"]), str(r[-1])[:200])
    r = h.call("clone_check", **{**base, "parts": [{**plan[0], "size": 2048 * 40}, plan[1], plan[2]]})
    check("уменьшение FAT отклонено", r[-1]["ev"] == "res" and any("уменьшить нельзя" in p for p in r[-1]["data"]["problems"]), str(r[-1])[:200])
    r = h.call("clone_check", **base)
    d = r[-1].get("data", {})
    check("нормальный план принят", r[-1]["ev"] == "res" and not d.get("problems"), str(d.get("problems") or r[-1])[:200])
    check("в плане 2 раздела на уменьшение", sum(1 for p in d.get("plan", []) if p["shrink"]) == 2)
    check("шаги: временный файл, уменьшение, запись", any("во временный файл" in s for s in d.get("steps", [])) and any("уменьшение файловой системы" in s for s in d.get("steps", [])))
    r = h.call("clone_check", **{**base, "staging": D})
    check("временная папка вне разрешённых мест отклонена", r[-1]["ev"] == "res" and bool(r[-1]["data"]["problems"]))
    r = h.call("clone_check", **{**base, "staging": "/tmp/dpstage"})
    check("/tmp как временная папка отклонён", r[-1]["ev"] == "res" and bool(r[-1]["data"]["problems"]))

    print("\n-- клонирование с уменьшением")
    ev = h.call("clone", limit=400, **{**base, "confirm": os.path.basename(D)})
    d = ev[-1]
    check("клонирование завершено", d["ev"] == "done" and d["reason"] == "finished", str(d)[:300])
    check("проверка целостности без замечаний", not [w for w in d.get("warnings", []) if "проверка" in w], str(d.get("warnings"))[:200])
    sudo("partprobe", D)
    sudo("udevadm", "settle")
    for i, (fs, want) in ((1, expect[1]), (2, expect[2]), (3, expect[3])):
        got, fssize = mount_sums(f"{D}p{i}", fs)
        check(f"раздел {i} ({fs}): файлы совпадают побитно", got == want, f"{len(got)} vs {len(want)}")
        if i == 2:
            check("NTFS уменьшен (≈50 МБ вместо 100)", 40e6 < fssize < 55e6, str(fssize))
        if i == 3:
            check("ext4 уменьшен (≈60 МБ вместо 100)", 45e6 < fssize < 65e6, str(fssize))
    nf = sudo("ntfsfix", "-n", f"{D}p2").stdout
    check("NTFS: резервный загрузочный сектор на месте", "alternate boot sector... OK" in nf, nf[-200:])
    ef = sudo("e2fsck", "-fn", f"{D}p3")
    check("ext4: fsck чистый", ef.returncode == 0, ef.stdout[-200:])
    check("источник не изменился (хеш образа тот же)", sha_file(SRC_IMG) == src_before)
    check("временные файлы удалены", not any(os.scandir(STAGE)) if os.path.isdir(STAGE) else True, str(list(os.scandir(STAGE)))[:100])

    print("\n-- бэкап и восстановление с уменьшением")
    ev = h.call("backup", limit=400, src=S, dest=BK, parts=[f"{S}p1", f"{S}p2", f"{S}p3"], level=3, verify=True)
    check("копия создана", ev[-1]["ev"] == "done" and ev[-1]["reason"] == "finished", str(ev[-1])[:200])
    man = json.load(open(os.path.join(BK, "manifest.json")))
    check("в манифесте есть минимальные размеры NTFS и ext4", man["partitions"][1]["min_bytes"] > 0 and man["partitions"][2]["min_bytes"] > 0, str([p["min_bytes"] for p in man["partitions"]]))
    D2 = attach(DST2_IMG)
    rplan = [{"index": 1, "start": 2048, "size": P1}, {"index": 2, "start": 2048 + P1, "size": P2}, {"index": 3, "start": 2048 + P1 + P2, "size": P3}]
    rbase = {"folder": BK, "dst": D2, "new_ids": False, "verify": True, "parts": rplan, "staging": STAGE}
    r = h.call("restore_check", **rbase)
    check("восстановление с уменьшением принято", r[-1]["ev"] == "res" and not r[-1]["data"]["problems"], str(r[-1].get("data", {}).get("problems") or r[-1])[:200])
    ev = h.call("restore", limit=400, **{**rbase, "confirm": os.path.basename(D2)})
    check("восстановление завершено", ev[-1]["ev"] == "done" and ev[-1]["reason"] == "finished", str(ev[-1])[:300])
    sudo("partprobe", D2)
    sudo("udevadm", "settle")
    for i, (fs, want) in ((1, expect[1]), (2, expect[2]), (3, expect[3])):
        got, _ = mount_sums(f"{D2}p{i}", fs)
        check(f"восстановленный раздел {i} ({fs}): файлы совпадают побитно", got == want, f"{len(got)} vs {len(want)}")
    h.p.stdin.close()
    h.p.wait(timeout=30)
finally:
    for dev in loops:
        sudo("umount", "-R", dev)
        sudo("losetup", "-d", dev)
    for f in (SRC_IMG, DST_IMG, DST2_IMG):
        if os.path.exists(f):
            os.remove(f)
    shutil.rmtree(BK, ignore_errors=True)
    shutil.rmtree(STAGE, ignore_errors=True)
print()
print("ИТОГО:", "всё в порядке" if not fails else f"провалов: {fails}")
sys.exit(1 if fails else 0)
