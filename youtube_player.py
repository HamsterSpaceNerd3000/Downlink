"""Shared YouTube browser surface for a playback window."""

import ctypes
import html
import http.server
import json
import multiprocessing
import platform
import threading
import uuid

import tkinter as tk
import customtkinter as ctk
import webview

from app_support import debug_log
from streaming import youtube_video_id


def _youtube_page(video_ids, credit_name, credit_url):
    columns = min(3, max(1, len(video_ids)))
    row_divs = []
    for start in range(0, len(video_ids), columns):
        cells = []
        for video_id in video_ids[start:start + columns]:
            safe_id = html.escape(video_id, quote=True)
            cells.append(
                '<div class="cell">'
                f'<iframe src="https://www.youtube.com/embed/{safe_id}?autoplay=1&amp;mute=1&amp;playsinline=1&amp;rel=0" '
                'allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>'
                '</div>'
            )
        row_divs.append(f'<div class="row">{"".join(cells)}</div>')
    return f"""<!doctype html>
<html><head><meta name="referrer" content="origin"><style>
html, body {{ width:100%; height:100%; margin:0; background:#000; overflow:hidden; }}
body {{ display:flex; }}
.video-grid {{ display:flex; flex:1 1 auto; flex-direction:column; min-width:0; gap:2px; }}
.row {{ display:flex; flex:1 1 0; min-height:0; gap:2px; }}
.cell {{ flex:1 1 0; min-width:0; }}
iframe {{ width:100%; height:100%; border:0; display:block; background:#000; }}
.feed-toolbar {{ box-sizing:border-box; width:0; flex:0 0 0; overflow:hidden; display:flex; align-items:center; justify-content:flex-start; flex-direction:column; gap:6px; padding:10px 0; opacity:0; background:#1b1e23; border-left:0 solid #3c434d; transition:width 120ms ease, flex-basis 120ms ease, padding 120ms ease, opacity 100ms ease; }}
body:hover .feed-toolbar {{ width:64px; flex-basis:64px; padding:10px 6px; opacity:1; border-left-width:1px; }}
.feed-toolbar button {{ width:100%; border:1px solid #4a525e; border-radius:4px; background:#252a31; color:#fff; cursor:pointer; font:13px sans-serif; padding:7px 4px; }}
.feed-toolbar button:hover {{ background:#343b45; }}
</style></head><body><div class="video-grid">{''.join(row_divs)}</div><aside class="feed-toolbar"><button type="button" onclick="copyCredit(this)">Link</button><button type="button" disabled title="Coming soon">GIF</button></aside><script>
const creditText = {json.dumps(f'[ Credit: [{credit_name}](<{credit_url}>) ]')};
function copyCredit(button) {{
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


def run_youtube_webview(video_ids, title, credit_name, credit_url):
    """Run one WebView containing all YouTube iframes for a playback."""
    page = _youtube_page(video_ids, credit_name, credit_url)

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
    webview.start(gui="edgechromium")
    server.shutdown()


class YouTubeBrowser(ctk.CTkFrame):
    """One embedded browser surface containing a playback's YouTube feeds."""

    def __init__(self, master, feeds, title):
        super().__init__(master, fg_color="#080808", corner_radius=6)
        self.video_ids = [video_id for video_id in (youtube_video_id(feed["url"]) for feed in feeds) if video_id]
        if not self.video_ids:
            raise ValueError("No supported YouTube URLs were provided.")
        credit_feed = feeds[0]
        self.credit_name = credit_feed.get("name", "Source")
        self.credit_url = credit_feed.get("credit_url", credit_feed["url"])

        self.process = None
        self.browser_title = f"Downlink YouTube {uuid.uuid4().hex}"
        self.video_frame = tk.Frame(self, bg="black")
        self.video_frame.pack(fill="both", expand=True, padx=2, pady=2)
        debug_log(f"[{title}] YOUTUBE_BROWSER_START count={len(self.video_ids)}")
        self.process = multiprocessing.Process(
            target=run_youtube_webview,
            args=(self.video_ids, self.browser_title, self.credit_name, self.credit_url),
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
