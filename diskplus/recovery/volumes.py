"""Поиск томов (разделов и файловых систем) на диске или в образе."""
from __future__ import annotations

import re
import struct
import subprocess
from dataclasses import dataclass

# файловые системы, которые умеет читать Sleuth Kit (дерево папок, удалённые файлы)
TSK_FS = {"ntfs", "fat12", "fat16", "fat32", "exfat", "ext2", "ext3", "ext4", "hfs+", "apfs"}

FS_TITLES = {"ntfs": "NTFS", "fat12": "FAT12", "fat16": "FAT16", "fat32": "FAT32", "exfat": "exFAT", "ext2": "ext2",
             "ext3": "ext3", "ext4": "ext4", "hfs+": "HFS+", "apfs": "APFS", "btrfs": "btrfs", "xfs": "XFS",
             "luks": "LUKS (зашифрован)", "bitlocker": "BitLocker (зашифрован)", "unknown": "не определена"}


@dataclass
class Volume:
    offset: int            # начало, в секторах
    length: int            # длина, в секторах (0 = неизвестно)
    fs: str                # ключ из FS_TITLES
    label: str = ""
    source: str = "table"  # table / whole / scan
    desc: str = ""         # описание из таблицы разделов
    sector: int = 512

    @property
    def supported(self) -> bool:
        return self.fs in TSK_FS

    @property
    def title(self) -> str:
        return FS_TITLES.get(self.fs, self.fs)

    @property
    def size_bytes(self) -> int:
        return self.length * self.sector


def _u(data, off, fmt):
    try:
        return struct.unpack_from(fmt, data, off)[0]
    except struct.error:
        return 0


def sniff(data: bytes, sector: int = 512):
    """Определить ФС по первым байтам тома. data >= 0x10100 нужно только для btrfs; иначе хватает ~4 КиБ.
    Возвращает (fs, label, length_sectors) или None."""
    if len(data) < 2048:
        return None
    if data[3:11] == b"NTFS    ":
        return "ntfs", "", _u(data, 0x28, "<Q")
    if data[3:11] == b"EXFAT   ":
        return "exfat", "", _u(data, 0x48, "<Q")
    if data[3:11] == b"-FVE-FS-":
        return "bitlocker", "", 0
    if data[0:6] == b"LUKS\xba\xbe":
        return "luks", "", 0
    if data[0:4] == b"XFSB":
        blocks, bsz = _u(data, 8, ">Q"), _u(data, 4, ">I")
        return "xfs", "", (blocks * bsz) // sector if bsz else 0
    if data[32:36] == b"NXSB":
        return "apfs", "", 0
    sig = data[510:512] == b"\x55\xaa"
    if sig and data[82:90] == b"FAT32   ":
        total = _u(data, 0x20, "<I") or _u(data, 0x13, "<H")
        return "fat32", data[71:82].decode("latin-1").strip(), total
    if sig and data[54:59] in (b"FAT16", b"FAT12", b"FAT  "):
        total = _u(data, 0x13, "<H") or _u(data, 0x20, "<I")
        kind = "fat12" if data[54:62] == b"FAT12   " else "fat16"
        return kind, data[43:54].decode("latin-1").strip(), total
    if len(data) >= 1090 and _u(data, 1080, "<H") == 0xEF53:
        compat, incompat = _u(data, 1024 + 0x5C, "<I"), _u(data, 1024 + 0x60, "<I")
        fs = "ext4" if incompat & (0x40 | 0x200 | 0x80) else "ext3" if compat & 0x4 else "ext2"
        bsz = 1024 << _u(data, 1024 + 0x18, "<I")
        blocks = _u(data, 1024 + 4, "<I")
        return fs, data[1024 + 0x78:1024 + 0x88].split(b"\0")[0].decode("utf-8", "replace"), blocks * bsz // sector
    if data[1024:1026] in (b"H+", b"HX"):
        return "hfs+", "", 0
    if len(data) >= 0x10048 and data[0x10040:0x10048] == b"_BHRfS_M":
        return "btrfs", "", _u(data, 0x10070, "<Q") // sector
    return None


def read_at(f, offset: int, n: int) -> bytes:
    try:
        f.seek(offset)
        return f.read(n)
    except (OSError, ValueError):
        return b""


def detect_sector_size(path: str) -> int:
    """512 или 4096 по расположению заголовка GPT."""
    try:
        with open(path, "rb") as f:
            if read_at(f, 512, 8) == b"EFI PART":
                return 512
            if read_at(f, 4096, 8) == b"EFI PART":
                return 4096
    except OSError:
        pass
    return 512


def _mmls(path: str, sector: int):
    try:
        out = subprocess.run(["mmls", "-b", str(sector), path], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.*)$", line)
        if not m:
            continue
        slot, start, _end, length, desc = m.groups()
        if slot in ("Meta", "-------") or "Unallocated" in desc or "Table" in desc and slot == "Meta":
            continue
        rows.append((int(start), int(length), desc.strip()))
    return rows


def list_volumes(path: str, sector: int | None = None) -> list[Volume]:
    """Тома из таблицы разделов + файловая система без таблицы (вся «флешка» целиком)."""
    sector = sector or detect_sector_size(path)
    vols: list[Volume] = []
    try:
        f = open(path, "rb")
    except OSError:
        return []
    with f:
        for start, length, desc in _mmls(path, sector):
            head = read_at(f, start * sector, 0x10100)
            s = sniff(head, sector)
            vols.append(Volume(start, length, s[0] if s else "unknown", (s[1] if s else "") or "", "table", desc, sector))
        if not vols:
            head = read_at(f, 0, 0x10100)
            s = sniff(head, sector)
            if s:
                vols.append(Volume(0, s[2], s[0], s[1], "whole", "", sector))
    return vols


def scan_lost(path: str, size: int, known: list[Volume], sector: int = 512, step: int = 1 << 20,
              on_progress=None, cancel=None) -> list[Volume]:
    """Быстрый поиск потерянных разделов: проверка загрузочных секторов через каждые `step` байт
    (плюс типичные старые смещения 63 и 2048 секторов). Не заменяет глубокое сканирование."""
    found: list[Volume] = []
    have = {v.offset for v in known}
    cands = sorted({63 * sector, 32256, 2048 * sector} | set(range(0, size, step)))
    n = len(cands)
    try:
        f = open(path, "rb")
    except OSError:
        return []
    with f:
        for i, off in enumerate(cands):
            if cancel is not None and cancel.is_set():
                break
            if on_progress and i % 256 == 0:
                on_progress(i / n)
            if off // sector in have:
                continue
            head = read_at(f, off, 0x10100 if off % step == 0 else 4096)
            s = sniff(head, sector)
            if s:
                found.append(Volume(off // sector, s[2], s[0], s[1], "scan", "найден поиском", sector))
                have.add(off // sector)
    return found
