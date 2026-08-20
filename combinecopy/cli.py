import os
import argparse
import math
import time
import threading
import sys
import tempfile
import zipfile
import atexit
import shutil
import random
import re
import subprocess
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.rule import Rule

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

# Ensure the root directory is in sys.path so 'combinecopy' can be imported
# regardless of where the script is executed from.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from combinecopy.utils import (
    console,
    safe_read_file,
    get_files_recursive,
    generate_tree_string,
    print_auto_summary,
    display_summary,
    copy_to_clipboard,
    copy_file_to_clipboard,
    render_partial_content,
    find_search_hits,
    build_search_blocks,
    partial_coverage_ratio,
    SEARCH_CONTEXT_LINES
)

from combinecopy.prompts import (
    get_system_prompt,
    build_prompt,
    get_user_prompt,
    get_ast,
    get_file_context,
    get_system_prompt_important,
    get_git_diff
)
from combinecopy.tui.selection import run_file_selector
from combinecopy.tui.prompt import SystemPromptApp
from combinecopy.tui.confirm import ConfirmCopyApp
from combinecopy.tui.apply import AutoAgentApp, OrchestratorAgentApp

from combinecopy.mobile.env import is_termux
from combinecopy.mobile.inbox import ensure_inbox_dir
from combinecopy.mobile.provision import run_doctor, install_url_opener

# Beyond this many hits, or this much of the file exposed, we stop and let the
# user decide rather than silently dumping most of a file into the context.
SEARCH_HIT_WARN_THRESHOLD = 25
SEARCH_COVERAGE_WARN_RATIO = 0.8


def _pick_search_targets(entry_path, resolved_map, scanned_files, root_dir):
    """Resolves a search entry to relative paths, asking the user when it cannot."""
    if entry_path and entry_path in resolved_map:
        return resolved_map[entry_path]

    label = f"'{entry_path}'" if entry_path else "(no path was provided)"
    console.print(f"\n[bold yellow]Search target {label} could not be resolved.[/bold yellow]")
    console.print(f"The current workspace scan holds [cyan]{len(scanned_files)}[/cyan] file(s).")
    console.print("  [cyan]A.[/cyan] Search every file in the scanned workspace")
    console.print("  [cyan]S.[/cyan] Select specific files to search (opens the file selector)")
    console.print("  [cyan]Enter.[/cyan] Skip this search")

    ans = console.input("[bold]Choice: [/bold]").strip().upper()
    if ans == 'A':
        return [os.path.relpath(f, root_dir).replace("\\", "/") for f in scanned_files]
    if ans == 'S':
        selected = run_file_selector(root_dir, scanned_files)
        if not selected or not selected[0]:
            console.print("[yellow]No files selected. Skipping this search.[/yellow]")
            return []
        return [os.path.relpath(f, root_dir).replace("\\", "/") for f in selected[0]]
    return []


def _confirm_large_search(query, rel_path, hit_count, coverage, total_lines):
    """Asks how to handle an oversized search. Returns full/all/truncate/skip."""
    console.print(f"\n[bold yellow]Search '{query}' in {rel_path} matched {hit_count} line(s).[/bold yellow]")
    console.print(
        f"With {SEARCH_CONTEXT_LINES} lines of context in both directions this would expose "
        f"{coverage * 100:.0f}% of the file ({total_lines} lines total)."
    )
    console.print("  [cyan]F.[/cyan] Include the whole file instead")
    console.print("  [cyan]A.[/cyan] Include every match as context blocks anyway")
    console.print(f"  [cyan]T.[/cyan] Truncate to the first {SEARCH_HIT_WARN_THRESHOLD} matches")
    console.print("  [cyan]S.[/cyan] Skip this search entirely")

    choices = {'F': 'full', 'A': 'all', 'T': 'truncate', 'S': 'skip'}
    for _ in range(5):
        ans = console.input("[bold]Choice (F/A/T/S): [/bold]").strip().upper()
        if ans in choices:
            return choices[ans]
        console.print("[red]Please enter F, A, T or S.[/red]")
    return 'skip'


def _run_single_search(query, rel_path, root_dir, use_regex, case_sensitive,
                       found_files_final, important_files, partial_files):
    """Searches one file and folds any hits into partial_files. Returns a report entry."""
    abs_path = os.path.abspath(os.path.join(root_dir, rel_path))
    if not os.path.isfile(abs_path):
        return None
    if abs_path in important_files:
        # A full-file include already supersedes anything a blob could add.
        return None

    try:
        content = safe_read_file(abs_path)
    except Exception as e:
        console.print(f"  [red]Could not read {rel_path}: {e}[/red]")
        return None
    if not content or content.startswith("(This is a binary"):
        return None

    hits = find_search_hits(content, query, regex=use_regex, case_sensitive=case_sensitive)
    if not hits:
        return None

    total_lines = len(content.splitlines()) or 1
    blocks = build_search_blocks(hits, query)
    coverage = partial_coverage_ratio(total_lines, blocks)
    truncated = 0

    if len(hits) > SEARCH_HIT_WARN_THRESHOLD or coverage >= SEARCH_COVERAGE_WARN_RATIO:
        choice = _confirm_large_search(query, rel_path, len(hits), coverage, total_lines)
        if choice == 'skip':
            return {"query": query, "path": rel_path, "hits": len(hits),
                    "truncated": 0, "note": "skipped by the user"}
        if choice == 'full':
            if abs_path not in found_files_final:
                found_files_final.append(abs_path)
            if abs_path not in important_files:
                important_files.append(abs_path)
            partial_files.pop(abs_path, None)
            console.print(f"  Search promoted [cyan]{rel_path}[/cyan] to a full-file include.")
            return {"query": query, "path": rel_path, "hits": len(hits),
                    "truncated": 0, "note": "the whole file was included instead of context blocks"}
        if choice == 'truncate':
            truncated = len(hits) - SEARCH_HIT_WARN_THRESHOLD
            hits = hits[:SEARCH_HIT_WARN_THRESHOLD]
            blocks = build_search_blocks(hits, query)

    if abs_path not in found_files_final:
        found_files_final.append(abs_path)
    existing = partial_files.setdefault(abs_path, [])
    existing_names = {b["name"] for b in existing}
    for b in blocks:
        if b["name"] not in existing_names:
            existing.append(b)

    console.print(
        f"  Search '[yellow]{query}[/yellow]' in [cyan]{rel_path}[/cyan]: "
        f"{len(hits)} hit(s) with {SEARCH_CONTEXT_LINES} lines of context each side."
    )
    return {"query": query, "path": rel_path, "hits": len(hits),
            "truncated": truncated, "note": ""}


