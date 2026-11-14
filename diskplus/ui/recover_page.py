"""Восстановление файлов: источник -> тома -> сканирование -> дерево папок с предпросмотром -> восстановление."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .. import devices, recent  # noqa: E402
from ..recovery import carve, extract, fat_scan, index as idxmod, ntfs_scan, volumes  # noqa: E402

MAX_IMG_PREVIEW = 12 * 1024 * 1024
TEXT_EXT = {".txt", ".md", ".log", ".csv", ".json", ".xml", ".html", ".htm", ".ini", ".conf", ".py", ".sh", ".c",
            ".h", ".cpp", ".js", ".css", ".yml", ".yaml", ".cfg", ".srt"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg", ".ico"}


@dataclass
class Source:
    path: str              # что реально открываем (устройство или файл образа)
    title: str
    device: bool
    size: int = 0
    disk: object = None


class FileItem(GObject.Object):
    selected = GObject.Property(type=bool, default=False)

    def __init__(self, node, flat=False):
        super().__init__()
        self.node = node
        self.flat = flat


def fmt_date(ts):
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts and ts > 0 else "—"


def icon_for(node):
    if node.is_dir:
        return Gio.ThemedIcon.new("folder-symbolic")
    ctype, _ = Gio.content_type_guess(node.name, None)
    return Gio.content_type_get_symbolic_icon(ctype) if ctype else Gio.ThemedIcon.new("text-x-generic-symbolic")


class RecoverPage(Gtk.Box):
    def __init__(self, window, store):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.store = window, store
        self.source: Source | None = None
        self.vols: list[volumes.Volume] = []
        self.vol: volumes.Volume | None = None
        self.index: idxmod.Index | None = None
        self.selected: set[int] = set()
        self.show_system = False
        self.deleted_only = False
        self.cancel = threading.Event()
        self._preview_token = 0

        # верхняя полоса навигации по шагам
        self.bar = Gtk.Box(spacing=8)
        self.bar.add_css_class("dp-step-bar")
        for m in ("start", "end"):
            getattr(self.bar, f"set_margin_{m}")(30)
        self.bar.set_margin_top(0)
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
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.append(self.stack)
        self._build_source()
        self._build_volumes()
        self._build_scan()
        self._build_browser()
        self.show_step("source")
        store.connect(self._on_store)

    # ------------------------------------------------------------------ шаги
    def show_step(self, name):
        self.step = name
        self.stack.set_visible_child_name(name)
        self.back_btn.set_visible(name != "source")
        self.crumb.set_label({"source": "Откуда восстанавливаем?", "volumes": self.source.title if self.source else "",
                              "scan": "Сканирование…",
                              "browser": f"{self.source.title} · {self.vol.title if self.vol else ''}" if self.source else ""}[name])

    def go_back(self):
        if self.step == "scan":
            self.cancel.set()
            return
        self.show_step({"volumes": "source", "browser": "volumes"}.get(self.step, "source"))

    # ------------------------------------------------------------------ 1. источник
    def _build_source(self):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(30)
        clamp = Adw.Clamp(maximum_size=960, tightening_threshold=700)
        clamp.set_child(box)
        sc.set_child(clamp)
        intro = Gtk.Label(wrap=True, xalign=0,
                          label="Выберите накопитель или файл образа. Для повреждённого диска сначала создайте образ.")
        intro.add_css_class("dim-label")
        box.append(intro)
        self.src_disks = Adw.PreferencesGroup(title="Диски")
        box.append(self.src_disks)
        self.src_recent = Adw.PreferencesGroup(title="Образы")
        box.append(self.src_recent)
        row = Adw.ActionRow(title="Открыть файл образа…", subtitle=".img, .dd, .raw и подобные")
        b = Gtk.Button(label="Выбрать…", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda _b: self.pick_image())
        row.add_suffix(b)
        row.set_activatable_widget(b)
        self.src_recent.add(row)
        self._recent_rows = []
        self.stack.add_named(sc, "source")
        self.refill_source()

    def refill_source(self):
        for r in getattr(self, "_disk_rows", []):
            self.src_disks.remove(r)
        self._disk_rows = []
        for d in self.store.disks:
            row = Adw.ActionRow(title=d.title, subtitle=f"{d.kind} · {devices.human_size(d.size)} · {d.path}")
            b = Gtk.Button(label="Открыть", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, d=d: self.open_disk(d))
            row.add_suffix(b)
            self.src_disks.add(row)
            self._disk_rows.append(row)
        for r in self._recent_rows:
            self.src_recent.remove(r)
        self._recent_rows = []
        for p in recent.load():
            row = Adw.ActionRow(title=os.path.basename(p), subtitle=os.path.dirname(p))
            row.set_subtitle_lines(1)
            b = Gtk.Button(label="Открыть", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, p=p: self.open_image(p))
            row.add_suffix(b)
            self.src_recent.add(row)
            self._recent_rows.append(row)

    def _on_store(self, ev, payload):
        if ev == "disks" and self.step == "source":
            self.refill_source()

    def pick_image(self):
        dlg = Gtk.FileDialog(title="Файл образа")

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.open_image(f.get_path())

        dlg.open(self.window, None, done)

    def open_image(self, path):
        if not os.path.isfile(path):
            self.window.toast("Файл не найден")
            return
        recent.add(path)
        self.set_source(Source(path, os.path.basename(path), False, os.path.getsize(path)))

    def open_disk(self, d):
        def ready():
            def got(msg):
                if msg.get("ev") != "res":
                    self.window.toast(msg.get("msg", "Не удалось получить доступ к диску"))
                    return
                self.set_source(Source(msg["data"]["dev"], d.title, True, d.size, d))

            self.store.helper.request("grant_read", got, dev=d.path)

        self.store.ensure_helper(ready, lambda m: self.window.toast(m))

    def open_path_image(self, path):
        """Вызывается снаружи (после снятия образа)."""
        self.open_image(path)

    # ------------------------------------------------------------------ 2. тома
    def _build_volumes(self):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(30)
        sc.set_child(box)
        self.vol_status = Gtk.Label(wrap=True, xalign=0)
        self.vol_status.add_css_class("dim-label")
        box.append(self.vol_status)
        self.vol_group = Adw.PreferencesGroup(title="Разделы и файловые системы")
        box.append(self.vol_group)
        self._vol_rows = []
        lost = Adw.PreferencesGroup(title="Нет нужного раздела?",
                                    description="Быстрый поиск потерянных разделов проверяет загрузочные секторы "
                                                "через каждый мегабайт. Глубокий поиск будет в следующих версиях.")
        self.lost_row = Adw.ActionRow(title="Искать потерянные разделы")
        self.lost_btn = Gtk.Button(label="Искать", valign=Gtk.Align.CENTER)
        self.lost_btn.connect("clicked", lambda _b: self.find_lost())
        self.lost_row.add_suffix(self.lost_btn)
        lost.add(self.lost_row)
        box.append(lost)
        sig = Adw.PreferencesGroup(title="Поиск по содержимому (PhotoRec)",
                                   description="Когда файловая система не читается совсем. Находит фото, видео, документы "
                                               "и архивы по их внутреннему устройству, но без имён и папок. "
                                               "Документы Office (docx, xlsx) находятся как архивы zip.")
        self.carve_row = Adw.ComboRow(title="Что искать", model=Gtk.StringList.new([v[0] for v in carve.PRESETS.values()]))
        sig.add(self.carve_row)
        crow = Adw.ActionRow(title="Искать и сохранить найденное", subtitle="Нужно выбрать папку на другом диске")
        cb = Gtk.Button(label="Искать…", valign=Gtk.Align.CENTER)
        cb.connect("clicked", lambda _b: self.pick_carve_dest())
        crow.add_suffix(cb)
        sig.add(crow)
        box.append(sig)
        self.stack.add_named(sc, "volumes")

    def set_source(self, src):
        self.source = src
        self.vols = []
        self.vol_status.set_label("Читаю таблицу разделов…")
        self.vol_status.set_visible(True)
        self._fill_volumes()
        self.show_step("volumes")
        threading.Thread(target=self._load_volumes, daemon=True).start()

    def _load_volumes(self):
        vols = volumes.list_volumes(self.source.path)
        GLib.idle_add(self._volumes_ready, vols)

    def _volumes_ready(self, vols):
        self.vols = vols
        self.vol_status.set_label("" if vols else
                                  "Разделов и файловых систем не найдено. Таблица могла быть повреждена: попробуйте поиск потерянных разделов.")
        self.vol_status.set_visible(not vols)
        self._fill_volumes()
        return False

    def _fill_volumes(self):
        for r in self._vol_rows:
            self.vol_group.remove(r)
        self._vol_rows = []
        for v in self.vols:
            name = v.label or v.desc or "Без названия"
            row = Adw.ActionRow(title=f"{v.title} · {name}",
                                subtitle=f"{devices.human_size(v.size_bytes)} · начало: сектор {v.offset}"
                                         + (" · найден поиском" if v.source == "scan" else ""))
            if v.supported:
                path = idxmod.db_path_for(self.source.path, v.offset, v.length)
                cached = os.path.exists(path) and idxmod.Index(path).complete
                b = Gtk.Button(label="Открыть сохранённое" if cached else "Сканировать", valign=Gtk.Align.CENTER)
                b.add_css_class("suggested-action")
                b.connect("clicked", lambda _b, v=v: self.start_scan(v, use_cache=True))
                row.add_suffix(b)
                if v.fs in ("ntfs", "fat32", "exfat"):
                    bd = Gtk.Button(label="Глубокий поиск", valign=Gtk.Align.CENTER,
                                    tooltip_text="Для отформатированных и повреждённых томов: ищет остатки записей файлов по всему тому")
                    bd.connect("clicked", lambda _b, v=v: self.start_deep(v))
                    row.add_suffix(bd)
                if cached:
                    b2 = Gtk.Button(label="Пересканировать", valign=Gtk.Align.CENTER)
                    b2.connect("clicked", lambda _b, v=v: self.start_scan(v, use_cache=False))
                    row.add_suffix(b2)
            else:
                msg = ("Зашифрован: сначала разблокируйте" if v.fs in ("luks", "bitlocker") else
                       "Пока не читается напрямую" if v.fs != "unknown" else "Тип не определён")
                lab = Gtk.Label(label=msg)
                lab.add_css_class("dim-label")
                row.add_suffix(lab)
            self.vol_group.add(row)
            self._vol_rows.append(row)

    def find_lost(self):
        if self.source is None:
            return
        self.lost_btn.set_sensitive(False)
        self.lost_btn.set_label("Ищу…")
        self.cancel.clear()

        def run():
            def prog(frac):
                GLib.idle_add(self.lost_btn.set_label, f"Ищу… {int(frac * 100)}%")

            found = volumes.scan_lost(self.source.path, self.source.size or os.path.getsize(self.source.path)
                                      if not self.source.device else self.source.size,
                                      self.vols, 512, on_progress=prog)
            GLib.idle_add(self._lost_done, found)

        threading.Thread(target=run, daemon=True).start()

    def _lost_done(self, found):
        self.lost_btn.set_sensitive(True)
        self.lost_btn.set_label("Искать")
        if found:
            self.vols += found
            self.window.toast(f"Найдено томов: {len(found)}")
        else:
            self.window.toast("Потерянных разделов не найдено")
        self._fill_volumes()
        return False

    # ------------------------------------------------------------------ 3. сканирование
    def _build_scan(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER,
                      halign=Gtk.Align.CENTER)
        self.scan_spin = Gtk.Spinner(spinning=True)
        self.scan_spin.set_size_request(48, 48)
        box.append(self.scan_spin)
        self.scan_title = Gtk.Label(label="Читаю файловую систему…")
        self.scan_title.add_css_class("title-2")
        box.append(self.scan_title)
        self.scan_count = Gtk.Label(label="")
        self.scan_count.add_css_class("dim-label")
        box.append(self.scan_count)
        self.scan_bar = Gtk.ProgressBar(visible=False)
        self.scan_bar.set_size_request(360, -1)
        box.append(self.scan_bar)
        b = Gtk.Button(label="Отмена", halign=Gtk.Align.CENTER)
        b.connect("clicked", lambda _b: self.cancel.set())
        box.append(b)
        self.stack.add_named(box, "scan")

    def start_scan(self, v, use_cache=True):
        self.vol = v
        path = idxmod.db_path_for(self.source.path, v.offset, v.length)
        idx = idxmod.Index(path)
        if use_cache and idx.complete:
            self.open_browser(idx)
            return
        self.cancel.clear()
        self.scan_count.set_label("Найдено объектов: 0")
        self.scan_title.set_label("Читаю файловую систему…")
        self.scan_bar.set_visible(False)
        self.scan_spin.set_visible(True)
        self.show_step("scan")

        def prog(n, el):
            GLib.idle_add(self.scan_count.set_label, f"Найдено объектов: {n:,}  ·  {int(el)} с".replace(",", " "))

        def run():
            ok, err = idxmod.scan_volume(self.source.path, v.offset, v.sector, idx, prog, self.cancel)
            GLib.idle_add(self._scan_done, idx, ok, err)

        threading.Thread(target=run, daemon=True).start()

    def _scan_done(self, idx, ok, err):
        if not ok:
            self.show_step("volumes")
            if err and err != "отменено":
                self.window.toast("Не удалось прочитать файловую систему: " + err)
            return False
        self.open_browser(idx)
        return False

    # --- глубокий поиск
    def start_deep(self, v, use_cache=True):
        self.vol = v
        path = idxmod.db_path_for(self.source.path, v.offset, v.length, deep=True)
        idx = idxmod.Index(path)
        if use_cache and idx.complete:
            self.open_browser(idx, deep=True)
            return
        self.cancel.clear()
        self.scan_title.set_label("Глубокий поиск…")
        self.scan_count.set_label("Читаю том целиком, это может занять много времени")
        self.scan_spin.set_visible(False)
        self.scan_bar.set_visible(True)
        self.scan_bar.set_fraction(0)
        self.show_step("scan")

        def prog(frac, n, el):
            GLib.idle_add(self._deep_prog, frac, n, el)

        def run():
            mod = ntfs_scan if v.fs == "ntfs" else fat_scan
            ok, err, info = mod.deep_scan(self.source.path, v.offset, v.length, v.sector, idx, prog, self.cancel)
            GLib.idle_add(self._deep_done, idx, ok, err, info)

        threading.Thread(target=run, daemon=True).start()

    def _deep_prog(self, frac, n, el):
        self.scan_bar.set_fraction(min(1.0, frac))
        self.scan_count.set_label(f"{int(frac * 100)}%  ·  найдено записей: {n:,}  ·  {int(el)} с".replace(",", " "))
        return False

    def _deep_done(self, idx, ok, err, info):
        self.scan_bar.set_visible(False)
        self.scan_spin.set_visible(True)
        if not ok:
            self.show_step("volumes")
            if err and err != "отменено":
                self.window.toast("Глубокий поиск не удался: " + err)
            return False
        if info.get("geometry", "").startswith("размер кластера"):
            self.window.toast("Размер кластера определён приблизительно: часть файлов может оказаться повреждённой")
        self.open_browser(idx, deep=True)
        return False

    # --- поиск по сигнатурам
    def pick_carve_dest(self):
        dlg = Gtk.FileDialog(title="Папка для найденных файлов")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self._start_carve(f.get_path())

        dlg.select_folder(self.window, None, done)

    def _start_carve(self, dest):
        if self._same_disk(dest):
            self.window.toast("Выберите папку на другом диске: на этом найденное может затереть ещё не найденные данные")
            return
        preset = list(carve.PRESETS)[self.carve_row.get_selected()]
        folder = os.path.join(dest, "Disk++ поиск " + time.strftime("%Y-%m-%d %H-%M"))
        self.cancel.clear()
        self.scan_title.set_label("Поиск по содержимому…")
        self.scan_count.set_label("Подготовка…")
        self.scan_spin.set_visible(False)
        self.scan_bar.set_visible(True)
        self.scan_bar.set_fraction(0)
        self.step_back = "volumes"
        self.show_step("scan")

        def prog(frac, files, el, pas):
            GLib.idle_add(self._carve_prog, frac, files, el, pas)

        def run():
            ok, err, counts = carve.run(self.source.path, folder, preset, prog, self.cancel)
            groups = carve.organize(folder) if (ok or counts) else {}
            GLib.idle_add(self._carve_done, ok, err, groups, folder)

        threading.Thread(target=run, daemon=True).start()

    def _carve_prog(self, frac, files, el, pas):
        self.scan_bar.set_fraction(min(1.0, frac))
        self.scan_count.set_label(f"Проход {pas}  ·  {int(frac * 100)}%  ·  найдено файлов: {files}  ·  {int(el)} с")
        return False

    def _carve_done(self, ok, err, groups, folder):
        self.scan_bar.set_visible(False)
        self.scan_spin.set_visible(True)
        self.show_step("volumes")
        total = sum(groups.values())
        if not ok and err and err != "отменено":
            self.window.toast("Поиск не удался: " + err)
            return False
        head = "Поиск остановлен" if err == "отменено" else "Поиск завершён"
        body = (f"Найдено файлов: {total}\n" + "\n".join(f"• {g}: {n}" for g, n in groups.items()) + f"\n\n{folder}") if total \
            else "Файлов не найдено. Попробуйте набор «Все типы» или глубокий поиск."
        dlg = Adw.AlertDialog.new(head, body)
        dlg.add_response("close", "Закрыть")
        if total:
            dlg.add_response("open", "Показать в папке")
            dlg.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("open")
        dlg.set_close_response("close")

        def answered(d, r):
            if d.choose_finish(r) == "open":
                try:
                    Gio.AppInfo.launch_default_for_uri("file://" + folder, None)
                except GLib.Error:
                    pass

        dlg.choose(self.window, None, answered)
        return False

    # ------------------------------------------------------------------ 4. обзор файлов
    def _build_browser(self):
        self.split = Adw.OverlaySplitView(sidebar_position=Gtk.PackType.END, min_sidebar_width=300,
                                          max_sidebar_width=400, sidebar_width_fraction=0.32)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.hint = Adw.Banner(title="Здесь почти пусто. Если диск форматировали или он повреждён, запустите глубокий поиск",
                               button_label="Глубокий поиск")
        self.hint.connect("button-clicked", lambda *_: self.vol and self.start_deep(self.vol, use_cache=False))
        outer.append(self.hint)
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, vexpand=True)
        for m in ("start", "end", "bottom"):
            getattr(main, f"set_margin_{m}")(14)
        tools = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text="Поиск по имени", hexpand=True)
        self.search.connect("search-changed", lambda _e: self.refresh_list())
        tools.append(self.search)
        self.tg_del = Gtk.ToggleButton(label="Только удалённые")
        self.tg_del.connect("toggled", lambda b: self._set_deleted(b.get_active()))
        tools.append(self.tg_del)
        self.tg_sys = Gtk.ToggleButton(label="Служебные", tooltip_text="Показать служебные файлы файловой системы")
        self.tg_sys.connect("toggled", lambda b: self._set_system(b.get_active()))
        tools.append(self.tg_sys)
        self.btn_recover = Gtk.Button(label="Восстановить", sensitive=False)
        self.btn_recover.add_css_class("suggested-action")
        self.btn_recover.connect("clicked", lambda _b: self.on_recover())
        tools.append(self.btn_recover)
        main.append(tools)
        self.summary = Gtk.Label(xalign=0)
        self.summary.add_css_class("caption")
        self.summary.add_css_class("dim-label")
        main.append(self.summary)

        self.colview = Gtk.ColumnView(show_row_separators=True, show_column_separators=False)
        self.colview.add_css_class("card")
        self._add_columns()
        sc = Gtk.ScrolledWindow(vexpand=True, hexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.colview)
        main.append(sc)

        self.prog_box = Gtk.Box(spacing=10, visible=False)
        self.prog_bar = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER)
        self.prog_lbl = Gtk.Label(xalign=0, ellipsize=3, max_width_chars=40)
        self.prog_lbl.add_css_class("caption")
        cb = Gtk.Button(label="Отмена")
        cb.connect("clicked", lambda _b: self.cancel.set())
        self.prog_box.append(self.prog_lbl)
        self.prog_box.append(self.prog_bar)
        self.prog_box.append(cb)
        main.append(self.prog_box)
        outer.append(main)
        self.split.set_content(outer)

        # панель предпросмотра
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("start", "end", "top", "bottom"):
            getattr(side, f"set_margin_{m}")(14)
        self.pv_stack = Gtk.Stack(vexpand=False)
        self.pv_stack.set_size_request(-1, 230)
        hint = Adw.StatusPage(icon_name="document-open-symbolic", title="Предпросмотр",
                              description="Выберите файл в списке")
        hint.add_css_class("compact")
        self.pv_stack.add_named(hint, "none")
        self.pv_pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.pv_stack.add_named(self.pv_pic, "image")
        self.pv_text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.pv_text.set_top_margin(6)
        self.pv_text.set_left_margin(8)
        tsc = Gtk.ScrolledWindow()
        tsc.set_child(self.pv_text)
        self.pv_stack.add_named(tsc, "text")
        self.pv_none = Adw.StatusPage(icon_name="dialog-information-symbolic", title="Нет предпросмотра")
        self.pv_none.add_css_class("compact")
        self.pv_stack.add_named(self.pv_none, "nopreview")
        fr = Gtk.Frame()
        fr.set_child(self.pv_stack)
        side.append(fr)
        self.pv_name = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.pv_name.add_css_class("title-4")
        side.append(self.pv_name)
        self.pv_info = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.pv_info.add_css_class("caption")
        side.append(self.pv_info)
        self.pv_warn = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.pv_warn.add_css_class("caption")
        self.pv_warn.add_css_class("warning")
        side.append(self.pv_warn)
        self.split.set_sidebar(side)
        self.stack.add_named(self.split, "browser")

    def _add_columns(self):
        # флажок
        f = Gtk.SignalListItemFactory()
        f.connect("setup", lambda _f, li: li.set_child(Gtk.CheckButton()))

        def bind_chk(_f, li):
            row = li.get_item()
            item = row.get_item()
            chk = li.get_child()
            chk.set_active(item.selected)
            li._h = chk.connect("toggled", lambda c, it=item: self._toggle(it, c.get_active()))

        def unbind_chk(_f, li):
            h = getattr(li, "_h", None)
            if h:
                li.get_child().disconnect(h)
                li._h = None

        f.connect("bind", bind_chk)
        f.connect("unbind", unbind_chk)
        col = Gtk.ColumnViewColumn(title="", factory=f)
        col.set_fixed_width(44)
        self.colview.append_column(col)
        # имя
        f2 = Gtk.SignalListItemFactory()

        def setup_name(_f, li):
            exp = Gtk.TreeExpander()
            box = Gtk.Box(spacing=8)
            img = Gtk.Image()
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            name = Gtk.Label(xalign=0, ellipsize=3)
            sub = Gtk.Label(xalign=0, ellipsize=3)
            sub.add_css_class("caption")
            sub.add_css_class("dim-label")
            vb.append(name)
            vb.append(sub)
            box.append(img)
            box.append(vb)
            exp.set_child(box)
            li.set_child(exp)

        def bind_name(_f, li):
            row = li.get_item()
            item = row.get_item()
            exp = li.get_child()
            exp.set_list_row(row)
            box = exp.get_child()
            img = box.get_first_child()
            vb = img.get_next_sibling()
            name = vb.get_first_child()
            sub = name.get_next_sibling()
            img.set_from_gicon(icon_for(item.node))
            name.set_label(item.node.name)
            if item.flat:
                sub.set_label(os.path.dirname(item.node.path) or "/")
                sub.set_visible(True)
            else:
                sub.set_visible(False)

        f2.connect("setup", setup_name)
        f2.connect("bind", bind_name)
        col2 = Gtk.ColumnViewColumn(title="Имя", factory=f2)
        col2.set_expand(True)
        self.colview.append_column(col2)

        def text_col(title, fn, width, css=None):
            ff = Gtk.SignalListItemFactory()
            ff.connect("setup", lambda _f, li: li.set_child(Gtk.Label(xalign=0)))

            def b(_f, li):
                lab = li.get_child()
                node = li.get_item().get_item().node
                lab.set_label(fn(node))
                for c in ("dim-label",):
                    lab.remove_css_class(c)
                lab.add_css_class("dim-label")

            ff.connect("bind", b)
            c = Gtk.ColumnViewColumn(title=title, factory=ff)
            c.set_fixed_width(width)
            self.colview.append_column(c)

        text_col("Размер", lambda n: "—" if n.is_dir else devices.human_size(n.size), 90)
        text_col("Изменён", lambda n: fmt_date(n.mtime), 130)
        # статус
        f3 = Gtk.SignalListItemFactory()
        f3.connect("setup", lambda _f, li: li.set_child(Gtk.Label(xalign=0, halign=Gtk.Align.START, valign=Gtk.Align.CENTER)))

        def bind_st(_f, li):
            lab = li.get_child()
            n = li.get_item().get_item().node
            for c in ("dp-pill", "dp-pill-3", "dp-pill-1", "dp-pill--1"):
                lab.remove_css_class(c)
            if n.deleted:
                lab.set_label("перезаписан?" if n.realloc else "удалён")
                lab.add_css_class("dp-pill")
                lab.add_css_class("dp-pill-1" if n.realloc else "dp-pill-3")
            else:
                lab.set_label("")

        f3.connect("setup", lambda *_: None)
        f3.connect("bind", bind_st)
        col3 = Gtk.ColumnViewColumn(title="Статус", factory=f3)
        col3.set_fixed_width(110)
        self.colview.append_column(col3)

    # --- данные списка
    def open_browser(self, idx, deep=False):
        self.index = idx
        self.deep = deep
        self.selected.clear()
        self.search.set_text("")
        self.tg_del.set_active(False)
        total, deleted = idx.counts()
        self._counts = (total, deleted)
        self.hint.set_revealed(bool(not deep and total < 5 and self.vol and self.vol.fs in ("ntfs", "fat32", "exfat")))
        self.show_step("browser")
        self.refresh_list()
        self._update_summary()
        self._show_preview(None)

    def _update_summary(self):
        total, deleted = self._counts
        txt = f"Файлов: {total:,}, из них удалённых: {deleted:,}".replace(",", " ")
        if getattr(self, "deep", False):
            txt = f"Глубокий поиск: найдено файлов {total:,}.".replace(",", " ")
            if self.vol and self.vol.fs != "ntfs":
                txt += " Названия папок верхнего уровня могли потеряться; файлы читаются в предположении, что лежат подряд."
        if self.vol and self.vol.fs in ("exfat", "ext4", "ext3", "ext2"):
            txt += ".  Для этой файловой системы удалённые файлы читаются ограниченно."
        self.summary.set_label(txt)

    def _make_model(self, nodes, flat=False):
        st = Gio.ListStore.new(FileItem)
        for n in nodes:
            it = FileItem(n, flat)
            it.selected = n.id in self.selected
            st.append(it)
        return st

    def refresh_list(self):
        if not self.index:
            return
        q = self.search.get_text().strip()
        if q or self.deleted_only:
            nodes = self.index.search(q, self.deleted_only)
            model = Gtk.TreeListModel.new(self._make_model(nodes, True), False, False, lambda _i: None)
            n = len(nodes)
            self.summary.set_label(f"Найдено: {n}" + (" (показаны первые 3000)" if n >= 3000 else ""))
        else:
            kids = self.index.children(0, self.show_system, False)
            model = Gtk.TreeListModel.new(self._make_model(kids), False, False, self._children_of)
            self._update_summary()
        self.sel = Gtk.SingleSelection(model=model, autoselect=False, can_unselect=True)
        self.sel.connect("notify::selected-item", self._on_select)
        self.colview.set_model(self.sel)

    def _children_of(self, item):
        n = item.node
        if not n.is_dir:
            return None
        kids = self.index.children(n.id, self.show_system, False)
        return self._make_model(kids) if kids else None

    def _set_deleted(self, on):
        self.deleted_only = on
        self.refresh_list()

    def _set_system(self, on):
        self.show_system = on
        self.refresh_list()

    # --- выбор и предпросмотр
    def _toggle(self, item, on):
        item.selected = on
        (self.selected.add if on else self.selected.discard)(item.node.id)
        n = len(self.selected)
        self.btn_recover.set_sensitive(n > 0)
        self.btn_recover.set_label(f"Восстановить ({n})" if n else "Восстановить")

    def _on_select(self, sel, _p):
        row = sel.get_selected_item()
        self._show_preview(row.get_item().node if row else None)

    def _show_preview(self, node):
        self._preview_token += 1
        token = self._preview_token
        self.pv_warn.set_visible(False)
        if node is None:
            self.pv_stack.set_visible_child_name("none")
            self.pv_name.set_label("")
            self.pv_info.set_label("")
            return
        self.pv_name.set_label(node.name)
        info = [("Тип", "Папка" if node.is_dir else "Файл"), ("Размер", "—" if node.is_dir else devices.human_size(node.size)),
                ("Изменён", fmt_date(node.mtime)), ("Путь", "/" + node.path), ("Запись (inode)", node.inode)]
        self.pv_info.set_label("\n".join(f"{k}: {v}" for k, v in info))
        if node.deleted:
            self.pv_warn.set_label("Файл удалён. Если его место уже занято новыми данными, содержимое может быть повреждено."
                                   if not node.realloc else "Запись перезаписана другим файлом, содержимое ненадёжно.")
            self.pv_warn.set_visible(True)
        ext = os.path.splitext(node.name)[1].lower()
        if node.is_dir or node.size == 0:
            self.pv_none.set_title("Пустой файл" if not node.is_dir else "Папка")
            self.pv_stack.set_visible_child_name("nopreview")
            return
        if ext in IMG_EXT and node.size <= MAX_IMG_PREVIEW:
            kind, limit = "image", MAX_IMG_PREVIEW
        elif ext in TEXT_EXT:
            kind, limit = "text", 64 * 1024
        else:
            self.pv_none.set_title("Нет предпросмотра")
            self.pv_stack.set_visible_child_name("nopreview")
            return
        self.pv_none.set_title("Загружаю…")
        self.pv_stack.set_visible_child_name("nopreview")
        v, src = self.vol, self.source

        def work():
            data = extract.read_head(src.path, v.offset, v.sector, node, limit,
                                     cluster=int(self.index.meta("cluster", 4096)))
            GLib.idle_add(self._preview_ready, token, kind, data)

        threading.Thread(target=work, daemon=True).start()

    def _preview_ready(self, token, kind, data):
        if token != self._preview_token:
            return False
        if not data:
            self.pv_none.set_title("Не удалось прочитать содержимое")
            self.pv_stack.set_visible_child_name("nopreview")
            return False
        if kind == "image":
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                self.pv_pic.set_paintable(tex)
                self.pv_stack.set_visible_child_name("image")
            except GLib.Error:
                self.pv_none.set_title("Изображение не открывается (возможно, повреждено)")
                self.pv_stack.set_visible_child_name("nopreview")
        else:
            self.pv_text.get_buffer().set_text(data.decode("utf-8", "replace"))
            self.pv_stack.set_visible_child_name("text")
        return False

    # --- восстановление
    def on_recover(self):
        if not self.selected:
            return
        dlg = Gtk.FileDialog(title="Куда сохранить восстановленные файлы")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self._check_dest(f.get_path())

        dlg.select_folder(self.window, None, done)

    def _same_disk(self, dest):
        if not self.source or not self.source.disk:
            return False
        from .image_page import mount_of
        owner = mount_of(dest, self.store.disks)
        return bool(owner and owner.path == self.source.disk.path)

    def _check_dest(self, dest):
        if self._same_disk(dest):
            dlg = Adw.AlertDialog.new("Это тот же диск",
                                      "Сохранение на тот же диск, с которого восстанавливаем, может перезаписать "
                                      "ещё не восстановленные данные. Выберите другой диск.")
            dlg.add_response("cancel", "Выбрать другую папку")
            dlg.add_response("go", "Всё равно сохранить здесь")
            dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")
            dlg.choose(self.window, None, lambda d, r: (self._run_recover(dest) if d.choose_finish(r) == "go"
                                                       else self.on_recover()))
            return
        self._run_recover(dest)

    def _run_recover(self, dest):
        self.cancel.clear()
        self.prog_box.set_visible(True)
        self.btn_recover.set_sensitive(False)
        self.prog_bar.set_fraction(0)
        self.prog_lbl.set_label("Подготовка…")
        ids = list(self.selected)
        v, src, idx = self.vol, self.source, self.index

        def prog(done, total, got, total_bytes, cur):
            GLib.idle_add(self._rec_progress, done, total, got, total_bytes, cur)

        def run():
            res = extract.recover(src.path, v.offset, v.sector, idx, ids, dest, prog, self.cancel, fs=v.fs)
            GLib.idle_add(self._rec_done, res)

        threading.Thread(target=run, daemon=True).start()

    def _rec_progress(self, done, total, got, total_bytes, cur):
        self.prog_bar.set_fraction(min(1.0, got / total_bytes) if total_bytes else (done / total if total else 0))
        self.prog_lbl.set_label(f"{done} из {total}  ·  {os.path.basename(cur)}")
        return False

    def _rec_done(self, res):
        self.prog_box.set_visible(False)
        self.btn_recover.set_sensitive(bool(self.selected))
        head = "Восстановление отменено" if res.cancelled else "Готово"
        body = f"Восстановлено файлов: {res.ok} ({devices.human_size(res.bytes)})\n{res.dest}"
        if res.failed:
            body += f"\n\nНе удалось: {len(res.failed)}\n" + "\n".join(f"• {p}: {r}" for p, r in res.failed[:8])
            if len(res.failed) > 8:
                body += f"\n… и ещё {len(res.failed) - 8}"
        if res.warnings:
            body += f"\n\nС замечаниями: {len(res.warnings)} (размер отличается от записанного)"
        dlg = Adw.AlertDialog.new(head, body)
        dlg.add_response("close", "Закрыть")
        dlg.add_response("open", "Показать в папке")
        dlg.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("open")
        dlg.set_close_response("close")

        def answered(d, r):
            if d.choose_finish(r) == "open":
                try:
                    Gio.AppInfo.launch_default_for_uri("file://" + res.dest, None)
                except GLib.Error:
                    pass

        dlg.choose(self.window, None, answered)
        return False

    # --- адаптивность
    def set_narrow(self, narrow):
        self.split.set_collapsed(narrow)
        if narrow:
            self.split.set_show_sidebar(False)
        else:
            self.split.set_show_sidebar(True)
