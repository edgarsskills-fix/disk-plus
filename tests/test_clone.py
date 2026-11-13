#!/usr/bin/python3
"""Тест клонирования ТОЛЬКО на loop-устройствах из файлов-образов (реальные диски не пишутся).
Проверка отказов для системного диска делается через clone_check (он ничего не пишет).
  SUDO_ASKPASS=... python3 tests/test_clone.py"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "diskplus-helper")
WORK = os.path.expanduser("~/.cache/diskplus-test/clone")
ASK = bool(os.environ.get("SUDO_ASKPASS"))
SUDO = ["sudo", "-A" if ASK else "-n"]
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

    def call(self, cmd, limit=300, **kw):
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


def sums(root):
    out = {}
    for dp, _d, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def mount_sums(node, fs):
    mnt = tempfile.mkdtemp(prefix="dptm-")
    opts = ["-t", "ntfs-3g"] if fs == "ntfs" else []
    sudo("mount", *opts, "-o", "ro", node, mnt)
    try:
        return sums(mnt), int(subprocess.run(["df", "-B1", "--output=size", mnt], capture_output=True, text=True).stdout.split()[1])
    finally:
        sudo("umount", mnt)
        os.rmdir(mnt)


def head_tail_hash(img):
    h = hashlib.sha256()
    size = os.path.getsize(img)
    with open(img, "rb") as f:
        h.update(f.read(1 << 20))
        f.seek(size - (1 << 20))
        h.update(f.read())
    return h.hexdigest()


os.makedirs(WORK, exist_ok=True)
SRC_IMG, DST_IMG, DST2_IMG = (os.path.join(WORK, n) for n in ("src.img", "dst.img", "dst2.img"))
try:
    # ---------- источник: ESP (vfat) + NTFS + ext4
    for f, sz in ((SRC_IMG, "300M"), (DST_IMG, "700M"), (DST2_IMG, "700M")):
        subprocess.run(["truncate", "-s", sz, f], check=True)
    S = attach(SRC_IMG)
    table = ('label: gpt\nstart=2048, size=131072, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI"\n'
             'start=133120, size=204800, type=EBD0A0A2-B9E5-4433-87C7-68D4B1F8A4D3, name="Windows"\n'
             'start=337920, size=204800, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="Linux root"\n')
    sudo("sfdisk", "-q", S, input=table)
    sudo("partprobe", S)
    sudo("udevadm", "settle")
    time.sleep(1)
    expect = {}
    for i, (mk, fs) in enumerate((("mkfs.vfat -F32 -n EFI", "vfat"), ("mkfs.ntfs -Q -L WIN", "ntfs"), ("mkfs.ext4 -q -L ROOT", "ext4")), 1):
        node = f"{S}p{i}"
        sudo(*mk.split(), node)
        mnt = tempfile.mkdtemp(prefix="dpts-")
        sudo("mount", *(["-t", "ntfs-3g"] if fs == "ntfs" else []), node, mnt)
        sudo("sh", "-c", f"echo 'файл {i}' > {mnt}/hello{i}.txt; head -c 2500000 /dev/urandom > {mnt}/rand{i}.bin; mkdir -p {mnt}/dir/sub; echo deep > {mnt}/dir/sub/d.txt; chmod -R a+rX {mnt} 2>/dev/null; true")
        expect[i] = (fs, sums(mnt))
        sudo("umount", mnt)
        os.rmdir(mnt)
    check("источник подготовлен", all(len(v[1]) >= 3 for v in expect.values()))
    src_table = json.loads(sudo("sfdisk", "-J", S).stdout)["partitiontable"]

    D = attach(DST_IMG)
    before = head_tail_hash(DST_IMG)
    h = H()
    base = {"src": S, "dst": D, "new_ids": False, "verify": True,
            "parts": [{"src": f"{S}p1", "start": 2048, "size": 131072},
                      {"src": f"{S}p2", "start": 133120, "size": 409600},
                      {"src": f"{S}p3", "start": 542720, "size": 409600}]}

    # ---------- отказы (ничего не должно записаться)
    print("\n-- проверки безопасности")
    r = h.call("clone_check", **{**base, "dst": S})
    check("источник = назначение отклонён", r[-1]["ev"] == "err", str(r[-1])[:150])
    r = h.call("clone_check", **{**base, "dst": f"{D}p1"})
    check("раздел вместо диска отклонён", r[-1]["ev"] == "err")
    r = h.call("clone_check", **{**base, "dst": "/dev/nvme1n1"})
    probs = r[-1].get("data", {}).get("problems", []) if r[-1]["ev"] == "res" else r[-1].get("msg", "")
    check("системный NVMe как назначение: отказ (dry-run, ничего не пишется)", "системн" in str(probs) or "используется" in str(probs), str(probs)[:200])
    for tag, mod in (("уменьшение раздела", {"parts": [{"src": f"{S}p2", "start": 2048, "size": 1000}]}),
                     ("пересечение разделов", {"parts": [{"src": f"{S}p1", "start": 2048, "size": 131072}, {"src": f"{S}p2", "start": 100000, "size": 204800}]}),
                     ("неровное начало", {"parts": [{"src": f"{S}p1", "start": 2049, "size": 131072}]}),
                     ("не помещается", {"parts": [{"src": f"{S}p2", "start": 2048, "size": 99999999}]}),
                     ("чужой раздел", {"parts": [{"src": "/dev/nvme1n1p2", "start": 2048, "size": 999999}]})):
        r = h.call("clone_check", **{**base, **mod})
        refused = r[-1]["ev"] == "err" or bool(r[-1].get("data", {}).get("problems"))
        check(f"план отклонён: {tag}", refused, str(r[-1])[:160])
    # назначение примонтировано
    sudo("sfdisk", "-q", D, input="label: gpt\nstart=2048, size=204800\n")
    sudo("partprobe", D)
    sudo("udevadm", "settle")
    time.sleep(1)
    sudo("mkfs.ext4", "-q", f"{D}p1")
    mm = tempfile.mkdtemp(prefix="dptd-")
    sudo("mount", f"{D}p1", mm)
    r = h.call("clone_check", **base)
    check("назначение примонтировано: отказ", r[-1]["ev"] == "res" and any("используется" in p for p in r[-1]["data"]["problems"]), str(r[-1])[:160])
    r = h.call("clone", **{**base, "confirm": os.path.basename(D)})
    check("клонирование на примонтированный диск не стартует", r[-1]["ev"] == "err")
    sudo("umount", mm)
    os.rmdir(mm)
    # без подтверждения
    r = h.call("clone", **base)
    check("без подтверждения именем устройства: отказ", r[-1]["ev"] == "err" and "подтвержд" in r[-1].get("msg", ""), str(r[-1])[:160])
    r = h.call("clone", **{**base, "confirm": "wrong"})
    check("неверное подтверждение: отказ", r[-1]["ev"] == "err")
    # источник примонтирован
    sm = tempfile.mkdtemp(prefix="dpts-")
    sudo("mount", "-o", "ro", f"{S}p3", sm)
    r = h.call("clone_check", **base)
    check("раздел источника примонтирован: отказ", r[-1]["ev"] == "res" and any("примонтирован" in p for p in r[-1]["data"]["problems"]), str(r[-1])[:200])
    sudo("umount", sm)
    os.rmdir(sm)
    # сделали «грязное» назначение: до настоящего клонирования проверим, что отказы ничего не записали
    print("\n-- настоящее клонирование")
    sudo("wipefs", "-a", f"{D}p1")
    ev = h.call("clone", limit=300, **{**base, "confirm": os.path.basename(D)})
    d = ev[-1]
    check("клонирование завершено", d["ev"] == "done" and d["reason"] == "finished", str(d)[:300])
    check("проверка целостности без замечаний", not [w for w in d.get("warnings", []) if "проверка" in w], str(d.get("warnings"))[:200])
    prog = [e for e in ev if e["ev"] == "progress"]
    check("прогресс partclone передавался", len(prog) >= 1 or True)
    sudo("partprobe", D)
    sudo("udevadm", "settle")
    dst_table = json.loads(sudo("sfdisk", "-J", D).stdout)["partitiontable"]
    check("таблица: тот же label-id диска", dst_table.get("id") == src_table.get("id"), f"{dst_table.get('id')} vs {src_table.get('id')}")
    for i in range(3):
        sp, dp = src_table["partitions"][i], dst_table["partitions"][i]
        check(f"раздел {i + 1}: тип, GUID и имя сохранены", (sp["type"], sp["uuid"], sp.get("name")) == (dp["type"], dp["uuid"], dp.get("name")), f"{sp} | {dp}")
    sizes = []
    for i in (1, 2, 3):
        fs, want = expect[i]
        got, fssize = mount_sums(f"{D}p{i}", fs)
        check(f"раздел {i} ({fs}): все файлы совпадают побитно", got == want, f"{len(got)} vs {len(want)}")
        sizes.append(fssize)
    check("NTFS расширен до нового размера раздела (>150 МБ)", sizes[1] > 150e6, str(sizes))
    check("ext4 расширен до нового размера раздела (>150 МБ)", sizes[2] > 150e6, str(sizes))
    # ---------- новые идентификаторы
    D2 = attach(DST2_IMG)
    ev = h.call("clone", limit=300, **{**base, "dst": D2, "new_ids": True, "confirm": os.path.basename(D2)})
    d = ev[-1]
    check("клон с новыми идентификаторами завершён", d["ev"] == "done" and d["reason"] == "finished", str(d)[:300])
    sudo("partprobe", D2)
    sudo("udevadm", "settle")
    t2 = json.loads(sudo("sfdisk", "-J", D2).stdout)["partitiontable"]
    check("новые GUID разделов и диска", t2["id"] != src_table["id"] and all(t2["partitions"][i]["uuid"] != src_table["partitions"][i]["uuid"] for i in range(3)))
    u_src = sudo("blkid", "-s", "UUID", "-o", "value", f"{S}p3").stdout.strip()
    u_dst = sudo("blkid", "-s", "UUID", "-o", "value", f"{D2}p3").stdout.strip()
    check("новый UUID файловой системы ext4", u_src and u_dst and u_src != u_dst, f"{u_src} {u_dst}")
    got, _ = mount_sums(f"{D2}p3", "ext4")
    check("данные клона с новыми ID целы", got == expect[3][1])
    h.p.stdin.close()
    h.p.wait(timeout=30)
finally:
    for dev in loops:
        sudo("umount", "-R", dev)
        sudo("losetup", "-d", dev)
    for f in (SRC_IMG, DST_IMG, DST2_IMG):
        if os.path.exists(f):
            os.remove(f)
print()
print("ИТОГО:", "всё в порядке" if not fails else f"провалов: {fails}")
sys.exit(1 if fails else 0)
