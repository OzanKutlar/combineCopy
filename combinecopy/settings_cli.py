"""Interactive editor and printer for the persistent settings file."""

import os

from rich import box
from rich.table import Table

from combinecopy.mobile.env import detect_available_editors, editor_display_name
from combinecopy.settings import (
    CHOICES,
    DEFAULTS,
    SETTINGS_PATH,
    delete_settings,
    load_settings,
    parse_bool_word,
    save_settings,
)

_MAX_ATTEMPTS = 5


def _render(key, value):
    kind = DEFAULTS[key][0]
    if kind == 'bool':
        return '[green]on[/green]' if value else '[dim]off[/dim]'
    if kind == 'nullable-bool':
        if value is None:
            return '[cyan]auto[/cyan]'
        return '[green]on[/green]' if value else '[dim]off[/dim]'
    if kind == 'list':
        return ' '.join(value) if value else '[dim](none)[/dim]'
    if kind == 'nullable-str':
        return value if value else '[dim](auto)[/dim]'
    if kind == 'bool-or-str':
        if value is True:
            return '[green]on[/green]'
        if not value:
            return '[dim]off[/dim]'
        return str(value)
    return str(value)


def print_settings_table(console, settings=None):
    if settings is None:
        settings = load_settings(console)
    table = Table(title='combineCopy Settings', box=box.ROUNDED)
    table.add_column('#', style='dim', no_wrap=True)
    table.add_column('Key', style='cyan', no_wrap=True)
    table.add_column('Value', style='magenta')
    table.add_column('Description', style='white')
    for index, key in enumerate(DEFAULTS):
        table.add_row(str(index + 1), key, _render(key, settings.get(key)), DEFAULTS[key][2])
    console.print(table)
    state = 'yes' if os.path.exists(SETTINGS_PATH) else 'no (built-in defaults in use)'
    console.print(f'[dim]File: {SETTINGS_PATH}  |  exists: {state}[/dim]')


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


def _pick(console, keys, token):
    try:
        index = int(token.strip()) - 1
    except ValueError:
        console.print('[red]Please enter a number from the table.[/red]')
        return None
    if not 0 <= index < len(keys):
        console.print('[red]That number is not in the table.[/red]')
        return None
    return keys[index]


def _prompt_value(console, key, current):
    kind = DEFAULTS[key][0]
    if kind == 'bool':
        return not bool(current)
    if kind == 'nullable-bool':
        # auto -> on -> off -> auto
        if current is None:
            return True
        return False if current else None

    for _ in range(_MAX_ATTEMPTS):
        raw = console.input(f'[bold]New value for {key} (Enter to keep): [/bold]').strip()
        if not raw:
            return current
        if kind == 'choice':
            if raw.lower() in CHOICES[key]:
                return raw.lower()
            console.print(f"[red]Pick one of: {', '.join(CHOICES[key])}.[/red]")
            continue
        if kind == 'int':
            try:
                number = int(raw)
            except ValueError:
                console.print('[red]Please enter a whole number.[/red]')
                continue
            if number < 1:
                console.print('[red]The value must be 1 or greater.[/red]')
                continue
            return number
        if kind == 'list':
            return [] if raw.lower() in ('none', 'clear') else raw.split()
        if kind == 'nullable-str':
            if key == 'editor':
                detected = detect_available_editors()
                console.print("\n[bold cyan]Choose an editor or enter a custom command (${file} as file placeholder):[/bold cyan]")
                console.print("  [cyan]0.[/cyan] auto (Default: Notepad++ on Win, micro on POSIX)")
                for i, (ident, label, tmpl) in enumerate(detected, 1):
                    console.print(f"  [cyan]{i}.[/cyan] {label} [dim]({tmpl})[/dim]")
                console.print("  [cyan]C.[/cyan] Custom command (e.g. [green]code --wait ${file}[/green] or [green]micro ${file}[/green])")
                
                sub_ans = console.input("[bold]Editor Choice (0-" + str(len(detected)) + "/C/Enter to keep): [/bold]").strip()
                if not sub_ans:
                    return current
                if sub_ans == '0' or sub_ans.lower() in ('auto', 'none', 'clear'):
                    return None
                if sub_ans.isdigit():
                    idx = int(sub_ans) - 1
                    if 0 <= idx < len(detected):
                        return detected[idx][2]
                    console.print("[red]Invalid selection.[/red]")
                    continue
                if sub_ans.upper() == 'C':
                    custom_cmd = console.input("[bold]Custom command template: [/bold]").strip()
                    return custom_cmd if custom_cmd else current
                return sub_ans
            return None if raw.lower() in ('none', 'auto', 'clear') else raw
        if kind == 'bool-or-str':
            parsed = parse_bool_word(raw)
            return parsed if parsed is not None else raw

    console.print('[yellow]Too many invalid entries; the value was left unchanged.[/yellow]')
    return current


def _default_for(key):
    default = DEFAULTS[key][1]
    return list(default) if isinstance(default, list) else default


def run_settings_editor(console):
    settings = load_settings(console)
    keys = list(DEFAULTS)
    dirty = False

    while True:
        console.print()
        print_settings_table(console, settings)
        console.print("[dim]Enter a number to change a value, 'r <num>' to reset one, 's' to save, 'q' to discard.[/dim]")
        answer = console.input('[bold]Choice: [/bold]').strip().lower()

        if not answer:
            continue

        if answer == 'q':
            if dirty:
                confirm = console.input('[bold yellow]Discard unsaved changes? (y/N): [/bold yellow]').strip().lower()
                if confirm not in ('y', 'yes'):
                    continue
            console.print('[yellow]No changes were written.[/yellow]')
            return

        if answer == 's':
            if save_settings(settings):
                console.print(f'[green]Saved to {SETTINGS_PATH}.[/green]')
            else:
                console.print('[red]Could not write the settings file.[/red]')
            return

        if answer.startswith('r '):
            target = _pick(console, keys, answer[2:])
            if target:
                settings[target] = _default_for(target)
                dirty = True
            continue

        target = _pick(console, keys, answer)
        if not target:
            continue
        settings[target] = _prompt_value(console, target, settings.get(target))
        dirty = True
