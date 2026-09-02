"""Shared YouTube browser surface for a playback window."""

import ctypes
from datetime import datetime
import html
import http.server
import json
import multiprocessing
import platform
import threading
import time
import uuid
from collections import deque
from io import BytesIO
from pathlib import Path

import cv2
import tkinter as tk
import customtkinter as ctk
import webview
from PIL import Image

from app_support import (
    APP_TITLE,
    YOUTUBE_PROFILE_DIR,
    YOUTUBE_SESSION_FILE,
    debug_log,
)
from streaming import resolve_stream, youtube_video_id

webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False

def _set_youtube_signed_in(signed_in: bool):
    try:
        with open(YOUTUBE_SESSION_FILE, "w", encoding="utf-8") as file:
            json.dump({"signed_in": signed_in}, file)
    except OSError as exc:
        debug_log(f"YOUTUBE_SESSION_WRITE_FAILED error={exc!r}")


def youtube_signed_in() -> bool:
    try:
        with open(YOUTUBE_SESSION_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return bool(data.get("signed_in", False))
    except (OSError, ValueError, TypeError):
        return False

class RollingGifBuffer:
    """Keeps a bounded history of recent stream frames for pre-click GIF exports."""

    FRAME_RATE = 3
    MAX_SECONDS = 60
    MAX_WIDTH = 640
    JPEG_QUALITY = 90

    def __init__(self, source_url):
        self.source_url = source_url
        self.frames = deque(maxlen=self.FRAME_RATE * self.MAX_SECONDS)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def recent_frames(self, duration):
        with self._lock:
            frame_count = min(len(self.frames), max(1, round(duration * self.FRAME_RATE)))
            frames = [frame_data for _, frame_data in list(self.frames)[-frame_count:]]
        if not frames:
            raise RuntimeError("GIF buffer is still starting. Try again in a moment.")
        return frames

    def _capture_loop(self):
        try:
            stream_url, _, _ = resolve_stream(self.source_url)
            capture = cv2.VideoCapture(stream_url)
            if not capture.isOpened():
                raise RuntimeError("Could not open the video stream for GIF capture.")
            frame_interval = 1 / self.FRAME_RATE
            next_frame_at = time.monotonic()
            while not self._stop_event.is_set():
                ok, frame = capture.read()
                if not ok:
                    raise RuntimeError("The video stream stopped while filling the GIF buffer.")
                now = time.monotonic()
                if now < next_frame_at:
                    continue
                next_frame_at = now + frame_interval
                height, width = frame.shape[:2]
                if width > self.MAX_WIDTH:
                    frame = cv2.resize(
                        frame, (self.MAX_WIDTH, round(height * self.MAX_WIDTH / width))
                    )
                encoded, image_data = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.JPEG_QUALITY]
                )
                if not encoded:
                    continue
                with self._lock:
                    self.frames.append((now, image_data.tobytes()))
        except Exception as exc:
            debug_log(f"GIF_BUFFER failed: {exc}")
        finally:
            if "capture" in locals():
                capture.release()


def export_webp(frame_buffer, duration, speed_up):
    """Save the recent frame buffer as an animated WebP in the Downloads folder."""
    frames = [Image.open(BytesIO(frame_data)).convert("RGB") for frame_data in frame_buffer.recent_frames(duration)]
    downloads = Path.home() / "Downloads"
    downloads.mkdir(exist_ok=True)
    output_path = downloads / f"downlink-{datetime.now():%Y%m%d-%H%M%S}.webp"
    frame_duration = 100 if speed_up else 200
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=frame_duration,
        loop=0,
        format="WEBP",
        quality=80,
        method=0,
    )
    debug_log(f"WEBP_EXPORT saved={output_path} duration={duration}s speed_up={speed_up} frames={len(frames)}")
    return output_path.name


