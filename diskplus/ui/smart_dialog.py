"""Окно «Здоровье и SMART» для одного диска."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import devices, smart  # noqa: E402
from .widgets import HealthRing  # noqa: E402

NVME_FIELDS = [
    ("critical_warning", "Критические предупреждения", lambda v: "нет" if not v else f"код {v}"),
    ("percentage_used", "Выработано ресурса", lambda v: f"{v} %"),
    ("available_spare", "Резервные блоки", lambda v: f"{v} %"),
    ("media_errors", "Ошибки носителя", str),
    ("num_err_log_entries", "Записей в журнале ошибок", str),
    ("unsafe_shutdowns", "Аварийных отключений питания", str),
    ("power_cycles", "Включений", str),
    ("data_units_read", "Прочитано", lambda v: devices.human_size(v * 512_000)),
    ("data_units_written", "Записано", lambda v: devices.human_size(v * 512_000)),
]


def show(parent, store, disk):
    info = store.smart.get(disk.path)
    h = store.health.get(disk.path)
    dlg = Adw.Dialog()
    dlg.set_title(disk.title)
    dlg.set_content_width(760)
    dlg.set_content_height(720)
    tv = Adw.ToolbarView()
    tv.add_top_bar(Adw.HeaderBar())
    page = Adw.PreferencesPage()

    g = Adw.PreferencesGroup()
    head = Gtk.Box(spacing=18)
    head.set_margin_top(6)
    head.set_margin_bottom(6)
    ring = HealthRing(110)
    ring.set_health(h.score if h else None, h.level if h else -1)
    head.append(ring)
    v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
    t = Gtk.Label(label=h.label if h else "Нет данных", xalign=0)
    t.add_css_class("title-1")
    v.append(t)
    sub = Gtk.Label(xalign=0, wrap=True)
    sub.add_css_class("dim-label")
    if h and h.reasons:
        sub.set_label("\n".join("• " + r for r in h.reasons))
    elif h and h.score is not None:
        sub.set_label("Замечаний нет. Оценка Disk++ — это эвристика на основе SMART, а не гарантия.")
    else:
        sub.set_label(info.message if info and info.message else "SMART пока не прочитан")
    v.append(sub)
    head.append(v)
    g.add(head)
    page.add(g)

    if info and info.ok:
        g2 = Adw.PreferencesGroup(title="Общее")

        def row(title, value):
            r = Adw.ActionRow(title=title)
            lab = Gtk.Label(label=str(value), selectable=True)
            lab.add_css_class("dim-label")
            r.add_suffix(lab)
            g2.add(r)

        row("Модель", info.model or disk.model)
        if info.serial:
            row("Серийный номер", info.serial)
        if info.firmware:
            row("Прошивка", info.firmware)
        row("Статус SMART", "исправен" if info.passed else "НЕИСПРАВЕН" if info.passed is False else "нет данных")
        if info.temp is not None:
            row("Температура", f"{info.temp} °C")
        if info.power_on_hours is not None:
            d = info.power_on_hours // 24
            row("Наработка", f"{info.power_on_hours:,} ч (≈ {d // 365} лет {d % 365 // 30} мес)".replace(",", " "))
        if info.power_cycles is not None:
            row("Включений", f"{info.power_cycles:,}".replace(",", " "))
        page.add(g2)

        if info.nvme:
            g3 = Adw.PreferencesGroup(title="Журнал состояния NVMe")
            for key, title, fmt in NVME_FIELDS:
                if key in info.nvme:
                    r = Adw.ActionRow(title=title)
                    lab = Gtk.Label(label=fmt(info.nvme[key]))
                    lab.add_css_class("dim-label")
                    r.add_suffix(lab)
                    g3.add(r)
            page.add(g3)

        if info.attrs:
            g4 = Adw.PreferencesGroup(title="Атрибуты SMART",
                                      description="Красным и жёлтым помечены значения, на которые стоит обратить внимание")
            for a in info.attrs:
                r = Adw.ActionRow(title=f"{a.id}  {a.name}")
                r.set_title_lines(1)
                r.set_subtitle((a.note + " · " if a.note else "") + f"значение {a.value}, худшее {a.worst}, порог {a.thresh}")
                lab = Gtk.Label(label=a.raw_text.split(" (")[0])
                lab.add_css_class("dp-mono")
                if a.level == 2:
                    lab.add_css_class("error")
                elif a.level == 1:
                    lab.add_css_class("warning")
                else:
                    lab.add_css_class("dim-label")
                r.add_suffix(lab)
                g4.add(r)
            page.add(g4)

        g5 = Adw.PreferencesGroup(title="Самотест диска",
                                  description="Диск проверяет себя сам, не мешая работе. Короткий тест занимает ~2 минуты.")
        for kind, title in (("short", "Короткий тест"), ("long", "Полный тест")):
            r = Adw.ActionRow(title=title, subtitle="Результат появится в атрибутах после завершения")
            b = Gtk.Button(label="Запустить", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, k=kind: _selftest(dlg, store, disk, info, k))
            r.add_suffix(b)
            g5.add(r)
        page.add(g5)

    tv.set_content(page)
    dlg.set_child(tv)
    dlg.present(parent)


def _selftest(dlg, store, disk, info, kind):
    def done(msg):
        txt = "Тест запущен: диск проверяет себя" if msg.get("ev") == "res" and msg["data"].get("rc", 1) in (0, 4) \
            else "Не удалось запустить тест на этом устройстве"
        toast_target = dlg.get_root() or dlg
        try:
            dlg.close()
        except Exception:  # noqa: BLE001
            pass
        store.emit("toast", txt)

    store.helper.request("selftest", done, dev=disk.path, kind=kind, dtype=info.dtype)
