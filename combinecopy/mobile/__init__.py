"""Mobile (Termux) support for combineCopy.

Android 10+ restricts clipboard *reads* to whichever app currently holds focus,
which makes the desktop clipboard-polling loop both unreliable and slow inside
Termux. This package replaces the ingest channel entirely while keeping
clipboard *writes*, which remain unrestricted.
"""

from combinecopy.mobile.env import (
    Capabilities,
    detect_capabilities,
    editor_display_name,
    has_meld,
    has_termux_api,
    is_narrow_screen,
    is_termux,
    resolve_editor,
    terminal_width,
)
from combinecopy.mobile.clipboard import (
    CLIPBOARD_SIZE_LIMIT,
    OUTBOX_DIR,
    copy_text,
    read_text_once,
    stage_outbound,
)
from combinecopy.mobile.inbox import (
    INBOX_DIR,
    PROCESSED_DIR,
    PayloadInbox,
    ensure_inbox_dir,
    read_latest_dropped_file,
)

__all__ = [
    "Capabilities",
    "detect_capabilities",
    "editor_display_name",
    "has_meld",
    "has_termux_api",
    "is_narrow_screen",
    "is_termux",
    "resolve_editor",
    "terminal_width",
    "CLIPBOARD_SIZE_LIMIT",
    "OUTBOX_DIR",
    "copy_text",
    "read_text_once",
    "stage_outbound",
    "INBOX_DIR",
    "PROCESSED_DIR",
    "PayloadInbox",
    "ensure_inbox_dir",
    "read_latest_dropped_file",
]
