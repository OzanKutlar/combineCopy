"""Persistent defaults for combineCopy.

Every CLI toggle has three states: unset (fall back to this file), explicitly
on, and explicitly off. This module owns that middle layer, plus the argv
pre-pass that rewrites `--xml off` into `--no-xml` before argparse ever runs.
"""

import json
import os
import tempfile

from combinecopy.mobile.env import is_termux

SETTINGS_DIR = os.path.expanduser('~/.configs/combineCopy')
SETTINGS_PATH = os.path.join(SETTINGS_DIR, 'settings.json')

_TRUE_WORDS = {'on', 'true', 'yes', 'y', '1', 'enable', 'enabled'}
_FALSE_WORDS = {'off', 'false', 'no', 'n', '0', 'disable', 'disabled'}

# key -> (kind, built-in default, description)
DEFAULTS = {
    'xml': ('bool', False, 'Ask the AI for XML payloads instead of JSON'),
    'prompt_ui': ('choice', 'tui', 'Request area style: cli or tui'),
    'apply_ui': ('choice', 'tui', 'Apply listener style: cli or tui'),
    'apply_chain': ('bool', False, 'Allow chained apply commands like 5m or ac'),
    'settings_ui': ('choice', 'tui', 'Settings editor style: cli or tui'),
    'tfs': ('bool', False, 'Use TFVC (tf.exe) instead of git'),
    'cli': ('bool', False, 'Let the AI emit terminal commands in its payload'),
    'consult': ('bool', False, 'Enable the external Expert LLM consult phase'),
    'consult_transport': ('choice', 'clipboard', 'How consult text crosses to the external model: clipboard, file or both'),
    'consult_answer_budget': ('int', 250, 'Per-answer word budget requested from the external model'),
    'rehab': ('bool', False, 'Enable Rehab (active learning) mode'),
    'rehab_level': ('choice', 'quiz', 'Rehab method: explain, quiz or cloze'),
    'rehab_journal': ('bool', False, 'Log rehab quiz and blank results to ~/.cc_rehab for --rehab-review'),
    'divide': ('bool', False, 'Enable Large Task Mode'),
    'diff': ('bool', False, 'Inject the uncommitted git or TFS diff into the prompt'),
    'file_culling': ('bool', False, 'Enable file culling and AST map generation'),
    'prune': ('bool', False, 'Send context pruning instructions with the system prompt'),
    'select': ('bool', False, 'Launch the file selector TUI'),
    'auto': ('bool', False, 'Run the apply listener (--apply)'),
    'file': ('bool', False, 'Save the prompt to a temp file and copy the file object'),
    'web_apply': ('bool', False, 'Enable web macro mode'),
    'mobile': ('nullable-bool', None, 'Mobile mode (auto = detect Termux)'),
    'system': ('bool-or-str', False, 'Always inject the system prompt, or a path to a custom one'),
    'identity': ('str', 'antigravity', 'AI persona in the system prompt (antigravity, claude, gemini, chatgpt, neutral, dynamic, or custom)'),
    'limit': ('int', 100, 'Maximum recursion depth for directory scans'),
    'batches': ('int', 1, 'Number of batches to split large copies into'),
    'file_types': ('list', [], 'Default file extensions to include'),
    'exclude': ('list', [], 'Directory names to always exclude from scans'),
    'search_confirm': ('bool', False, 'Ask per file how to handle oversized search results instead of taking the default'),
    'search_max_files': ('int', 100, 'Files one search query may match before you must accept, narrow or reject it'),
    'search_max_tokens': ('int', 50000, 'Tokens one search query may match before you must accept, narrow or reject it'),
    'editor': ('nullable-str', None, 'Editor command override (auto = detect)'),
}

CHOICES = {
    'prompt_ui': ('cli', 'tui'),
    'apply_ui': ('cli', 'tui'),
    'settings_ui': ('cli', 'tui'),
    'consult_transport': ('clipboard', 'file', 'both'),
    'rehab_level': ('explain', 'quiz', 'cloze'),
}

