import os
import re
import threading
import tempfile
import shutil
import subprocess
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Label, TextArea, Button, OptionList
from textual.binding import Binding
from combinecopy.tui.rules import RulesScreen

class SystemPromptApp(App):
    """TUI for injecting system instructions and user requests."""
    CSS = """
    Screen { background: #2d2825; }
    Header { background: #d08c60; color: #2d2825; }
    Footer { background: #3c3431; }
    #layout { height: 100%; }
    #left-pane {
        width: 30%;
        border-right: solid #5a4d45;
        background: #241f1c;
    }
    #right-pane {
        width: 70%;
        padding: 1 2;
    }
    .panel-title {
        background: #4a3f39;
        color: #d08c60;
        padding: 1;
        text-style: bold; 
        margin-bottom: 1;
    }
    TextArea {
        border: solid #5a4d45;
        background: #1e1a18;
        margin-bottom: 1;
    }
    #left-pane OptionList { height: 1fr; }
    #file-actions {
        height: 3;
        margin-bottom: 1;
    }
    #btn-reselect { width: 1fr; }
    TextArea:focus {
        border: double #d08c60;
    }
    #user-request { height: 1fr; }
    #sys-prompt { height: 2fr; }
    #action-buttons { height: 3; margin-top: 1; margin-bottom: 1; }
    #btn-submit { width: 1fr; margin-right: 1; }
    #btn-editor { width: auto; margin-right: 1; }
    #btn-rules { width: auto; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+j", "submit", "Submit Request", show=False),
        Binding("ctrl+enter", "submit", "Submit Request"),
        Binding("f2", "open_editor", "Open in Editor"),
        Binding("f3", "edit_rules", "Edit Rules"),
        Binding("f4", "reselect_files", "Reselect Files")
    ]
    
    def __init__(self, root_dir: str, files: list[str], sys_prompt: str,
                 important=None, partials=None, max_depth: int = 100,
                 ext_filters=None, exclude_dirs=None, ast_mode: bool = False):
        super().__init__()
        self.root_dir = root_dir
        self.files = list(files or [])
        self.important = list(important) if important is not None else list(self.files)
        self.partials = dict(partials) if partials else {}
        self.max_depth = max_depth if max_depth is not None else 100
        self.ext_filters = ext_filters
        self.exclude_dirs = exclude_dirs
        self.ast_mode = bool(ast_mode)
        # Only reported back when the selector actually returned something.
        self.selection_changed = False
        self.sys_prompt = sys_prompt
        
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="layout"):
            with Vertical(id="left-pane"):
                yield Label(f"Files in Context ({len(self.files)})", id="files-title", classes="panel-title")
                with Horizontal(id="file-actions"):
                    yield Button("Reselect Files (F4)", id="btn-reselect", variant="warning")
                rel_files = [os.path.relpath(f, self.root_dir) for f in self.files]
                yield OptionList(*rel_files, id="file-list")
            with Vertical(id="right-pane"):
                yield Label("Your Request / Problem (Ctrl+Enter to Submit):", classes="panel-title")
                yield TextArea(id="user-request", text="")
                yield Label("System Prompt (Injected):", classes="panel-title")
                yield TextArea(id="sys-prompt", text=self.sys_prompt)
                with Horizontal(id="action-buttons"):
                    yield Button("Submit & Continue (Ctrl+Enter)", id="btn-submit", variant="success")
                    yield Button("Open in Notepad++ (F2)", id="btn-editor", variant="primary")
                    yield Button("Edit Rules (F3)", id="btn-rules", variant="warning")
        yield Footer()
        
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-submit":
            self.action_submit()
        elif event.button.id == "btn-editor":
            self.action_open_editor()
        elif event.button.id == "btn-rules":
            self.action_edit_rules()
        elif event.button.id == "btn-reselect":
            self.action_reselect_files()
            
    def action_open_editor(self) -> None:
        btn = self.query_one("#btn-editor", Button)
        if btn.disabled:
            return
        btn.disabled = True
        
        current_text = self.query_one("#user-request", TextArea).text
        thread = threading.Thread(target=self._editor_worker, args=(current_text,), daemon=True)
        thread.start()
        self.notify("Waiting for external editor to close...", severity="info")
    def _editor_worker(self, current_text: str) -> None:
        fd, temp_path = tempfile.mkstemp(suffix=".txt", text=True)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(current_text)
            
        npp_path = shutil.which("notepad++") or shutil.which("notepad++.exe")
        if not npp_path:
            possible_paths = [
                r"C:\Program Files\Notepad++\notepad++.exe",
                r"C:\Program Files (x86)\Notepad++\notepad++.exe"
            ]
            for p in possible_paths:
                if os.path.exists(p):
                    npp_path = p
                    break
                    
        if npp_path:
            cmd = [npp_path, "-multiInst", "-nosession", temp_path]
        elif os.name == 'nt':
            cmd = ["notepad", temp_path]
        else:
            editor = os.environ.get('EDITOR', 'nano')
            cmd = [editor, temp_path]
            
        try:
            subprocess.run(cmd, check=True)
        except Exception as e:
            self.call_from_thread(self.notify, f"Editor failed to launch: {e}", severity="error")
            
        try:
            with open(temp_path, 'r', encoding='utf-8') as f:
                new_text = f.read()
            self.call_from_thread(self._update_request_text, new_text)
        except Exception as e:
            self.call_from_thread(self.notify, f"Failed to read from editor: {e}", severity="error")
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            self.call_from_thread(self._enable_editor_button)
            
    def _update_request_text(self, new_text: str) -> None:
        ta = self.query_one("#user-request", TextArea)
        ta.text = new_text
        self.notify("Text updated from editor!", title="Success")
        
    def _enable_editor_button(self) -> None:
        self.query_one("#btn-editor", Button).disabled = False
    def action_reselect_files(self) -> None:
        from combinecopy.tui.selection import run_file_selector
        from combinecopy.utils import get_files_recursive

        scanned = get_files_recursive(
            self.root_dir, 0, self.max_depth, self.ext_filters,
            exclude_dirs=self.exclude_dirs
        )
        # A file targeted directly on the command line may sit outside the
        # scan filters. Offering only the scan would silently drop it.
        for path in self.files:
            if path not in scanned:
                scanned.append(path)

        if not scanned:
            self.notify("The scan found no files to select from.", severity="warning")
            return

        def _launch():
            return run_file_selector(
                self.root_dir,
                scanned,
                ast_mode=self.ast_mode,
                preselected_files=list(self.files),
                preselected_partials=dict(self.partials)
            )

        try:
            # The selector is a Textual App in its own right, so this one has to
            # release the terminal first. Same handoff the paste buffer uses.
            suspend = getattr(self, "suspend", None)
            if suspend is not None:
                with suspend():
                    selected = _launch()
            else:
                selected = _launch()
        except Exception as error:
            self.notify(f"File selector failed: {error}", severity="error")
            return

        if selected is None:
            self.notify("Selection cancelled; the context is unchanged.", severity="information")
            return

        self.files = list(selected[0])
        self.important = list(selected[1] or [])
        self.partials = dict(selected[2] or {})
        self.selection_changed = True
        self._refresh_file_list()

    def _refresh_file_list(self) -> None:
        option_list = self.query_one("#file-list", OptionList)
        option_list.clear_options()
        for path in self.files:
            option_list.add_option(os.path.relpath(path, self.root_dir))
        self.query_one("#files-title", Label).update(f"Files in Context ({len(self.files)})")
        self.refresh(layout=True)
        self.notify(f"Context updated: {len(self.files)} file(s).", title="Files")

    def action_edit_rules(self) -> None:
        self.app.push_screen(
            RulesScreen(self.root_dir),
            callback=self._on_rules_screen_dismissed
        )

    def _on_rules_screen_dismissed(self, new_rules: str | None) -> None:
        if new_rules is not None:
            self._update_rules_in_textarea(new_rules)

    def _update_rules_in_textarea(self, new_rules: str) -> None:
        ta = self.query_one("#sys-prompt", TextArea)
        current_text = ta.text
        if new_rules:
            replacement = f"<user_rules>\n{new_rules}\n</user_rules>"
        else:
            replacement = "<user_rules>\n\nThe user has not defined any custom rules.\n\n</user_rules>"
            
        new_text = re.sub(
            r'<user_rules>.*?</user_rules>',
            replacement,
            current_text,
            flags=re.DOTALL
        )
        if new_text != current_text:
            ta.text = new_text
            self.notify("System prompt rules updated!", title="Success")
        else:
            self.notify("Rules checked, no changes detected.", severity="info")
            
    def _enable_rules_button(self) -> None:
        self.query_one("#btn-rules", Button).disabled = False
    def action_submit(self) -> None:
        req = self.query_one("#user-request", TextArea).text
        sys_text = self.query_one("#sys-prompt", TextArea).text
        result = {"request": req, "system": sys_text}
        if self.selection_changed:
            result["files"] = list(self.files)
            result["important"] = list(self.important)
            result["partials"] = dict(self.partials)
        self.exit(result)
        
    def action_cancel(self) -> None:
        self.exit(None)
