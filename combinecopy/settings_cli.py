"""Interactive editors and printers for the persistent settings file.

The CLI editor opens on a short category menu, and every category is its own
small page, so no screen ever shows all of the settings at once.
"""

import os

from rich import box
from rich.markup import escape
from rich.table import Table

from combinecopy.mobile.env import detect_available_editors
from combinecopy.settings import (
    CHOICES,
    DEFAULTS,
    SETTINGS_PATH,
    category_keys,
    category_names,
    default_for,
    delete_settings,
    format_setting_value,
    is_changed,
    load_settings,
    ordered_keys,
    parse_setting_text,
    resolve_category,
    save_settings,
)

_MAX_ATTEMPTS = 5
_MAX_COMMANDS = 1000
_CHANGED_MARK = '[bold yellow]●[/bold yellow]'

IDENTITY_PRESETS = (
    ('antigravity', 'Antigravity (Google DeepMind default)'),
    ('claude', 'Claude (Anthropic)'),
    ('gemini', 'Gemini (Google)'),
    ('chatgpt', 'ChatGPT (OpenAI)'),
    ('neutral', 'Neutral (generic AI coding assistant, no vendor)'),
    ('dynamic', 'Dynamic placeholder ({{IDENTITY}} for external apps)'),
)


# --- rendering --------------------------------------------------------------

def _render(key, value):
    kind = DEFAULTS[key][0]
    if kind == 'bool':
        return '[green]on[/green]' if value else '[dim]off[/dim]'
    if kind == 'nullable-bool':
        if value is None:
            return '[cyan]auto[/cyan]'
        return '[green]on[/green]' if value else '[dim]off[/dim]'
    if kind == 'list':
        return escape(' '.join(value)) if value else '[dim](none)[/dim]'
    if kind == 'nullable-str':
        return escape(value) if value else '[dim](auto)[/dim]'
    if kind == 'bool-or-str':
        if value is True:
            return '[green]on[/green]'
        if not value:
            return '[dim]off[/dim]'
        return escape(str(value))
    return escape(str(value))


def _settings_table(title, keys, settings, numbered=True):
    table = Table(title=title, box=box.ROUNDED, title_justify='left')
    if numbered:
        table.add_column('#', style='dim', no_wrap=True)
    table.add_column('Key', style='cyan', no_wrap=True)
    table.add_column('Value', style='magenta')
    table.add_column('Description', style='white')
    for position, key in enumerate(keys, start=1):
        value = settings.get(key)
        mark = _CHANGED_MARK + ' ' if is_changed(key, value) else '  '
        row = [key, mark + _render(key, value), DEFAULTS[key][2]]
        if numbered:
            row.insert(0, str(position))
        table.add_row(*row)
    return table


def _print_file_state(console):
    state = 'yes' if os.path.exists(SETTINGS_PATH) else 'no (built-in defaults in use)'
    console.print(
        f'[dim]File: {escape(SETTINGS_PATH)}  |  exists: {state}  |  '
        f'{_CHANGED_MARK} differs from the built-in default[/dim]'
    )


def print_settings_table(console, settings=None, category=None):
    """Prints one table per category, or only the named one."""
    if settings is None:
        settings = load_settings(console)
    names = [category] if category else category_names()
    for name in names:
        console.print(_settings_table(escape(name), category_keys(name), settings, numbered=False))
    _print_file_state(console)


def reset_settings(console):
    if not os.path.exists(SETTINGS_PATH):
        console.print('[yellow]There is no settings file to reset.[/yellow]')
        return
    answer = console.input(f'[bold yellow]Delete {SETTINGS_PATH}? (y/N): [/bold yellow]').strip().lower()
    if answer not in ('y', 'yes'):
        console.print('[yellow]Left the settings file untouched.[/yellow]')
        return
    if delete_settings():
        console.print('[green]Settings reset to built-in defaults.[/green]')
    else:
        console.print('[red]Could not delete the settings file.[/red]')


# --- value prompts ----------------------------------------------------------

def _warn_missing_placeholder(console, command):
    if '${file}' not in command and '$file' not in command:
        console.print('[dim]No ${file} placeholder, so the path will be appended to the end.[/dim]')


