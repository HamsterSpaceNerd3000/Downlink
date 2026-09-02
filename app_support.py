"""Shared application paths and diagnostic logging."""

import os
import queue
import shutil
import threading
import time

APP_VERSION = "0.3.2"
APP_TITLE = f"Downlink v{APP_VERSION}"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Downlink")
DEFAULT_ICON_FILE = os.path.join(BASE_DIR, "downlink.ico")
ICON_FILE = os.path.join(APP_DATA_DIR, "downlink.ico")
DEFAULT_FEEDS_FILE = os.path.join(BASE_DIR, "downlink_feeds.json")
FEEDS_FILE = os.path.join(APP_DATA_DIR, "downlink_feeds.json")
DEBUG_FILE = os.path.join(APP_DATA_DIR, "downlink_log.log")
DEBUG_ENABLED = os.environ.get("DOWNLINK_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
_DEBUG_QUEUE = queue.Queue()


def _initialize_data_directory():
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    if not os.path.exists(FEEDS_FILE) and os.path.exists(DEFAULT_FEEDS_FILE):
        shutil.copyfile(DEFAULT_FEEDS_FILE, FEEDS_FILE)
    if not os.path.exists(ICON_FILE) and os.path.exists(DEFAULT_ICON_FILE):
        shutil.copyfile(DEFAULT_ICON_FILE, ICON_FILE)


_initialize_data_directory()


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
