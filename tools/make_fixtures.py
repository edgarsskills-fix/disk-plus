#!/usr/bin/python3
"""Создаёт тестовые образы дисков с NTFS/exFAT/FAT32/ext4: файлы + удалённые файлы + манифест контрольных сумм.
Нужен root (losetup/mount). Результат: ~/.cache/diskplus-test/fx-<fs>.img и fx-<fs>.json
Реальные диски не трогает: работает только с файлами-образами и loop-устройствами."""
import hashlib
import json
import os
import pwd
import random
import shutil
import subprocess
import sys
import tempfile

OWNER = int(os.environ.get("SUDO_UID", "1000"))
HOME = pwd.getpwuid(OWNER).pw_dir
OUT = os.path.join(HOME, ".cache", "diskplus-test")
FS = {
    "ntfs": dict(size=160, mkfs=["mkfs.ntfs", "-Q", "-L", "FXNTFS"], mount=["-t", "ntfs-3g"]),   # ntfs-3g удаляет «по-виндовому»
    "exfat": dict(size=160, mkfs=["mkfs.exfat", "-L", "FXEXFAT"], mount=["-t", "exfat"]),
    "fat32": dict(size=160, mkfs=["mkfs.vfat", "-F", "32", "-n", "FXFAT32"], mount=["-t", "vfat"], mdel=True),
    # NTFS, быстро отформатированный ПОВЕРХ файлов: обычное чтение видит пустой диск, глубокий поиск должен найти старое
    "ntfsfmt": dict(size=160, mkfs=["mkfs.ntfs", "-Q", "-L", "FXFMT"], mount=["-t", "ntfs-3g"], reformat=True, keep=True),
    "fat32fmt": dict(size=160, mkfs=["mkfs.vfat", "-F", "32", "-n", "FXFAT32F"], mount=["-t", "vfat"], reformat=True, keep=True),
    "exfatfmt": dict(size=160, mkfs=["mkfs.exfat", "-L", "FXEXFATF"], mount=["-t", "exfat"], wipe_exfat=True, keep=True),   # как быстрое форматирование Windows: FAT, битовая карта и корень очищены
    "ext4": dict(size=160, mkfs=["mkfs.ext4", "-q", "-L", "FXEXT4"], mount=["-t", "ext4"]),
}


def run(*a, **kw):
    return subprocess.run(a, check=True, capture_output=True, text=True, **kw)


def make_png(path, seed):
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    im = Image.new("RGB", (320, 200), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    d = ImageDraw.Draw(im)
    for _ in range(40):
        x0, x1 = sorted((rnd.randrange(320), rnd.randrange(320)))
        y0, y1 = sorted((rnd.randrange(200), rnd.randrange(200)))
        d.rectangle([x0, y0, x1, y1], fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    im.save(path)


def populate(root, remove=True):
    rnd = random.Random(42)
    files = {}   # относительный путь -> удалить ли

    def put(rel, data=None, deleted=False):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if data is not None:
            open(p, "wb").write(data)
        files[rel] = deleted

    put("Documents/report.txt", ("Квартальный отчёт\n" * 300).encode())
    put("Documents/notes/todo.txt", b"1. buy milk\n2. recover disk\n" * 50)
    put("Documents/old/draft.txt", "Старый черновик\n".encode() * 400, deleted=True)
    put("Documents/old/plan.txt", b"plan " * 3000, deleted=True)
    put("Documents/old/sub/data.bin", bytes(rnd.randrange(256) for _ in range(150_000)), deleted=True)
    os.makedirs(os.path.join(root, "Photos"), exist_ok=True)
    make_png(os.path.join(root, "Photos", "pic1.png"), 2)
    files["Photos/pic1.png"] = False
    make_png(os.path.join(root, "Photos", "pic2.png"), 3)
    files["Photos/pic2.png"] = True
    put("Music/track.bin", rnd.randbytes(3_000_000))
    put("Big/big.bin", rnd.randbytes(20_000_000), deleted=True)
    put("Работа/файл с пробелами и юникодом.txt", "Привет, мир! ".encode() * 500)
    put("empty.txt", b"")
    manifest = {}
    for rel, deleted in files.items():
        p = os.path.join(root, rel)
        data = open(p, "rb").read()
        manifest[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "deleted": deleted}
    if remove:
        for rel, deleted in files.items():
            if deleted:
                os.remove(os.path.join(root, rel))
    return manifest


def build(name):
    cfg = FS[name]
    img = os.path.join(OUT, f"fx-{name}.img")
    os.makedirs(OUT, exist_ok=True)
    if os.path.exists(img):
        os.remove(img)
    run("truncate", "-s", f"{cfg['size']}M", img)
    run("sgdisk", "-o", "-n", "1:2048:0", "-t", "1:0700", "-c", "1:Данные", img)
    loop = run("losetup", "--find", "--show", "-P", img).stdout.strip()
    mnt = tempfile.mkdtemp(prefix="dpfx-")
    try:
        run(*cfg["mkfs"], f"{loop}p1")
        run("mount", *cfg["mount"], f"{loop}p1", mnt)
        try:
            manifest = populate(mnt, remove=not (cfg.get("mdel") or cfg.get("keep")))
            run("sync")
        finally:
            run("umount", mnt)
    finally:
        subprocess.run(["losetup", "-d", loop])
        shutil.rmtree(mnt, ignore_errors=True)
    if cfg.get("reformat"):   # быстрое форматирование того же раздела
        loop2 = run("losetup", "--find", "--show", "-P", img).stdout.strip()
        try:
            run(*cfg["mkfs"], f"{loop2}p1")
        finally:
            subprocess.run(["losetup", "-d", loop2])
    if cfg.get("wipe_exfat"):
        import struct
        with open(img, "r+b") as f:
            base = 2048 * 512
            f.seek(base)
            b = f.read(512)
            bps, spc = 1 << b[0x6C], 1 << b[0x6D]
            fat_off, fat_len, heap, root = (struct.unpack_from("<I", b, o)[0] for o in (0x50, 0x54, 0x58, 0x60))
            f.seek(base + fat_off * bps)
            f.write(b"\0" * (fat_len * bps))
            f.seek(base + heap * bps + (root - 2) * bps * spc)
            f.write(b"\0" * (bps * spc))      # корневой каталог (1 кластер) и всё, что в нём было: метка, битовая карта, верхний уровень
    if cfg.get("mdel"):   # FAT: Linux обнуляет размер при удалении, поэтому удаляем mtools-ом, как это делает Windows
        for rel, info in manifest.items():
            if info["deleted"]:
                run("mdel", "-i", f"{img}@@1048576", "::/" + rel)
    json.dump(manifest, open(os.path.join(OUT, f"fx-{name}.json"), "w"), ensure_ascii=False, indent=1)
    os.chown(img, OWNER, OWNER)
    os.chown(os.path.join(OUT, f"fx-{name}.json"), OWNER, OWNER)
    print(f"готово: {img} ({len(manifest)} файлов, удалено {sum(v['deleted'] for v in manifest.values())})")


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("нужен root: sudo python3 tools/make_fixtures.py [ntfs exfat fat32 ext4]")
    for n in (sys.argv[1:] or FS):
        build(n)
