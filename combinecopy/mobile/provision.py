"""One-time setup helpers and an environment doctor for mobile mode."""

import os
import shutil
import stat

from combinecopy.mobile.env import (
    editor_display_name,
    has_termux_api,
    is_termux,
    resolve_editor,
    terminal_width,
)
from combinecopy.mobile.inbox import INBOX_DIR, ensure_inbox_dir

URL_OPENER_PATH = os.path.expanduser("~/bin/termux-url-opener")

URL_OPENER_SCRIPT = """#!/data/data/com.termux/files/usr/bin/bash
# Installed by combineCopy mobile mode.
# Anything shared into Termux lands in ~/.cc_inbox as a timestamped .txt file.
# Press 'r' inside the combineCopy listener to ingest the newest drop.
set -eu
INBOX="$HOME/.cc_inbox"
mkdir -p "$INBOX"
TARGET="$INBOX/shared_$(date +%s).txt"
if [ -f "$1" ]; then
  cp "$1" "$TARGET"
else
  printf '%s' "$1" > "$TARGET"
fi
echo "combineCopy: saved payload to $TARGET"
"""


def _check(label: str, ok: bool, detail: str = "") -> str:
    mark = "[bold green]OK  [/bold green]" if ok else "[bold red]MISS[/bold red]"
    line = f"  {mark} {label}"
    if detail:
        line += f"  [dim]{detail}[/dim]"
    return line


def run_doctor(console) -> None:
    """Prints a checklist of everything mobile mode depends on."""
    console.print("\n[bold cyan]combineCopy - Mobile Mode Doctor[/bold cyan]\n")

    termux = is_termux()
    console.print(_check(
        "Running inside Termux", termux,
        os.environ.get("TERMUX_VERSION", "") or "not detected"
    ))

    api = has_termux_api()
    console.print(_check(
        "termux-api CLI available", api,
        "" if api else "pkg install termux-api"
    ))
    if not api and termux:
        console.print("       [yellow]Also install the Termux:API app from F-Droid.[/yellow]")
        console.print("       [yellow]The pkg alone provides shims that hang without it.[/yellow]")

    editor = resolve_editor()
    console.print(_check(
        "Editor for large payloads", editor is not None,
        editor_display_name() if editor else "pkg install micro"
    ))
    if editor and os.path.basename(editor[0]).lower().split(".")[0] != "micro":
        console.print("       [yellow]micro not found; falling back. 'pkg install micro' recommended.[/yellow]")

    if termux:
        storage_ok = os.path.isdir(os.path.expanduser("~/storage"))
        console.print(_check(
            "Storage permission granted", storage_ok,
            "" if storage_ok else "run termux-setup-storage"
        ))

    inbox_ok = os.path.isdir(INBOX_DIR)
    console.print(_check("Inbox directory", inbox_ok, INBOX_DIR))

    hook_ok = os.path.exists(URL_OPENER_PATH)
    console.print(_check(
        "Share-sheet hook installed", hook_ok,
        URL_OPENER_PATH if hook_ok else "combineCopy --install-url-opener"
    ))

    width = terminal_width()
    console.print(_check("Terminal width", width >= 50, f"{width} columns"))

    git_ok = shutil.which("git") is not None
    console.print(_check("git available", git_ok, "" if git_ok else "pkg install git"))

    console.print("\n[dim]Listener keys: v = paste buffer, V = editor, r = inbox drop / clipboard.[/dim]\n")


def install_url_opener(console, force: bool = False) -> bool:
    """Installs the Termux share-sheet hook.

    Termux permits exactly one url-opener, so an existing hook is never
    silently clobbered.
    """
    if not is_termux():
        console.print("[yellow]Not running in Termux; the share hook only works there.[/yellow]")
        return False

    if os.path.exists(URL_OPENER_PATH) and not force:
        console.print(f"[yellow]{URL_OPENER_PATH} already exists.[/yellow]")
        console.print("Termux allows only ONE url-opener hook, so it was left untouched.")
        console.print("Re-run with [bold]--force[/bold] to overwrite, or merge this manually:\n")
        console.print(URL_OPENER_SCRIPT)
        return False

    try:
        os.makedirs(os.path.dirname(URL_OPENER_PATH), exist_ok=True)
        with open(URL_OPENER_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(URL_OPENER_SCRIPT)
        mode = os.stat(URL_OPENER_PATH).st_mode
        os.chmod(URL_OPENER_PATH, mode | stat.S_IXUSR)
        ensure_inbox_dir()
        console.print(f"[bold green]Installed share hook to {URL_OPENER_PATH}[/bold green]")
        console.print(f"Shared text now lands in {INBOX_DIR}.")
        console.print("Press [bold]r[/bold] in the listener to ingest the newest drop.")
        return True
    except Exception as e:
        console.print(f"[bold red]Failed to install hook: {e}[/bold red]")
        return False