def _youtube_page(feeds, slot_count, playback_title):
    if slot_count <= 1:
        columns, rows = 1, 1
    elif slot_count <= 3:
        columns, rows = slot_count, 1
    elif slot_count == 4:
        columns, rows = 2, 2
    else:
        columns, rows = 3, (slot_count + 2) // 3
    feed_by_slot = {feed["slot_index"]: (index, feed) for index, feed in enumerate(feeds)}
    cells = []
    for slot_index in range(slot_count):
        indexed_feed = feed_by_slot.get(slot_index)
        if indexed_feed is None:
            cells.append('<div class="cell empty"><div class="placeholder-panel"></div></div>')
            continue
        index, feed = indexed_feed
        safe_id = html.escape(feed["video_id"], quote=True)
        cells.append(
            '<div class="cell">'
            f'<iframe src="https://www.youtube.com/embed/{safe_id}?autoplay=1&amp;mute=1&amp;playsinline=1&amp;rel=0" '
            'allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>'
            f'<aside class="feed-toolbar"><button type="button" onclick="copyCredit({index}, this)">Link</button></aside>'
            '</div>'
        )
    return f"""<!doctype html>
<html><head><meta name="referrer" content="origin"><style>
html, body {{ width:100%; height:100%; margin:0; background:#000; overflow:hidden; }}
body {{ display:flex; }}
.video-grid {{ display:grid; flex:1 1 auto; min-width:0; gap:2px; grid-template-columns:repeat({columns}, minmax(0, 1fr)); grid-template-rows:repeat({rows}, minmax(0, 1fr)); }}
.cell {{ display:flex; flex:1 1 0; min-width:0; }}
.cell.empty {{ box-sizing:border-box; display:block; padding:2px; background:#080808; }}
.placeholder-panel {{ box-sizing:border-box; width:100%; height:100%; border-radius:4px; background:#121212; }}
iframe {{ flex:1 1 auto; min-width:0; height:100%; border:0; display:block; background:#000; }}
.feed-toolbar {{ box-sizing:border-box; width:0; flex:0 0 0; overflow:hidden; display:flex; align-items:center; justify-content:flex-start; flex-direction:column; gap:6px; padding:10px 0; opacity:0; background:#1b1e23; border-left:0 solid #3c434d; transition:width 120ms ease, flex-basis 120ms ease, padding 120ms ease, opacity 100ms ease; }}
.cell:hover .feed-toolbar {{ width:56px; flex-basis:56px; padding:10px 5px; opacity:1; border-left-width:1px; }}
.feed-toolbar button {{ width:100%; border:1px solid #4a525e; border-radius:4px; background:#252a31; color:#fff; cursor:pointer; font:13px sans-serif; padding:7px 4px; }}
.feed-toolbar button:hover {{ background:#343b45; }}
</style></head><body><div class="video-grid">{''.join(cells)}</div><script>
const feeds = {json.dumps(feeds)};
function copyCredit(feedIndex, button) {{
    const creditText = feeds[feedIndex].credit_text;
    navigator.clipboard.writeText(creditText).catch(() => {{
        const input = document.createElement('textarea');
        input.value = creditText;
        document.body.appendChild(input);
        input.select();
        document.execCommand('copy');
        input.remove();
    }});
    button.textContent = 'Copied';
    setTimeout(() => button.textContent = 'Link', 1200);
}}
</script></body></html>""".encode("utf-8")


def run_youtube_webview(feeds, slot_count, title):
    """Run one WebView containing all YouTube iframes for a playback."""
    page = _youtube_page(feeds, slot_count, title)

    class WrapperHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, _format, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), WrapperHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webview.create_window(
        title,
        f"http://127.0.0.1:{server.server_address[1]}/",
        width=960,
        height=600,
        hidden=True,
    )
    webview.start(gui="edgechromium", private_mode=False, storage_path=YOUTUBE_PROFILE_DIR)
    server.shutdown()


