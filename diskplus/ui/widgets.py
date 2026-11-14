"""Собственные виджеты: кольцо здоровья, карта сектором, график скорости, полоса разделов."""
from __future__ import annotations

import math
from pathlib import Path

import cairo
import gi
import numpy as np

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

# ----- палитры: (светлая тема, тёмная тема) -----------------------------------------

LEVEL_COLORS = {   # уровень здоровья: -1 нет данных, 0 хорошо, 1 внимание, 2 плохо, 3 критично
    -1: ("#9a9996", "#77767b"),
    0: ("#26a269", "#57e389"),
    1: ("#e5a50a", "#f8e45c"),
    2: ("#e66100", "#ffa348"),
    3: ("#c01c28", "#f66151"),
}

# 0..4 — корзины по времени отклика, 5 — самые медленные, 6 — ошибка чтения
BUCKET_COLORS = (
    ("#2ec27e", "#2ec27e"),
    ("#bfd730", "#bfd730"),
    ("#f5c211", "#f5c211"),
    ("#ff7800", "#ff7800"),
    ("#e01b24", "#ed333b"),
    ("#8b1a6b", "#c061cb"),
    ("#000000", "#ffffff"),
)

# палитра образа: 0 спасено, 2 не обрезано, 3 не добрано, 6 нечитаемо (остальное не используется)
RESCUE_COLORS = (
    ("#2ec27e", "#2ec27e"), ("#2ec27e", "#2ec27e"), ("#f5c211", "#f5c211"), ("#ff7800", "#ff7800"),
    ("#2ec27e", "#2ec27e"), ("#2ec27e", "#2ec27e"), ("#e01b24", "#ff5f4a"),
)

FS_COLORS = {
    "ntfs": "#62a0ea", "exfat": "#f6d32d", "vfat": "#f9f06b", "fat32": "#f9f06b", "ext4": "#57e389",
    "ext3": "#57e389", "ext2": "#57e389", "btrfs": "#33b077", "xfs": "#2ec27e", "apfs": "#c061cb",
    "hfsplus": "#c061cb", "swap": "#ff7800", "crypto_LUKS": "#77767b", "BitLocker": "#77767b",
    "f2fs": "#8ff0a4", "LVM2_member": "#a51d2d", "": "#9a9996",
}


def _hex(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def is_dark() -> bool:
    return Adw.StyleManager.get_default().get_dark()


def level_color(level: int):
    return _hex(LEVEL_COLORS.get(level, LEVEL_COLORS[-1])[1 if is_dark() else 0])


def bucket_color(i: int):
    return _hex(BUCKET_COLORS[i][1 if is_dark() else 0])


class _Themed:
    """Миксин: перерисовка при смене светлой/тёмной темы."""

    def _watch_theme(self):
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.queue_draw())


class HealthRing(Gtk.DrawingArea, _Themed):
    def __init__(self, size: int = 96):
        super().__init__()
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.score: int | None = None
        self.level = -1
        self.set_draw_func(self._draw)
        self._watch_theme()

    def set_health(self, score, level):
        self.score, self.level = score, level
        self.queue_draw()

    def _draw(self, _area, cr, w, h):
        fg = self.get_color()
        r = min(w, h) / 2 - 5
        cx, cy = w / 2, h / 2
        lw = max(6.0, r * 0.17)
        cr.set_line_width(lw)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.13)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        col = level_color(self.level)
        if self.score is not None:
            a0 = -math.pi / 2
            cr.set_source_rgb(*col)
            cr.arc(cx, cy, r, a0, a0 + 2 * math.pi * max(self.score, 3) / 100)
            cr.stroke()
        txt = "—" if self.score is None else f"{self.score}"
        lay = self.create_pango_layout(txt)
        from gi.repository import Pango
        fd = Pango.FontDescription()
        fd.set_family("Sans")
        fd.set_weight(Pango.Weight.BOLD)
        fd.set_absolute_size(r * 0.78 * Pango.SCALE)
        lay.set_font_description(fd)
        tw, th = lay.get_pixel_size()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 1.0 if self.score is not None else 0.5)
        cr.move_to(cx - tw / 2, cy - th / 2 - (r * 0.04 if self.score is not None else 0))
        from gi.repository import PangoCairo
        PangoCairo.show_layout(cr, lay)
        if self.score is not None:
            lay2 = self.create_pango_layout("%")
            fd2 = Pango.FontDescription()
            fd2.set_family("Sans")
            fd2.set_absolute_size(r * 0.30 * Pango.SCALE)
            lay2.set_font_description(fd2)
            tw2, th2 = lay2.get_pixel_size()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.move_to(cx - tw2 / 2, cy + th / 2 - th2 * 0.55)
            PangoCairo.show_layout(cr, lay2)


