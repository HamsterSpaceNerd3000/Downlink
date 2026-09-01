"""Shared application paths and diagnostic logging."""

import os
import queue
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDS_FILE = os.path.join(BASE_DIR, "downlink_feeds.json")
DEBUG_FILE = os.path.join(BASE_DIR, "downlink_log.log")
DEBUG_ENABLED = os.environ.get("DOWNLINK_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
_DEBUG_QUEUE = queue.Queue()


def _debug_worker():
    while True:
        line = _DEBUG_QUEUE.get()
        if line is None:
            _DEBUG_QUEUE.task_done()
            break
        try:
            with open(DEBUG_FILE, "a", encoding="utf-8") as file:
                file.write(line)
        except OSError:
            pass
        finally:
            _DEBUG_QUEUE.task_done()


if DEBUG_ENABLED:
    _DEBUG_THREAD = threading.Thread(target=_debug_worker, daemon=True)
    _DEBUG_THREAD.start()


def debug_log(message: str):
    if not DEBUG_ENABLED:
        return
    timestamp = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
    line = f"[{timestamp}] {message}\n"
    try:
        _DEBUG_QUEUE.put_nowait(line)
    except Exception:
        pass
