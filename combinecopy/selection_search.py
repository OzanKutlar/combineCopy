"""Text searches requested by a SELECT payload.

Every search query runs in three stages. Each target file is evaluated first,
without touching the context. The query's total breadth is then checked
against the configured limits, and the user decides when it is too wide.
Only the files that survive are committed to the context.
"""

import os
import re
from dataclasses import dataclass, field

from rich.markup import escape

from combinecopy.tui.selection import run_file_selector
from combinecopy.utils import (
    SEARCH_CONTEXT_LINES,
    build_search_blocks,
    console,
    estimate_tokens,
    find_search_hits,
    partial_coverage_ratio,
    render_partial_content,
    safe_read_file,
)

# Beyond this many hits, or this much of one file exposed, that file's results
# count as oversized and become either the whole file or every block.
SEARCH_HIT_WARN_THRESHOLD = 25
SEARCH_COVERAGE_WARN_RATIO = 0.8

DEFAULT_MAX_FILES = 100
DEFAULT_MAX_TOKENS = 50000

_MAX_PROMPT_ATTEMPTS = 5
_MAX_BREADTH_ROUNDS = 5
_MAX_LISTED_FILES = 20
_MAX_QUERY_LABEL = 60
_BINARY_MARKER = '(This is a binary'
_UNRESOLVED = '(unresolved)'
_ALL_FILES = 'all files'


@dataclass(frozen=True)
class SearchOptions:
    """How searches are decided for one run."""
    confirm: bool = False
    max_files: int = DEFAULT_MAX_FILES
    max_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self):
        for name in ('max_files', 'max_tokens'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f'SearchOptions.{name} must be a whole number of 1 or more, got {value!r}.')

    @classmethod
    def from_args(cls, args):
        """Builds options from CLI args that resolve_settings has already filled."""
        return cls(
            confirm=bool(getattr(args, 'search_confirm', False)),
            max_files=getattr(args, 'search_max_files', None) or DEFAULT_MAX_FILES,
            max_tokens=getattr(args, 'search_max_tokens', None) or DEFAULT_MAX_TOKENS,
        )


@dataclass
class SearchContext:
    """The selection state that searches read from and commit into."""
    root_dir: str
    scanned_files: list
    resolved_map: dict
    found_files: list
    important_files: list
    partial_files: dict
    folder_scopes: dict = field(default_factory=dict)


def _rel(path, root_dir):
    return os.path.relpath(path, root_dir).replace('\\', '/')


def _short_query(query):
    label = query.replace('\n', ' ')
    return label if len(label) <= _MAX_QUERY_LABEL else label[:_MAX_QUERY_LABEL] + '...'


def _report(query, path, note, hits=0, kind='file'):
    return {'query': query, 'path': path, 'hits': hits, 'truncated': 0, 'note': note, 'kind': kind}


# --- folder targets ---------------------------------------------------------

def _normalize_folder_path(entry_path):
    text = re.sub(r'^(?:\./)+', '', entry_path.strip().replace('\\', '/'))
    return text.strip('/')


def _known_directories(rel_files):
    directories = set()
    for rel in rel_files:
        parts = rel.split('/')[:-1]
        for depth in range(1, len(parts) + 1):
            directories.add('/'.join(parts[:depth]))
    return directories


def resolve_search_folder(entry_path, scanned_files, root_dir):
    """Returns (matched_dirs, rel_files) when entry_path names a folder, else None.

    A path matching a scanned file exactly is always a file. A partial folder
    path such as 'tui' matches every folder ending in that segment. Files come
    from the scan, so the extension and exclude filters still apply.
    """
    if not isinstance(entry_path, str) or not entry_path.strip():
        return None
    folder = _normalize_folder_path(entry_path)
    rel_files = [_rel(path, root_dir) for path in scanned_files]
    if folder in ('', '.'):
        return ['.'], rel_files
    if folder in rel_files:
        return None
    known = _known_directories(rel_files)
    if folder in known or os.path.isdir(os.path.join(root_dir, folder)):
        matched = [folder]
    else:
        matched = sorted(directory for directory in known if directory.endswith('/' + folder))
    if not matched:
        return None
    prefixes = tuple(directory + '/' for directory in matched)
    return matched, [rel for rel in rel_files if rel.startswith(prefixes)]


