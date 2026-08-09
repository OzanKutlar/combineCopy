"""Environment detection and capability probing.

Everything in here is cheap and side-effect free so it can be called from
argument parsing, TUI mount handlers, and the doctor command alike.
"""

import os
import shutil
from dataclasses import dataclass

_WINDOWS_NPP_PATHS = [
    r"C:\Program Files\Notepad++\notepad++.exe",
    r"C:\Program Files (x86)\Notepad++\notepad++.exe",
]

_WINDOWS_MELD_PATHS = [
    r"C:\Program Files\Meld\Meld.exe",
    r"C:\Program Files (x86)\Meld\Meld.exe",
]

# Preferred terminal editors, best first. micro leads because it has sane
# mouse support and normal keybindings, which matters a lot on a phone.
_TERMINAL_EDITORS = ("micro", "nano", "vi")

_TERMINAL_EDITOR_BASENAMES = {
    "micro", "nano", "vi", "vim", "nvim", "emacs", "helix", "hx", "kak",
}


def is_termux() -> bool:
    """True when running inside a Termux session."""
    if os.environ.get("TERMUX_VERSION"):
        return True
    return "com.termux" in os.environ.get("PREFIX", "")


def has_termux_api() -> bool:
    """True when the termux-api CLI package is installed.

    Note this does NOT prove the Termux:API *app* is installed; the CLI shims
    exist without it and simply hang or fail. The doctor command warns about
    that separately.
    """
    return shutil.which("termux-clipboard-set") is not None


def has_meld() -> bool:
    if shutil.which("meld") or shutil.which("meld.exe"):
        return True
    for path in _WINDOWS_MELD_PATHS:
        if os.path.exists(path):
            return True
    return False


def has_keyboard_hook() -> bool:
    """True when a global hotkey hook can actually be installed.

    The `keyboard` module imports fine on Linux but silently needs root to read
    /dev/input, so an import check alone is not enough.
    """
    try:
        import keyboard  # noqa: F401
    except Exception:
        return False
    if os.name == "nt":
        return True
    getuid = getattr(os, "geteuid", None)
    if getuid is None:
        return False
    try:
        return getuid() == 0
    except Exception:
        return False


def resolve_editor() -> list[str] | None:
    """Returns an argv prefix for a blocking editor, or None if none is found.

    Consolidates the Notepad++ lookup that was previously duplicated across
    tui/apply.py, tui/prompt.py and tui/rules.py.
    """
    env_editor = os.environ.get("EDITOR", "").strip()

    if os.name == "nt":
        npp = shutil.which("notepad++") or shutil.which("notepad++.exe")
        if not npp:
            for path in _WINDOWS_NPP_PATHS:
                if os.path.exists(path):
                    npp = path
                    break
        if npp:
            return [npp, "-multiInst", "-nosession"]
        if env_editor:
            return [env_editor]
        return ["notepad"]

    for name in _TERMINAL_EDITORS:
        found = shutil.which(name)
        if found:
            return [found]
    if env_editor:
        return [env_editor]
    return None


def editor_display_name() -> str:
    cmd = resolve_editor()
    if not cmd:
        return "editor"
    return os.path.basename(cmd[0]).replace(".exe", "")


def is_terminal_editor(argv0: str) -> bool:
    """True when the editor takes over the TTY and therefore needs suspend()."""
    base = os.path.basename(argv0).lower().split(".")[0]
    return base in _TERMINAL_EDITOR_BASENAMES


def terminal_width(default: int = 80) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def is_narrow_screen(threshold: int = 70) -> bool:
    return terminal_width() < threshold


@dataclass(frozen=True)
class Capabilities:
    is_termux: bool
    can_write_clipboard: bool
    can_read_clipboard: bool
    has_meld: bool
    has_keyboard_hook: bool
    editor_cmd: tuple[str, ...] | None
    editor_name: str
    narrow_screen: bool


def detect_capabilities() -> Capabilities:
    termux = is_termux()
    api = has_termux_api() if termux else True
    editor = resolve_editor()
    return Capabilities(
        is_termux=termux,
        can_write_clipboard=api,
        can_read_clipboard=api,
        has_meld=has_meld(),
        has_keyboard_hook=has_keyboard_hook(),
        editor_cmd=tuple(editor) if editor else None,
        editor_name=editor_display_name(),
        narrow_screen=is_narrow_screen(),
    )
