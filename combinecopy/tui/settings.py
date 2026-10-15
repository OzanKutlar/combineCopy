"""Full-screen settings editor.

Categories sit on the left and the selected category's settings on the right,
with one widget per setting type. Every typed value goes through
settings.parse_setting_text, so this screen accepts and rejects exactly what
the CLI editor and --set do.
"""

from rich.console import Group
from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Select, Static, Switch

from combinecopy.mobile.env import detect_available_editors, is_narrow_screen
from combinecopy.settings import (
    CHOICES,
    DEFAULTS,
    SETTINGS_PATH,
    category_keys,
    category_names,
    category_of,
    default_for,
    format_setting_value,
    is_changed,
    load_settings,
    ordered_keys,
    parse_setting_text,
    save_settings,
)
from combinecopy.settings_cli import IDENTITY_PRESETS

_ALL_TITLE = 'All settings'
_PRESET_SUFFIX = '::preset'
_TRISTATE = [('auto', 'auto'), ('on', 'on'), ('off', 'off')]
_MAX_FOCUS_DEPTH = 4
_KIND_HINTS = {
    'bool': 'Switch on or off',
    'nullable-bool': 'auto, on or off',
    'int': 'A whole number, 1 or greater',
    'list': 'Space separated, empty for none',
    'nullable-str': 'Empty or auto to detect',
    'bool-or-str': 'on, off, or a path to a custom prompt',
    'str': 'Any text',
}


def _kind_hint(key):
    kind = DEFAULTS[key][0]
    if kind == 'choice':
        return 'Options: ' + ', '.join(CHOICES[key])
    return _KIND_HINTS.get(kind, '')


def _placeholder(key):
    if key == 'editor':
        return 'auto, or a command using ${file} for the path'
    return _kind_hint(key)


def _preset_options(key):
    """Returns (label, value) pairs for the picker beside a text field, or []."""
    if key == 'identity':
        return [(f'{name}  {label}', name) for name, label in IDENTITY_PRESETS]
    if key == 'system':
        return [('off  never inject', 'off'), ('on  built-in system prompt', 'on')]
    if key != 'editor':
        return []
    options = [('auto  detect an editor', 'auto')]
    try:
        detected = detect_available_editors()
    except Exception:
        detected = []
    seen = {'auto'}
    for _, label, command in detected:
        if command not in seen:
            seen.add(command)
            options.append((label, command))
    return options


def _build_field(key, value):
    """Returns the widgets that edit one setting."""
    kind = DEFAULTS[key][0]
    if kind == 'bool':
        return [Switch(value=bool(value), name=key)]
    if kind == 'nullable-bool':
        return [Select(_TRISTATE, value=format_setting_value(key, value), allow_blank=False, name=key)]
    if kind == 'choice':
        options = [(choice, choice) for choice in CHOICES[key]]
        current = value if value in CHOICES[key] else DEFAULTS[key][1]
        return [Select(options, value=current, allow_blank=False, name=key)]
    widgets = []
    presets = _preset_options(key)
    if presets:
        widgets.append(Select(presets, prompt='Presets', name=key + _PRESET_SUFFIX))
    widgets.append(Input(value=format_setting_value(key, value), placeholder=_placeholder(key), name=key))
    return widgets


def _build_row(key, value):
    """Returns (row, mark label) for one setting."""
    mark = Label('', classes='setting-mark')
    title = Label(key, classes='setting-key')
    field = Vertical(*_build_field(key, value), classes='setting-field')
    return Horizontal(mark, title, field, classes='setting-row'), mark


class SettingsForm(VerticalScroll):
    """The right-hand form. Tells the app which setting has focus."""

    def on_descendant_focus(self, event) -> None:
        self.app.describe_focused()