# Ordered (name, keys) pairs. Every key in DEFAULTS sits in exactly one
# category, and the check below fails at import time if the two drift apart.
CATEGORIES = (
    ('Payload & Prompt', ('xml', 'identity', 'system', 'prune', 'diff', 'file')),
    ('Interface', ('prompt_ui', 'apply_ui', 'apply_chain', 'settings_ui', 'editor')),
    ('Agent Modes', ('cli', 'divide', 'auto', 'select', 'file_culling', 'web_apply')),
    ('Rehab', ('rehab', 'rehab_level', 'rehab_journal')),
    ('Consult', ('consult', 'consult_transport', 'consult_answer_budget')),
    ('Scanning', ('limit', 'batches', 'file_types', 'exclude', 'search_confirm', 'search_max_files', 'search_max_tokens')),
    ('Environment', ('tfs', 'mobile')),
)


def _check_categories():
    listed = [key for _, keys in CATEGORIES for key in keys]
    missing = sorted(set(DEFAULTS) - set(listed))
    unknown = sorted(set(listed) - set(DEFAULTS))
    duplicated = sorted({key for key in listed if listed.count(key) > 1})
    if missing or unknown or duplicated:
        raise RuntimeError(
            'settings.CATEGORIES is out of sync with DEFAULTS: '
            f'missing={missing}, unknown={unknown}, duplicated={duplicated}'
        )


_check_categories()


def category_names():
    return [name for name, _ in CATEGORIES]


def category_keys(name):
    for category, keys in CATEGORIES:
        if category == name:
            return list(keys)
    return []


def category_of(key):
    for category, keys in CATEGORIES:
        if key in keys:
            return category
    return None


def ordered_keys():
    """Every key, in category order rather than DEFAULTS order."""
    return [key for _, keys in CATEGORIES for key in keys]


def _slug(text):
    return ''.join(char for char in text.lower() if char.isalnum())


def resolve_category(text):
    """Returns (name, error) for a 1-based number, a full name or an unambiguous prefix."""
    if not isinstance(text, str) or not text.strip():
        return None, 'No category was given.'
    token = text.strip()
    names = category_names()
    listed = ', '.join(names)
    if token.isdigit():
        index = int(token) - 1
        if 0 <= index < len(names):
            return names[index], None
        return None, f'There is no category {token}. Pick 1 to {len(names)}.'
    wanted = _slug(token)
    exact = [name for name in names if wanted and _slug(name) == wanted]
    if exact:
        return exact[0], None
    prefixed = [name for name in names if wanted and _slug(name).startswith(wanted)]
    if len(prefixed) == 1:
        return prefixed[0], None
    if prefixed:
        matches = ', '.join(prefixed)
        return None, f'{token} matches more than one category: {matches}.'
    return None, f'Unknown category {token}. Categories: {listed}.'

# argparse dest -> settings key. None means "no persistent setting; off unless asked".
ARG_TOGGLES = {
    'xml': 'xml',
    'apply_chain': 'apply_chain',
    'tfs': 'tfs',
    'cli': 'cli',
    'consult': 'consult',
    'rehab': 'rehab',
    'rehab_journal': 'rehab_journal',
    'divide': 'divide',
    'diff': 'diff',
    'file_culling': 'file_culling',
    'prune': 'prune',
    'select': 'select',
    'auto': 'auto',
    'file': 'file',
    'web_apply': 'web_apply',
    'search_confirm': 'search_confirm',
    'web': None,
    'revert': None,
    'json_select': None,
}

