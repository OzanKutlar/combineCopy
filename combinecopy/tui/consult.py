"""Review, redact, send and collect one CONSULT round trip.

The local model asks. The human checks the questions for anything internal,
carries them to a larger external model, and carries the answers back. This
screen is the checkpoint in the middle, so its job is to make that courier
step safe and cheap.
"""

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Markdown, Static, TextArea

from combinecopy.consult_core import (
    INBOX_DIR,
    OUTBOX_DIR,
    collect_workspace_identifiers,
    describe_answer_text,
    find_similar,
    format_reused_answer,
    normalize_transport,
    parse_answers,
    read_inbound,
    scan_for_leaks,
    write_outbound,
)
from combinecopy.prompts import build_external_consult_prompt
from combinecopy.utils import copy_to_clipboard

_EDITABLE_FIELDS = (
    ('stack', '#q-stack'),
    ('constraints', '#q-constraints'),
    ('already_tried', '#q-tried'),
)
_EDITOR_IDS = ('q-stack', 'q-constraints', 'q-tried')
_PREVIEW_CHARS = 800
_SNIPPET_CHARS = 70
_SEND_LABEL = 'Send (F5)'
_SEND_ARMED_LABEL = 'Send Anyway (F5)'


def _preview(text: str) -> str:
    if len(text) <= _PREVIEW_CHARS:
        return text
    clipped = text[:_PREVIEW_CHARS]
    if clipped.count('```') % 2 == 1:
        clipped += '\n```'
    return clipped + '\n\n*(preview truncated)*'


