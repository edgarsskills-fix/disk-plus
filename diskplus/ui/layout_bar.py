"""Полоса раскладки диска: разделы в масштабе; в режиме редактирования правый край раздела тянется мышью."""
from __future__ import annotations

import math

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo  # noqa: E402

from .. import devices  # noqa: E402
from .widgets import FS_COLORS, _hex  # noqa: E402

SECTOR = 512
HANDLE = 7


class Seg:
    def __init__(self, label, fs, start, size, min_size=None, note=""):
        self.label, self.fs, self.start, self.size = label, fs, start, size
        self.min_size = min_size if min_size is not None else size
        self.note = note


class LayoutBar(Gtk.DrawingArea):
    def __init__(self, editable=False, on_resize=None, height=78):
        super().__init__()
        self.editable = editable
        self.on_resize = on_resize
        self.set_content_height(height)
        self.set_hexpand(True)
        self.total = 1
        self.segs: list[Seg] = []
        self.title = ""
        self._rects: list[tuple[float, float]] = []
        self._drag = None
        self.set_draw_func(self._draw)
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.queue_draw())
        if editable:
            self.set_cursor(None)
            drag = Gtk.GestureDrag()
            drag.connect("drag-begin", self._begin)
            drag.connect("drag-update", self._update)
            drag.connect("drag-end", lambda *_: setattr(self, "_drag", None))
            self.add_controller(drag)
            mot = Gtk.EventControllerMotion()
            mot.connect("motion", self._motion)
            self.add_controller(mot)

    def set_data(self, total_sectors: int, segs: list[Seg]):
        self.total = max(total_sectors, 1)
        self.segs = segs
        self.queue_draw()

    # --- интерактив
    def _edge_at(self, x):
        for i, (x0, w) in enumerate(self._rects):
            if abs(x - (x0 + w)) <= HANDLE:
                return i
        return None

    def _motion(self, _c, x, _y):
        self.set_cursor(Gdk.Cursor.new_from_name("col-resize") if self._edge_at(x) is not None else None)

    def _begin(self, _g, x, _y):
        i = self._edge_at(x)
        if i is not None:
            self._drag = (i, self.segs[i].size, x)

    def _update(self, _g, dx, _dy):
        if not self._drag or not self.on_resize:
            return
        i, size0, _x = self._drag
        width = max(self.get_width() - 2, 1)
        delta = int(dx / width * self.total)
        self.on_resize(i, size0 + delta)

    # --- рисование
    def _draw(self, _a, cr, w, h):
        fg = self.get_color()
        bar_h = h - 26
        top = 0
        self._rects = []
        lay_font = Pango.FontDescription()
        lay_font.set_family("Sans")
        total_w = w - 2
        pos = 0.0
        gap = 3
        for seg in self.segs:
            sw = seg.size / self.total * total_w
            x0 = 1 + seg.start / self.total * total_w
            self._rects.append((x0, sw))
            r = 9
            cr.new_sub_path()
            xx, ww = x0, max(sw - gap, 4)
            cr.arc(xx + ww - r, top + r, r, -math.pi / 2, 0)
            cr.arc(xx + ww - r, top + bar_h - r, r, 0, math.pi / 2)
            cr.arc(xx + r, top + bar_h - r, r, math.pi / 2, math.pi)
            cr.arc(xx + r, top + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()
            col = _hex(FS_COLORS.get(seg.fs, FS_COLORS[""]))
            cr.set_source_rgba(*col, 0.88)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.12)
            cr.set_line_width(1)
            cr.stroke()
            # подпись
            text = f"{seg.label}\n{devices.human_size(seg.size * SECTOR)}"
            lay = self.create_pango_layout(text)
            lay_font.set_absolute_size(11.5 * Pango.SCALE)
            lay.set_font_description(lay_font)
            lay.set_width(int((ww - 12) * Pango.SCALE))
            lay.set_ellipsize(Pango.EllipsizeMode.END)
            tw, th = lay.get_pixel_size()
            if ww > 46:
                cr.set_source_rgba(0, 0, 0, 0.78)
                cr.move_to(xx + 8, top + (bar_h - th) / 2)
                PangoCairo.show_layout(cr, lay)
            # ручка
            if self.editable and seg.size < self.total:
                cr.set_source_rgba(0, 0, 0, 0.35)
                hx = xx + ww - 5
                for k in (-6, 0, 6):
                    cr.arc(hx, top + bar_h / 2 + k, 1.6, 0, 2 * math.pi)
                    cr.fill()
            pos = max(pos, x0 + sw)
        # свободное место
        if self.segs and pos < w - 6:
            free = self.total - max(s.start + s.size for s in self.segs)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
            cr.set_dash([5, 4])
            cr.rectangle(pos + 1, top + 1, w - pos - 3, bar_h - 2)
            cr.stroke()
            cr.set_dash([])
            if w - pos > 70:
                lay = self.create_pango_layout("Свободно\n" + devices.human_size(free * SECTOR))
                lay_font.set_absolute_size(11 * Pango.SCALE)
                lay.set_font_description(lay_font)
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
                tw, th = lay.get_pixel_size()
                cr.move_to(pos + 10, top + (bar_h - th) / 2)
                PangoCairo.show_layout(cr, lay)
        # заголовок под полосой
        if self.title:
            lay = self.create_pango_layout(self.title)
            lay_font.set_absolute_size(11.5 * Pango.SCALE)
            lay.set_font_description(lay_font)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.65)
            cr.move_to(2, bar_h + 6)
            PangoCairo.show_layout(cr, lay)
