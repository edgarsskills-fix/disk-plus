"""Накопители: поиск, SMART и карта разделов в общей сетке карточек."""
from __future__ import annotations
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk, Pango
from .. import devices
from . import smart_dialog
from .widgets import HealthRing, PartBar


class DiskCard(Gtk.Box):
    def __init__(self, page, disk):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.page, self.disk = page, disk
        self.set_size_request(300, -1)
        self.add_css_class('dp-disk-card')
        top = Gtk.Box(spacing=12)
        icon = Gtk.Image.new_from_icon_name('drive-removable-media-symbolic' if disk.removable else 'drive-harddisk-symbolic')
        icon.set_pixel_size(24)
        icon.set_valign(Gtk.Align.START)
        icon.add_css_class('dp-device-icon')
        top.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, hexpand=True)
        self.title = Gtk.Label(label=disk.title, xalign=0, wrap=True, lines=2,
                               ellipsize=Pango.EllipsizeMode.END, max_width_chars=25)
        self.title.add_css_class('dp-device-title')
        self.title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.title.set_tooltip_text(disk.title + (f'\nСерийный номер: {disk.serial}' if disk.serial else ''))
        text.append(self.title)
        self.sub = Gtk.Label(label=f'{disk.kind}  ·  {disk.path}', xalign=0,
                             ellipsize=Pango.EllipsizeMode.END, max_width_chars=30)
        self.sub.add_css_class('caption')
        self.sub.add_css_class('dim-label')
        text.append(self.sub)
        top.append(text)
        self.append(top)
        capacity_line = Gtk.Box(spacing=10)
        capacity = Gtk.Label(label=devices.human_size(disk.size), xalign=0, hexpand=True)
        capacity.add_css_class('dp-capacity')
        capacity_line.append(capacity)
        if disk.is_system:
            system = Gtk.Label(label='Системный', valign=Gtk.Align.CENTER)
            system.add_css_class('dp-pill')
            system.add_css_class('dp-pill--1')
            capacity_line.append(system)
        self.append(capacity_line)
        self.append(Gtk.Separator())
        health = Gtk.Box(spacing=14)
        self.ring = HealthRing(62)
        health.append(self.ring)
        health_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                              hexpand=True, valign=Gtk.Align.CENTER)
        caption = Gtk.Label(label='Состояние SMART', xalign=0)
        caption.add_css_class('caption')
        caption.add_css_class('dim-label')
        health_text.append(caption)
        self.pill = Gtk.Label(label='Нет данных', halign=Gtk.Align.START)
        self.pill.add_css_class('dp-pill')
        self.pill.add_css_class('dp-pill--1')
        health_text.append(self.pill)
        health.append(health_text)
        self.temp = Gtk.Label(xalign=1, wrap=True, max_width_chars=14)
        self.temp.add_css_class('caption')
        self.temp.add_css_class('dim-label')
        health.append(self.temp)
        self.append(health)
        self.reasons = Gtk.Label(xalign=0, wrap=True, visible=False, max_width_chars=40)
        self.reasons.add_css_class('caption')
        self.append(self.reasons)
        partition_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        self.bar = PartBar()
        self.bar.set_content_height(7)
        self.bar.set_parts(disk.parts, disk.size)
        partition_box.append(self.bar)
        desc = '  ·  '.join(f'{p.label or p.fstype or p.name}  {devices.human_size(p.size)}' for p in disk.parts)
        lab = Gtk.Label(label=desc or 'Нет разделов', xalign=0, wrap=True, lines=2,
                         ellipsize=Pango.EllipsizeMode.END, max_width_chars=38)
        lab.add_css_class('dp-bar-label')
        lab.set_tooltip_text(desc or 'На диске не найдены разделы')
        partition_box.append(lab)
        self.append(partition_box)
        self.append(Gtk.Box(vexpand=True))
        actions = Gtk.Box(spacing=8)
        smart = Gtk.Button(label='SMART', hexpand=True, tooltip_text='Атрибуты и состояние накопителя')
        smart.connect('clicked', lambda _: page.open_smart(disk))
        scan = Gtk.Button(label='Проверить', hexpand=True, tooltip_text='Проверка поверхности в режиме чтения')
        scan.add_css_class('suggested-action')
        scan.connect('clicked', lambda _: page.window.go_scan(disk))
        more = Gtk.MenuButton(icon_name='view-more-symbolic', tooltip_text='Другие действия с накопителем')
        popover = Gtk.Popover()
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for title, callback in [
            ('Создать образ…', lambda: page.window.go_image(disk)),
            ('Восстановить файлы…', lambda: (page.window.recover_page.open_disk(disk), page.window.select('recover'))),
            ('Клонировать…', lambda: page.window.go_clone(disk)),
            ('Резервная копия…', lambda: page.window.select('backup')),
        ]:
            item = Gtk.Button(label=title)
            item.add_css_class('flat')
            item.get_child().set_halign(Gtk.Align.START)
            item.connect('clicked', lambda _, cb=callback: (popover.popdown(), cb()))
            menu.append(item)
        popover.set_child(menu)
        more.set_popover(popover)
        for item in [smart, scan, more]:
            actions.append(item)
        self.append(actions)
        self.refresh()

    def refresh(self):
        store = self.page.store
        health = store.health.get(self.disk.path)
        for level in (-1, 0, 1, 2, 3):
            self.pill.remove_css_class(f'dp-pill-{level}')
        for css in ('warning', 'error'):
            self.reasons.remove_css_class(css)
        if health is None:
            self.ring.set_health(None, -1)
            self.pill.add_css_class('dp-pill--1')
            self.pill.set_label('Виртуальный диск' if self.disk.virtual else 'Требуется доступ' if not store.helper_ready else 'Чтение SMART…')
            self.temp.set_label('')
            self.reasons.set_visible(False)
            return
        self.ring.set_health(health.score, health.level)
        self.pill.set_label(health.label)
        self.pill.add_css_class(f'dp-pill-{health.level}')
        info = store.smart.get(self.disk.path)
        text = []
        if info and info.temp is not None:
            text.append(f'{info.temp} °C')
        if info and info.power_on_hours is not None:
            text.append(f'{info.power_on_hours:,} ч'.replace(',', ' '))
        self.temp.set_label('\n'.join(text))
        self.reasons.set_visible(bool(health.reasons))
        if health.reasons:
            self.reasons.set_label('\n'.join(health.reasons[:3]))
            self.reasons.add_css_class('warning' if health.level < 3 else 'error')