def resolve_search_requests(search_list, resolved_map, scanned_files, root_dir,
                            found_files_final, important_files, partial_files):
    """Runs every SELECT search entry, returning a report for the model."""
    report = []
    if not search_list:
        return report

    for entry in search_list:
        if not isinstance(entry, dict):
            continue
        query = entry.get("query") or entry.get("string") or ""
        if not query:
            console.print("[yellow]Warning:[/yellow] A search entry had no 'query' and was skipped.")
            continue

        use_regex = bool(entry.get("regex", False))
        case_sensitive = entry.get("case_sensitive", True) is not False

        targets = _pick_search_targets(entry.get("path"), resolved_map, scanned_files, root_dir)
        if not targets:
            report.append({"query": query, "path": entry.get("path") or "(unresolved)",
                           "hits": 0, "truncated": 0,
                           "note": "the target file could not be resolved, so nothing was searched"})
            continue

        searched = 0
        matched = 0
        aborted = False
        for rel_path in targets:
            try:
                result = _run_single_search(
                    query, rel_path, root_dir, use_regex, case_sensitive,
                    found_files_final, important_files, partial_files
                )
            except ValueError as e:
                console.print(f"  [red]{e}[/red]")
                report.append({"query": query, "path": entry.get("path") or "(multiple)",
                               "hits": 0, "truncated": 0, "note": str(e)})
                aborted = True
                break
            searched += 1
            if result:
                matched += 1
                report.append(result)

        if aborted:
            continue
        if len(targets) > 1:
            console.print(
                f"  [dim]Searched {searched} file(s) for '{query}'; "
                f"{matched} contained a match.[/dim]"
            )
        if matched == 0:
            where = entry.get("path") or f"{searched} scanned file(s)"
            report.append({"query": query, "path": where, "hits": 0,
                           "truncated": 0, "note": "no matches found"})
    return report


def build_search_note(search_report):
    """Formats the search outcome so the model knows what was and was not found."""
    if not search_report:
        return ""
    lines = ["\n--- SYSTEM NOTE: SEARCH RESULTS ---"]
    for r in search_report:
        line = f"- '{r['query']}' in {r['path']}: {r['hits']} match(es)"
        if r.get("truncated"):
            line += (f", TRUNCATED. {r['truncated']} further match(es) were withheld by the user. "
                     f"Narrow your query and search again if you need the rest.")
        elif r.get("note"):
            line += f" ({r['note']})"
        else:
            line += f", shown with {SEARCH_CONTEXT_LINES} lines of context on each side."
        lines.append(line)
    lines.append("Matches whose context windows overlapped were merged into a single block, "
                 "so the block count may be lower than the match count.")
    lines.append("")
    return "\n".join(lines)