class SettingsApp(App):
    """Exits with 'saved' after a successful save, or None."""

    CSS = '''
    Screen { background: #2d2825; }
    Header { background: #d08c60; color: #2d2825; }
    Footer { background: #3c3431; }
    #layout { height: 1fr; }
    #sidebar {
        width: 30%;
        border-right: solid #5a4d45;
        background: #241f1c;
        padding: 0 1;
    }
    #search { margin: 1 0; border: solid #5a4d45; background: #1e1a18; }
    #search:focus { border: double #d08c60; }
    #categories { height: 1fr; background: #241f1c; }
    #main { width: 70%; padding: 0 2; }
    #page-title {
        width: 100%;
        margin-top: 1;
        padding: 0 1;
        background: #4a3f39;
        color: #d08c60;
        text-style: bold;
    }
    #form { height: 1fr; margin-top: 1; }
    .setting-row { height: auto; margin-bottom: 1; }
    .setting-mark { width: 2; padding-top: 1; }
    .setting-key { width: 24; padding-top: 1; color: #ead6c9; text-style: bold; }
    .setting-field { width: 1fr; height: auto; }
    .setting-field Input { border: solid #5a4d45; background: #1e1a18; }
    .setting-field Input:focus { border: double #d08c60; }
    .setting-field Input.invalid { border: solid #ff5555; }
    #description {
        height: auto;
        min-height: 3;
        padding: 0 1;
        border-top: solid #5a4d45;
    }
    #status { width: 100%; padding: 0 1; background: #3c3431; }
    Screen.narrow #layout { layout: vertical; }
    Screen.narrow #sidebar {
        width: 100%;
        height: 35%;
        border-right: none;
        border-bottom: solid #5a4d45;
    }
    Screen.narrow #main { width: 100%; height: 65%; padding: 0 1; }
    Screen.narrow .setting-key { width: 18; }
    '''

    BINDINGS = [
        Binding('ctrl+s', 'save', 'Save'),
        Binding('escape', 'quit_editor', 'Quit'),
        Binding('ctrl+f', 'focus_search', 'Search'),
        Binding('slash', 'focus_search', 'Search', show=False),
        Binding('ctrl+r', 'reset_field', 'Reset Field'),
        Binding('ctrl+t', 'reset_page', 'Reset Page'),
    ]
    TITLE = 'combineCopy — Settings'

    def __init__(self, settings, start_category=None):
        super().__init__()
        self.saved = dict(settings)
        self.settings = dict(settings)
        self.start_category = start_category if start_category in category_names() else None
        self._errors = {}
        self._marks = {}
        self._category_labels = {}
        self._page_title = ''
        self._page_keys = []
        self._quit_armed = False

    # --- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id='layout'):
            with Vertical(id='sidebar'):
                yield Input(placeholder='Search settings (/)', id='search')
                items = []
                for position, name in enumerate(category_names()):
                    label = Label(Text(self._category_text(name)))
                    self._category_labels[name] = label
                    items.append(ListItem(label, id=f'cat-{position}'))
                items.append(ListItem(Label(_ALL_TITLE), id='cat-all'))
                yield ListView(*items, id='categories')
            with Vertical(id='main'):
                yield Label('', id='page-title')
                yield SettingsForm(id='form')
                yield Static('', id='description')
        yield Label('', id='status')
        yield Footer()

    def on_mount(self) -> None:
        if is_narrow_screen():
            self.screen.add_class('narrow')
        names = category_names()
        start = names.index(self.start_category) if self.start_category else 0
        categories = self.query_one('#categories', ListView)
        categories.index = start
        categories.focus()
        self._show_highlighted()
        self._refresh_status()

    # --- pages ------------------------------------------------------------

    def _category_text(self, name):
        keys = category_keys(name)
        changed = sum(1 for key in keys if is_changed(key, self.settings.get(key)))
        return f'{name}  ● {changed}' if changed else name

    def _show_highlighted(self) -> None:
        index = self.query_one('#categories', ListView).index
        names = category_names()
        if index is not None and 0 <= index < len(names):
            self._show_keys(names[index], category_keys(names[index]))
        else:
            self._show_keys(_ALL_TITLE, ordered_keys())

    def _show_keys(self, title, keys) -> None:
        keys = list(keys)
        if title == self._page_title and keys == self._page_keys:
            return
        self._discard_errors()
        form = self.query_one('#form', SettingsForm)
        form.remove_children()
        self._marks = {}
        rows = []
        for key in keys:
            row, mark = _build_row(key, self.settings.get(key))
            self._marks[key] = mark
            rows.append(row)
        if not rows:
            rows.append(Label('No setting matches the search.'))
        form.mount(*rows)
        self._page_title = title
        self._page_keys = keys
        self.query_one('#page-title', Label).update(Text(title))
        for key in keys:
            self._refresh_mark(key)
        self._describe(keys[0] if keys else None)

    def _discard_errors(self) -> None:
        if not self._errors:
            return
        dropped = ', '.join(sorted(self._errors))
        self._errors = {}
        self.notify(f'Discarded invalid text for: {dropped}. The last valid values are kept.', severity='warning')
        self._refresh_status()

    def _apply_search(self, term) -> None:
        needle = (term or '').strip().lower()
        if not needle:
            self._show_highlighted()
            return
        keys = [
            key for key in ordered_keys()
            if needle in key.lower() or needle in DEFAULTS[key][2].lower()
        ]
        self._show_keys(f'Search: {term.strip()}', keys)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None:
            return
        search = self.query_one('#search', Input)
        if search.value:
            # Clearing the search posts Input.Changed, which shows this category.
            search.value = ''
            return
        self._show_highlighted()

    # --- editing ----------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == 'search':
            self._apply_search(event.value)
            return
        key = event.input.name
        if key not in DEFAULTS:
            return
        value, error = parse_setting_text(key, event.value)
        if error is None:
            self._errors.pop(key, None)
            event.input.remove_class('invalid')
            self._store(key, value)
        else:
            self._errors[key] = error
            event.input.add_class('invalid')
        self._refresh_status()
        self._describe(key)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        key = event.switch.name
        if key in DEFAULTS:
            self._store(key, bool(event.value))

    def on_select_changed(self, event: Select.Changed) -> None:
        name = event.select.name or ''
        if not isinstance(event.value, str):
            return
        if name.endswith(_PRESET_SUFFIX):
            self._fill_from_preset(name[:-len(_PRESET_SUFFIX)], event.value, event.select)
            return
        if name not in DEFAULTS:
            return
        value, error = parse_setting_text(name, event.value)
        if error is None:
            self._store(name, value)

    def _fill_from_preset(self, key, value, select) -> None:
        field = select.parent
        if field is None:
            return
        for widget in field.query(Input):
            if widget.name == key:
                # Input.Changed then validates and stores the value.
                widget.value = value
                widget.focus()
                return

    def _store(self, key, value) -> None:
        if self.settings.get(key) == value:
            return
        self.settings[key] = value
        self._quit_armed = False
        self._refresh_mark(key)
        name = category_of(key)
        label = self._category_labels.get(name)
        if label is not None:
            label.update(Text(self._category_text(name)))
        self._refresh_status()

    def _sync_widget(self, key) -> None:
        value = self.settings.get(key)
        form = self.query_one('#form', SettingsForm)
        for widget in form.query('Input, Switch, Select'):
            if widget.name != key:
                continue
            if isinstance(widget, Switch):
                widget.value = bool(value)
            elif isinstance(widget, Select):
                widget.value = format_setting_value(key, value)
            elif isinstance(widget, Input):
                widget.value = format_setting_value(key, value)
                widget.remove_class('invalid')

    def _reset_keys(self, keys) -> None:
        for key in keys:
            self._errors.pop(key, None)
            self._store(key, default_for(key))
            self._sync_widget(key)
        self._refresh_status()

    # --- display ----------------------------------------------------------

    def _refresh_mark(self, key) -> None:
        mark = self._marks.get(key)
        if mark is None:
            return
        changed = is_changed(key, self.settings.get(key))
        mark.update(Text('●', style='bold yellow') if changed else '')

    def _refresh_status(self) -> None:
        unsaved = sum(1 for key in DEFAULTS if self.settings.get(key) != self.saved.get(key))
        parts = [f'{unsaved} unsaved change(s)' if unsaved else 'No unsaved changes']
        if self._errors:
            parts.append(f'{len(self._errors)} invalid value(s)')
        parts.append(SETTINGS_PATH)
        style = 'bold red' if self._errors else ('yellow' if unsaved else 'dim')
        self.query_one('#status', Label).update(Text('   |   '.join(parts), style=style))

    def _describe(self, key) -> None:
        panel = self.query_one('#description', Static)
        if key not in DEFAULTS:
            panel.update('')
            return
        default_text = format_setting_value(key, default_for(key)) or '(none)'
        lines = [
            Text.assemble((key, 'bold cyan'), '  ', DEFAULTS[key][2]),
            Text(f'Default: {default_text}   {_kind_hint(key)}'.rstrip(), style='dim'),
        ]
        error = self._errors.get(key)
        if error:
            lines.append(Text(error, style='bold red'))
        panel.update(Group(*lines))

    def _focused_key(self):
        node = self.focused
        for _ in range(_MAX_FOCUS_DEPTH):
            if node is None:
                return None
            name = getattr(node, 'name', None) or ''
            if name.endswith(_PRESET_SUFFIX):
                name = name[:-len(_PRESET_SUFFIX)]
            if name in DEFAULTS:
                return name
            node = node.parent
        return None

    def describe_focused(self) -> None:
        key = self._focused_key()
        if key is not None:
            self._describe(key)

    # --- actions ----------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one('#search', Input).focus()

    def action_reset_field(self) -> None:
        key = self._focused_key()
        if key is None:
            self.notify('Focus a setting first, then press Ctrl+R.', severity='warning')
            return
        self._reset_keys([key])
        self.notify(f'{key} reset to its built-in default.')

    def action_reset_page(self) -> None:
        if not self._page_keys:
            return
        self._reset_keys(self._page_keys)
        self.notify(f'Reset {len(self._page_keys)} setting(s) on this page to their defaults.')

    def action_save(self) -> None:
        if self._errors:
            keys = ', '.join(sorted(self._errors))
            self.notify(f'Fix the invalid value(s) first: {keys}', severity='error')
            return
        if not save_settings(self.settings):
            self.notify('Could not write the settings file.', severity='error')
            return
        self.saved = dict(self.settings)
        self.exit('saved')

    def action_quit_editor(self) -> None:
        unsaved = any(self.settings.get(key) != self.saved.get(key) for key in DEFAULTS)
        if unsaved and not self._quit_armed:
            self._quit_armed = True
            self.notify('You have unsaved changes. Press Esc again to discard them, or Ctrl+S to save.', severity='warning')
            return
        self.exit(None)


def run_settings_tui(console, category=None):
    """Runs the settings TUI. Returns False when it could not start, so the caller can fall back."""
    try:
        result = SettingsApp(load_settings(console), start_category=category).run()
    except Exception as error:
        console.print(f'[yellow]The settings TUI could not start ({escape(str(error))}); using the CLI editor instead.[/yellow]')
        return False
    if result == 'saved':
        console.print(f'[green]Saved to {escape(SETTINGS_PATH)}.[/green]')
    else:
        console.print('[yellow]No changes were written.[/yellow]')
    return True