# Every spelling the argv pre-pass recognises, mapped to its canonical long flag.
ARGV_ALIASES = {
    '-x': '--xml', '--xml': '--xml',
    '--apply-chain': '--apply-chain',
    '--tfs': '--tfs',
    '--cli': '--cli',
    '--consult': '--consult',
    '--rehab': '--rehab',
    '--rehab-journal': '--rehab-journal',
    '--divide': '--divide',
    '-d': '--diff', '--diff': '--diff',
    '--file-culling': '--file-culling', '--file-cull': '--file-culling',
    '--prune': '--prune',
    '-s': '--select', '--select': '--select',
    '-a': '--apply', '--apply': '--apply', '--auto': '--apply',
    '--file': '--file',
    '--web-apply': '--web-apply',
    '--web': '--web',
    '-r': '--revert', '--revert': '--revert',
    '-js': '--json-select', '--json-select': '--json-select',
    '--search-confirm': '--search-confirm',
    '-m': '--mobile', '--mobile': '--mobile',
    '--system': '--system',
}


def parse_bool_word(token):
    """Returns True/False for an on/off word, or None when it is not one."""
    if not isinstance(token, str):
        return None
    key = token.strip().lower()
    if key in _TRUE_WORDS:
        return True
    if key in _FALSE_WORDS:
        return False
    return None


def _negate(flag):
    return '--no-' + flag[2:]


def normalize_argv(argv):
    """Rewrites `--xml off` and `--xml=off` into the paired --no-xml flag.

    This runs before argparse so a trailing path can never be swallowed as a
    flag value. A bare `--` ends the pass, and the following token is only
    consumed when it is exactly a recognised on/off word.
    """
    out = []
    index = 0
    total = len(argv)
    while index < total:
        token = argv[index]
        if token == '--':
            out.extend(argv[index:])
            break

        base = token
        inline = None
        if token.startswith('--') and '=' in token:
            base, inline = token.split('=', 1)

        canonical = ARGV_ALIASES.get(base)
        if canonical is None:
            out.append(token)
            index += 1
            continue

        if inline is not None:
            state = parse_bool_word(inline)
            if state is None:
                out.append(token)
            else:
                out.append(canonical if state else _negate(canonical))
            index += 1
            continue

        state = parse_bool_word(argv[index + 1]) if index + 1 < total else None
        if state is None:
            out.append(token)
            index += 1
        else:
            out.append(canonical if state else _negate(canonical))
            index += 2
    return out


def _coerce(key, value):
    """Returns the validated value, or None when the entry is unusable."""
    kind = DEFAULTS[key][0]
    if kind == 'bool':
        return value if isinstance(value, bool) else parse_bool_word(value)
    if kind == 'nullable-bool':
        if value is None or isinstance(value, bool):
            return value
        return parse_bool_word(value)
    if kind == 'int':
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 1 else None
    if kind == 'choice':
        if isinstance(value, str) and value.strip().lower() in CHOICES[key]:
            return value.strip().lower()
        return None
    if kind == 'list':
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        return None
    if kind == 'str':
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
    if kind == 'nullable-str':
        if value is None or isinstance(value, str):
            return value
        return None
    if kind == 'bool-or-str':
        if value is None or isinstance(value, bool):
            return bool(value)
        if isinstance(value, str):
            parsed = parse_bool_word(value)
            return parsed if parsed is not None else value
        return None
    return None


def _built_in():
    return {
        key: (list(spec[1]) if isinstance(spec[1], list) else spec[1])
        for key, spec in DEFAULTS.items()
    }


def _is_explicit_null(value):
    """True for the values a nullable setting may legitimately store as None."""
    if value is None:
        return True
    return isinstance(value, str) and value.strip().lower() in ('', 'auto', 'none')


def default_for(key):
    """Returns a fresh copy of a setting's built-in default."""
    default = DEFAULTS[key][1]
    return list(default) if isinstance(default, list) else default


def is_changed(key, value):
    """True when value differs from the built-in default."""
    return value != DEFAULTS[key][1]


