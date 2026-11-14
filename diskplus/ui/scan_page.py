"""Проверка поверхности: чтение диска с замером времени каждого блока (только чтение)."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402

from .. import devices  # noqa: E402
from .widgets import BUCKET_COLORS, HealthRing, ScanMap, SmoothBar, SpeedGraph, bucket_color, is_dark, map_panel  # noqa: E402

TH_HDD = [5, 20, 50, 150, 500]
TH_FLASH = [2, 5, 20, 100, 500]
BLOCKS = [(65536, "64 КБ"), (131072, "128 КБ"), (262144, "256 КБ"), (524288, "512 КБ"),
          (1048576, "1 МБ"), (4194304, "4 МБ")]


def fmt_time(sec):
    if sec is None or sec < 0:
        return "—"
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Stat(Gtk.Box):
    def __init__(self, title):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("dp-stat")
        self.set_hexpand(True)
        t = Gtk.Label(label=title, xalign=0)
        t.add_css_class("caption")
        t.add_css_class("dim-label")
        self.num = Gtk.Label(label="—", xalign=0)
        self.num.add_css_class("dp-stat-num")
        self.num.set_ellipsize(3)
        self.append(t)
        self.append(self.num)

    def set(self, v):
        self.num.set_label(v)


class ScanPage(Gtk.Box):
    def __init__(self, window, store):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.window, self.store = window, store
        self.state = "idle"       # idle / running / paused
        self.scan_id = None
        self.th = TH_HDD
        self.total = 0
        self.max_speed = 0.0
        self.last_result = None
        self.log_count = 0
        self._log_opened = False

        # ---------- левая колонка: настройки ----------
        left = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=False)
        left.set_size_request(330, -1)
        self.left = left
        left.add_css_class("dp-control-pane")
        lbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("start", "end", "top", "bottom"):
            getattr(lbox, f"set_margin_{m}")(24)
        left.set_child(lbox)

        g = Adw.PreferencesGroup(title="Что проверяем")
        self.disk_row = Adw.ComboRow(title="Диск")
        self.disk_row.connect("notify::selected", self._on_disk_changed)
        g.add(self.disk_row)
        self.start_row = Adw.SpinRow.new_with_range(0, 99, 1)
        self.start_row.set_title("Начало, %")
        self.start_row.set_value(0)
        self.end_row = Adw.SpinRow.new_with_range(1, 100, 1)
        self.end_row.set_title("Конец, %")
        self.end_row.set_value(100)
        g.add(self.start_row)
        g.add(self.end_row)
        self.block_row = Adw.ComboRow(title="Размер блока", model=Gtk.StringList.new([b[1] for b in BLOCKS]))
        self.block_row.set_selected(1)
        g.add(self.block_row)
        self.dir_row = Adw.ComboRow(title="Направление", model=Gtk.StringList.new(["Вперёд", "Назад"]))
        g.add(self.dir_row)
        lbox.append(g)

        note = Gtk.Label(wrap=True, xalign=0,
                         label="Только чтение. Данные и разметка диска не изменяются.")
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        lbox.append(note)

        btns = Gtk.Box(spacing=8)
        self.btn_start = Gtk.Button(label="Начать проверку", hexpand=True)
        self.btn_start.add_css_class("suggested-action")
        self.btn_start.add_css_class("pill")
        self.btn_start.connect("clicked", lambda _b: self.on_start())
        self.btn_pause = Gtk.Button(label="Пауза", visible=False)
        self.btn_pause.connect("clicked", lambda _b: self.on_pause())
        self.btn_stop = Gtk.Button(label="Стоп", visible=False)
        self.btn_stop.add_css_class("destructive-action")
        self.btn_stop.connect("clicked", lambda _b: self.on_stop())
        btns.append(self.btn_start)
        btns.append(self.btn_pause)
        btns.append(self.btn_stop)
        lbox.append(btns)

        self.result_group = Adw.PreferencesGroup(title="Итог", visible=False)
        rbox = Gtk.Box(spacing=14)
        rbox.set_margin_top(6)
        rbox.set_margin_bottom(6)
        self.ring = HealthRing(84)
        rbox.append(self.ring)
        rcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
        self.res_title = Gtk.Label(xalign=0)
        self.res_title.add_css_class("title-3")
        self.res_text = Gtk.Label(xalign=0, wrap=True)
        self.res_text.add_css_class("caption")
        self.res_text.add_css_class("dim-label")
        rcol.append(self.res_title)
        rcol.append(self.res_text)
        rbox.append(rcol)
        self.result_group.add(rbox)
        lbox.append(self.result_group)

        # ---------- правая часть ----------
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, hexpand=True)
        for m in ("start", "end", "top", "bottom"):
            getattr(right, f"set_margin_{m}")(24)
        self.right = right

        stats = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=10,
                            row_spacing=10, min_children_per_line=2, max_children_per_line=5)
        self.st_progress = Stat("Готово")
        self.st_speed = Stat("Скорость")
        self.st_avg = Stat("Средняя / максимум")
        self.st_left = Stat("Осталось")
        self.st_pos = Stat("Позиция (LBA)")
        for s in (self.st_progress, self.st_speed, self.st_avg, self.st_left, self.st_pos):
            stats.append(s)
        right.append(stats)

        self.progress = SmoothBar()
        right.append(self.progress)

        frame = Gtk.Frame()
        self.map = ScanMap()
        panel, self.map_empty = map_panel(self.map, "Проверка не запущена", "Здесь появится карта прочитанных блоков")
        frame.set_child(panel)
        frame.set_vexpand(True)
        right.append(frame)

        self.legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False, column_spacing=18,
                                  row_spacing=6, min_children_per_line=2, max_children_per_line=7)
        self.legend_nums: list[Gtk.Label] = []
        self.legend_names: list[Gtk.Label] = []
        for i in range(7):
            b = Gtk.Box(spacing=7)
            dot = Gtk.DrawingArea()
            dot.set_content_width(12)
            dot.set_content_height(12)
            dot.set_valign(Gtk.Align.CENTER)
            dot.set_draw_func(lambda _a, cr, w, h, i=i: self._dot(cr, w, h, i))
            nm = Gtk.Label(xalign=0)
            nm.add_css_class("caption")
            n = Gtk.Label(label="0", xalign=0)
            n.add_css_class("caption")
            n.add_css_class("dp-mono")
            b.append(dot)
            b.append(nm)
            b.append(n)
            self.legend_names.append(nm)
            self.legend_nums.append(n)
            self.legend.append(b)
        right.append(self.legend)
        self._set_legend_names()

        self.graph = SpeedGraph()
        right.append(self.graph)

        exp = Gtk.Expander(label="Журнал: медленные и нечитаемые блоки")
        self.logbuf = Gtk.TextBuffer()
        tv = Gtk.TextView(buffer=self.logbuf, editable=False, cursor_visible=False, monospace=True)
        tv.set_top_margin(6)
        tv.set_left_margin(8)
        lsc = Gtk.ScrolledWindow(min_content_height=70, max_content_height=110, propagate_natural_height=True)
        lsc.set_child(tv)
        exp.set_child(lsc)
        self.log_expander = exp
        right.append(exp)

        self.append(left)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        rsc = Gtk.ScrolledWindow(hexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        rsc.set_child(right)
        self.rsc = rsc
        self.append(rsc)

        store.connect(self._on_store)
        self.reload_disks()

    # ----- адаптивность: вызывается окном -----
    def set_narrow(self, narrow):
        self.set_orientation(Gtk.Orientation.VERTICAL if narrow else Gtk.Orientation.HORIZONTAL)
        self.left.set_size_request(-1 if narrow else 330, -1)
        self.left.set_vexpand(False)
        self.rsc.set_vexpand(True)
        self.map.set_size_request(200, 220 if narrow else 160)

    # ----- вспомогательное -----
    def _dot(self, cr, w, h, i):
        r, g, b = bucket_color(i)
        cr.set_source_rgb(r, g, b)
        cr.arc(w / 2, h / 2, 5.5, 0, 6.2832)
        cr.fill()

    def _set_legend_names(self):
        t = self.th
        names = [f"< {t[0]} мс", f"{t[0]}–{t[1]}", f"{t[1]}–{t[2]}", f"{t[2]}–{t[3]}", f"{t[3]}–{t[4]}",
                 f"> {t[4]} мс", "Ошибка"]
        for lab, nm in zip(self.legend_names, names):
            lab.set_label(nm)

    def reload_disks(self):
        keep = self.selected_disk().path if self.selected_disk() else None
        self.disks = list(self.store.disks)
        labels = [f"{d.title} · {devices.human_size(d.size)}" for d in self.disks]
        self.disk_row.set_model(Gtk.StringList.new(labels or ["Дисков не найдено"]))
        if keep:
            for i, d in enumerate(self.disks):
                if d.path == keep:
                    self.disk_row.set_selected(i)
        self._on_disk_changed()

    def selected_disk(self):
        i = self.disk_row.get_selected()
        disks = getattr(self, "disks", [])
        return disks[i] if 0 <= i < len(disks) else None

    def select_disk(self, disk):
        for i, d in enumerate(self.disks):
            if d.path == disk.path:
                self.disk_row.set_selected(i)

    def _on_disk_changed(self, *_):
        d = self.selected_disk()
        if not d or self.state != "idle":
            return
        self.th = d.thresholds
        self._set_legend_names()
        self.block_row.set_selected(4 if d.flash_profile else 1)

    def _on_store(self, ev, payload):
        if ev == "disks" and self.state == "idle":
            self.reload_disks()

    # ----- управление -----
    def set_state(self, st):
        self.state = st
        self.map_empty.set_visible(st == "idle" and not len(self.map.codes))
        idle = st == "idle"
        self.btn_start.set_visible(idle)
        self.btn_pause.set_visible(not idle)
        self.btn_stop.set_visible(not idle)
        self.btn_pause.set_label("Продолжить" if st == "paused" else "Пауза")
        for w in (self.disk_row, self.start_row, self.end_row, self.block_row, self.dir_row):
            w.set_sensitive(idle)

    def on_start(self):
        d = self.selected_disk()
        if not d:
            return
        self.store.ensure_helper(lambda: self._go(d), lambda msg: self.window.toast(msg))

    def _go(self, d):
        self.th = d.thresholds
        self._set_legend_names()
        sp, ep = int(self.start_row.get_value()), int(self.end_row.get_value())
        if ep <= sp:
            self.window.toast("Конец диапазона должен быть больше начала")
            return
        start = int(d.size * sp / 100) // 4096 * 4096
        end = int(d.size * ep / 100)
        block = BLOCKS[self.block_row.get_selected()][0]
        self.map.reset(0)
        self.graph.clear()
        self.max_speed = 0.0
        self.logbuf.set_text("")
        self.log_count = 0
        self._log_opened = False
        self.log_expander.set_expanded(False)
        self.result_group.set_visible(False)
        for n in self.legend_nums:
            n.set_label("0")
        self.progress.set_fraction(0)
        self.disk_scanned = d
        self._start_b = start
        self._sp = 0
        self.set_state("running")
        self.scan_id = self.store.helper.request(
            "scan", self._on_msg, dev=d.path, start=start, end=end, block=block, th=self.th, cells=24000,
            reverse=self.dir_row.get_selected() == 1)

    def on_pause(self):
        cmd = "resume" if self.state == "paused" else "pause"
        self.store.helper.request(cmd, None, target=self.scan_id)
        self.set_state("running" if self.state == "paused" else "paused")

    def on_stop(self):
        self.store.helper.request("stop", None, target=self.scan_id)

    # ----- события от помощника -----
    def _on_msg(self, m):
        ev = m.get("ev")
        if ev == "start":
            self.total = m["total"]
            self.map.reset(m["cells"])
            self.window.toast("Прямой доступ к диску включён" if m.get("direct") else
                              "Внимание: диск читается через кэш системы, замеры менее точны", quiet=m.get("direct"))
        elif ev == "progress":
            self._progress(m)
        elif ev == "done":
            self.map.set_head(None)
            self._finish(m)
        elif ev == "err":
            self.set_state("idle")
            self.window.toast(m.get("msg", "Ошибка сканирования"))

    def _progress(self, m):
        if "map" in m:
            self.map.update(m["map_from"], m["map"])
        b = m["buckets"]
        total_blocks = sum(b) or 1
        for i, n in enumerate(self.legend_nums):
            n.set_label(f"{b[i]}" + (f"  ({b[i] / total_blocks * 100:.1f}%)" if b[i] and i > 0 else ""))
        done = m["bytes"]
        frac = min(1.0, done / self.total) if self.total else 0
        self.progress.set_fraction(frac)
        self.st_progress.set(f"{frac * 100:.1f} %")
        mbps = m["mbps"]
        if mbps > 0 and self.state == "running":
            self.graph.add(mbps)
            self.max_speed = max(self.max_speed, mbps)
        self._sp = mbps if not getattr(self, "_sp", 0) else self._sp * 0.5 + mbps * 0.5     # сглаженная скорость в цифрах
        self.st_speed.set(f"{self._sp:.0f} МБ/с")
        el = m["elapsed"]
        avg = done / el / 1e6 if el > 0 else 0
        self.st_avg.set(f"{avg:.0f} / {self.max_speed:.0f}")
        left = (self.total - done) / (avg * 1e6) if avg > 0 else None
        self.st_left.set(fmt_time(left))
        if m.get("pos") is not None:
            self.st_pos.set(f"{m['pos'] // 512:,}".replace(",", " "))
            if self.total and self.map.codes.size:
                start = getattr(self, "_start_b", 0)
                self.map.set_head(int((m["pos"] - start) / max(self.total, 1) * self.map.codes.size))
        for off, ms, code in m.get("log", []):
            if self.log_count < 400:
                kind = "ОШИБКА" if code == 6 else f"{ms:.0f} мс"
                self.logbuf.insert(self.logbuf.get_end_iter(), f"LBA {off // 512:>12,}   {kind}\n".replace(",", " "))
                self.log_count += 1
        if self.log_count and not self._log_opened:
            self._log_opened = True
            self.log_expander.set_expanded(True)

    def _finish(self, m):
        stopped = m["reason"] == "stopped"
        d = self.disk_scanned
        self.set_state("idle")
        if m["bytes"] <= 0:
            return
        res = {"buckets": m["buckets"], "bytes": m["bytes"], "complete": not stopped}
        self.store.scan_result[d.path] = res
        self.store.recompute_health(d.path)
        h = self.store.health.get(d.path)
        b = m["buckets"]
        if h is None:   # нет SMART (виртуальный диск) — оцениваем только по сканированию
            from .. import smart
            h = smart.health(smart.SmartInfo(ok=False), res)
        self.ring.set_health(h.score, h.level)
        self.res_title.set_label(h.label + (" (проверка остановлена)" if stopped else ""))
        lines = [f"Блоков прочитано: {sum(b)}"]
        if b[6]:
            lines.append(f"Нечитаемых: {b[6]}")
        if b[5]:
            lines.append(f"Очень медленных: {b[5]}")
        if not b[6] and not b[5]:
            lines.append("Ошибок и серьёзных задержек нет")
        self.res_text.set_label("\n".join(lines))
        self.result_group.set_visible(True)
        self.store.emit("smart", d.path)
