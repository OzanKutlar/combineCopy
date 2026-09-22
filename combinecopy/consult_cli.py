"""Plain-terminal consult round trip for the CLI apply listener.

This is the non-Textual counterpart to ConsultationScreen. It reviews and
redacts the questions, sends them to the external model, collects the
answers, and returns the same result dict the screen does, so
consult_core.complete_consultation handles both.
"""

import json
import os
import tempfile

from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule

from combinecopy.consult_core import (
    INBOX_DIR,
    collect_workspace_identifiers,
    find_similar,
    format_reused_answer,
    normalize_queries,
    normalize_transport,
    parse_answers,
    read_inbound,
    scan_for_leaks,
    write_outbound,
)
from combinecopy.mobile.env import editor_display_name, run_editor
from combinecopy.prompts import build_external_consult_prompt
from combinecopy.utils import console, copy_to_clipboard

_MAX_COMMANDS = 500
_PASTE_HEADER = "# Paste the expert's whole reply below this line, then save and close.\n"
_FIELD_LABELS = (
    ('stack', 'Stack'),
    ('constraints', 'Constraints'),
    ('already_tried', 'Already tried'),
    ('want', 'Wanted'),
)
_HELP_ROWS = (
    ('l', 'List the questions, their leak flags and their status'),
    ('e', 'Edit the questions in your editor (as JSON) to redact them'),
    ('s', 'Send the unanswered questions to the expert'),
    ('r', 'Read the expert reply from the clipboard'),
    ('p', 'Paste the expert reply through your editor'),
    ('i', 'Load the newest reply file from the consult inbox'),
    ('u <id>', 'Reuse a past answer from the consult log'),
    ('f', 'Finish with the answers received so far'),
    ('q', 'Cancel the consultation'),
)


