"""The Rehab quiz that runs right after a file is applied.

The change being asked about stays on the left for the whole quiz, so every
question is answered by reading the code rather than from memory.
"""

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, Markdown, RichLog, Static

from combinecopy.mobile.env import is_narrow_screen
from combinecopy.utils import render_word_diff

_MAX_OPTION_KEYS = 6


class QuizScreen(ModalScreen[list]):
    """Dismisses with one result dict per question, skipped ones included."""

    CSS = '''
    QuizScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }
    #quiz-dialog {
        width: 95%;
        height: 95%;
        border: solid #d08c60;
        background: #2d2825;
        padding: 0 1;
    }
    .quiz-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        color: #d08c60;
        background: #4a3f39;
        padding: 0 1;
    }
    #quiz-body { height: 1fr; }
    #quiz-code { width: 60%; border-right: solid #5a4d45; padding-right: 1; }
    #quiz-side { width: 40%; padding-left: 1; }
    .quiz-panel { color: #d08c60; text-style: bold; }
    #quiz-diff { height: 1fr; border: solid #5a4d45; background: #1e1a18; }
    #quiz-question { height: auto; margin: 1 0; }
    #quiz-options { height: auto; margin-bottom: 1; }
    #quiz-feedback { height: auto; }
    #quiz-keys { width: 100%; height: auto; padding: 0 1; }
    QuizScreen.narrow #quiz-body { layout: vertical; }
    QuizScreen.narrow #quiz-code {
        width: 100%;
        height: 55%;
        border-right: none;
        border-bottom: solid #5a4d45;
        padding-right: 0;
    }
    QuizScreen.narrow #quiz-side { width: 100%; height: 45%; padding-left: 0; }
    '''

    BINDINGS = [
        Binding('escape', 'skip', 'Skip Quiz'),
        Binding('enter', 'next', 'Next'),
    ] + [
        Binding(str(number), f'answer({number})', f'Answer {number}', show=False)
        for number in range(1, _MAX_OPTION_KEYS + 1)
    ]

    def __init__(self, path: str, questions: list, old_text: str = '', new_text: str = ''):
        super().__init__()
        self.path = path or 'file'
        self.questions = list(questions or [])
        self.old_text = old_text or ''
        self.new_text = new_text or ''
        self.position = 0
        self.chosen = None
        self.results = []

    def compose(self) -> ComposeResult:
        with Vertical(id='quiz-dialog'):
            yield Label(f'Rehab Quiz: {escape(self.path)}', classes='quiz-title')
            with Horizontal(id='quiz-body'):
                with Vertical(id='quiz-code'):
                    yield Label('The change you just applied', classes='quiz-panel')
                    yield RichLog(id='quiz-diff', wrap=False)
                with Vertical(id='quiz-side'):
                    yield Label('', id='quiz-progress', classes='quiz-panel')
                    yield Static('', id='quiz-question')
                    yield Static('', id='quiz-options')
                    yield Markdown('', id='quiz-feedback')
            yield Label('', id='quiz-keys')

    def on_mount(self) -> None:
        if is_narrow_screen():
            self.add_class('narrow')
        log = self.query_one('#quiz-diff', RichLog)
        if self.old_text == self.new_text:
            log.write(Text('No change was recorded for this file.', style='dim'))
        else:
            render_word_diff(self.old_text, self.new_text, log)
        if not self.questions:
            self.dismiss([])
            return
        self._render_question()

    def _current(self) -> dict:
        return self.questions[self.position]

    def _render_question(self) -> None:
        question = self._current()
        count = len(question['options'])
        self.query_one('#quiz-progress', Label).update(f'Question {self.position + 1} of {len(self.questions)}')
        self.query_one('#quiz-question', Static).update(Text(question['question'], style='bold'))
        self.query_one('#quiz-feedback', Markdown).update('')
        self._render_options(question)
        self._set_keys(f'Press 1-{count} to answer  |  Esc skips the rest  |  scroll the change on the left')

    def _render_options(self, question: dict) -> None:
        text = Text()
        for number, option in enumerate(question['options'], start=1):
            mark, style = '  ', ''
            if self.chosen is not None:
                if number == question['answer']:
                    mark, style = '✓ ', 'bold green'
                elif number == self.chosen:
                    mark, style = '✗ ', 'bold red'
                else:
                    style = 'dim'
            text.append(f'{mark}{number}. {option}\n', style=style)
        self.query_one('#quiz-options', Static).update(text)

    def _set_keys(self, message: str) -> None:
        self.query_one('#quiz-keys', Label).update(Text(message, style='dim'))

    def action_answer(self, choice: int) -> None:
        if self.chosen is not None or self.position >= len(self.questions):
            return
        question = self._current()
        if not 1 <= choice <= len(question['options']):
            return
        self.chosen = choice
        self._render_options(question)
        number = question['answer']
        verdict = '**Correct.**' if choice == number else f'**Not quite.** The answer is {number}.'
        explanation = question.get('explanation', '')
        self.query_one('#quiz-feedback', Markdown).update(f'{verdict} {explanation}'.strip())
        last = self.position + 1 >= len(self.questions)
        self._set_keys('Enter to finish' if last else 'Enter for the next question')

    def action_next(self) -> None:
        if self.chosen is None:
            self.notify('Pick an answer with the number keys, or press Esc to skip.', severity='warning')
            return
        self.results.append(_result(self._current(), self.chosen))
        self.position += 1
        self.chosen = None
        if self.position >= len(self.questions):
            self.dismiss(self.results)
            return
        self._render_question()

    def action_skip(self) -> None:
        for index in range(self.position, len(self.questions)):
            chosen = self.chosen if index == self.position else None
            self.results.append(_result(self.questions[index], chosen))
        self.dismiss(self.results)


def _result(question, chosen):
    correct = None if chosen is None else chosen == question['answer']
    return {'question': question, 'chosen': chosen, 'correct': correct}
