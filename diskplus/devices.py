"""Список дисков из lsblk (без прав root) и признаки безопасности."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field

COLS = "NAME,PATH,SIZE,TYPE,MODEL,SERIAL,TRAN,ROTA,FSTYPE,LABEL,MOUNTPOINTS,RM,HOTPLUG,PTTYPE,PARTLABEL,PKNAME,VENDOR"
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var"}
SHOW_VIRTUAL = bool(os.environ.get("DISKPLUS_DEV"))


@dataclass
class Part:
    path: str
    name: str
    size: int
    fstype: str
    label: str
    mounts: list[str]
    children: list["Part"] = field(default_factory=list)

    def all_mounts(self) -> list[str]:
        out = list(self.mounts)
        for c in self.children:
            out += c.all_mounts()
        return out


@dataclass
class Disk:
    path: str
    name: str
    model: str
    serial: str
    vendor: str
    size: int
    tran: str          # sata / nvme / usb / ...
    rotational: bool
    removable: bool
    pttype: str
    parts: list[Part]
    mounts: list[str]
    virtual: bool = False
    rm: bool = False

    @property
    def kind(self) -> str:
        if self.virtual:
            return "Виртуальный"
        if self.tran == "nvme":
            return "NVMe SSD"
        if self.tran in ("usb", "mmc") and self.rm:      # флешки и карты в переходниках часто врут «вращающийся диск»
            return "USB-накопитель"
        if self.rotational:
            return "HDD"
        if self.tran == "usb":
            return "USB-SSD"
        return "SSD"

    @property
    def is_system(self) -> bool:
        """Диск, на котором лежит работающая система."""
        return any(m in SYSTEM_MOUNTS or m.startswith("/boot") for m in self.mounts)

    @property
    def in_use(self) -> bool:
        return bool(self.mounts)

    @property
    def title(self) -> str:
        t = (f"{self.vendor} {self.model}" if self.vendor and self.model and self.vendor.strip() not in self.model
             else self.model or self.name).strip()
        return t or self.name

    @property
    def thresholds(self) -> list[int]:
        """Пороги времени отклика (мс) для блока 128 КиБ (HDD) или 1 МиБ (флеш) по типу накопителя."""
        if self.tran in ("usb", "mmc") and self.rm:
            return [15, 40, 100, 300, 1000]     # флешки и карты: ≥70 МБ/с зелёный, ≥25 МБ/с салатовый, ≥10 МБ/с жёлтый
        if self.rotational and not self.virtual:
            return [5, 20, 50, 150, 500]
        if self.tran in ("usb", "mmc") or self.removable:
            return [15, 40, 100, 300, 1000]     # флешки и карты: ≥70 МБ/с зелёный, ≥25 МБ/с салатовый, ≥10 МБ/с жёлтый
        return [2, 5, 20, 100, 500]

    @property
    def flash_profile(self) -> bool:
        """SSD/NVMe/флешка: другие пороги времени отклика, чем у HDD."""
        return not self.rotational or (self.tran in ("usb", "mmc") and self.rm)


def _part(node: dict) -> Part:
    return Part(
        path=node.get("path") or "/dev/" + node["name"],
        name=node["name"],
        size=int(node.get("size") or 0),
        fstype=node.get("fstype") or "",
        label=node.get("label") or node.get("partlabel") or "",
        mounts=[m for m in (node.get("mountpoints") or []) if m],
        children=[_part(c) for c in node.get("children") or []],
    )


def list_disks() -> list[Disk]:
    try:
        out = subprocess.run(["lsblk", "-J", "-b", "-o", COLS], capture_output=True, text=True, timeout=15).stdout
        nodes = json.loads(out)["blockdevices"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return []
    flat = []
    for n in nodes:   # тестовый диск: loop -> dm (diskplus-*) показываем как один виртуальный диск
        if n.get("type") == "loop":
            dms = [c for c in n.get("children") or [] if c.get("type") == "dm" and c["name"].startswith("diskplus-")]
            flat += dms if SHOW_VIRTUAL and dms else [n]
        else:
            flat.append(n)
    nodes = flat
    disks = []
    for n in nodes:
        name = n["name"]
        typ = n.get("type")
        virtual = False
        if int(n.get("size") or 0) == 0:      # пустой слот картридера
            continue
        if typ == "disk" and not name.startswith("zram"):
            pass
        elif typ == "loop" and SHOW_VIRTUAL and int(n.get("size") or 0) > 0:
            virtual = True
        elif typ in ("dm",) and name.startswith("diskplus-"):
            virtual = True
        else:
            continue
        parts = [_part(c) for c in n.get("children") or []]
        mounts = [m for m in (n.get("mountpoints") or []) if m]
        for p in parts:
            mounts += p.all_mounts()
        disks.append(Disk(
            path=n.get("path") or "/dev/" + name, name=name,
            model=(n.get("model") or "").strip(), serial=(n.get("serial") or "").strip(),
            vendor=(n.get("vendor") or "").strip(),
            size=int(n.get("size") or 0), tran=n.get("tran") or "",
            rotational=bool(n.get("rota")), removable=bool(n.get("rm") or n.get("hotplug")),
            pttype=n.get("pttype") or "", parts=parts, mounts=mounts, virtual=virtual, rm=bool(n.get("rm")),
        ))
    return disks


def human_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1000 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") or n >= 100 else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1000
    return f"{n} ТБ"