class ConsultationScreen(ModalScreen[dict | None]):
    """Modal for one consultation. Dismisses with a result dict, or None on cancel."""

    CSS = """
    ConsultationScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }
    #consult-dialog {
        width: 95%;
        height: 95%;
        border: solid #d08c60;
        background: #2d2825;
        padding: 0 1;
    }
    .consult-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        color: #d08c60;
        background: #4a3f39;
        padding: 0 1;
    }
    #consult-hint {
        color: #a0a0a0;
        height: auto;
        padding: 0 1;
    }
    #consult-body { height: 1fr; }
    #consult-left { width: 35%; border-right: solid #5a4d45; padding-right: 1; }
    #consult-right { width: 65%; padding-left: 1; }
    .consult-panel { color: #d08c60; text-style: bold; }
    #query-list { height: 1fr; }
    #query-list ListItem { height: auto; }
    .query-card { border: solid #5a4d45; background: #1e1a18; padding: 0 1; height: auto; }
    #q-question { height: 2fr; border: solid #5a4d45; background: #1e1a18; }
    #q-question:focus { border: double #d08c60; }
    #consult-status-scroll { height: 3fr; border-top: solid #5a4d45; }
    #consult-warning { color: #ff5555; text-style: bold; height: auto; padding: 0 1; }
    #consult-footer { height: 3; align: right middle; border-top: solid #5a4d45; }
    #consult-footer Button { margin-left: 1; }
    """

    BINDINGS = [
        Binding('escape', 'cancel', 'Cancel'),
        Binding('f5', 'copy_prompt', 'Send Questions'),
        Binding('f6', 'paste_answers', 'Paste Answers'),
        Binding('f7', 'load_inbox', 'Load Inbox'),
        Binding('f8', 'reuse_answer', 'Use Past Answer'),
        Binding('f9', 'finish', 'Finish'),
    ]

    def __init__(self, queries: list, root_dir: str, known_files=None,
                 transport: str = 'clipboard', answer_budget: int = 250):
        super().__init__()
        self.queries = [dict(query) for query in queries or []]
        self.root_dir = root_dir
        self.known_files = list(known_files or [])
        self.transport = normalize_transport(transport)
        self.answer_budget = answer_budget
        self.answers = {}
        self.reused = set()
        self._identifiers = None
        self._similar = {}
        self._editor_idx = None
        self._send_armed = False
        self._dismiss_on_resume = False

    # --- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id='consult-dialog'):
            yield Label('Consult: ask an external expert', classes='consult-title')
            yield Static(self._hint_text(), id='consult-hint')
            with Horizontal(id='consult-body'):
                with Vertical(id='consult-left'):
                    yield Label('Questions', classes='consult-panel')
                    items = [
                        ListItem(
                            Static(self._list_label(idx), id=f'qlabel-{idx}', classes='query-card'),
                            id=f'query-{idx}',
                        )
                        for idx in range(len(self.queries))
                    ]
                    yield ListView(*items, id='query-list')
                with Vertical(id='consult-right'):
                    yield Label('Question text (edit here to redact before sending)', classes='consult-panel')
                    yield TextArea('', id='q-question')
                    yield Input(placeholder='Stack: language, version, libraries', id='q-stack')
                    yield Input(placeholder='Constraints', id='q-constraints')
                    yield Input(placeholder='Already tried', id='q-tried')
                    with VerticalScroll(id='consult-status-scroll'):
                        yield Markdown('', id='consult-status')
            yield Label('', id='consult-warning')
            with Horizontal(id='consult-footer'):
                yield Button(_SEND_LABEL, id='btn-send', variant='success')
                yield Button('Paste Answers (F6)', id='btn-paste', variant='primary')
                yield Button('Inbox (F7)', id='btn-inbox', variant='default')
                yield Button('Past Answer (F8)', id='btn-reuse', variant='default', disabled=True)
                yield Button('Finish (F9)', id='btn-finish', variant='warning', disabled=True)
                yield Button('Cancel (Esc)', id='btn-cancel', variant='error')

    def _hint_text(self) -> str:
        targets = {
            'clipboard': 'your clipboard',
            'file': escape(OUTBOX_DIR),
            'both': f'your clipboard and {escape(OUTBOX_DIR)}',
        }
        return (
            f'Redact anything internal, then F5 sends the questions to {targets[self.transport]}. '
            'Paste them into the expert model and copy its whole reply: it is picked up automatically. '
            f'F6 pastes a reply by hand, F7 loads one from {escape(INBOX_DIR)}.'
        )

    def on_mount(self) -> None:
        if self.queries:
            self.query_one('#query-list', ListView).index = 0
            self._load_editor(0)
        self._refresh_status()
        self.run_worker(self._prepare_context, thread=True, exclusive=True)

    def on_screen_resume(self) -> None:
        if self._dismiss_on_resume:
            self._dismiss_on_resume = False
            self.dismiss(self._result())

    # --- background context ---------------------------------------------

    def _prepare_context(self) -> None:
        """Runs in a worker thread: identifier scan plus consult log lookup."""
        try:
            identifiers = collect_workspace_identifiers(self.root_dir, self.known_files)
        except Exception as error:
            identifiers = set()
            self.app.call_from_thread(
                self.notify, f'Workspace identifier scan failed: {error}', severity='warning'
            )
        similar = {}
        for query in self.queries:
            try:
                similar[query['id']] = find_similar(query['question'])
            except Exception as error:
                similar[query['id']] = []
                self.app.call_from_thread(
                    self.notify, f'Could not read the consult log: {error}', severity='warning'
                )
                break
        self.app.call_from_thread(self._on_context_ready, identifiers, similar)

    def _on_context_ready(self, identifiers: set, similar: dict) -> None:
        self._identifiers = identifiers
        self._similar = similar
        try:
            self._refresh_all()
        except NoMatches:
            # The screen was dismissed while the scan was still running.
            return

    # --- editor ---------------------------------------------------------

    def _load_editor(self, idx: int) -> None:
        if not 0 <= idx < len(self.queries):
            return
        query = self.queries[idx]
        self.query_one('#q-question', TextArea).text = query.get('question', '')
        for field, selector in _EDITABLE_FIELDS:
            self.query_one(selector, Input).value = query.get(field, '')
        self._editor_idx = idx
        self._refresh_status()

    def _commit_editor(self) -> None:
        idx = self._editor_idx
        if idx is None or not 0 <= idx < len(self.queries):
            return
        query = self.queries[idx]
        query['question'] = self.query_one('#q-question', TextArea).text.strip()
        for field, selector in _EDITABLE_FIELDS:
            value = self.query_one(selector, Input).value.strip()
            if value:
                query[field] = value
            else:
                query.pop(field, None)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if item is None or not item.id or not item.id.startswith('query-'):
            return
        try:
            idx = int(item.id.split('-', 1)[1])
        except ValueError:
            return
        if idx == self._editor_idx:
            return
        self._commit_editor()
        self._load_editor(idx)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == 'q-question':
            self._on_editor_changed()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in _EDITOR_IDS:
            self._on_editor_changed()

    def _on_editor_changed(self) -> None:
        self._commit_editor()
        self._disarm_send()
        if self._editor_idx is not None:
            self._refresh_label(self._editor_idx)
        self._refresh_status()

    # --- rendering ------------------------------------------------------

    def _current_query(self):
        idx = self._editor_idx
        if idx is None or not 0 <= idx < len(self.queries):
            return None
        return self.queries[idx]

    def _list_label(self, idx: int) -> str:
        query = self.queries[idx]
        query_id = query['id']
        if query_id in self.reused:
            status = '[green]reused[/green]'
        elif query_id in self.answers:
            status = '[green]answered[/green]'
        else:
            status = '[yellow]pending[/yellow]'
        leaks = len(scan_for_leaks([query], self._identifiers or set()))
        leak_text = f'  [bold red]⚠ {leaks} flag(s)[/bold red]' if leaks else ''
        question = query.get('question', '').replace('\n', ' ')
        snippet = question if len(question) <= _SNIPPET_CHARS else question[:_SNIPPET_CHARS] + '...'
        return f'[bold cyan]{escape(query_id)}[/bold cyan] {status}{leak_text}\n{escape(snippet)}'

    def _refresh_label(self, idx: int) -> None:
        if 0 <= idx < len(self.queries):
            self.query_one(f'#qlabel-{idx}', Static).update(self._list_label(idx))

    def _refresh_all(self) -> None:
        for idx in range(len(self.queries)):
            self._refresh_label(idx)
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.query_one('#consult-status', Markdown).update(self._status_markdown(self._current_query()))
        self._update_buttons()

    def _status_markdown(self, query) -> str:
        lines = [f'**{len(self.answers)} of {len(self.queries)} answered.**', '']
        if query is None:
            return '\n'.join(lines)
        if query.get('want'):
            lines.extend([f"*Wants: {query['want']}*", ''])
        lines.extend(self._leak_lines(query))
        answer = self.answers.get(query['id'])
        if answer:
            lines.extend(['', '### Answer received', '', _preview(answer)])
        else:
            lines.extend(self._similar_lines(query))
        return '\n'.join(lines)

    def _leak_lines(self, query: dict) -> list:
        lines = []
        if self._identifiers is None:
            lines.extend(['*Scanning workspace identifiers. Only the pattern checks have run so far.*', ''])
        findings = scan_for_leaks([query], self._identifiers or set())
        if not findings:
            lines.append('No possible leaks found in this question.')
            return lines
        lines.extend(['### Possible leaks', ''])
        for finding in findings:
            lines.append(f"- **{finding['kind']}**: `{finding['match']}`")
        lines.extend(['', 'Rename or remove these in the editor above before sending.'])
        return lines

    def _similar_lines(self, query: dict) -> list:
        similar = self._similar.get(query['id']) or []
        if not similar:
            return []
        lines = ['', '### Asked before (F8 reuses the best match)', '']
        for entry in similar:
            lines.append(f"- {int(entry['score'] * 100)}% match, {entry['date']}: {entry['question'][:120]}")
        return lines

    def _update_buttons(self) -> None:
        query = self._current_query()
        has_past = bool(query and self._similar.get(query['id']) and query['id'] not in self.answers)
        self.query_one('#btn-reuse', Button).disabled = not has_past
        self.query_one('#btn-finish', Button).disabled = not self.answers

    # --- sending --------------------------------------------------------

    def _pending_queries(self) -> list:
        return [query for query in self.queries if query['id'] not in self.answers]

    def _missing_ids(self) -> list:
        return [query['id'] for query in self._pending_queries()]

    def _arm_send(self, count: int) -> None:
        self._send_armed = True
        self.query_one('#consult-warning', Label).update(
            f'⚠ {count} possible leak(s) in the questions about to leave. '
            'Review them, or press F5 again to send anyway.'
        )
        button = self.query_one('#btn-send', Button)
        button.label = _SEND_ARMED_LABEL
        button.variant = 'error'

    def _disarm_send(self) -> None:
        if not self._send_armed:
            return
        self._send_armed = False
        self.query_one('#consult-warning', Label).update('')
        button = self.query_one('#btn-send', Button)
        button.label = _SEND_LABEL
        button.variant = 'success'

    def action_copy_prompt(self) -> None:
        self._commit_editor()
        pending = self._pending_queries()
        if not pending:
            self.notify('Every question already has an answer. Press F9 to finish.')
            return
        if any(not query.get('question') for query in pending):
            self.notify('A question is empty. Write it, or cancel the consultation.', severity='error')
            return
        findings = scan_for_leaks(pending, self._identifiers or set())
        if findings and not self._send_armed:
            self._arm_send(len(findings))
            return
        self._disarm_send()
        prompt = build_external_consult_prompt(pending, answer_budget=self.answer_budget)
        self._deliver_prompt(prompt, len(pending))

    def _deliver_prompt(self, prompt: str, count: int) -> None:
        destinations = []
        if self.transport in ('clipboard', 'both') and copy_to_clipboard(prompt):
            self._remember_outbound(prompt)
            destinations.append('the clipboard')
        if self.transport in ('file', 'both') or not destinations:
            path = write_outbound(prompt)
            if path:
                destinations.append(path)
        if not destinations:
            self.notify('Could not write the questions to the clipboard or the outbox.', severity='error')
            return
        self.notify(
            f"Sent {count} question(s) to {' and '.join(destinations)}. Paste them into the expert model.",
            title='Consult',
            timeout=8,
        )

    def _remember_outbound(self, prompt: str) -> None:
        # The listener polls the clipboard. Our own prompt must not come back
        # round as a candidate answer.
        if hasattr(self.app, 'last_clipboard'):
            self.app.last_clipboard = prompt.strip()

    # --- receiving ------------------------------------------------------

    def receive_answer_text(self, text: str, source: str = 'the clipboard') -> bool:
        """Merges any answers found in text. Returns True when at least one was found."""
        expected = [query['id'] for query in self.queries]
        answers, _ = parse_answers(text, expected)
        if not answers:
            return False
        self.answers.update(answers)
        self.reused.difference_update(answers)
        missing = self._missing_ids()
        if not missing:
            self.notify(f'All {len(expected)} answer(s) received from {source}.', title='Consult')
            self._dismiss_with_results()
            return True
        self.notify(
            f"Received {len(answers)} answer(s) from {source}. Still missing: {', '.join(missing)}. "
            'F5 re-sends only those, F9 finishes without them.',
            severity='warning',
            timeout=8,
        )
        self._refresh_all()
        return True

    def action_paste_answers(self) -> None:
        from combinecopy.tui.paste import PasteBufferScreen
        expected = [query['id'] for query in self.queries]
        self.app.push_screen(
            PasteBufferScreen(
                title='Paste Expert Answers  -  Ctrl+S submit | Ctrl+E editor | Esc cancel',
                analyzer=lambda text: describe_answer_text(text, expected),
            ),
            callback=self._on_pasted_answers,
        )

    def _on_pasted_answers(self, text) -> None:
        if not text:
            return
        if not self.receive_answer_text(text, source='the paste buffer'):
            self.notify("No answers found. Expected blocks starting with '=== ANSWER Q1 ==='.", severity='error')

    def action_load_inbox(self) -> None:
        text = read_inbound()
        if not text:
            self.notify(f'No reply files in {INBOX_DIR}.', severity='warning')
            return
        if not self.receive_answer_text(text, source='the inbox'):
            self.notify('The newest inbox file held no answers. It was moved to processed/.', severity='error')

    def action_reuse_answer(self) -> None:
        query = self._current_query()
        if query is None:
            return
        similar = self._similar.get(query['id']) or []
        if not similar or query['id'] in self.answers:
            self.notify('No earlier answer to reuse for this question.', severity='warning')
            return
        self.answers[query['id']] = format_reused_answer(similar[0])
        self.reused.add(query['id'])
        if not self._missing_ids():
            self._dismiss_with_results()
            return
        self.notify(f"Reused an earlier answer for {query['id']}.")
        self._refresh_all()

    # --- closing --------------------------------------------------------

    def _result(self) -> dict:
        return {
            'queries': [dict(query) for query in self.queries],
            'answers': dict(self.answers),
            'missing': self._missing_ids(),
            'reused': sorted(self.reused),
        }

    def _dismiss_with_results(self) -> None:
        self._commit_editor()
        if self.app.screen is not self:
            # A paste buffer is still open on top. Close once it is gone.
            self._dismiss_on_resume = True
            return
        self.dismiss(self._result())

    def action_finish(self) -> None:
        if not self.answers:
            self.notify('No answers yet. Send the questions with F5 first, or press Escape to cancel.', severity='warning')
            return
        self._dismiss_with_results()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            'btn-send': self.action_copy_prompt,
            'btn-paste': self.action_paste_answers,
            'btn-inbox': self.action_load_inbox,
            'btn-reuse': self.action_reuse_answer,
            'btn-finish': self.action_finish,
            'btn-cancel': self.action_cancel,
        }
        handler = actions.get(event.button.id)
        if handler is not None:
            handler()
