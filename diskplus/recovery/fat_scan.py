"""Глубокий поиск для FAT32 и exFAT: после быстрого форматирования таблицы FAT и корень пусты, но записи каталогов
остались в области данных. Находим их, собираем дерево и предполагаем, что файлы лежат подряд с первого кластера."""
from __future__ import annotations

import json
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .ntfs_scan import LOST_ROOT

CHUNK_CLUSTERS_BYTES = 8 << 20


@dataclass
class Geo:
    kind: str            # fat32 / exfat
    cluster: int         # байт в кластере
    data_start: int      # смещение области данных от начала тома, байт
    clusters: int        # число кластеров
    root: int            # кластер корня
    where: str = ""


def _u(b, off, fmt):
    return struct.unpack_from(fmt, b, off)[0]


def read_geometry(f, vol_start: int, vol_len: int, kind_hint: str = "") -> Geo | None:
    def rd(off):
        try:
            f.seek(vol_start + off)
            return f.read(512)
        except (OSError, ValueError):
            return b""
    tries = [(0, "загрузочный сектор"), (6 * 512, "резервный загрузочный сектор FAT32"), (12 * 512, "резервный загрузочный сектор exFAT")]
    for off, where in tries:
        b = rd(off)
        if len(b) < 512:
            continue
        if b[3:11] == b"EXFAT   ":
            bps, spc = 1 << b[0x6C], 1 << b[0x6D]
            return Geo("exfat", bps * spc, _u(b, 0x58, "<I") * bps, _u(b, 0x5C, "<I"), _u(b, 0x60, "<I"), where)
        if b[82:90] == b"FAT32   " and b[510:512] == b"\x55\xaa":
            bps, spc, rsv, nf = _u(b, 0x0B, "<H"), b[0x0D], _u(b, 0x0E, "<H"), b[0x10]
            fsz, root = _u(b, 0x24, "<I"), _u(b, 0x2C, "<I")
            total = _u(b, 0x20, "<I") or _u(b, 0x13, "<H")
            if bps and spc and fsz:
                data_start = (rsv + nf * fsz) * bps
                return Geo("fat32", bps * spc, data_start, (total - rsv - nf * fsz) // spc, root, where)
    return None


# --------------------------------------------------------------------------- FAT32

def _fat_date(d, t):
    try:
        y, mo, da = 1980 + (d >> 9), (d >> 5) & 15, d & 31
        h, mi, se = t >> 11, (t >> 5) & 63, (t & 31) * 2
        if not (1 <= mo <= 12 and 1 <= da <= 31):
            return 0
        import calendar
        return calendar.timegm((y, mo, da, min(h, 23), min(mi, 59), min(se, 59)))
    except (ValueError, OverflowError):
        return 0


def _parse_fat_dir(buf: bytes):
    """Записи каталога -> [(name, is_dir, start, size, mtime, deleted)], имя из LFN, если есть."""
    out, lfn = [], []
    for i in range(0, len(buf) - 31, 32):
        e = buf[i:i + 32]
        first = e[0]
        if first == 0:
            break
        attr = e[11]
        deleted = first == 0xE5
        if attr == 0x0F:
            chars = e[1:11] + e[14:26] + e[28:32]
            lfn.append(chars)
            continue
        if attr & 0x08:      # метка тома
            lfn = []
            continue
        if e[0:1] == b"." :
            lfn = []
            continue
        name = ""
        if lfn:
            raw = b"".join(reversed(lfn))
            try:
                name = raw.decode("utf-16-le").split("\0")[0].split("￿")[0]
            except UnicodeDecodeError:
                name = ""
        if not name:
            base = e[0:8].decode("cp866", "replace").rstrip()
            ext = e[8:11].decode("cp866", "replace").rstrip()
            if deleted:
                base = "_" + base[1:]
            name = base + ("." + ext if ext else "")
        lfn = []
        start = (_u(e, 20, "<H") << 16) | _u(e, 26, "<H")
        out.append((name, bool(attr & 0x10), start, _u(e, 28, "<I"), _fat_date(_u(e, 24, "<H"), _u(e, 22, "<H")), deleted))
    return out


def scan_fat32(f, vol_start: int, geo: Geo, on_progress=None, cancel=None):
    """Возвращает (каталоги, число кластеров): dirs[cluster] = {'parent': кластер, 'entries': [...]}"""
    cl = geo.cluster
    per = max(1, CHUNK_CLUSTERS_BYTES // cl)
    dirs = {}
    total = geo.clusters
    t0, last = time.time(), 0.0
    c = 2
    while c < total + 2:
        if cancel is not None and cancel.is_set():
            break
        n = min(per, total + 2 - c)
        f.seek(vol_start + geo.data_start + (c - 2) * cl)
        data = f.read(n * cl)
        if not data:
            break
        arr = np.frombuffer(data, dtype=np.uint8)
        nn = len(data) // cl
        starts = arr[: nn * cl].reshape(nn, cl)
        # каталог начинается с записей «.» и «..»
        mask = (starts[:, 0] == 0x2E) & (starts[:, 11] == 0x10) & (starts[:, 32] == 0x2E) & (starts[:, 33] == 0x2E)
        for k in np.nonzero(mask)[0]:
            blk = data[k * cl:(k + 1) * cl]
            self_cl = (_u(blk, 20, "<H") << 16) | _u(blk, 26, "<H")
            parent = (_u(blk, 52, "<H") << 16) | _u(blk, 58, "<H")
            cur = c + int(k)
            if self_cl not in (cur, 0) and self_cl != cur:
                continue
            dirs[cur] = {"parent": parent, "entries": _parse_fat_dir(blk[64:])}
        c += n
        now = time.time()
        if on_progress and now - last > 0.4:
            last = now
            on_progress((c - 2) / max(total, 1), len(dirs), now - t0)
    if on_progress:
        on_progress(1.0, len(dirs), time.time() - t0)
    return dirs


# --------------------------------------------------------------------------- exFAT

def scan_exfat(f, vol_start: int, geo: Geo, on_progress=None, cancel=None):
    """Возвращает dirs[cluster] = {'entries': [(name, is_dir, start, size, mtime, deleted, contiguous)]}"""
    cl = geo.cluster
    per = max(1, CHUNK_CLUSTERS_BYTES // cl)
    dirs: dict[int, dict] = {}
    total = geo.clusters
    t0, last = time.time(), 0.0
    c = 2
    carry = b""
    while c < total + 2:
        if cancel is not None and cancel.is_set():
            break
        n = min(per, total + 2 - c)
        f.seek(vol_start + geo.data_start + (c - 2) * cl)
        data = f.read(n * cl + 32 * 20)        # запас на набор записей, пересекающий границу куска
        if not data:
            break
        arr = np.frombuffer(data, dtype=np.uint8)
        slots = len(data) // 32
        types = arr[: slots * 32 : 32]
        cand = np.nonzero(((types[:-1] == 0x85) | (types[:-1] == 0x05)) & ((types[1:] == 0xC0) | (types[1:] == 0x40)))[0]
        for s in cand:
            off = int(s) * 32
            if off >= n * cl:
                continue        # принадлежит следующему куску
            sec = data[off + 1]
            if sec < 2 or sec > 19 or off + (sec + 1) * 32 > len(data):
                continue
            blk = data[off:off + (sec + 1) * 32]
            # контрольная сумма набора записей
            chk = 0
            for i, bte in enumerate(blk):
                if i in (2, 3):
                    continue
                chk = (((chk << 15) | (chk >> 1)) + bte) & 0xFFFF
            if chk != _u(blk, 2, "<H"):
                continue
            attr = _u(blk, 4, "<H")
            flags = blk[33]
            nlen = blk[35]
            first = _u(blk, 52, "<I")
            dlen = _u(blk, 56, "<Q")
            name_raw = b"".join(blk[64 + 32 * j + 2:64 + 32 * j + 32] for j in range(sec - 1))
            try:
                name = name_raw.decode("utf-16-le")[:nlen]
            except UnicodeDecodeError:
                continue
            if not name or "\0" in name:
                continue
            # время изменения: дата/время FAT в поле 12..15 (modify)
            mt = _fat_date(_u(blk, 14, "<H"), _u(blk, 12, "<H"))
            cluster_of_set = c + off // cl
            d = dirs.setdefault(cluster_of_set, {"entries": []})
            d["entries"].append((name, bool(attr & 0x10), first, dlen, mt, blk[0] == 0x05, bool(flags & 2)))
        c += n
        now = time.time()
        if on_progress and now - last > 0.4:
            last = now
            on_progress((c - 2) / max(total, 1), len(dirs), now - t0)
    if on_progress:
        on_progress(1.0, len(dirs), time.time() - t0)
    return dirs


# --------------------------------------------------------------------------- дерево

def build_rows(geo: Geo, dirs: dict):
    """Каталоги -> строки индекса (как в ntfs_scan.build_rows)."""
    child_name: dict[int, str] = {}      # кластер каталога -> имя из записи родителя
    parent_of: dict[int, int] = {}
    for dcl, d in dirs.items():
        for e in d["entries"]:
            name, is_dir, start = e[0], e[1], e[2]
            if is_dir and start >= 2:
                child_name.setdefault(start, name)
                parent_of.setdefault(start, dcl)
    if geo.kind == "fat32":
        for dcl, d in dirs.items():
            p = d.get("parent", 0)
            if p not in (0, geo.root):
                parent_of.setdefault(dcl, p)
            elif p in (0, geo.root):
                parent_of[dcl] = 0

    cache: dict[int, str] = {}

    def dir_path(cl_: int, depth=0) -> str:
        if cl_ in (0, geo.root):
            return ""
        if cl_ in cache:
            return cache[cl_]
        name = child_name.get(cl_, f"Папка {cl_}")
        parent = parent_of.get(cl_)
        if parent is None:
            base = ""      # родитель неизвестен: у exFAT это каталоги верхнего уровня (корень стёрт)
        elif depth > 64 or (parent != 0 and parent not in dirs):
            base = f"{LOST_ROOT}/Папка {parent}" if parent not in (0, geo.root) else ""
        else:
            base = dir_path(parent, depth + 1) if parent != 0 else ""
        p = (base + "/" if base else "") + name
        cache[cl_] = p
        return p

    rows = []
    ids: dict[str, int] = {}
    nid = 1

    def ensure_dir(path: str) -> int:
        nonlocal nid
        if path == "":
            return 0
        if path in ids:
            return ids[path]
        pp, _, base = path.rpartition("/")
        pid = ensure_dir(pp)
        ids[path] = nid
        rows.append((nid, pid, base, path, "", 1, 0, 0, 0, 0, 0, ""))
        nid += 1
        return ids[path]

    entries = []
    for dcl, d in dirs.items():
        dp = dir_path(dcl)
        for e in d["entries"]:
            name, is_dir, start, size, mt, deleted = e[:6]
            contiguous = e[6] if len(e) > 6 else True
            entries.append((dp, name, is_dir, start, size, mt, deleted, contiguous, dcl))
    entries.sort(key=lambda t: (t[0].count("/"), t[0].lower(), t[1].lower()))
    for dp, name, is_dir, start, size, mt, deleted, contiguous, dcl in entries:
        pid = ensure_dir(dp)
        path = (dp + "/" if dp else "") + name
        if is_dir:
            if path not in ids:
                ids[path] = nid
                rows.append((nid, pid, name, path, f"{start}", 1, int(deleted), 0, 0, mt, 0, ""))
                nid += 1
            continue
        if start < 2 or start >= geo.clusters + 2 or (size and start + (size + geo.cluster - 1) // geo.cluster - 1 >= geo.clusters + 2):
            continue
        meta = {"t": "c", "off": geo.data_start + (start - 2) * geo.cluster, "size": size}
        if not contiguous:
            meta["frag"] = 1
        rows.append((nid, pid, name, path, f"{start}", 0, int(deleted), 0, size, mt, 0, json.dumps(meta, separators=(",", ":"))))
        nid += 1
    return rows


def deep_scan(source: str, vol_offset: int, vol_length: int, sector: int, index, on_progress=None,
              cancel: threading.Event | None = None):
    start = vol_offset * sector
    length = vol_length * sector
    try:
        f = open(source, "rb")
    except OSError as e:
        return False, f"не удалось открыть источник: {e.strerror}", {}
    with f:
        geo = read_geometry(f, start, length)
        if geo is None:
            return False, "не удалось определить геометрию тома (загрузочный сектор повреждён)", {}
        dirs = scan_fat32(f, start, geo, on_progress, cancel) if geo.kind == "fat32" else \
            scan_exfat(f, start, geo, on_progress, cancel)
    if cancel is not None and cancel.is_set():
        return False, "отменено", {}
    index.clear()
    rows = build_rows(geo, dirs)
    for i in range(0, len(rows), 2000):
        index.insert_many(rows[i:i + 2000])
    index.set_meta("cluster", geo.cluster)
    index.set_meta("deep", 1)
    index.set_meta("complete", 1)
    return True, "", {"dirs": len(dirs), "files": sum(1 for r in rows if not r[5]), "cluster": geo.cluster,
                      "geometry": geo.where, "kind": geo.kind}