def format_setting_value(key, value):
    """Plain-text form of a value. parse_setting_text reads it back unchanged."""
    kind = DEFAULTS[key][0]
    if kind == 'bool':
        return 'on' if value else 'off'
    if kind == 'nullable-bool':
        if value is None:
            return 'auto'
        return 'on' if value else 'off'
    if kind == 'list':
        return ' '.join(value or [])
    if kind == 'nullable-str':
        return value if value else 'auto'
    if kind == 'bool-or-str':
        if value is True:
            return 'on'
        if not value:
            return 'off'
        return str(value)
    return '' if value is None else str(value)


def _parse_bool_text(key, text):
    state = parse_bool_word(text)
    if state is None:
        return None, 'Use on or off.'
    return state, None


def _parse_nullable_bool_text(key, text):
    if text.lower() in ('', 'auto', 'none'):
        return None, None
    state = parse_bool_word(text)
    if state is None:
        return None, 'Use auto, on or off.'
    return state, None


def _parse_int_text(key, text):
    try:
        number = int(text)
    except ValueError:
        return None, 'Enter a whole number.'
    if number < 1:
        return None, 'The value must be 1 or greater.'
    return number, None


def _parse_choice_text(key, text):
    lowered = text.lower()
    if lowered in CHOICES[key]:
        return lowered, None
    options = ', '.join(CHOICES[key])
    return None, f'Pick one of: {options}.'


def _parse_list_text(key, text):
    if text.lower() in ('', 'none', 'clear'):
        return [], None
    return text.replace(',', ' ').split(), None


def _parse_str_text(key, text):
    if not text:
        return None, 'The value cannot be empty.'
    return text, None


def _parse_nullable_str_text(key, text):
    if text.lower() in ('', 'auto', 'none', 'clear'):
        return None, None
    return text, None


def _parse_bool_or_str_text(key, text):
    if not text:
        return None, 'Use on, off, or a path to a custom system prompt.'
    state = parse_bool_word(text)
    return (text if state is None else state), None


_TEXT_PARSERS = {
    'bool': _parse_bool_text,
    'nullable-bool': _parse_nullable_bool_text,
    'int': _parse_int_text,
    'choice': _parse_choice_text,
    'list': _parse_list_text,
    'str': _parse_str_text,
    'nullable-str': _parse_nullable_str_text,
    'bool-or-str': _parse_bool_or_str_text,
}


def parse_setting_text(key, raw):
    """Turns typed text into a validated value. Returns (value, error).

    The CLI editor, the settings TUI and --set all go through here, so they
    accept and reject exactly the same input. A None value with no error is
    a valid answer for the nullable settings, and means auto.
    """
    if key not in DEFAULTS:
        return None, f'Unknown setting {key!r}.'
    if not isinstance(raw, str):
        return None, 'The value must be text.'
    parser = _TEXT_PARSERS.get(DEFAULTS[key][0])
    if parser is None:
        return None, f'Setting {key!r} cannot be set from text.'
    return parser(key, raw.strip())


def load_settings(console=None):
    """Reads the settings file. A bad file degrades loudly, it never raises."""
    data = _built_in()
    if not os.path.exists(SETTINGS_PATH):
        return data

    try:
        with open(SETTINGS_PATH, 'r', encoding='utf-8') as handle:
            raw = json.load(handle)
    except Exception as error:
        if console:
            console.print(f'[dim yellow]Warning: could not read {SETTINGS_PATH} ({error}). Using built-in defaults.[/dim yellow]')
        return data

    if not isinstance(raw, dict):
        if console:
            console.print(f'[dim yellow]Warning: {SETTINGS_PATH} is not a JSON object. Using built-in defaults.[/dim yellow]')
        return data

    nullable = ('nullable-bool', 'nullable-str')
    for key, value in raw.items():
        if key not in DEFAULTS:
            if console:
                console.print(f"[dim yellow]Warning: unknown setting '{key}' was ignored.[/dim yellow]")
            continue
        coerced = _coerce(key, value)
        if coerced is None and not (DEFAULTS[key][0] in nullable and _is_explicit_null(value)):
            if console:
                console.print(f"[dim yellow]Warning: setting '{key}' has an invalid value and was ignored.[/dim yellow]")
            continue
        data[key] = coerced
    return data


