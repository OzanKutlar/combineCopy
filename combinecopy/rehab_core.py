"""Shared logic for Rehab Mode.

The model's code always lands. Rehab only adds a teaching layer that rides in
the same EXECUTION payload: short lessons, a quiz right after a file is
applied, and at the cloze level a few key lines swapped for TODO markers that
the user writes themselves. Both apply listeners drive it through this module,
the same way they share consult_core for the CONSULT phase.
"""

import json
import os
import re
import tempfile
import time

from combinecopy.apply_core import write_text_preserving
from combinecopy.utils import detect_text_newline, normalize_newlines, safe_read_file

LEVELS = ('explain', 'quiz', 'cloze')
DEFAULT_LEVEL = 'quiz'
QUIZ_LEVELS = ('quiz', 'cloze')

PENDING_FILENAME = '.cc_rehab_pending.json'
JOURNAL_DIR = os.path.expanduser('~/.cc_rehab')
JOURNAL_PATH = os.path.join(JOURNAL_DIR, 'journal.jsonl')

MAX_QUIZ_QUESTIONS = 3
MAX_BLANKS = 3
MAX_BLANK_LINES = 5
MAX_LADDER_STEPS = 4
MAX_OPTIONS = 6
PLACEHOLDER = 'pass  # <- rehab: replace this line'

# Every loop in this module is bounded by one of these.
_MAX_FIELD_CHARS = 400
_MAX_INTENT_CHARS = 160
_MAX_FIND_ATTEMPTS = 10000
_MAX_JOURNAL_LINES = 5000
_MAX_PENDING = 200

_RESOLVED = frozenset(('solved', 'kept_own', 'revealed', 'lost'))
_LESSON_FIELDS = (('why', 'Why'), ('watch_out', 'Watch out'))
_LINE_COMMENTS = (
    ('#', ('.py', '.pyw', '.sh', '.bash', '.zsh', '.rb', '.pl', '.r', '.yaml', '.yml', '.toml', '.ps1', '.cfg', '.conf')),
    ('//', ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.java', '.cs', '.c', '.h', '.cc', '.cpp', '.hpp',
            '.go', '.rs', '.kt', '.kts', '.swift', '.scala', '.dart', '.php', '.gradle')),
    ('--', ('.sql', '.lua', '.hs')),
    ('%', ('.tex',)),
)
_BLOCK_COMMENTS = (
    (('<!--', '-->'), ('.html', '.htm', '.xml', '.xaml', '.vue', '.svg', '.md')),
    (('/*', '*/'), ('.css', '.scss', '.less')),
)
_PLACEHOLDER_EXTS = ('.py', '.pyw')


# --- levels -----------------------------------------------------------------

def normalize_level(value):
    """Returns a rehab level name, or None when rehab is off.

    True means rehab is on with no level chosen, which is the default level.
    """
    if value is None or value is False:
        return None
    if value is True:
        return DEFAULT_LEVEL
    text = str(value).strip().lower()
    if text in ('', 'off', 'none', 'false'):
        return None
    return text if text in LEVELS else DEFAULT_LEVEL


def effective_level(level, revert_mode=False, web_mode=False):
    """Narrows the level to what the current listener mode can support."""
    if revert_mode:
        return None
    if web_mode and level == 'cloze':
        # Macro mode pastes into a browser IDE, so there is no local file to blank.
        return 'quiz'
    return level


def comment_syntax(path):
    """Returns (open, close) comment tokens for the file type, or None."""
    ext = os.path.splitext(path or '')[1].lower()
    if not ext:
        return None
    for token, exts in _LINE_COMMENTS:
        if ext in exts:
            return token, ''
    for pair, exts in _BLOCK_COMMENTS:
        if ext in exts:
            return pair
    return None


# --- payload normalisation --------------------------------------------------

