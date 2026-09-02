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

from app_support import APP_TITLE, debug_log
from streaming import resolve_stream, youtube_video_id


class RollingGifBuffer:
    """Keeps a bounded history of recent stream frames for pre-click GIF exports."""

    FRAME_RATE = 5
    MAX_SECONDS = 60

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
        frame_count = min(len(self.frames), max(1, round(duration * self.FRAME_RATE)))
        with self._lock:
            frames = [frame.copy() for _, frame in list(self.frames)[-frame_count:]]
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
                if width > 320:
                    frame = cv2.resize(frame, (320, round(height * 320 / width)))
                encoded, image_data = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80]
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


def export_gif(frame_buffer, duration, speed_up):
    """Save the recent frame buffer as a GIF in the user's Downloads folder."""
    frames = [Image.open(BytesIO(frame_data)).convert("RGB") for frame_data in frame_buffer.recent_frames(duration)]
    downloads = Path.home() / "Downloads"
    downloads.mkdir(exist_ok=True)
    output_path = downloads / f"downlink-{datetime.now():%Y%m%d-%H%M%S}.gif"
    frame_duration = 100 if speed_up else 200
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=frame_duration,
        loop=0,
        optimize=False,
    )
    debug_log(f"GIF_EXPORT saved={output_path} duration={duration}s speed_up={speed_up} frames={len(frames)}")
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
            f'<aside class="feed-toolbar"><button type="button" onclick="copyCredit({index}, this)">Link</button><button type="button" onclick="createQuickGif({index}, this)">Quick</button><button type="button" onclick="openGifDialog({index})">GIF</button></aside>'
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
.gif-dialog {{ width:250px; border:1px solid #4a525e; border-radius:6px; padding:0; background:#20242a; color:#f4f6f8; font:13px sans-serif; }}
.gif-dialog::backdrop {{ background:rgba(0, 0, 0, .55); }}
.gif-form {{ display:flex; flex-direction:column; gap:14px; padding:16px; }}
.gif-form h2 {{ margin:0; font-size:16px; font-weight:600; }}
.gif-status {{ min-height:16px; margin:0; color:#aeb6c2; font-size:12px; }}
.gif-form label {{ display:flex; flex-direction:column; gap:6px; }}
.gif-form input[type="number"] {{ box-sizing:border-box; width:100%; border:1px solid #4a525e; border-radius:4px; background:#121416; color:#f4f6f8; padding:7px; }}
.gif-form .checkbox-label {{ flex-direction:row; align-items:center; gap:8px; }}
.gif-actions {{ display:flex; justify-content:flex-end; gap:8px; }}
.gif-actions button {{ width:auto; padding:7px 12px; }}
.gif-actions .primary {{ background:#2f6fbd; border-color:#4386d9; }}
.gif-actions .primary:hover {{ background:#3a7ed1; }}
</style></head><body><div class="video-grid">{''.join(cells)}</div><dialog class="gif-dialog" id="gif-dialog"><form class="gif-form" method="dialog" onsubmit="saveGifSettings(event)"><h2>Create GIF</h2><label>Duration (seconds)<input id="gif-duration" type="number" min="1" max="60" value="30" required></label><label class="checkbox-label"><input id="gif-speed-up" type="checkbox"> Speed up</label><p class="gif-status" id="gif-status"></p><div class="gif-actions"><button type="button" onclick="closeGifDialog()">Cancel</button><button class="primary" id="gif-create" type="submit">Create</button></div></form></dialog><script>
const feeds = {json.dumps(feeds)};
let lastGifSettings = {{duration: 30, speed_up: false}};
let selectedFeedIndex = 0;
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
function openGifDialog(feedIndex) {{
    selectedFeedIndex = feedIndex;
    document.getElementById('gif-dialog').showModal();
}}
function closeGifDialog() {{
    document.getElementById('gif-dialog').close();
}}
async function saveGifSettings(event) {{
    event.preventDefault();
    const duration = Number(document.getElementById('gif-duration').value);
    const speedUp = document.getElementById('gif-speed-up').checked;
    const createButton = document.getElementById('gif-create');
    const status = document.getElementById('gif-status');
        lastGifSettings = {{duration, speed_up: speedUp}};
        await createGif(selectedFeedIndex, lastGifSettings, createButton, status);
}}
    async function createQuickGif(feedIndex, button) {{
        await createGif(feedIndex, lastGifSettings, button);
}}
    async function createGif(feedIndex, settings, button, status = null) {{
        const originalLabel = button.textContent;
        button.disabled = true;
        button.textContent = 'Wait';
        if (status) status.textContent = 'Creating GIF from recent footage...';
    try {{
        const response = await fetch('/gif', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{...settings, feed_index: feedIndex}}),
        }});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'GIF export failed.');
            if (status) status.textContent = `Saved to Downloads: ${{result.filename}}`;
            button.textContent = 'Saved';
    }} catch (error) {{
            if (status) status.textContent = error.message;
            button.textContent = 'Error';
    }} finally {{
            setTimeout(() => button.textContent = originalLabel, 1200);
            button.disabled = false;
    }}
}}
</script></body></html>""".encode("utf-8")


def run_youtube_webview(feeds, slot_count, title):
    """Run one WebView containing all YouTube iframes for a playback."""
    page = _youtube_page(feeds, slot_count, title)
    frame_buffers = [RollingGifBuffer(feed["source_url"]) for feed in feeds]
    for frame_buffer in frame_buffers:
        frame_buffer.start()

    class WrapperHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, _format, *_args):
            return

        def do_POST(self):
            if self.path != "/gif":
                self.send_error(404)
                return
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                options = json.loads(self.rfile.read(content_length))
                duration = max(1, int(options["duration"]))
                feed_index = int(options["feed_index"])
                if not 0 <= feed_index < len(frame_buffers):
                    raise ValueError("The selected feed is unavailable.")
                filename = export_gif(
                    frame_buffers[feed_index], duration, bool(options.get("speed_up"))
                )
                response, status = {"filename": filename}, 200
            except Exception as exc:
                debug_log(f"GIF_EXPORT failed: {exc}")
                response, status = {"error": str(exc)}, 500
            encoded = json.dumps(response).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), WrapperHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webview.create_window(
        title,
        f"http://127.0.0.1:{server.server_address[1]}/",
        width=960,
        height=600,
        hidden=True,
    )
    webview.start(gui="edgechromium")
    for frame_buffer in frame_buffers:
        frame_buffer.stop()
    server.shutdown()


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