def folder_label(directories):
    """Describes a folder scope, for example: directory 'combinecopy/tui/'."""
    if directories == ['.']:
        return _ALL_FILES
    quoted = ', '.join(f"'{directory}/'" for directory in directories)
    noun = 'directory' if len(directories) == 1 else 'directories'
    return f'{noun} {quoted}'


def split_folder_searches(search_list, scanned_files, root_dir):
    """Returns (folder_scopes, folder_files) for every search path that names a folder."""
    scopes = {}
    files = {}
    for entry in search_list or []:
        path = entry.get('path') if isinstance(entry, dict) else None
        if not isinstance(path, str) or path in scopes:
            continue
        resolved = resolve_search_folder(path, scanned_files, root_dir)
        if resolved is None:
            continue
        scopes[path], files[path] = resolved
        console.print(
            f'  Search path [cyan]{escape(path)}[/cyan] resolved to '
            f'{escape(folder_label(resolved[0]))} ({len(resolved[1])} scanned file(s)).'
        )
    return scopes, files


# --- target resolution ------------------------------------------------------

def pick_search_targets(entry_path, scanned_files, root_dir):
    """Asks which files to search when a path cannot be resolved. Returns (rel_paths, scope)."""
    label = f"'{entry_path}'" if entry_path else '(no path was provided)'
    console.print(f'\n[bold yellow]Search target {escape(label)} could not be resolved.[/bold yellow]')
    console.print(f'The current workspace scan holds [cyan]{len(scanned_files)}[/cyan] file(s).')
    console.print('  [cyan]A.[/cyan] Search every file in the scanned workspace')
    console.print('  [cyan]S.[/cyan] Select specific files to search (opens the file selector)')
    console.print('  [cyan]Enter.[/cyan] Skip this search')

    answer = console.input('[bold]Choice: [/bold]').strip().upper()
    if answer == 'A':
        return [_rel(path, root_dir) for path in scanned_files], _ALL_FILES
    if answer == 'S':
        selected = run_file_selector(root_dir, scanned_files)
        if not selected or not selected[0]:
            console.print('[yellow]No files selected. Skipping this search.[/yellow]')
            return [], _UNRESOLVED
        return [_rel(path, root_dir) for path in selected[0]], f'{len(selected[0])} selected file(s)'
    return [], _UNRESOLVED


def _resolve_targets(entry_path, ctx):
    """Returns (rel_paths, scope_label, failure_note) for one search entry."""
    if isinstance(entry_path, str) and entry_path in ctx.folder_scopes:
        label = folder_label(ctx.folder_scopes[entry_path])
        files = ctx.resolved_map.get(entry_path) or []
        if not files:
            return [], label, 'the folder holds no files matching the current scan filters'
        return files, label, ''
    if isinstance(entry_path, str) and entry_path in ctx.resolved_map:
        files = ctx.resolved_map[entry_path]
        if len(files) == 1:
            return files, f"file '{files[0]}'", ''
        return files, f"{len(files)} files matching '{entry_path}'", ''
    files, label = pick_search_targets(entry_path, ctx.scanned_files, ctx.root_dir)
    if not files:
        return [], label, 'the target file could not be resolved, so nothing was searched'
    return files, label, ''


# --- stage 1: evaluate ------------------------------------------------------

def _default_large_search_choice(coverage):
    """Returns the per-file default for an oversized search.

    Once most of the file would be exposed anyway, the whole file is the honest
    answer. Below that, every match as its own block. Truncate is never chosen.
    """
    if coverage >= SEARCH_COVERAGE_WARN_RATIO:
        return 'F'
    return 'A'


def _confirm_large_search(query, rel_path, hit_count, coverage, total_lines):
    """Asks how to handle one oversized file. Returns full/all/truncate/skip."""
    default_key = _default_large_search_choice(coverage)
    options = [
        ('F', 'Include the whole file instead'),
        ('A', 'Include every match as context blocks anyway'),
        ('T', f'Truncate to the first {SEARCH_HIT_WARN_THRESHOLD} matches'),
        ('S', 'Skip this file'),
    ]
    console.print(
        f"\n[bold yellow]Search '{escape(_short_query(query))}' in {escape(rel_path)} "
        f'matched {hit_count} line(s).[/bold yellow]'
    )
    console.print(
        f'With {SEARCH_CONTEXT_LINES} lines of context in both directions this would expose '
        f'{coverage * 100:.0f}% of the file ({total_lines} lines total).'
    )
    for key, label in options:
        suffix = ' [dim](default)[/dim]' if key == default_key else ''
        console.print(f'  [cyan]{key}.[/cyan] {label}{suffix}')

    choices = {'F': 'full', 'A': 'all', 'T': 'truncate', 'S': 'skip'}
    for _ in range(_MAX_PROMPT_ATTEMPTS):
        answer = console.input(f'[bold]Choice (F/A/T/S) \\[{default_key}]: [/bold]').strip().upper()
        if not answer:
            answer = default_key
        if answer in choices:
            return choices[answer]
        console.print('[red]Please enter F, A, T or S, or press Enter for the default.[/red]')
    return 'skip'


