"""Terminal front end for Rehab Mode: lessons, quizzes and --rehab-review."""

from rich.markup import escape
from rich.rule import Rule

from combinecopy.rehab_core import (
    JOURNAL_PATH,
    PENDING_FILENAME,
    check_blanks,
    collect_lessons,
    fill_blanks,
    load_pending,
    missed_questions,
    read_journal,
    record_quiz_results,
    weak_concepts,
)

_MAX_ATTEMPTS = 20
_STATUS_TEXT = {
    'solved': '[green]solved, your code stays[/green]',
    'mismatch': '[red]not quite yet[/red]',
    'empty': '[yellow]still empty[/yellow]',
    'missing': '[yellow]markers not found[/yellow]',
    'error': '[red]could not write the file[/red]',
}


def print_lessons(console, file_obj):
    """Prints the lessons attached to a file and its blocks."""
    for title, lines in collect_lessons(file_obj):
        console.print(f'[bold magenta]Lesson - {escape(title)}[/bold magenta]')
        for line in lines:
            console.print(f'  [magenta]{escape(line)}[/magenta]')


def ask_quiz_cli(console, questions, show_change=None):
    """Asks each question in turn. Returns one result dict per question."""
    questions = list(questions or [])
    results = []
    for position, question in enumerate(questions):
        try:
            chosen = _ask_one(console, question, position, len(questions), show_change)
        except (KeyboardInterrupt, EOFError):
            console.print('\n[yellow]Quiz skipped.[/yellow]')
            results.extend(_result(rest, None) for rest in questions[position:])
            return results
        result = _result(question, chosen)
        _print_feedback(console, question, chosen, result['correct'])
        results.append(result)
    return results


def _result(question, chosen):
    correct = None if chosen is None else chosen == question['answer']
    return {'question': question, 'chosen': chosen, 'correct': correct}


def _ask_one(console, question, position, total, show_change):
    options = question['options']
    text = escape(question['question'])
    console.print(Rule(f'[bold magenta]Quiz {position + 1}/{total}[/bold magenta]'))
    console.print(f'[bold]{text}[/bold]')
    for number, option in enumerate(options, start=1):
        console.print(f'  [cyan]{number}.[/cyan] {escape(option)}')
    view = 'v = view the change, ' if show_change else ''
    prompt = f'[bold magenta]answer[/bold magenta] (1-{len(options)}, {view}s = skip)> '
    for _ in range(_MAX_ATTEMPTS):
        answer = console.input(prompt).strip().lower()
        if answer == 'v' and show_change is not None:
            show_change()
            continue
        if answer == 's':
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        console.print(f'[red]Type a number from 1 to {len(options)}, {view}or s to skip.[/red]')
    return None


def _print_feedback(console, question, chosen, correct):
    number = question['answer']
    right = escape(question['options'][number - 1])
    if chosen is None:
        console.print(f'[dim]Skipped. The answer was {number}: {right}[/dim]')
    elif correct:
        console.print('[bold green]Correct.[/bold green]')
    else:
        console.print(f'[bold red]Not quite.[/bold red] The answer is {number}: {right}')
    explanation = question.get('explanation')
    if explanation:
        console.print(f'[dim]{escape(explanation)}[/dim]')


def run_review(console, root_dir, journal=False):
    """Finishes open blanks, then retakes missed questions from the journal."""
    console.print(Rule('[bold magenta]Rehab Review[/bold magenta]'))
    _review_open_blanks(console, root_dir, journal)
    entries = read_journal()
    if not entries:
        console.print(
            f'[dim]The rehab journal at {escape(JOURNAL_PATH)} is empty. '
            'Turn rehab_journal on in combineCopy --settings to start recording.[/dim]'
        )
        return
    _print_weak_concepts(console, entries)
    _retake_missed(console, entries, journal)


def _review_open_blanks(console, root_dir, journal):
    blanks = load_pending(root_dir)['blanks']
    if not blanks:
        console.print('[dim]No open blanks in this workspace.[/dim]')
        return
    console.print(f'[bold]{len(blanks)} open blank(s) in {PENDING_FILENAME}:[/bold]')
    for blank in blanks:
        label = blank['id']
        where = escape(blank['path'])
        concept = escape(blank.get('concept') or '')
        console.print(f'  [cyan]rehab-{label}[/cyan] {where} [dim]{concept}[/dim]')
    try:
        answer = console.input('[bold]c = check your attempts, f = fill them all with the AI version, Enter = leave them: [/bold]').strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    paths = sorted({blank['path'] for blank in blanks})
    if answer == 'c':
        for path in paths:
            for result in check_blanks(root_dir, path, journal=journal):
                status = _STATUS_TEXT.get(result['status'], result['status'])
                console.print(f"  rehab-{result['id']}: {status}")
    elif answer == 'f':
        filled = 0
        for path in paths:
            results = fill_blanks(root_dir, path, journal=journal)
            filled += sum(1 for result in results if result['status'] == 'revealed')
        console.print(f'[green]Filled {filled} blank(s) with the AI version.[/green]')


def _print_weak_concepts(console, entries):
    weak = weak_concepts(entries)
    if not weak:
        return
    console.print('[bold]Concepts you needed the most help with:[/bold]')
    for concept, count in weak:
        console.print(f'  [magenta]{count}x[/magenta] {escape(concept)}')


def _retake_missed(console, entries, journal):
    missed = missed_questions(entries)
    if not missed:
        console.print('[green]No missed quiz questions to retake.[/green]')
        return
    try:
        answer = console.input(f'[bold]Retake {len(missed)} missed or skipped question(s)? (Y/n): [/bold]').strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if answer in ('n', 'no'):
        return
    results = ask_quiz_cli(console, missed)
    correct = sum(1 for result in results if result['correct'] is True)
    for result in results:
        record_quiz_results([result], result['question'].get('path', ''), enabled=journal)
    console.print(f'[bold magenta]Review: {correct}/{len(results)} correct.[/bold magenta]')
    if not journal:
        console.print('[dim]rehab_journal is off, so these answers were not recorded.[/dim]')
