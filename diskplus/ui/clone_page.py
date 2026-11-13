"""Клонирование диска: выбор дисков -> раскладка (перетаскивание размеров) -> подтверждение -> выполнение."""
from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import devices  # noqa: E402
from ..clone import plan as planmod  # noqa: E402
from .layout_bar import LayoutBar, Seg  # noqa: E402
from .widgets import SmoothBar  # noqa: E402

MODES = ["Автоматически (уместить или растянуть)", "Как есть, без изменения размеров", "Растянуть основной раздел", "Распределить пропорционально"]
STATE_ICON = {"running": None, "done": "object-select-symbolic", "warn": "dialog-warning-symbolic",
              "error": "dialog-error-symbolic", "wait": "content-loading-symbolic"}


def fmt_eta(sec):
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}" if sec < 3600 else f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class ClonePage(Gtk.Box):
    def __init__(self, window, store):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.store = window, store
        self.src = self.dst = None
        self.label = "gpt"
        self.src_parts: list[planmod.SrcPart] = []
        self.layout: planmod.Layout | None = None
        self.job = None
        self.running = False
        self.step_rows: list = []
        self.checked = None

        self.bar = Gtk.Box(spacing=8)
        for m in ("start", "end"):
            getattr(self.bar, f"set_margin_{m}")(14)
        self.bar.set_margin_top(10)
        self.bar.set_margin_bottom(4)
        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Назад", visible=False)
        self.back_btn.add_css_class("flat")
        self.back_btn.connect("clicked", lambda _b: self.go_back())
        self.crumb = Gtk.Label(xalign=0, hexpand=True)
        self.crumb.add_css_class("heading")
        self.bar.append(self.back_btn)
        self.bar.append(self.crumb)
        self.append(self.bar)
        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.append(self.stack)
        self._build_pick()
        self._build_layout()
        self._build_run()
        self.show_step("pick")
        store.connect(self._on_store)
        self.reload_disks()

    # ------------------------------------------------------------ навигация
    def show_step(self, name):
        self.step = name
        self.stack.set_visible_child_name(name)
        self.back_btn.set_visible(name == "layout")
        self.crumb.set_label({"pick": "Клонирование диска: откуда и куда", "layout": "Раскладка разделов",
                              "run": "Выполнение"}[name])

    def go_back(self):
        if self.step == "layout":
            self.show_step("pick")

    # ------------------------------------------------------------ 1. выбор дисков
    def _build_pick(self):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(18)
        sc.set_child(box)
        intro = Gtk.Label(wrap=True, xalign=0,
                          label="Копия диска целиком: разделы, таблица разделов и данные. Исходный диск не изменяется. "
                                "Диск назначения будет полностью стёрт. Разделы можно только увеличивать, "
                                "уменьшать пока нельзя, поэтому новый диск должен вмещать все выбранные разделы.")
        intro.add_css_class("dim-label")
        box.append(intro)
        g = Adw.PreferencesGroup(title="Откуда копируем")
        self.src_row = Adw.ComboRow(title="Исходный диск")
        self.src_row.connect("notify::selected", lambda *_: self._on_pick_changed())
        g.add(self.src_row)
        box.append(g)
        g2 = Adw.PreferencesGroup(title="Куда копируем", description="Все данные на этом диске будут уничтожены")
        self.dst_row = Adw.ComboRow(title="Диск назначения")
        self.dst_row.connect("notify::selected", lambda *_: self._on_pick_changed())
        g2.add(self.dst_row)
        box.append(g2)
        self.pick_msg = Gtk.Label(wrap=True, xalign=0)
        self.pick_msg.add_css_class("caption")
        box.append(self.pick_msg)
        self.next_btn = Gtk.Button(label="Далее: раскладка", halign=Gtk.Align.START)
        self.next_btn.add_css_class("suggested-action")
        self.next_btn.add_css_class("pill")
        self.next_btn.connect("clicked", lambda _b: self.to_layout())
        box.append(self.next_btn)
        self.stack.add_named(sc, "pick")

    def reload_disks(self):
        if self.running:
            return
        self.disks = [d for d in self.store.disks]
        labels = [f"{d.title} · {devices.human_size(d.size)} · {d.path}" for d in self.disks] or ["Дисков не найдено"]
        keep_s = self.src.path if self.src else None
        keep_d = self.dst.path if self.dst else None
        self.src_row.set_model(Gtk.StringList.new(labels))
        self.dst_row.set_model(Gtk.StringList.new(labels))
        for i, d in enumerate(self.disks):
            if d.path == keep_s:
                self.src_row.set_selected(i)
            if d.path == keep_d:
                self.dst_row.set_selected(i)
        self._on_pick_changed()

    def _sel(self, row):
        i = row.get_selected()
        return self.disks[i] if 0 <= i < len(self.disks) else None

    def set_pair(self, src, dst):
        for i, d in enumerate(self.disks):
            if d.path == src.path:
                self.src_row.set_selected(i)
            if d.path == dst.path:
                self.dst_row.set_selected(i)
        self._on_pick_changed()

    def preselect_source(self, disk):
        for i, d in enumerate(self.disks):
            if d.path == disk.path:
                self.src_row.set_selected(i)

    def _on_pick_changed(self):
        s, d = self._sel(self.src_row), self._sel(self.dst_row)
        problems, notes = [], []
        if not s or not d:
            problems.append("Выберите диски")
        else:
            if s.path == d.path:
                problems.append("Диск назначения совпадает с исходным")
            if d.is_system:
                problems.append("Диск назначения системный: на него клонировать нельзя")
            elif d.in_use:
                problems.append("Диск назначения используется (есть примонтированные разделы): отмонтируйте их")
            if s.in_use:
                notes.append("С исходного диска сейчас идёт работа: примонтированные разделы нельзя копировать, "
                             "отмонтируйте их или загрузитесь с Rescue-флешки.")
            if d.size < s.size and not problems:
                notes.append("Диск назначения меньше исходного: поместятся только выбранные разделы (на следующем шаге).")
        self.pick_msg.set_label("\n".join(problems + notes))
        for c in ("error", "warning"):
            self.pick_msg.remove_css_class(c)
        self.pick_msg.add_css_class("error" if problems else "warning")
        self.next_btn.set_sensitive(not problems)

    # ------------------------------------------------------------ 2. раскладка
    def _build_layout(self):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(18)
        sc.set_child(box)
        self.src_bar = LayoutBar(editable=False)
        box.append(self.src_bar)
        self.part_group = Adw.PreferencesGroup(title="Какие разделы копировать")
        box.append(self.part_group)
        self._part_rows = []
        g = Adw.PreferencesGroup(title="Размещение на новом диске")
        self.mode_row = Adw.ComboRow(title="Размеры разделов", model=Gtk.StringList.new(MODES))
        self.mode_row.connect("notify::selected", lambda *_: self._apply_mode())
        g.add(self.mode_row)
        box.append(g)
        hint = Gtk.Label(xalign=0, wrap=True,
                         label="Край раздела на нижней полосе можно тянуть мышью: следующие разделы сдвинутся.")
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        box.append(hint)
        self.dst_bar = LayoutBar(editable=True, on_resize=self._on_drag)
        box.append(self.dst_bar)
        self.sum_lbl = Gtk.Label(xalign=0, wrap=True)
        self.sum_lbl.add_css_class("caption")
        box.append(self.sum_lbl)
        og = Adw.PreferencesGroup(title="Параметры")
        self.newid_row = Adw.SwitchRow(title="Новые идентификаторы",
                                       subtitle="Новые UUID у разделов и файловых систем: нужно, если оригинал и клон будут подключены одновременно")
        self.verify_row = Adw.SwitchRow(title="Проверить после копирования", subtitle="Проверка файловых систем клона (только чтение)", active=True)
        og.add(self.newid_row)
        og.add(self.verify_row)
        box.append(og)
        self.plan_msg = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.plan_msg.add_css_class("caption")
        box.append(self.plan_msg)
        row = Gtk.Box(spacing=8)
        self.go_btn = Gtk.Button(label="Проверить план и продолжить")
        self.go_btn.add_css_class("suggested-action")
        self.go_btn.add_css_class("pill")
        self.go_btn.connect("clicked", lambda _b: self.check_plan())
        row.append(self.go_btn)
        box.append(row)
        self.stack.add_named(sc, "layout")

    def to_layout(self):
        self.src, self.dst = self._sel(self.src_row), self._sel(self.dst_row)
        self.label, self.src_parts = planmod.read_source(self.src.path)
        if not self.src_parts or self.label not in ("gpt", "dos"):
            self.window.toast("На исходном диске нет таблицы разделов GPT или MBR: такой диск пока нельзя клонировать")
            return
        self.next_btn.set_sensitive(False)
        self.next_btn.set_label("Считаю минимальные размеры…")

        def ready():
            def got(msg):
                self.next_btn.set_sensitive(True)
                self.next_btn.set_label("Далее: раскладка")
                if msg.get("ev") == "res":
                    self.staging = msg["data"].get("staging")
                    by = {p["node"]: p for p in msg["data"]["parts"]}
                    for sp in self.src_parts:
                        info = by.get(sp.node)
                        if info and info.get("min"):
                            sp.min_size, sp.used = info["min"], info.get("used", 0)
                self._build_layout_view()

            self.store.helper.request("shrink_info", got, src=self.src.path)

        def failed(msg):
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Далее: раскладка")
            self.window.toast(msg + ". Без доступа раздел уменьшить не получится.")
            self._build_layout_view()

        self.store.ensure_helper(ready, failed)

    def _build_layout_view(self):
        self.layout = planmod.Layout(self.src_parts, self.dst.size // 512, self.label)
        for r in self._part_rows:
            self.part_group.remove(r)
        self._part_rows = []
        for sp in self.src_parts:
            nm = sp.label or sp.name or ""
            extra = f" · можно уменьшить до {devices.human_size(sp.floor * 512)}" if sp.floor < sp.size else ""
            row = Adw.SwitchRow(title=f"Раздел {sp.number}: {sp.fs or 'без файловой системы'}" + (f" · {nm}" if nm else ""),
                                subtitle=devices.human_size(sp.size * 512) + extra + (f" · примонтирован ({', '.join(sp.mounts)})" if sp.mounts else ""),
                                active=True)
            row.connect("notify::active", lambda r, _p, sp=sp: self._toggle_part(sp, r.get_active()))
            self.part_group.add(row)
            self._part_rows.append(row)
        self.src_bar.title = f"Источник: {self.src.title}"
        self.src_bar.set_data(self.src.size // 512, [Seg(f"{sp.number}  {sp.fs or ''}", sp.fs, sp.start, sp.size) for sp in self.src_parts])
        self.mode_row.set_selected(0)
        self._apply_mode()
        self.plan_msg.set_label("")
        self.show_step("layout")

    def _toggle_part(self, sp, on):
        for p in self.layout.parts:
            if p.src is sp:
                p.enabled = on
        self._apply_mode()

    def _apply_mode(self):
        if not self.layout:
            return
        m = self.mode_row.get_selected()
        {0: self.layout.mode_auto, 1: self.layout.mode_asis, 2: self.layout.mode_stretch_largest,
         3: self.layout.mode_proportional}[m]()
        self._refresh_bar()

    def _on_drag(self, idx_enabled, new_size):
        en = self.layout.enabled()
        if idx_enabled >= len(en):
            return
        i = self.layout.parts.index(en[idx_enabled])
        self.layout.resize(i, new_size)
        self._refresh_bar()

    def _refresh_bar(self):
        L = self.layout
        segs = []
        for p in L.enabled():
            sp = p.src
            segs.append(Seg(f"{sp.number}  {sp.fs or ''}", sp.fs, p.start, p.size, sp.size))
        self.dst_bar.title = f"Диск назначения: {self.dst.title}"
        self.dst_bar.set_data(L.target, segs)
        free = max(L.free, 0)
        txt = f"Занято {devices.human_size((L.end) * 512)} из {devices.human_size(L.target * 512)}, свободно {devices.human_size(free * 512)}."
        shr = L.shrunk()
        if shr:
            txt += "\nБудут уменьшены: " + "; ".join(f"раздел {p.src.number} ({p.src.fs}) {devices.human_size(a * 512)} → {devices.human_size(b * 512)}" for p, a, b in shr) \
                   + ".\nФайловая система сначала уменьшается в временном файле (понадобится свободное место на другом диске), исходный диск не меняется."
        ok = L.fits and bool(L.enabled())
        if not L.fits:
            txt = (f"Не помещается: даже с уменьшением нужно минимум {devices.human_size(L.min_total() * 512)}, "
                   f"а диск {devices.human_size(L.target * 512)}. Отключите часть разделов или выберите диск побольше.")
        elif not L.enabled():
            txt = "Не выбран ни один раздел"
        self.sum_lbl.set_label(txt)
        for c in ("error", "warning", "dim-label"):
            self.sum_lbl.remove_css_class(c)
        self.sum_lbl.add_css_class("dim-label" if ok else "error")
        self.go_btn.set_sensitive(ok)

    # ------------------------------------------------------------ проверка плана и подтверждение
    def _request_args(self):
        return dict(src=self.src.path, dst=self.dst.path, parts=self.layout.to_request(),
                    new_ids=self.newid_row.get_active(), verify=self.verify_row.get_active(),
                    **({"staging": self.staging_dir} if getattr(self, "staging_dir", None) else {}))

    def check_plan(self):
        self.go_btn.set_sensitive(False)
        self.plan_msg.set_label("Проверяю план…")

        def ready():
            def got(msg):
                self.go_btn.set_sensitive(True)
                if msg.get("ev") != "res":
                    self.plan_msg.set_label("Отказ: " + msg.get("msg", "ошибка проверки"))
                    self._style_msg(True)
                    return
                d = msg["data"]
                self.checked = d
                if d["problems"]:
                    self.plan_msg.set_label("Нельзя продолжать:\n" + "\n".join("• " + p for p in d["problems"]))
                    self._style_msg(True)
                    return
                self.plan_msg.set_label(("Предупреждения:\n" + "\n".join("• " + w for w in d["warnings"])) if d["warnings"] else "План принят.")
                self._style_msg(False)
                self.confirm()

            self.store.helper.request("clone_check", got, **self._request_args())

        self.store.ensure_helper(ready, lambda m: (self.go_btn.set_sensitive(True), self.window.toast(m)))

    def _style_msg(self, err):
        for c in ("error", "warning"):
            self.plan_msg.remove_css_class(c)
        self.plan_msg.add_css_class("error" if err else "warning")

    def confirm(self):
        name = os.path.basename(self.dst.path)
        parts = "\n".join(f"  • раздел {p.src.number} ({p.src.fs or '—'}): {devices.human_size(p.size * 512)}"
                          + (f"  (уменьшается с {devices.human_size(p.src.size * 512)})" if p.size < p.src.size else "")
                          for p in self.layout.enabled())
        body = (f"Диск назначения будет ПОЛНОСТЬЮ СТЁРТ:\n{self.dst.title}\n{self.dst.path} · {devices.human_size(self.dst.size)}"
                + (f" · серийный № {self.dst.serial}" if self.dst.serial else "")
                + f"\n\nНа него запишется копия диска «{self.src.title}»:\n{parts}\n\n"
                  f"Для подтверждения введите имя диска назначения: {name}")
        dlg = Adw.AlertDialog.new("Стереть диск и записать клон?", body)
        entry = Gtk.Entry(placeholder_text=name)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Отмена")
        dlg.add_response("go", "Клонировать")
        dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_response_enabled("go", False)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        entry.connect("changed", lambda e: dlg.set_response_enabled("go", e.get_text().strip() == name))
        dlg.choose(self.window, None, lambda d, r: self._confirmed(d, r, entry, name))

    def _confirmed(self, dlg, res, entry, name):
        if dlg.choose_finish(res) == "go" and entry.get_text().strip() == name:
            self.start_clone(name)

    # ------------------------------------------------------------ 3. выполнение
    def _build_run(self):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(18)
        sc.set_child(box)
        self.run_title = Gtk.Label(xalign=0, wrap=True)
        self.run_title.add_css_class("title-2")
        box.append(self.run_title)
        self.steps_group = Adw.PreferencesGroup(title="Ход работы")
        box.append(self.steps_group)
        self.cur_bar = SmoothBar()
        box.append(self.cur_bar)
        self.cur_lbl = Gtk.Label(xalign=0)
        self.cur_lbl.add_css_class("caption")
        box.append(self.cur_lbl)
        btns = Gtk.Box(spacing=8)
        self.stop_btn = Gtk.Button(label="Остановить")
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.connect("clicked", lambda _b: self.confirm_stop())
        self.done_btn = Gtk.Button(label="Готово", visible=False)
        self.done_btn.add_css_class("suggested-action")
        self.done_btn.connect("clicked", lambda _b: self.finish_view())
        btns.append(self.stop_btn)
        btns.append(self.done_btn)
        box.append(btns)
        self.result_lbl = Gtk.Label(xalign=0, wrap=True, selectable=True, visible=False)
        box.append(self.result_lbl)
        self.stack.add_named(sc, "run")

    def start_clone(self, confirm_name):
        self.running = True
        for r in self.step_rows:
            self.steps_group.remove(r[0])
        self.step_rows = []
        self.cur_bar.set_fraction(0)
        self.cur_lbl.set_label("")
        self.result_lbl.set_visible(False)
        self.stop_btn.set_visible(True)
        self.done_btn.set_visible(False)
        self.run_title.set_label(f"{self.src.title} → {self.dst.title}")
        self.show_step("run")
        self.job = self.store.helper.request("clone", self._on_msg, confirm=confirm_name, **self._request_args())

    def _on_msg(self, m):
        ev = m.get("ev")
        if ev == "start":
            for title in m["steps"]:
                row = Adw.ActionRow(title=title)
                sp = Gtk.Spinner(visible=False)
                img = Gtk.Image(visible=False)
                pre = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
                pre.set_size_request(24, 24)
                pre.append(sp)
                pre.append(img)
                row.add_prefix(pre)
                self.steps_group.add(row)
                self.step_rows.append((row, sp, img))
        elif ev == "step":
            row, sp, img = self.step_rows[m["id_"]]
            st = m["state"]
            sp.set_visible(st == "running")
            sp.set_spinning(st == "running")
            icon = STATE_ICON.get(st)
            img.set_visible(bool(icon))
            if icon:
                img.set_from_icon_name(icon)
            if m.get("note"):
                row.set_subtitle(m["note"])
            if st == "running":
                self.cur_bar.set_fraction(0)
        elif ev == "progress":
            self.cur_bar.set_fraction(min(1.0, m["percent"] / 100))
            self.cur_lbl.set_label(f"{m['percent']:.0f}%  ·  {devices.human_size(m['rate'])}/с  ·  осталось {fmt_eta(m['eta'])}")
        elif ev == "done":
            self._finished(m)
        elif ev == "err":
            self.running = False
            self.window.toast(m.get("msg", "Ошибка клонирования"))
            self.show_step("layout")

    def _finished(self, m):
        self.running = False
        self.stop_btn.set_visible(False)
        self.done_btn.set_visible(True)
        reason = m.get("reason")
        if reason == "finished":
            self.cur_bar.set_fraction(1.0)
            head = "Клон готов"
            tips = ("Если вы заменяете диск: выключите компьютер и поставьте клон вместо оригинала. "
                    "Не включайте оригинал и клон одновременно, если у них одинаковые идентификаторы.")
            self.cur_lbl.set_label("")
        elif reason == "stopped":
            head = "Остановлено"
            tips = "Диск назначения остался в неполном состоянии: клонирование нужно запустить заново."
        else:
            head = "Не удалось завершить клонирование"
            tips = f"{m.get('error', '')}\nДиск назначения остался в неполном состоянии."
        warns = m.get("warnings") or []
        txt = f"{head}\n{tips}" + (("\n\nЗамечания:\n" + "\n".join("• " + w for w in warns)) if warns else "")
        self.result_lbl.set_label(txt)
        self.result_lbl.set_visible(True)
        self.store.refresh_disks()

    def confirm_stop(self):
        dlg = Adw.AlertDialog.new("Остановить клонирование?", "Диск назначения останется в неполном состоянии, "
                                  "и операцию придётся начать заново.")
        dlg.add_response("no", "Продолжить копирование")
        dlg.add_response("yes", "Остановить")
        dlg.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("no")
        dlg.set_close_response("no")
        dlg.choose(self.window, None, lambda d, r: d.choose_finish(r) == "yes" and self.store.helper.request("stop", None, target=self.job))

    def finish_view(self):
        self.show_step("pick")
        self.reload_disks()

    def _on_store(self, ev, payload):
        if ev == "disks" and self.step == "pick":
            self.reload_disks()

    def set_narrow(self, narrow):
        pass
