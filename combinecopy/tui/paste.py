"""Manual payload ingest for mobile mode.

The TextArea handles normal payloads. For anything large it slows down badly
(it is doing syntax-aware document surgery per insert), so Ctrl+E hands off to
a real terminal editor via App.suspend().
"""

import os
import subprocess
import tempfile

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, TextArea

from combinecopy.mobile.clipboard import read_text_once
from combinecopy.mobile.env import editor_display_name, resolve_editor
from combinecopy.mobile.inbox import INBOX_DIR, read_latest_dropped_file
from combinecopy.utils import safe_read_file

_MAX_SCAN_CHARS = 2_000_000


def analyze_payload(text: str) -> tuple[bool, str]:
    """Cheap structural check to catch silently truncated pastes.

    A terminal paste that overruns the PTY buffer does not error, it just stops
    early, so the buffer looks fine until the parser rejects it. Catching an
    unbalanced payload before submit saves a full round trip.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False, "Buffer is empty."

    if "<antigravity_payload>" in stripped:
        if "</antigravity_payload>" not in stripped:
            return False, "Missing closing </antigravity_payload> - the paste was likely truncated."
        return True, "XML payload looks complete."

    first = stripped.find("{")
    if first == -1:
        return False, "No JSON object or XML payload found in the buffer."

    depth = 0
    in_string = False
    escape = False
    for ch in stripped[first:first + _MAX_SCAN_CHARS]:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break

    if depth != 0:
        return False, f"JSON braces unbalanced (depth {depth}) - the paste was likely truncated."
    if '"phase"' not in stripped:
        return True, 'Braces balanced, but no "phase" key was found.'
    return True, "JSON payload looks complete."


class PasteBufferScreen(ModalScreen[str]):
    """Full-screen manual ingest buffer."""

    CSS = """
    PasteBufferScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #paste-dialog {
        width: 98%;
        height: 98%;
        border: solid #d08c60;
        background: #2d2825;
        padding: 0 1;
    }
    .paste-title {
        background: #4a3f39;
        color: #d08c60;
        text-align: center;
        text-style: bold;
        padding: 1;
    }
    #paste-area {
        height: 1fr;
        border: solid #5a4d45;
        background: #1e1a18;
    }
    #paste-area:focus {
        border: double #d08c60;
    }
    #paste-status {
        height: auto;
        color: #ead6c9;
        padding: 0 1;
    }
    #paste-buttons {
        height: 3;
        align: center middle;
    }
    Button {
        margin: 0 1;
        min-width: 10;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "submit", "Submit"),
        Binding("ctrl+e", "open_editor", "Editor"),
        Binding("ctrl+r", "load_clipboard", "Clipboard"),
        Binding("ctrl+d", "load_drop", "Load Drop"),
    ]

    def __init__(self, initial_text: str = "", auto_editor: bool = False):
        super().__init__()
        self.initial_text = initial_text or ""
        self.auto_editor = auto_editor
        self.temp_path = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="paste-dialog"):
            yield Label(
                "Paste Payload  -  Ctrl+S submit | Ctrl+E editor | Esc cancel",
                classes="paste-title",
            )
            yield TextArea(self.initial_text, id="paste-area")
            yield Label("", id="paste-status")
            with Horizontal(id="paste-buttons"):
                yield Button(editor_display_name(), id="btn-paste-editor", variant="primary")
                yield Button("Clipboard", id="btn-paste-clip", variant="warning")
                yield Button("Drop", id="btn-paste-drop", variant="warning")
                yield Button("Submit", id="btn-paste-submit", variant="success")
                yield Button("Cancel", id="btn-paste-cancel", variant="error")

    def on_mount(self) -> None:
        self.query_one("#paste-area", TextArea).focus()
        self._update_status()
        if self.auto_editor:
            self.call_after_refresh(self.action_open_editor)

    def on_unmount(self) -> None:
        if self.temp_path and os.path.exists(self.temp_path):
            try:
                os.remove(self.temp_path)
            except OSError:
                pass

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._update_status()

    def _update_status(self) -> None:
        text = self.query_one("#paste-area", TextArea).text
        chars = len(text)
        lines = text.count("\n") + 1 if text else 0
        ok, msg = analyze_payload(text)
        colour = "green" if ok else "yellow"
        self.query_one("#paste-status", Label).update(
            f"[dim]{chars} chars, {lines} lines[/dim]   [{colour}]{msg}[/{colour}]"
        )

    def _set_text(self, text: str, source: str) -> None:
        area = self.query_one("#paste-area", TextArea)
        area.text = text
        self._update_status()
        self.notify(f"Loaded {len(text)} chars from {source}.", severity="information")

    def action_open_editor(self) -> None:
        editor = resolve_editor()
        if not editor:
            self.notify("No editor found. Try: pkg install micro", severity="error")
            return

        area = self.query_one("#paste-area", TextArea)
        try:
            if not self.temp_path:
                fd, self.temp_path = tempfile.mkstemp(
                    prefix="combineCopy_paste_", suffix=".txt", text=True
                )
                os.close(fd)
            with open(self.temp_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(area.text)
        except Exception as e:
            self.notify(f"Could not stage temp file: {e}", severity="error")
            return

        argv = list(editor) + [self.temp_path]
        try:
            # suspend() restores the terminal, runs the editor in the foreground,
            # then re-enters application mode and forces a repaint.
            suspend = getattr(self.app, "suspend", None)
            if suspend is not None:
                with suspend():
                    subprocess.run(argv, check=False)
            else:
                subprocess.run(argv, check=False)
        except Exception as e:
            self.notify(f"Editor failed to launch: {e}", severity="error")
            return

        try:
            self._set_text(safe_read_file(self.temp_path), editor_display_name())
        except Exception as e:
            self.notify(f"Could not read editor output: {e}", severity="error")

    def action_load_clipboard(self) -> None:
        text = read_text_once()
        if not text:
            self.notify(
                "Clipboard read returned nothing. Long-press and Paste, or use Ctrl+E.",
                severity="warning",
            )
            return
        self._set_text(text, "clipboard")

    def action_load_drop(self) -> None:
        text = read_latest_dropped_file()
        if not text:
            self.notify(f"No pending files in {INBOX_DIR}.", severity="warning")
            return
        self._set_text(text, "inbox drop")

    def action_submit(self) -> None:
        text = self.query_one("#paste-area", TextArea).text.strip()
        if not text:
            self.notify("Nothing to submit.", severity="warning")
            return
        ok, msg = analyze_payload(text)
        if not ok:
            self.notify(f"Submitting anyway: {msg}", severity="warning")
        self.dismiss(text)

    def action_cancel(self) -> None:
        self.dismiss("")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-paste-editor":
            self.action_open_editor()
        elif btn_id == "btn-paste-clip":
            self.action_load_clipboard()
        elif btn_id == "btn-paste-drop":
            self.action_load_drop()
        elif btn_id == "btn-paste-submit":
            self.action_submit()
        elif btn_id == "btn-paste-cancel":
            self.action_cancel()
