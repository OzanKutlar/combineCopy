"""The single funnel every mobile ingest source feeds into.

There is deliberately no background watcher thread and no HTTP server here.
Every source is either user-triggered (paste buffer, editor handoff) or a
one-shot manual read (inbox drop), which keeps the mobile listener free of
long-lived sockets and idle CPU burn.
"""

import os
import queue
import shutil
import time

INBOX_DIR = os.path.expanduser("~/.cc_inbox")
PROCESSED_DIR = os.path.join(INBOX_DIR, "processed")

_ACCEPTED_SUFFIXES = (".txt", ".json", ".xml", ".md")

# Bounded so a pathological inbox directory can never stall the TUI.
_MAX_SCAN_ENTRIES = 200
_MAX_DRAIN_ITEMS = 64


class PayloadInbox:
    """Thread-safe queue of candidate payload strings."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()

    def put(self, text: str, source: str = "unknown") -> None:
        if not text or not text.strip():
            return
        self._queue.put({"text": text, "source": source, "ts": time.time()})

    def drain(self) -> dict | None:
        """Returns the newest queued item, discarding any staler ones.

        Only the latest payload is ever relevant, so older entries are dropped
        rather than queued up behind it.
        """
        item = None
        for _ in range(_MAX_DRAIN_ITEMS):
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
        return item

    def empty(self) -> bool:
        return self._queue.empty()


def ensure_inbox_dir() -> str:
    try:
        os.makedirs(INBOX_DIR, exist_ok=True)
    except Exception:
        pass
    return INBOX_DIR


def pending_drop_count() -> int:
    if not os.path.isdir(INBOX_DIR):
        return 0
    try:
        return len(_candidate_files())
    except Exception:
        return 0


def _candidate_files() -> list[tuple[float, str]]:
    entries: list[tuple[float, str]] = []
    for name in os.listdir(INBOX_DIR)[:_MAX_SCAN_ENTRIES]:
        full = os.path.join(INBOX_DIR, name)
        if not os.path.isfile(full):
            continue
        if not name.lower().endswith(_ACCEPTED_SUFFIXES):
            continue
        try:
            entries.append((os.path.getmtime(full), full))
        except OSError:
            continue
    entries.sort()
    return entries


def read_latest_dropped_file() -> str | None:
    """Reads and archives the newest file dropped into the inbox.

    This is the only ingest path with no paste-size ceiling whatsoever, since
    the payload never travels through the PTY.
    """
    if not os.path.isdir(INBOX_DIR):
        return None
    try:
        entries = _candidate_files()
        if not entries:
            return None
        newest = entries[-1][1]
        with open(newest, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        _archive(newest)
        return content.strip() or None
    except Exception:
        return None


def _archive(path: str) -> None:
    try:
        os.makedirs(PROCESSED_DIR, exist_ok=True)
        dest = os.path.join(PROCESSED_DIR, f"{int(time.time())}_{os.path.basename(path)}")
        shutil.move(path, dest)
    except Exception:
        pass