def normalize_payload(data, level):
    """Cleans the rehab fields in place, keeping only what the level asks for."""
    if not isinstance(data, dict) or not isinstance(data.get('files'), list):
        return
    budget = {
        'blanks': MAX_BLANKS if level == 'cloze' else 0,
        'quiz': MAX_QUIZ_QUESTIONS if level in QUIZ_LEVELS else 0,
    }
    for file_obj in data['files']:
        if isinstance(file_obj, dict):
            _normalize_file(file_obj, budget)


def _normalize_file(file_obj, budget):
    _normalize_unit(file_obj, budget, file_obj.get('content'))
    for block in file_obj.get('search_replace') or []:
        if isinstance(block, dict):
            _normalize_unit(block, budget, block.get('replace'))
    quiz = _clean_quiz(file_obj.get('quiz'), budget)
    if quiz:
        file_obj['quiz'] = quiz
    else:
        file_obj.pop('quiz', None)


def _normalize_unit(unit, budget, source):
    # 'instruction' is the old rehab field. It still makes a usable lesson.
    lesson = _clean_lesson(unit.get('lesson')) or _clean_lesson(unit.get('instruction'))
    unit.pop('instruction', None)
    unit.pop('hints', None)
    if lesson:
        unit['lesson'] = lesson
    else:
        unit.pop('lesson', None)
    blank = _clean_blank(unit.get('blank'), source)
    if blank and budget['blanks'] > 0:
        unit['blank'] = blank
        unit['ladder'] = _clean_ladder(unit.get('ladder'))
        budget['blanks'] -= 1
    else:
        unit.pop('blank', None)
        unit.pop('ladder', None)


def _clip(value, limit=_MAX_FIELD_CHARS):
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())[:limit]


def _clean_lesson(raw):
    if isinstance(raw, str):
        text = _clip(raw)
        return {'concept': text} if text else {}
    if not isinstance(raw, dict):
        return {}
    lesson = {}
    for key in ('concept', 'why', 'watch_out'):
        text = _clip(raw.get(key))
        if text:
            lesson[key] = text
    return lesson


def _clean_ladder(raw):
    if not isinstance(raw, list):
        return []
    steps = [_clip(step) for step in raw[:MAX_LADDER_STEPS]]
    return [step for step in steps if step]


def _clean_blank(raw, source):
    """Returns the blank as whole LF lines, or None when it cannot be used."""
    if not isinstance(raw, str) or not raw.strip() or not isinstance(source, str):
        return None
    blank = normalize_newlines(raw, '\n')
    if not blank.endswith('\n'):
        blank += '\n'
    if blank.count('\n') > MAX_BLANK_LINES:
        return None
    # A blank must be lifted from the code that is about to land, or there is
    # nothing to swap out.
    if blank.rstrip('\n') not in normalize_newlines(source, '\n'):
        return None
    return blank


def _clean_quiz(raw, budget):
    if not isinstance(raw, list) or budget['quiz'] <= 0:
        return []
    quiz = []
    for position, item in enumerate(raw[:MAX_QUIZ_QUESTIONS]):
        if budget['quiz'] <= 0:
            break
        question = clean_question(item, position)
        if question:
            quiz.append(question)
            budget['quiz'] -= 1
    return quiz


def clean_question(item, position=0):
    """Returns a validated quiz question, or None when it cannot be graded."""
    if not isinstance(item, dict):
        return None
    raw_options = item.get('options')
    if not isinstance(raw_options, list):
        return None
    text = _clip(item.get('question') or item.get('text'))
    options = [_clip(option) for option in raw_options[:MAX_OPTIONS]]
    options = [option for option in options if option]
    if not text or len(options) < 2:
        return None
    answer = _parse_answer(item.get('answer'), options)
    if answer is None:
        return None
    return {
        'id': _clip(str(item.get('id') or f'Q{position + 1}'), 20),
        'question': text,
        'options': options,
        'answer': answer,
        'explanation': _clip(item.get('explanation')),
    }