def resolve_selection_payload(selection_data, root_dir, max_depth, ext_filters, exclude_dirs):
    found_files = []
    important_files = []
    partial_files = {}

    from combinecopy.utils import prime_ast_cache, get_cached_blocks, resolve_paths

    with console.status("[bold green]Scanning directory structure...[/bold green]", spinner="dots"):
        scanned_files = get_files_recursive(root_dir, 0, max_depth, ext_filters, exclude_dirs=exclude_dirs)

    prime_ast_cache(root_dir, scanned_files)
    full_files_list = selection_data.get("files", [])
    functions_list = selection_data.get("functions", [])
    search_list = selection_data.get("search", []) or []

    req_paths = set(full_files_list)
    for entry in functions_list:
        if entry.get("path"):
            req_paths.add(entry.get("path"))
    # Search targets go through the exact same resolver as everything else, so
    # they inherit suffix/basename matching and the ambiguity prompt for free.
    for entry in search_list:
        if isinstance(entry, dict) and entry.get("path"):
            req_paths.add(entry.get("path"))
    resolved_map, ambiguous_map, missing_list = resolve_paths(req_paths, scanned_files, root_dir)
    resolved_map = {k: [v] for k, v in resolved_map.items()}

    if ambiguous_map:
        console.print("\n[bold yellow]Ambiguous File Resolution:[/bold yellow]")
        for req, options in ambiguous_map.items():
            console.print(f"\n[bold yellow]File '{req}' is ambiguous.[/bold yellow]")
            console.print(f"It matches {len(options)} file(s) in the workspace:")
            for i, p in enumerate(options):
                console.print(f"  [cyan]{i + 1}.[/cyan] {p}")

            while True:
                ans = console.input("[bold]Select files to include (A for All, comma-separated numbers like 1,3, or Enter to skip): [/bold]").strip().upper()
                if not ans:
                    break
                if ans == 'A':
                    resolved_map[req] = options
                    break
                else:
                    try:
                        selected_indices = [int(x.strip()) - 1 for x in ans.split(',')]
                        valid = True
                        selected_paths = []
                        for idx in selected_indices:
                            if 0 <= idx < len(options):
                                selected_paths.append(options[idx])
                            else:
                                console.print(f"[red]Invalid number: {idx + 1}[/red]")
                                valid = False
                                break
                        if valid:
                            if selected_paths:
                                resolved_map[req] = selected_paths
                            break
                    except ValueError:
                        console.print("[red]Invalid input. Please enter 'A' or comma-separated numbers.[/red]")

    if missing_list:
        console.print("\n[bold yellow]Missing Files:[/bold yellow]")
        for req in missing_list:
            console.print(f"  [red]Missing:[/red] {req} (Not found in workspace, skipping)")
            
        ans = console.input("\n[bold yellow]Continue without missing files? [Y/n]: [/bold yellow]").strip().lower()
        if ans in ['n', 'no']:
            console.print("[bold yellow]Operation cancelled by user.[/bold yellow]")
            return None, None, None, None, None

    missing_files_warnings = [p for p in req_paths if p not in resolved_map]
    if missing_files_warnings:
        console.print(f"[bold yellow]Warning: {len(missing_files_warnings)} requested files could not be resolved and will be skipped.[/bold yellow]")

    found_files_final = []
    for f in full_files_list:
        if f in resolved_map:
            for rel_path in resolved_map[f]:
                abs_path = os.path.abspath(os.path.join(root_dir, rel_path))
                if abs_path not in found_files_final:
                    found_files_final.append(abs_path)
                if abs_path not in important_files:
                    important_files.append(abs_path)
                console.print(f"  Selected full file: [cyan]{rel_path}[/cyan] (Resolved from {f})")

    missing_funcs_to_search = []
    for entry in functions_list:
        fpath = entry.get("path")
        names = entry.get("names", [])

        if fpath in resolved_map:
            for rel_path in resolved_map[fpath]:
                abs_path = os.path.abspath(os.path.join(root_dir, rel_path))

                blocks = get_cached_blocks(abs_path, root_dir)
                found_names = []
                for name in names:
                    found_block = False
                    for b in blocks:
                        if name in b["name"]:
                            if abs_path not in partial_files:
                                partial_files[abs_path] = []
                            if b not in partial_files[abs_path]:
                                partial_files[abs_path].append(b)
                            found_block = True
                    if found_block:
                        found_names.append(name)
                    else:
                        missing_funcs_to_search.append(name)

                if found_names:
                    if abs_path not in found_files_final:
                        found_files_final.append(abs_path)
                    console.print(f"  Selected functions from [cyan]{rel_path}[/cyan]: {', '.join(found_names)}")
        else:
            missing_funcs_to_search.extend(names)

    missing_funcs_to_search = list(dict.fromkeys(missing_funcs_to_search))

    if missing_funcs_to_search:
        from combinecopy.utils import search_ast_for_functions, get_blocks_by_name
        candidate_map = search_ast_for_functions(missing_funcs_to_search, root_dir)

        ambiguous_funcs = {k: v for k, v in candidate_map.items() if v}
        unfound_funcs = [k for k in missing_funcs_to_search if not candidate_map.get(k)]

        if ambiguous_funcs:
            func_resolutions = {k: [] for k in ambiguous_funcs}
            for fname, paths in ambiguous_funcs.items():
                console.print(f"\n[bold yellow]Function/Class '{fname}' not found in target file.[/bold yellow]")
                console.print(f"It was found in {len(paths)} other file(s):")
                for i, p in enumerate(paths):
                    console.print(f"  [cyan]{i + 1}.[/cyan] {p}")

                while True:
                    ans = console.input("[bold]Select files to include (A for All, comma-separated numbers like 1,3, or Enter to skip): [/bold]").strip().upper()
                    if not ans:
                        break
                    if ans == 'A':
                        func_resolutions[fname] = paths
                        break
                    else:
                        try:
                            selected_indices = [int(x.strip()) - 1 for x in ans.split(',')]
                            valid = True
                            selected_paths = []
                            for idx in selected_indices:
                                if 0 <= idx < len(paths):
                                    selected_paths.append(paths[idx])
                                else:
                                    console.print(f"[red]Invalid number: {idx + 1}[/red]")
                                    valid = False
                                    break
                            if valid:
                                func_resolutions[fname] = selected_paths
                                break
                        except ValueError:
                            console.print("[red]Invalid input. Please enter 'A' or comma-separated numbers.[/red]")

            for fname, selected_paths in func_resolutions.items():
                if not selected_paths:
                    continue
                for spath in selected_paths:
                    abs_p = os.path.abspath(os.path.join(root_dir, spath))
                    blocks = get_blocks_by_name(abs_p, root_dir, fname)
                    if blocks:
                        if abs_p not in found_files_final:
                            found_files_final.append(abs_p)
                        if abs_p not in partial_files:
                            partial_files[abs_p] = []
                        existing_names = [b["name"] for b in partial_files[abs_p]]
                        for b in blocks:
                            if b["name"] not in existing_names:
                                partial_files[abs_p].append(b)
                                console.print(f"  Resolved and selected function [cyan]{b['name']}[/cyan] in [cyan]{spath}[/cyan]")
        for uf in unfound_funcs:
            console.print(f"  [yellow]Warning:[/yellow] Function/Class '[red]{uf}[/red]' could not be found anywhere in the workspace.")

    search_report = resolve_search_requests(
        search_list, resolved_map, scanned_files, root_dir,
        found_files_final, important_files, partial_files
    )

    return found_files_final, important_files, partial_files, missing_files_warnings, search_report

