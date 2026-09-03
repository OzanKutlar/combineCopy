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
    'tfs': ('bool', False, 'Use TFVC (tf.exe) instead of git'),
    'cli': ('bool', False, 'Let the AI emit terminal commands in its payload'),
    'consult': ('bool', False, 'Enable the external Expert LLM consult phase'),
    'rehab': ('bool', False, 'Enable Rehab (active learning) mode'),
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
    'limit': ('int', 100, 'Maximum recursion depth for directory scans'),
    'batches': ('int', 1, 'Number of batches to split large copies into'),
    'file_types': ('list', [], 'Default file extensions to include'),
    'exclude': ('list', [], 'Directory names to always exclude from scans'),
    'editor': ('nullable-str', None, 'Editor command override (auto = detect)'),
}

CHOICES = {'prompt_ui': ('cli', 'tui'), 'apply_ui': ('cli', 'tui')}

# argparse dest -> settings key. None means "no persistent setting; off unless asked".
ARG_TOGGLES = {
    'xml': 'xml',
    'tfs': 'tfs',
    'cli': 'cli',
    'consult': 'consult',
    'rehab': 'rehab',
    'divide': 'divide',
    'diff': 'diff',
    'file_culling': 'file_culling',
    'prune': 'prune',
    'select': 'select',
    'auto': 'auto',
    'file': 'file',
    'web_apply': 'web_apply',
    'web': None,
    'revert': None,
    'json_select': None,
}

# Every spelling the argv pre-pass recognises, mapped to its canonical long flag.
ARGV_ALIASES = {
    '-x': '--xml', '--xml': '--xml',
    '--tfs': '--tfs',
    '--cli': '--cli',
    '--consult': '--consult',
    '--rehab': '--rehab',
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
        if coerced is None and DEFAULTS[key][0] not in nullable:
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

    if getattr(args, 'limit', None) is None:
        args.limit = settings.get('limit', 100)
    if getattr(args, 'batches', None) is None:
        args.batches = settings.get('batches', 1)
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

    args.editor = settings.get('editor')
    return active_settings(settings)