def save_settings(data):
    """Writes the settings atomically so an interrupt cannot corrupt the file."""
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=SETTINGS_DIR, suffix='.tmp', text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        os.replace(temp_path, SETTINGS_PATH)
        return True
    except Exception:
        return False


def delete_settings():
    try:
        if os.path.exists(SETTINGS_PATH):
            os.remove(SETTINGS_PATH)
        return True
    except Exception:
        return False


def active_settings(settings):
    """Returns the (key, value) pairs that differ from the built-in defaults."""
    changed = []
    for key, spec in DEFAULTS.items():
        if settings.get(key) != spec[1]:
            changed.append((key, settings.get(key)))
    return changed


def describe_active(changed):
    parts = []
    for key, value in changed:
        if value is True:
            parts.append(f'{key}=on')
        elif value is False:
            parts.append(f'{key}=off')
        elif isinstance(value, list):
            parts.append(f"{key}={' '.join(value)}")
        else:
            parts.append(f'{key}={value}')
    return ', '.join(parts)


def resolve_settings(args, settings):
    """Fills every unset flag from settings. Returns the non-default settings in play."""
    for dest, key in ARG_TOGGLES.items():
        if getattr(args, dest, None) is None:
            setattr(args, dest, bool(settings.get(key, False)) if key else False)

    # Detection sits below an explicit flag but also below an explicit setting,
    # so mobile mode can be forced off permanently on a Termux device.
    if getattr(args, 'mobile', None) is None:
        forced = settings.get('mobile')
        args.mobile = is_termux() if forced is None else bool(forced)

    if getattr(args, 'prompt_ui', None) is None:
        args.prompt_ui = settings.get('prompt_ui') or 'tui'

    if getattr(args, 'apply_ui', None) is None:
        args.apply_ui = settings.get('apply_ui') or 'tui'

    if getattr(args, 'consult_transport', None) is None:
        args.consult_transport = settings.get('consult_transport') or 'clipboard'
    args.consult_answer_budget = settings.get('consult_answer_budget', 250)

    # A level on the command line is an explicit request for rehab, so it
    # switches the mode on even when the saved setting has it off.
    if getattr(args, 'rehab_level', None):
        args.rehab = True
    else:
        args.rehab_level = settings.get('rehab_level') or 'quiz'
    # From here on args.rehab carries the level itself, or None when rehab is
    # off, so prompt builders and listeners get the method with no extra flag.
    args.rehab = args.rehab_level if args.rehab else None

    if getattr(args, 'limit', None) is None:
        args.limit = settings.get('limit', 100)
    if getattr(args, 'batches', None) is None:
        args.batches = settings.get('batches', 1)
    if getattr(args, 'search_max_files', None) is None:
        args.search_max_files = settings.get('search_max_files', 100)
    if getattr(args, 'search_max_tokens', None) is None:
        args.search_max_tokens = settings.get('search_max_tokens', 50000)
    if not getattr(args, 'file_types', None):
        args.file_types = list(settings.get('file_types') or []) or None
    if not getattr(args, 'exclude', None):
        args.exclude = list(settings.get('exclude') or []) or None

    if args.system is False:
        args.system = None
    elif args.system is None:
        configured = settings.get('system', False)
        if configured is True:
            args.system = 'DEFAULT'
        elif isinstance(configured, str) and configured.strip():
            args.system = configured

    if getattr(args, 'identity', None) is None or not str(args.identity).strip():
        args.identity = settings.get('identity') or 'antigravity'
    else:
        args.identity = str(args.identity).strip()

    args.editor = settings.get('editor')
    return active_settings(settings)
