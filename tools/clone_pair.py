#!/usr/bin/python3
"""Пара виртуальных дисков для проверки клонирования в интерфейсе (только loop-устройства из файлов).
  sudo python3 tools/clone_pair.py up    -> печатает «SRC DST»
  sudo python3 tools/clone_pair.py down"""
import os
import pwd
import subprocess
import sys
import tempfile

OWNER = int(os.environ.get("SUDO_UID", "1000"))
WORK = os.path.join(pwd.getpwuid(OWNER).pw_dir, ".cache", "diskplus-test", "clonegui")
STATE = os.path.join(WORK, "loops")


def sh(*a, **kw):
    return subprocess.run(a, check=True, capture_output=True, text=True, **kw).stdout


def up():
    os.makedirs(WORK, exist_ok=True)
    src, dst = os.path.join(WORK, "src.img"), os.path.join(WORK, "dst.img")
    sh("truncate", "-s", "300M", src)
    sh("truncate", "-s", "900M", dst)
    S = sh("losetup", "-fP", "--show", src).strip()
    D = sh("losetup", "-fP", "--show", dst).strip()
    sh("sfdisk", "-q", S, input='label: gpt\nstart=2048, size=131072, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI"\n'
       'start=133120, size=204800, type=EBD0A0A2-B9E5-4433-87C7-68D4B1F8A4D3, name="Windows"\n'
       'start=337920, size=204800, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="Linux root"\n')
    sh("partprobe", S)
    sh("udevadm", "settle")
    for i, (mk, opts) in enumerate((("mkfs.vfat -F32 -n EFI", []), ("mkfs.ntfs -Q -L WIN", ["-t", "ntfs-3g"]), ("mkfs.ext4 -q -L ROOT", [])), 1):
        sh(*mk.split(), f"{S}p{i}")
        mnt = tempfile.mkdtemp(prefix="dpcp-")
        sh("mount", *opts, f"{S}p{i}", mnt)
        sh("sh", "-c", f"echo 'файл {i}' > {mnt}/hello{i}.txt; head -c 2000000 /dev/urandom > {mnt}/rand.bin")
        sh("umount", mnt)
        os.rmdir(mnt)
    open(STATE, "w").write(f"{S} {D}\n")
    os.chown(WORK, OWNER, OWNER)
    print(S, D)


def down():
    if os.path.exists(STATE):
        for dev in open(STATE).read().split():
            subprocess.run(["umount", "-R", dev], capture_output=True)
            subprocess.run(["losetup", "-d", dev], capture_output=True)
        os.remove(STATE)
    for f in ("src.img", "dst.img"):
        p = os.path.join(WORK, f)
        if os.path.exists(p):
            os.remove(p)


if __name__ == "__main__":
    if os.geteuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in ("up", "down"):
        sys.exit("sudo python3 tools/clone_pair.py up|down")
    up() if sys.argv[1] == "up" else down()
