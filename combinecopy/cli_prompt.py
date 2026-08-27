"""A slash-command request area that runs in a plain terminal.

This is the non-Textual counterpart to SystemPromptApp. It collects the request
line by line, dispatches /commands, and hands off to a real editor for anything
longer than a few lines. It returns the same dict shape the TUI returns, so
nothing downstream has to know which one ran.
"""

import os
import re
import subprocess
import tempfile

from rich.rule import Rule

from combinecopy.mobile.env import editor_display_name, resolve_editor
from combinecopy.utils import (
    console,
    load_default_rules,
    safe_read_file,
    save_default_rule,
)

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False

HISTORY_PATH = os.path.expanduser('~/.configs/combineCopy/prompt_history')

# Returned by a key binding in place of a line of text.
_ACT_EDITOR = '::cc-editor::'
_ACT_RULES = '::cc-rules::'
_ACT_SEND = '::cc-send::'
_KEY_ACTIONS = {_ACT_EDITOR: 'editor', _ACT_RULES: 'rules', _ACT_SEND: 'send'}

_COMMANDS = {
    'notepad': 'editor',
    'np': 'editor',
    'editor': 'editor',
    'rules': 'rules',
    'system': 'system',
    'send': 'send',
    'submit': 'send',
    'files': 'files',
    'show': 'show',
    'clear': 'clear',
    'help': 'help',
    'cancel': 'cancel',
    'quit': 'cancel',
}

_HELP_ROWS = [
    ('/notepad  (F2)', 'Edit the request in your editor'),
    ('/rules    (F3)', 'Browse, edit and save rule sets'),
    ('/system', 'Edit the system prompt for this run'),
    ('/send     (Alt+Enter)', 'Finish and continue'),
    ('/files', 'List the files currently in context'),
    ('/show', 'Reprint the request buffer'),
    ('/clear', 'Empty the request buffer'),
    ('/help', 'Show this list'),
    ('/cancel   (Ctrl+C)', 'Abandon the run'),
]


def _split_lines(text):
    if not text:
        return []
    return text.replace('\r\n', '\n').rstrip('\n').split('\n')


