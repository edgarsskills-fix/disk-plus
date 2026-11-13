#!/usr/bin/python3
"""Бэкапы: создание, проверка, обнаружение повреждения, восстановление и извлечение раздела. ТОЛЬКО loop-устройства.
  SUDO_ASKPASS=... python3 tests/test_backup.py"""
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
from diskplus import backup as bk  # noqa: E402
from diskplus.recovery import extract, index, volumes  # noqa: E402

HELPER = os.path.join(ROOT, "lib", "diskplus-helper")
WORK = os.path.expanduser("~/.cache/diskplus-test/backup")
SUDO = ["sudo", "-A" if os.environ.get("SUDO_ASKPASS") else "-n"]
fails = 0
loops = []


def check(name, cond, extra=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"   [{extra}]" if extra else ""))
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


def mount_sums(node, fs):
    mnt = tempfile.mkdtemp(prefix="dptb-")
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


os.makedirs(WORK, exist_ok=True)
SRC_IMG, DST_IMG = os.path.join(WORK, "src.img"), os.path.join(WORK, "dst.img")
BK = os.path.join(WORK, "copy1")
shutil.rmtree(BK, ignore_errors=True)
try:
    for f, sz in ((SRC_IMG, "300M"), (DST_IMG, "700M")):
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
        sudo("sh", "-c", f"echo 'файл {i}' > {mnt}/hello{i}.txt; head -c 2500000 /dev/urandom > {mnt}/rand{i}.bin; mkdir -p {mnt}/d/s; echo deep > {mnt}/d/s/x.txt; chmod -R a+rX {mnt} 2>/dev/null; true")
        expect[i] = (fs, mount_sums(f"{S}p{i}", fs)[0] if False else None)
        sudo("umount", mnt)
        os.rmdir(mnt)
    for i in (1, 2, 3):
        fs = expect[i][0]
        expect[i] = (fs, mount_sums(f"{S}p{i}", fs)[0])
    check("источник подготовлен", all(len(v[1]) >= 3 for v in expect.values()))
    src_table = json.loads(sudo("sfdisk", "-J", S).stdout)["partitiontable"]
    h = H()
    allp = [f"{S}p1", f"{S}p2", f"{S}p3"]

    print("\n-- создание копии: отказы")
    r = h.call("backup", src=allp[0], dest=BK, parts=allp)
    check("раздел вместо диска отклонён", r[-1]["ev"] == "err")
    r = h.call("backup", src=S, dest="/etc/diskplus-bk", parts=allp)
    check("папка вне разрешённых мест отклонена", r[-1]["ev"] == "err")
    r = h.call("backup", src=S, dest="/tmp/diskplus-bk", parts=allp)
    check("/tmp не разрешён", r[-1]["ev"] == "err")
    r = h.call("backup", src=S, dest=WORK, parts=allp)
    check("существующая папка отклонена", r[-1]["ev"] == "err")
    r = h.call("backup", src=S, dest=BK, parts=["/dev/nvme1n1p2"])
    check("чужой раздел отклонён", r[-1]["ev"] == "err")
    sm = tempfile.mkdtemp(prefix="dpts-")
    sudo("mount", "-o", "ro", f"{S}p3", sm)
    r = h.call("backup", src=S, dest=BK, parts=allp)
    check("примонтированный раздел отклонён", r[-1]["ev"] == "err" and "примонтирован" in r[-1].get("msg", ""), str(r[-1])[:160])
    sudo("umount", sm)
    os.rmdir(sm)
    check("после отказов папка не создана", not os.path.exists(BK))

    print("\n-- создание копии")
    ev = h.call("backup", limit=300, src=S, dest=BK, parts=allp, level=3, verify=True)
    d = ev[-1]
    check("копия создана", d["ev"] == "done" and d["reason"] == "finished", str(d)[:300])
    man = bk.read_manifest(BK)
    check("манифест читается, 3 раздела", man and len(man["partitions"]) == 3, str(man)[:100])
    files = sorted(os.listdir(BK))
    check("файлы копии на месте", all(f in files for f in ("manifest.json", "table.sfdisk")) and sum(f.endswith(".pcl.zst") for f in files) == 3, str(files))
    check("файлы отданы пользователю", all(os.stat(os.path.join(BK, f)).st_uid == os.getuid() for f in files))
    okh = all(hashlib.sha256(open(os.path.join(BK, p["file"]), "rb").read()).hexdigest() == p["sha256"] for p in man["partitions"])
    check("sha256 в манифесте совпадают с файлами", okh)
    check("копия сжата (меньше исходных 300 МБ)", bk.total_bytes(man) < 40e6, str(bk.total_bytes(man)))
    check("найдена в списке копий", any(f == BK for f, _m in bk.find_backups(WORK)))
    r = h.call("backup_verify", folder=BK)
    check("проверка копии: чисто", r[-1]["ev"] == "res" and not r[-1]["data"]["problems"], str(r[-1])[:200])

    print("\n-- повреждение копии")
    BAD = os.path.join(WORK, "copy_bad")
    shutil.rmtree(BAD, ignore_errors=True)
    shutil.copytree(BK, BAD)
    target = os.path.join(BAD, man["partitions"][2]["file"])
    with open(target, "r+b") as f:
        f.seek(os.path.getsize(target) // 2)
        b = f.read(1)
        f.seek(-1, 1)
        f.write(bytes([b[0] ^ 0xFF]))
    r = h.call("backup_verify", folder=BAD)
    check("повреждение обнаружено", r[-1]["ev"] == "res" and len(r[-1]["data"]["problems"]) >= 1, str(r[-1])[:200])
    shutil.rmtree(BAD, ignore_errors=True)

    print("\n-- извлечение раздела без прав root")
    out = os.path.join(WORK, "p3.img")
    ok, err = bk.extract_partition(BK, man["partitions"][2], out)
    check("раздел развернут в файл", ok, err)
    vs = volumes.list_volumes(out)
    check("движок восстановления видит ext4", vs and vs[0].fs == "ext4", str(vs))
    db = index.Index(tempfile.mktemp(suffix=".sqlite"))
    index.scan_volume(out, vs[0].offset, vs[0].sector, db)
    ids = [n.id for n in db.search("")]
    dst = tempfile.mkdtemp(prefix="dpx-")
    r2 = extract.recover(out, vs[0].offset, vs[0].sector, db, ids, dst)
    got = {os.path.relpath(os.path.join(dp, f), dst): hashlib.sha256(open(os.path.join(dp, f), "rb").read()).hexdigest()
           for dp, _d, fl in os.walk(dst) for f in fl}
    check("файлы из копии совпадают побитно", got == expect[3][1], f"{len(got)} vs {len(expect[3][1])}")
    os.remove(out)

    print("\n-- восстановление: отказы")
    D = attach(DST_IMG)
    base = {"folder": BK, "dst": D, "new_ids": False, "verify": True,
            "parts": [{"index": 1, "start": 2048, "size": 131072}, {"index": 2, "start": 133120, "size": 409600}, {"index": 3, "start": 542720, "size": 409600}]}
    r = h.call("restore_check", **{**base, "dst": f"{D}p1"})
    check("раздел вместо диска отклонён", r[-1]["ev"] == "err")
    r = h.call("restore_check", **{**base, "dst": "/dev/nvme1n1"})
    pr = r[-1].get("data", {}).get("problems", []) if r[-1]["ev"] == "res" else r[-1].get("msg", "")
    check("системный NVMe как назначение: отказ (проверка без записи)", "системн" in str(pr) or "используется" in str(pr), str(pr)[:160])
    r = h.call("restore_check", **{**base, "parts": [{"index": 2, "start": 2048, "size": 1000}]})
    check("уменьшение раздела отклонено", r[-1]["ev"] == "res" and bool(r[-1]["data"]["problems"]))
    r = h.call("restore_check", **{**base, "folder": "/etc"})
    check("папка без копии отклонена", r[-1]["ev"] == "err")
    r = h.call("restore", **base)
    check("без подтверждения: отказ", r[-1]["ev"] == "err")

    print("\n-- восстановление")
    ev = h.call("restore", limit=300, **{**base, "confirm": os.path.basename(D)})
    d = ev[-1]
    check("восстановление завершено", d["ev"] == "done" and d["reason"] == "finished", str(d)[:300])
    sudo("partprobe", D)
    sudo("udevadm", "settle")
    t2 = json.loads(sudo("sfdisk", "-J", D).stdout)["partitiontable"]
    check("label-id диска и GUID разделов сохранены", t2["id"] == src_table["id"] and all(
        (t2["partitions"][i]["uuid"], t2["partitions"][i]["type"], t2["partitions"][i].get("name")) ==
        (src_table["partitions"][i]["uuid"], src_table["partitions"][i]["type"], src_table["partitions"][i].get("name")) for i in range(3)))
    sizes = []
    for i in (1, 2, 3):
        fs, want = expect[i]
        got, fssize = mount_sums(f"{D}p{i}", fs)
        check(f"раздел {i} ({fs}): файлы совпадают побитно", got == want, f"{len(got)} vs {len(want)}")
        sizes.append(fssize)
    check("NTFS и ext4 расширены (>150 МБ)", sizes[1] > 150e6 and sizes[2] > 150e6, str(sizes))
    h.p.stdin.close()
    h.p.wait(timeout=30)
finally:
    for dev in loops:
        sudo("umount", "-R", dev)
        sudo("losetup", "-d", dev)
    for f in (SRC_IMG, DST_IMG):
        if os.path.exists(f):
            os.remove(f)
    shutil.rmtree(BK, ignore_errors=True)
print()
print("ИТОГО:", "всё в порядке" if not fails else f"провалов: {fails}")
sys.exit(1 if fails else 0)