def _prompt_editor(console, current):
    """Editor picker. Drawn once, then loops on the choice only."""
    try:
        presets = detect_available_editors()
    except Exception:
        presets = []

    shown = current if current else 'auto'
    console.print()
    console.print('[bold cyan]Choose an editor, or enter a command using ${file} as the path placeholder.[/bold cyan]')
    console.print(f'[dim]Current: {escape(shown)}[/dim]')
    console.print('  [cyan]0.[/cyan] auto [dim](Notepad++ then Notepad on Windows; micro, nano then vi elsewhere)[/dim]')
    for position, preset in enumerate(presets, start=1):
        console.print(f'  [cyan]{position}.[/cyan] {escape(preset[1])} [dim]{escape(preset[2])}[/dim]')
    console.print('  [cyan]c.[/cyan] Custom command [dim](e.g. code --wait ${file})[/dim]')

    for _ in range(_MAX_ATTEMPTS):
        answer = console.input('[bold]Editor (Enter to keep): [/bold]').strip()
        if not answer:
            return current
        lowered = answer.lower()
        if lowered in ('0', 'auto', 'none', 'clear'):
            return None
        if lowered == 'c':
            custom = console.input('[bold]Command template: [/bold]').strip()
            if not custom:
                return current
            _warn_missing_placeholder(console, custom)
            return custom
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(presets):
                return presets[index][2]
            console.print('[red]That number is not in the list.[/red]')
            continue
        # Anything else is a command typed straight in, which is the natural
        # thing to do at this prompt.
        _warn_missing_placeholder(console, answer)
        return answer

    console.print('[yellow]Too many invalid entries; the editor was left unchanged.[/yellow]')
    return current


def _prompt_identity(console, current):
    """Interactive picker for the system prompt identity."""
    shown = current if current else 'antigravity'
    console.print()
    console.print('[bold cyan]Choose an identity preset, or enter a custom name, sentence or placeholder.[/bold cyan]')
    console.print(f'[dim]Current: {escape(shown)}[/dim]')
    for position, (key, label) in enumerate(IDENTITY_PRESETS, start=1):
        console.print(f'  [cyan]{position}.[/cyan] {key:<12} [dim]{label}[/dim]')
    console.print('  [cyan]c.[/cyan] Custom identity or placeholder [dim](e.g. DeepSeek, {{MY_AGENT}}, or a full sentence)[/dim]')

    for _ in range(_MAX_ATTEMPTS):
        answer = console.input('[bold]Identity (Enter to keep): [/bold]').strip()
        if not answer:
            return current
        if answer.lower() == 'c':
            custom = console.input('[bold]Custom identity string: [/bold]').strip()
            return custom if custom else current
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(IDENTITY_PRESETS):
                return IDENTITY_PRESETS[index][0]
            console.print('[red]That number is not in the list.[/red]')
            continue
        return answer

    console.print('[yellow]Too many invalid entries; the identity was left unchanged.[/yellow]')
    return current


def _value_hint(key):
    kind = DEFAULTS[key][0]
    if kind == 'choice':
        options = '/'.join(CHOICES[key])
        return f' ({options})'
    if kind == 'int':
        return ' (whole number)'
    if kind == 'list':
        return ' (space separated, or none)'
    if kind == 'bool-or-str':
        return ' (on, off, or a path)'
    if kind == 'nullable-str':
        return ' (or auto)'
    return ''


def _prompt_value(console, key, current):
    """Returns the new value for one setting, or current when left unchanged."""
    kind = DEFAULTS[key][0]
    if key == 'editor':
        return _prompt_editor(console, current)
    if key == 'identity':
        return _prompt_identity(console, current)
    if kind == 'bool':
        return not bool(current)
    if kind == 'nullable-bool':
        # auto -> on -> off -> auto
        if current is None:
            return True
        return False if current else None

    hint = _value_hint(key)
    for _ in range(_MAX_ATTEMPTS):
        raw = console.input(f'[bold]New value for {key}{hint} (Enter to keep): [/bold]').strip()
        if not raw:
            return current
        value, error = parse_setting_text(key, raw)
        if error is None:
            return value
        console.print(f'[red]{escape(error)}[/red]')

    console.print('[yellow]Too many invalid entries; the value was left unchanged.[/yellow]')
    return current


# --- categorized editor -----------------------------------------------------