class PartBar(Gtk.DrawingArea, _Themed):
    """Полоса разделов: пропорционально размеру, цвет по файловой системе."""

    def __init__(self):
        super().__init__()
        self.set_content_height(14)
        self.set_hexpand(True)
        self.segs: list[tuple[float, str]] = []
        self.set_draw_func(self._draw)
        self._watch_theme()

    def set_parts(self, parts, disk_size):
        total = max(disk_size, sum(p.size for p in parts), 1)
        self.segs = [(p.size / total, p.fstype) for p in parts if p.size]
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        fg = self.get_color()
        rad = h / 2
        cr.new_sub_path()
        cr.arc(w - rad, rad, rad, -math.pi / 2, math.pi / 2)
        cr.arc(rad, rad, rad, math.pi / 2, 3 * math.pi / 2)
        cr.close_path()
        cr.clip_preserve()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.10)
        cr.fill()
        x = 0.0
        gap = 2.0
        for frac, fs in self.segs:
            sw = max(frac * w, 4)
            cr.set_source_rgb(*_hex(FS_COLORS.get(fs, FS_COLORS[""])))
            cr.rectangle(x, 0, max(sw - gap, 2), h)
            cr.fill()
            x += sw


class SpeedGraph(Gtk.DrawingArea, _Themed):
    def __init__(self):
        super().__init__()
        self.set_content_height(130)
        self.set_hexpand(True)
        self.samples: list[float] = []
        self.disp_last = 0.0
        self._tick_id = 0
        self._last = 0
        self.set_draw_func(self._draw)
        self._watch_theme()

    def clear(self):
        self.samples = []
        self.disp_last = 0.0
        self.queue_draw()

    def add(self, v: float):
        # лёгкое скользящее среднее убирает «зубцы» от неравномерных сообщений
        if self.samples:
            v = self.samples[-1] * 0.35 + v * 0.65
        self.samples.append(v)
        if len(self.samples) > 1200:
            self.samples = self.samples[::2]
        if len(self.samples) == 1:
            self.disp_last = v
        if not self._tick_id:
            self._last = 0
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _w, clock):
        now = clock.get_frame_time()
        dt = min((now - self._last) / 1e6, 0.1) if self._last else 1 / 60
        self._last = now
        if self.samples:
            tgt = self.samples[-1]
            self.disp_last += (tgt - self.disp_last) * (1 - math.exp(-dt * 9.0))
            self.queue_draw()
            if abs(tgt - self.disp_last) > 0.01 * max(tgt, 1.0):
                return True
        self._tick_id = 0
        return False

    def _draw(self, _a, cr, w, h):
        fg = self.get_color()
        pad_l, pad_b, pad_t = 44.0, 4.0, 6.0
        gw, gh = w - pad_l - 4, h - pad_b - pad_t
        samples = self.samples[:-1] + [self.disp_last] if self.samples else []
        top = max(samples) if samples else 100.0
        step = 10 ** math.floor(math.log10(max(top, 0.1)))
        top = math.ceil(top * 1.08 / step) * step or 100.0
        fmt = "{:.1f}" if top < 10 else "{:.0f}"
        from gi.repository import Pango, PangoCairo
        cr.set_line_width(1)
        for i in range(0, 5):
            y = pad_t + gh * i / 4
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.10)
            cr.move_to(pad_l, y + 0.5)
            cr.line_to(w - 4, y + 0.5)
            cr.stroke()
            lay = self.create_pango_layout(fmt.format(top * (4 - i) / 4))
            fd = Pango.FontDescription()
            fd.set_family("Sans")
            fd.set_absolute_size(10.5 * Pango.SCALE)
            lay.set_font_description(fd)
            tw, th = lay.get_pixel_size()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.move_to(pad_l - tw - 6, y - th / 2)
            PangoCairo.show_layout(cr, lay)
        n = len(samples)
        if n < 2:
            return
        sm = Adw.StyleManager.get_default()
        if hasattr(sm, "get_accent_color_rgba"):      # libadwaita >= 1.6
            acc = sm.get_accent_color_rgba()
        else:
            acc = Gdk.RGBA()
            acc.red, acc.green, acc.blue, acc.alpha = 0.21, 0.52, 0.89, 1.0
        pts = [(pad_l + gw * i / (n - 1), pad_t + gh * (1 - min(v / top, 1))) for i, v in enumerate(samples)]

        def curve():
            """Кривая Безье по методу Catmull-Rom: плавная, без углов."""
            cr.move_to(*pts[0])
            for i in range(len(pts) - 1):
                p0 = pts[max(i - 1, 0)]
                p1, p2 = pts[i], pts[i + 1]
                p3 = pts[min(i + 2, len(pts) - 1)]
                c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
                c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
                cr.curve_to(c1[0], c1[1], c2[0], c2[1], p2[0], p2[1])

        curve()
        cr.line_to(pts[-1][0], pad_t + gh)
        cr.line_to(pts[0][0], pad_t + gh)
        cr.close_path()
        cr.set_source_rgba(acc.red, acc.green, acc.blue, 0.18)
        cr.fill()
        cr.set_line_width(2.0)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(acc.red, acc.green, acc.blue, 1)
        curve()
        cr.stroke()
        cr.arc(pts[-1][0], pts[-1][1], 3.2, 0, 2 * math.pi)     # точка на конце линии
        cr.fill()


