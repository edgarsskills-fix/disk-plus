"""Точка входа Disk++."""
from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, GLib, Gtk, Graphene  # noqa: E402

from . import APP_ID, APP_NAME  # noqa: E402
from .state import Store  # noqa: E402
from .ui.widgets import install_css  # noqa: E402
from .ui.window import MainWindow  # noqa: E402

SHOT = os.environ.get("DISKPLUS_SHOT", "")
AUTOTEST = os.environ.get("DISKPLUS_AUTOTEST", "")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=os.environ.get("DISKPLUS_APP_ID") or APP_ID)    # отдельный id нужен только автотестам
        self.store = Store()
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        install_css()
        Gtk.Window.set_default_icon_name(APP_ID)
        if os.environ.get("DISKPLUS_DARK"):
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    def do_activate(self):
        if not self.win:
            self.win = MainWindow(self, self.store)
            self.win.connect("close-request", self._close)
            self.store.refresh_disks()
        size = os.environ.get("DISKPLUS_SIZE")      # только для автотестов: например 1920x1000
        if size:
            wd, ht = (int(v) for v in size.split("x"))
            self.win.set_default_size(wd, ht)
        self.win.present()
        if SHOT:
            self.win.stack.set_transition_type(Gtk.StackTransitionType.NONE)
            for pg in (self.win.recover_page, self.win.clone_page):
                pg.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        if AUTOTEST:
            GLib.timeout_add(600, self._autotest)

    def _close(self, _w):
        self.store.helper.stop()
        return False

    # --- скрытая самопроверка ----------------------------------------------------
    def shot(self, name, _try=0):
        if not SHOT:
            return False
        if _try == 0:     # скрытому окну GTK не рисует кадры: выводим вперёд и ждём перерисовку
            self.win.present()
            GLib.timeout_add(450, lambda: self.shot(name, 1))
            return False
        w = self.win
        width, height = w.get_width(), w.get_height()
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(w).snapshot(snap, width, height)
        node = snap.to_node()
        if node is None:
            if _try < 6:
                GLib.timeout_add(700, lambda: self.shot(name, _try + 1))
            else:
                print("снимок пропущен:", name, flush=True)
            return False
        tex = w.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, width, height))
        tex.save_to_png(f"{SHOT}-{name}.png")
        print(f"снимок: {name} ({width}x{height})", flush=True)
        return False

    def _seq(self, items):
        t = 0
        for delay, fn in items:
            t += delay
            GLib.timeout_add(t, lambda f=fn: (f(), False)[1])

    def _autotest_image(self):
        w, st = self.win, self.store
        virt = next((d for d in st.disks if d.virtual), None)
        ip = w.image_page

        def start():
            w.go_image(virt)
            ip.set_dest(os.path.expanduser("~/.cache/diskplus-test"))
            ip.name_row.set_text("dp-ui-test")
            ip.on_start()

        def wait_done(n=[0]):
            n[0] += 1
            if ip.state == "idle" or n[0] > 60:
                GLib.timeout_add(800, lambda: (self.shot("i4-done"), print("AUTOTEST image state:", ip.state, flush=True),
                                               self.quit(), False)[-1])
                return False
            return True

        self._seq([
            (200, lambda: st.ensure_helper(lambda: print("AUTOTEST helper ready", flush=True), print)),
            (2500, lambda: (w.go_image(virt), ip.set_dest(os.path.expanduser("~/.cache/diskplus-test")), ip.name_row.set_text("dp-ui-test"))),
            (800, lambda: self.shot("i1-form")),
            (300, start),
            (9000, lambda: self.shot("i2-early")),
            (30000, lambda: self.shot("i3-mid")),
            (200, lambda: GLib.timeout_add(2000, wait_done)),
        ])
        return False

    def _autotest_recover(self):
        w = self.win
        rp = w.recover_page
        img = os.path.expanduser("~/.cache/diskplus-test/fx-ntfs.img")
        out = os.path.expanduser("~/.cache/diskplus-test/out-recover")

        def pick(name):
            m = rp.sel.get_model()
            for i in range(m.get_n_items()):
                row = m.get_item(i)
                if row.get_item().node.name == name:
                    rp.sel.set_selected(i)
                    return row.get_item()

        def deleted_view():
            rp.tg_del.set_active(True)
            it = pick("pic2.png")
            if it:
                it.selected = True
                rp._toggle(it, True)
            it2 = pick("big.bin")
            if it2:
                rp._toggle(it2, True)
            pick("pic2.png")

        w.select("recover")
        self._seq([
            (300, lambda: rp.open_image(img)),
            (1800, lambda: self.shot("r1-volumes")),
            (200, lambda: rp.start_scan(rp.vols[0]) if rp.vols else print("AUTOTEST нет томов", flush=True)),
            (2500, lambda: self.shot("r2-browser")),
            (200, deleted_view),
            (2500, lambda: self.shot("r3-deleted-preview")),
            (200, lambda: rp._run_recover(out)),
            (3500, lambda: self.shot("r4-recovered")),
            (200, lambda: (print("AUTOTEST recovered files:", sorted(os.listdir(out)) if os.path.isdir(out) else None, flush=True), self.quit())),
        ])
        return False

    def _autotest_deep(self):
        w = self.win
        rp = w.recover_page
        img = os.path.expanduser("~/.cache/diskplus-test/fx-ntfsfmt.img")
        out = os.path.expanduser("~/.cache/diskplus-test/out-deep")

        def open_photo():
            m = rp.sel.get_model()
            for i in range(m.get_n_items()):
                row = m.get_item(i)
                if row.get_item().node.name == "Photos":
                    row.set_expanded(True)
            m = rp.sel.get_model()
            for i in range(m.get_n_items()):
                it = m.get_item(i).get_item()
                if it.node.name == "pic1.png":
                    rp.sel.set_selected(i)

        w.select("recover")
        self._seq([
            (300, lambda: rp.open_image(img)),
            (1800, lambda: self.shot("d1-volumes")),
            (200, lambda: rp.start_scan(rp.vols[0])),
            (2500, lambda: self.shot("d2-quick-empty")),
            (200, lambda: rp.show_step("volumes")),
            (300, lambda: rp.start_deep(rp.vols[0], use_cache=False)),
            (4500, lambda: self.shot("d3-deep-tree")),
            (200, open_photo),
            (1500, lambda: self.shot("d4-deep-preview")),
            (200, lambda: (rp.selected.update(n.id for n in rp.index.search("")), rp._run_recover(out))),
            (4000, lambda: self.shot("d5-recovered")),
            (200, lambda: (print("AUTOTEST deep recovered:", sorted(os.listdir(out)) if os.path.isdir(out) else None, flush=True), self.quit())),
        ])
        return False

    def _autotest_clone(self):
        w, st = self.win, self.store
        cp = w.clone_page
        src_p, dst_p = os.environ["DISKPLUS_CLONE_SRC"], os.environ["DISKPLUS_CLONE_DST"]

        def setup():
            st.refresh_disks()
            s = st.disk_by_path(src_p)
            d = st.disk_by_path(dst_p)
            print("AUTOTEST disks:", [x.path for x in st.disks], "src", bool(s), "dst", bool(d), flush=True)
            cp.reload_disks()
            cp.set_pair(s, d)
            w.select("clone")

        def drag():
            cp._on_drag(1, 300000)

        def dismiss():
            dlg = w.get_visible_dialog()
            if dlg:
                dlg.force_close()

        self._seq([
            (200, lambda: st.ensure_helper(lambda: print("AUTOTEST helper ready", flush=True), print)),
            (2000, setup),
            (800, lambda: self.shot("c1-pick")),
            (200, cp.to_layout),
            (3500, lambda: self.shot("c2-layout")),
            (200, drag),
            (800, lambda: self.shot("c3-dragged")),
            (200, cp.check_plan),
            (2500, lambda: self.shot("c4-confirm")),
            (200, dismiss),
            (500, lambda: cp.start_clone(os.path.basename(cp.dst.path))),
            (4000, lambda: self.shot("c5-running")),
            (14000, lambda: self.shot("c6-result")),
            (1800, lambda: (print("AUTOTEST clone running:", cp.running, flush=True), self.quit())),
        ])
        return False

    def _autotest_backup(self):
        w, st = self.win, self.store
        bp = w.backup_page
        src_p = os.environ["DISKPLUS_CLONE_SRC"]

        def setup():
            st.refresh_disks()
            bp.reload_disks()
            i = next(i for i, d in enumerate(bp.disks) if d.path == src_p)
            bp.c_src_row.set_selected(i)
            bp.c_name.set_text("autotest-copy")
            bp.show_step("create")

        self._seq([
            (200, lambda: st.ensure_helper(lambda: print("AUTOTEST helper ready", flush=True), print)),
            (800, lambda: w.select("backup")),
            (1500, lambda: self.shot("b1-home")),
            (200, setup),
            (1200, lambda: self.shot("b2-create")),
            (200, bp.start_backup),
            (9000, lambda: self.shot("b3-done")),
            (200, lambda: bp.show_step("home")),
            (1500, lambda: self.shot("b4-list")),
            (200, lambda: bp.start_restore_flow(*bp_list_first(bp))),
            (1500, lambda: self.shot("b5-restore")),
            (1800, lambda: (print("AUTOTEST backup running:", bp.running, flush=True), self.quit())),
        ])
        return False

    def _autotest_disks(self):
        w, st = self.win, self.store
        w.select("disks")
        self._seq([
            (200, lambda: st.ensure_helper(lambda: print("AUTOTEST helper ready", flush=True), print)),
            (4500, lambda: self.shot("disks")),
            (1500, lambda: self.quit()),
        ])
        return False

    def _autotest(self):
        if AUTOTEST == "disks":
            return self._autotest_disks()
        if AUTOTEST == "backup":
            return self._autotest_backup()
        if AUTOTEST == "clone":
            return self._autotest_clone()
        if AUTOTEST == "deep":
            return self._autotest_deep()
        if AUTOTEST == "recover":
            return self._autotest_recover()
        if AUTOTEST == "image":
            return self._autotest_image()
        w, st = self.win, self.store
        virt = next((d for d in st.disks if d.virtual), None)

        def grant():
            st.ensure_helper(lambda: print("AUTOTEST helper ready", flush=True), print)

        def start_scan():
            if virt:
                w.go_scan(virt)
                w.scan_page.block_row.set_selected(2)
                w.scan_page.on_start()

        def open_smart():
            from .ui import smart_dialog
            d = next((x for x in st.disks if x.path == "/dev/nvme1n1"), None)
            w.select("disks")
            if d:
                smart_dialog.show(w, st, d)

        self._seq([
            (200, grant),
            (4000, lambda: self.shot("1-disks")),
            (200, start_scan),
            (4000, lambda: self.shot("2-scan-mid")),
            (7000, lambda: self.shot("3-scan-done")),
            (200, lambda: print("AUTOTEST state:", w.scan_page.state, flush=True)),
            (200, open_smart),
            (1500, lambda: self.shot("4-smart")),
        ])
        return False


def bp_list_first(bp):
    from . import backup as bk
    return bk.find_backups(os.path.expanduser("~/Резервные копии Disk++"))[0]


def main(argv):
    if Adw.get_major_version() < 1 or (Adw.get_major_version() == 1 and Adw.get_minor_version() < 5):
        sys.stderr.write(
            f"Disk++ нужна библиотека libadwaita не старше 1.5, а у вас {Adw.get_major_version()}.{Adw.get_minor_version()}.\n"
            "Обновите систему (например, Ubuntu 24.10+, Debian 13+, Fedora 41+, Arch/CachyOS) и запустите снова.\n")
        return 1
    return App().run(argv)
