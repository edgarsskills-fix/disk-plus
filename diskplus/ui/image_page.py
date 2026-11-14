"""Образ диска: копирование через ddrescue с живой картой (первыми собираются «лёгкие» области)."""
from __future__ import annotations

import os
import re
import shutil
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import devices  # noqa: E402
from .scan_page import Stat, fmt_time  # noqa: E402
from .widgets import RESCUE_COLORS, ScanMap, SmoothBar, SpeedGraph, _hex, is_dark, map_panel  # noqa: E402

ALLOWED = ("/home/", "/run/media/", "/mnt/", "/media/")
# символ карты helper-а -> код клетки ScanMap (255 = ещё не пробовали)
CODE_OF = {ord("?"): 255, ord("+"): 0, ord("*"): 2, ord("/"): 3, ord("-"): 6}
PHASES = (("copy", "1  Копирование"), ("trim", "2  Обрезка"), ("scrape", "3  Добор"), ("retry", "4  Повторы"))
LEGEND = (("Ещё не пробовали", None), ("Спасено", 0), ("Не обрезано", 2), ("Не добрано", 3), ("Нечитаемо", 6))


def safe_name(text):
    t = re.sub(r"[^\w.\-]+", "_", text, flags=re.UNICODE).strip("._")
    return t or "disk"


def mount_of(path, disks):
    """К какому диску относится путь (по самой длинной точке монтирования)."""
    best, owner = "", None
    for d in disks:
        for m in d.mounts:
            if (path == m or path.startswith(m.rstrip("/") + "/")) and len(m) > len(best):
                best, owner = m, d
    return owner


