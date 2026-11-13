"""Главное окно Disk++."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from .. import APP_NAME  # noqa: E402
from .backup_page import BackupPage
from .clone_page import ClonePage
from .disks_page import DisksPage  # noqa: E402
from .image_page import ImagePage
from .recover_page import RecoverPage
from .scan_page import ScanPage  # noqa: E402


def _value(v):
    val = GObject.Value()
    if isinstance(v, bool):
        val.init(GObject.TYPE_BOOLEAN)
        val.set_boolean(v)
    return val


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, store):
        super().__init__(application=app, title=APP_NAME)
        self.store = store
        self.set_default_size(1240, 820)
        self.set_size_request(380, 520)
        self.overlay = Adw.ToastOverlay()

        self.split = Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(230)
        self.split.set_max_sidebar_width(280)
        self.overlay.set_child(self.split)
        self.set_content(self.overlay)

        # --- страницы
        self.disks_page = DisksPage(self, store)
        self.scan_page = ScanPage(self, store)
        self.image_page = ImagePage(self, store)
        self.recover_page = RecoverPage(self, store)
        self.clone_page = ClonePage(self, store)
        self.backup_page = BackupPage(self, store)
        self.pages = [
            ("disks", "Диски", "drive-harddisk-symbolic", "ДИАГНОСТИКА", self.disks_page),
            ("scan", "Проверка поверхности", "view-grid-symbolic", "ДИАГНОСТИКА", self.scan_page),
            ("image", "Образ диска", "media-floppy-symbolic", "СПАСЕНИЕ ДАННЫХ", self.image_page),
            ("recover", "Восстановление файлов", "folder-saved-search-symbolic", "СПАСЕНИЕ ДАННЫХ", self.recover_page),
            ("clone", "Клонирование диска", "edit-copy-symbolic", "КОПИИ И ПЕРЕНОС", self.clone_page),
            ("backup", "Резервные копии", "document-save-symbolic", "КОПИИ И ПЕРЕНОС", self.backup_page),
        ]

        # --- боковая панель
        self.side_list = Gtk.ListBox()
        self.side_list.add_css_class("navigation-sidebar")
        self.side_list.set_header_func(self._side_header)
        for pid, title, icon, _sec, _w in self.pages:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=title, xalign=0))
            row.set_child(box)
            row.page_id = pid
            row.section = _sec
            self.side_list.append(row)
        self.side_list.connect("row-selected", self._on_row)
        stb = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.WindowTitle.new(APP_NAME, "Диски: проверка, спасение, копии"))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить список дисков")
        refresh.connect("clicked", lambda _b: store.refresh_disks())
        hb.pack_end(refresh)
        stb.add_top_bar(hb)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.side_list)
        stb.set_content(sc)
        self.split.set_sidebar(Adw.NavigationPage.new(stb, APP_NAME))

        # --- контент
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        for pid, _t, _i, _s, w in self.pages:
            self.stack.add_named(w, pid)
        self.content_title = Adw.WindowTitle()
        ctb = Adw.ToolbarView()
        chb = Adw.HeaderBar()
        chb.set_title_widget(self.content_title)
        ctb.add_top_bar(chb)
        ctb.set_content(self.stack)
        self.content_page = Adw.NavigationPage.new(ctb, "Диски")
        self.split.set_content(self.content_page)

        # --- адаптивность
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        bp.add_setter(self.split, "collapsed", _value(True))
        self.add_breakpoint(bp)
        self.bp_narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        self.connect("notify::default-width", lambda *_: None)
        bp.connect("apply", lambda *_: self._narrow(True))
        bp.connect("unapply", lambda *_: self._narrow(False))

        store.connect(self._on_store)
        self.select("disks")

    def _side_header(self, row, before):
        if before is None or before.section != row.section:
            lab = Gtk.Label(label=row.section, xalign=0)
            lab.add_css_class("caption-heading")
            lab.add_css_class("dim-label")
            lab.set_margin_start(12)
            lab.set_margin_top(10)
            lab.set_margin_bottom(4)
            row.set_header(lab)
        else:
            row.set_header(None)

    def _on_row(self, _lb, row):
        if row is not None:
            self.select(row.page_id, from_list=True)
            self.split.set_show_content(True)

    def select(self, pid, from_list=False):
        self.stack.set_visible_child_name(pid)
        for i, p in enumerate(self.pages):
            if p[0] == pid:
                self.content_title.set_title(p[1])
                self.content_page.set_title(p[1])
                if not from_list:
                    self.side_list.select_row(self.side_list.get_row_at_index(i))

    def _narrow(self, narrow):
        self.scan_page.set_narrow(narrow)
        self.image_page.set_narrow(narrow)
        self.recover_page.set_narrow(narrow)

    def go_clone(self, disk):
        self.clone_page.preselect_source(disk)
        self.select("clone")
        self.split.set_show_content(True)

    def go_recover_image(self, path):
        self.recover_page.open_image(path)
        self.select("recover")
        self.split.set_show_content(True)

    def go_image(self, disk):
        self.image_page.select_disk(disk)
        self.select("image")
        self.split.set_show_content(True)

    def go_scan(self, disk):
        self.scan_page.select_disk(disk)
        self.select("scan")
        self.split.set_show_content(True)

    def toast(self, text, quiet=False):
        if quiet:
            return
        self.overlay.add_toast(Adw.Toast(title=text, timeout=4))

    def _on_store(self, ev, payload):
        if ev == "toast":
            self.toast(payload)
