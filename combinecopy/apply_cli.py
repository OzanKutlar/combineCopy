"""A plain-terminal command-line interface for the apply listener.

Serves as the non-Textual counterpart to AutoAgentApp. It operates on the
current clipboard payload, displaying pending files, diffs on demand, and
offering single-key operations for applying, discarding, committing, Meld review,
and JSON repairs.
"""

import os
import sys
import json
import difflib
import subprocess
import tempfile
import pyperclip
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich import box
from rich.text import Text

from combinecopy.utils import (
    console,
    safe_read_file,
    detect_newline,
    extract_json_from_text,
    extract_xml_from_text,
    parse_xml_to_dict,
    intelligent_json_fix,
    render_word_diff,
    compute_new_text,
    find_line_number,
)
from combinecopy.apply_core import (
    write_text_preserving,
    validate_file_obj,
    commit_git,
    commit_tfs,
)
from combinecopy.mobile.env import find_meld, resolve_editor, run_editor, editor_display_name
from combinecopy.mobile.inbox import read_latest_dropped_file, INBOX_DIR
from combinecopy.mobile.clipboard import read_text_once


class _ConsoleDiffSink:
    """Adapts rich.Text writes from render_word_diff into console.print."""
    def __init__(self, console):
        self.console = console

    def write(self, text_or_str) -> None:
        self.console.print(text_or_str)


