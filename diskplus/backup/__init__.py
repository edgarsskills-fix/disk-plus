"""Резервные копии: чтение манифеста, поиск копий, извлечение раздела в файл (без прав root)."""
from __future__ import annotations

import json
import os
import subprocess
import threading

CLONERS = {"ntfs": "partclone.ntfs", "ext2": "partclone.extfs", "ext3": "partclone.extfs", "ext4": "partclone.extfs",
           "btrfs": "partclone.btrfs", "xfs": "partclone.xfs", "vfat": "partclone.vfat", "exfat": "partclone.exfat",
           "f2fs": "partclone.f2fs", "hfsplus": "partclone.hfsp"}
FORMAT = "diskplus-backup/1"


def read_manifest(folder: str) -> dict | None:
    try:
        m = json.load(open(os.path.join(folder, "manifest.json")))
    except (OSError, ValueError):
        return None
    return m if m.get("format") == FORMAT else None


def find_backups(root: str, depth: int = 1) -> list[tuple[str, dict]]:
    """Копии в папке (на один уровень вглубь): [(папка, манифест)], новые первыми."""
    out = []
    try:
        entries = [os.path.join(root, n) for n in os.listdir(root)]
    except OSError:
        return []
    for d in entries:
        if os.path.isdir(d):
            m = read_manifest(d)
            if m:
                out.append((d, m))
    out.sort(key=lambda t: t[1].get("created", ""), reverse=True)
    return out


def total_bytes(m: dict) -> int:
    return sum(p.get("bytes", 0) for p in m["partitions"])


def extract_partition(folder: str, part: dict, outfile: str, on_progress=None, cancel: threading.Event | None = None):
    """Развернуть раздел из копии в обычный файл (разреженный). Возвращает (успех, ошибка).
    Работает от имени пользователя: partclone восстанавливает в файл заранее нужного размера."""
    if not part.get("file"):
        return False, "у этого раздела нет данных (раздел подкачки)"
    src = os.path.join(folder, part["file"])
    if not os.path.isfile(src) or os.path.dirname(os.path.realpath(src)) != os.path.realpath(folder):
        return False, "файл копии не найден"
    tool = "partclone.dd" if part.get("method") == "raw" else CLONERS.get(part.get("fs", ""))
    if not tool:
        return False, "тип файловой системы не поддерживается"
    if os.path.exists(outfile):
        os.remove(outfile)
    with open(outfile, "wb") as f:
        f.truncate(part["size"] * 512)
    log = outfile + ".log"
    env = dict(os.environ, LC_ALL="C")
    z = subprocess.Popen(["zstd", "-dc", "-q", src], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    p = subprocess.Popen([tool, "-r", "-s", "-", "-O", outfile, "-L", log], stdin=z.stdout, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=env)
    z.stdout.close()
    while p.poll() is None:
        if cancel is not None and cancel.is_set():
            p.terminate()
            z.terminate()
            break
        try:
            p.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            if on_progress:
                try:
                    on_progress(os.stat(outfile).st_blocks * 512)
                except OSError:
                    pass
    z.wait()
    try:
        os.remove(log)
    except OSError:
        pass
    if cancel is not None and cancel.is_set():
        return False, "отменено"
    if p.returncode != 0:
        try:
            os.remove(outfile)
        except OSError:
            pass
        return False, "не удалось развернуть раздел из копии"
    return True, ""