def _decide_large_search(query, rel_path, hit_count, coverage, total_lines, confirm):
    """Returns full/all/truncate/skip for an oversized file, asking only when confirm is on."""
    if confirm:
        return _confirm_large_search(query, rel_path, hit_count, coverage, total_lines)
    choice = 'full' if _default_large_search_choice(coverage) == 'F' else 'all'
    outcome = 'including the whole file' if choice == 'full' else 'keeping every match as its own block'
    console.print(
        f'  [dim]auto: {escape(rel_path)} has {hit_count} match(es) exposing '
        f'{coverage * 100:.0f}% of the file; {outcome}.[/dim]'
    )
    return choice


def _stage_hits(query, rel_path, abs_path, content, hits, options):
    """Decides how one file's hits would be sent, and counts their tokens."""
    total_lines = len(content.splitlines()) or 1
    blocks = build_search_blocks(hits, query)
    coverage = partial_coverage_ratio(total_lines, blocks)
    choice = 'all'
    if len(hits) > SEARCH_HIT_WARN_THRESHOLD or coverage >= SEARCH_COVERAGE_WARN_RATIO:
        choice = _decide_large_search(query, rel_path, len(hits), coverage, total_lines, options.confirm)
    staged = {
        'query': query, 'rel_path': rel_path, 'abs_path': abs_path,
        'hits': len(hits), 'blocks': blocks, 'full': False, 'skip': False,
        'truncated': 0, 'note': '', 'tokens': 0, 'estimated': False,
    }
    if choice == 'skip':
        staged.update(skip=True, note='skipped by the user')
        return staged
    if choice == 'full':
        staged.update(full=True, note='the whole file was included instead of context blocks')
        rendered = content
    else:
        if choice == 'truncate':
            kept_hits = hits[:SEARCH_HIT_WARN_THRESHOLD]
            staged.update(truncated=len(hits) - len(kept_hits), blocks=build_search_blocks(kept_hits, query))
        rendered = render_partial_content(content, staged['blocks'])
    staged['tokens'], staged['estimated'] = estimate_tokens(rendered)
    return staged


def _read_searchable(abs_path, rel_path):
    """Returns the file's text, or None when it cannot or should not be searched."""
    try:
        content = safe_read_file(abs_path)
    except OSError as error:
        console.print(f'  [red]Could not read {escape(rel_path)}: {escape(str(error))}[/red]')
        return None
    if not content or content.startswith(_BINARY_MARKER):
        return None
    return content


def _evaluate_file(query, rel_path, flags, ctx, options):
    """Stages one file without touching the context. Returns None when it has no hits.

    Raises ValueError for an invalid regex, which aborts the whole query.
    """
    abs_path = os.path.abspath(os.path.join(ctx.root_dir, rel_path))
    if not os.path.isfile(abs_path) or abs_path in ctx.important_files:
        # A full-file include already supersedes anything a search could add.
        return None
    content = _read_searchable(abs_path, rel_path)
    if content is None:
        return None
    use_regex, case_sensitive = flags
    hits = find_search_hits(content, query, regex=use_regex, case_sensitive=case_sensitive)
    if not hits:
        return None
    return _stage_hits(query, rel_path, abs_path, content, hits, options)


def _evaluate_targets(query, targets, flags, ctx, options):
    """Stages every target file. Raises ValueError for an invalid regex."""
    staged = []
    for rel_path in targets:
        item = _evaluate_file(query, rel_path, flags, ctx, options)
        if item is not None:
            staged.append(item)
    return staged


# --- stage 2: breadth check -------------------------------------------------

def _active(staged):
    return [item for item in staged if not item['skip']]


def _totals(items):
    """Returns (file_count, token_count, any_estimated)."""
    tokens = sum(item['tokens'] for item in items)
    estimated = any(item['estimated'] for item in items)
    return len(items), tokens, estimated


