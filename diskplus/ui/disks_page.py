"""Страница «Диски»: карточки накопителей с кольцом здоровья."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import devices  # noqa: E402
from . import smart_dialog  # noqa: E402
from .widgets import HealthRing, PartBar  # noqa: E402


class DiskCard(Gtk.Box):
    def __init__(self, page, disk):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_size_request(340, -1)
        self.page, self.disk = page, disk
        self.add_css_class("card")
        self.add_css_class("dp-card")
        top = Gtk.Box(spacing=16)
        self.ring = HealthRing(96)
        top.append(self.ring)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(label=disk.title, xalign=0, wrap=True, ellipsize=3)
        self.title.add_css_class("title-3")
        col.append(self.title)
        sub = [disk.kind, devices.human_size(disk.size)]
        if disk.serial:
            sub.append(disk.serial)
        self.sub = Gtk.Label(label=" · ".join(sub), xalign=0, ellipsize=3)
        self.sub.add_css_class("dim-label")
        self.sub.add_css_class("caption")
        col.append(self.sub)
        pills = Gtk.Box(spacing=6)
        pills.set_margin_top(4)
        self.pill = Gtk.Label(label="Нет данных")
        self.pill.add_css_class("dp-pill")
        self.pill.add_css_class("dp-pill--1")
        pills.append(self.pill)
        if disk.is_system:
            p = Gtk.Label(label="Системный")
            p.add_css_class("dp-pill")
            p.add_css_class("dp-pill--1")
            pills.append(p)
        self.temp = Gtk.Label(label="")
        self.temp.add_css_class("dim-label")
        self.temp.add_css_class("caption")
        pills.append(self.temp)
        col.append(pills)
        top.append(col)
        self.append(top)

        self.reasons = Gtk.Label(xalign=0, wrap=True, visible=False, max_width_chars=44)
        self.reasons.add_css_class("caption")
        self.append(self.reasons)

        bar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.bar = PartBar()
        self.bar.set_parts(disk.parts, disk.size)
        bar_box.append(self.bar)
        desc = []
        for p in disk.parts:
            nm = p.label or p.fstype or "—"
            desc.append(f"{nm} {devices.human_size(p.size)}")
        lab = Gtk.Label(label="  ·  ".join(desc) if desc else "Разделов нет", xalign=0, wrap=True)
        lab.add_css_class("dp-bar-label")
        bar_box.append(lab)
        self.append(bar_box)

        self.append(Gtk.Box(vexpand=True))      # прижимает кнопки к низу: в ряду они выравниваются
        win = page.window
        row = Gtk.Box(spacing=8)
        b_health = Gtk.Button(label="Здоровье", hexpand=True, tooltip_text="Состояние диска и атрибуты SMART")
        b_health.connect("clicked", lambda _b: page.open_smart(disk))
        b_scan = Gtk.Button(label="Проверить", hexpand=True, tooltip_text="Проверка поверхности (только чтение)")
        b_scan.add_css_class("suggested-action")
        b_scan.connect("clicked", lambda _b: win.go_scan(disk))
        more = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="Другие действия")
        pop = Gtk.Popover()
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for label, fn in (("Снять образ…", lambda: win.go_image(disk)),
                          ("Восстановить файлы…", lambda: (win.recover_page.open_disk(disk), win.select("recover"))),
                          ("Клонировать диск…", lambda: win.go_clone(disk)),
                          ("Резервная копия…", lambda: win.select("backup"))):
            bt = Gtk.Button(label=label)
            bt.add_css_class("flat")
            bt.get_child().set_halign(Gtk.Align.START)
            bt.connect("clicked", lambda _b, fn=fn: (pop.popdown(), fn()))
            pbox.append(bt)
        pop.set_child(pbox)
        more.set_popover(pop)
        row.append(b_health)
        row.append(b_scan)
        row.append(more)
        self.append(row)
        self.refresh()

    def refresh(self):
        st = self.page.store
        h = st.health.get(self.disk.path)
        if self.disk.virtual and h is None:
            self.ring.set_health(None, -1)
            self.pill.set_label("Тестовый диск")
            return
        if h is None:
            self.ring.set_health(None, -1)
            self.pill.set_label("Нужен доступ" if not st.helper_ready else "Читаю…")
            self.reasons.set_visible(False)
            return
        self.ring.set_health(h.score, h.level)
        self.pill.set_label(h.label)
        for lvl in (-1, 0, 1, 2, 3):
            self.pill.remove_css_class(f"dp-pill-{lvl}")
        self.pill.add_css_class(f"dp-pill-{h.level}")
        info = st.smart.get(self.disk.path)
        t = []
        if info and info.temp is not None:
            t.append(f"{info.temp} °C")
        if info and info.power_on_hours is not None:
            t.append(f"{info.power_on_hours:,} ч".replace(",", " "))
        self.temp.set_label("  ·  ".join(t))
        if h.reasons:
            self.reasons.set_label("\n".join("• " + r for r in h.reasons[:3]))
            self.reasons.set_visible(True)
            self.reasons.add_css_class("warning" if h.level < 3 else "error")
        else:
            self.reasons.set_visible(False)


class DisksPage(Gtk.Box):
    def __init__(self, window, store):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.store = window, store
        self.cards: dict[str, DiskCard] = {}
        self.banner = Adw.Banner(title="Для проверки здоровья дисков нужен доступ администратора",
                                 button_label="Разрешить", revealed=True)
        self.banner.connect("button-clicked", lambda *_: self.grant())
        self.append(self.banner)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=16,
                                row_spacing=16, min_children_per_line=1, max_children_per_line=4,
                                valign=Gtk.Align.START)
        for m in ("start", "end", "top", "bottom"):
            getattr(self.flow, f"set_margin_{m}")(18)
        sc.set_child(self.flow)
        self.append(sc)
        store.connect(self._on_event)
        self.rebuild()

    def grant(self):
        self.banner.set_button_label("Ожидаю подтверждения…")
        self.banner.set_sensitive(False)

        def ready():
            self.banner.set_sensitive(True)

        def fail(msg):
            self.banner.set_sensitive(True)
            self.banner.set_button_label("Разрешить")
            self.banner.set_title(msg)

        self.store.ensure_helper(ready, fail)

    def rebuild(self):
        while (c := self.flow.get_first_child()) is not None:
            self.flow.remove(c)
        self.cards.clear()
        for d in self.store.disks:
            card = DiskCard(self, d)
            self.cards[d.path] = card
            clamp = Adw.Clamp(maximum_size=460, tightening_threshold=340)   # ограничивает ширину карточки: иначе сетка не соберётся
            clamp.set_child(card)
            self.flow.append(clamp)

    def open_smart(self, disk):
        if not self.store.helper_ready:
            self.grant()
            return
        smart_dialog.show(self.window, self.store, disk)

    def _on_event(self, ev, payload):
        if ev == "disks":
            self.rebuild()
        elif ev == "smart" and payload in self.cards:
            self.cards[payload].refresh()
        elif ev == "helper":
            self.banner.set_revealed(not payload)
            for c in self.cards.values():
                c.refresh()