def _parse_answer(raw, options):
    """Reads the 1-based correct option. An unreadable answer drops the question."""
    if isinstance(raw, bool):
        return None
    value = None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        token = raw.strip().lower()
        if token.isdigit():
            value = int(token)
        elif len(token) == 1 and 'a' <= token <= 'z':
            value = ord(token) - ord('a') + 1
        else:
            # Some models repeat the option text instead of its number.
            lowered = [option.lower() for option in options]
            value = lowered.index(token) + 1 if token in lowered else None
    if value is None or not 1 <= value <= len(options):
        return None
    return value


# --- lessons ----------------------------------------------------------------

def collect_lessons(file_obj):
    """Returns [(title, [lines])] for every lesson on a file and its blocks."""
    if not isinstance(file_obj, dict):
        return []
    units = [('File', file_obj)]
    for position, block in enumerate(file_obj.get('search_replace') or []):
        if isinstance(block, dict):
            units.append((f'Block {position + 1}', block))
    entries = []
    for label, unit in units:
        lesson = unit.get('lesson')
        if not isinstance(lesson, dict) or not lesson:
            continue
        title = label + ': ' + lesson['concept'] if lesson.get('concept') else label
        lines = [f'{name}: {lesson[key]}' for key, name in _LESSON_FIELDS if lesson.get(key)]
        entries.append((title, lines))
    return entries


# --- blanks -----------------------------------------------------------------

def apply_blanks(new_text, file_obj, path, root_dir=None):
    """Swaps each blank for TODO markers. Returns (text, notes).

    With a root_dir the blanks are registered in the pending store, which is
    what applying a file does. Without one this is a preview for the diff
    view, and nothing is written anywhere.
    """
    units = _blank_units(file_obj)
    if not units or not isinstance(new_text, str):
        return new_text, []
    syntax = comment_syntax(path)
    if syntax is None:
        return new_text, [f'Rehab blanks skipped for {path}: no comment syntax is known for this file type.']
    store = load_pending(root_dir) if root_dir else _empty_store()
    next_id = store['next_id']
    working = normalize_newlines(new_text, '\n')
    opened = []
    notes = []
    for unit in units:
        span = _locate_blank(working, unit)
        if span is None:
            notes.append(f'A rehab blank in {path} was not found in the applied text, so those lines were kept.')
            continue
        start, end = span
        answer = _with_newline(working[start:end])
        working = working[:start] + _marker_block(next_id, answer, unit, syntax, path) + working[end:]
        opened.append(_pending_entry(next_id, path, answer, unit))
        next_id += 1
    if not opened:
        return new_text, notes
    if root_dir:
        store['blanks'].extend(opened)
        store['next_id'] = next_id
        if not save_pending(root_dir, store):
            # Without the saved answers the markers could never be filled back
            # in, so the AI version is kept instead.
            return new_text, notes + [f'Could not save {PENDING_FILENAME}, so no blanks were opened in {path}.']
        notes.append(
            f'{len(opened)} rehab blank(s) opened in {path}. Write the missing line(s) between the '
            'TODO(rehab-N) and END(rehab-N) markers. n gives a hint, k checks, g fills them in.'
        )
    return normalize_newlines(working, detect_text_newline(new_text)), notes


def _blank_units(file_obj):
    if not isinstance(file_obj, dict):
        return []
    units = [file_obj] if file_obj.get('blank') else []
    for block in file_obj.get('search_replace') or []:
        if isinstance(block, dict) and block.get('blank'):
            units.append(block)
    return units


def _locate_blank(working, unit):
    """Returns the (start, end) of the blank's lines in the applied text, or None."""
    blank = unit.get('blank')
    if not isinstance(blank, str) or not blank:
        return None
    source = unit.get('replace') if isinstance(unit.get('replace'), str) else unit.get('content')
    anchor = -1
    if isinstance(source, str) and source.strip():
        anchor = working.find(normalize_newlines(source, '\n').rstrip('\n'))
    start = _find_line_start(working, blank, max(anchor, 0))
    if start < 0 and anchor > 0:
        start = _find_line_start(working, blank, 0)
    if start >= 0:
        return start, start + len(blank)
    tail = blank.rstrip('\n')
    # The blank may be the last line of a file that has no trailing newline.
    if tail and working.endswith(tail):
        start = len(working) - len(tail)
        if start == 0 or working[start - 1] == '\n':
            return start, len(working)
    return None