class ApplyCliSession:
    """One apply CLI listener session."""

    def __init__(
        self,
        root_dir: str,
        known_files: list[str] | None = None,
        revert_mode: bool = False,
        ignore_initial_clipboard: bool = False,
        web_mode: bool = False,
        tfs_mode: bool = False,
        xml_mode: bool = False,
        consult_mode: bool = False,
        rehab_mode: bool = False,
        mobile_mode: bool = False,
    ):
        self.root_dir = root_dir
        self.known_files = known_files or []
        self.revert_mode = revert_mode
        self.ignore_initial_clipboard = ignore_initial_clipboard
        self.web_mode = web_mode
        self.tfs_mode = tfs_mode
        self.xml_mode = xml_mode
        self.consult_mode = consult_mode
        self.rehab_mode = rehab_mode
        self.mobile_mode = mobile_mode

        self.payload: dict | None = None
        self.selected_idx: int | None = None
        self.session_applied_files: list[dict] = []
        self.last_clipboard = ""
        self.broken_json_content = ""
        self.json_error_text: str | None = None

    def _read_inbound_text(self) -> str:
        if self.mobile_mode:
            text = read_latest_dropped_file()
            if not text:
                text = read_text_once()
            return (text or "").strip()
        try:
            return (pyperclip.paste() or "").strip()
        except Exception:
            return ""

    def _parse_payload_string(self, content: str) -> tuple[dict | None, str | None, dict | None]:
        """Parses content into (execution_data, task_division_data, error_detail)."""
        if not content:
            return None, None, None

        # XML check
        if '<antigravity_payload>' in content:
            xml_blocks = extract_xml_from_text(content)
            for xml_str in xml_blocks:
                data = parse_xml_to_dict(xml_str)
                if data:
                    if data.get("phase") == "EXECUTION" and "files" in data:
                        return data, None, None
                    if data.get("phase") == "TASK" and "tasks" in data:
                        return None, data, None

        # JSON check
        if '"phase":' in content and ('"EXECUTION"' in content or '"TASK"' in content):
            json_blocks = extract_json_from_text(content)
            for json_str in json_blocks:
                try:
                    data = json.loads(json_str)
                    if isinstance(data, dict):
                        if data.get("phase") == "EXECUTION" and "files" in data:
                            return data, None, None
                        if data.get("phase") == "TASK" and "tasks" in data:
                            return None, data, None
                except json.JSONDecodeError as e:
                    fixed_data, _ = intelligent_json_fix(json_str)
                    if fixed_data and isinstance(fixed_data, dict):
                        if fixed_data.get("phase") == "EXECUTION" and "files" in fixed_data:
                            console.print("[dim green]Auto-fixed JSON syntax errors in payload.[/dim green]")
                            return fixed_data, None, None
                    if '"EXECUTION"' in json_str and '"phase"' in json_str:
                        self.broken_json_content = json_str
                        lines = json_str.split("\n")
                        error_line = lines[e.lineno - 1] if 0 < e.lineno <= len(lines) else ""
                        self.json_error_text = (
                            f"Execution JSON failed to parse due to a syntax error:\n"
                            f"{e.msg} on line {e.lineno}, column {e.colno}\n\n"
                            f"Failing line context:\n{error_line}"
                        )
                        return None, None, {"error": e, "raw": json_str}
        return None, None, None

    def _load_payload(self, data: dict) -> None:
        # Clean up key variations
        for file_obj in data.get("files", []):
            for block in file_obj.get("search_replace", []):
                if "replacement" in block and "replace" not in block:
                    block["replace"] = block.pop("replacement")

        if self.revert_mode:
            data["commit_message"] = "Revert: " + data.get("commit_message", "")
            for file_obj in data.get("files", []):
                action = file_obj.get("action", "modify").lower()
                if action == "command":
                    file_obj["_revert_warning"] = "Commands cannot be automatically reverted."
                elif action == "create":
                    file_obj["action"] = "delete"
                elif action == "delete":
                    file_obj["action"] = "create"
                    file_obj["content"] = ""
                    file_obj["_revert_warning"] = "Reverting a delete creates an empty file."
                elif action == "modify":
                    if "search_replace" in file_obj:
                        new_sr = []
                        for block in reversed(file_obj.get("search_replace", [])):
                            new_sr.append({
                                "search": block.get("replace", ""),
                                "replace": block.get("search", ""),
                            })
                        file_obj["search_replace"] = new_sr
                    elif "content" in file_obj:
                        file_obj["_revert_error"] = "Cannot revert a full file overwrite without original content."

        files_list = data.get("files", [])
        for file_obj in files_list:
            file_obj["_status"] = "pending"
            file_obj.setdefault("_warnings", [])
            if "_revert_warning" in file_obj:
                file_obj["_warnings"].append(file_obj.pop("_revert_warning"))
            validate_file_obj(file_obj, self.root_dir, self.known_files, web_mode=self.web_mode)

        self.payload = data
        self.selected_idx = 0 if files_list else None
        self.json_error_text = None
        self.broken_json_content = ""

    def _print_files_table(self) -> None:
        if not self.payload:
            console.print("[dim yellow](No execution payload loaded. Copy one and press 'r' to reload)[/dim yellow]")
            return

        files = self.payload.get("files", [])
        table = Table(title="Pending Execution Payload", box=box.ROUNDED)
        table.add_column("#", style="dim", no_wrap=True)
        table.add_column("Action", style="bold", no_wrap=True)
        table.add_column("Target / Command", style="cyan")
        table.add_column("Status", style="magenta")

        for idx, file_obj in enumerate(files):
            action = file_obj.get("action", "modify").upper()
            if action == "COMMAND":
                target = file_obj.get("command", "(command)")
            else:
                target = file_obj.get("path", "(path)")

            status = file_obj.get("_status", "pending")
            errors = file_obj.get("_errors", [])
            warnings = file_obj.get("_warnings", [])

            if status == "applied":
                status_str = "[bold green]✓ applied[/bold green]"
            elif status == "discarded":
                status_str = "[bold red]✗ discarded[/bold red]"
            else:
                status_str = "[yellow]pending[/yellow]"

            if errors:
                status_str += " [bold red](error)[/bold red]"
            elif warnings:
                status_str += f" [dim yellow]({len(warnings)} warn)[/dim yellow]"

            prefix = ">" if self.selected_idx == idx else " "
            action_col = f"[{'green' if action=='CREATE' else 'yellow' if action=='MODIFY' else 'red'}]{action}[/]"
            table.add_row(f"{prefix}{idx + 1}", action_col, target, status_str)

        console.print(table)
        commit_msg = self.payload.get("commit_message")
        if commit_msg:
            console.print(f"[dim]Proposed commit: {commit_msg.splitlines()[0]}[/dim]")

    def _print_selected_details(self) -> None:
        if self.selected_idx is None or not self.payload:
            return
        files = self.payload.get("files", [])
        if not (0 <= self.selected_idx < len(files)):
            return

        f = files[self.selected_idx]
        action = f.get("action", "modify").upper()
        target = f.get("command" if action == "COMMAND" else "path", "unknown")
        console.print(f"\n[bold cyan]Selected #{self.selected_idx + 1}:[/bold cyan] [{action}] {target}")

        warnings = f.get("_warnings", [])
        for w in warnings:
            console.print(f"  [yellow]⚠ {w}[/yellow]")
        errors = f.get("_errors", [])
        for e in errors:
            console.print(f"  [bold red]⛔ {e}[/bold red]")
        console.print("[dim]Press 'v' to view diff, 'm' for Meld, 'a' to apply, 'd' to discard.[/dim]")

    def _render_selected_diff(self) -> None:
        if self.selected_idx is None or not self.payload:
            console.print("[yellow]No file selected to diff.[/yellow]")
            return
        files = self.payload.get("files", [])
        if not (0 <= self.selected_idx < len(files)):
            return

        f = files[self.selected_idx]
        action = f.get("action", "modify").upper()
        if action == "COMMAND":
            console.print(f"[cyan]Command to execute:[/cyan] {f.get('command')}")
            return

        path = f.get("path", "")
        full_path = os.path.join(self.root_dir, path)
        old_text = safe_read_file(full_path) if os.path.exists(full_path) else ""
        new_text = compute_new_text(f, old_text)

        console.print(Rule(f"[bold blue]Diff for {path}[/bold blue]"))
        if old_text == new_text:
            console.print("[dim]No changes detected.[/dim]")
            return
        sink = _ConsoleDiffSink(console)
        render_word_diff(old_text, new_text, sink)

    def _apply_file_idx(self, idx: int) -> bool:
        if not self.payload:
            return False
        files = self.payload.get("files", [])
        if not (0 <= idx < len(files)):
            return False
        f = files[idx]
        action = f.get("action", "modify").lower()

        if action == "command":
            cmd = f.get("command", "")
            console.print(f"[bold cyan]Running command:[/bold cyan] {cmd}")
            try:
                res = subprocess.run(cmd, shell=True, cwd=self.root_dir)
                if res.returncode == 0:
                    f["_status"] = "applied"
                    self._record_applied(f)
                    console.print("[green]Command completed successfully.[/green]")
                    return True
                else:
                    console.print(f"[bold red]Command exited with code {res.returncode}.[/bold red]")
                    return False
            except Exception as e:
                console.print(f"[bold red]Failed to run command: {e}[/bold red]")
                return False

        path = f.get("path", "")
        full_path = os.path.join(self.root_dir, path)

        if action == "delete":
            if self.tfs_mode:
                from combinecopy.vcs_tfs import tfs_delete
                errs = tfs_delete(self.root_dir, [path])
                if errs:
                    console.print(f"[red]TFS delete error: {errs[0]}[/red]")
            else:
                if os.path.exists(full_path):
                    os.remove(full_path)
            f["_status"] = "applied"
            self._record_applied(f)
            console.print(f"[bold red]✗ Deleted file:[/] {path}")
            return True

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        if action == "create":
            write_text_preserving(full_path, f.get("content", ""), original_newline="\n")
            f["_status"] = "applied"
            self._record_applied(f)
            console.print(f"[bold green]✓ Created file:[/] {path}")
            return True

        if action == "modify":
            if self.tfs_mode and os.path.exists(full_path):
                from combinecopy.vcs_tfs import tfs_checkout
                errs = tfs_checkout(self.root_dir, [path])
                if errs:
                    console.print(f"[yellow]TFS checkout warning: {errs[0]}[/yellow]")

            original_nl = detect_newline(full_path) if os.path.exists(full_path) else "\n"
            old_text = safe_read_file(full_path) if os.path.exists(full_path) else ""
            new_text = compute_new_text(f, old_text)

            diff = list(difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True), n=0))
            added = sum(1 for line in diff if line.startswith('+') and not line.startswith('+++'))
            removed = sum(1 for line in diff if line.startswith('-') and not line.startswith('---'))
            f["_added"] = added
            f["_removed"] = removed

            write_text_preserving(full_path, new_text, original_newline=original_nl or "\n")
            f["_status"] = "applied"
            self._record_applied(f)
            console.print(f"[bold yellow]✓ Modified file:[/] {path} (+{added}/-{removed})")
            return True
        return False

    def _record_applied(self, file_obj: dict) -> None:
        identifier = file_obj.get("path") or file_obj.get("command")
        if not identifier:
            return
        self.session_applied_files = [
            f for f in self.session_applied_files
            if (f.get("path") or f.get("command")) != identifier
        ]
        self.session_applied_files.append(file_obj)

    def _handle_meld(self) -> None:
        if self.selected_idx is None or not self.payload:
            console.print("[yellow]No file selected.[/yellow]")
            return
        f = self.payload["files"][self.selected_idx]
        if f.get("action", "").lower() == "command":
            console.print("[yellow]Cannot open command in Meld.[/yellow]")
            return

        path = f.get("path", "")
        full_path = os.path.join(self.root_dir, path)
        old_text = safe_read_file(full_path) if os.path.exists(full_path) else ""
        nl = detect_newline(full_path) or "\n"
        new_text = compute_new_text(f, old_text)

        base_name = os.path.basename(path) or "file"
        fd_old, path_old = tempfile.mkstemp(suffix="_old_" + base_name, text=True)
        fd_new, path_new = tempfile.mkstemp(suffix="_new_" + base_name, text=True)
        os.close(fd_old)
        os.close(fd_new)

        try:
            write_text_preserving(path_old, old_text, original_newline=nl)
            write_text_preserving(path_new, new_text, original_newline=nl)

            meld_exe = find_meld()
            if not meld_exe:
                console.print("[yellow]Meld not found; showing unified git diff instead:[/yellow]")
                proc = subprocess.run(
                    ["git", "diff", "--no-index", "--color=always", path_old, path_new],
                    capture_output=True, text=True, errors="replace"
                )
                console.print(proc.stdout or proc.stderr or "No diff.")
                return

            console.print("[dim]Opening Meld. Modify and save the right pane to fold changes back.[/dim]")
            subprocess.run([meld_exe, path_old, path_new], check=False)
            edited_text = safe_read_file(path_new)
            if edited_text != new_text:
                f["content"] = edited_text
                f.pop("search_replace", None)
                f.pop("regex_replace", None)
                warn = "Meld edited: replacement content was hand-edited in Meld."
                if warn not in f.setdefault("_warnings", []):
                    f["_warnings"].append(warn)
                validate_file_obj(f, self.root_dir, self.known_files, web_mode=self.web_mode)
                console.print("[green]Folded Meld edits back into pending modification.[/green]")
            else:
                console.print("[dim]Meld closed without modifications.[/dim]")
        finally:
            for p in (path_old, path_new):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass

    def _handle_json_fix(self) -> None:
        if not self.broken_json_content:
            console.print("[yellow]No broken JSON payload recorded to fix.[/yellow]")
            return
        fd, temp_path = tempfile.mkstemp(suffix=".json", text=True)
        os.close(fd)
        try:
            with open(temp_path, "w", encoding="utf-8") as h:
                h.write(self.broken_json_content)
            console.print(f"[dim]Launching {editor_display_name()} to repair JSON...[/dim]")
            if run_editor(temp_path):
                with open(temp_path, "r", encoding="utf-8") as h:
                    fixed = h.read()
                if fixed != self.broken_json_content:
                    pyperclip.copy(fixed)
                    console.print("[green]Fixed JSON copied to clipboard. Reloading...[/green]")
                    self.reload_inbound(fixed)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def _handle_commit(self) -> dict | None:
        if not self.payload:
            console.print("[yellow]No payload loaded.[/yellow]")
            return None
        msg = self.payload.get("commit_message", "Auto-commit from AI agent")
        applied = [f for f in self.payload.get("files", []) if f.get("_status") == "applied"]
        paths = [f.get("path") for f in applied if f.get("path") and f.get("action", "").lower() != "command"]

        if not paths and not any(f.get("action", "").lower() == "command" for f in applied):
            console.print("[yellow]No applied changes to commit.[/yellow]")
            return None

        commit_hash = None
        if self.tfs_mode:
            changeset, warnings, error = commit_tfs(self.root_dir, msg, applied, paths)
            for w in warnings:
                console.print(f"[yellow]TFS warning: {w}[/yellow]")
            if error:
                console.print(f"[bold red]TFS commit error: {error}[/bold red]")
                return None
            commit_hash = f"CS{changeset}" if changeset else None
            console.print(f"[bold green]Changes checked into TFS as changeset #{changeset}![/bold green]")
        else:
            commit_hash, error = commit_git(self.root_dir, msg, paths)
            if error:
                console.print(f"[bold red]{error}[/bold red]")
                return None
            console.print(f"[bold green]Changes committed to git ({commit_hash or 'HEAD'})![/bold green]")

        return {
            "commit_message": msg,
            "files": self.payload.get("files", []),
            "commit_hash": commit_hash,
        }

    def reload_inbound(self, explicit_text: str | None = None) -> None:
        content = explicit_text if explicit_text is not None else self._read_inbound_text()
        if not content:
            console.print(f"[yellow]Clipboard is empty. (Inbox: {INBOX_DIR})[/yellow]")
            return
        exec_data, task_data, err = self._parse_payload_string(content)
        if task_data:
            console.print("[green]Task division payload detected.[/green]")
            self.payload = task_data
            return
        if exec_data:
            self._load_payload(exec_data)
            console.print(f"[green]Loaded execution payload ({len(exec_data.get('files', []))} file(s)).[/green]")
            self._print_files_table()
            return
        if err:
            console.print(f"[bold red]Failed to parse execution JSON: {err['error']}[/bold red]")
            console.print("[dim]Press 'f' to open and fix in your editor, or 'e' to copy the error.[/dim]")
            return
        console.print("[yellow]No valid EXECUTION or TASK payload found on clipboard.[/yellow]")

    def run(self) -> dict | None:
        console.print(Rule("[bold blue]CLI Apply Listener[/bold blue]"))
        console.print("[dim]Checking clipboard for execution payload...[/dim]")
        self.reload_inbound()

        while True:
            try:
                raw = console.input("\n[bold cyan]apply[/bold cyan]> ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Apply listener exiting.[/yellow]")
                return self._summary_result(None)

            if not raw:
                continue

            # Numbers select a file
            if raw.isdigit():
                idx = int(raw) - 1
                if self.payload and 0 <= idx < len(self.payload.get("files", [])):
                    self.selected_idx = idx
                    self._print_selected_details()
                else:
                    console.print("[red]Invalid file number.[/red]")
                continue

            # Single-key operations (case-sensitive for A/D)
            cmd = raw

            if cmd == "v":
                self._render_selected_diff()

            elif cmd == "a":
                if self.selected_idx is None:
                    console.print("[yellow]No file selected. Type a file number first.[/yellow]")
                else:
                    self._apply_file_idx(self.selected_idx)
                    self._print_files_table()

            elif cmd == "A":
                if not self.payload:
                    console.print("[yellow]No payload loaded.[/yellow]")
                else:
                    pending = [i for i, f in enumerate(self.payload.get("files", [])) if f.get("_status") == "pending"]
                    for i in pending:
                        self._apply_file_idx(i)
                    self._print_files_table()

            elif cmd == "d":
                if self.selected_idx is None or not self.payload:
                    console.print("[yellow]No file selected.[/yellow]")
                else:
                    self.payload["files"][self.selected_idx]["_status"] = "discarded"
                    console.print(f"[yellow]Discarded change for file #{self.selected_idx + 1}.[/yellow]")
                    self._print_files_table()

            elif cmd == "D":
                if not self.payload:
                    console.print("[yellow]No payload loaded.[/yellow]")
                else:
                    for f in self.payload.get("files", []):
                        if f.get("_status") == "pending":
                            f["_status"] = "discarded"
                    console.print("[yellow]Discarded all pending changes.[/yellow]")
                    self._print_files_table()

            elif cmd == "c":
                res = self._handle_commit()
                if res is not None:
                    return res

            elif cmd == "m":
                self._handle_meld()

            elif cmd == "f":
                self._handle_json_fix()

            elif cmd == "r":
                self.reload_inbound()

            elif cmd == "e":
                if self.json_error_text:
                    pyperclip.copy(self.json_error_text)
                    console.print("[green]Copied JSON syntax error to clipboard.[/green]")
                elif self.selected_idx is not None and self.payload:
                    f = self.payload["files"][self.selected_idx]
                    errs = f.get("_errors", [])
                    if errs:
                        msg = f"ACTION FAILED VALIDATION FOR {f.get('path', 'unknown')}:\n" + "\n".join(f"- {e}" for e in errs)
                        pyperclip.copy(msg)
                        console.print("[green]Copied file validation error to clipboard.[/green]")
                    else:
                        console.print("[yellow]Selected file has no errors.[/yellow]")
                else:
                    console.print("[yellow]No error available to copy.[/yellow]")

            elif cmd in ("?", "help"):
                self._print_help()

            elif cmd in ("l", "list"):
                self._print_files_table()

            elif cmd in ("q", "quit", "exit"):
                return self._summary_result(None)

            # NOT IMPLEMENTED: Partial Add
            elif cmd == "p":
                console.print("[yellow]Partial Add (p) is only supported in the full TUI. Run without --apply-cli.[/yellow]")

            # NOT IMPLEMENTED: Human Correct
            elif cmd == "h":
                console.print("[yellow]Human Correct (h) candidate selection is only supported in the full TUI. Run without --apply-cli.[/yellow]")

            # NOT IMPLEMENTED: Rehab Mode practice session
            elif cmd == "t":
                console.print("[yellow]Active Recall practice (t) is only supported in the full TUI. Run without --apply-cli.[/yellow]")

            else:
                console.print(f"[yellow]Unknown command '{cmd}'. Type '?' for help.[/yellow]")

    def _summary_result(self, commit_hash: str | None) -> dict | None:
        """Always returns a session summary dictionary upon quitting or finishing."""
        if self.payload and self.payload.get("phase") == "TASK":
            return {"type": "task_division", "data": self.payload}
        files = getattr(self, "session_applied_files", [])
        if not files and self.payload:
            files = [f for f in self.payload.get("files", []) if f.get("_status") == "applied"]
        return {
            "commit_message": (self.payload.get("commit_message") if self.payload else "Not committed") or "Not committed",
            "files": files,
            "commit_hash": commit_hash,
        }

    def _print_help(self) -> None:
        console.print(Rule("[bold blue]Commands[/bold blue]"))
        console.print("  [cyan]<number>[/cyan]     Select file by index to inspect warnings/errors")
        console.print("  [cyan]v[/cyan]            View word-level diff of the selected file")
        console.print("  [cyan]a[/cyan]            Apply the selected file")
        console.print("  [cyan]A[/cyan]            Apply all pending files")
        console.print("  [cyan]d[/cyan] / [cyan]D[/cyan]        Discard selected file / Discard all pending")
        console.print("  [cyan]m[/cyan]            Open selected file diff in Meld (folds edits back)")
        console.print("  [cyan]f[/cyan]            Open broken JSON in your editor to repair and reload")
        console.print("  [cyan]c[/cyan]            Commit applied changes to VCS (Git or TFS)")
        console.print("  [cyan]r[/cyan]            Reload inbound payload from clipboard / inbox")
        console.print("  [cyan]e[/cyan]            Copy validation or JSON error to clipboard")
        console.print("  [cyan]l[/cyan]            Reprint the file status table")
        console.print("  [cyan]q[/cyan]            Quit session and show summary")


def run_apply_cli(
    root_dir: str,
    known_files: list[str] | None = None,
    revert_mode: bool = False,
    ignore_initial_clipboard: bool = False,
    web_mode: bool = False,
    tfs_mode: bool = False,
    xml_mode: bool = False,
    consult_mode: bool = False,
    rehab_mode: bool = False,
    mobile_mode: bool = False,
) -> dict | None:
    """Runs the CLI apply listener session."""
    session = ApplyCliSession(
        root_dir=root_dir,
        known_files=known_files,
        revert_mode=revert_mode,
        ignore_initial_clipboard=ignore_initial_clipboard,
        web_mode=web_mode,
        tfs_mode=tfs_mode,
        xml_mode=xml_mode,
        consult_mode=consult_mode,
        rehab_mode=rehab_mode,
        mobile_mode=mobile_mode,
    )
    return session.run()