def run_youtube_account(action):
    """Manage the persistent YouTube browser session."""
    title = f"{APP_TITLE} YouTube Account"

    if action == "signin":
        window = webview.create_window(
            title,
            "https://www.youtube.com/",
            width=520,
            height=700,
            hidden=False,
        )

        def check_login(attempt=0):
            try:
                result = window.evaluate_js(r"""
                    (() => {
                        const avatar =
                            document.querySelector('#avatar-btn') ||
                            document.querySelector('ytd-topbar-menu-button-renderer#avatar-btn');

                        const accountButton =
                            document.querySelector('button[aria-label*="Account"]') ||
                            document.querySelector('button[aria-label*="account"]');

                        const signIn =
                            document.querySelector('a[href*="accounts.google.com/ServiceLogin"]') ||
                            document.querySelector('ytd-button-renderer a[href*="ServiceLogin"]');

                        const signedIn =
                            (!!avatar || !!accountButton) && !signIn;

                        return {
                            signed_in: signedIn,
                            url: window.location.href,
                            title: document.title
                        };
                    })()
                """)

                signed_in = (
                    isinstance(result, dict)
                    and result.get("signed_in") is True
                )

                debug_log(
                    f"YOUTUBE_LOGIN_CHECK attempt={attempt} "
                    f"signed_in={signed_in} "
                    f"url={result.get('url') if isinstance(result, dict) else 'unknown'}"
                )

                if signed_in:
                    _set_youtube_signed_in(True)
                    debug_log("YOUTUBE_SIGNIN_DETECTED")
                    return

                # Give YouTube more time to finish rendering/login redirects.
                if attempt < 20:
                    window.evaluate_js(
                        "setTimeout(() => {}, 100)"
                    )
                    threading.Timer(
                        0.75,
                        lambda: check_login(attempt + 1)
                    ).start()
                    return

                # Do not overwrite an already-known signed-in session
                # just because the final DOM check was inconclusive.
                debug_log("YOUTUBE_SIGNIN_NOT_CONFIRMED")

            except Exception as exc:
                debug_log(
                    f"YOUTUBE_SIGNIN_CHECK_FAILED "
                    f"attempt={attempt} error={exc!r}"
                )

                if attempt < 20:
                    threading.Timer(
                        0.75,
                        lambda: check_login(attempt + 1)
                    ).start()

        def on_loaded(_window=None):
            threading.Timer(
                2.0,
                lambda: check_login(0)
            ).start()

        def on_closed():
            # The page may have just finished a Google → YouTube redirect.
            # Give the browser a moment before the final check.
            threading.Timer(
                0.5,
                lambda: check_login(0)
            ).start()

        window.events.loaded += on_loaded
        window.events.closed += on_closed

        webview.start(
            gui="edgechromium",
            private_mode=False,
            storage_path=YOUTUBE_PROFILE_DIR,
        )
        return

    if action == "signout":
        _set_youtube_signed_in(False)

        window = webview.create_window(
            title,
            "https://www.youtube.com/",
            width=520,
            height=700,
            hidden=True,
        )

        def clear_session():
            try:
                window.clear_cookies()
                _set_youtube_signed_in(False)
                window.destroy()
            except Exception as exc:
                debug_log(f"YOUTUBE_SIGNOUT_FAILED error={exc!r}")

        webview.start(
            clear_session,
            gui="edgechromium",
            private_mode=False,
            storage_path=YOUTUBE_PROFILE_DIR,
        )
        return


class YouTubeBrowser(ctk.CTkFrame):
    """One embedded browser surface containing a playback's YouTube feeds."""

    def __init__(self, master, feed_slots, slot_count, title):
        super().__init__(master, fg_color="#080808", corner_radius=6)
        self.feeds = [
            {
                "slot_index": slot_index,
                "video_id": youtube_video_id(feed["url"]),
                "source_url": feed["url"],
                "credit_text": f"[ Credit: [{feed.get('name', 'Source')}](<{feed.get('credit_url', feed['url'])}>) ]",
            }
            for slot_index, feed in feed_slots
            if youtube_video_id(feed.get("url", ""))
        ]
        if not self.feeds:
            raise ValueError("No supported YouTube URLs were provided.")

        self.process = None
        self.browser_title = f"{APP_TITLE} YouTube {uuid.uuid4().hex}"
        self.video_frame = tk.Frame(self, bg="black")
        self.video_frame.pack(fill="both", expand=True, padx=2, pady=2)
        debug_log(f"[{title}] YOUTUBE_BROWSER_START count={len(self.feeds)}")
        self.process = multiprocessing.Process(
            target=run_youtube_webview,
            args=(self.feeds, slot_count, self.browser_title),
            daemon=True,
        )
        self.process.start()
        if platform.system() == "Windows":
            self.after(100, self._attach)

    def _attach(self, attempts=0):
        if not self.winfo_exists() or not self.process.is_alive():
            return
        hwnd = ctypes.windll.user32.FindWindowW(None, self.browser_title)
        if hwnd:
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, -16)
            chrome = 0x80000000 | 0x00C00000 | 0x00080000 | 0x00040000 | 0x00020000 | 0x00010000 | 0x00000080
            user32.SetWindowLongW(hwnd, -16, (style | 0x40000000) & ~chrome)
            user32.SetParent(hwnd, self.video_frame.winfo_id())
            user32.ShowWindow(hwnd, 5)
            self._resize(hwnd)
            self.video_frame.bind("<Configure>", lambda _event: self._resize(hwnd), add="+")
            debug_log(f"YOUTUBE_BROWSER_ATTACHED hwnd={hwnd}")
            return
        if attempts < 40:
            self.after(250, lambda: self._attach(attempts + 1))

    def _resize(self, hwnd):
        if self.winfo_exists():
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, 0, 0, max(1, self.video_frame.winfo_width()),
                max(1, self.video_frame.winfo_height()), 0x0010 | 0x0040 | 0x0020
            )

    def stop(self):
        debug_log("YOUTUBE_BROWSER_STOP")
        if self.process and self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)
        self.destroy()