def _find_line_start(text, needle, offset):
    cursor = offset
    for _ in range(_MAX_FIND_ATTEMPTS):
        found = text.find(needle, cursor)
        if found < 0:
            return -1
        if found == 0 or text[found - 1] == '\n':
            return found
        cursor = found + 1
    return -1


def _marker_block(blank_id, answer, unit, syntax, path):
    open_token, close_token = syntax
    indent = _indent_of(answer)
    tail = f' {close_token}' if close_token else ''
    lines = [f'{indent}{open_token} TODO(rehab-{blank_id}): {_intent_for(unit, close_token)}{tail}']
    if os.path.splitext(path)[1].lower() in _PLACEHOLDER_EXTS:
        # An empty Python block will not even import, so leave a stand-in.
        lines.append(f'{indent}{PLACEHOLDER}')
    lines.append(f'{indent}{open_token} END(rehab-{blank_id}){tail}')
    return '\n'.join(lines) + '\n'


def _indent_of(text):
    for line in text.split('\n'):
        if line.strip():
            return line[:len(line) - len(line.lstrip())]
    return ''


def _intent_for(unit, close_token):
    ladder = unit.get('ladder') or []
    lesson = unit.get('lesson') or {}
    intent = (ladder[0] if ladder else '') or lesson.get('concept') or 'write the missing line(s)'
    intent = _clip(intent, _MAX_INTENT_CHARS)
    return intent.replace(close_token, '') if close_token else intent


def _pending_entry(blank_id, path, answer, unit):
    ladder = list(unit.get('ladder') or [])
    # The first rung is already printed in the TODO marker, so it counts as seen.
    seen = 1 if ladder else 0
    return {
        'id': blank_id,
        'path': _norm_path(path),
        'answer': answer,
        'ladder': ladder,
        'concept': (unit.get('lesson') or {}).get('concept', ''),
        'rungs': seen,
        'free_rungs': seen,
        'created': time.time(),
    }


def _norm_path(path):
    return (path or '').replace('\\', '/').strip()


def _with_newline(text):
    return text if text.endswith('\n') else text + '\n'


# --- pending store ----------------------------------------------------------

def _empty_store():
    return {'next_id': 1, 'blanks': []}


def load_pending(root_dir):
    """Reads the open blanks for a workspace. A bad file reads as empty."""
    store = _empty_store()
    if not root_dir:
        return store
    path = os.path.join(root_dir, PENDING_FILENAME)
    if not os.path.exists(path):
        return store
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return store
    if not isinstance(raw, dict) or not isinstance(raw.get('blanks'), list):
        return store
    blanks = [entry for entry in raw['blanks'][:_MAX_PENDING] if _valid_pending(entry)]
    highest = max((entry['id'] for entry in blanks), default=0)
    next_id = raw.get('next_id')
    store['next_id'] = next_id if isinstance(next_id, int) and next_id > highest else highest + 1
    store['blanks'] = blanks
    return store


def _valid_pending(entry):
    if not isinstance(entry, dict):
        return False
    if not isinstance(entry.get('id'), int) or not isinstance(entry.get('path'), str):
        return False
    if not isinstance(entry.get('answer'), str):
        return False
    if not isinstance(entry.get('ladder'), list):
        entry['ladder'] = []
    for key in ('rungs', 'free_rungs'):
        if not isinstance(entry.get(key), int):
            entry[key] = 0
    if not isinstance(entry.get('concept'), str):
        entry['concept'] = ''
    return True