class CliPromptSession:
    """One request-entry session. Owns the buffer and the system prompt text."""

    def __init__(self, root_dir, files, sys_prompt, editor_override=None):
        self.root_dir = root_dir
        self.files = list(files or [])
        self.sys_prompt = sys_prompt or ''
        self.editor_override = editor_override
        self.lines = []
        self.session = self._build_session()

    # --- input plumbing -------------------------------------------------

    def _build_session(self):
        if not PROMPT_TOOLKIT_AVAILABLE:
            return None

        bindings = KeyBindings()

        @bindings.add('f2')
        def _to_editor(event):
            event.app.exit(result=_ACT_EDITOR)

        @bindings.add('f3')
        def _to_rules(event):
            event.app.exit(result=_ACT_RULES)

        @bindings.add('escape', 'enter')
        def _alt_enter(event):
            event.app.exit(result=_ACT_SEND)

        try:
            @bindings.add('c-j')
            def _ctrl_enter(event):
                event.app.exit(result=_ACT_SEND)
        except Exception:
            # Plenty of terminals send no distinct Ctrl+Enter at all.
            pass

        history = None
        try:
            os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
            history = FileHistory(HISTORY_PATH)
        except Exception:
            history = None

        completer = WordCompleter(sorted('/' + name for name in _COMMANDS))
        try:
            return PromptSession(key_bindings=bindings, history=history, completer=completer)
        except Exception:
            return None

    def _read_line(self):
        marker = '> ' if not self.lines else '. '
        if self.session is not None:
            return self.session.prompt(marker)
        return input(marker)

    # --- editor handoff -------------------------------------------------

    def _editor_command(self):
        if self.editor_override:
            return [self.editor_override]
        return resolve_editor()

    def _open_in_editor(self, text, suffix='.txt'):
        command = self._editor_command()
        if not command:
            console.print('[red]No editor found. Set the editor setting, or install micro/nano.[/red]')
            return None

        fd, path = tempfile.mkstemp(prefix='combineCopy_', suffix=suffix, text=True)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(text or '')
            subprocess.run(list(command) + [path], check=False)
            with open(path, 'r', encoding='utf-8') as handle:
                return handle.read()
        except Exception as error:
            console.print(f'[red]Editor failed: {error}[/red]')
            return None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    # --- actions --------------------------------------------------------

    def _action_editor(self):
        edited = self._open_in_editor('\n'.join(self.lines))
        if edited is None:
            return
        self.lines = _split_lines(edited)
        console.print(f'[green]Request updated from {editor_display_name()} ({len(self.lines)} line(s)).[/green]')

    def _action_system(self):
        edited = self._open_in_editor(self.sys_prompt, suffix='.md')
        if edited is None:
            return
        if edited.strip() == self.sys_prompt.strip():
            console.print('[dim]System prompt unchanged.[/dim]')
            return
        self.sys_prompt = edited
        console.print('[green]System prompt updated for this run.[/green]')

    def _action_files(self):
        if not self.files:
            console.print('[yellow]No files are in context.[/yellow]')
            return
        console.print(Rule('[bold blue]Files in Context[/bold blue]'))
        for path in self.files:
            console.print('  ' + os.path.relpath(path, self.root_dir))
        console.print(f'[dim]{len(self.files)} file(s).[/dim]')

    def _action_show(self):
        if not self.lines:
            console.print('[dim](the request is empty)[/dim]')
            return
        console.print(Rule('[bold blue]Current Request[/bold blue]'))
        for number, line in enumerate(self.lines, start=1):
            console.print(f'[dim]{number:>3}[/dim]  {line}')

    def _action_clear(self):
        if not self.lines:
            return
        answer = console.input('[bold yellow]Clear the request buffer? (y/N): [/bold yellow]').strip().lower()
        if answer in ('y', 'yes'):
            self.lines = []
            console.print('[green]Buffer cleared.[/green]')

    def _action_help(self):
        console.print(Rule('[bold blue]Commands[/bold blue]'))
        for name, description in _HELP_ROWS:
            console.print(f'  [cyan]{name:<24}[/cyan]{description}')
        if not PROMPT_TOOLKIT_AVAILABLE or self.session is None:
            console.print('[dim]Function keys are unavailable here; the slash commands do the same jobs.[/dim]')

    # --- rules ----------------------------------------------------------

    def _rule_entries(self):
        entries = [('scratch', 'Create a new rule from scratch', '# Define your custom rules here\n')]
        ccrules_path = os.path.join(self.root_dir, '.ccrules')
        if os.path.exists(ccrules_path):
            entries.append(('ccrules', 'Existing project rules (.ccrules)', safe_read_file(ccrules_path)))
        for rule in load_default_rules(self.root_dir):
            entries.append(('default', rule.get('title', 'Untitled rule'), rule.get('content', '')))
        return entries

    def _action_rules(self):
        entries = self._rule_entries()
        console.print(Rule('[bold blue]Rule Catalog[/bold blue]'))
        for position, entry in enumerate(entries):
            console.print(f'  [cyan]{position + 1}.[/cyan] {entry[1]}')

        choice = console.input('[bold]Select a rule to edit (Enter to go back): [/bold]').strip()
        if not choice:
            return
        try:
            index = int(choice) - 1
        except ValueError:
            console.print('[red]Please enter a number from the list.[/red]')
            return
        if not 0 <= index < len(entries):
            console.print('[red]That number is not in the list.[/red]')
            return

        kind, title, content = entries[index]
        edited = self._open_in_editor(content, suffix='.md')
        if edited is None:
            return
        edited = edited.strip()
        self._save_rule(kind, title, edited)
        self._apply_rules(edited)

    def _save_rule(self, kind, title, content):
        console.print('  [cyan]w.[/cyan] Write to .ccrules (this project)')
        console.print('  [cyan]d.[/cyan] Save to default_rules.json (reusable)')
        console.print('  [cyan]b.[/cyan] Both')
        console.print('  [cyan]Enter.[/cyan] Do not save, just use it for this run')
        answer = console.input('[bold]Save where? (w/d/b): [/bold]').strip().lower()

        if answer in ('w', 'b'):
            try:
                with open(os.path.join(self.root_dir, '.ccrules'), 'w', encoding='utf-8') as handle:
                    handle.write(content + '\n')
                console.print('[green]Wrote .ccrules.[/green]')
            except Exception as error:
                console.print(f'[red]Could not write .ccrules: {error}[/red]')

        if answer in ('d', 'b'):
            name = title
            if kind != 'default':
                name = console.input('[bold]Title for the saved rule: [/bold]').strip() or 'Custom Rule'
            if save_default_rule(name, content, root_dir=self.root_dir):
                console.print(f'[green]Saved {name} to default_rules.json.[/green]')
            else:
                console.print('[red]Could not save to default_rules.json.[/red]')

    def _apply_rules(self, rules_text):
        if rules_text:
            replacement = '<user_rules>\n' + rules_text + '\n</user_rules>'
        else:
            replacement = '<user_rules>\n\nThe user has not defined any custom rules.\n\n</user_rules>'
        # A lambda replacement keeps backslash sequences inside the rules from
        # being interpreted as group references.
        updated = re.sub(
            r'<user_rules>.*?</user_rules>',
            lambda match: replacement,
            self.sys_prompt,
            flags=re.DOTALL
        )
        if updated != self.sys_prompt:
            self.sys_prompt = updated
            console.print('[green]System prompt rules updated.[/green]')
        else:
            console.print('[dim]No user_rules block in the system prompt, so nothing was patched.[/dim]')

    # --- loop -----------------------------------------------------------

    def _print_banner(self):
        console.print(Rule('[bold blue]Request Area[/bold blue]'))
        console.print(f'[dim]{len(self.files)} file(s) in context. Type your request, then /send (or Alt+Enter).[/dim]')
        console.print('[dim]/notepad (F2) editor  |  /rules (F3) rule sets  |  /system edit prompt  |  /help[/dim]')
        if not PROMPT_TOOLKIT_AVAILABLE or self.session is None:
            console.print('[dim yellow]prompt_toolkit is unavailable, so F2/F3/Alt+Enter are off. Slash commands still work.[/dim yellow]')

    def _finish(self):
        request = '\n'.join(self.lines).strip()
        if not request:
            answer = console.input('[bold yellow]The request is empty. Submit anyway? (y/N): [/bold yellow]').strip().lower()
            if answer not in ('y', 'yes'):
                return None
        return {'request': request, 'system': self.sys_prompt}

    def run(self):
        self._print_banner()
        while True:
            try:
                raw = self._read_line()
            except KeyboardInterrupt:
                console.print('[yellow]Request entry cancelled.[/yellow]')
                return None
            except EOFError:
                raw = '/send'

            if raw is None:
                continue

            action = _KEY_ACTIONS.get(raw)
            if action is None:
                stripped = raw.strip()
                if stripped.startswith('/'):
                    parts = stripped[1:].split(None, 1)
                    name = parts[0].lower() if parts else ''
                    action = _COMMANDS.get(name)
                    if action is None:
                        console.print(f'[yellow]Unknown command /{name}. Type /help for the list.[/yellow]')
                        continue
                else:
                    self.lines.append(raw)
                    continue

            if action == 'send':
                finished = self._finish()
                if finished is not None:
                    return finished
                continue

            if action == 'cancel':
                console.print('[yellow]Request entry cancelled.[/yellow]')
                return None

            handler = getattr(self, '_action_' + action, None)
            if handler is not None:
                handler()


def run_cli_prompt(root_dir, files, sys_prompt, editor_override=None):
    """Collects a request through the CLI area. Returns None when cancelled."""
    return CliPromptSession(root_dir, files, sys_prompt, editor_override).run()