def manage_tasks_cli(tasks_data, root_dir, max_depth, ext_filters, exclude_dirs, args, custom_rules):
    import json
    tasks_file = os.path.join(root_dir, ".cc_tasks.json")

    while True:
        console.print(f"\n[bold cyan]Mega Task:[/bold cyan] {tasks_data.get('mega_task_name', 'Unnamed Task')}")
        console.print("[bold]Sub-Tasks:[/bold]")

        tasks = tasks_data.get("tasks", [])
        first_incomplete = -1
        for i, t in enumerate(tasks):
            status = "[green][x][/green]" if t.get("completed") else "[yellow][ ][/yellow]"
            if not t.get("completed") and first_incomplete == -1:
                first_incomplete = i
            console.print(f"  {i+1}. {status} {t.get('task_name', 'Unnamed')}")

        console.print("\n[dim]Enter a number to select a task, 'c <num>' to toggle completion, or 'q' to quit.[/dim]")
        ans = console.input("[bold]Choice:[/bold] ").strip().lower()

        if not ans:
            if first_incomplete != -1:
                ans = str(first_incomplete + 1)
            else:
                console.print("[yellow]All tasks completed. Exiting.[/yellow]")
                break

        if ans == 'q':
            break

        if ans.startswith('c '):
            try:
                idx = int(ans.split(' ')[1]) - 1
                if 0 <= idx < len(tasks):
                    tasks[idx]["completed"] = not tasks[idx].get("completed", False)
                    with open(tasks_file, "w", encoding='utf-8') as f:
                        json.dump(tasks_data, f, indent=4)
                else:
                    console.print("[red]Invalid task number.[/red]")
            except:
                console.print("[red]Invalid format. Use 'c <number>'.[/red]")
            continue
        try:
            idx = int(ans) - 1
            if 0 <= idx < len(tasks):
                selected_task = tasks[idx]

                console.print(f"\n[bold cyan]Task Sub-Prompt:[/bold cyan]\n{selected_task.get('sub_prompt', 'No prompt provided.')}")
                
                console.print("\n[bold cyan]Requested Files:[/bold cyan]")
                for f in selected_task.get("files", []):
                    console.print(f"  - [green]{f}[/green]")
                for func in selected_task.get("functions", []):
                    console.print(f"  - [yellow]{func.get('path')} -> {', '.join(func.get('names', []))}[/yellow]")
                    
                ans_mod = console.input("\n[bold yellow]Modify requested files? \\[y/N]: [/bold yellow]").strip().lower()

                sel_data = {
                    "files": selected_task.get("files", []),
                    "functions": selected_task.get("functions", [])
                }
                res = resolve_selection_payload(sel_data, root_dir, max_depth, ext_filters, exclude_dirs)
                if res[0] is None:
                    continue
                found_files, imp_files, part_files, missing, task_search_report = res
                
                if ans_mod in ['y', 'yes']:
                    from combinecopy.tui.selection import run_file_selector
                    from combinecopy.utils import get_files_recursive
                    with console.status("[bold green]Scanning directory structure...[/bold green]", spinner="dots"):
                        scanned_files = get_files_recursive(root_dir, 0, max_depth, ext_filters, exclude_dirs=exclude_dirs)
                    selected = run_file_selector(root_dir, scanned_files, ast_mode=True, preselected_files=found_files, preselected_partials=part_files)
                    if selected is None:
                        console.print("[bold yellow]Modification cancelled.[/bold yellow]")
                        continue
                    found_files, imp_files, part_files = selected

                agent_type = "cli" if getattr(args, 'cli', False) else "default"
                sys_prompt_text = get_system_prompt(agent_type=agent_type, file_cull=True, xml_mode=args.xml, consult=args.consult, custom_rules=custom_rules, rehab=args.rehab, divide=False)
                file_context_buffer = []
                separator = "-" * 35
                important_set = set(imp_files) if imp_files is not None else set()
                for file_path in found_files:
                    rel_path = os.path.relpath(file_path, root_dir)
                    is_important = file_path in important_set
                    is_partial = file_path in part_files and not is_important

                    # Files that are neither important (FULL) nor partial are
                    # represented only via the AST map, not the full context.
                    if not is_important and not is_partial:
                        continue

                    _, ext = os.path.splitext(rel_path)
                    lang = ext.lstrip('.').lower()
                    file_context_buffer.append(separator)
                    file_context_buffer.append(f"FILE: {rel_path}")
                    file_context_buffer.append(separator)
                    file_context_buffer.append(f"```{lang}")
                    try:
                        content = safe_read_file(file_path)
                        if is_partial:
                            file_context_buffer.append(render_partial_content(content, part_files[file_path]))
                        else:
                            file_context_buffer.append(content)
                    except Exception as e:
                        file_context_buffer.append(f"[Error reading file: {e}]")
                    file_context_buffer.append("```")
                    file_context_buffer.append("\n")
                if missing:
                    file_context_buffer.append("\n--- SYSTEM NOTE: MISSING FILES ---")
                    file_context_buffer.append("The following files were requested but could not be found or resolved:")
                    for mfw in missing:
                        file_context_buffer.append(f"- {mfw}")
                    file_context_buffer.append("")

                search_note = build_search_note(task_search_report)
                if search_note:
                    file_context_buffer.append(search_note)
                    
                file_context_buffer.append("\n--- SYSTEM NOTE: CONTEXT PRUNING ---")
                from combinecopy.prompts import get_prune
                file_context_buffer.append(get_prune(args.xml))
                file_context_buffer.append("")

                file_context_str = "\n".join(file_context_buffer)
                ast_map_str = generate_tree_string(found_files, root_dir)

                full_text = build_prompt(
                    user_request=selected_task.get("sub_prompt", ""),
                    file_context=file_context_str,
                    ast_map=ast_map_str,
                    file_cull=True,
                    system_prompt=sys_prompt_text,
                    agent_type=agent_type,
                    xml_mode=args.xml,
                    consult=args.consult,
                    custom_rules=custom_rules,
                    git_diff="",
                    rehab=args.rehab,
                    divide=False
                )

                copy_to_clipboard(full_text)
                console.print(f"[bold green]Payload for task '{selected_task.get('task_name')}' copied to clipboard![/bold green]")
                break
            else:
                console.print("[red]Invalid task number.[/red]")
        except ValueError:
            console.print("[red]Invalid input.[/red]")

def resolve_random_paths(paths: list[str]) -> list[str]:
    resolved = []
    pattern = re.compile(r'\$\{r\((\d+),\s*(\d+)\)\}')
    for p in paths:
        match = pattern.search(p)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start > end:
                start, end = end, start
            
            candidates = []
            for i in range(start, end + 1):
                test_path = p.replace(match.group(0), str(i), 1)
                if os.path.exists(test_path):
                    candidates.append(test_path)
            
            if not candidates:
                console.print(Panel(f"No existing files found for range pattern in:\n{p}", title="Error", style="bold red"))
                sys.exit(1)
            
            selected = random.choice(candidates)
            console.print(f"[bold cyan]Randomly selected:[/bold cyan] {selected} [dim](from {len(candidates)} candidates)[/dim]")
            resolved.append(selected)
        else:
            resolved.append(p)
    return resolved

