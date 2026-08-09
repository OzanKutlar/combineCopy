"""Clipboard access that works on both desktop and Termux.

Writes are unrestricted on Android and work well. Reads are restricted to the
focused app since API 29, so `read_text_once` is deliberately named to make it
obvious it must never be called from a polling loop.
"""

import os
import subprocess
import time

from combinecopy.mobile.env import has_termux_api, is_termux

OUTBOX_DIR = os.path.expanduser("~/.cc_outbox")

# Android's clipboard service starts truncating and misbehaving well before
# this, but 64KB is a safe practical ceiling for termux-clipboard-set.
CLIPBOARD_SIZE_LIMIT = 64 * 1024

_READ_TIMEOUT_SECONDS = 3
_WRITE_TIMEOUT_SECONDS = 10


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


def _termux_set(text: str) -> bool:
    """Writes via stdin rather than argv.

    argv has a hard length cap and mangles embedded newlines, which silently
    corrupts multi-line prompts.
    """
    try:
        proc = subprocess.run(
            ["termux-clipboard-set"],
            input=_encode(text),
            timeout=_WRITE_TIMEOUT_SECONDS,
        )
        return proc.returncode == 0
    except Exception:
        return False


def copy_text(text: str) -> bool:
    """Copies text to the system clipboard. Returns False rather than raising."""
    if text is None:
        return False

    if is_termux():
        if not has_termux_api():
            return False
        if len(_encode(text)) > CLIPBOARD_SIZE_LIMIT:
            return False
        return _termux_set(text)

    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def read_text_once() -> str | None:
    """One deliberate clipboard read.

    NEVER call this from a timer or polling loop on Termux: each call costs
    roughly 0.5-1s of IPC and only succeeds when Termux holds focus.
    """
    if is_termux():
        if not has_termux_api():
            return None
        try:
            proc = subprocess.run(
                ["termux-clipboard-get"],
                capture_output=True,
                timeout=_READ_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0:
                return None
            out = proc.stdout.decode("utf-8", errors="replace").strip()
            return out or None
        except Exception:
            return None

    try:
        import pyperclip
        out = (pyperclip.paste() or "").strip()
        return out or None
    except Exception:
        return None


def stage_outbound(text: str) -> str | None:
    """Writes an oversized outbound prompt to disk and returns its path.

    Used when the clipboard write path is unavailable or the payload is too
    large, so the user gets a real file to share rather than a silent failure.
    """
    try:
        os.makedirs(OUTBOX_DIR, exist_ok=True)
        path = os.path.join(OUTBOX_DIR, f"prompt_{int(time.time())}.txt")
        with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
        return path
    except Exception:
        return None