class DisksPage(Gtk.Box):
    CARD_MIN = 350

    def __init__(self, window, store):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.store = window, store
        self.cards = {}
        self.query = ''
        self._fit_source = 0
        tools = Gtk.Box(spacing=16, margin_start=30, margin_end=30, margin_bottom=18)
        self.summary = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.summary.add_css_class('caption')
        self.summary.add_css_class('dim-label')
        tools.append(self.summary)
        self.search = Gtk.SearchEntry(placeholder_text='Найти накопитель…')
        self.search.set_size_request(150, -1)
        self.search.set_hexpand(True)
        self.search.connect('search-changed', self._search_changed)
        tools.append(self.search)
        self.append(tools)
        self.banner = Adw.Banner(title='Для чтения SMART нужен доступ к накопителям',
                                 button_label='Разрешить', revealed=not store.helper_ready)
        self.banner.add_css_class('dp-access')
        self.banner.set_overflow(Gtk.Overflow.HIDDEN)
        self.banner.set_margin_start(30)
        self.banner.set_margin_end(30)
        self.banner.set_margin_bottom(18)
        self.banner.connect('button-clicked', lambda *_: self.grant())
        self.append(self.banner)
        self.views = Gtk.Stack(vexpand=True)
        self.sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                column_spacing=16, row_spacing=16, min_children_per_line=1,
                                max_children_per_line=3, valign=Gtk.Align.START,
                                margin_start=30, margin_end=30, margin_bottom=30)
        self.flow.set_filter_func(self._filter)
        self.sc.set_child(self.flow)
        self.views.add_named(self.sc, 'disks')
        self.empty = Adw.StatusPage(icon_name='drive-harddisk-symbolic', title='Накопители не найдены',
                                    description='Подключите накопитель и обновите список.')
        self.views.add_named(self.empty, 'empty')
        self.append(self.views)
        self.sc.get_hadjustment().connect('notify::page-size', lambda *_: self._schedule_columns())
        store.connect(self._on_event)
        self.rebuild()

    def _filter(self, child):
        disk = child.get_child().disk
        return self.query in f'{disk.title} {disk.path} {disk.kind} {disk.serial}'.casefold()

    def _search_changed(self, entry):
        self.query = entry.get_text().strip().casefold()
        self.flow.invalidate_filter()
        self._update_summary()

    def _update_summary(self):
        shown = sum(self.query in f'{d.title} {d.path} {d.kind} {d.serial}'.casefold() for d in self.store.disks)
        count = len(self.store.disks)
        self.summary.set_label(f'Накопителей: {count}  ·  {devices.human_size(sum(d.size for d in self.store.disks))}' if not self.query else f'Найдено: {shown} из {count}')
        self.views.set_visible_child_name('disks' if shown else 'empty')
        self.empty.set_title('Ничего не найдено' if self.query else 'Накопители не найдены')
        self.empty.set_description('Измените поисковый запрос.' if self.query else 'Подключите накопитель и обновите список.')
        self.banner.set_revealed(bool(count) and not self.store.helper_ready)

    def _schedule_columns(self):
        if not self._fit_source:
            self._fit_source = GLib.idle_add(self._fit_columns)

    def _fit_columns(self):
        self._fit_source = 0
        width = self.sc.get_hadjustment().get_page_size() - 60
        cols = max(1, min(3, max(1, len(self.cards)), int((width + 16) // (self.CARD_MIN + 16))))
        if self.flow.get_max_children_per_line() != cols:
            self.flow.set_min_children_per_line(1)
            self.flow.set_max_children_per_line(cols)
        return GLib.SOURCE_REMOVE

    def grant(self):
        self.banner.set_button_label('Ожидание…')
        self.banner.set_sensitive(False)
        def ready():
            self.banner.set_sensitive(True)
            self.banner.set_button_label('Разрешить')
            self.banner.set_title('Для чтения SMART нужен доступ к накопителям')
        def fail(message):
            self.banner.set_sensitive(True)
            self.banner.set_button_label('Повторить')
            self.banner.set_title(message)
        self.store.ensure_helper(ready, fail)

    def rebuild(self):
        while (child := self.flow.get_first_child()) is not None:
            self.flow.remove(child)
        self.cards.clear()
        for disk in self.store.disks:
            card = DiskCard(self, disk)
            card.set_hexpand(True)
            self.cards[disk.path] = card
            self.flow.append(card)
        self._update_summary()

    def open_smart(self, disk):
        if not self.store.helper_ready:
            self.grant()
            return
        smart_dialog.show(self.window, self.store, disk)

    def _on_event(self, event, payload):
        if event == 'disks':
            self.rebuild()
        elif event == 'smart' and payload in self.cards:
            self.cards[payload].refresh()
        elif event == 'helper':
            self.banner.set_revealed(not payload and bool(self.cards))
            for card in self.cards.values():
                card.refresh()
