"""Главное окно Disk++: навигация и единое оформление инструментов."""
from __future__ import annotations

import json
import os
from pathlib import Path
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk
from .. import APP_ID, APP_NAME, VERSION
from .backup_page import BackupPage
from .clone_page import ClonePage
from .disks_page import DisksPage
from .image_page import ImagePage
from .recover_page import RecoverPage
from .scan_page import ScanPage


class MainWindow(Adw.ApplicationWindow):
    DESCRIPTIONS = {
        'disks': 'Накопители, разделы и состояние SMART',
        'scan': 'Проверка времени отклика и ошибок чтения',
        'image': 'Сохранение содержимого накопителя в файл образа',
        'recover': 'Поиск файлов на диске или в образе',
        'clone': 'Перенос разделов на другой накопитель',
        'backup': 'Создание, проверка и восстановление копий разделов',
    }

    def __init__(self, app, store):
        super().__init__(application=app, title=APP_NAME)
        self.store = store
        self.set_default_size(1240, 840)
        self.set_size_request(400, 520)
        self.add_css_class('dp-window')
        self.overlay = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(236)
        self.split.set_max_sidebar_width(236)
        self.overlay.set_child(self.split)
        self.set_content(self.overlay)

        self.disks_page = DisksPage(self, store)
        self.scan_page = ScanPage(self, store)
        self.image_page = ImagePage(self, store)
        self.recover_page = RecoverPage(self, store)
        self.clone_page = ClonePage(self, store)
        self.backup_page = BackupPage(self, store)
        self.pages = [
            ('disks', 'Накопители', 'drive-harddisk-symbolic', 'ДИАГНОСТИКА', self.disks_page),
            ('scan', 'Проверка поверхности', 'view-grid-symbolic', 'ДИАГНОСТИКА', self.scan_page),
            ('image', 'Образ диска', 'media-floppy-symbolic', 'ВОССТАНОВЛЕНИЕ', self.image_page),
            ('recover', 'Восстановление файлов', 'folder-saved-search-symbolic', 'ВОССТАНОВЛЕНИЕ', self.recover_page),
            ('clone', 'Клонирование', 'edit-copy-symbolic', 'КОПИРОВАНИЕ', self.clone_page),
            ('backup', 'Резервные копии', 'document-save-symbolic', 'КОПИРОВАНИЕ', self.backup_page),
        ]
        for pid, _title, _icon, _section, page in self.pages:
            page.add_css_class('dp-workspace')
            page.add_css_class('dp-' + pid + '-page')

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.add_css_class('dp-sidebar')
        shb = Adw.HeaderBar()
        shb.set_show_end_title_buttons(False)
        shb.set_title_widget(Gtk.Box())
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_toolbar.add_css_class('dp-sidebar')
        sidebar_toolbar.add_top_bar(shb)
        brand = Gtk.Box(spacing=0, margin_start=24, margin_top=14, margin_bottom=24)
        name = Gtk.Label(label='Disk', xalign=0)
        name.add_css_class('dp-brand')
        plus = Gtk.Label(label='++')
        plus.add_css_class('dp-brand-plus')
        brand.append(name)
        brand.append(plus)
        side.append(brand)
        self.side_list = Gtk.ListBox()
        self.side_list.add_css_class('dp-nav')
        self.side_list.set_header_func(self._side_header)
        for pid, title, icon, section, _page in self.pages:
            row = Gtk.ListBoxRow()
            line = Gtk.Box(spacing=12)
            line.append(Gtk.Image.new_from_icon_name(icon))
            text = Gtk.Label(label=title, xalign=0, wrap=True)
            text.set_max_width_chars(21)
            text.set_lines(2)
            line.append(text)
            row.set_child(line)
            row.page_id, row.section = pid, section
            self.side_list.append(row)
        self.side_list.connect('row-selected', self._on_row)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.side_list)
        side.append(scroll)
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_start=24, margin_end=24, margin_bottom=24)
        footer.append(Gtk.Separator())
        self.access_label = Gtk.Label(label='Стандартный доступ', xalign=0, wrap=True)
        self.access_label.add_css_class('caption')
        self.access_label.add_css_class('dim-label')
        footer.append(self.access_label)
        version = Gtk.Label(label=f'Disk++  {VERSION}', xalign=0)
        version.add_css_class('dp-version')
        footer.append(version)
        side.append(footer)
        sidebar_toolbar.set_content(side)
        self.split.set_sidebar(Adw.NavigationPage.new(sidebar_toolbar, APP_NAME))

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=150, vexpand=True, hhomogeneous=False, vhomogeneous=False)
        for pid, _title, _icon, _section, page in self.pages:
            self.stack.add_named(page, pid)
        self.content_title = Adw.WindowTitle(visible=False)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_title_widget(Gtk.Box())
        menu = Gio.Menu()
        menu.append('Обновить накопители', 'win.refresh')
        appearance = Gio.Menu()
        for text, value in [('Как в системе', 'system'), ('Светлая', 'light'), ('Тёмная', 'dark')]:
            item = Gio.MenuItem.new(text, None)
            item.set_action_and_target_value('win.theme', GLib.Variant('s', value))
            appearance.append_item(item)
        menu.append_submenu('Оформление', appearance)
        menu.append('О Disk++', 'win.about')
        header.pack_end(Gtk.MenuButton(icon_name='open-menu-symbolic', menu_model=menu,
                                       tooltip_text='Меню приложения'))
        toolbar.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        heading = Gtk.Box(spacing=12)
        heading.add_css_class('dp-page-heading')
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.page_title = Gtk.Label(xalign=0, wrap=True)
        self.page_title.add_css_class('dp-page-title')
        self.page_subtitle = Gtk.Label(xalign=0, wrap=True)
        self.page_subtitle.add_css_class('dim-label')
        titles.append(self.page_title)
        titles.append(self.page_subtitle)
        heading.append(titles)
        self.refresh_button = Gtk.Button(icon_name='view-refresh-symbolic', valign=Gtk.Align.CENTER,
                                          tooltip_text='Обновить накопители', action_name='win.refresh')
        self.refresh_button.add_css_class('flat')
        heading.append(self.refresh_button)
        content.append(heading)
        content.append(self.stack)
        toolbar.set_content(content)
        self.content_page = Adw.NavigationPage.new(toolbar, 'Накопители')
        self.split.set_content(self.content_page)

        refresh = Gio.SimpleAction.new('refresh', None)
        refresh.connect('activate', lambda *_: store.refresh_disks())
        self.add_action(refresh)
        about = Gio.SimpleAction.new('about', None)
        about.connect('activate', lambda *_: self.show_about())
        self.add_action(about)
        page_action = Gio.SimpleAction.new('page', GLib.VariantType.new('s'))
        page_action.connect('activate', lambda _a, value: self.select(value.get_string()))
        self.add_action(page_action)
        app.set_accels_for_action('win.refresh', ['<Control>r'])
        for index, (pid, *_rest) in enumerate(self.pages, 1):
            app.set_accels_for_action('win.page::' + pid, ['<Alt>' + str(index)])
        self.appearance_path = Path(GLib.get_user_config_dir()) / 'disk-plus/appearance.json'
        try:
            scheme = json.loads(self.appearance_path.read_text()).get('theme', 'system')
        except (OSError, ValueError):
            scheme = 'system'
        if scheme not in ('system', 'dark', 'light'):
            scheme = 'system'
        theme = Gio.SimpleAction.new_stateful('theme', GLib.VariantType.new('s'), GLib.Variant('s', scheme))
        theme.connect('activate', self._theme_changed)
        self.add_action(theme)
        if not os.environ.get('DISKPLUS_DARK'):
            self._apply_theme(scheme)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 1000sp'))
        breakpoint.add_setter(self.split, 'collapsed', True)
        breakpoint.connect('apply', lambda *_: self._narrow(True))
        breakpoint.connect('unapply', lambda *_: self._narrow(False))
        self.add_breakpoint(breakpoint)
        store.connect(self._on_store)
        self.select('disks')

    def _side_header(self, row, before):
        if before is None or before.section != row.section:
            text = Gtk.Label(label=row.section, xalign=0)
            text.add_css_class('dp-section')
            row.set_header(text)
        else:
            row.set_header(None)

    def _on_row(self, _list, row):
        if row is not None:
            self.select(row.page_id, from_list=True)

    def select(self, pid, from_list=False):
        if not self.stack.get_child_by_name(pid):
            return
        self.stack.set_visible_child_name(pid)
        for index, page in enumerate(self.pages):
            if page[0] == pid:
                self.content_title.set_title(page[1])
                self.content_page.set_title(page[1])
                self.page_title.set_label(page[1])
                self.page_subtitle.set_label(self.DESCRIPTIONS[pid])
                self.refresh_button.set_visible(pid == 'disks')
                if not from_list:
                    self.side_list.select_row(self.side_list.get_row_at_index(index))
                break
        self.split.set_show_content(True)

    def _narrow(self, narrow):
        self.scan_page.set_narrow(narrow)
        self.image_page.set_narrow(narrow)
        self.recover_page.set_narrow(narrow)

    def go_clone(self, disk):
        self.clone_page.preselect_source(disk)
        self.select('clone')

    def go_recover_image(self, path):
        self.recover_page.open_image(path)
        self.select('recover')

    def go_image(self, disk):
        self.image_page.select_disk(disk)
        self.select('image')

    def go_scan(self, disk):
        self.scan_page.select_disk(disk)
        self.select('scan')

    def toast(self, text, quiet=False):
        if not quiet:
            self.overlay.add_toast(Adw.Toast(title=text, timeout=4))

    def _on_store(self, event, payload):
        if event == 'toast':
            self.toast(payload)
        elif event == 'helper':
            self.access_label.set_label('Доступ к накопителям разрешён' if payload else 'Стандартный доступ')

    def _apply_theme(self, name):
        scheme = {'system': Adw.ColorScheme.DEFAULT, 'dark': Adw.ColorScheme.FORCE_DARK,
                  'light': Adw.ColorScheme.FORCE_LIGHT}[name]
        Adw.StyleManager.get_default().set_color_scheme(scheme)

    def _theme_changed(self, action, value):
        name = value.get_string()
        if name not in ('system', 'dark', 'light'):
            return
        action.set_state(value)
        self._apply_theme(name)
        try:
            self.appearance_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.appearance_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'theme': name}))
            temporary.replace(self.appearance_path)
        except OSError:
            self.toast('Не удалось сохранить оформление')

    def show_about(self):
        dialog = Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_ID,
                                 developer_name='edgarsskills-fix', version=VERSION,
                                 comments='Диагностика, восстановление и копирование накопителей',
                                 website='https://github.com/edgarsskills-fix/disk-plus',
                                 issue_url='https://github.com/edgarsskills-fix/disk-plus/issues',
                                 license_type=Gtk.License.MIT_X11)
        dialog.present(self)