class SettingsEditor:
    """One CLI editing session. Changes stay in a working copy until saved."""

    def __init__(self, console, start_category=None):
        self.console = console
        self.saved = load_settings(console)
        self.settings = dict(self.saved)
        # None is the category menu. Otherwise (kind, title, keys), where kind
        # is 'category', 'search' or 'all'.
        self.page = None
        if start_category:
            self._open_category(start_category)

    def run(self):
        for _ in range(_MAX_COMMANDS):
            self._render_current()
            try:
                answer = self.console.input('[bold]Choice: [/bold]').strip()
            except (KeyboardInterrupt, EOFError):
                self.console.print()
                self.console.print('[yellow]No changes were written.[/yellow]')
                return
            if answer and self._dispatch(answer) == 'exit':
                return
        self.console.print('[yellow]Too many commands; the editor closed without saving.[/yellow]')

    # --- state ------------------------------------------------------------

    def _unsaved_count(self):
        return sum(1 for key in DEFAULTS if self.settings.get(key) != self.saved.get(key))

    def _unsaved_text(self):
        count = self._unsaved_count()
        return f'   [yellow]{count} unsaved change(s)[/yellow]' if count else ''

    def _open_category(self, name):
        self.page = ('category', name, category_keys(name))

    def _category_index(self):
        if self.page is None or self.page[0] != 'category':
            return None
        names = category_names()
        return names.index(self.page[1]) if self.page[1] in names else None

    # --- rendering --------------------------------------------------------

    def _render_current(self):
        if self.page is None:
            self._print_menu()
        else:
            self._print_page()

    def _print_menu(self):
        console = self.console
        console.print()
        console.print(f'[bold blue]combineCopy Settings[/bold blue]{self._unsaved_text()}')
        for position, name in enumerate(category_names(), start=1):
            keys = category_keys(name)
            changed = sum(1 for key in keys if is_changed(key, self.settings.get(key)))
            note = f'   {_CHANGED_MARK} {changed} changed' if changed else ''
            console.print(f'  [cyan]{position}.[/cyan] {escape(name):<18} [dim]{len(keys)} settings[/dim]{note}')
        console.print('[dim]<num> open a category   a all settings   /text search   s save   q quit[/dim]')

    def _print_page(self):
        _, title, keys = self.page
        console = self.console
        console.print()
        console.print(_settings_table(escape(title) + self._unsaved_text(), keys, self.settings))
        nav = '   n/p next/prev page' if self._category_index() is not None else ''
        console.print(f'[dim]<num> change   r <num> reset{nav}   b back   /text search   s save   q quit[/dim]')

    # --- commands ---------------------------------------------------------

    def _dispatch(self, answer):
        lowered = answer.lower()
        if lowered == 's':
            return self._save()
        if lowered == 'q':
            return self._quit()
        if answer.startswith('/'):
            self._search(answer[1:])
        elif self.page is None:
            self._dispatch_menu(lowered)
        else:
            self._dispatch_page(lowered)
        return None

    def _dispatch_menu(self, answer):
        if answer == 'a':
            self.page = ('all', 'All settings', ordered_keys())
            return
        name, error = resolve_category(answer)
        if error:
            self.console.print(f'[red]{escape(error)}[/red]')
            return
        self._open_category(name)

    def _dispatch_page(self, answer):
        if answer == 'b':
            self.page = None
        elif answer in ('n', 'p'):
            self._step(1 if answer == 'n' else -1)
        elif answer == 'r' or answer.startswith('r '):
            self._reset(answer[1:].strip())
        elif answer.isdigit():
            self._edit(answer)
        else:
            self.console.print(f'[yellow]Unknown command: {escape(answer)}. Type a number, or b to go back.[/yellow]')

    def _pick(self, token):
        keys = self.page[2]
        if not token.isdigit():
            self.console.print('[red]Enter a number from the table.[/red]')
            return None
        index = int(token) - 1
        if not 0 <= index < len(keys):
            self.console.print(f'[red]Pick a number from 1 to {len(keys)}.[/red]')
            return None
        return keys[index]

    def _edit(self, token):
        key = self._pick(token)
        if key is None:
            return
        try:
            value = _prompt_value(self.console, key, self.settings.get(key))
        except (KeyboardInterrupt, EOFError):
            self.console.print()
            self.console.print('[yellow]Left unchanged.[/yellow]')
            return
        self.settings[key] = value

    def _reset(self, token):
        if not token:
            self.console.print('[yellow]Usage: r <num>, for example r 2.[/yellow]')
            return
        key = self._pick(token)
        if key is None:
            return
        self.settings[key] = default_for(key)
        self.console.print(f'[green]{key}[/green] reset to {_render(key, self.settings[key])}.')

    def _step(self, step):
        index = self._category_index()
        if index is None:
            self.console.print('[yellow]n and p only page through categories. Press b to go back.[/yellow]')
            return
        names = category_names()
        self._open_category(names[(index + step) % len(names)])

    def _search(self, term):
        needle = term.strip().lower()
        if not needle:
            self.console.print('[yellow]Type what to search for after the slash, for example /rehab.[/yellow]')
            return
        keys = [
            key for key in ordered_keys()
            if needle in key.lower() or needle in DEFAULTS[key][2].lower()
        ]
        if not keys:
            self.console.print(f'[yellow]No setting matches: {escape(needle)}[/yellow]')
            return
        self.page = ('search', f'Search: {needle}', keys)

    def _save(self):
        if save_settings(self.settings):
            self.console.print(f'[green]Saved to {escape(SETTINGS_PATH)}.[/green]')
            return 'exit'
        self.console.print('[red]Could not write the settings file. Nothing was saved.[/red]')
        return None

    def _quit(self):
        if self._unsaved_count():
            try:
                confirm = self.console.input('[bold yellow]Discard unsaved changes? (y/N): [/bold yellow]').strip().lower()
            except (KeyboardInterrupt, EOFError):
                return None
            if confirm not in ('y', 'yes'):
                return None
        self.console.print('[yellow]No changes were written.[/yellow]')
        return 'exit'


