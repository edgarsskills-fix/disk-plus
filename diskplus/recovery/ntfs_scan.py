"""Глубокий поиск для NTFS: сканирование тома на записи MFT («FILE0»), сборка дерева по ссылкам на родителей
и чтение данных по спискам кластеров. Работает, когда обычный разбор файловой системы уже ничего не видит
(быстрое форматирование, повреждённая таблица MFT и т. п.)."""
from __future__ import annotations

import json
import struct
import threading
import time
from dataclasses import dataclass, field

FILETIME_EPOCH = 116444736000000000
LOST_ROOT = "Потерянные папки"
CHUNK = 8 << 20


@dataclass
class Rec:
    recno: int
    seq: int
    lsn: int
    in_use: bool
    is_dir: bool
    names: list = field(default_factory=list)      # (parent_recno, name, namespace)
    mtime: int = 0
    size: int = 0
    resident: bytes | None = None
    runs: list | None = None
    flags: int = 0                                  # флаги атрибута $DATA (сжатие/шифрование)
    has_data: bool = False


def _ft(t: int) -> int:
    return max(0, (t - FILETIME_EPOCH) // 10_000_000) if t else 0


def decode_runs(buf: bytes):
    """Список (lcn|None, длина) из runlist; None = «дыра» (sparse)."""
    runs, pos, lcn = [], 0, 0
    while pos < len(buf) and buf[pos]:
        h = buf[pos]
        ln, off = h & 0xF, h >> 4
        pos += 1
        if ln == 0 or ln > 8 or off > 8 or pos + ln + off > len(buf):
            return None
        length = int.from_bytes(buf[pos:pos + ln], "little")
        pos += ln
        if off:
            lcn += int.from_bytes(buf[pos:pos + off], "little", signed=True)
            pos += off
            runs.append((lcn, length))
        else:
            runs.append((None, length))
    return runs


def parse_record(raw: bytes) -> Rec | None:
    """Разобрать одну запись MFT. Возвращает None, если запись недействительна."""
    n = len(raw)
    if n < 512 or raw[:4] != b"FILE":
        return None
    usa_off, usa_cnt = struct.unpack_from("<HH", raw, 4)
    if usa_cnt < 2 or usa_cnt != n // 512 + 1 or usa_off + usa_cnt * 2 > n:
        return None
    # fixup: в конце каждого сектора лежит USN, настоящие байты хранятся в USA
    buf = bytearray(raw)
    usn = raw[usa_off:usa_off + 2]
    for i in range(1, usa_cnt):
        end = i * 512
        if buf[end - 2:end] != usn:
            return None   # «рваная» запись
        buf[end - 2:end] = raw[usa_off + 2 * i:usa_off + 2 * i + 2]
    lsn, seq, _links, first, flags, used, alloc = struct.unpack_from("<QHHHHII", buf, 8)
    base = struct.unpack_from("<Q", buf, 0x20)[0]
    if base != 0 or first < 0x30 or first >= n or used > n or alloc not in (n,):
        return None
    recno = struct.unpack_from("<I", buf, 0x2C)[0] if first >= 0x30 else 0
    rec = Rec(recno, seq, lsn, bool(flags & 1), bool(flags & 2))
    pos = first
    while pos + 16 <= used:
        atype, alen = struct.unpack_from("<II", buf, pos)
        if atype == 0xFFFFFFFF:
            break
        if alen < 16 or alen % 8 or pos + alen > n:
            break
        nonres = buf[pos + 8]
        nlen = buf[pos + 9]
        aflags = struct.unpack_from("<H", buf, pos + 12)[0]
        if not nonres:
            clen, coff = struct.unpack_from("<IH", buf, pos + 16)
            content = bytes(buf[pos + coff:pos + coff + clen]) if pos + coff + clen <= n else b""
            if atype == 0x30 and len(content) >= 0x42:
                parent = int.from_bytes(content[0:6], "little")
                nl, ns = content[0x40], content[0x41]
                try:
                    name = content[0x42:0x42 + nl * 2].decode("utf-16-le")
                except UnicodeDecodeError:
                    name = ""
                if name:
                    rec.names.append((parent, name, ns))
            elif atype == 0x10 and len(content) >= 0x20:
                rec.mtime = _ft(struct.unpack_from("<Q", content, 8)[0])
            elif atype == 0x80 and nlen == 0:
                rec.has_data = True
                rec.size = clen
                rec.resident = content
                rec.flags = aflags
        else:
            if atype == 0x80 and nlen == 0:
                runoff = struct.unpack_from("<H", buf, pos + 0x20)[0]
                real = struct.unpack_from("<Q", buf, pos + 0x30)[0]
                runs = decode_runs(bytes(buf[pos + runoff:pos + alen]))
                rec.has_data = True
                rec.size = real
                rec.runs = runs
                rec.flags = aflags
        pos += alen
    return rec


def boot_geometry(f, vol_start: int, vol_len_bytes: int):
    """(байт на кластер, размер записи MFT, откуда) по загрузочному сектору или его копии; иначе 4096/1024."""
    def read(off):
        try:
            f.seek(off)
            return f.read(512)
        except (OSError, ValueError):
            return b""
    cands = [(vol_start, "загрузочный сектор")]
    if vol_len_bytes > 1024:
        cands.append((vol_start + vol_len_bytes - 512, "резервный загрузочный сектор"))
    for off, where in cands:
        b = read(off)
        if len(b) == 512 and b[3:11] == b"NTFS    ":
            bps = struct.unpack_from("<H", b, 0x0B)[0]
            spc = b[0x0D]
            if spc > 0x80:
                spc = 1 << (256 - spc)
            cpr = struct.unpack_from("<b", b, 0x40)[0]
            rec = cpr * bps * spc if cpr > 0 else 1 << -cpr
            if bps in (512, 1024, 2048, 4096) and spc and rec in (1024, 4096):
                return bps * spc, rec, where
    return 4096, 1024, "размер кластера не удалось определить, принят 4096"


def scan_records(f, start: int, end: int, rec_size: int = 1024, on_progress=None, cancel: threading.Event | None = None):
    """Найти все записи MFT в диапазоне байт. Возвращает dict recno -> Rec (при повторах побеждает больший LSN)."""
    found: dict[int, Rec] = {}
    pos = start - start % 512
    total = max(end - pos, 1)
    last = 0.0
    t0 = time.time()
    count = 0
    tail = b""
    while pos < end:
        if cancel is not None and cancel.is_set():
            break
        f.seek(pos)
        data = f.read(min(CHUNK, end - pos))
        if not data:
            break
        buf = tail + data
        base = pos - len(tail)
        i = buf.find(b"FILE")
        limit = len(buf) - rec_size if pos + len(data) < end else len(buf) - 512
        while i != -1 and i <= limit:
            if (base + i) % 512 == 0:
                rec = parse_record(buf[i:i + rec_size])
                if rec is not None and rec.names:
                    count += 1
                    old = found.get(rec.recno)
                    if old is None or rec.lsn >= old.lsn:
                        found[rec.recno] = rec
            i = buf.find(b"FILE", i + 1)
        keep = rec_size
        tail = buf[-keep:] if len(buf) > keep else buf
        pos += len(data)
        now = time.time()
        if on_progress and now - last > 0.4:
            last = now
            on_progress((pos - start) / total, count, now - t0)
    if on_progress:
        on_progress(1.0 if not (cancel is not None and cancel.is_set()) else (pos - start) / total, count,
                    time.time() - t0)
    return found


def best_name(rec: Rec):
    """Предпочитаем Win32-имя (ns 1/3) перед DOS (2)."""
    order = {1: 0, 3: 0, 0: 1, 2: 2}
    return sorted(rec.names, key=lambda n: order.get(n[2], 3))[0]


def build_rows(found: dict[int, Rec], cluster: int):
    """dict записей -> строки для индекса (parents-first). Возвращает список кортежей
    (id, parent, name, path, inode, is_dir, deleted, realloc, size, mtime, sys, meta)."""
    dirs = {r.recno for r in found.values() if r.is_dir}
    path_cache: dict[int, str] = {}

    def dir_path(recno: int, depth=0) -> str:
        if recno == 5:
            return ""
        if recno in path_cache:
            return path_cache[recno]
        rec = found.get(recno)
        if rec is None or not rec.is_dir or depth > 64:
            p = f"{LOST_ROOT}/Папка {recno}"
        else:
            parent, name, _ns = best_name(rec)
            pp = dir_path(parent, depth + 1)
            p = (pp + "/" if pp else "") + name
        path_cache[recno] = p
        return p

    items = []
    for rec in found.values():
        if rec.recno < 24 and rec.recno != 5:
            continue          # служебные файлы NTFS ($MFT, $Bitmap, …)
        if rec.recno == 5:
            continue
        if not rec.is_dir and not rec.has_data:
            continue
        parent, name, _ns = best_name(rec)
        if name.startswith("$") and parent == 5:
            continue
        ppath = dir_path(parent)
        path = (ppath + "/" if ppath else "") + name
        items.append((path, rec, ppath))
    items.sort(key=lambda t: (t[0].count("/"), t[0].lower()))
    ids: dict[str, int] = {}
    rows = []
    nid = 1

    def ensure_dir(path: str) -> int:
        """Создать недостающие (виртуальные) папки пути."""
        nonlocal nid
        if path == "":
            return 0
        if path in ids:
            return ids[path]
        parent_path, _, base = path.rpartition("/")
        pid = ensure_dir(parent_path)
        ids[path] = nid
        rows.append((nid, pid, base, path, "", 1, 0, 0, 0, 0, 0, ""))
        nid += 1
        return ids[path]

    for path, rec, ppath in items:
        pid = ensure_dir(ppath)
        base = path.rpartition("/")[2]
        if rec.is_dir:
            if path in ids:   # папку уже создали как родителя — просто обновим метаданные не будем
                continue
            ids[path] = nid
            rows.append((nid, pid, base, path, f"{rec.recno}-144", 1, int(not rec.in_use), 0, 0, rec.mtime, 0, ""))
            nid += 1
            continue
        meta = {"t": "ntfs", "size": rec.size}
        if rec.flags & 0x4001:
            meta["bad"] = "сжат" if rec.flags & 1 else "зашифрован"
        if rec.resident is not None:
            meta["res"] = rec.resident.hex()
        elif rec.runs:
            meta["runs"] = [[(-1 if lcn is None else lcn), ln] for lcn, ln in rec.runs]
        else:
            meta["runs"] = []
        rows.append((nid, pid, base, path, f"{rec.recno}-128", 0, int(not rec.in_use), 0, rec.size, rec.mtime, 0,
                     json.dumps(meta, separators=(",", ":"))))
        nid += 1
    return rows


def open_node_stream(f, vol_start: int, cluster: int, meta: dict):
    """Итератор по кускам данных файла, описанного meta."""
    size = meta.get("size", 0)
    if meta.get("t") == "c":        # непрерывный файл (FAT/exFAT): offset внутри тома и размер
        f.seek(vol_start + meta["off"])
        left = size
        while left > 0:
            chunk = f.read(min(left, 1 << 20))
            if not chunk:
                raise RuntimeError("чтение за пределами устройства")
            yield chunk
            left -= len(chunk)
        return
    if "bad" in meta:
        raise RuntimeError(f"файл {meta['bad']}: такие данные пока не восстанавливаются")
    if "res" in meta:
        yield bytes.fromhex(meta["res"])[:size]
        return
    left = size
    for lcn, length in meta.get("runs", []):
        if left <= 0:
            break
        nbytes = min(length * cluster, left)
        if lcn == -1:
            while nbytes > 0:
                n = min(nbytes, 1 << 20)
                yield b"\0" * n
                nbytes -= n
                left -= n
            continue
        off = vol_start + lcn * cluster
        f.seek(off)
        while nbytes > 0:
            n = min(nbytes, 1 << 20)
            chunk = f.read(n)
            if not chunk:
                raise RuntimeError("чтение за пределами устройства")
            yield chunk
            nbytes -= len(chunk)
            left -= len(chunk)


def deep_scan(source: str, vol_offset: int, vol_length: int, sector: int, index, on_progress=None,
              cancel: threading.Event | None = None) -> tuple[bool, str, dict]:
    """Глубокий поиск по тому NTFS: заполняет индекс. Возвращает (успех, ошибка, сведения)."""
    start = vol_offset * sector
    length = vol_length * sector
    try:
        f = open(source, "rb")
    except OSError as e:
        return False, f"не удалось открыть источник: {e.strerror}", {}
    with f:
        if length <= 0:
            try:
                f.seek(0, 2)
                length = f.tell() - start
            except OSError:
                return False, "размер тома неизвестен", {}
        cluster, rec_size, where = boot_geometry(f, start, length)
        found = scan_records(f, start, start + length, rec_size, on_progress, cancel)
    if cancel is not None and cancel.is_set():
        return False, "отменено", {}
    index.clear()
    rows = build_rows(found, cluster)
    for i in range(0, len(rows), 2000):
        index.insert_many(rows[i:i + 2000])
    index.set_meta("cluster", cluster)
    index.set_meta("deep", 1)
    index.set_meta("complete", 1)
    info = {"records": len(found), "files": sum(1 for r in rows if not r[5]), "cluster": cluster, "geometry": where}
    return True, "", info
