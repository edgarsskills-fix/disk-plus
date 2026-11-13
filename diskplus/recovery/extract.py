"""Извлечение файлов: `icat` в папку назначения с сохранением структуры; чтение начала файла для предпросмотра."""
from __future__ import annotations

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import json

from . import ntfs_scan
from .index import Index, Node


def safe_parts(rel: str) -> list[str]:
    """Путь из индекса -> безопасные компоненты (без .., пустых, управляющих символов)."""
    out = []
    for p in rel.split("/"):
        p = p.replace("\0", "").strip()
        if p in ("", ".", ".."):
            continue
        out.append(p.replace("\\", "_"))
    return out


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def icat_cmd(source: str, offset: int, sector: int, node: Node) -> list[str]:
    cmd = ["icat"] + (["-r"] if node.deleted else []) + ["-b", str(sector), "-o", str(offset), source, node.inode]
    return cmd


def _meta_stream(source, offset, sector, node, cluster):
    meta = json.loads(node.meta)
    with open(source, "rb") as f:
        yield from ntfs_scan.open_node_stream(f, offset * sector, cluster, meta)


def read_head(source: str, offset: int, sector: int, node: Node, limit: int = 1 << 20, cluster: int = 4096) -> bytes:
    """Первые `limit` байт файла (для предпросмотра)."""
    if node.meta:
        out = b""
        try:
            for chunk in _meta_stream(source, offset, sector, node, cluster):
                out += chunk
                if len(out) >= limit:
                    break
        except (RuntimeError, OSError):
            pass
        return out[:limit]
    try:
        p = subprocess.Popen(icat_cmd(source, offset, sector, node), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return b""
    try:
        data = p.stdout.read(limit)
    finally:
        p.kill()
        p.wait()
    return data


@dataclass
class Result:
    ok: int = 0
    failed: list = field(default_factory=list)     # (путь, причина)
    warnings: list = field(default_factory=list)   # (путь, замечание)
    bytes: int = 0
    dest: str = ""
    cancelled: bool = False


def expand(index: Index, ids: list[int]) -> list[Node]:
    """Выбранные узлы + всё содержимое выбранных папок, без дублей, папки первыми по пути."""
    seen, out = set(), []
    for i in ids:
        n = index.get(i)
        if not n:
            continue
        group = [n] + (index.descendants(n.path) if n.is_dir else [])
        for g in group:
            if g.id not in seen:
                seen.add(g.id)
                out.append(g)
    out.sort(key=lambda n: (n.path.count("/"), not n.is_dir, n.path))
    return out


def recover(source: str, offset: int, sector: int, index: Index, ids: list[int], dest: str,
            on_progress=None, cancel: threading.Event | None = None, workers: int = 3,
            fs: str = "") -> Result:
    nodes = expand(index, ids)
    cluster = int(index.meta("cluster", 4096))
    res = Result(dest=dest)
    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    files = [n for n in nodes if not n.is_dir]
    total_bytes = sum(max(n.size, 0) for n in files)
    state = {"done": 0, "bytes": 0}
    lock = threading.Lock()
    # куда класть каждый узел
    targets: dict[int, str] = {}
    for n in nodes:
        parts = safe_parts(n.path)
        if not parts:
            continue
        target = os.path.join(dest, *parts)
        if not os.path.abspath(target).startswith(dest + os.sep):
            res.failed.append((n.path, "небезопасный путь"))
            continue
        targets[n.id] = target
    for n in nodes:                                   # каталоги создаём сразу (в том числе пустые)
        if n.is_dir and n.id in targets:
            try:
                os.makedirs(targets[n.id], exist_ok=True)
            except OSError as e:
                res.failed.append((n.path, e.strerror or "не удалось создать папку"))

    def work(n: Node):
        if cancel is not None and cancel.is_set():
            return
        target = unique_path(targets[n.id])
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if n.deleted and n.size == 0 and fs == "exfat":
                raise RuntimeError("размер удалённого файла exFAT неизвестен, нужен глубокий поиск")
            if n.size == 0:
                open(target, "wb").close()
                got = 0
            else:
                if n.meta:
                    with open(target, "wb") as out:
                        for chunk in _meta_stream(source, offset, sector, n, cluster):
                            out.write(chunk)
                    got = os.path.getsize(target)
                else:
                    with open(target, "wb") as out:
                        p = subprocess.run(icat_cmd(source, offset, sector, n), stdout=out, stderr=subprocess.PIPE,
                                           timeout=3600)
                    got = os.path.getsize(target)
                    if p.returncode != 0 and got == 0:
                        raise RuntimeError((p.stderr.decode("utf-8", "replace").strip().splitlines() or ["ошибка чтения"])[-1])
            if n.size and got != n.size:
                with lock:
                    res.warnings.append((n.path, f"размер {got} вместо {n.size}"))
            if n.mtime > 0:
                try:
                    os.utime(target, (n.mtime, n.mtime))
                except OSError:
                    pass
            with lock:
                res.ok += 1
                res.bytes += got
        except Exception as e:  # noqa: BLE001
            with lock:
                res.failed.append((n.path, str(e)))
            try:
                if os.path.exists(target) and os.path.getsize(target) == 0:
                    os.remove(target)
            except OSError:
                pass
        with lock:
            state["done"] += 1
            state["bytes"] += max(n.size, 0)
            snap = (state["done"], len(files), state["bytes"], total_bytes, n.path)
        if on_progress:
            on_progress(*snap)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, [n for n in files if n.id in targets]))
    res.cancelled = bool(cancel is not None and cancel.is_set())
    return res