def _format_tokens(tokens, estimated):
    return f"{'~' if estimated else ''}{tokens:,}"


def _over_limit(items, options):
    files, tokens, _ = _totals(items)
    return files >= options.max_files or tokens >= options.max_tokens


def _ask_breadth(query, scope, active, options):
    """Shows the totals and returns accept/narrow/reject, or None when no valid key was typed.

    There is deliberately no default, so a reflexive Enter can never accept.
    """
    files, tokens, estimated = _totals(active)
    console.print(
        f"\n[bold yellow]Search query '{escape(_short_query(query))}' on {escape(scope)} matched "
        f'{files} file(s), totaling {_format_tokens(tokens, estimated)} tokens.[/bold yellow]'
    )
    console.print(
        f'[dim]The limit per query is {options.max_files} file(s) or '
        f'{options.max_tokens:,} tokens, whichever comes first.[/dim]'
    )
    console.print('  [cyan]A.[/cyan] Accept all matched files')
    console.print('  [cyan]S.[/cyan] Narrow the scope (opens the file selector on the matched files)')
    console.print('  [cyan]R.[/cyan] Reject this query')
    choices = {'A': 'accept', 'S': 'narrow', 'R': 'reject'}
    for _ in range(_MAX_PROMPT_ATTEMPTS):
        try:
            answer = console.input('[bold]Choice (A/S/R): [/bold]').strip().upper()
        except EOFError:
            return 'reject'
        if answer in choices:
            return choices[answer]
        console.print('[red]Type A, S or R. This prompt has no default.[/red]')
    return None


def _narrow_with_selector(active, root_dir):
    """Opens the selector on the matched files. Returns the kept items, or None when cancelled."""
    matched = [item['abs_path'] for item in active]
    try:
        selected = run_file_selector(root_dir, matched, preselected_files=matched)
    except Exception as error:
        console.print(f'[red]The file selector failed: {escape(str(error))}[/red]')
        return None
    if selected is None:
        console.print('[yellow]Selection cancelled. Choose again.[/yellow]')
        return None
    kept = set(selected[0] or [])
    return [item for item in active if item['abs_path'] in kept]


def _rejection_note(active):
    files, tokens, estimated = _totals(active)
    if files == 1:
        advice = 'Request only the part you need, with a `functions` request or a more specific query.'
    else:
        advice = 'Retry with a narrower `path` or a more specific query.'
    return (
        f'REJECTED by the user as too broad ({files} file(s), {_format_tokens(tokens, estimated)} tokens). '
        f'Nothing from this search was included. {advice}'
    )


def _narrow_note(original_count, narrowed):
    names = [item['rel_path'] for item in narrowed]
    listed = ', '.join(names[:_MAX_LISTED_FILES])
    extra = len(names) - _MAX_LISTED_FILES
    if extra > 0:
        listed += f', and {extra} more'
    return (
        f'the user NARROWED the scope of this search from {original_count} file(s) to '
        f'{len(names)}: {listed}. Results cover those files only.'
    )


def _narrowed_outcome(active, narrowed, skipped):
    if not narrowed:
        console.print('[yellow]Every file was deselected, so this query was rejected.[/yellow]')
        return [], _rejection_note(active)
    files, tokens, estimated = _totals(narrowed)
    console.print(f'  Narrowed to [cyan]{files}[/cyan] file(s), {_format_tokens(tokens, estimated)} tokens.')
    return narrowed + skipped, _narrow_note(len(active), narrowed)


def check_query_breadth(query, scope, staged, options, root_dir):
    """Lets the user accept, narrow or reject a query that is too broad.

    Returns (kept, scope_note). kept is a subset of staged, and scope_note is
    empty when the query went through unchanged.
    """
    active = _active(staged)
    if not _over_limit(active, options):
        return staged, ''
    skipped = [item for item in staged if item['skip']]
    for _ in range(_MAX_BREADTH_ROUNDS):
        choice = _ask_breadth(query, scope, active, options)
        if choice == 'accept':
            return staged, ''
        if choice == 'reject':
            return [], _rejection_note(active)
        if choice == 'narrow':
            narrowed = _narrow_with_selector(active, root_dir)
            if narrowed is not None:
                return _narrowed_outcome(active, narrowed, skipped)
    console.print('[yellow]No decision was made, so this query was rejected.[/yellow]')
    return [], _rejection_note(active)


# --- stage 3: commit --------------------------------------------------------