class ConsultCliSession:
    """One consult round trip in the terminal."""

    def __init__(self, queries, root_dir, known_files=None, transport='clipboard',
                 answer_budget=250, read_inbound_text=None):
        self.queries = normalize_queries(queries)
        self.root_dir = root_dir
        self.transport = normalize_transport(transport)
        self.answer_budget = answer_budget
        self.read_inbound_text = read_inbound_text
        self.answers = {}
        self.reused = set()
        self.identifiers = self._load_identifiers(known_files)
        self.similar = {query['id']: self._load_similar(query) for query in self.queries}

    # --- setup ----------------------------------------------------------

    def _load_identifiers(self, known_files):
        try:
            with console.status('[bold green]Collecting workspace identifiers for the leak check...[/bold green]', spinner='dots'):
                return collect_workspace_identifiers(self.root_dir, known_files)
        except Exception as error:
            console.print(f'[yellow]Workspace identifier scan failed ({escape(str(error))}); only pattern checks will run.[/yellow]')
            return set()

    def _load_similar(self, query):
        try:
            return find_similar(query['question'])
        except Exception as error:
            console.print(f'[dim yellow]Could not read the consult log: {escape(str(error))}[/dim yellow]')
            return []

    # --- display --------------------------------------------------------

    def _status_for(self, query_id):
        if query_id in self.reused:
            return '[green]reused[/green]'
        if query_id in self.answers:
            return '[green]answered[/green]'
        return '[yellow]pending[/yellow]'

    def _print_queries(self):
        for query in self.queries:
            query_id = query['id']
            lines = [escape(query['question'])]
            for field, label in _FIELD_LABELS:
                if query.get(field):
                    lines.append(f'[dim]{label}:[/dim] {escape(query[field])}')
            findings = scan_for_leaks([query], self.identifiers)
            for finding in findings:
                lines.append(f"[bold red]⚠ {finding['kind']}:[/bold red] {escape(finding['match'])}")
            past = self.similar.get(query_id) or []
            if past and query_id not in self.answers:
                best = past[0]
                lines.append(f"[green]Asked before ({int(best['score'] * 100)}% match, {best['date']}). 'u {query_id}' reuses it.[/green]")
            border = 'red' if findings else 'cyan'
            title = f'{escape(query_id)}  {self._status_for(query_id)}'
            console.print(Panel('\n'.join(lines), title=title, border_style=border))
        console.print(f'[dim]{len(self.answers)} of {len(self.queries)} answered.[/dim]')
        return None

    def _print_help(self):
        console.print(Rule('[bold blue]Consult Commands[/bold blue]'))
        for name, description in _HELP_ROWS:
            console.print(f'  [cyan]{escape(name):<8}[/cyan]{description}')
        return None

    # --- actions --------------------------------------------------------

    def _pending(self):
        return [query for query in self.queries if query['id'] not in self.answers]

    def _edit_queries(self):
        fd, path = tempfile.mkstemp(prefix='combineCopy_consult_', suffix='.json', text=True)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
                json.dump(self.queries, handle, indent=2, ensure_ascii=False)
            console.print(f'[dim]Opening {editor_display_name()}. Edit the questions, save, and close.[/dim]')
            if not run_editor(path):
                console.print('[red]The editor could not be launched. Nothing was changed.[/red]')
                return None
            with open(path, 'r', encoding='utf-8') as handle:
                edited = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            console.print(f'[red]Could not read the edited questions ({escape(str(error))}). Nothing was changed.[/red]')
            return None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        cleaned = normalize_queries(edited)
        if not cleaned:
            console.print('[yellow]The edit left no usable questions, so it was discarded.[/yellow]')
            return None
        self.queries = cleaned
        valid_ids = {query['id'] for query in cleaned}
        self.answers = {key: value for key, value in self.answers.items() if key in valid_ids}
        self.reused &= set(self.answers)
        console.print('[green]Questions updated.[/green]')
        return self._print_queries()

    def _send(self):
        pending = self._pending()
        if not pending:
            console.print("[green]Every question already has an answer. Press 'f' to finish.[/green]")
            return None
        findings = scan_for_leaks(pending, self.identifiers)
        if findings:
            console.print(f'[bold red]⚠ {len(findings)} possible leak(s) in the questions about to be sent:[/bold red]')
            for finding in findings:
                console.print(f"  [red]{escape(finding['id'])}[/red] {finding['kind']}: {escape(finding['match'])}")
            answer = console.input("[bold yellow]Send anyway? Press 'e' first to redact. (y/N): [/bold yellow]").strip().lower()
            if answer not in ('y', 'yes'):
                console.print('[yellow]Not sent.[/yellow]')
                return None

        prompt = build_external_consult_prompt(pending, answer_budget=self.answer_budget)
        destinations = self._deliver(prompt)
        if not destinations:
            console.print('[bold red]Could not write the questions to the clipboard or the outbox.[/bold red]')
            return None
        console.print(f"[green]Sent {len(pending)} question(s) to {escape(' and '.join(destinations))}.[/green]")
        console.print(
            "[dim]Paste them into the expert model, copy its whole reply, then press 'r'. "
            f"Or 'p' to paste it through your editor, or 'i' to load it from {escape(INBOX_DIR)}.[/dim]"
        )
        return None

    def _deliver(self, prompt):
        destinations = []
        if self.transport in ('clipboard', 'both') and copy_to_clipboard(prompt):
            destinations.append('the clipboard')
        if self.transport in ('file', 'both') or not destinations:
            path = write_outbound(prompt)
            if path:
                destinations.append(path)
        return destinations

    def _read_answers(self):
        text = self.read_inbound_text() if self.read_inbound_text else None
        return self._receive(text, 'the clipboard')

    def _load_inbox(self):
        text = read_inbound()
        if not text:
            console.print(f'[yellow]No reply files in {escape(INBOX_DIR)}.[/yellow]')
            return None
        return self._receive(text, 'the inbox')

    def _paste_answers(self):
        fd, path = tempfile.mkstemp(prefix='combineCopy_answers_', suffix='.md', text=True)
        text = ''
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(_PASTE_HEADER)
            console.print(f'[dim]Opening {editor_display_name()}. Paste the reply, save, and close.[/dim]')
            if not run_editor(path):
                console.print('[red]The editor could not be launched.[/red]')
                return None
            with open(path, 'r', encoding='utf-8') as handle:
                text = handle.read()
        except OSError as error:
            console.print(f'[red]Could not read the pasted reply: {escape(str(error))}[/red]')
            return None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return self._receive(text, 'the editor')

    def _receive(self, text, source):
        if not text or not text.strip():
            console.print(f'[yellow]Nothing was read from {source}.[/yellow]')
            return None
        answers, _ = parse_answers(text, [query['id'] for query in self.queries])
        if not answers:
            console.print(f"[yellow]No answers found in {source}. Expected blocks starting with '=== ANSWER Q1 ==='.[/yellow]")
            return None
        self.answers.update(answers)
        self.reused -= set(answers)
        console.print(f'[green]Received {len(answers)} answer(s) from {source}.[/green]')
        still_missing = [query['id'] for query in self._pending()]
        if not still_missing:
            console.print('[bold green]Every question is answered.[/bold green]')
            return 'done'
        console.print(
            f"[yellow]Still missing: {escape(', '.join(still_missing))}. "
            "Press 's' to re-send only those, or 'f' to finish without them.[/yellow]"
        )
        return None

    def _reuse(self, query_id):
        if not query_id:
            console.print('[yellow]Usage: u <id>, for example u Q1.[/yellow]')
            return None
        query = next((item for item in self.queries if item['id'].lower() == query_id.lower()), None)
        if query is None:
            console.print(f'[yellow]There is no question with id {escape(query_id)}.[/yellow]')
            return None
        past = self.similar.get(query['id']) or []
        if not past:
            console.print(f"[yellow]No earlier answer resembles {escape(query['id'])}.[/yellow]")
            return None
        self.answers[query['id']] = format_reused_answer(past[0])
        self.reused.add(query['id'])
        console.print(f"[green]Reused an earlier answer for {escape(query['id'])}.[/green]")
        return 'done' if not self._pending() else None

    # --- loop -----------------------------------------------------------

    def _dispatch(self, raw):
        command, _, argument = raw.partition(' ')
        command = command.lower()
        if command == 'q':
            return 'cancel'
        if command == 'f':
            if not self.answers:
                console.print("[yellow]No answers yet. Send the questions with 's' first, or 'q' to cancel.[/yellow]")
                return None
            return 'done'
        if command == 'u':
            return self._reuse(argument.strip())
        handlers = {
            'l': self._print_queries,
            'e': self._edit_queries,
            's': self._send,
            'r': self._read_answers,
            'p': self._paste_answers,
            'i': self._load_inbox,
            '?': self._print_help,
            'help': self._print_help,
        }
        handler = handlers.get(command)
        if handler is None:
            console.print(f"[yellow]Unknown command '{escape(raw)}'. Type '?' for help.[/yellow]")
            return None
        return handler()

    def _result(self):
        return {
            'queries': [dict(query) for query in self.queries],
            'answers': dict(self.answers),
            'missing': [query['id'] for query in self._pending()],
            'reused': sorted(self.reused),
        }

    def run(self):
        if not self.queries:
            console.print('[yellow]The CONSULT payload contained no usable questions.[/yellow]')
            return None
        console.print(Rule('[bold cyan]Consult: ask an external expert[/bold cyan]'))
        self._print_queries()
        self._print_help()
        for _ in range(_MAX_COMMANDS):
            try:
                raw = console.input('\n[bold cyan]consult[/bold cyan]> ').strip()
            except (KeyboardInterrupt, EOFError):
                return None
            if not raw:
                continue
            outcome = self._dispatch(raw)
            if outcome == 'cancel':
                return None
            if outcome == 'done':
                return self._result()
        console.print('[yellow]Too many commands; the consultation was abandoned.[/yellow]')
        return None


def run_consult_cli(queries, root_dir, known_files=None, transport='clipboard',
                    answer_budget=250, read_inbound_text=None):
    """Runs one consult round trip. Returns a result dict, or None when cancelled."""
    session = ConsultCliSession(
        queries,
        root_dir,
        known_files=known_files,
        transport=transport,
        answer_budget=answer_budget,
        read_inbound_text=read_inbound_text,
    )
    return session.run()