def save_pending(root_dir, store):
    """Writes the open blanks atomically, removing the file once none remain."""
    path = os.path.join(root_dir, PENDING_FILENAME)
    try:
        if not store['blanks']:
            if os.path.exists(path):
                os.remove(path)
            return True
        fd, temp_path = tempfile.mkstemp(dir=root_dir, prefix='.cc_rehab_', suffix='.tmp', text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(store, handle, indent=2, ensure_ascii=False)
        os.replace(temp_path, path)
        return True
    except OSError:
        return False


def open_blanks_for(root_dir, path):
    key = _norm_path(path)
    return [entry for entry in load_pending(root_dir)['blanks'] if entry['path'] == key]


def count_open_blanks(root_dir, paths):
    keys = {_norm_path(path) for path in paths or [] if path}
    if not keys:
        return 0
    return sum(1 for entry in load_pending(root_dir)['blanks'] if entry['path'] in keys)


# --- working a blank --------------------------------------------------------

def reveal_next_rung(root_dir, path):
    """Reveals the next hint for the first open blank in a file."""
    store = load_pending(root_dir)
    key = _norm_path(path)
    blank = next((entry for entry in store['blanks'] if entry['path'] == key), None)
    if blank is None:
        return {'error': 'This file has no open rehab blanks.'}
    ladder = blank['ladder']
    if blank['rungs'] >= len(ladder):
        return {'id': blank['id'], 'exhausted': True, 'total': len(ladder)}
    text = ladder[blank['rungs']]
    blank['rungs'] += 1
    save_pending(root_dir, store)
    return {'id': blank['id'], 'text': text, 'rung': blank['rungs'], 'total': len(ladder)}


def check_blanks(root_dir, path, journal=False):
    """Grades every open blank in a file. A correct attempt closes the blank."""
    return _process_blanks(root_dir, path, _decide_check, None, journal)


def accept_attempts(root_dir, path, ids, journal=False):
    """Keeps the user's own code for the given blanks, even where it differs."""
    return _process_blanks(root_dir, path, _decide_keep, set(ids or []), journal)


def fill_blanks(root_dir, path, ids=None, journal=False):
    """Writes the AI version into the open blanks of a file."""
    wanted = set(ids) if ids is not None else None
    return _process_blanks(root_dir, path, _decide_fill, wanted, journal)


def _decide_check(blank, attempt):
    if attempt is None:
        return 'missing', None
    if not attempt.strip():
        return 'empty', None
    if _same_code(attempt, blank['answer']):
        return 'solved', attempt
    return 'mismatch', None


def _decide_keep(blank, attempt):
    if attempt is None:
        return 'missing', None
    if not attempt.strip():
        return 'empty', None
    return 'kept_own', attempt


def _decide_fill(blank, attempt):
    if attempt is None:
        # The markers are gone, so there is nowhere left to write the answer.
        return 'lost', None
    return 'revealed', blank['answer']


def _process_blanks(root_dir, path, decide, wanted, journal):
    store = load_pending(root_dir)
    key = _norm_path(path)
    targets = [
        entry for entry in store['blanks']
        if entry['path'] == key and (wanted is None or entry['id'] in wanted)
    ]
    if not targets:
        return []
    full_path = os.path.join(root_dir, key)
    original = safe_read_file(full_path) if os.path.isfile(full_path) else None
    text = normalize_newlines(original, '\n') if original else ''
    results = []
    changed = False
    for blank in targets:
        span = _marker_span(text, blank['id']) if text else None
        attempt = _extract_attempt(text[span[1]:span[2]]) if span else None
        status, replacement = decide(blank, attempt)
        if span and replacement is not None:
            text = text[:span[0]] + _with_newline(replacement) + text[span[3]:]
            changed = True
        results.append({'id': blank['id'], 'status': status, 'attempt': attempt or '', 'answer': blank['answer']})
    if changed:
        try:
            write_text_preserving(full_path, text, original_newline=detect_text_newline(original))
        except OSError as error:
            return [dict(result, status='error', error=str(error)) for result in results]
    for blank, result in zip(targets, results):
        if result['status'] in _RESOLVED:
            _resolve(store, blank, result['status'], journal)
    if any(result['status'] in _RESOLVED for result in results):
        save_pending(root_dir, store)
    return results


def _marker_span(text, blank_id):
    """Returns (start, body_start, body_end, end) around a blank's markers, or None."""
    start = re.compile(rf'^[ \t]*\S+ TODO\(rehab-{blank_id}\):[^\n]*(?:\n|$)', re.M).search(text)
    if start is None:
        return None
    end = re.compile(rf'^[ \t]*\S+ END\(rehab-{blank_id}\)[^\n]*(?:\n|$)', re.M).search(text, start.end())
    if end is None:
        return None
    return start.start(), start.end(), end.start(), end.end()


def _extract_attempt(body):
    lines = [line for line in body.split('\n') if line.strip() != PLACEHOLDER]
    return '\n'.join(lines)


def _code_lines(text):
    return [' '.join(line.split()) for line in text.split('\n') if line.strip()]


def _same_code(attempt, answer):
    return _code_lines(attempt) == _code_lines(answer)


def _resolve(store, blank, status, journal):
    store['blanks'] = [entry for entry in store['blanks'] if entry['id'] != blank['id']]
    append_journal({
        'type': 'blank',
        'ts': time.time(),
        'path': blank['path'],
        'concept': blank.get('concept', ''),
        'hints_used': max(0, blank.get('rungs', 0) - blank.get('free_rungs', 0)),
        'outcome': status,
    }, enabled=journal)


# --- journal ----------------------------------------------------------------

def append_journal(entry, enabled=False):
    """Appends one entry to the rehab journal. Does nothing while the journal is off."""
    if not enabled or not isinstance(entry, dict):
        return False
    try:
        os.makedirs(JOURNAL_DIR, exist_ok=True)
        with open(JOURNAL_PATH, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        return False
    return True


def read_journal():
    if not os.path.exists(JOURNAL_PATH):
        return []
    try:
        with open(JOURNAL_PATH, 'r', encoding='utf-8', errors='replace') as handle:
            lines = handle.readlines()[-_MAX_JOURNAL_LINES:]
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get('type') in ('quiz', 'blank'):
            entries.append(entry)
    return entries


def record_quiz_results(results, path, enabled=False):
    """Journals one quiz sitting. Returns (correct, answered, total)."""
    results = [
        result for result in results or []
        if isinstance(result, dict) and isinstance(result.get('question'), dict)
    ]
    correct = 0
    answered = 0
    for result in results:
        question = result['question']
        chosen = result.get('chosen')
        if chosen is not None:
            answered += 1
        if result.get('correct') is True:
            correct += 1
        append_journal({
            'type': 'quiz',
            'ts': time.time(),
            'path': path or '',
            'question': question.get('question', ''),
            'options': question.get('options', []),
            'answer': question.get('answer'),
            'explanation': question.get('explanation', ''),
            'chosen': chosen,
            'correct': result.get('correct'),
        }, enabled=enabled)
    return correct, answered, len(results)


def missed_questions(entries, limit=10):
    """Returns the questions whose latest attempt was wrong or skipped."""
    latest = {}
    for entry in entries:
        if entry.get('type') == 'quiz' and isinstance(entry.get('question'), str):
            latest[entry['question']] = entry
    missed = []
    for entry in latest.values():
        if len(missed) >= limit:
            break
        if entry.get('correct') is True:
            continue
        question = clean_question(entry, len(missed))
        if question:
            question['path'] = entry.get('path', '')
            missed.append(question)
    return missed


def weak_concepts(entries, limit=5):
    """Returns [(concept, count)] for the blanks that needed hints or a reveal."""
    counts = {}
    for entry in entries:
        concept = entry.get('concept')
        if entry.get('type') != 'blank' or not isinstance(concept, str) or not concept:
            continue
        hints = entry.get('hints_used')
        if entry.get('outcome') == 'revealed' or (isinstance(hints, int) and hints > 0):
            counts[concept] = counts.get(concept, 0) + 1
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