def _promote_to_full(abs_path, ctx):
    if abs_path not in ctx.important_files:
        ctx.important_files.append(abs_path)
    ctx.partial_files.pop(abs_path, None)


def _merge_blocks(abs_path, blocks, ctx):
    existing = ctx.partial_files.setdefault(abs_path, [])
    names = {block['name'] for block in existing}
    for block in blocks:
        if block['name'] not in names:
            existing.append(block)


def commit_search_result(item, ctx):
    """Adds one staged file to the context. Returns its report entry."""
    entry = {
        'query': item['query'], 'path': item['rel_path'], 'hits': item['hits'],
        'truncated': item['truncated'], 'note': item['note'], 'kind': 'file',
    }
    if item['skip']:
        return entry
    abs_path = item['abs_path']
    if abs_path not in ctx.found_files:
        ctx.found_files.append(abs_path)
    if item['full']:
        _promote_to_full(abs_path, ctx)
        console.print(f"  Search promoted [cyan]{escape(item['rel_path'])}[/cyan] to a full-file include.")
        return entry
    _merge_blocks(abs_path, item['blocks'], ctx)
    console.print(
        f"  Search '[yellow]{escape(_short_query(item['query']))}[/yellow]' in [cyan]{escape(item['rel_path'])}[/cyan]: "
        f"{item['hits']} hit(s) with {SEARCH_CONTEXT_LINES} lines of context each side."
    )
    return entry


# --- orchestration ----------------------------------------------------------

def _run_query(entry, query, ctx, options):
    """Runs one search entry through all three stages. Returns its report entries."""
    entry_path = entry.get('path')
    targets, scope, failure = _resolve_targets(entry_path, ctx)
    if failure:
        return [_report(query, entry_path or scope, failure)]
    flags = (bool(entry.get('regex', False)), entry.get('case_sensitive', True) is not False)
    try:
        staged = _evaluate_targets(query, targets, flags, ctx, options)
    except ValueError as error:
        console.print(f'  [red]{escape(str(error))}[/red]')
        return [_report(query, scope, str(error))]
    if len(targets) > 1:
        console.print(
            f"  [dim]Searched {len(targets)} file(s) for '{escape(_short_query(query))}'; "
            f'{len(staged)} contained a match.[/dim]'
        )
    if not staged:
        where = scope if len(targets) == 1 else f'{scope} ({len(targets)} file(s) searched)'
        return [_report(query, where, 'no matches found')]
    kept, scope_note = check_query_breadth(query, scope, staged, options, ctx.root_dir)
    report = []
    if scope_note:
        total_hits = sum(item['hits'] for item in staged)
        report.append(_report(query, scope, scope_note, hits=total_hits, kind='scope'))
    report.extend(commit_search_result(item, ctx) for item in kept)
    return report


def resolve_search_requests(search_list, ctx, options=None):
    """Runs every SELECT search entry, returning a report for the model."""
    if not isinstance(ctx, SearchContext):
        raise TypeError('resolve_search_requests needs a SearchContext.')
    options = options or SearchOptions()
    report = []
    for entry in search_list or []:
        if not isinstance(entry, dict):
            continue
        query = entry.get('query') or entry.get('string') or ''
        if not isinstance(query, str) or not query:
            console.print("[yellow]Warning:[/yellow] A search entry had no 'query' and was skipped.")
            continue
        report.extend(_run_query(entry, query, ctx, options))
    return report


# --- model-facing note ------------------------------------------------------

def _note_line(entry):
    if entry.get('kind') == 'scope':
        return f"- '{entry['query']}' on {entry['path']}: {entry['note']}"
    line = f"- '{entry['query']}' in {entry['path']}: {entry['hits']} match(es)"
    if entry.get('truncated'):
        return line + (
            f", TRUNCATED. {entry['truncated']} further match(es) were withheld by the user. "
            'Narrow your query and search again if you need the rest.'
        )
    if entry.get('note'):
        return line + f" ({entry['note']})"
    return line + f', shown with {SEARCH_CONTEXT_LINES} lines of context on each side.'


def build_search_note(search_report):
    """Formats the search outcome so the model knows what was and was not found."""
    if not search_report:
        return ''
    lines = ['\n--- SYSTEM NOTE: SEARCH RESULTS ---']
    lines.extend(_note_line(entry) for entry in search_report)
    lines.append('Matches whose context windows overlapped were merged into a single block, '
                 'so the block count may be lower than the match count.')
    lines.append('')
    return '\n'.join(lines)
