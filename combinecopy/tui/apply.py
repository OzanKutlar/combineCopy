import os
import sys
import asyncio
import time
import json
import difflib
import re
import subprocess
import threading
import tempfile
import shutil
from rich.text import Text
from rich.style import Style
import pyperclip

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Label, ListView, ListItem, Button, Static, RichLog, TextArea, Markdown
from textual.binding import Binding
from textual.screen import ModalScreen
try:
    from textual.widgets.text_area import Selection
except ImportError:
    Selection = None

from combinecopy.utils import (
    safe_read_file,
    intelligent_json_fix,
    render_word_diff,
    copy_to_clipboard,
    detect_newline,
    extract_json_from_text,
    extract_xml_from_text,
    parse_xml_to_dict,
    extract_consult_answers,
    compute_new_text,
    find_line_number
)

def _write_text_preserving(path: str, text: str, original_newline: str | None = None) -> None:
    """Writes `text` to `path` preserving the file's original line endings exactly.
    Uses surrogateescape so non-UTF8 bytes that were read back can round-trip
    losslessly, and `newline=\"\"` to disable Python's automatic newline translation.
    """
    if original_newline is None:
        original_newline = "\n"
    if original_newline and original_newline != "\n":
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        text = normalized.replace("\n", original_newline)
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)
from combinecopy.vcs_tfs import tfs_checkout, tfs_add, tfs_delete, tfs_checkin
from combinecopy.mobile.inbox import PayloadInbox, read_latest_dropped_file, INBOX_DIR
from combinecopy.mobile.clipboard import read_text_once
class RehabScreen(ModalScreen[bool]):
    CSS = """
    RehabScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }
    #rehab-dialog {
        width: 95%;
        height: 95%;
        border: solid #d08c60;
        background: #2d2825;
        padding: 1 2;
    }
    .rehab-title {
        text-align: center;
        text-style: bold;
        color: #d08c60;
        margin-bottom: 1;
        background: #4a3f39;
        padding: 1;
    }
    #rehab-body {
        height: 1fr;
    }
    #rehab-left {
        width: 50%;
        border-right: solid #5a4d45;
        padding-right: 1;
        overflow-y: auto;
    }
    #rehab-right {
        width: 50%;
        padding-left: 1;
    }
    #rehab-instructions {
        height: auto;
    }
    #rehab-solution-container {
        height: 1fr;
    }
    #rehab-hidden-msg {
        height: 1fr;
        content-align: center middle;
        color: #a0a0a0;
        border: dashed #5a4d45;
        padding: 2;
    }
    #rehab-solution {
        height: 1fr;
        border: solid #5a4d45;
        background: #1e1a18;
        padding: 1;
        overflow-y: auto;
        display: none;
    }
    #rehab-footer {
        height: 3;
        align: right middle;
        border-top: solid #5a4d45;
        margin-top: 1;
    }
    Button {
        margin: 0 1;
    }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("o", "open_editor", "Open in Editor"),
        Binding("m", "verify_meld", "Verify in Meld"),
        Binding("r", "reveal_solution", "Reveal AI Solution"),
        Binding("h", "show_hint", "Show Hint"),
    ]
    def __init__(self, file_obj: dict, root_dir: str, original_text: str):
        super().__init__()
        self.file_obj = file_obj
        self.root_dir = root_dir
        self.original_text = original_text
        self.file_path = file_obj.get("path", "")
        self.full_path = os.path.join(self.root_dir, self.file_path)
        self._solution_loaded = False
        self.filename = os.path.basename(self.file_path)
        self.all_hints = []
        self.revealed_hints = 0
        
        action = self.file_obj.get("action", "modify").upper()
        if action == "CREATE":
            self.all_hints.extend(self.file_obj.get("hints", []))
        else:
            for b in self.file_obj.get("search_replace", []):
                self.all_hints.extend(b.get("hints", []))
                
        self.original_newline = detect_newline(self.full_path) if os.path.exists(self.full_path) else "\n"
        if not self.original_newline:
            self.original_newline = "\n"
            
        fd, self.temp_human_path = tempfile.mkstemp(suffix=f"_HUMAN_{self.filename}", text=True)
        os.close(fd)
        _write_text_preserving(self.temp_human_path, self.original_text, original_newline=self.original_newline)

    def on_unmount(self) -> None:
        try:
            if os.path.exists(self.temp_human_path):
                os.remove(self.temp_human_path)
        except:
            pass

    def compose(self) -> ComposeResult:
        with Vertical(id="rehab-dialog"):
            yield Label(f"Rehab Mode: {self.file_path}", classes="rehab-title")
            with Horizontal(id="rehab-body"):
                with Vertical(id="rehab-left"):
                    yield Label("Instructions", classes="panel-title")
                    yield Markdown(self._build_instructions_md(), id="rehab-instructions")
                with Vertical(id="rehab-right"):
                    yield Label("AI Solution", classes="panel-title")
                    with Vertical(id="rehab-solution-container"):
                        yield Label("Solution is hidden to encourage active recall.\n\nPress 'r' or click 'Reveal AI Code' to view the AI's exact changes.", id="rehab-hidden-msg")
                        yield RichLog(id="rehab-solution", highlight=True)
            with Horizontal(id="rehab-footer"):
                yield Button("Open in Editor (o)", id="btn-editor", variant="primary")
                yield Button("Verify in Meld (m)", id="btn-meld", variant="success")
                yield Button("Hint (h)", id="btn-hint", variant="default", disabled=len(self.all_hints) == 0)
                yield Button("Reveal AI Code (r)", id="btn-reveal", variant="warning")
                yield Button("Cancel", id="btn-cancel", variant="error")

    def _build_instructions_md(self) -> str:
        md = []
        action = self.file_obj.get("action", "modify").upper()
        if action == "CREATE":
            md.append("### Create File")
            md.append(f"**Path:** `{self.file_path}`")
            if "instruction" in self.file_obj:
                md.append(f"**Instruction:** {self.file_obj['instruction']}")
            else:
                md.append("**Instruction:** Write the entire file based on the context.")
        else:
            md.append(f"### Modify File: `{self.file_path}`")
            blocks = self.file_obj.get("search_replace", [])
            for i, b in enumerate(blocks):
                md.append(f"#### Block {i+1}")
                inst = b.get("instruction", "*(No instruction provided by AI)*")
                md.append(f"**Task:** {inst}")
                search_code = b.get("search", "")
                md.append("\n**Target Code:**")
                md.append(f"```python\n{search_code}\n```")
                md.append("---")
                
        if self.revealed_hints > 0:
            md.append("\n### Hints")
            for i in range(self.revealed_hints):
                md.append(f"{i+1}. {self.all_hints[i]}")
                
        return "\n".join(md)
    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_show_hint(self) -> None:
        if self.revealed_hints < len(self.all_hints):
            self.revealed_hints += 1
            self.query_one("#rehab-instructions", Markdown).update(self._build_instructions_md())
            if self.revealed_hints >= len(self.all_hints):
                self.query_one("#btn-hint", Button).disabled = True
    def action_open_editor(self) -> None:
        line_num = 1
        blocks = self.file_obj.get("search_replace", [])
        if blocks:
            line_num = find_line_number(self.original_text, blocks[0].get("search", ""))
        from combinecopy.mobile.env import resolve_editor, editor_display_name, is_terminal_editor

        editor = resolve_editor()
        if not editor:
            self.notify("No editor found. Try: pkg install micro", severity="error")
            return

        base = os.path.basename(editor[0]).lower()
        argv = list(editor)
        if "notepad++" in base:
            argv.append(f"-n{line_num}")
        elif base.split(".")[0] in ("micro", "nano", "vi", "vim", "nvim"):
            argv.append(f"+{line_num}")
        argv.append(self.temp_human_path)

        try:
            if is_terminal_editor(editor[0]):
                # Terminal editors take over the TTY, so the TUI must step aside.
                suspend = getattr(self.app, "suspend", None)
                if suspend is not None:
                    with suspend():
                        subprocess.run(argv, check=False)
                else:
                    subprocess.run(argv, check=False)
                self.notify(
                    f"Returned from {editor_display_name()}. Verify in Meld when ready.",
                    severity="information",
                )
            else:
                subprocess.Popen(argv)
                self.notify("Editor opened. Edit your copy, save, then Verify in Meld.", severity="info")
        except Exception as e:
            self.notify(f"Failed to open editor: {e}", severity="error")

    def action_verify_meld(self) -> None:
        self.run_worker(self._run_meld, exclusive=True)

    async def _run_meld(self) -> None:
        ai_text = compute_new_text(self.file_obj, self.original_text)
        base_name = os.path.basename(self.file_path)
        fd_ai, path_ai = tempfile.mkstemp(suffix=f"_AI_{base_name}", text=True)
        fd_merge, path_merge = tempfile.mkstemp(suffix=f"_MERGED_{base_name}", text=True)
        os.close(fd_ai)
        os.close(fd_merge)

        _write_text_preserving(path_ai, ai_text, original_newline=self.original_newline)
        _write_text_preserving(path_merge, self.original_text, original_newline=self.original_newline)
        try:
            from combinecopy.mobile.env import find_meld
            meld_exe = find_meld()
            if not meld_exe:
                self.notify("Meld not found! Please install Meld and add it to PATH.", severity="error")
                return

            self.notify("Launching Meld... Center panel is the target output. Save and close when done.", severity="info")
            process = await asyncio.create_subprocess_exec(meld_exe, path_ai, path_merge, self.temp_human_path)
            await process.wait()

            final_text = safe_read_file(path_merge)

            _write_text_preserving(self.full_path, final_text, original_newline=self.original_newline)
            
            old_lines = self.original_text.splitlines(keepends=True)
            new_lines = final_text.splitlines(keepends=True)
            diff = list(difflib.unified_diff(old_lines, new_lines, n=0))
            added = sum(1 for line in diff if line.startswith('+') and not line.startswith('+++'))
            removed = sum(1 for line in diff if line.startswith('-') and not line.startswith('---'))
            self.file_obj["_added"] = added
            self.file_obj["_removed"] = removed
            
            self.dismiss(True)

        except Exception as e:
            self.notify(f"Failed to run Meld: {e}", severity="error")
        finally:
            for p in [path_ai, path_merge]:
                try: os.remove(p)
                except: pass

    def action_reveal_solution(self) -> None:
        log = self.query_one("#rehab-solution", RichLog)
        msg = self.query_one("#rehab-hidden-msg", Label)
        if log.styles.display == "none":
            log.styles.display = "block"
            msg.styles.display = "none"
            if not self._solution_loaded:
                action = self.file_obj.get("action", "modify").upper()
                if action == "CREATE":
                    log.write(self.file_obj.get("content", ""))
                else:
                    for i, b in enumerate(self.file_obj.get("search_replace", [])):
                        log.write(f"--- BLOCK {i+1} REPLACEMENT ---")
                        log.write(b.get("replace", ""))
                self._solution_loaded = True
        else:
            log.styles.display = "none"
            msg.styles.display = "block"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-editor":
            self.action_open_editor()
        elif btn_id == "btn-meld":
            self.action_verify_meld()
        elif btn_id == "btn-reveal":
            self.action_reveal_solution()
        elif btn_id == "btn-hint":
            self.action_show_hint()
        elif btn_id == "btn-cancel":
            self.action_cancel()

class CommandExecutionScreen(ModalScreen[bool]):
    CSS = """
    CommandExecutionScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }
    #cmd-dialog {
        width: 80%;
        height: 80%;
        border: solid #d08c60;
        background: #2d2825;
        padding: 1 2;
    }
    .cmd-title {
        text-align: center;
        text-style: bold;
        color: #d08c60;
        margin-bottom: 1;
    }
    #cmd-log {
        height: 1fr;
        border: solid #5a4d45;
        background: #1e1a18;
        margin-bottom: 1;
    }
    #cmd-buttons {
        height: 3;
        align: right middle;
    }
    Button {
        margin: 0 1;
    }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel/Close"),
    ]

    def __init__(self, command: str, root_dir: str):
        super().__init__()
        self.command = command
        self.root_dir = root_dir
        self.process = None

    def compose(self) -> ComposeResult:
        with Vertical(id="cmd-dialog"):
            yield Label(f"Executing: {self.command}", classes="cmd-title")
            yield RichLog(id="cmd-log", highlight=True, wrap=True)
            with Horizontal(id="cmd-buttons"):
                yield Button("Close", id="btn-cmd-close", variant="primary", disabled=True)

    async def on_mount(self) -> None:
        self.run_worker(self.execute_command(), exclusive=True)

    async def execute_command(self) -> None:
        log = self.query_one("#cmd-log", RichLog)
        try:
            self.process = await asyncio.create_subprocess_shell(
                self.command,
                cwd=self.root_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded_line = line.decode('utf-8', errors='replace').rstrip('\r\n')
                log.write(decoded_line)
            
            await self.process.wait()
            log.write(f"\n[Process exited with code {self.process.returncode}]")
        except Exception as e:
            log.write(f"\n[Error executing command: {e}]")
        
        self.query_one("#btn-cmd-close", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cmd-close":
            success = self.process and self.process.returncode == 0
            self.dismiss(success)

    def action_cancel(self) -> None:
        btn = self.query_one("#btn-cmd-close", Button)
        if not btn.disabled:
            success = self.process and self.process.returncode == 0
            self.dismiss(success)
class HumanCorrectScreen(ModalScreen[str]):
    CSS = """
    HumanCorrectScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }
    #hc-dialog {
        width: 95%;
        height: 95%;
        border: solid #d08c60;
        background: #2d2825;
    }
    #hc-body {
        height: 1fr;
    }
    #hc-left {
        width: 25%;
        border-right: solid #5a4d45;
    }
    #hc-right {
        width: 75%;
    }
    #hc-top-right {
        height: 40%;
        border-bottom: solid #5a4d45;
    }
    #hc-diff-pane {
        width: 50%;
        border-right: solid #5a4d45;
    }
    #hc-replace-pane {
        width: 50%;
    }
    #hc-file-pane {
        height: 60%;
    }
    .hc-title {
        background: #4a3f39;
        color: #d08c60;
        padding: 1;
        text-style: bold;
    }
    #hc-footer {
        height: 3; 
        border-top: solid #5a4d45;
        align: right middle;
    }
    .hc-warning-label {
        color: #ff5555;
        text-style: bold;
        margin-right: 1;
        content-align: right middle;
        width: 1fr;
    }
    Button {
        margin: 0 1;
    }
    """

    def __init__(self, file_path: str, file_text: str, original_search: str, candidates: list, replace_text: str):
        super().__init__()
        self.file_path = file_path
        self.file_text = file_text
        self.original_search = original_search
        self.candidates = candidates
        self.replace_text = replace_text
        self.confirm_armed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="hc-dialog"):
            with Horizontal(id="hc-body"):
                with Vertical(id="hc-left"):
                    yield Label("Partial Matches", classes="hc-title")
                    list_items = []
                    for i, c in enumerate(self.candidates):
                        cov_pct = int(c["coverage"] * 100)
                        warn = "⚠️ " if cov_pct < 40 else ""
                        lbl = f"{warn}Lines {c['start_line']}-{c['end_line']}\n{c['matched_lines']}/{c['search_lines']} matched ({cov_pct}%)"
                        list_items.append(ListItem(Label(lbl), id=f"cand-{i}"))
                    yield ListView(*list_items, id="hc-cand-list")
                with Vertical(id="hc-right"):
                    with Horizontal(id="hc-top-right"):
                        with Vertical(id="hc-diff-pane"):
                            yield Label("Diff: Original Search vs Selected Target", classes="hc-title")
                            yield RichLog(id="hc-diff-view", highlight=True)
                        with Vertical(id="hc-replace-pane"):
                            yield Label("Replacement Code", classes="hc-title")
                            yield TextArea(self.replace_text, id="hc-replace-view", read_only=True)
                    with Vertical(id="hc-file-pane"):
                        yield Label("File Content (Select the WHOLE region to replace to avoid duplication!)", classes="hc-title")
                        yield TextArea(self.file_text, id="hc-file-text")
            with Horizontal(id="hc-footer"):
                yield Label("", id="hc-warning", classes="hc-warning-label")
                yield Button("Confirm Selection", id="btn-confirm", variant="success")
                yield Button("Cancel", id="btn-cancel", variant="error")

    def on_mount(self) -> None:
        if self.candidates:
            self.query_one("#hc-cand-list", ListView).index = 0
            self.action_scroll_to_candidate(0)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item and event.item.id and event.item.id.startswith("cand-"):
            idx = int(event.item.id.split("-")[1])
            self.action_scroll_to_candidate(idx)
            
    def action_scroll_to_candidate(self, idx: int) -> None:
        if 0 <= idx < len(self.candidates):
            c = self.candidates[idx]
            file_ta = self.query_one("#hc-file-text", TextArea)
            if Selection is not None:
                file_ta.selection = Selection((c["start_line"] - 1, 0), (c["end_line"] - 1, 9999))
            else:
                file_ta.move_cursor((c["start_line"] - 1, 0))
            file_ta.scroll_cursor_visible(center=True)
            self._render_candidate_diff(idx)

    def _render_candidate_diff(self, idx: int) -> None:
        c = self.candidates[idx]
        file_lines = self.file_text.splitlines(keepends=True)
        cand_lines = file_lines[c["start_line"] - 1 : c["end_line"]]
        cand_text = "".join(cand_lines)
        search_text = self.original_search
        
        diff_view = self.query_one("#hc-diff-view", RichLog)
        diff_view.clear()
        if search_text == cand_text:
            diff_view.write(Text("No changes detected.", style="dim"))
        else:
            render_word_diff(search_text, cand_text, diff_view)

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        if event.text_area.id == "hc-file-text" and self.confirm_armed:
            self.confirm_armed = False
            self.query_one("#hc-warning", Label).update("")
            btn = self.query_one("#btn-confirm", Button)
            btn.label = "Confirm Selection"
            btn.variant = "success"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            file_ta = self.query_one("#hc-file-text", TextArea)
            selected_text = file_ta.selected_text
            if not selected_text:
                self.app.notify("Please select some text in the File Content area first.", severity="error")
                return
            
            selected_lines = len(selected_text.splitlines())
            search_lines = self.candidates[0]["search_lines"] if self.candidates else 0
            
            if search_lines > 0 and (selected_lines < search_lines * 0.5):
                if not self.confirm_armed:
                    self.confirm_armed = True
                    warn_label = self.query_one("#hc-warning", Label)
                    warn_label.update(f"⚠️ Warning: Selected region ({selected_lines} lines) is much shorter than search block ({search_lines} lines). Duplication likely!")
                    btn = self.query_one("#btn-confirm", Button)
                    btn.label = "Confirm Anyway"
                    btn.variant = "error"
                    return

            self.dismiss(selected_text)
        elif event.button.id == "btn-cancel":
            self.dismiss(None)

class MacroScreen(ModalScreen):
    """TUI for applying changes step-by-step using a keyboard macro or clipboard interception."""
    CSS = """
    MacroScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }
    #macro-dialog {
        width: 75%;
        height: auto;
        max-height: 90%;
        border: solid #d08c60;
        background: #2d2825;
        padding: 1 2;
    }
    .macro-title {
        text-align: center;
        text-style: bold;
        color: #d08c60;
        margin-bottom: 1;
    }
    .macro-inst {
        text-align: center;
        text-style: bold;
        color: #ead6c9;
        margin-bottom: 1;
    }
    .macro-sub {
        text-align: center;
        color: #a0a0a0;
        margin-bottom: 1;
    }
    .macro-error {
        color: #ff5555;
        text-style: bold;
        margin: 1 0;
    }
    #macro-text-display {
        height: 12;
        border: tall #5a4d45;
        background: #1e1a18;
        color: #ead6c9;
        margin: 1 0;
    }
    #macro-diff-display {
        height: 12;
        border: tall #5a4d45;
        background: #1e1a18;
        margin: 1 0;
        display: none;
    }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "force_advance", "Force Next Step", show=True),
        Binding("space", "force_advance", "Force Next Step", show=False)
    ]

    def __init__(self, payload: dict, indices: list[int]):
        super().__init__()
        self.payload = payload
        self.indices = indices
        self.steps = []
        self.current_step_idx = 0
        self.is_executing = False
        self.hook = None
        self.completed_file_indices = set()
        self._build_steps()

    def _build_steps(self) -> None:
        for idx in self.indices:
            file_obj = self.payload.get("files", [])[idx]
            action = file_obj.get("action", "modify").upper()
            path = file_obj.get("path", "unknown")

            if action == "CREATE":
                self.steps.append({"type": "CREATE", "path": path, "content": file_obj.get("content", ""), "file_idx": idx})
            elif action == "DELETE":
                self.steps.append({"type": "DELETE", "path": path, "file_idx": idx})
            elif action == "COMMAND":
                self.steps.append({"type": "COMMAND", "command": file_obj.get("command", ""), "file_idx": idx})
            elif action == "MODIFY":
                if "content" in file_obj:
                    self.steps.append({"type": "CREATE", "path": path, "content": file_obj["content"], "file_idx": idx, "desc": "Overwrite file"})
                else:
                    self.steps.append({
                        "type": "MODIFY_WEB",
                        "path": path,
                        "blocks": file_obj.get("search_replace", []),
                        "regex_blocks": file_obj.get("regex_replace", []),
                        "file_idx": idx,
                        "sub_state": "WAITING_COPY"
                    })

    def compose(self) -> ComposeResult:
        with Vertical(id="macro-dialog"):
            yield Label("Web Assistant Mode Active", classes="macro-title")
            yield Label("", id="macro-file", classes="macro-inst")
            yield Label("", id="macro-action", classes="macro-inst")
            yield Label("", id="macro-error-label", classes="macro-error")
            yield TextArea(id="macro-text-display", read_only=True)
            yield RichLog(id="macro-diff-display", highlight=True)
            yield Label("", id="macro-trigger-label", classes="macro-sub")
            yield Label("", id="macro-progress", classes="macro-sub")

    def on_mount(self) -> None:
        if KEYBOARD_AVAILABLE:
            self.hook = keyboard.add_hotkey('+', self.on_hotkey, suppress=True)
        self.last_clipboard = ""
        self.set_interval(0.5, self.check_clipboard_poll)
        self._render_step()

    def on_unmount(self) -> None:
        if self.hook and KEYBOARD_AVAILABLE:
            keyboard.remove_hotkey(self.hook)

    def _render_step(self) -> None:
        if self.current_step_idx >= len(self.steps):
            self.dismiss(list(self.completed_file_indices))
            return
            
        step = self.steps[self.current_step_idx]
        stype = step["type"]
        
        self.query_one("#macro-file", Label).update(f"File: [bold cyan]{step['path']}[/bold cyan]")
        self.query_one("#macro-error-label", Label).update("")
        
        action_text = ""
        text_to_show = ""
        trigger_hint = "Press [bold green]+[/bold green] or [bold green]Enter[/bold green] to continue."

        text_display = self.query_one("#macro-text-display", TextArea)
        diff_display = self.query_one("#macro-diff-display", RichLog)
        
        text_display.display = True
        diff_display.display = False

        if stype == "CREATE" or "desc" in step:
            action_text = f"Action: Create/Overwrite File"
            text_to_show = step.get("content", "")
            trigger_hint = "Press [bold green]+[/bold green] inside your editor to Paste (or Copy from here manually)."
        elif stype == "DELETE":
            action_text = f"Action: Delete File"
            trigger_hint = "Please delete the file manually, then press [bold green]Enter[/bold green] here."
        elif stype == "COMMAND":
            self.query_one("#macro-file", Label).update(f"Command: [bold cyan]{step['command']}[/bold cyan]")
            action_text = f"Action: Run Command manually"
            text_to_show = step.get("command", "")
            trigger_hint = "Run this command manually in your environment, then press [bold green]Enter[/bold green] here."
        elif stype == "MODIFY_WEB":
            if step.get("sub_state") == "WAITING_COPY":
                action_text = "[yellow]Action: COPY ENTIRE FILE[/yellow]"
                text_to_show = "1. Go to your IDE/Editor.\n2. Select All (Ctrl+A).\n3. Copy (Ctrl+C)."
                trigger_hint = "[bold cyan]Waiting for clipboard...[/bold cyan] (Or press Enter to force)"
            elif step.get("sub_state") == "REVIEW_DIFF":
                action_text = "[magenta]Action: REVIEW CHANGES[/magenta]"
                errs = step.get("errors", [])
                if errs:
                    self.query_one("#macro-error-label", Label).update(f"MISSING BLOCKS IN {step.get('path', 'unknown')}:\n" + "\n".join(errs))
                trigger_hint = "Press [bold green]Enter[/bold green] to accept changes and copy to clipboard."
                text_display.display = False
                diff_display.display = True
                diff_display.clear()
                render_word_diff(step["old_text"], step["new_text"], diff_display)
                self.query_one("#macro-action", Label).update(action_text)
                self.query_one("#macro-trigger-label", Label).update(trigger_hint)
                self.query_one("#macro-progress", Label).update(f"Step {self.current_step_idx + 1} of {len(self.steps)}")
                return
            else:
                action_text = "[green]Action: PASTE MODIFIED FILE[/green]"
                errs = step.get("errors", [])
                if errs:
                    self.query_one("#macro-error-label", Label).update(f"MISSING BLOCKS IN {step.get('path', 'unknown')}:\n" + "\n".join(errs))
                text_to_show = "Modified content is on your clipboard.\n\n1. Go back to your editor.\n2. Make sure everything is still selected (Ctrl+A).\n3. Paste (Ctrl+V)."
                trigger_hint = "Press [bold green]Enter[/bold green] here once you have pasted the changes."
            
        self.query_one("#macro-action", Label).update(action_text)
        text_display.load_text(text_to_show)
        self.query_one("#macro-trigger-label", Label).update(trigger_hint)
        self.query_one("#macro-progress", Label).update(f"Step {self.current_step_idx + 1} of {len(self.steps)}")

    def check_clipboard_poll(self) -> None:
        if self.current_step_idx >= len(self.steps) or self.is_executing:
            return
        step = self.steps[self.current_step_idx]
        if step["type"] == "MODIFY_WEB" and step.get("sub_state") == "WAITING_COPY":
            try:
                current = pyperclip.paste()
                if current and current != self.last_clipboard:
                    blocks = step.get("blocks", [])
                    is_likely_file = False
                    if not blocks:
                        is_likely_file = True
                    else: 
                        for b in blocks[:5]:
                            if b.get("search", "") in current:
                                is_likely_file = True
                                break
                    if is_likely_file:
                        self.last_clipboard = current
                        self._trigger_processing(current)
            except Exception:
                pass

    def action_force_advance(self) -> None:
        if self.is_executing:
            return
        step = self.steps[self.current_step_idx]
        if step["type"] == "MODIFY_WEB":
            if step.get("sub_state") == "WAITING_COPY":
                self._trigger_processing(pyperclip.paste())
            elif step.get("sub_state") == "REVIEW_DIFF":
                new_text = step["new_text"]
                pyperclip.copy(new_text)
                self.last_clipboard = new_text
                step["sub_state"] = "WAITING_PASTE"
                self._render_step()
            else:
                self._advance()
        else:
            self._advance()

    def on_hotkey(self) -> None:
        if self.is_executing:
            return
        step = self.steps[self.current_step_idx]
        if step["type"] == "MODIFY_WEB":
            if step.get("sub_state") == "REVIEW_DIFF":
                new_text = step["new_text"]
                pyperclip.copy(new_text)
                self.last_clipboard = new_text
                step["sub_state"] = "WAITING_PASTE"
                self.app.call_from_thread(self._render_step)
            return
        self.is_executing = True
        try:
            time.sleep(0.4)
            keyboard.send('backspace')
            if step["type"] == "CREATE" or "desc" in step:
                pyperclip.copy(step["content"])
                time.sleep(0.1)
                keyboard.send('ctrl+v')
            elif step["type"] == "COMMAND":
                pyperclip.copy(step["command"])
        except Exception:
            pass
        self.app.call_from_thread(self._advance)

    def _trigger_processing(self, content: str) -> None:
        self.is_executing = True
        step = self.steps[self.current_step_idx]
        new_text = content.replace('\r\n', '\n')
        errors = []
        
        def _norm(t):
            return "\n".join(l.strip() for l in t.strip().split('\n') if l.strip())

        for i, block in enumerate(step.get("blocks", [])):
            s = block.get("search", "")
            r = block.get("replace", "")
            if s and s in new_text:
                new_text = new_text.replace(s, r, 1)
            else:
                ns = _norm(s)
                source_lines = new_text.split('\n')
                found = False
                for j in range(len(source_lines)):
                    for k in range(j, len(source_lines)):
                        window = '\n'.join(source_lines[j : k + 1])
                        window_norm = _norm(window)
                        if window_norm == ns:
                            new_text = new_text.replace(window, r, 1)
                            found = True
                            break
                        elif len(window_norm) > len(ns):
                            break
                    if found:
                        break
                if not found:
                    errors.append(f"Block {i+1} not found.")

        for i, block in enumerate(step.get("regex_blocks", [])):
            pattern = block.get("pattern", "")
            replacement = block.get("replacement", "")
            if pattern:
                try:
                    new_text = re.sub(pattern, replacement, new_text)
                except re.error as e:
                    errors.append(f"Regex block {i+1} error: {e}")
        step["errors"] = errors
        step["new_text"] = new_text
        step["old_text"] = content
        step["sub_state"] = "REVIEW_DIFF"
        self.is_executing = False
        self._render_step()

    def _advance(self) -> None:
        if self.current_step_idx >= len(self.steps):
            return
        step = self.steps[self.current_step_idx]
        file_idx = step["file_idx"]
        is_last_for_file = True
        for upcoming in self.steps[self.current_step_idx + 1:]:
            if upcoming["file_idx"] == file_idx:
                is_last_for_file = False
                break
        if is_last_for_file:
            self.completed_file_indices.add(file_idx)
        self.current_step_idx += 1
        self.is_executing = False
        self._render_step()

    def action_cancel(self) -> None:
        self.dismiss(list(self.completed_file_indices))

class PartialAddScreen(ModalScreen[str]):
    CSS = """
    PartialAddScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }
    #pa-dialog {
        width: 95%;
        height: 95%;
        border: solid #d08c60;
        background: #2d2825;
    }
    #pa-body {
        height: 1fr;
    }
    #pa-diff-pane {
        width: 50%;
        border-right: solid #5a4d45;
    }
    #pa-preview-pane {
        width: 50%;
    }
    .pa-title {
        background: #4a3f39;
        color: #d08c60;
        padding: 1;
        text-style: bold;
    }
    #pa-footer {
        height: 3; 
        border-top: solid #5a4d45;
        align: right middle;
    }
    Button {
        margin: 0 1;
    }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("w", "prev_change", "Previous", show=False),
        Binding("a", "prev_change", "Previous"),
        Binding("s", "next_change", "Next", show=False),
        Binding("d", "next_change", "Next"),
        Binding("space", "toggle_current", "Toggle"),
    ]

    def __init__(self, file_path: str, old_text: str, new_text: str):
        super().__init__()
        self.file_path = file_path
        self.old_text = old_text
        self.new_text = new_text
        self.old_lines = old_text.splitlines(keepends=True)
        self.new_lines = new_text.splitlines(keepends=True)
        self.tokens = []
        self.atoms = []
        self.cursor_index = 0

    def on_mount(self) -> None:
        log = self.query_one("#pa-hunk-diff", RichLog)
        log.auto_scroll = False
        matcher = difflib.SequenceMatcher(None, self.old_lines, self.new_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal':
                if i2 - i1 > 6:
                    if i1 != 0:
                        self.tokens.append("".join(self.old_lines[i1:i1+3]))
                    skip_start = i1 + 3 if i1 != 0 else i1
                    skip_end = i2 - 3 if i2 != len(self.old_lines) else i2
                    if skip_end > skip_start:
                        self.tokens.append({"type": "skip", "text": "".join(self.old_lines[skip_start:skip_end])})
                    if i2 != len(self.old_lines):
                        self.tokens.append("".join(self.old_lines[i2-3:i2]))
                else:
                    self.tokens.append("".join(self.old_lines[i1:i2]))
            else:
                old_hunk = "".join(self.old_lines[i1:i2])
                new_hunk = "".join(self.new_lines[j1:j2])
                old_words = re.findall(r'\S+|\s+', old_hunk)
                new_words = re.findall(r'\S+|\s+', new_hunk)
                word_matcher = difflib.SequenceMatcher(None, old_words, new_words)
                for w_tag, wi1, wi2, wj1, wj2 in word_matcher.get_opcodes():
                    if w_tag == 'equal':
                        self.tokens.append("".join(old_words[wi1:wi2]))
                    else:
                        atom = {
                            "id": len(self.atoms),
                            "tag": w_tag,
                            "old_text": "".join(old_words[wi1:wi2]),
                            "new_text": "".join(new_words[wj1:wj2]),
                            "accepted": True
                        }
                        self.atoms.append(atom)
                        self.tokens.append(atom)
        self._update_diff_view()
        self._update_preview()

    def action_next_change(self) -> None:
        if not self.atoms: return
        self.cursor_index = (self.cursor_index + 1) % len(self.atoms)
        self._update_diff_view()
        self._update_preview()
        
    def action_prev_change(self) -> None:
        if not self.atoms: return
        self.cursor_index = (self.cursor_index - 1) % len(self.atoms)
        self._update_diff_view()
        self._update_preview()
        
    def action_toggle_current(self) -> None:
        if not self.atoms or self.cursor_index is None: return
        self.atoms[self.cursor_index]["accepted"] = not self.atoms[self.cursor_index]["accepted"]
        self._update_diff_view()
        self._update_preview()

    def action_toggle_atom(self, a_id_str: str) -> None:
        a_id = int(a_id_str)
        if 0 <= a_id < len(self.atoms):
            self.cursor_index = a_id
            self.atoms[a_id]["accepted"] = not self.atoms[a_id]["accepted"]
            self._update_diff_view()
            self._update_preview()

    def _update_diff_view(self) -> None:
        log = self.query_one("#pa-hunk-diff", RichLog)
        log.clear()
        current_line = Text()
        lines_written = 0
        target_line = 0
        
        def process_string(s: str, base_style: str, a_id: int = None):
            nonlocal current_line, lines_written, target_line
            parts = s.split('\n')
            style = Style.parse(base_style) if base_style else Style()
            if a_id is not None:
                style = style + Style(meta={"@click": f"toggle_atom('{a_id}')"})
                if a_id == self.cursor_index:
                    style = style + Style(reverse=True)
                    target_line = lines_written
            for i, part in enumerate(parts):
                if i > 0:
                    log.write(current_line)
                    lines_written += 1
                    current_line = Text()
                if part:
                    current_line.append(part, style=style)
                    
        for token in self.tokens:
            if isinstance(token, str):
                process_string(token, "")
            elif isinstance(token, dict) and token.get("type") == "skip":
                process_string("\n...\n", "bold dim")
            else:
                a_id = token["id"]
                if token["accepted"]:
                    if token["tag"] == "delete":
                        process_string(token["old_text"], "bold red strike", a_id)
                    elif token["tag"] in ("insert", "replace"):
                        process_string(token["new_text"], "bold green", a_id)
                else:
                    if token["tag"] == "insert":
                        process_string(token["new_text"], "dim red strike", a_id)
                    elif token["tag"] in ("replace", "delete"):
                        process_string(token["old_text"], "bold red", a_id)
        if len(current_line) > 0:
            log.write(current_line)
        def do_scroll():
            half_height = (log.size.height // 2) if log.size.height > 0 else 15
            log.scroll_y = max(0, target_line - half_height)
        self.set_timer(0.05, do_scroll)

    def _update_preview(self) -> None:
        res = []
        current_row = 0
        target_row = 0
        for t in self.tokens:
            text_to_add = ""
            if isinstance(t, str):
                text_to_add = t
            elif isinstance(t, dict) and t.get("type") == "skip":
                text_to_add = t["text"]
            else:
                if t.get("id") == self.cursor_index:
                    target_row = current_row
                if t["accepted"]:
                    if t["tag"] in ("insert", "replace"):
                        text_to_add = t["new_text"]
                else:
                    if t["tag"] in ("delete", "replace"):
                        text_to_add = t["old_text"]
            res.append(text_to_add)
            current_row += text_to_add.count('\n')
        
        ta = self.query_one("#pa-preview", TextArea)
        ta.load_text("".join(res))
        ta.move_cursor((target_row, 0))
        ta.scroll_cursor_visible(center=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-pa-accept-all":
            for atom in self.atoms:
                atom["accepted"] = True
            self._update_diff_view()
            self._update_preview()
        elif btn_id == "btn-pa-reject-all":
            for atom in self.atoms:
                atom["accepted"] = False
            self._update_diff_view()
            self._update_preview()
        elif btn_id == "btn-pa-apply":
            res = []
            for t in self.tokens:
                if isinstance(t, str):
                    res.append(t)
                elif isinstance(t, dict) and t.get("type") == "skip":
                    res.append(t["text"])
                else:
                    if t["accepted"]:
                        if t["tag"] in ("insert", "replace"):
                            res.append(t["new_text"])
                    else:
                        if t["tag"] in ("delete", "replace"):
                            res.append(t["old_text"])
            self.dismiss("".join(res))
        elif btn_id == "btn-pa-cancel":
            self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="pa-dialog"):
            with Horizontal(id="pa-body"):
                with Vertical(id="pa-diff-pane"):
                    yield Label(f"Diff for {os.path.basename(self.file_path)} (Click red/green words to toggle!)", classes="pa-title")
                    yield RichLog(id="pa-hunk-diff", highlight=True, wrap=True)
                with Vertical(id="pa-preview-pane"):
                    yield Label("Live Preview (Resulting File)", classes="pa-title")
                    yield TextArea(id="pa-preview", read_only=True)
            with Horizontal(id="pa-footer"):
                yield Button("Accept All", id="btn-pa-accept-all", variant="success")
                yield Button("Reject All", id="btn-pa-reject-all", variant="error")
                yield Button("Apply Custom", id="btn-pa-apply", variant="primary")
                yield Button("Cancel", id="btn-pa-cancel", variant="default")

class AutoAgentApp(App):
    """TUI for monitoring clipboard and applying AI execution changes."""
    CSS = """
    Screen { background: #2d2825; }
    Header { background: #d08c60; color: #2d2825; }
    Footer { background: #3c3431; }
    #layout { height: 100%; }
    #sidebar {
        width: 30%;
        border-right: solid #5a4d45;
        background: #241f1c;
    }
    #sidebar ListView { height: 1fr; }
    #sidebar ListItem { height: auto; }
    #main-area {
        width: 70%;
        padding: 1 2;
    }
    .panel-title {
        background: #4a3f39;
        color: #d08c60;
        text-align: center;
        padding: 1; 
        text-style: bold;
    }
    .action-row {
        height: 3; 
        margin-bottom: 1;
        align: right middle;
    }
    Button { margin-left: 1; }
    #diff-view {
        margin-top: 1;
        height: 1fr;
        border: solid #5a4d45;
        background: #241f1c;
    }
    #diff-view:focus {
        border: double #d08c60;
    }
    #ai-markdown {
        height: auto;
        max-height: 40%;
        overflow-y: auto;
        border-bottom: solid #5a4d45;
    }
    #file-header {
        background: #3c3431;
        color: #ead6c9;
        padding: 0 1;
        height: auto;
        border-bottom: solid #5a4d45;
        margin-bottom: 1;
    }
    Screen.mobile #layout {
        layout: vertical;
    }
    Screen.mobile #sidebar {
        width: 100%;
        height: 40%;
        border-right: none;
        border-bottom: solid #5a4d45;
    }
    Screen.mobile #main-area {
        width: 100%;
        height: 60%;
        padding: 0 1;
    }
    Screen.mobile #global-action-bar,
    Screen.mobile #file-action-bar {
        display: none;
    }
    Screen.mobile #ai-markdown {
        max-height: 25%;
    }
    """
    BINDINGS = [
        Binding("escape", "quit", "Quit"),
        Binding("v", "paste_buffer", "Paste Payload"),
        Binding("V", "paste_editor", "Paste via Editor"),
        Binding("a", "apply_file", "Apply File"),
        Binding("t", "practice", "Practice (Rehab)"),
        Binding("p", "partial_add", "Partial Add"),
        Binding("A", "apply_all", "Apply All"),
        Binding("c", "commit", "Commit"),
        Binding("d", "discard_file", "Discard File"),
        Binding("D", "discard_all", "Discard All"),
        Binding("r", "reload", "Reload Clipboard"),
        Binding("e", "copy_error", "Copy Error"),
        Binding("h", "human_correct", "Human Correct"),
        Binding("m", "open_meld", "Open in Meld"),
        Binding("f", "fix_json", "Fix JSON"),
    ]
    TITLE = "CombineCopy — Auto Agent Listener"
    def __init__(self, root_dir: str, known_files: list[str] | None = None, revert_mode: bool = False, ignore_initial_clipboard: bool = False, web_mode: bool = False, tfs_mode: bool = False, xml_mode: bool = False, consult_mode: bool = False, rehab_mode: bool = False, mobile_mode: bool = False, inbox=None):
        super().__init__()
        self.root_dir = root_dir
        self.known_files = known_files or []
        self.revert_mode = revert_mode
        self.mobile_mode = mobile_mode
        self.inbox = inbox if inbox is not None else (PayloadInbox() if mobile_mode else None)
        # Web macro mode needs a global hotkey hook, which is unavailable on Termux.
        self.web_mode = web_mode and not mobile_mode
        self.tfs_mode = tfs_mode
        self.xml_mode = xml_mode
        self.consult_mode = consult_mode
        self.rehab_mode = rehab_mode
        self.is_consulting = False
        self._meld_running = False
        self.ignore_initial_clipboard = ignore_initial_clipboard
        self.last_clipboard = ""
        self.payload = None
        self.polling_timer = None
        self.json_error_text = None
        self.broken_json_content = ""
        self.is_loading_payload = False
        self.session_applied_files = []
        if self.revert_mode:
            self.title = "CombineCopy — Auto Agent Listener (REVERT MODE)"
        if self.web_mode:
            self.title = "CombineCopy — Auto Agent Listener (WEB MACRO MODE)"
        if self.tfs_mode:
            self.title = "CombineCopy — Auto Agent Listener (TFS MODE)"
        if self.rehab_mode:
            self.title = "CombineCopy — Auto Agent Listener (REHAB MODE)"
        if self.mobile_mode:
            self.title = "CombineCopy — Mobile Listener"

    def action_reload(self) -> None:
        if self.mobile_mode:
            self._reload_mobile()
            return
        self.last_clipboard = ""
        self.query_one("#status-label", Label).update("[bold yellow]Reloading clipboard...[/bold yellow]")
        self.check_clipboard()

    def _reload_mobile(self) -> None:
        """Manual ingest: inbox drop first, then one deliberate clipboard read."""
        text = read_latest_dropped_file()
        source = "inbox drop"
        if not text:
            text = read_text_once()
            source = "clipboard"
        if not text:
            self.notify(
                f"Nothing in {INBOX_DIR} or on the clipboard. Press 'v' to paste manually.",
                severity="warning",
            )
            return
        self.notify(f"Ingested {len(text)} chars from {source}.", severity="information")
        self.last_clipboard = ""
        self._process_incoming(text)

    def action_paste_buffer(self) -> None:
        self._open_paste_screen(auto_editor=False)

    def action_paste_editor(self) -> None:
        self._open_paste_screen(auto_editor=True)

    def _open_paste_screen(self, auto_editor: bool = False, initial_text: str = "") -> None:
        if getattr(self, "is_loading_payload", False):
            return
        from combinecopy.tui.paste import PasteBufferScreen
        self.app.push_screen(
            PasteBufferScreen(initial_text=initial_text, auto_editor=auto_editor),
            callback=self._on_paste_result,
        )

    def _on_paste_result(self, text: str | None) -> None:
        if not text:
            return
        self.last_clipboard = ""
        self._process_incoming(text.strip())

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="layout"):
            with Vertical(id="sidebar"):
                yield Label("Waiting for AI...", id="status-label", classes="panel-title")
                yield ListView(id="file-list")
            with Vertical(id="main-area"):
                yield Label("Select a file to inspect changes", id="file-header")
                with Horizontal(classes="action-row", id="global-action-bar"):
                    yield Button("Apply All (Shift+A)", id="btn-apply-all", variant="success", disabled=True)
                    yield Button("Discard All (Shift+D)", id="btn-discard-all", variant="error", disabled=True)
                    yield Button("Commit (c)", id="btn-commit", variant="primary", disabled=True)
                    yield Button("Fix JSON (f)", id="btn-fix-json", variant="warning", disabled=True)
                with Horizontal(classes="action-row", id="file-action-bar"):
                    yield Button("Apply File (a)", id="btn-apply-file", variant="success", disabled=True)
                    yield Button("Practice (t)", id="btn-practice", variant="primary", disabled=True)
                    yield Button("Partial Add (p)", id="btn-partial-add", variant="warning", disabled=True)
                    yield Button("Discard File (d)", id="btn-discard-file", variant="error", disabled=True)
                    yield Button("Human Correct (h)", id="btn-human-correct", variant="warning", disabled=True)
                    yield Button("Meld Diff (m)", id="btn-open-meld", variant="primary", disabled=True)
                yield Markdown("*(AI output will appear here)*", id="ai-markdown")
                yield RichLog(id="diff-view", highlight=True)
        yield Footer()
    def on_mount(self) -> None:
        if self.mobile_mode:
            self.screen.add_class("mobile")
            self.query_one("#status-label", Label).update("Press 'v' to paste, 'V' for editor")
            # The mobile loop only drains an in-process queue, so 1s is plenty and
            # avoids spinning the CPU on phone battery.
            self.polling_timer = self.set_interval(1.0, self.check_clipboard)
            self.query_one("#diff-view", RichLog).write(
                "No payload loaded. Press 'v' to open the paste buffer, 'V' for the editor."
            )
            self.call_after_refresh(self.action_paste_buffer)
            return
        if self.ignore_initial_clipboard:
            try:
                self.last_clipboard = pyperclip.paste().strip()
            except Exception:
                pass
        self.polling_timer = self.set_interval(0.5, self.check_clipboard)
        self.query_one("#diff-view", RichLog).write("Select a file to view diffs.")
    def check_clipboard(self) -> None:
        """Polling entry point. Selects the ingest source for the current mode."""
        if self.mobile_mode:
            # Android restricts clipboard reads to the focused app, so the mobile
            # loop never touches it. Payloads arrive through the PayloadInbox.
            if self.inbox is None:
                return
            item = self.inbox.drain()
            if item:
                self._process_incoming(item.get("text", ""))
            return
        try:
            content = pyperclip.paste().strip()
        except Exception:
            return
        if not content or content == self.last_clipboard:
            return
        self._process_incoming(content)

    def _process_incoming(self, content: str) -> None:
        """Parses one candidate payload string, whatever source it came from."""
        try:
            content = (content or "").strip()
            if not content:
                return

            # Check for consult results FIRST if we are consulting
            if getattr(self, 'is_consulting', False):
                answers = extract_consult_answers(content)
                if answers:
                    self.last_clipboard = content
                    self.app.pop_screen()
                    self.is_consulting = False
                    self._finish_consultation(answers)
                    return
                    
            self.last_clipboard = content
            
            # Check XML first
            if '<antigravity_payload>' in content:
                xml_blocks = extract_xml_from_text(content)
                for xml_str in xml_blocks:
                    data = parse_xml_to_dict(xml_str)
                    if data:
                        if data.get("phase") == "EXECUTION" and "files" in data:
                            self.load_payload(data)
                            return
                        elif data.get("phase") == "TASK" and "tasks" in data:
                            self.exit({"type": "task_division", "data": data})
                            return
                        elif data.get("phase") == "CONSULT":
                            self.load_consult_payload(data)
                            return
                            
            # Fallback to JSON
            if '"phase":' in content and ('"EXECUTION"' in content or '"CONSULT"' in content or '"TASK"' in content):
                json_blocks = extract_json_from_text(content)
                for json_str in json_blocks:
                    try:
                        data = json.loads(json_str)
                        if isinstance(data, dict):
                            if data.get("phase") == "EXECUTION" and "files" in data:
                                self.load_payload(data)
                                return
                            elif data.get("phase") == "TASK" and "tasks" in data:
                                self.exit({"type": "task_division", "data": data})
                                return
                            elif data.get("phase") == "CONSULT" and getattr(self, 'consult_mode', False):
                                self.load_consult_payload(data)
                                return
                    except json.JSONDecodeError as e:
                        fixed_data, fixed_str = intelligent_json_fix(json_str)
                        if fixed_data and isinstance(fixed_data, dict):
                            if fixed_data.get("phase") == "EXECUTION" and "files" in fixed_data:
                                self.notify("Intelligently auto-fixed JSON syntax errors!", title="Auto-Fix Success", severity="info")
                                self.load_payload(fixed_data)
                                return
                            elif fixed_data.get("phase") == "CONSULT":
                                self.load_consult_payload(fixed_data)
                                return
                        if '"EXECUTION"' in json_str and '"phase"' in json_str:
                            self.show_json_error(e, json_str)
                            return
                    except Exception:
                        continue
        except Exception:
            pass

    def load_consult_payload(self, data: dict) -> None:
        from combinecopy.tui.consult import ConsultationScreen
        self.is_consulting = True
        self.query_one("#status-label", Label).update("[bold cyan]Consultation Phase Active[/bold cyan]")
        self.app.push_screen(ConsultationScreen(data, self.xml_mode), self.on_consult_cancelled)
        
    def on_consult_cancelled(self, cancelled: bool | None) -> None:
        if cancelled:
            self.is_consulting = False
            self.query_one("#status-label", Label).update("Waiting for AI...")
            
    def _finish_consultation(self, answers: dict) -> None:
        buffer = ["--- RESEARCH RESULTS ---", "Here is the external knowledge you requested. Use this to formulate your PLANNING and EXECUTION.\n"]
        for q_id, ans in answers.items():
            buffer.append(f"=====\nQuery ID: {q_id}\n=====\n{ans}\n")
        buffer.append("\n--- SYSTEM REMINDER ---")
        buffer.append("You have completed the CONSULT phase. Please enter PLANNING mode or EXECUTION mode to proceed.")
        
        final_text = "\n".join(buffer)
        if copy_to_clipboard(final_text):
            self.notify("Consultation results formatted and copied to clipboard!", title="Success")
        self.query_one("#status-label", Label).update("Waiting for AI...")
        self.query_one("#ai-markdown", Markdown).update("**Consultation Complete!**\n\nThe external answers have been copied to your clipboard. Paste them back to the local AI.")

    def _normalize_text(self, text: str) -> str:
        return "\n".join(
            line.strip()
            for line in text.strip().split('\n')
            if line.strip()
        )

    def show_json_error(self, error: json.JSONDecodeError, content: str) -> None:
        self.polling_timer.pause()
        self.broken_json_content = content
        self.query_one("#status-label", Label).update("[bold red]JSON Parse Error[/bold red]")
        lines = content.split("\n")
        error_line = lines[error.lineno - 1] if 0 < error.lineno <= len(lines) else ""
        self.json_error_text = (
            f"Your execution JSON failed to parse due to a syntax error:\n"
            f"{error.msg} on line {error.lineno}, column {error.colno}\n\n"
            f"Failing line context:\n"
            f"{error_line}"
        )
        error_md = (
            f"### ❌ Invalid JSON from AI\n\n"
            f"**Option 1 (Recommended):** Press **'f'** to instantly open and fix the JSON locally.\n"
            f"**Option 2:** Press **'e'** to copy the error below and send it to the LLM to fix.\n\n"
            f"```text\n"
            f"{self.json_error_text}\n"
            f"```"
        )
        self.query_one("#ai-markdown", Markdown).update(error_md)
        self.query_one("#file-list", ListView).clear()
        
        diff_view = self.query_one("#diff-view", RichLog)
        diff_view.clear()
        diff_view.write(f"JSON Exception: {error.msg}\nLine: {error.lineno}\nColumn: {error.colno}\n\nContext:\n{error_line}")
        self._disable_all_buttons()
        self.query_one("#btn-discard-all", Button).disabled = False
        if self.query("Button#btn-fix-json"):
            self.query_one("#btn-fix-json", Button).disabled = False
    def _find_partial_matches(self, search_text: str, file_text: str) -> list:
        search_lines = search_text.splitlines()
        file_lines = file_text.splitlines()
        if len(search_lines) <= 1: return []
        search_norm = [line.strip() for line in search_lines]
        file_norm = [line.strip() for line in file_lines]
        
        matcher = difflib.SequenceMatcher(None, search_norm, file_norm)
        blocks = matcher.get_matching_blocks()
        candidates = []
        for block in blocks:
            if block.size > 0:
                matched_text = "".join(search_norm[block.a : block.a + block.size])
                if not matched_text: continue
                
                start_line = max(1, block.b - block.a + 1)
                end_line = min(len(file_lines), block.b - block.a + len(search_lines))
                
                candidates.append({
                    "start_line": start_line,
                    "end_line": end_line,
                    "matched_lines": block.size,
                    "search_lines": len(search_lines),
                    "coverage": block.size / len(search_lines)
                })
                
        candidates.sort(key=lambda x: (x["matched_lines"], x["coverage"]), reverse=True)
        unique_cands = {}
        for c in candidates:
            key = (c["start_line"], c["end_line"])
            if key not in unique_cands:
                unique_cands[key] = c
            if len(unique_cands) >= 5:
                break
                
        return list(unique_cands.values())

    def _validate_file_obj(self, file_obj: dict, status_callback=None) -> None:
        action = file_obj.get("action", "modify").upper()
        if action == "COMMAND":
            if "command" not in file_obj:
                file_obj["_errors"] = ["Missing 'command' key for COMMAND action."]
            else:
                file_obj["_errors"] = []
            return
            
        path = file_obj.get("path", "unknown")
        full_path = os.path.join(self.root_dir, path)
        errors = []
        if "_revert_error" in file_obj:
            errors.append(file_obj["_revert_error"])
        if not os.path.exists(full_path) and action != "CREATE":
            filename = os.path.basename(path)
            if self.known_files:
                matches = [f for f in self.known_files if os.path.basename(f) == filename]
                if len(matches) == 1:
                    correct_path_rel = os.path.relpath(matches[0], self.root_dir)
                    warn_msg = f"Path corrected from '{path}' to '{correct_path_rel}'."
                    if warn_msg not in file_obj.setdefault("_warnings", []):
                        file_obj["_warnings"].append(warn_msg)
                    file_obj["path"] = correct_path_rel
                    path = correct_path_rel
                    full_path = os.path.join(self.root_dir, path)
                elif len(matches) > 1:
                    if self.web_mode:
                        file_obj.setdefault("_warnings", []).append(f"Ambiguous file: '{filename}' found in multiple locations.")
                    else:
                        errors.append(f"Ambiguous file: '{filename}' found in multiple locations.")
                else:
                    if self.web_mode:
                        file_obj.setdefault("_warnings", []).append(f"Target file '{path}' does not exist locally.")
                    else:
                        errors.append(f"Target file '{path}' does not exist and was not found in context.")
            else:
                if self.web_mode:
                    file_obj.setdefault("_warnings", []).append(f"Target file '{path}' does not exist locally.")
                else:
                    errors.append(f"Target file '{path}' does not exist.")

        if action == "MODIFY" and not errors:
            if "regex_replace" in file_obj and os.path.exists(full_path):
                if status_callback: status_callback(f"Evaluating regex replacements for {path}...")
                old_text = safe_read_file(full_path)
                for b_idx, block in enumerate(file_obj.get("regex_replace", [])):
                    pattern = block.get("pattern", "")
                    if pattern:
                        try:
                            compiled = re.compile(pattern)
                            if not compiled.search(old_text):
                                warn_msg = f"Regex pattern '{pattern}' found no matches."
                                if warn_msg not in file_obj.setdefault("_warnings", []):
                                    file_obj["_warnings"].append(warn_msg)
                        except re.error as e:
                            errors.append(f"Invalid regex pattern '{pattern}': {e}")

            if "search_replace" in file_obj and os.path.exists(full_path):
                try:
                    if status_callback: status_callback(f"Reading {path}...")
                    old_text = safe_read_file(full_path)
                    for b_idx, block in enumerate(file_obj.get("search_replace", [])):
                        if status_callback: status_callback(f"Checking match {b_idx + 1}/{len(file_obj.get('search_replace', []))} in {path}...")
                        block.pop("_candidates", None)
                        if "replace" not in block:
                            errors.append(f"No replacement found for search block {b_idx + 1}.")
                        search_text = block.get("search", "")
                        if search_text and search_text not in old_text:
                            if status_callback: status_callback(f"Searching fuzzy match {b_idx + 1} in {path}...")
                            normalized_old = self._normalize_text(old_text)
                            normalized_search = self._normalize_text(search_text)
                            if normalized_search in normalized_old:
                                source_lines = old_text.split('\n')
                                found_exact = False
                                for i in range(len(source_lines)):
                                    for j in range(i, len(source_lines)):
                                        window = '\n'.join(source_lines[i : j + 1])
                                        nw = self._normalize_text(window)
                                        if nw == normalized_search:
                                            block['search'] = window
                                            warn_msg = f"Used fuzzy matching for search block {b_idx + 1}."
                                            if warn_msg not in file_obj.setdefault("_warnings", []):
                                                file_obj["_warnings"].append(warn_msg)
                                            found_exact = True
                                            break
                                        elif len(nw) > len(normalized_search):
                                            break
                                    if found_exact:
                                        break
                                if not found_exact:
                                    errors.append(f"Fuzzy match found but couldn't map to original text for block {b_idx+1}.")
                            else:
                                if status_callback: status_callback(f"Searching partial matches {b_idx + 1} in {path} (this can take a while)...")
                                candidates = self._find_partial_matches(search_text, old_text)
                                if candidates:
                                    block["_candidates"] = candidates
                                    block["_original_search"] = search_text
                                    best_cand = candidates[0]
                                    cov_pct = int(best_cand["coverage"] * 100)
                                    errors.append(f"Search block {b_idx + 1} not found. Found partial match covering {best_cand['matched_lines']}/{best_cand['search_lines']} lines ({cov_pct}%) near lines {best_cand['start_line']}-{best_cand['end_line']}. Press 'h' to resolve.")
                                else:
                                    errors.append(f"Search block {b_idx + 1} not found. Fuzzy match and partial match also failed.")
                except Exception as e:
                    errors.append(f"Error reading file: {e}")
        file_obj["_errors"] = errors

    def load_payload(self, data: dict) -> None:
        self.polling_timer.pause()
        self.is_loading_payload = True
        self.query_one("#status-label", Label).update("[bold cyan]Starting payload validation...[/bold cyan]")
        thread = threading.Thread(target=self._background_load_payload, args=(data,), daemon=True)
        thread.start()

    def _background_load_payload(self, data: dict) -> None:
        try:
            for file_obj in data.get("files", []):
                for block in file_obj.get("search_replace", []):
                    if "replacement" in block and "replace" not in block:
                        block["replace"] = block.pop("replacement")

            if getattr(self, 'revert_mode', False):
                data["commit_message"] = "Revert: " + data.get("commit_message", "")
                for file_obj in data.get("files", []):
                    action = file_obj.get("action", "modify").lower()
                    if action == "command":
                        file_obj["_revert_warning"] = "Commands cannot be automatically reverted. Please verify manually."
                    elif action == "create":
                        file_obj["action"] = "delete"
                    elif action == "delete":
                        file_obj["action"] = "create"
                        file_obj["content"] = ""
                        file_obj["_revert_warning"] = "Reverting a delete will create an empty file."
                    elif action == "modify":
                        if "search_replace" in file_obj:
                            new_sr = []
                            for block in reversed(file_obj.get("search_replace", [])):
                                new_sr.append({
                                    "search": block.get("replace", ""),
                                    "replace": block.get("search", "")
                                })
                            file_obj["search_replace"] = new_sr
                        elif "content" in file_obj:
                            file_obj["_revert_error"] = "Cannot revert a full file overwrite without original content."
            
            def status_cb(msg):
                def update_lbl():
                    try:
                        self.query_one("#status-label", Label).update(f"[bold cyan]{msg}[/bold cyan]")
                    except Exception:
                        pass
                self.call_from_thread(update_lbl)

            files_list = data.get("files", [])
            for idx, file_obj in enumerate(files_list):
                file_obj["_status"] = "pending"
                file_obj.setdefault("_warnings", [])
                if "_revert_warning" in file_obj:
                    file_obj["_warnings"].append(file_obj.pop("_revert_warning"))
                
                status_cb(f"Validating file {idx + 1}/{len(files_list)}: {file_obj.get('path', 'unknown')}")
                self._validate_file_obj(file_obj, status_callback=status_cb)
            
            self.call_from_thread(self._finish_load_payload, data)
        except Exception as e:
            self.call_from_thread(self._cancel_load_payload, str(e))

    def _finish_load_payload(self, data: dict) -> None:
        self.payload = data
        self.is_loading_payload = False
        self.query_one("#status-label", Label).update("Files waiting to be changed")
        self.query_one("#ai-markdown", Markdown).update(data.get("markdown", "No markdown provided."))
        self.refresh_file_list()
        file_list = self.query_one("#file-list", ListView)
        if len(file_list) > 0:
            file_list.index = 0

    def _cancel_load_payload(self, err: str) -> None:
        self.is_loading_payload = False
        self.notify(f"Error validating payload: {err}", severity="error")
        self.reset_state()

    def _disable_all_buttons(self) -> None:
        self.query_one("#btn-apply-all", Button).disabled = True
        self.query_one("#btn-discard-all", Button).disabled = True
        self.query_one("#btn-commit", Button).disabled = True
        self.query_one("#btn-apply-file", Button).disabled = True
        self.query_one("#btn-discard-file", Button).disabled = True
        if self.query("Button#btn-practice"):
            self.query_one("#btn-practice", Button).disabled = True
        if self.query("Button#btn-partial-add"):
            self.query_one("#btn-partial-add", Button).disabled = True
        if self.query("Button#btn-human-correct"):
            self.query_one("#btn-human-correct", Button).disabled = True
        if self.query("Button#btn-open-meld"):
            self.query_one("#btn-open-meld", Button).disabled = True
        if self.query("Button#btn-fix-json"):
            self.query_one("#btn-fix-json", Button).disabled = True

    def refresh_file_list(self) -> None:
        if not self.payload: return
        file_list = self.query_one("#file-list", ListView)
        current_idx = file_list.index
        file_list.clear()
        for idx, file_obj in enumerate(self.payload.get("files", [])):
            action = file_obj.get("action", "modify").upper()
            if action == "COMMAND":
                path = file_obj.get("command", "unknown command")
            else:
                path = file_obj.get("path", "unknown")
            status = file_obj.get("_status", "pending")
            errors = file_obj.get("_errors", [])
            warnings = file_obj.get("_warnings", [])
            if status == "applied":
                status_marker = " [bold green]✓[/bold green]"
                style = "dim"
            elif status == "discarded":
                status_marker = " [bold red]✗[/bold red]"
                style = "strike dim"
            else:
                status_marker = ""
                style = ""
            color = "green" if action == "CREATE" else "yellow" if action == "MODIFY" else "red"
            err_marker = " [bold red](Error)[/bold red]" if errors else ""
            path_text = f"[bold red]{path}[/bold red]" if errors else path
            warn_marker = ""
            if "Path corrected" in "".join(warnings):
                warn_marker += " [yellow](Path Corrected)[/yellow]"
            if any("fuzzy matching" in w for w in warnings):
                warn_marker += " [yellow](Fuzzy Match)[/yellow]"
            if any("Human corrected" in w for w in warnings):
                warn_marker += " [yellow](Human Corrected)[/yellow]"
            if any("Meld edited" in w for w in warnings):
                warn_marker += " [yellow](Meld Edited)[/yellow]"
            label_text = f"[{color}]{action}[/{color}] {path_text}{err_marker}{warn_marker}{status_marker}"
            unique_id = f"file-{idx}-{time.time_ns()}"
            item = ListItem(Label(label_text, classes=style), id=unique_id)
            file_list.append(item)
        if current_idx is not None and current_idx < len(file_list):
            file_list.index = current_idx
            self._render_diff_for_index(current_idx)
        self._update_buttons()

    def _update_buttons(self) -> None:
        if not self.payload:
            self._disable_all_buttons()
            return
        files = self.payload.get("files", [])
        has_pending = any(f.get("_status") == "pending" for f in files)
        has_applied = any(f.get("_status") == "applied" for f in files)
        self.query_one("#btn-apply-all", Button).disabled = not has_pending
        self.query_one("#btn-discard-all", Button).disabled = not has_pending
        self.query_one("#btn-commit", Button).disabled = not has_applied
        
        file_list = self.query_one("#file-list", ListView)
        if file_list.index is not None and file_list.index < len(files):
            selected_file = files[file_list.index]
            is_pending = selected_file.get("_status") == "pending"
            action = selected_file.get("action", "").upper()
            self.query_one("#btn-apply-file", Button).disabled = not is_pending
            
            if self.query("Button#btn-practice"):
                self.query_one("#btn-practice", Button).disabled = not is_pending or action == "COMMAND"
                
            if action == "COMMAND":
                if self.query("Button#btn-partial-add"):
                    self.query_one("#btn-partial-add", Button).disabled = True
                if self.query("Button#btn-open-meld"):
                    self.query_one("#btn-open-meld", Button).disabled = True
                if self.query("Button#btn-human-correct"):
                    self.query_one("#btn-human-correct", Button).disabled = True
            else:
                if self.query("Button#btn-partial-add"):
                    self.query_one("#btn-partial-add", Button).disabled = not is_pending
                if self.query("Button#btn-open-meld"):
                    self.query_one("#btn-open-meld", Button).disabled = not is_pending or self._meld_running
                has_candidates = False
                for block in selected_file.get("search_replace", []):
                    if "_candidates" in block:
                        has_candidates = True
                        break
                if self.query("Button#btn-human-correct"):
                    self.query_one("#btn-human-correct", Button).disabled = not (is_pending and has_candidates)
                    
            self.query_one("#btn-discard-file", Button).disabled = not is_pending
        else:
            self.query_one("#btn-apply-file", Button).disabled = True
            if self.query("Button#btn-practice"):
                self.query_one("#btn-practice", Button).disabled = True
            if self.query("Button#btn-partial-add"):
                self.query_one("#btn-partial-add", Button).disabled = True
            self.query_one("#btn-discard-file", Button).disabled = True
            self.query_one("#btn-human-correct", Button).disabled = True
            self.query_one("#btn-open-meld", Button).disabled = True

    def _render_diff_for_index(self, idx: int) -> None:
        if not self.payload or idx < 0 or idx >= len(self.payload.get("files", [])): return
        self._update_buttons()
        file_obj = self.payload["files"][idx]
        action = file_obj.get("action", "").upper()
        
        if action == "COMMAND":
            header_text = Text()
            header_text.append("Command: ", style="bold cyan")
            header_text.append(file_obj.get("command", ""), style="bold yellow")
            self.query_one("#file-header", Label).update(header_text)
            
            diff_view = self.query_one("#diff-view", RichLog)
            diff_view.clear()
            diff_view.write(Text("This is a CLI command. Click 'Apply File' to execute it.", style="bold cyan"))
            return
            
        path = file_obj.get("path")
        dirname = os.path.dirname(path)
        filename = os.path.basename(path)
        header_text = Text()
        header_text.append("Target: ", style="bold cyan")
        if dirname:
            header_text.append(f"{dirname}/", style="dim")
        header_text.append(filename, style="bold yellow")
        self.query_one("#file-header", Label).update(header_text)
        
        full_path = os.path.join(self.root_dir, path)
        old_text = ""
        if os.path.exists(full_path):
            try:
                old_text = safe_read_file(full_path)
            except Exception:
                old_text = "[Error reading existing file]\n"
        new_text = compute_new_text(file_obj, old_text)
        diff_view = self.query_one("#diff-view", RichLog)
        diff_view.clear()
        
        header_text = ""
        errors = file_obj.get("_errors", [])
        warnings = file_obj.get("_warnings", [])
        if warnings:
            warn_header = f"⚠️ AUTOMATED CORRECTIONS APPLIED FOR {file_obj.get('path', 'unknown')}\n"
            for warn in warnings:
                warn_header += f" - {warn}\n"
            warn_header += "=" * 60 + "\n\n"
            header_text += warn_header
        if errors:
            error_header = f"⛔️ ACTION FAILED VALIDATION FOR {file_obj.get('path', 'unknown')}\n"
            for err in errors:
                error_header += f" - {err}\n"
            error_header += "\nCopy this error and give it to the AI to correct its search block.\n"
            error_header += "=" * 60 + "\n\n"
            header_text += error_header
        if header_text:
            style = "bold red" if errors else "bold yellow"
            diff_view.write(Text(header_text, style=style))
        if old_text == new_text:
            diff_view.write(Text("No changes detected.", style="dim"))
            return
        render_word_diff(old_text, new_text, diff_view)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None and event.item.id and event.item.id.startswith("file-"):
            idx = int(event.item.id.split("-")[1])
            self._render_diff_for_index(idx)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item is not None and event.item.id and event.item.id.startswith("file-"):
            idx = int(event.item.id.split("-")[1])
            self._render_diff_for_index(idx)

    def _check_auto_reset(self) -> None:
        files = self.payload.get("files", [])
        has_pending = any(f.get("_status") == "pending" for f in files)
        has_applied = any(f.get("_status") == "applied" for f in files)
        if not has_pending and not has_applied:
            self.reset_state()
    def action_practice(self) -> None:
        btn = self.query_one("#btn-practice", Button)
        if not btn.disabled:
            file_list = self.query_one("#file-list", ListView)
            if file_list.index is not None:
                file_obj = self.payload["files"][file_list.index]
                full_path = os.path.join(self.root_dir, file_obj["path"])
                old_text = safe_read_file(full_path) if os.path.exists(full_path) else ""
                self.app.push_screen(
                    RehabScreen(file_obj, self.root_dir, old_text),
                    callback=lambda success: self.on_rehab_done(file_list.index, success)
                )

    def action_apply_file(self) -> None:
        btn = self.query_one("#btn-apply-file", Button)
        if not btn.disabled:
            file_list = self.query_one("#file-list", ListView)
            if file_list.index is not None:
                file_obj = self.payload["files"][file_list.index]
                if file_obj.get("action", "").upper() == "COMMAND":
                    self.app.push_screen(
                        CommandExecutionScreen(file_obj.get("command", ""), self.root_dir),
                        callback=lambda success: self.on_command_done(file_list.index, success)
                    )
                elif getattr(self, 'rehab_mode', False):
                    full_path = os.path.join(self.root_dir, file_obj["path"])
                    old_text = safe_read_file(full_path) if os.path.exists(full_path) else ""
                    self.app.push_screen(
                        RehabScreen(file_obj, self.root_dir, old_text),
                        callback=lambda success: self.on_rehab_done(file_list.index, success)
                    )
                elif self.web_mode:
                    self.app.push_screen(MacroScreen(self.payload, [file_list.index]), self.on_macro_done)
                else:
                    self._apply_single_file(file_list.index)
                    self.refresh_file_list()
    def on_command_done(self, idx: int, success: bool) -> None:
        self.payload["files"][idx]["_status"] = "applied"
        self._record_applied_file(self.payload["files"][idx])
        self.refresh_file_list()
        self._check_auto_reset()

    def on_rehab_done(self, idx: int, success: bool) -> None:
        if success:
            file_obj = self.payload["files"][idx]
            file_obj["_status"] = "applied"
            self._record_applied_file(file_obj)
            self.refresh_file_list()
            self._check_auto_reset()

    def action_partial_add(self) -> None:
        btn = self.query_one("#btn-partial-add", Button)
        if not btn.disabled and self.payload:
            file_list = self.query_one("#file-list", ListView)
            if file_list.index is not None:
                file_idx = file_list.index
                file_obj = self.payload["files"][file_idx]
                if file_obj.get("action", "").lower() != "modify":
                    self.notify("Partial Add is only available for modified files.", severity="warning")
                    return
                full_path = os.path.join(self.root_dir, file_obj["path"])
                old_text = safe_read_file(full_path) if os.path.exists(full_path) else ""
                new_text = compute_new_text(file_obj, old_text)
                if old_text == new_text:
                    self.notify("No changes detected to partially add.", severity="warning")
                    return
                self.app.push_screen(
                    PartialAddScreen(file_obj["path"], old_text, new_text),
                    callback=lambda result: self.on_partial_add_result(file_idx, result)
                )

    def on_partial_add_result(self, file_idx: int, resolved_text: str | None) -> None:
        if resolved_text is None: return
        file_obj = self.payload["files"][file_idx]
        file_obj["content"] = resolved_text
        file_obj.pop("search_replace", None)
        file_obj.pop("regex_replace", None)
        self._apply_single_file(file_idx)
        self.refresh_file_list()
        self._check_auto_reset()

    def action_discard_file(self) -> None:
        btn = self.query_one("#btn-discard-file", Button)
        if not btn.disabled:
            file_list = self.query_one("#file-list", ListView)
            if file_list.index is not None:
                self.payload["files"][file_list.index]["_status"] = "discarded"
                self.refresh_file_list()
                self._check_auto_reset()

    def action_apply_all(self) -> None:
        btn = self.query_one("#btn-apply-all", Button)
        if not btn.disabled:
            pending_indices = [i for i, f in enumerate(self.payload["files"]) if f.get("_status") == "pending"]
            if self.web_mode:
                self.app.push_screen(MacroScreen(self.payload, pending_indices), self.on_macro_done)
            else:
                self._apply_next_pending(pending_indices)

    def _apply_next_pending(self, indices: list[int]) -> None:
        if not indices:
            self.refresh_file_list()
            self._check_auto_reset()
            return
        idx = indices.pop(0)
        file_obj = self.payload["files"][idx]
        if file_obj.get("action", "").upper() == "COMMAND":
            self.app.push_screen(
                CommandExecutionScreen(file_obj.get("command", ""), self.root_dir),
                callback=lambda success: self._on_apply_all_command_done(idx, success, indices)
            )
        elif getattr(self, 'rehab_mode', False):
            full_path = os.path.join(self.root_dir, file_obj["path"])
            old_text = safe_read_file(full_path) if os.path.exists(full_path) else ""
            def callback(success):
                if success:
                    self.payload["files"][idx]["_status"] = "applied"
                    self._record_applied_file(self.payload["files"][idx])
                self._apply_next_pending(indices)
            self.app.push_screen(RehabScreen(file_obj, self.root_dir, old_text), callback=callback)
        else:
            self._apply_single_file(idx)
            self._apply_next_pending(indices)

    def _on_apply_all_command_done(self, idx: int, success: bool, remaining_indices: list[int]) -> None:
        self.payload["files"][idx]["_status"] = "applied"
        self._record_applied_file(self.payload["files"][idx])
        self._apply_next_pending(remaining_indices)

    def on_macro_done(self, completed_indices: list[int] | None) -> None:
        if completed_indices:
            for idx in completed_indices:
                self.payload["files"][idx]["_status"] = "applied"
                self._record_applied_file(self.payload["files"][idx])
            self.refresh_file_list()
            self._check_auto_reset()

    def action_discard_all(self) -> None:
        btn = self.query_one("#btn-discard-all", Button)
        if not btn.disabled:
            for f in self.payload["files"]:
                if f.get("_status") == "pending":
                    f["_status"] = "discarded"
            self.refresh_file_list()
            self._check_auto_reset()

    def action_commit(self) -> None:
        btn = self.query_one("#btn-commit", Button)
        if not btn.disabled:
            self.commit_changes()
            self.reset_state()

    def action_human_correct(self) -> None:
        file_list = self.query_one("#file-list", ListView)
        if file_list.index is not None and self.payload:
            file_obj = self.payload["files"][file_list.index]
            for b_idx, block in enumerate(file_obj.get("search_replace", [])):
                if "_candidates" in block:
                    full_path = os.path.join(self.root_dir, file_obj["path"])
                    old_text = safe_read_file(full_path)
                    self.app.push_screen(
                        HumanCorrectScreen(
                            file_path=file_obj["path"],
                            file_text=old_text,
                            original_search=block["_original_search"],
                            candidates=block["_candidates"],
                            replace_text=block.get("replace", "")
                        ),
                        callback=lambda selected_text, b=b_idx: self.on_human_correct_result(file_list.index, b, selected_text)
                    )
                    return
            self.notify("No fixable blocks found in this file.", severity="warning")
    def on_human_correct_result(self, file_idx: int, block_idx: int, selected_text: str | None) -> None:
        if selected_text is None: return
        file_obj = self.payload["files"][file_idx]
        block = file_obj["search_replace"][block_idx]
        old_len = len(block["search"].splitlines())
        new_len = len(selected_text.splitlines())
        block["search"] = selected_text
        block.pop("_candidates", None)
        block.pop("_original_search", None)
        if "_warnings" not in file_obj:
            file_obj["_warnings"] = []
        file_obj["_warnings"].append(f"Human corrected search block {block_idx + 1} ({old_len} -> {new_len} lines).")
        self._validate_file_obj(file_obj)
        self.refresh_file_list()
    def action_fix_json(self) -> None:
        btn = self.query_one("#btn-fix-json", Button)
        if btn.disabled or not hasattr(self, 'broken_json_content') or not self.broken_json_content:
            return
        if self.mobile_mode:
            # A terminal editor cannot be launched from the worker thread below
            # without corrupting the TUI, so mobile reuses the paste buffer's
            # suspend-based handoff instead.
            self._open_paste_screen(auto_editor=True, initial_text=self.broken_json_content)
            return
        btn.disabled = True
        thread = threading.Thread(target=self._fix_json_worker, args=(self.broken_json_content,), daemon=True)
        thread.start()
        self.notify("Waiting for external editor to close...", severity="info")
    def _fix_json_worker(self, current_text: str) -> None:
        fd, temp_path = tempfile.mkstemp(suffix=".json", text=True)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(current_text)
        from combinecopy.mobile.env import resolve_editor

        editor = resolve_editor()
        fallback = ["notepad"] if os.name == "nt" else ["vi"]
        cmd = (list(editor) if editor else fallback) + [temp_path]
        try:
            subprocess.run(cmd, check=False)
        except Exception as e:
            self.call_from_thread(self.notify, f"Editor failed to launch: {e}", severity="error")
        try:
            with open(temp_path, 'r', encoding='utf-8') as f:
                new_text = f.read()
            if new_text != current_text:
                pyperclip.copy(new_text)
                self.call_from_thread(self.notify, "Clipboard updated with fixed JSON!", title="Success")
                self.call_from_thread(self.action_reload)
        except Exception as e:
            self.call_from_thread(self.notify, f"Failed to read from editor: {e}", severity="error")
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            def re_enable():
                if self.query("Button#btn-fix-json"):
                    btn = self.query_one("#btn-fix-json", Button)
                    if hasattr(self, 'broken_json_content') and self.broken_json_content == current_text:
                        btn.disabled = False
            self.call_from_thread(re_enable)

    def action_copy_error(self) -> None: 
        if self.json_error_text:
            error_context = self.json_error_text
            prompt = (
                f"Your previous modification payload generated a validation error or search/replace mismatch. "
                f"Here is the exact failure information: {error_context}. "
                f"Re-analyze your target lines and output ONLY the corrected pure execution payload block needed to apply this file change successfully."
            )
            pyperclip.copy(prompt)
            self.notify("JSON error copied to clipboard!", title="Copied")
            return
        if not self.payload: return
        file_list = self.query_one("#file-list", ListView)
        if file_list.index is not None and file_list.index < len(self.payload["files"]):
            file_obj = self.payload["files"][file_list.index]
            errors = file_obj.get("_errors", [])
            if errors:
                error_context = f"ACTION FAILED VALIDATION FOR {file_obj.get('path', 'unknown')}\n"
                for err in errors:
                    error_context += f" - {err}\n"
                error_context = error_context.strip()
                prompt = (
                    f"Your previous modification payload generated a validation error or search/replace mismatch. "
                    f"Here is the exact failure information: {error_context}. "
                    f"Re-analyze your target lines and output ONLY the corrected pure execution payload block needed to apply this file change successfully."
)
                pyperclip.copy(prompt)
                self.notify("File validation error copied!", title="Copied")
            else:
                self.notify("No errors for the selected file.", severity="warning")
    def action_open_meld(self) -> None:
        btn = self.query_one("#btn-open-meld", Button)
        if btn.disabled or self._meld_running or not self.payload:
            return
        file_list = self.query_one("#file-list", ListView)
        if file_list.index is None or file_list.index >= len(self.payload.get("files", [])):
            return
        idx = file_list.index
        self._meld_running = True
        self._update_buttons()
        self.run_worker(self._run_meld_session(idx), exclusive=False)

    async def _run_meld_session(self, idx: int) -> None:
        from combinecopy.mobile.env import find_meld
        meld_exe = find_meld()

        file_obj = self.payload["files"][idx]
        path = file_obj.get("path") or ""
        full_path = os.path.join(self.root_dir, path)
        old_text = ""
        nl = "\n"
        if os.path.exists(full_path):
            old_text = safe_read_file(full_path)
            nl = detect_newline(full_path) or "\n"
        new_text = compute_new_text(file_obj, old_text)

        base_name = os.path.basename(path) or "file"
        fd_old, path_old = tempfile.mkstemp(suffix="_old_" + base_name, text=True)
        fd_new, path_new = tempfile.mkstemp(suffix="_new_" + base_name, text=True)
        os.close(fd_old)
        os.close(fd_new)

        _write_text_preserving(path_old, old_text, original_newline=nl)
        _write_text_preserving(path_new, new_text, original_newline=nl)

        timer_was_active = False
        if self.polling_timer and not self.polling_timer.is_paused:
            self.polling_timer.pause()
            timer_was_active = True

        try:
            if not meld_exe:
                self._fallback_text_diff(path_old, path_new)
                return

            self.notify("Opened in Meld. Edit the right-hand pane and save when ready.", severity="info")
            proc = await asyncio.create_subprocess_exec(meld_exe, path_old, path_new)
            await proc.wait()

            edited_text = safe_read_file(path_new)
            if edited_text == new_text:
                self.notify("Meld closed. No changes detected.", severity="info")
            else:
                self._absorb_meld_edit(idx, edited_text)
        except Exception as e:
            self.notify(f"Failed during Meld session: {e}", severity="error")
        finally:
            for p in (path_old, path_new):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            self._meld_running = False
            if timer_was_active and self.polling_timer:
                self.polling_timer.resume()
            self._update_buttons()

    def _absorb_meld_edit(self, idx: int, edited_text: str) -> None:
        if not self.payload or idx >= len(self.payload.get("files", [])):
            return
        file_obj = self.payload["files"][idx]
        action = file_obj.get("action", "").lower()
        if action in ("delete", "command"):
            self.notify(f"Cannot update content for {action} action from Meld.", severity="warning")
            return

        file_obj["content"] = edited_text
        file_obj.pop("search_replace", None)
        file_obj.pop("regex_replace", None)

        warn_msg = "Meld edited: replacement content was hand-edited in Meld."
        warnings = file_obj.setdefault("_warnings", [])
        if warn_msg not in warnings:
            warnings.append(warn_msg)

        self._validate_file_obj(file_obj)
        self.refresh_file_list()
        self._render_diff_for_index(idx)
        self.notify("Updated pending changes with edits from Meld!", severity="success")

    def _fallback_text_diff(self, path_old: str, path_new: str) -> None:
        """No Meld (the normal case on Termux) - render a unified diff inline."""
        diff_view = self.query_one("#diff-view", RichLog)
        try:
            proc = subprocess.run(
                ["git", "diff", "--no-index", "--color=never", path_old, path_new],
                capture_output=True, text=True, errors="replace", timeout=30
            )
            output = proc.stdout or proc.stderr
        except Exception as e:
            output = f"Could not produce a diff: {e}"

        diff_view.clear()
        diff_view.write(Text("Meld not found - showing a unified diff instead.\n", style="bold yellow"))
        for line in (output or "No differences found.").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                diff_view.write(Text(line, style="green"))
            elif line.startswith("-") and not line.startswith("---"):
                diff_view.write(Text(line, style="red"))
            else:
                diff_view.write(Text(line, style="dim"))
        self.notify("Meld not found; rendered an inline diff instead.", severity="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-discard-all":
            self.action_discard_all()
        elif btn_id == "btn-apply-all":
            self.action_apply_all()
        elif btn_id == "btn-discard-file":
            self.action_discard_file()
        elif btn_id == "btn-apply-file":
            self.action_apply_file()
        elif btn_id == "btn-practice":
            self.action_practice()
        elif btn_id == "btn-partial-add":
            self.action_partial_add()
        elif btn_id == "btn-commit":
            self.action_commit()
        elif btn_id == "btn-human-correct":
            self.action_human_correct()
        elif btn_id == "btn-open-meld":
            self.action_open_meld()
        elif btn_id == "btn-fix-json":
            self.action_fix_json()

    def _apply_single_file(self, idx: int) -> None:
        file_obj = self.payload["files"][idx]
        action = file_obj.get("action", "").lower()
        if action == "command":
            file_obj["_status"] = "applied"
            self._record_applied_file(file_obj)
            return
        path = file_obj.get("path")
        full_path = os.path.join(self.root_dir, path)
        if action == "delete":
            if self.tfs_mode:
                errors = tfs_delete(self.root_dir, [path])
                if errors:
                    self.notify(f"TFS delete error: {errors[0]}", severity="error")
            else:
                if os.path.exists(full_path):
                    os.remove(full_path)
            file_obj["_status"] = "applied"
            self._record_applied_file(file_obj)
            return
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        if action == "create":
            # Use newline="" to prevent automatic \n -> \r\n translation on Windows.
            # The AI's JSON content uses \n; write it as-is so emojis and exact bytes survive.
            _write_text_preserving(full_path, file_obj.get("content", ""), original_newline="\n")
        elif action == "modify":
            # TFS server workspace: checkout before editing
            if self.tfs_mode and os.path.exists(full_path):
                errors = tfs_checkout(self.root_dir, [path])
                if errors:
                    self.notify(f"TFS checkout error: {errors[0]}", severity="error")
            original_newline = detect_newline(full_path) if os.path.exists(full_path) else "\n"
            if not original_newline:
                original_newline = "\n"
            old_text = ""
            if os.path.exists(full_path):
                old_text = safe_read_file(full_path)
            new_text = compute_new_text(file_obj, old_text)
            old_lines = old_text.splitlines(keepends=True)
            new_lines = new_text.splitlines(keepends=True)
            diff = difflib.unified_diff(old_lines, new_lines, n=0)
            added = 0
            removed = 0
            for line in diff:
                if line.startswith('+') and not line.startswith('+++'):
                    added += 1
                elif line.startswith('-') and not line.startswith('---'):
                    removed += 1
            file_obj["_added"] = added
            file_obj["_removed"] = removed
            _write_text_preserving(full_path, new_text, original_newline=original_newline)
        file_obj["_status"] = "applied"
        self._record_applied_file(file_obj)

    def commit_changes(self) -> None:
        msg = self.payload.get("commit_message", "Auto-commit from AI agent")
        applied_files = [f for f in self.payload.get("files", []) if f.get("_status") == "applied"]
        paths_to_stage = [f.get("path") for f in applied_files if f.get("path") and f.get("action", "").lower() != "command"]

        if not paths_to_stage:
            self.notify("No applied files to stage.", severity="warning")
            return

        if self.tfs_mode:
            self._commit_tfs(msg, applied_files, paths_to_stage)
        else:
            self._commit_git(msg, paths_to_stage)

    def _commit_git(self, msg: str, paths_to_stage: list[str]) -> None:
        try:
            subprocess.run(["git", "add"] + paths_to_stage, cwd=self.root_dir, check=True)
            subprocess.run(["git", "commit", "-m", msg], cwd=self.root_dir, check=True)

            commit_hash = ""
            try:
                commit_hash = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root_dir, text=True).strip()
            except Exception:
                pass

            self.notify("Changes successfully committed to Git! Closing app.", title="Success")
            summary_data = {
                "commit_message": msg,
                "files": self.payload.get("files", []),
                "commit_hash": commit_hash
            }
            self.exit(summary_data)
        except subprocess.CalledProcessError as e:
            self.notify(f"Git error: {e}", title="Error", severity="error")

    def _commit_tfs(self, msg: str, applied_files: list[dict], paths_to_stage: list[str]) -> None:
        # Separate new files that need tf add
        add_paths = [f.get("path") for f in applied_files if f.get("action", "").lower() == "create" and f.get("path")]
        errors = tfs_add(self.root_dir, add_paths)
        if errors:
            self.notify(f"TFS add warnings: {'; '.join(errors)}", severity="warning")

        changeset, error = tfs_checkin(self.root_dir, paths_to_stage, msg)
        if error:
            self.notify(f"TFS checkin error: {error}", title="Error", severity="error")
            return

        self.notify(f"Checked in as Changeset #{changeset}! Closing app.", title="Success")
        summary_data = {
            "commit_message": msg,
            "files": self.payload.get("files", []),
            "commit_hash": f"CS{changeset}" if changeset else None
        }
        self.exit(summary_data)

    def reset_state(self) -> None:
        self.payload = None
        self.last_clipboard = ""
        self.json_error_text = None
        self.broken_json_content = ""
        self.is_loading_payload = False
        self.query_one("#status-label", Label).update("Waiting for AI...")
        self.query_one("#ai-markdown", Markdown).update("*(AI output will appear here)*")
        self.query_one("#file-list", ListView).clear()
        diff_view = self.query_one("#diff-view", RichLog)
        diff_view.clear()
        diff_view.write("Select a file to view diffs.")
        self._disable_all_buttons()
        if self.mobile_mode:
            self.query_one("#status-label", Label).update("Press 'v' to paste, 'V' for editor")
            self.polling_timer.resume()
            self.call_after_refresh(self.action_paste_buffer)
            return
        try:
            pyperclip.copy("")
        except Exception:
            pass
        self.polling_timer.resume()

    def _record_applied_file(self, file_obj: dict) -> None:
        if not hasattr(self, "session_applied_files"):
            self.session_applied_files = []
        identifier = file_obj.get("path") or file_obj.get("command")
        if not identifier:
            return
        self.session_applied_files = [
            f for f in self.session_applied_files
            if (f.get("path") or f.get("command")) != identifier
        ]
        self.session_applied_files.append(file_obj)

    def action_quit(self) -> None:
        applied = getattr(self, "session_applied_files", [])
        if applied:
            summary_data = {
                "commit_message": "Not committed",
                "files": applied,
                "commit_hash": None
            }
            self.exit(summary_data)
        else:
            self.exit(None)
def run_auto_agent(root_dir: str, known_files: list[str] | None = None, revert_mode: bool = False, ignore_initial_clipboard: bool = False, web_mode: bool = False, xml_mode: bool = False, consult_mode: bool = False, rehab_mode: bool = False, mobile_mode: bool = False):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f_in, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f_out:
            in_name = f_in.name
            out_name = f_out.name
            
        try:
            args_dict = {
                "root_dir": root_dir,
                "known_files": known_files,
                "revert_mode": revert_mode,
                "ignore_initial_clipboard": ignore_initial_clipboard,
                "web_mode": web_mode,
                "xml_mode": xml_mode,
                "consult_mode": consult_mode,
                "rehab_mode": rehab_mode,
                "mobile_mode": mobile_mode
            }
            with open(in_name, "w", encoding="utf-8") as f:
                json.dump(args_dict, f)
                
            script_path = os.path.abspath(__file__)
            subprocess.run([sys.executable, script_path, "auto_agent", in_name, out_name], check=True)
            
            if os.path.exists(out_name):
                with open(out_name, "r", encoding="utf-8") as f:
                    content = f.read()
                    if content.strip():
                        return json.loads(content)
            return None
        finally:
            for p in (in_name, out_name):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
    else:
        app = AutoAgentApp(root_dir, known_files, revert_mode, ignore_initial_clipboard, web_mode, xml_mode=xml_mode, consult_mode=consult_mode, rehab_mode=rehab_mode, mobile_mode=mobile_mode)
        return app.run()

if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "auto_agent":
        in_path = sys.argv[2]
        out_path = sys.argv[3]
        
        with open(in_path, "r", encoding="utf-8") as f_in:
            args_dict = json.load(f_in)
        app = AutoAgentApp(
            root_dir=args_dict.get("root_dir"),
            known_files=args_dict.get("known_files"),
            revert_mode=args_dict.get("revert_mode", False),
            ignore_initial_clipboard=args_dict.get("ignore_initial_clipboard", False),
            web_mode=args_dict.get("web_mode", False),
            xml_mode=args_dict.get("xml_mode", False),
            consult_mode=args_dict.get("consult_mode", False),
            rehab_mode=args_dict.get("rehab_mode", False),
            mobile_mode=args_dict.get("mobile_mode", False)
        )
        res = app.run()
        
        with open(out_path, "w", encoding="utf-8") as f_out:
            json.dump(res if res is not None else {}, f_out)
