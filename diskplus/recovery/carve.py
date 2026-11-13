"""Поиск файлов по сигнатурам (PhotoRec): работает без файловой системы, имена и папки не восстанавливаются."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time

# Имена типов — именно те, что принимает PhotoRec (проверено): docx/xlsx/odt он находит внутри «zip»,
# mp4/3gp — внутри «mov», wav/avi — внутри «riff».
PRESETS = {
    "popular": ("Популярные типы", ["jpg", "png", "gif", "bmp", "tif", "psd", "raf", "orf", "rw2", "pdf", "doc", "txt",
                                    "xml", "zip", "rar", "7z", "mp3", "flac", "ogg", "mov", "mkv", "mpg", "riff", "asf",
                                    "flv", "sqlite"]),
    "photo": ("Фото и видео", ["jpg", "png", "gif", "bmp", "tif", "psd", "raf", "orf", "rw2", "mov", "mkv", "mpg", "riff",
                               "asf", "flv"]),
    "docs": ("Документы и архивы", ["pdf", "doc", "txt", "xml", "zip", "rar", "7z", "gz", "tar", "sqlite"]),
    "all": ("Все типы (много мусора)", None),
}

TYPE_GROUPS = {
    "Картинки": {"jpg", "png", "gif", "bmp", "tif", "psd", "raf", "orf", "rw2", "ico", "webp", "heic", "cr2", "nef", "arw", "dng"},
    "Видео и звук": {"mov", "mp4", "3gp", "mkv", "mpg", "avi", "wav", "riff", "asf", "wmv", "flv", "mp3", "flac", "ogg"},
    "Документы": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "txt", "xml", "sqlite"},
    "Архивы": {"zip", "rar", "7z", "gz", "bz2", "tar", "xz"},
}


def build_cmd(source: str, dest_dir: str, preset: str) -> list[str]:
    types = PRESETS[preset][1]
    parts = ["partition_none", "options", "keep_corrupted_file", "wholespace", "fileopt", "everything"]
    if types is None:
        parts.append("enable")
    else:
        parts.append("disable")
        for t in types:
            parts += [t, "enable"]
    parts.append("search")
    return ["photorec", "/d", os.path.join(dest_dir, "recup"), "/cmd", source, ",".join(parts)]


PASS_RE = re.compile(rb"Pass (\d+) - Reading sector\s+(\d+)/(\d+), (\d+) files found")
TYPE_RE = re.compile(rb"([a-z0-9_]+): (\d+) recovered")


def run(source: str, dest_dir: str, preset: str = "popular", on_progress=None,
        cancel: threading.Event | None = None) -> tuple[bool, str, dict]:
    """Запустить PhotoRec. Возвращает (успех, ошибка, {расширение: количество})."""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        p = subprocess.Popen(build_cmd(source, dest_dir, preset), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=dict(os.environ, TERM="xterm", LC_ALL="C"))
    except OSError:
        return False, "PhotoRec не установлен (пакет testdisk)", {}
    t0 = time.time()
    last = 0.0
    tail = b""
    state = {"pass": 1, "done": 0, "total": 1, "files": 0}
    while True:
        if cancel is not None and cancel.is_set():
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
            break
        chunk = os.read(p.stdout.fileno(), 65536)
        if not chunk:
            break
        tail = (tail + chunk)[-8192:]
        m = None
        for m in PASS_RE.finditer(tail):
            pass
        if m:
            state.update({"pass": int(m.group(1)), "done": int(m.group(2)), "total": max(int(m.group(3)), 1),
                          "files": int(m.group(4))})
        now = time.time()
        if on_progress and now - last > 0.4:
            last = now
            on_progress(state["done"] / state["total"], state["files"], now - t0, state["pass"])
    rc = p.wait()
    counts = summarize(dest_dir)
    if cancel is not None and cancel.is_set():
        return False, "отменено", counts
    if rc != 0 and not counts:
        return False, f"PhotoRec завершился с ошибкой ({rc})", counts
    if on_progress:
        on_progress(1.0, sum(counts.values()), time.time() - t0, state["pass"])
    return True, "", counts


def summarize(dest_dir: str) -> dict:
    out: dict[str, int] = {}
    for root, _d, files in os.walk(dest_dir):
        if not os.path.basename(root).startswith("recup"):
            continue
        for fn in files:
            if fn.startswith(("f", "b")) and "." in fn:
                ext = fn.rsplit(".", 1)[1].lower()
                out[ext] = out.get(ext, 0) + 1
    return out


def organize(dest_dir: str) -> dict:
    """Разложить найденное по папкам типов (Картинки, Видео, …) и вернуть счётчики по группам."""
    groups: dict[str, int] = {}
    for root, _d, files in list(os.walk(dest_dir)):
        if not os.path.basename(root).startswith("recup"):
            continue
        for fn in files:
            if not re.match(r"^[fb]\d+\.", fn):
                continue
            ext = fn.rsplit(".", 1)[1].lower()
            grp = next((g for g, s in TYPE_GROUPS.items() if ext in s), "Прочее")
            target = os.path.join(dest_dir, grp)
            os.makedirs(target, exist_ok=True)
            dst = os.path.join(target, fn)
            i = 2
            while os.path.exists(dst):
                dst = os.path.join(target, f"{fn.rsplit('.', 1)[0]}_{i}.{ext}")
                i += 1
            os.replace(os.path.join(root, fn), dst)
            groups[grp] = groups.get(grp, 0) + 1
    return groups