def main():
    parser = argparse.ArgumentParser(description="Scan folder and combine file contents to clipboard.")
    parser.add_argument("-l", "--limit", type=int, default=100, help="Max recursion depth")
    parser.add_argument("paths", nargs='*', help="Specific files or directories to include (bypasses full directory scan)")
    parser.add_argument("-f", "--file_types", nargs='+', default=None, help="File extension filters (separated by space)")
    parser.add_argument("-b", "--batches", type=int, default=1, help="Number of batches")
    parser.add_argument("-e", "--exclude", nargs='+', default=None, help="Directory names to exclude from scan (separated by space)")
    parser.add_argument("-s", "--select", action="store_true", help="Open TUI to pick files interactively")
    parser.add_argument("-a", "--auto", action="store_true", help="Run in continuous AI listener mode")
    parser.add_argument("--rehab", action="store_true", help="Enable Rehab Mode to manually type AI suggestions with Meld verification.")
    parser.add_argument("-r", "--revert", action="store_true", help="Run in continuous AI listener mode but reverse all changes")
    parser.add_argument("-o", "--orchestrate", action="store_true", help="Run in orchestrator mode to generate a precise execution plan and prompt.")
    parser.add_argument("--cli", action="store_true", help="Enable CLI Mode. Allows the AI to output terminal commands to be executed.")
    parser.add_argument("--web", action="store_true", help="Launch the local web UI server.")
    parser.add_argument("--web-apply", action="store_true", dest="web_apply", help="Enable web macro mode. Translates applies into simulated keyboard strokes for web IDEs.")
    parser.add_argument("--tfs", action="store_true", help="Use TFVC (tf.exe) instead of git for checkout and checkin operations.")
    parser.add_argument("--system", nargs='?', const='DEFAULT', default=None, help="Inject system prompt and user instructions. Optionally provide a path to a custom system prompt file.")
    parser.add_argument("--system-only", action="store_true", help="Copy only the system prompt to the clipboard and exit.")
    parser.add_argument("--file", action="store_true", help="Save prompt to a temp file and copy the file to clipboard")
    parser.add_argument("--file-culling", "--file-cull", action="store_true", dest="file_culling", help="Enable file culling / AST selection mode")
    parser.add_argument("-js", "--json-select", action="store_true", help="Parse a JSON selection payload from clipboard to automatically select files/functions")
    parser.add_argument("-x", "--xml", action="store_true", help="Instruct the AI to use XML for payloads instead of JSON to completely avoid quote escaping issues.")
    parser.add_argument("--consult", action="store_true", help="Enable CONSULT phase for the AI to ask abstract questions to an external LLM.")
    parser.add_argument("-d", "--diff", action="store_true", help="Inject current uncommitted git diff directly into the prompt context.")
    parser.add_argument("--divide", action="store_true", help="Enable Large Task Mode to divide complex requests into sub-tasks.")
    parser.add_argument("-m", "--mobile", action="store_true", help="Enable Mobile (Termux) mode. Ingests payloads via the TUI paste buffer or an editor instead of polling the clipboard.")
    parser.add_argument("--no-mobile", action="store_true", help="Disable the automatic Termux detection that would otherwise turn mobile mode on.")
    parser.add_argument("--mobile-doctor", action="store_true", help="Run environment checks for Termux mobile mode and exit.")
    parser.add_argument("--install-url-opener", action="store_true", help="Install the Termux share-sheet hook that drops shared text into ~/.cc_inbox, then exit.")
    parser.add_argument("--force", action="store_true", help="Allow destructive provisioning steps, e.g. overwriting an existing termux-url-opener.")
    args = parser.parse_args()

    # Termux gets mobile mode by default; --no-mobile is the escape hatch.
    if args.no_mobile:
        args.mobile = False
    elif is_termux():
        args.mobile = True
    if args.mobile_doctor:
        run_doctor(console)
        return

    if args.install_url_opener:
        install_url_opener(console, force=args.force)
        return

    if args.file_culling and not (args.system_only or args.json_select):
        args.select = True

    if args.mobile:
        ensure_inbox_dir()
        console.print(
            "[bold cyan]Mobile mode:[/bold cyan] clipboard polling disabled. "
            "Press [bold]v[/bold] to paste, [bold]V[/bold] for the editor, "
            "[bold]r[/bold] to pull from ~/.cc_inbox."
        )

    custom_rules = ""
    ccrules_path = os.path.join(os.getcwd(), '.ccrules')
    if os.path.exists(ccrules_path):
        try:
            with open(ccrules_path, 'r', encoding='utf-8') as f:
                custom_rules = f.read().strip()
        except Exception as e:
            console.print(f"[dim yellow]Warning: Could not read .ccrules file: {e}[/dim yellow]")
    if args.system_only:
        agent_type = "orchestrator" if args.orchestrate else "cli" if args.cli else "default"
        sys_prompt = get_system_prompt(agent_type=agent_type, file_cull=args.file_culling, xml_mode=args.xml, consult=args.consult, custom_rules=custom_rules, rehab=args.rehab, divide=args.divide)
        important = get_system_prompt_important(agent_type=agent_type, xml_mode=args.xml, divide=args.divide)
        
        full_sys_prompt = f"--- SYSTEM INSTRUCTIONS ---\n{sys_prompt}\n\n{important}"
        
        if args.file:
            try:
                fd, temp_path = tempfile.mkstemp(prefix="combineCopy_sysprompt_", suffix=".txt", text=True)
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(full_sys_prompt)
                if copy_file_to_clipboard(temp_path):
                    console.print(Panel(f"[bold green]System prompt saved to {temp_path} and copied to clipboard![/bold green]", title="Success"))
            except Exception as e:
                console.print(f"[bold red]Failed to save/copy file:[/bold red] {e}")
        else:
            if copy_to_clipboard(full_sys_prompt):
                console.print(Panel("[bold green]System prompt copied to clipboard![/bold green]", title="Success"))
            else:
                console.print(full_sys_prompt)
        return

    if args.paths:
        args.paths = resolve_random_paths(args.paths)

    root_dir = os.getcwd()
    max_depth = args.limit
    batch_count = args.batches
    zip_path_to_cleanup = None

    if args.web_apply and not KEYBOARD_AVAILABLE:
        console.print("[bold red]Error:[/bold red] The '--web-apply' flag requires the 'keyboard' module.")
        console.print("Please install it using: [cyan]pip install keyboard[/cyan]")
        sys.exit(1)

    if args.paths and len(args.paths) == 1 and args.paths[0].lower().endswith('.zip'):
        zip_path_to_cleanup = os.path.abspath(args.paths[0])
        temp_dir = tempfile.mkdtemp(prefix="combineCopy_zip_")
        atexit.register(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        
        console.print(f"[bold cyan]Extracting {args.paths[0]} to temporary directory...[/bold cyan]")
        try:
            with zipfile.ZipFile(args.paths[0], 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            extracted_items = os.listdir(temp_dir)
            if len(extracted_items) == 1 and os.path.isdir(os.path.join(temp_dir, extracted_items[0])):
                root_dir = os.path.join(temp_dir, extracted_items[0])
            else:
                root_dir = temp_dir
            args.paths = []
        except Exception as e:
            console.print(f"[bold red]Failed to extract zip file: {e}[/bold red]")
            sys.exit(1)
    ext_filters = args.file_types
    if ext_filters:
        normalized_exts = []
        for ext in ext_filters:
            if not ext.startswith("."):
                normalized_exts.append(f".{ext.lower()}")
            else:
                normalized_exts.append(ext.lower())
        ext_filters = normalized_exts

    if args.divide:
        tasks_file = os.path.join(root_dir, ".cc_tasks.json")
        if os.path.exists(tasks_file):
            try:
                import json
                with open(tasks_file, 'r', encoding='utf-8') as f:
                    tasks_data = json.load(f)
                if tasks_data and "tasks" in tasks_data:
                    uncompleted = [t for t in tasks_data.get("tasks", []) if not t.get("completed")]
                    if uncompleted:
                        ans = console.input(f"[bold yellow]Found unfinished Mega Task '{tasks_data.get('mega_task_name', 'Unnamed')}'. Resume? [Y/n]: [/bold yellow]").strip().lower()
                        if ans in ['', 'y', 'yes']:
                            manage_tasks_cli(tasks_data, root_dir, max_depth, ext_filters, args.exclude, args, custom_rules)
                            return
            except Exception as e:
                console.print(f"[dim yellow]Warning: Failed to read .cc_tasks.json: {e}[/dim yellow]")

    all_known_files = []

    if (args.auto or args.revert or args.orchestrate) and not (args.select or args.file_types or args.paths or args.system is not None or args.cli):
        if args.orchestrate:
            app = OrchestratorAgentApp(root_dir, use_file_clipboard=args.file, cli_mode=args.cli, xml_mode=args.xml)
            result = app.run()
            if result:
                console.print(Panel("Orchestrator payload successfully copied to clipboard.", title="Success", style="bold green"))
            return
        else:
            # NOTE: this previously passed web_mode=args.web, which meant --web
            # accidentally enabled keyboard macro mode. It should track --web-apply,
            # matching the other AutoAgentApp construction site below.
            app = AutoAgentApp(root_dir, revert_mode=args.revert, web_mode=args.web_apply, tfs_mode=args.tfs, xml_mode=args.xml, consult_mode=args.consult, rehab_mode=args.rehab, mobile_mode=args.mobile)
            result = app.run()
            if isinstance(result, dict) and result.get("type") == "task_division":
                data = result.get("data")
                tasks_file = os.path.join(root_dir, ".cc_tasks.json")
                import json
                with open(tasks_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4)
                console.print("\n[bold green]Task split payload intercepted![/bold green]")
                manage_tasks_cli(data, root_dir, max_depth, ext_filters, args.exclude, args, custom_rules)
                return
            elif result:
                print_auto_summary(result)
            return

    if args.web:
        from combinecopy.web.server import start_server
        console.print("\n[bold green]Starting CombineCopy Web UI...[/bold green]")
        console.print("Access it in your browser at: [bold cyan]http://127.0.0.1:5000[/bold cyan]\n")
        start_server(root_dir, max_depth, ext_filters, args.exclude)
        return
    separator = "-" * 35

    try:
        console.print(Rule("[bold blue]CombineCopy Tool[/bold blue]"))
        found_files = []
        important_files = None
        partial_files = {}
        missing_files_warnings = []
        search_report = []

        if args.json_select:
            console.print("[bold cyan]Phase: Selection Parsing[/bold cyan]")
            import pyperclip
            from combinecopy.utils import extract_json_from_text, intelligent_json_fix, extract_xml_from_text, parse_xml_to_dict
            try:
                clipboard_content = pyperclip.paste().strip()
            except Exception as e:
                console.print(f"[bold red]Error reading clipboard:[/bold red] {e}")
                return
            
            if not clipboard_content:
                console.print("[bold red]Clipboard is empty.[/bold red]")
                return

            selection_data = None
            # First check for XML
            xml_results = extract_xml_from_text(clipboard_content)
            for xml_str in xml_results:
                data = parse_xml_to_dict(xml_str)
                if data:
                    phase = data.get("phase")
                    if phase == "SELECT" or (not phase and ("files" in data or "functions" in data or "search" in data)):
                        selection_data = data
                        break
                    
            # Fallback to JSON
            if not selection_data:
                results = extract_json_from_text(clipboard_content)
                for json_str in results:
                    data, _ = intelligent_json_fix(json_str)
                    if data and isinstance(data, dict):
                            phase = data.get("phase")
                            if phase == "SELECT" or (not phase and ("files" in data or "functions" in data or "search" in data)):
                                selection_data = data
                                break

            if not selection_data:
                console.print("[bold red]No valid SELECT JSON or XML payload found on clipboard.[/bold red]")
                return

            console.print("[green]Found valid JSON selection payload.[/green]")
            res = resolve_selection_payload(selection_data, root_dir, max_depth, ext_filters, args.exclude)
            if res[0] is None:
                return
            found_files, important_files, partial_files, missing_files_warnings, search_report = res
            
            if not found_files:
                console.print("[bold red]No files were successfully selected from the payload.[/bold red]")
                return
            
            all_known_files = list(found_files)

        else:
            if args.paths:
                found_files = []
                important_files = []
                
                # Single file special casing for root_dir adjustment
                if len(args.paths) == 1 and os.path.isfile(args.paths[0]):
                    target_path = os.path.abspath(args.paths[0])
                    if not target_path.startswith(root_dir):
                        root_dir = os.path.dirname(target_path)
                
                for p in args.paths:
                    target_path = os.path.abspath(p)
                    if os.path.isfile(target_path):
                        found_files.append(target_path)
                        important_files.append(target_path)
                        console.print(f"[green]Targeting file:[/green] {p}")
                    elif os.path.isdir(target_path):
                        with console.status(f"[bold green]Scanning directory: {p}...[/bold green]", spinner="dots"):
                            dir_files = get_files_recursive(target_path, 0, max_depth, ext_filters, exclude_dirs=args.exclude)
                            found_files.extend(dir_files)
                    else:
                        console.print(Panel(f"Path not found: {p}", title="Error", style="bold red"))
                        return
                        
                # Deduplicate while preserving order
                found_files = list(dict.fromkeys(found_files))
                important_files = list(dict.fromkeys(important_files))
            else:
                with console.status("[bold green]Scanning directory structure...[/bold green]", spinner="dots"):
                    found_files = get_files_recursive(root_dir, 0, max_depth, ext_filters, exclude_dirs=args.exclude)
            
            if args.file_culling or args.select:
                from combinecopy.utils import prime_ast_cache
                prime_ast_cache(root_dir, found_files)
                
            all_known_files = list(found_files)

            partial_files = {}
            if args.select and found_files:
                console.print("[bold cyan]Phase: Manual File Selection[/bold cyan]")
                selected = run_file_selector(root_dir, found_files, ast_mode=args.file_culling)
                if selected is None:
                    console.print(Panel("Selection cancelled.", title="Cancelled", style="bold yellow"))
                    return
                found_files, important_files, partial_files = selected
                all_known_files = list(found_files)
            else:
                if important_files is None:
                    important_files = list(found_files)
    
        total_files = len(found_files)
        if total_files == 0:
            console.print(Panel("No matching files found.", title="Result", style="bold red"))
            return

        only_files_targeted = bool(args.paths) and all(os.path.isfile(p) for p in args.paths)
        is_targeted = args.select or only_files_targeted
        if total_files > 250 and not is_targeted:
            app = ConfirmCopyApp(total_files)
            confirmed = app.run()
            if not confirmed:
                console.print(Panel("Large copy operation cancelled.", title="Cancelled", style="bold yellow"))
                return
    
        display_summary(root_dir, max_depth, ext_filters, batch_count, total_files)
    
        agent_type = "orchestrator" if args.orchestrate else "cli" if args.cli else "default"

        user_request_data = None
        if args.system is not None or args.cli:
            console.print("[bold cyan]Phase: Instruction & System Prompt[/bold cyan]")
            sys_arg = args.system if args.system else 'DEFAULT'
            if sys_arg == 'DEFAULT' or sys_arg == '':
                sys_prompt_text = get_system_prompt(agent_type=agent_type, file_cull=args.file_culling, xml_mode=args.xml, consult=args.consult, custom_rules=custom_rules, rehab=args.rehab, divide=args.divide)
            else:
                try:
                    with open(sys_arg, 'r', encoding='utf-8') as f:
                        sys_prompt_text = f.read().strip()
                except Exception as e:
                    console.print(f"[red]Error reading system prompt file: {e}[/red]")
                    return
                    
            app = SystemPromptApp(root_dir, found_files, sys_prompt_text)
            user_request_data = app.run()
            if not user_request_data:
                console.print(Panel("System prompt setup cancelled.", title="Cancelled", style="bold yellow"))
                return

        git_diff_text = ""
        if args.diff:
            try:
                out = subprocess.check_output(
                    ['git', 'diff', 'HEAD'],
                    cwd=root_dir,
                    text=True,
                    errors="replace",
                    stderr=subprocess.STDOUT
                )
                if out.strip():
                    git_diff_text = out.strip()
                    console.print("[cyan]ℹ[/cyan] Captured uncommitted git diff.")
                else:
                    console.print("[yellow]Warning: --diff flag provided but no git diff found.[/yellow]")
            except Exception as e:
                console.print(f"[yellow]Warning: Failed to capture git diff: {e}[/yellow]")

        files_per_batch = math.ceil(total_files / batch_count)
        console.print(f"\n[dim]Splitting into {batch_count} batch(es). ~{files_per_batch} files/batch.[/dim]\n")
    
        for i in range(batch_count):
            batch_num = i + 1
            start_index = i * files_per_batch
            end_index = start_index + files_per_batch
            current_batch_files = found_files[start_index:end_index]
            
            if not current_batch_files:
                break
    
            console.print(Rule(f"[bold yellow]Batch {batch_num}/{batch_count}[/bold yellow]"))
            stop_event = threading.Event()

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console
            ) as progress:
                task = progress.add_task(f"[cyan]Processing {len(current_batch_files)} files (Press Ctrl+C to cancel)...", total=len(current_batch_files))

                def worker_fn():
                    important_set = set(important_files) if important_files is not None else set()
                    file_context_buffer = []
                    
                    for file_path in current_batch_files:
                        if stop_event.is_set(): return
                        if file_path in important_set or file_path in partial_files:
                            rel_path = os.path.relpath(file_path, root_dir)
                            is_partial = file_path in partial_files and file_path not in important_set
                            
                            if is_partial:
                                progress.console.print(f"  [green]✓[/green] Adding [bold]{rel_path}[/bold] (Partial Context)")
                            else:
                                progress.console.print(f"  [green]✓[/green] Adding [bold]{rel_path}[/bold] (Full Context)")
                                
                            _, ext = os.path.splitext(rel_path)
                            lang = ext.lstrip('.').lower()
                            file_context_buffer.append(separator)
                            file_context_buffer.append(f"FILE: {rel_path}")
                            file_context_buffer.append(separator)
                            file_context_buffer.append(f"```{lang}")
                            try:
                                content = safe_read_file(file_path)
                                if is_partial:
                                    file_context_buffer.append(render_partial_content(content, partial_files[file_path]))
                                else:
                                    file_context_buffer.append(content)
                            except Exception as e:
                                progress.console.print(f"  [red]![/red] Error reading {rel_path}: {e}")
                                file_context_buffer.append(f"[Error reading file: {e}]")
                            file_context_buffer.append("```")
                            file_context_buffer.append("\n")
                        else:
                            rel_path = os.path.relpath(file_path, root_dir)
                            progress.console.print(f"  [cyan]ℹ[/cyan] Included [bold]{rel_path}[/bold] in AST Map")
                        progress.advance(task)
                    if stop_event.is_set(): return
                    
                    if missing_files_warnings:
                        file_context_buffer.append("\n--- SYSTEM NOTE: MISSING FILES ---")
                        file_context_buffer.append("The following files were requested but could not be found or resolved in the workspace:")
                        for mfw in missing_files_warnings:
                            file_context_buffer.append(f"- {mfw}")
                        file_context_buffer.append("Please check your paths and request them again if necessary.\n")
                    search_note = build_search_note(search_report)
                    if search_note:
                        file_context_buffer.append(search_note)

                    if args.json_select:
                        from combinecopy.prompts import get_prune
                        file_context_buffer.append("\n--- SYSTEM NOTE: CONTEXT PRUNING ---")
                        file_context_buffer.append(get_prune(args.xml))
                        file_context_buffer.append("")
                    file_context_str = "\n".join(file_context_buffer)
                    ast_map_str = generate_tree_string(found_files, root_dir) if args.file_culling else ""

                    if batch_count == 1 and user_request_data:
                        full_text = build_prompt(
                            user_request=user_request_data["request"],
                            file_context=file_context_str,
                            ast_map=ast_map_str,
                            file_cull=args.file_culling,
                            system_prompt=user_request_data["system"],
                            agent_type=agent_type,
                            xml_mode=args.xml,
                            consult=args.consult,
                            custom_rules=custom_rules,
                            git_diff=git_diff_text,
                            rehab=args.rehab,
                            divide=args.divide
                        )
                    else:
                        parts = []
                        if batch_num == 1 and user_request_data:
                            parts.append(get_user_prompt(user_request_data["request"]))
                            if ast_map_str:
                                parts.append(get_ast(ast_map_str))
                            if file_context_str:
                                parts.append(get_file_context(file_context_str))
                            if git_diff_text:
                                parts.append(get_git_diff(git_diff_text))
                            parts.append(get_user_prompt(user_request_data["request"]))
                            parts.append(f"--- SYSTEM INSTRUCTIONS ---\n{user_request_data['system']}")
                        else:
                            if batch_num == 1 and ast_map_str:
                                parts.append(get_ast(ast_map_str))
                                if file_context_str:
                                    parts.append(get_file_context(file_context_str))
                            else:
                                if file_context_str:
                                    parts.append(file_context_str)
                            if batch_num == 1 and git_diff_text:
                                parts.append(get_git_diff(git_diff_text))
                        if batch_num == batch_count and user_request_data:
                            parts.append(get_user_prompt(user_request_data["request"], reminder=True))
                            parts.append(get_system_prompt_important(agent_type, xml_mode=args.xml, divide=args.divide))
                            
                        full_text = "\n\n".join(parts)

                    if args.file:
                        try:
                            fd, temp_path = tempfile.mkstemp(prefix="combineCopy_prompt_", suffix=".txt", text=True)
                            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                                f.write(full_text)
                            if copy_file_to_clipboard(temp_path):
                                progress.console.print(Panel(
                                    f"[bold green]Batch {batch_num} saved to {temp_path} and copied to clipboard![/bold green]\n"
                                    f"Contains {len(current_batch_files)} files.",
                                    border_style="green"
                                ))
                        except Exception as e:
                            progress.console.print(f"[bold red]Failed to save/copy file:[/bold red] {e}")
                    else:
                        if copy_to_clipboard(full_text):
                            progress.console.print(Panel(
                                f"[bold green]Batch {batch_num} copied to clipboard![/bold green]\n"
                                f"Contains {len(current_batch_files)} files.",
                                border_style="green"
                                ))

                worker_thread = threading.Thread(target=worker_fn, daemon=True)
                worker_thread.start()
                try:
                    while worker_thread.is_alive():
                        worker_thread.join(0.1)
                except KeyboardInterrupt:
                    stop_event.set()
                    raise KeyboardInterrupt
            if stop_event.is_set():
                break
    
            if batch_num < batch_count and end_index < total_files:
                console.print("[bold white on blue] PAUSE [/bold white on blue] Paste content now, then press [bold]Enter[/bold] for next batch...")
                input()
                console.print()
            else:
                console.print(Rule("[bold green]All Done[/bold green]"))
                
    except KeyboardInterrupt:
        console.print()
        console.print(Panel("[bold red]Process interrupted by user (Ctrl+C).[/bold red]", title="Cancelled"))
        return
        
    if args.auto or args.revert or args.orchestrate:
        if args.orchestrate:
            console.print(f"\n[bold cyan]Phase: Orchestrator Agent Execution[/bold cyan]")
            app = OrchestratorAgentApp(root_dir, use_file_clipboard=args.file, cli_mode=args.cli, xml_mode=args.xml)
            result = app.run()
            if result:
                console.print(Panel("Orchestrator payload successfully copied to clipboard.", title="Success", style="bold green"))
        else:
            phase_name = "Auto Agent Execution (Revert Mode)" if args.revert else "Auto Agent Execution"
            if args.web_apply:
                phase_name += " [WEB MACRO MODE]"
            console.print(f"\n[bold cyan]Phase: {phase_name}[/bold cyan]")
            app = AutoAgentApp(root_dir, all_known_files, revert_mode=args.revert, ignore_initial_clipboard=True, web_mode=args.web_apply, tfs_mode=args.tfs, xml_mode=args.xml, consult_mode=args.consult, rehab_mode=args.rehab, mobile_mode=args.mobile)
            result = app.run()
            if isinstance(result, dict) and result.get("type") == "task_division":
                data = result.get("data")
                tasks_file = os.path.join(root_dir, ".cc_tasks.json")
                import json
                with open(tasks_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4)
                console.print("\n[bold green]Task split payload intercepted![/bold green]")
                manage_tasks_cli(data, root_dir, max_depth, ext_filters, args.exclude, args, custom_rules)
            elif result:
                print_auto_summary(result)

    if zip_path_to_cleanup and os.path.exists(zip_path_to_cleanup):
        console.print()
        ans = console.input(f"[bold yellow]Delete the source .zip file ({os.path.basename(zip_path_to_cleanup)})? [Y/n]: [/bold yellow]").strip().lower()
        if ans in ['', 'y', 'yes']:
            try:
                os.remove(zip_path_to_cleanup)
                console.print(f"[green]Successfully deleted {os.path.basename(zip_path_to_cleanup)}[/green]")
            except Exception as e:
                console.print(f"[red]Failed to delete {zip_path_to_cleanup}: {e}[/red]")

def app_main():
    """Entry point for the 'app' command. Injects the '-a' flag automatically."""
    import sys
    if "-a" not in sys.argv and "--auto" not in sys.argv:
        sys.argv.insert(1, "-a")
    main()

if __name__ == "__main__":
    main()