def run_settings_editor(console, category=None):
    """Runs the CLI editor, optionally opening on one category."""
    start = None
    if category:
        start, error = resolve_category(category)
        if error:
            console.print(f'[red]{escape(error)}[/red]')
            return
    SettingsEditor(console, start).run()


# --- scriptable commands ----------------------------------------------------

def run_set(console, assignments, settings=None):
    """Saves KEY=VALUE pairs. Either all of them are valid and saved, or none are."""
    current = settings if settings is not None else load_settings(console)
    working = dict(current)
    errors = []
    applied = []
    for assignment in assignments or []:
        key, separator, raw = str(assignment).partition('=')
        key = key.strip()
        if not separator or not key:
            errors.append(f'{assignment} is not in KEY=VALUE form.')
            continue
        value, error = parse_setting_text(key, raw)
        if error:
            errors.append(f'{key}: {error}')
            continue
        working[key] = value
        applied.append(key)
    if errors:
        for error in errors:
            console.print(f'[red]{escape(error)}[/red]')
        console.print('[yellow]Nothing was saved.[/yellow]')
        return False
    if not save_settings(working):
        console.print('[red]Could not write the settings file.[/red]')
        return False
    current.update(working)
    for key in applied:
        console.print(f'[green]{key}[/green] = {_render(key, working[key])}')
    return True


def run_unset(console, keys, settings=None):
    """Resets settings to their built-in defaults and saves once."""
    current = settings if settings is not None else load_settings(console)
    unknown = [key for key in keys or [] if key not in DEFAULTS]
    if unknown:
        listed = ', '.join(unknown)
        console.print(f'[red]Unknown setting(s): {escape(listed)}. Nothing was saved.[/red]')
        return False
    working = dict(current)
    for key in keys or []:
        working[key] = default_for(key)
    if not save_settings(working):
        console.print('[red]Could not write the settings file.[/red]')
        return False
    current.update(working)
    for key in keys or []:
        console.print(f'[green]{key}[/green] reset to {_render(key, working[key])}')
    return True


def run_get(console, key, settings=None):
    """Prints one setting as plain text, so scripts can read it."""
    if key not in DEFAULTS:
        console.print(f'[red]Unknown setting: {escape(str(key))}[/red]')
        return False
    current = settings if settings is not None else load_settings(console)
    print(format_setting_value(key, current.get(key)))
    return True