class ImagePage(Gtk.Box):
    def __init__(self, window, store):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.window, self.store = window, store
        self.state = "idle"
        self.job_id = None
        self.disks = []
        self.dest_dir = os.path.expanduser("~")
        self.total = 0
        self.max_rate = 0.0
        self.image = self.mapfile = None
        self.cur_phase = "copy"

        left = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=False)
        left.set_size_request(330, -1)
        self.left = left
        left.add_css_class("dp-control-pane")
        lbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("start", "end", "top", "bottom"):
            getattr(lbox, f"set_margin_{m}")(24)
        left.set_child(lbox)

        g1 = Adw.PreferencesGroup(title="Откуда")
        self.disk_row = Adw.ComboRow(title="Диск или раздел")
        self.disk_row.connect("notify::selected", self._on_changed)
        g1.add(self.disk_row)
        lbox.append(g1)

        g2 = Adw.PreferencesGroup(title="Куда", description="Образ нельзя сохранять на тот же диск, который спасаем")
        self.dir_row = Adw.ActionRow(title="Папка", subtitle=self.dest_dir)
        self.dir_row.set_subtitle_lines(1)
        pick = Gtk.Button(label="Выбрать…", valign=Gtk.Align.CENTER)
        pick.connect("clicked", lambda _b: self.pick_folder())
        self.dir_row.add_suffix(pick)
        g2.add(self.dir_row)
        self.name_row = Adw.EntryRow(title="Имя образа")
        self.name_row.connect("changed", self._on_changed)
        g2.add(self.name_row)
        self.space_row = Adw.ActionRow(title="Свободное место")
        self.space_lbl = Gtk.Label(label="—", wrap=True, max_width_chars=23, width_chars=18, xalign=1)
        self.space_row.add_suffix(self.space_lbl)
        g2.add(self.space_row)
        lbox.append(g2)

        adv = Adw.ExpanderRow(title="Дополнительно", subtitle="Повторы, добор, направление")
        self.retry_row = Adw.SpinRow.new_with_range(0, 10, 1)
        self.retry_row.set_title("Повторных проходов")
        self.retry_row.set_subtitle("Сколько раз перечитывать плохие секторы после основного прохода")
        adv.add_row(self.retry_row)
        self.noscrape_row = Adw.SwitchRow(title="Пропустить добор",
                                          subtitle="Быстрее, но сложные участки останутся нечитаемыми")
        adv.add_row(self.noscrape_row)
        self.rev_row = Adw.SwitchRow(title="Читать с конца", subtitle="Иногда помогает, если голова диска «застревает»")
        adv.add_row(self.rev_row)
        g3 = Adw.PreferencesGroup()
        g3.add(adv)
        lbox.append(g3)

        self.resume_note = Gtk.Label(wrap=True, xalign=0, visible=False,
                                     label="Найден сохранённый сеанс: копирование продолжится с места остановки, нечитаемые области будут перечитаны.")
        self.resume_note.add_css_class("caption")
        self.resume_note.add_css_class("accent")
        lbox.append(self.resume_note)

        note = Gtk.Label(wrap=True, xalign=0,
                         label="Источник не изменяется. Копирование можно остановить и продолжить позже.")
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        lbox.append(note)

        btns = Gtk.Box(spacing=8)
        self.btn_start = Gtk.Button(label="Начать копирование", hexpand=True)
        self.btn_start.add_css_class("suggested-action")
        self.btn_start.add_css_class("pill")
        self.btn_start.connect("clicked", lambda _b: self.on_start())
        self.btn_pause = Gtk.Button(label="Пауза", visible=False)
        self.btn_pause.connect("clicked", lambda _b: self.on_pause())
        self.btn_stop = Gtk.Button(label="Стоп", visible=False)
        self.btn_stop.add_css_class("destructive-action")
        self.btn_stop.connect("clicked", lambda _b: self.on_stop())
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            btns.append(b)
        lbox.append(btns)

        self.result_group = Adw.PreferencesGroup(title="Итог", visible=False)
        self.result_title = Gtk.Label(xalign=0, wrap=True)
        self.result_title.add_css_class("title-3")
        self.result_text = Gtk.Label(xalign=0, wrap=True)
        self.result_text.add_css_class("caption")
        self.result_text.add_css_class("dim-label")
        rb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        rb.set_margin_top(4)
        rb.append(self.result_title)
        rb.append(self.result_text)
        rbtn = Gtk.Box(spacing=8)
        self.btn_open = Gtk.Button(label="Показать в папке")
        self.btn_open.connect("clicked", lambda _b: self.open_folder())
        self.btn_again = Gtk.Button(label="Добрать ещё")
        self.btn_again.set_tooltip_text("Продолжить сеанс: плохие области будут перечитаны ещё раз")
        self.btn_again.connect("clicked", lambda _b: self.on_start(more=True))
        self.btn_recover = Gtk.Button(label="Восстановить файлы из образа")
        self.btn_recover.connect("clicked", lambda _b: self.window.go_recover_image(self.image))
        rbtn.append(self.btn_open)
        rbtn.append(self.btn_again)
        rbtn.append(self.btn_recover)
        rb.append(rbtn)
        self.result_group.add(rb)
        lbox.append(self.result_group)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, hexpand=True)
        for m in ("start", "end", "top", "bottom"):
            getattr(right, f"set_margin_{m}")(24)
        stats = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=10,
                            row_spacing=10, min_children_per_line=2, max_children_per_line=5)
        self.st_done = Stat("Спасено")
        self.st_bad = Stat("Нечитаемо")
        self.st_rate = Stat("Скорость")
        self.st_left = Stat("Осталось")
        self.st_err = Stat("Ошибок чтения")
        for s in (self.st_done, self.st_bad, self.st_rate, self.st_left, self.st_err):
            stats.append(s)
        right.append(stats)

        self.phase_box = Gtk.Box(spacing=8)
        self.phase_lbls = {}
        for key, name in PHASES:
            l = Gtk.Label(label=name)
            l.add_css_class("dp-phase")
            self.phase_lbls[key] = l
            self.phase_box.append(l)
        right.append(self.phase_box)

        self.progress = SmoothBar()
        right.append(self.progress)
        frame = Gtk.Frame(vexpand=True)
        self.map = ScanMap(palette=RESCUE_COLORS)
        panel, self.map_empty = map_panel(self.map, "Копирование не запущено", "Здесь появится карта сохранённых данных")
        frame.set_child(panel)
        right.append(frame)

        legend = Gtk.Box(spacing=18)
        for name, code in LEGEND:
            b = Gtk.Box(spacing=7)
            dot = Gtk.DrawingArea()
            dot.set_content_width(12)
            dot.set_content_height(12)
            dot.set_valign(Gtk.Align.CENTER)
            dot.set_draw_func(lambda _a, cr, w, h, c=code: self._dot(cr, w, h, c))
            lab = Gtk.Label(label=name)
            lab.add_css_class("caption")
            b.append(dot)
            b.append(lab)
            legend.append(b)
        right.append(legend)
        self.graph = SpeedGraph()
        right.append(self.graph)

        self.append(left)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        rsc = Gtk.ScrolledWindow(hexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        rsc.set_child(right)
        self.rsc = rsc
        self.append(rsc)

        store.connect(self._on_store)
        self.reload_disks()

    # ---------- адаптивность ----------
    def set_narrow(self, narrow):
        self.set_orientation(Gtk.Orientation.VERTICAL if narrow else Gtk.Orientation.HORIZONTAL)
        self.left.set_size_request(-1 if narrow else 330, -1)
        self.map.set_size_request(200, 220 if narrow else 160)

    # ---------- данные ----------
    def _dot(self, cr, w, h, code):
        if code is None:
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
        else:
            cr.set_source_rgb(*_hex(RESCUE_COLORS[code][1 if is_dark() else 0]))
        cr.arc(w / 2, h / 2, 5.5, 0, 6.2832)
        cr.fill()

    def reload_disks(self):
        keep = self.selected_disk().path if self.selected_disk() else None
        self.disks = list(self.store.disks)
        self.disk_row.set_model(Gtk.StringList.new(
            [f"{d.title} · {devices.human_size(d.size)}" for d in self.disks] or ["Дисков не найдено"]))
        for i, d in enumerate(self.disks):
            if d.path == keep:
                self.disk_row.set_selected(i)
        self._on_changed()

    def selected_disk(self):
        i = self.disk_row.get_selected()
        return self.disks[i] if 0 <= i < len(self.disks) else None

    def select_disk(self, disk):
        for i, d in enumerate(self.disks):
            if d.path == disk.path:
                self.disk_row.set_selected(i)
        self._suggest_dest()

    def _suggest_dest(self):
        """Папка на другом диске с максимумом свободного места."""
        d = self.selected_disk()
        if not d:
            return
        cands = [os.path.expanduser("~")]
        for base in ("/run/media", "/mnt", "/media"):
            if os.path.isdir(base):
                for root, dirs, _f in os.walk(base):
                    depth = root[len(base):].count("/")
                    if depth >= 2:
                        dirs[:] = []
                        continue
                    if os.path.ismount(root):
                        cands.append(root)
        best, free = None, -1
        for c in cands:
            owner = mount_of(c, self.store.disks)
            if owner and owner.path == d.path:
                continue
            if not (c + "/").startswith(ALLOWED) or not os.access(c, os.W_OK):
                continue
            try:
                f = shutil.disk_usage(c).free
            except OSError:
                continue
            if f > free:
                best, free = c, f
        self.set_dest(best or os.path.expanduser("~"))
        stamp = time.strftime("%Y%m%d-%H%M")
        self.name_row.set_text(f"{safe_name(d.title)}-{stamp}")

    def set_dest(self, path):
        self.dest_dir = path
        self.dir_row.set_subtitle(path)
        self._on_changed()

    def pick_folder(self):
        dlg = Gtk.FileDialog(title="Папка для образа")
        dlg.set_initial_folder(Gio.File.new_for_path(self.dest_dir))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.set_dest(f.get_path())

        dlg.select_folder(self.window, None, done)

    def _paths(self):
        name = safe_name(self.name_row.get_text().strip()) if self.name_row.get_text().strip() else ""
        name = re.sub(r"\.(img|map)$", "", name)
        if not name:
            return None, None
        return os.path.join(self.dest_dir, name + ".img"), os.path.join(self.dest_dir, name + ".map")

    def _on_changed(self, *_):
        d = self.selected_disk()
        if self.state != "idle" or not d:
            return
        img, mp = self._paths()
        resume = bool(img and os.path.exists(img) and os.path.exists(mp))
        self.resume_note.set_visible(resume)
        self.btn_start.set_label("Продолжить копирование" if resume else "Начать копирование")
        try:
            free = shutil.disk_usage(self.dest_dir).free
        except OSError:
            free = 0
        need = 0 if resume else d.size
        self.space_lbl.set_label(f"{devices.human_size(free)}  (нужно до {devices.human_size(d.size)})")
        ok = free >= need + 64 * 1024 * 1024
        for c in ("error", "success"):
            self.space_lbl.remove_css_class(c)
        self.space_lbl.add_css_class("success" if ok else "error")
        owner = mount_of(self.dest_dir, self.store.disks)
        if owner and owner.path == d.path:
            self.space_lbl.set_label("это тот же диск!")
            self.space_lbl.remove_css_class("success")
            self.space_lbl.add_css_class("error")
            ok = False
        self.can_start = ok and bool(img)

    def _on_store(self, ev, payload):
        if ev == "disks" and self.state == "idle":
            self.reload_disks()
            if not self.name_row.get_text():
                self._suggest_dest()

    # ---------- управление ----------
    def set_state(self, st):
        self.state = st
        self.map_empty.set_visible(st == "idle" and not len(self.map.codes))
        idle = st == "idle"
        self.btn_start.set_visible(idle)
        self.btn_pause.set_visible(not idle)
        self.btn_stop.set_visible(not idle)
        self.btn_pause.set_label("Продолжить" if st == "paused" else "Пауза")
        for w in (self.disk_row, self.name_row, self.dir_row):
            w.set_sensitive(idle)

    def on_start(self, more=False):
        d = self.selected_disk()
        if not d:
            return
        if not getattr(self, "can_start", False):
            self.window.toast("Проверьте папку назначения и имя образа: места не хватает или это тот же диск")
            return
        if d.in_use:
            heading = "Этот диск сейчас используется"
            body = ("Системный диск работает прямо сейчас, образ получится «живым» и может быть неконсистентным. "
                    if d.is_system else
                    "С разделов этого диска идёт работа (они примонтированы), образ может получиться неконсистентным. ")
            body += "Для надёжной копии отмонтируйте разделы или загрузитесь с Rescue-флешки."
            dlg = Adw.AlertDialog.new(heading, body)
            dlg.add_response("cancel", "Отмена")
            dlg.add_response("go", "Всё равно копировать")
            dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")
            dlg.choose(self.window, None, lambda dd, res: self._after_confirm(dd, res, d, more))
            return
        self._launch(d, more)

    def _after_confirm(self, dlg, res, d, more):
        if dlg.choose_finish(res) == "go":
            self._launch(d, more)

    def _launch(self, d, more):
        self.store.ensure_helper(lambda: self._go(d, more), lambda msg: self.window.toast(msg))

    def _go(self, d, more):
        img, mp = self._paths()
        retries = int(self.retry_row.get_value()) + (1 if more else 0)
        self.image, self.mapfile = img, mp
        self.map.reset(0)
        self.graph.clear()
        self.max_rate = 0.0
        self.result_group.set_visible(False)
        self.progress.set_fraction(0)
        for key in self.phase_lbls:
            self._phase_style(key, "")
        self.disk_imaged = d
        self.set_state("running")
        self.job_id = self.store.helper.request(
            "image", self._on_msg, dev=d.path, image=img, mapfile=mp, retries=retries,
            reverse=self.rev_row.get_active(), no_scrape=self.noscrape_row.get_active(), cells=6000)

    def on_pause(self):
        cmd = "resume" if self.state == "paused" else "pause"
        self.store.helper.request(cmd, None, target=self.job_id)
        self.set_state("running" if self.state == "paused" else "paused")

    def on_stop(self):
        self.store.helper.request("stop", None, target=self.job_id)

    def open_folder(self):
        if self.image:
            try:
                Gio.AppInfo.launch_default_for_uri("file://" + os.path.dirname(self.image), None)
            except GLib.Error:
                pass

    # ---------- события ----------
    def _on_msg(self, m):
        ev = m.get("ev")
        if ev == "start":
            self.total = m["total"]
            self.map.reset(1)
            if m.get("resume"):
                self.window.toast("Продолжаю прерванный сеанс")
        elif ev == "progress":
            self._progress(m)
        elif ev == "done":
            self.map.set_head(None)
            self._finish(m)
        elif ev == "err":
            self.set_state("idle")
            self.window.toast(m.get("msg", "Ошибка копирования"))
            self._on_changed()

    def _phase_style(self, key, mode):
        lab = self.phase_lbls[key]
        for c in ("dp-phase-active", "dp-phase-done"):
            lab.remove_css_class(c)
        if mode:
            lab.add_css_class(f"dp-phase-{mode}")

    def _progress(self, m):
        sizes = m["sizes"]
        good = sizes.get("+", 0)
        bad = sizes.get("-", 0)
        pending = sizes.get("*", 0) + sizes.get("/", 0)
        total = self.total or 1
        if m.get("map"):
            tbl = bytes(CODE_OF.get(c, 255) for c in m["map"].encode("ascii"))
            self.map.set_codes(tbl)
            if m.get("ipos") and self.total and not m.get("paused"):
                self.map.set_head(int(m["ipos"] / self.total * len(tbl)))
        frac = min(1.0, (good + bad) / total)
        self.progress.set_fraction(min(1.0, good / total))
        self.st_done.set(f"{devices.human_size(good)} · {good / total * 100:.1f}%")
        self.st_bad.set(devices.human_size(bad + pending) if (bad or pending) else "0")
        rate = m.get("rate", 0) / 1e6
        self.st_rate.set(f"{rate:.1f} МБ/с")
        if rate > 0 and not m.get("paused"):
            self.graph.add(rate)
        rem = m.get("remaining")
        self.st_left.set(fmt_time(rem) if rem is not None else "—")
        self.st_err.set(f"{m.get('errors', 0)}")
        cur = m.get("phase", "copy")
        order = [k for k, _ in PHASES]
        if cur in order:
            ci = order.index(cur)
            for i, k in enumerate(order):
                self._phase_style(k, "done" if i < ci else "active" if i == ci else "")
            p = m.get("pass")
            if cur == "retry" and p:
                self.phase_lbls["retry"].set_label(f"4  Повторы · проход {p}")

    def _finish(self, m):
        self.set_state("idle")
        sizes = m.get("sizes", {})
        good, bad = sizes.get("+", 0), sizes.get("-", 0) + sizes.get("*", 0) + sizes.get("/", 0)
        total = self.total or 1
        reason = m.get("reason")
        if reason == "failed":
            self.window.toast("ddrescue завершился с ошибкой: " + (" ".join(m.get("tail", [])[-2:]) or "см. журнал"))
        self._on_changed()
        if good <= 0 and not bad:
            return
        pct = good / total * 100
        if reason == "finished" and not bad:
            self.result_title.set_label("Образ получен полностью")
            self.result_text.set_label(f"Спасено {devices.human_size(good)} (100 %).\n{self.image}")
            self.btn_again.set_visible(False)
        else:
            head = "Копирование остановлено" if reason == "stopped" else "Образ получен частично"
            self.result_title.set_label(head)
            self.result_text.set_label(f"Спасено {devices.human_size(good)} ({pct:.2f} %), "
                                       f"нечитаемо {devices.human_size(bad)}.\n{self.image}")
            self.btn_again.set_visible(True)
        self.result_group.set_visible(True)
        for k in self.phase_lbls:
            if reason == "finished":
                self._phase_style(k, "done")
