"""Планировщик раскладки разделов клона.

Раздел клона идёт подряд с выравниванием 1 МиБ. Размер можно только увеличивать (уменьшать файловую систему
мы пока не умеем), поэтому минимальный размер каждого раздела — размер оригинала. Растягивая раздел, мы сдвигаем
следующие; свободное место остаётся в конце диска."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

ALIGN = 2048              # секторов в МиБ
SECTOR = 512
GPT_TAIL = 34             # резерв под резервную копию GPT


@dataclass
class SrcPart:
    node: str
    number: int
    start: int
    size: int            # секторов
    type: str = ""
    name: str = ""
    fs: str = ""
    label: str = ""
    mounts: list = field(default_factory=list)

    @property
    def mib(self) -> int:
        return self.size * SECTOR // (1 << 20)


@dataclass
class PlanPart:
    src: SrcPart
    size: int            # секторов (>= src.size)
    start: int = 0
    enabled: bool = True


def read_source(path: str) -> tuple[str, list[SrcPart]]:
    """Таблица разделов источника: (label gpt/dos, список разделов) — через lsblk (без прав root)."""
    out = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,PARTTYPE,PARTLABEL,PTTYPE,MOUNTPOINTS,START",
                          path], capture_output=True, text=True, timeout=20).stdout
    try:
        d = json.loads(out)["blockdevices"][0]
    except (ValueError, KeyError, IndexError):
        return "", []
    parts = []
    for i, c in enumerate(d.get("children") or [], 1):
        if c.get("type") != "part":
            continue
        mounts = [m for m in (c.get("mountpoints") or []) if m]
        parts.append(SrcPart(c["path"], i, int(c.get("start") or 0), int(c["size"]) // SECTOR, c.get("parttype") or "",
                             c.get("partlabel") or "", c.get("fstype") or "", c.get("label") or "", mounts))
    return d.get("pttype") or "", parts


def align_up(x: int, a: int = ALIGN) -> int:
    return -(-x // a) * a


class Layout:
    """Раскладка клона на диске назначения."""

    def __init__(self, parts: list[SrcPart], target_sectors: int, label: str = "gpt"):
        self.target = target_sectors
        self.label = label
        self.parts = [PlanPart(p, p.size) for p in parts]
        self.recalc()

    @property
    def usable_end(self) -> int:
        return self.target - (GPT_TAIL if self.label == "gpt" else 0)

    def enabled(self) -> list[PlanPart]:
        return [p for p in self.parts if p.enabled]

    def recalc(self):
        pos = ALIGN
        for p in self.enabled():
            p.start = pos
            pos = align_up(p.start + p.size)
        self.end = pos

    @property
    def free(self) -> int:
        return self.usable_end - self.end

    @property
    def fits(self) -> bool:
        return self.end <= self.usable_end

    def min_total(self) -> int:
        return ALIGN + sum(align_up(p.src.size) for p in self.enabled())

    def resize(self, idx: int, new_size: int) -> int:
        """Изменить размер раздела idx; возвращает фактический размер (в пределах [оригинал, остаток диска])."""
        p = self.parts[idx]
        others = sum(align_up(q.size) for q in self.enabled() if q is not p)
        room = self.usable_end - ALIGN - others
        new_size = max(p.src.size, min(align_up(new_size), room - room % ALIGN if room > 0 else p.src.size))
        p.size = new_size
        self.recalc()
        return new_size

    # --- быстрые режимы
    def mode_asis(self):
        for p in self.parts:
            p.size = p.src.size
        self.recalc()

    def mode_stretch_last(self):
        """Все разделы как есть, последний включённый тянется до конца диска."""
        self.mode_asis()
        en = self.enabled()
        if en:
            others = sum(align_up(q.size) for q in en[:-1])
            room = self.usable_end - ALIGN - others
            en[-1].size = max(en[-1].src.size, room - room % ALIGN)
            self.recalc()

    def mode_stretch_largest(self):
        """Все разделы как есть, самый большой растягивается на всё свободное место (для Windows это C:, а не «Восстановление»)."""
        self.mode_asis()
        en = self.enabled()
        if not en:
            return
        big = max(en, key=lambda p: p.src.size)
        others = sum(align_up(q.size) for q in en if q is not big)
        room = self.usable_end - ALIGN - others
        big.size = max(big.src.size, room - room % ALIGN)
        self.recalc()

    def mode_proportional(self):
        """Свободное место делится между разделами пропорционально их размеру (кроме загрузочных до 1 ГиБ)."""
        self.mode_asis()
        en = self.enabled()
        grow = [p for p in en if p.src.size * SECTOR > (1 << 30)] or en[-1:]
        extra = self.free
        total = sum(p.src.size for p in grow)
        if extra > 0 and total:
            for p in grow:
                p.size = p.src.size + align_up(extra * p.src.size // total, ALIGN) - ALIGN
                p.size = max(p.src.size, p.size)
            self.recalc()
            while not self.fits:
                grow[-1].size -= ALIGN
                self.recalc()

    def to_request(self) -> list[dict]:
        return [{"src": p.src.node, "start": p.start, "size": p.size} for p in self.enabled()]
