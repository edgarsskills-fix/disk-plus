"""Разбор вывода smartctl --json и оценка «здоровья» диска в процентах.

Оценка Disk++ — это эвристика поверх SMART и результатов сканирования, а не прямое
измерение: диск может умереть и при 100 %, а при низком проценте проработать годы.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Attr:
    id: int
    name: str
    value: int
    worst: int
    thresh: int
    raw: int
    raw_text: str
    prefail: bool
    failing: bool
    note: str = ""      # пояснение для человека
    level: int = 0      # 0 норма, 1 внимание, 2 плохо


@dataclass
class SmartInfo:
    ok: bool = False                 # удалось прочитать SMART
    protocol: str = ""               # ATA / NVMe / SCSI
    passed: bool | None = None       # общий вердикт SMART
    temp: int | None = None
    power_on_hours: int | None = None
    power_cycles: int | None = None
    attrs: list[Attr] = field(default_factory=list)    # ATA
    nvme: dict = field(default_factory=dict)           # NVMe health log
    selftest: dict = field(default_factory=dict)       # статус самотеста
    message: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""
    rotation: int | None = None
    dtype: str | None = None


@dataclass
class Health:
    score: int | None             # 0–100, None если оценить нечем
    label: str
    level: int                    # 0 хорошо, 1 внимание, 2 плохо, 3 критично, -1 нет данных
    reasons: list[str] = field(default_factory=list)


ATTR_NOTES = {
    5: "Переназначенные секторы: диск уже заменил часть плохих запасными",
    9: "Наработка в часах",
    10: "Повторные раскрутки шпинделя",
    12: "Количество включений",
    171: "Ошибки программирования флеш-памяти",
    172: "Ошибки стирания флеш-памяти",
    177: "Износ SSD",
    181: "Ошибки программирования",
    182: "Ошибки стирания",
    184: "Сквозные ошибки передачи (end-to-end)",
    187: "Неисправимые ошибки чтения",
    188: "Тайм-ауты команд",
    190: "Температура воздуха",
    191: "Ударные нагрузки",
    194: "Температура",
    196: "События переназначения секторов",
    197: "Нестабильные секторы (кандидаты на переназначение)",
    198: "Неисправимые секторы при автопроверке",
    199: "Ошибки CRC интерфейса (обычно кабель или разъём)",
    200: "Ошибки записи",
    231: "Остаток ресурса SSD",
    233: "Остаток ресурса флеш-памяти",
}


def _raw(a: dict) -> int:
    r = a.get("raw", {})
    v = int(r.get("value", 0) or 0)
    return v if v < 10_000_000 else v & 0xFFFF   # у ряда вендоров в raw лежат упакованные поля


def parse(data: dict, dtype: str | None = None, unavailable: bool = False) -> SmartInfo:
    s = SmartInfo(dtype=dtype)
    s.model = data.get("model_name", "") or ""
    s.serial = data.get("serial_number", "") or ""
    s.firmware = data.get("firmware_version", "") or ""
    s.rotation = data.get("rotation_rate")
    dev = data.get("device", {}) or {}
    s.protocol = dev.get("protocol", "") or ""
    msgs = (data.get("smartctl", {}) or {}).get("messages") or []
    if unavailable or not (data.get("ata_smart_attributes") or data.get("nvme_smart_health_information_log")
                           or "smart_status" in data):
        s.ok = False
        s.message = (msgs[0].get("string") if msgs else "") or "SMART недоступен для этого устройства"
        if "USB bridge" in s.message or "Unknown USB" in s.message:
            s.message = "USB-переходник не пропускает SMART"
        return s
    # Флешки и карты памяти в переходниках отдают только формальное «SMART исправен» без счётчиков:
    # называть такое «Отлично, 100 %» нельзя, настоящую оценку даст проверка поверхности.
    scsi_only = (s.protocol == "SCSI" and not data.get("ata_smart_attributes")
                 and not data.get("nvme_smart_health_information_log") and not data.get("scsi_error_counter_log")
                 and not data.get("scsi_grown_defect_list"))
    if scsi_only:
        s.ok = False
        s.message = "Устройство не сообщает диагностику (флешка или карта памяти). Оценку даст проверка поверхности."
        return s
    s.ok = True
    st = data.get("smart_status") or {}
    s.passed = st.get("passed")
    t = data.get("temperature") or {}
    s.temp = t.get("current")
    s.power_on_hours = (data.get("power_on_time") or {}).get("hours")
    s.power_cycles = data.get("power_cycle_count")
    nv = data.get("nvme_smart_health_information_log")
    if nv:
        s.nvme = nv
        s.protocol = "NVMe"
        s.power_cycles = nv.get("power_cycles", s.power_cycles)
        if s.temp is None:
            s.temp = nv.get("temperature")
    tbl = (data.get("ata_smart_attributes") or {}).get("table") or []
    for a in tbl:
        raw = _raw(a)
        failing = bool(a.get("when_failed"))
        attr = Attr(id=a["id"], name=a.get("name", "").replace("_", " "), value=a.get("value", 0),
                    worst=a.get("worst", 0), thresh=a.get("thresh", 0), raw=raw,
                    raw_text=(a.get("raw") or {}).get("string", str(raw)),
                    prefail=bool((a.get("flags") or {}).get("prefailure")), failing=failing,
                    note=ATTR_NOTES.get(a["id"], ""))
        if failing:
            attr.level = 2
        elif a["id"] in (5, 187, 197, 198, 196, 200, 184) and raw > 0:
            attr.level = 2 if a["id"] in (197, 198, 187) else 1
        elif a["id"] == 199 and raw > 0:
            attr.level = 1
        elif attr.thresh and attr.value <= attr.thresh + 5 and attr.prefail:
            attr.level = 1
        s.attrs.append(attr)
    ata_st = ((data.get("ata_smart_data") or {}).get("self_test") or {}).get("status")
    if ata_st:
        s.selftest = ata_st
    return s


def _attr(s: SmartInfo, aid: int) -> int:
    for a in s.attrs:
        if a.id == aid:
            return a.raw
    return 0


def health(s: SmartInfo, scan: dict | None = None) -> Health:
    """Оценка в процентах. scan = {'buckets': [..7..]} из сканирования поверхности (необязательно)."""
    if not s.ok and not scan:
        return Health(None, "Нет данных", -1, [s.message or "SMART недоступен"])
    score = 100.0
    reasons: list[str] = []
    cap = 100

    if s.ok:
        if s.passed is False:
            cap = min(cap, 10)
            reasons.append("Диск сам сообщает о неисправности (SMART: FAILED)")
        if s.nvme:
            n = s.nvme
            used = n.get("percentage_used", 0) or 0
            if used:
                score -= min(60.0, used * 0.6)
                if used >= 80:
                    reasons.append(f"Выработан ресурс накопителя: {used} %")
            if n.get("critical_warning"):
                cap = min(cap, 20)
                reasons.append("Контроллер выставил критическое предупреждение")
            spare, thr = n.get("available_spare"), n.get("available_spare_threshold")
            if spare is not None and thr is not None and spare <= thr:
                score -= 40
                reasons.append(f"Резервные блоки на исходе ({spare} % при пороге {thr} %)")
            if n.get("media_errors", 0) > 0:
                score -= 25
                reasons.append(f"Ошибки носителя: {n['media_errors']}")
        else:
            realloc, pending, unc, crc = _attr(s, 5), _attr(s, 197), _attr(s, 198), _attr(s, 199)
            rep = _attr(s, 187)
            if realloc:
                score -= 8 if realloc <= 10 else 18 if realloc <= 100 else 30
                reasons.append(f"Переназначено секторов: {realloc}")
            bad = pending + unc + rep
            if bad:
                score -= 25 if bad < 10 else 40
                reasons.append(f"Нестабильных/нечитаемых секторов: {bad}")
            if crc:
                score -= 3 if crc < 20 else 8
                reasons.append(f"Ошибки CRC интерфейса: {crc} (проверьте кабель)")
            for a in s.attrs:
                if a.failing:
                    cap = min(cap, 30)
                    reasons.append(f"Атрибут «{a.name}» ниже порога")
                    break
            for aid in (231, 233, 177):
                for a in s.attrs:
                    if a.id == aid and a.value < 100:
                        score -= min(50.0, (100 - a.value) * 0.6)
        if s.temp is not None:
            if s.temp >= 65:
                score -= 15
                reasons.append(f"Очень высокая температура: {s.temp} °C")
            elif s.temp >= 55 and not s.nvme:
                score -= 5
                reasons.append(f"Повышенная температура: {s.temp} °C")
        poh = s.power_on_hours or 0
        if not s.nvme and poh > 60000:
            score -= 10
            reasons.append(f"Большая наработка: {poh} ч")
        elif not s.nvme and poh > 40000:
            score -= 5

    if scan:
        b = scan.get("buckets") or [0] * 7
        total = sum(b) or 1
        err, bad, slow = b[6], b[5], b[4]
        if err:
            frac = err / total
            score -= min(60.0, 15 + frac * 4000)
            cap = min(cap, 50 if frac < 0.001 else 25)
            reasons.append(f"Сканирование: нечитаемых блоков {err}")
        if bad:
            score -= min(25.0, 5 + bad / total * 1000)
            reasons.append(f"Сканирование: очень медленных блоков {bad}")
        if slow and slow / total > 0.01:
            score -= 8
            reasons.append(f"Сканирование: много медленных блоков ({slow / total * 100:.1f} %)")

    final = int(max(0, min(cap, round(score))))
    if final >= 90:
        label, level = "Отлично", 0
    elif final >= 75:
        label, level = "Хорошо", 0
    elif final >= 50:
        label, level = "Внимание", 1
    elif final >= 25:
        label, level = "Плохо", 2
    else:
        label, level = "Критично", 3
    return Health(final, label, level, reasons)


def log2(x):  # noqa: D401 — для тестов
    return math.log2(x)
