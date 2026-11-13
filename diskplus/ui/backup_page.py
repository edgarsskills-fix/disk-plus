"""Резервные копии: создать копию разделов, проверить, восстановить на диск, открыть содержимое."""
from __future__ import annotations

import os
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import backup as bk  # noqa: E402
from .. import devices  # noqa: E402
from ..clone import plan as planmod  # noqa: E402
from .clone_page import MODES, STATE_ICON, fmt_eta  # noqa: E402
from .layout_bar import LayoutBar, Seg  # noqa: E402
from .widgets import SmoothBar  # noqa: E402

DEFAULT_DIR = os.path.expanduser("~/Резервные копии Disk++")
LEVELS = [("Быстро (меньше сжатие)", 1), ("Стандартно", 3), ("Сильнее (дольше)", 9)]


def safe_name(t):
    import re
    t = re.sub(r"[^\w.\- ]+", "_", t, flags=re.UNICODE).strip("._ ")
    return t or "copy"


class StepsView(Gtk.Box):
    """Список шагов с отметками и строкой прогресса (общий для создания и восстановления)."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.title = Gtk.Label(xalign=0, wrap=True)
        self.title.add_css_class("title-2")
        self.append(self.title)
        self.group = Adw.PreferencesGroup(title="Ход работы")
        self.append(self.group)
        self.bar = SmoothBar()
        self.append(self.bar)
        self.lbl = Gtk.Label(xalign=0)
        self.lbl.add_css_class("caption")
        self.append(self.lbl)
        self.rows = []
        self.result = Gtk.Label(xalign=0, wrap=True, selectable=True, visible=False)
        self.append(self.result)

    def reset(self, title):
        for r in self.rows:
            self.group.remove(r[0])
        self.rows = []
        self.title.set_label(title)
        self.bar.set_fraction(0)
        self.lbl.set_label("")
        self.result.set_visible(False)

    def on_start(self, steps):
        for t in steps:
            row = Adw.ActionRow(title=t)
            sp, img = Gtk.Spinner(visible=False), Gtk.Image(visible=False)
            pre = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            pre.set_size_request(24, 24)
            pre.append(sp)
            pre.append(img)
            row.add_prefix(pre)
            self.group.add(row)
            self.rows.append((row, sp, img))

    def on_step(self, m):
        row, sp, img = self.rows[m["id_"]]
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
            self.bar.set_fraction(0)

    def on_progress(self, m):
        self.bar.set_fraction(min(1.0, m["percent"] / 100))
        rate = f"  ·  {devices.human_size(m['rate'])}/с" if m.get("rate") else ""
        eta = f"  ·  осталось {fmt_eta(m['eta'])}" if m.get("eta") else ""
        self.lbl.set_label(f"{m['percent']:.0f}%{rate}{eta}")


class BackupPage(Gtk.Box):
    def __init__(self, window, store):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.store = window, store
        self.running = False
        self.job = None
        self.src = self.dst = None
        self.src_parts: list[planmod.SrcPart] = []
        self.manifest = None
        self.folder = None
        self.layout = None
        self.dest_dir = DEFAULT_DIR
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
        self._build_home()
        self._build_create()
        self._build_restore()
        self._build_run()
        self.show_step("home")
        store.connect(self._on_store)
        self.reload_disks()

    # ------------------------------------------------------------ навигация
    TITLES = {"home": "Резервные копии", "create": "Создать копию", "restore": "Восстановить из копии", "run": "Выполнение"}

    def show_step(self, name):
        self.step = name
        self.stack.set_visible_child_name(name)
        self.back_btn.set_visible(name in ("create", "restore"))
        self.crumb.set_label(self.TITLES[name])
        if name == "home":
            self.refresh_list()

    def go_back(self):
        if self.step in ("create", "restore"):
            self.show_step("home")

    def reload_disks(self):
        if self.running:
            return
        self.disks = list(self.store.disks)
        labels = [f"{d.title} · {devices.human_size(d.size)} · {d.path}" for d in self.disks] or ["Дисков не найдено"]
        for row in (self.c_src_row, self.r_dst_row):
            row.set_model(Gtk.StringList.new(labels))
        self._c_changed()

    def _on_store(self, ev, payload):
        if ev == "disks" and self.step in ("home", "create", "restore"):
            self.reload_disks()

    # ------------------------------------------------------------ главная
    def _build_home(self):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(18)
        sc.set_child(box)
        intro = Gtk.Label(wrap=True, xalign=0,
                          label="Копия сохраняет разделы диска в сжатые файлы с контрольными суммами. "
                                "Потом её можно проверить, развернуть на другой диск или открыть и достать отдельные файлы. "
                                "Копируются только занятые данные.")
        intro.add_css_class("dim-label")
        box.append(intro)
        g = Adw.PreferencesGroup()
        r1 = Adw.ActionRow(title="Создать резервную копию", subtitle="Сохранить выбранные разделы диска в папку")
        b1 = Gtk.Button(label="Создать…", valign=Gtk.Align.CENTER)
        b1.add_css_class("suggested-action")
        b1.connect("clicked", lambda _b: self.show_step("create"))
        r1.add_suffix(b1)
        g.add(r1)
        r2 = Adw.ActionRow(title="Открыть папку с копией", subtitle="Выбрать копию из другого места")
        b2 = Gtk.Button(label="Выбрать…", valign=Gtk.Align.CENTER)
        b2.connect("clicked", lambda _b: self.pick_backup())
        r2.add_suffix(b2)
        g.add(r2)
        box.append(g)
        self.list_group = Adw.PreferencesGroup(title="Копии в папке «Резервные копии Disk++»")
        box.append(self.list_group)
        self._list_rows = []
        self.stack.add_named(sc, "home")

    def refresh_list(self):
        for r in self._list_rows:
            self.list_group.remove(r)
        self._list_rows = []
        found = bk.find_backups(DEFAULT_DIR)
        for folder, m in found:
            self._add_row(folder, m)
        if not found:
            row = Adw.ActionRow(title="Здесь пока нет копий", subtitle=DEFAULT_DIR)
            self.list_group.add(row)
            self._list_rows.append(row)

    def _add_row(self, folder, m):
        src = m.get("source", {})
        row = Adw.ActionRow(title=f"{os.path.basename(folder)}",
                            subtitle=f"{m.get('created', '')} · {src.get('model') or src.get('path', '')} · "
                                     f"{len(m['partitions'])} раздел(ов) · {devices.human_size(bk.total_bytes(m))}")
        for label, fn in (("Проверить", lambda f=folder: self.verify(f)), ("Открыть…", lambda f=folder, mm=m: self.open_contents(f, mm)),
                          ("Восстановить", lambda f=folder, mm=m: self.start_restore_flow(f, mm))):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, fn=fn: fn())
            row.add_suffix(b)
        self.list_group.add(row)
        self._list_rows.append(row)

    def pick_backup(self):
        dlg = Gtk.FileDialog(title="Папка с резервной копией")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            path = f.get_path() if f else None
            m = bk.read_manifest(path) if path else None
            if not m:
                self.window.toast("В этой папке нет резервной копии Disk++")
                return
            self.start_restore_flow(path, m)

        dlg.select_folder(self.window, None, done)

    # ------------------------------------------------------------ создание
    def _build_create(self):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(18)
        sc.set_child(box)
        g = Adw.PreferencesGroup(title="Откуда")
        self.c_src_row = Adw.ComboRow(title="Диск")
        self.c_src_row.connect("notify::selected", lambda *_: self._c_changed())
        g.add(self.c_src_row)
        box.append(g)
        self.c_parts_group = Adw.PreferencesGroup(title="Какие разделы сохранить")
        box.append(self.c_parts_group)
        self._c_rows = []
        g2 = Adw.PreferencesGroup(title="Куда", description="Копию нельзя сохранять на тот же диск")
        self.c_dir_row = Adw.ActionRow(title="Папка", subtitle=self.dest_dir)
        self.c_dir_row.set_subtitle_lines(1)
        pb = Gtk.Button(label="Выбрать…", valign=Gtk.Align.CENTER)
        pb.connect("clicked", lambda _b: self.pick_dest())
        self.c_dir_row.add_suffix(pb)
        g2.add(self.c_dir_row)
        self.c_name = Adw.EntryRow(title="Название копии")
        g2.add(self.c_name)
        self.c_level = Adw.ComboRow(title="Сжатие", model=Gtk.StringList.new([l[0] for l in LEVELS]))
        self.c_level.set_selected(1)
        g2.add(self.c_level)
        self.c_verify = Adw.SwitchRow(title="Проверить после создания", subtitle="Пересчитать контрольные суммы и целостность", active=True)
        g2.add(self.c_verify)
        box.append(g2)
        self.c_msg = Gtk.Label(xalign=0, wrap=True)
        self.c_msg.add_css_class("caption")
        box.append(self.c_msg)
        self.c_go = Gtk.Button(label="Создать копию", halign=Gtk.Align.START)
        self.c_go.add_css_class("suggested-action")
        self.c_go.add_css_class("pill")
        self.c_go.connect("clicked", lambda _b: self.start_backup())
        box.append(self.c_go)
        self.stack.add_named(sc, "create")

    def _sel(self, row):
        i = row.get_selected()
        return self.disks[i] if 0 <= i < len(self.disks) else None

    def _c_changed(self):
        d = self._sel(self.c_src_row)
        if not d:
            return
        _lbl, parts = planmod.read_source(d.path)
        self.src, self.src_parts = d, parts
        for r in self._c_rows:
            self.c_parts_group.remove(r)
        self._c_rows = []
        for sp in parts:
            row = Adw.SwitchRow(title=f"Раздел {sp.number}: {sp.fs or 'без файловой системы'}" + (f" · {sp.label or sp.name}" if (sp.label or sp.name) else ""),
                                subtitle=devices.human_size(sp.size * 512) + (f" · примонтирован ({', '.join(sp.mounts)})" if sp.mounts else ""),
                                active=not sp.mounts)
            row.sp = sp
            self.c_parts_group.add(row)
            self._c_rows.append(row)
        self.c_name.set_text(f"{safe_name(d.title)}-{time.strftime('%Y-%m-%d_%H-%M')}")
        busy = any(sp.mounts for sp in parts)
        self.c_msg.set_label("Примонтированные разделы скопировать нельзя: отмонтируйте их или загрузитесь с Rescue-флешки." if busy else "")
        self.c_msg.add_css_class("warning")

    def pick_dest(self):
        dlg = Gtk.FileDialog(title="Папка для резервных копий")
        os.makedirs(self.dest_dir, exist_ok=True)

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.dest_dir = f.get_path()
                self.c_dir_row.set_subtitle(self.dest_dir)

        dlg.select_folder(self.window, None, done)

    def start_backup(self):
        chosen = [r.sp.node for r in self._c_rows if r.get_active()]
        if not chosen:
            self.c_msg.set_label("Выберите хотя бы один раздел")
            return
        os.makedirs(self.dest_dir, exist_ok=True)
        folder = os.path.join(self.dest_dir, safe_name(self.c_name.get_text()))
        level = LEVELS[self.c_level.get_selected()][1]
        verify = self.c_verify.get_active()
        src = self.src.path

        def ready():
            self.running = True
            self.steps.reset(f"Копия диска «{self.src.title}»")
            self.stop_btn.set_visible(True)
            self.done_btn.set_visible(False)
            self.show_step("run")
            self.job = self.store.helper.request("backup", lambda m: self._on_msg(m, "backup"), src=src, dest=folder,
                                                 parts=chosen, level=level, verify=verify)

        self.store.ensure_helper(ready, lambda m: self.window.toast(m))

    # ------------------------------------------------------------ восстановление
    def _build_restore(self):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(18)
        sc.set_child(box)
        self.r_info = Gtk.Label(xalign=0, wrap=True)
        self.r_info.add_css_class("dim-label")
        box.append(self.r_info)
        self.r_src_bar = LayoutBar(editable=False)
        box.append(self.r_src_bar)
        self.r_parts_group = Adw.PreferencesGroup(title="Какие разделы восстановить")
        box.append(self.r_parts_group)
        self._r_rows = []
        g = Adw.PreferencesGroup(title="Куда восстанавливаем", description="Все данные на этом диске будут уничтожены")
        self.r_dst_row = Adw.ComboRow(title="Диск назначения")
        self.r_dst_row.connect("notify::selected", lambda *_: self._r_layout())
        g.add(self.r_dst_row)
        self.r_mode = Adw.ComboRow(title="Размеры разделов", model=Gtk.StringList.new(MODES))
        self.r_mode.connect("notify::selected", lambda *_: self._r_apply())
        g.add(self.r_mode)
        box.append(g)
        self.r_dst_bar = LayoutBar(editable=True, on_resize=self._r_drag)
        box.append(self.r_dst_bar)
        self.r_sum = Gtk.Label(xalign=0, wrap=True)
        self.r_sum.add_css_class("caption")
        box.append(self.r_sum)
        og = Adw.PreferencesGroup(title="Параметры")
        self.r_newid = Adw.SwitchRow(title="Новые идентификаторы", subtitle="Нужно, если оригинал и восстановленный диск будут подключены одновременно")
        self.r_verify = Adw.SwitchRow(title="Проверить после восстановления", active=True)
        og.add(self.r_newid)
        og.add(self.r_verify)
        box.append(og)
        self.r_msg = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.r_msg.add_css_class("caption")
        box.append(self.r_msg)
        self.r_go = Gtk.Button(label="Проверить план и продолжить", halign=Gtk.Align.START)
        self.r_go.add_css_class("suggested-action")
        self.r_go.add_css_class("pill")
        self.r_go.connect("clicked", lambda _b: self.check_restore())
        box.append(self.r_go)
        self.stack.add_named(sc, "restore")

    def start_restore_flow(self, folder, m):
        self.folder, self.manifest = folder, m
        src = m["source"]
        self.r_info.set_label(f"Копия «{os.path.basename(folder)}» от {m.get('created', '')}\nИсточник: {src.get('model') or src.get('path')} · "
                              f"{devices.human_size(src.get('size_bytes', 0))} · таблица {src.get('label', '').upper()}")
        parts = []
        for p in m["partitions"]:
            sp = planmod.SrcPart(str(p["index"]), p["index"], p["start"], p["size"], p.get("type") or "", p.get("name", ""),
                                 p.get("fs", ""), p.get("fs_label", ""), [])
            parts.append(sp)
        self.r_src_parts = parts
        for r in self._r_rows:
            self.r_parts_group.remove(r)
        self._r_rows = []
        for sp in parts:
            row = Adw.SwitchRow(title=f"Раздел {sp.number}: {sp.fs or 'без файловой системы'}" + (f" · {sp.label or sp.name}" if (sp.label or sp.name) else ""),
                                subtitle=devices.human_size(sp.size * 512), active=True)
            row.connect("notify::active", lambda r, _p, sp=sp: self._r_toggle(sp, r.get_active()))
            self.r_parts_group.add(row)
            self._r_rows.append(row)
        self.r_src_bar.title = "В копии"
        self.r_src_bar.set_data(max(src.get("sectors") or 0, max(p.start + p.size for p in parts)),
                                [Seg(f"{sp.number}  {sp.fs or ''}", sp.fs, sp.start, sp.size) for sp in parts])
        self.r_msg.set_label("")
        self.show_step("restore")
        self._r_layout()

    def _r_layout(self):
        d = self._sel(self.r_dst_row)
        if not d or not getattr(self, "r_src_parts", None):
            return
        self.dst = d
        self.layout = planmod.Layout(self.r_src_parts, d.size // 512, self.manifest["source"].get("label", "gpt"))
        for p, row in zip(self.layout.parts, self._r_rows):
            p.enabled = row.get_active()
        self.r_mode.set_selected(0)
        self._r_apply()

    def _r_toggle(self, sp, on):
        if not self.layout:
            return
        for p in self.layout.parts:
            if p.src is sp:
                p.enabled = on
        self._r_apply()

    def _r_apply(self):
        if not self.layout:
            return
        {0: self.layout.mode_stretch_largest, 1: self.layout.mode_asis, 2: self.layout.mode_proportional}[self.r_mode.get_selected()]()
        self._r_refresh()

    def _r_drag(self, idx, new_size):
        en = self.layout.enabled()
        if idx < len(en):
            self.layout.resize(self.layout.parts.index(en[idx]), new_size)
            self._r_refresh()

    def _r_refresh(self):
        L = self.layout
        self.r_dst_bar.title = f"Диск назначения: {self.dst.title}"
        self.r_dst_bar.set_data(L.target, [Seg(f"{p.src.number}  {p.src.fs or ''}", p.src.fs, p.start, p.size, p.src.size) for p in L.enabled()])
        ok = L.fits and bool(L.enabled())
        if not L.fits:
            self.r_sum.set_label(f"Не помещается: нужно минимум {devices.human_size(L.min_total() * 512)}")
        elif not L.enabled():
            self.r_sum.set_label("Не выбран ни один раздел")
        else:
            self.r_sum.set_label(f"Занято {devices.human_size(L.end * 512)} из {devices.human_size(L.target * 512)}, свободно {devices.human_size(max(L.free, 0) * 512)}.")
        for c in ("error", "dim-label"):
            self.r_sum.remove_css_class(c)
        self.r_sum.add_css_class("dim-label" if ok else "error")
        self.r_go.set_sensitive(ok and not self.dst.is_system and not self.dst.in_use)
        if self.dst.is_system:
            self.r_msg.set_label("Диск назначения системный: восстанавливать на него нельзя")
        elif self.dst.in_use:
            self.r_msg.set_label("Диск назначения используется: отмонтируйте его разделы")
        elif not self.r_msg.get_label().startswith(("Нельзя", "Отказ", "Предупр")):
            self.r_msg.set_label("")

    def _r_args(self):
        return dict(folder=self.folder, dst=self.dst.path, new_ids=self.r_newid.get_active(), verify=self.r_verify.get_active(),
                    parts=[{"index": int(p.src.node), "start": p.start, "size": p.size} for p in self.layout.enabled()])

    def check_restore(self):
        self.r_go.set_sensitive(False)
        self.r_msg.set_label("Проверяю план…")

        def ready():
            def got(msg):
                self.r_go.set_sensitive(True)
                if msg.get("ev") != "res":
                    self.r_msg.set_label("Отказ: " + msg.get("msg", "ошибка проверки"))
                    return
                d = msg["data"]
                if d["problems"]:
                    self.r_msg.set_label("Нельзя продолжать:\n" + "\n".join("• " + p for p in d["problems"]))
                    return
                self.r_msg.set_label(("Предупреждения:\n" + "\n".join("• " + w for w in d["warnings"])) if d["warnings"] else "План принят.")
                self.confirm_restore()

            self.store.helper.request("restore_check", got, **self._r_args())

        self.store.ensure_helper(ready, lambda m: (self.r_go.set_sensitive(True), self.window.toast(m)))

    def confirm_restore(self):
        name = os.path.basename(self.dst.path)
        parts = "\n".join(f"  • раздел {p.src.number} ({p.src.fs or '—'}): {devices.human_size(p.size * 512)}" for p in self.layout.enabled())
        body = (f"Диск назначения будет ПОЛНОСТЬЮ СТЁРТ:\n{self.dst.title}\n{self.dst.path} · {devices.human_size(self.dst.size)}"
                + (f" · серийный № {self.dst.serial}" if self.dst.serial else "")
                + f"\n\nНа него восстановится копия «{os.path.basename(self.folder)}»:\n{parts}\n\n"
                  f"Для подтверждения введите имя диска назначения: {name}")
        dlg = Adw.AlertDialog.new("Стереть диск и восстановить копию?", body)
        entry = Gtk.Entry(placeholder_text=name)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Отмена")
        dlg.add_response("go", "Восстановить")
        dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_response_enabled("go", False)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        entry.connect("changed", lambda e: dlg.set_response_enabled("go", e.get_text().strip() == name))
        dlg.choose(self.window, None, lambda d, r: d.choose_finish(r) == "go" and entry.get_text().strip() == name and self.start_restore(name))

    def start_restore(self, name):
        self.running = True
        self.steps.reset(f"Восстановление «{os.path.basename(self.folder)}» → {self.dst.title}")
        self.stop_btn.set_visible(True)
        self.done_btn.set_visible(False)
        self.show_step("run")
        self.job = self.store.helper.request("restore", lambda m: self._on_msg(m, "restore"), confirm=name, **self._r_args())

    # ------------------------------------------------------------ проверка и открытие
    def verify(self, folder):
        def ready():
            self.window.toast("Проверяю копию…")

            def got(msg):
                if msg.get("ev") != "res":
                    self.window.toast(msg.get("msg", "Не удалось проверить"))
                    return
                pr = msg["data"]["problems"]
                dlg = Adw.AlertDialog.new("Копия цела" if not pr else "Копия повреждена",
                                          "Контрольные суммы и целостность всех файлов в порядке." if not pr else "\n".join("• " + p for p in pr))
                dlg.add_response("ok", "Закрыть")
                dlg.present(self.window)

            self.store.helper.request("backup_verify", got, folder=folder)

        self.store.ensure_helper(ready, lambda m: self.window.toast(m))

    def open_contents(self, folder, m):
        cands = [p for p in m["partitions"] if p.get("file") and (p.get("fs") in bk.CLONERS or p.get("method") == "raw")]
        if not cands:
            self.window.toast("В копии нет разделов с данными")
            return
        if len(cands) == 1:
            self._extract_and_open(folder, cands[0])
            return
        dlg = Adw.AlertDialog.new("Какой раздел открыть?", "Раздел будет развернут во временный файл (для просмотра и выборочного восстановления).")
        dlg.add_response("cancel", "Отмена")
        for p in cands:
            dlg.add_response(str(p["index"]), f"Раздел {p['index']}: {p.get('fs') or '—'} · {devices.human_size(p['size'] * 512)}")
        dlg.set_close_response("cancel")

        def answered(d, r):
            ans = d.choose_finish(r)
            if ans != "cancel":
                self._extract_and_open(folder, next(p for p in cands if str(p["index"]) == ans))

        dlg.choose(self.window, None, answered)

    def _extract_and_open(self, folder, part):
        out_dir = os.path.expanduser("~/.cache/disk-plus/opened")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"{safe_name(os.path.basename(folder))}-part{part['index']}.img")
        self.window.toast("Разворачиваю раздел во временный файл…")

        def work():
            ok, err = bk.extract_partition(folder, part, out)
            GLib.idle_add(self._opened, ok, err, out)

        threading.Thread(target=work, daemon=True).start()

    def _opened(self, ok, err, out):
        if not ok:
            self.window.toast("Не удалось открыть: " + err)
            return False
        self.window.go_recover_image(out)
        return False

    # ------------------------------------------------------------ выполнение
    def _build_run(self):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(18)
        sc.set_child(box)
        self.steps = StepsView()
        box.append(self.steps)
        btns = Gtk.Box(spacing=8)
        self.stop_btn = Gtk.Button(label="Остановить")
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.connect("clicked", lambda _b: self.store.helper.request("stop", None, target=self.job))
        self.done_btn = Gtk.Button(label="Готово", visible=False)
        self.done_btn.add_css_class("suggested-action")
        self.done_btn.connect("clicked", lambda _b: (self.show_step("home"), self.reload_disks()))
        btns.append(self.stop_btn)
        btns.append(self.done_btn)
        box.append(btns)
        self.stack.add_named(sc, "run")

    def _on_msg(self, m, kind):
        ev = m.get("ev")
        if ev == "start":
            self.steps.on_start(m["steps"])
        elif ev == "step":
            self.steps.on_step(m)
        elif ev == "progress":
            self.steps.on_progress(m)
        elif ev == "done":
            self._finished(m, kind)
        elif ev == "err":
            self.running = False
            self.window.toast(m.get("msg", "Ошибка"))
            self.show_step("create" if kind == "backup" else "restore")

    def _finished(self, m, kind):
        self.running = False
        self.stop_btn.set_visible(False)
        self.done_btn.set_visible(True)
        reason = m.get("reason")
        if reason == "finished":
            self.steps.bar.set_fraction(1.0)
            self.steps.lbl.set_label("")
            if kind == "backup":
                head, tips = "Копия готова", f"Папка: {m.get('folder', '')}\nХраните её на другом диске. Проверить, открыть или восстановить копию можно на главной странице."
            else:
                head, tips = "Диск восстановлен", "Выключите компьютер и поставьте диск на место. Не включайте оригинал и копию одновременно, если у них одинаковые идентификаторы."
        elif reason == "stopped":
            head, tips = "Остановлено", "Неполная копия удалена." if kind == "backup" else "Диск назначения остался в неполном состоянии: операцию нужно запустить заново."
        else:
            head, tips = "Не удалось завершить", f"{m.get('error', '')}" + ("\nНеполная копия удалена." if kind == "backup" else "\nДиск назначения остался в неполном состоянии.")
        warns = [w for w in (m.get("warnings") or []) if "идентификаторы" not in w or kind == "restore"]
        self.steps.result.set_label(f"{head}\n{tips}" + (("\n\nЗамечания:\n" + "\n".join("• " + w for w in warns)) if warns else ""))
        self.steps.result.set_visible(True)
        self.store.refresh_disks()

    def set_narrow(self, narrow):
        pass