class SmoothBar(Gtk.ProgressBar):
    """Полоса прогресса, которая плавно «догоняет» значение (60 кадров/с), а не прыгает при каждом сообщении."""

    def __init__(self):
        super().__init__()
        self._cur = 0.0
        self._target = 0.0
        self._tick_id = 0
        self._last = 0

    def set_fraction(self, f):          # noqa: D401 — переопределяем: ставим цель, а не значение
        f = max(0.0, min(1.0, f))
        if f == 0.0 or f < self._cur - 0.3:      # сброс или резкий откат: без анимации
            self._cur = self._target = f
            Gtk.ProgressBar.set_fraction(self, f)
            return
        self._target = f
        if not self._tick_id:
            self._last = 0
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _w, clock):
        now = clock.get_frame_time()
        dt = (now - self._last) / 1e6 if self._last else 1 / 60
        self._last = now
        k = 1 - math.exp(-dt * 7.0)
        self._cur += (self._target - self._cur) * k
        if abs(self._target - self._cur) < 0.0005:
            self._cur = self._target
        Gtk.ProgressBar.set_fraction(self, self._cur)
        if self._cur == self._target:
            self._tick_id = 0
            return False
        return True


class ScanMap(Gtk.Widget, _Themed):
    """Карта блоков: сетка квадратиков, цвет = время отклика. Новые клетки плавно проявляются, по карте скользит «сканер»."""

    FADE = 0.45         # секунд на проявление клетки

    def __init__(self, palette=None):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_size_request(200, 160)
        self.colors = palette or BUCKET_COLORS
        self.codes = np.zeros(0, dtype=np.uint8)
        self.alpha = np.zeros(0, dtype=np.float32)
        self.head = -1.0
        self.head_target = -1.0
        self._tex = None
        self._key = None
        self._tick_id = 0
        self._last = 0
        self._watch_theme()

    # --- данные
    def reset(self, cells: int):
        self.codes = np.full(cells, 255, dtype=np.uint8)
        self.alpha = np.ones(cells, dtype=np.float32)
        self.head = self.head_target = -1.0
        self._tex = None
        self.queue_draw()

    def _mark_changed(self, idx, was):
        """Клетки, которые только что появились или изменились, проявляем плавно."""
        if not len(idx):
            return
        fresh = was[idx] == 255
        self.alpha[idx] = np.where(fresh, 0.0, 0.35)
        self._start_tick()

    def set_codes(self, codes: bytes):
        """Заменить все клетки целиком (для образа: состояние клетки может улучшаться)."""
        arr = np.frombuffer(codes, dtype=np.uint8)
        if len(arr) != len(self.codes):
            self.codes = arr.copy()
            self.alpha = np.ones(len(arr), dtype=np.float32)
        else:
            was = self.codes.copy()
            idx = np.nonzero(arr != was)[0]
            self.codes[:] = arr
            self._mark_changed(idx, was)
        self._tex = None
        self.queue_draw()

    def update(self, start: int, chars: str):
        if not len(self.codes) or not chars:
            return
        arr = np.frombuffer(chars.encode("ascii"), dtype=np.uint8)
        seg = np.where(arr == ord("."), 255, arr - ord("0")).astype(np.uint8)
        end = min(start + len(seg), len(self.codes))
        cur = self.codes[start:end]
        new = seg[: end - start]
        # хранить худший результат в клетке
        mask = (cur == 255) | ((new != 255) & (new > cur))
        was = self.codes.copy() if mask.any() else self.codes
        idx = np.nonzero(mask)[0] + start
        cur[mask] = new[mask]
        self._mark_changed(idx, was)
        self._tex = None
        self.queue_draw()

    def set_head(self, cell_index):
        """Куда сейчас смотрит «сканер» (индекс клетки; None — скрыть)."""
        if cell_index is None or cell_index < 0:
            self.head_target = -1.0
        else:
            self.head_target = float(cell_index)
            if self.head < 0:
                self.head = self.head_target
        self._start_tick()

    # --- анимация
    def _start_tick(self):
        if not self._tick_id:
            self._last = 0
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _w, clock):
        now = clock.get_frame_time()
        dt = min((now - self._last) / 1e6, 0.1) if self._last else 1 / 60
        self._last = now
        busy = False
        fading = self.alpha < 1.0
        if fading.any():
            self.alpha[fading] = np.minimum(self.alpha[fading] + dt / self.FADE, 1.0)
            busy = True
        if self.head_target >= 0:
            self.head += (self.head_target - self.head) * (1 - math.exp(-dt * 8.0))
            busy = busy or abs(self.head_target - self.head) > 0.02
        elif self.head >= 0:
            self.head = -1.0
            busy = True
        self._tex = None
        self.queue_draw()
        if not busy:
            self._tick_id = 0
            return False
        return True

    def do_measure(self, orientation, for_size):
        return (160, 160, -1, -1)

    def _palette(self):
        dark = is_dark()
        fg = self.get_color()
        pal = np.zeros((256, 4), dtype=np.uint8)
        pal[:] = (int(fg.red * 255), int(fg.green * 255), int(fg.blue * 255), 30)   # «ещё не читали»
        for i in range(7):
            r, g, b = (int(c * 255) for c in _hex(self.colors[i][1 if dark else 0]))
            pal[i] = (r, g, b, 255)
        return pal

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        n = len(self.codes)
        if not n or w < 10 or h < 10:
            return
        cs = int(max(3, min(16, math.sqrt(w * h / n))))
        cols = max(1, w // cs)
        rows = -(-n // cols)
        while rows * cs > h and cs > 2:
            cs -= 1
            cols = max(1, w // cs)
            rows = -(-n // cols)
        key = (cs, cols, rows, is_dark())
        if self._tex is None or self._key != key:
            pal = self._palette()
            colors = pal[self.codes].astype(np.float32)               # (n, 4)
            scanned = self.codes != 255
            colors[scanned, 3] *= self.alpha[scanned]                   # плавное проявление
            # «сканер»: мягкое свечение вокруг текущей клетки
            if self.head >= 0:
                lo, hi = max(int(self.head) - 6, 0), min(int(self.head) + 7, n)
                idx = np.arange(lo, hi)
                wgt = np.exp(-((idx - self.head) ** 2) / 3.0).astype(np.float32)[:, None]
                glow = np.array([255, 255, 255, 255], dtype=np.float32)
                base = colors[lo:hi]
                base[:, 3] = np.maximum(base[:, 3], 60 * wgt[:, 0])
                colors[lo:hi] = base * (1 - 0.65 * wgt) + glow * (0.65 * wgt)
            colors = np.clip(colors, 0, 255).astype(np.uint8)
            padded = np.zeros((cols * rows, 4), dtype=np.uint8)
            padded[:n] = colors
            cell = padded.reshape(rows, 1, cols, 1, 4)
            img = np.zeros((rows, cs, cols, cs, 4), dtype=np.uint8)
            inner = max(cs - 1, 1)
            img[:, :inner, :, :inner, :] = cell
            tail = cols * rows - n
            if tail:
                img[rows - 1, :, cols - tail:, :, :] = 0
            data = np.ascontiguousarray(img.reshape(rows * cs, cols * cs, 4))
            self._tex = Gdk.MemoryTexture.new(cols * cs, rows * cs, Gdk.MemoryFormat.R8G8B8A8,
                                              GLib.Bytes.new(data.tobytes()), cols * cs * 4)
            self._key = key
        r = Graphene.Rect()
        r.init(0, 0, cols * cs, rows * cs)
        snap.append_scaled_texture(self._tex, Gsk.ScalingFilter.NEAREST, r)


CSS = """
.dp-pill { padding: 2px 10px; border-radius: 99px; font-weight: 600; font-size: 0.85em; }
.dp-pill-0  { background: alpha(@success_color, 0.16); color: @success_color; }
.dp-pill-1  { background: alpha(@warning_color, 0.20); color: @warning_color; }
.dp-pill-2  { background: alpha(@warning_color, 0.20); color: @warning_color; }
.dp-pill-3  { background: alpha(@error_color, 0.18); color: @error_color; }
.dp-pill--1 { background: alpha(currentColor, 0.10); color: alpha(currentColor, 0.7); }
.dp-stat    { padding: 10px 14px; border-radius: 12px; background: alpha(currentColor, 0.06); }
.dp-stat-num { font-size: 1.45em; font-weight: 700; }
.dp-card    { padding: 16px; }
.dp-legend-dot { min-width: 12px; min-height: 12px; border-radius: 4px; }
.dp-mono    { font-family: monospace; }
.dp-bar-label { font-size: 0.82em; opacity: 0.7; }
.dp-phase { padding: 3px 12px; border-radius: 99px; background: alpha(currentColor, 0.07); opacity: 0.55; font-size: 0.9em; }
.dp-phase-active { background: alpha(@accent_bg_color, 0.25); color: @accent_color; opacity: 1; font-weight: 700; }
.dp-phase-done { color: @success_color; opacity: 0.9; }
"""


def map_panel(widget, title, description):
    """A real empty state, replaced by the live block map when a job starts."""
    overlay = Gtk.Overlay()
    overlay.set_child(widget)
    empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                    halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    empty.add_css_class("dp-map-placeholder")
    image = Gtk.Image.new_from_icon_name("view-grid-symbolic")
    image.set_pixel_size(32)
    empty.append(image)
    heading = Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER)
    heading.add_css_class("heading")
    empty.append(heading)
    text = Gtk.Label(label=description, wrap=True, justify=Gtk.Justification.CENTER)
    text.add_css_class("caption")
    empty.append(text)
    overlay.add_overlay(empty)
    return overlay, empty


def install_css():
    prov = Gtk.CssProvider()
    prov.load_from_string(CSS + '\n' + Path(__file__).with_name('style.css').read_text())
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
