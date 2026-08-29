"""Downlink: a hub for saved feeds and multi-stream VLC windows."""

import json
import ctypes
import html
import http.server
import multiprocessing
import os
import platform
import re
import threading
import time
import uuid
from io import BytesIO
from urllib.parse import urlparse
from urllib.request import urlopen

import tkinter as tk
from tkinter import messagebox, simpledialog

import customtkinter as ctk
import vlc
import webview

DIRECT_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".m3u8"}
FEEDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downlink_feeds.json")
DEBUG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downlink_log.log")
DEBUG_LOCK = threading.Lock()

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def redact_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "<direct-url>"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def debug_log(message: str):
    timestamp = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
    line = f"[{timestamp}] {message}\n"
    try:
        with DEBUG_LOCK:
            with open(DEBUG_FILE, "a", encoding="utf-8") as file:
                file.write(line)
    except OSError:
        pass


def looks_like_direct_media(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in DIRECT_EXTENSIONS)


def youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host == "youtu.be":
        return parsed.path.strip("/").split("/")[0] or None
    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            return parsed.query.split("v=", 1)[1].split("&", 1)[0] if "v=" in parsed.query else None
        match = re.match(r"^/(?:live|embed|shorts)/([^/?]+)", parsed.path)
        return match.group(1) if match else None
    return None


def is_youtube_url(url: str) -> bool:
    return youtube_video_id(url) is not None


def run_youtube_webview(video_id: str, title: str):
    """Run one YouTube WebView on the child process's required main thread."""
    class WrapperHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            origin = f"http://127.0.0.1:{self.server.server_address[1]}"
            safe_id = html.escape(video_id, quote=True)
            safe_origin = html.escape(origin, quote=True)
            page = f"""<!doctype html>
<html><head><meta name="referrer" content="origin">
<style>html,body,iframe{{width:100%;height:100%;margin:0;border:0;background:#000;overflow:hidden}}</style>
</head><body><iframe
src="https://www.youtube.com/embed/{safe_id}?autoplay=1&amp;mute=1&amp;playsinline=1&amp;rel=0&amp;enablejsapi=1&amp;origin={safe_origin}"
allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen referrerpolicy="origin"></iframe>
</body></html>""".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, _format, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), WrapperHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webview.create_window(title, f"http://127.0.0.1:{server.server_address[1]}/", width=960, height=600)
    webview.start(gui="edgechromium")
    server.shutdown()


class YouTubePlayerWindow:
    """Open an official YouTube iframe in a dedicated WebView2 process."""

    @classmethod
    def open(cls, url: str, title: str, host=None):
        video_id = youtube_video_id(url)
        if not video_id:
            raise ValueError("That is not a supported YouTube URL.")
        window_title = f"Downlink YouTube {uuid.uuid4().hex}"
        debug_log(f"[{title}] YOUTUBE_EMBED video_id={video_id}")
        process = multiprocessing.Process(target=run_youtube_webview, args=(video_id, window_title), daemon=True)
        process.start()
        if host is not None and platform.system() == "Windows":
            cls._attach_to_tk(process, window_title, host)
        return process

    @classmethod
    def _attach_to_tk(cls, process, window_title, host, attempts=0):
        if not host.winfo_exists() or not process.is_alive():
            return
        hwnd = ctypes.windll.user32.FindWindowW(None, window_title)
        if hwnd:
            parent = host.winfo_id()
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, -16)
            # WS_CHILD plus no caption/menu/frame prevents a second-looking window.
            chrome = 0x80000000 | 0x00C00000 | 0x00080000 | 0x00040000 | 0x00020000 | 0x00010000 | 0x00000080
            user32.SetWindowLongW(hwnd, -16, (style | 0x40000000) & ~chrome)
            user32.SetParent(hwnd, parent)
            cls._resize_embedded(hwnd, host)
            host.bind("<Configure>", lambda _event: cls._resize_embedded(hwnd, host), add="+")
            debug_log(f"YOUTUBE_EMBED attached hwnd={hwnd}")
            return
        if attempts < 40:
            host.after(250, lambda: cls._attach_to_tk(process, window_title, host, attempts + 1))

    @staticmethod
    def _resize_embedded(hwnd, host):
        if host.winfo_exists():
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, 0, 0, max(1, host.winfo_width()), max(1, host.winfo_height()), 0x0010 | 0x0040 | 0x0020
            )


class YouTubePane(ctk.CTkFrame):
    """A lightweight tile representing a YouTube feed opened in WebView2."""

    def __init__(self, master, title, original_url):
        super().__init__(master, fg_color="#080808", corner_radius=6)
        self.original_url = original_url
        self.title = title
        self.process = None
        self.video_frame = tk.Frame(self, bg="black")
        self.video_frame.pack(fill="both", expand=True, padx=2, pady=2)
        self.process = YouTubePlayerWindow.open(original_url, title, self.video_frame)

    def stop(self):
        if self.process and self.process.is_alive():
            self.process.terminate()
        self.destroy()


def resolve_stream(url: str) -> tuple[str, str | None, dict]:
    """Return a VLC-ready URL, thumbnail URL, and required HTTP headers."""
    debug_log(f"RESOLVE starting original={redact_url(url)}")
    if looks_like_direct_media(url):
        return url, None, {}
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("This link needs yt-dlp. Install it with: pip install yt-dlp") from exc
    options = {
        "quiet": True,
        "no_warnings": True,
        "format": "best[ext=m3u8]/best[protocol^=m3u8]/best[vcodec!=none][acodec!=none]/best",
        "noplaylist": True,
        "live_from_start": False,  # Prevents grabbing dead live-stream buffer segments
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        if "url" in info:
            debug_log(f"RESOLVE success original={redact_url(url)} direct={redact_url(info['url'])} protocol={info.get('protocol', 'unknown')}")
            return info["url"], info.get("thumbnail"), info.get("http_headers", {})
        for fmt in reversed(info.get("formats", [])):
            if fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":
                debug_log(f"RESOLVE success original={redact_url(url)} direct={redact_url(fmt['url'])} protocol={fmt.get('protocol', 'unknown')}")
                return fmt["url"], info.get("thumbnail"), fmt.get("http_headers", info.get("http_headers", {}))
    raise RuntimeError("Could not extract a playable stream from that link.")


class StreamPane(ctk.CTkFrame):
    """One independently playable stream inside a window."""

    def __init__(self, master, stream_url: str, title: str, headers=None, original_url: str = "", instance=None):
        super().__init__(master, fg_color="#080808", corner_radius=6)
        debug_log(f"[{title}] STREAM_START original={redact_url(original_url or stream_url)}")
        self.stream_title = title
        self.original_url = original_url or stream_url
        self.headers = headers or {}
        self._stopped = False
        self._dragging = False
        self._is_reconnecting = False

        self.video_frame = tk.Frame(self, bg="black")
        self.video_frame.pack(fill="both", expand=True, padx=2, pady=(2, 0))

        controls = ctk.CTkFrame(self, fg_color="#171717", height=38)
        controls.pack(fill="x", padx=2, pady=2)

        ctk.CTkLabel(controls, text=title, anchor="w").pack(side="left", fill="x", expand=True, padx=8)
        self.play_pause_btn = ctk.CTkButton(controls, text="⏸", width=34, height=26, command=self.toggle_play)
        self.play_pause_btn.pack(side="left", padx=(2, 4), pady=4)
        ctk.CTkButton(controls, text="⏹", width=34, height=26, command=self.stop).pack(side="left", padx=2, pady=4)

        self.time_slider = ctk.CTkSlider(controls, from_=0, to=1000, command=lambda _value: None)
        self.time_slider.pack(side="left", fill="x", expand=True, padx=4, pady=4)

        vlc_args = [
            "--no-xlib",
            "--avcodec-hw=none",
            "--network-caching=3000",
            "--live-caching=3000",
            "--clock-jitter=0",
            "--clock-synchro=0",
        ]
        user_agent = self.headers.get("User-Agent") or self.headers.get("user-agent")
        if user_agent:
            vlc_args.append(f"--http-user-agent={user_agent}")
        self.instance = instance or vlc.Instance(vlc_args)
        if not self.instance:
            raise RuntimeError("Failed to initialize libVLC instance.")

        self.player = self.instance.media_player_new()
        self._set_media(stream_url, self.headers)

        self.player.audio_set_volume(80)
        self._last_position = None
        self._last_progress_at = time.monotonic()
        self._buffering_since = None
        self._last_state = None
        self._started_at = time.monotonic()
        self._last_logged_position = None
        self._register_vlc_events()
        self.time_slider.bind("<ButtonPress-1>", lambda _event: setattr(self, "_dragging", True))
        self.time_slider.bind("<ButtonRelease-1>", self._on_slider_release)

        self.after(100, self._embed_video)
        self._update_loop()

    def _set_media(self, stream_url: str, headers: dict):
        """Creates and configures a new media object with stream options."""
        previous_url = getattr(self, "current_stream_url", None)
        url_status = "same" if previous_url == stream_url else "new"
        debug_log(f"[{self.stream_title}] SET_MEDIA url={redact_url(stream_url)} url_status={url_status}")
        self.current_stream_url = stream_url
        old_media = self.player.get_media()
        self.player.stop()
        if old_media:
            old_media.release()

        media = self.instance.media_new(stream_url)
        
        # Stream buffering and reconnect options
        media.add_option(":network-caching=3000")
        media.add_option(":live-caching=3000")
        media.add_option(":http-forward-cookies=true")
        media.add_option(":avcodec-skiploopfilter=4")

        # User-Agent is applied globally; these headers are media-specific.
        referer = headers.get("Referer") or headers.get("referer")
        cookie = headers.get("Cookie") or headers.get("cookie")
        if referer:
            media.add_option(f":http-referrer={referer}")
        if cookie:
            media.add_option(f":http-cookie={cookie}")

        self.player.set_media(media)
        debug_log(f"[{self.stream_title}] MEDIA_CREATED url={redact_url(stream_url)}")

    def _register_vlc_events(self):
        event_manager = self.player.event_manager()
        event_names = (
            "MediaPlayerOpening",
            "MediaPlayerPlaying",
            "MediaPlayerBuffering",
            "MediaPlayerEncounteredError",
            "MediaPlayerEndReached",
            "MediaPlayerStopped",
        )
        for event_name in event_names:
            event_type = getattr(vlc.EventType, event_name, None)
            if event_type is not None:
                event_manager.event_attach(event_type, self._on_vlc_event)

    def _on_vlc_event(self, event):
        event_name = getattr(event.type, "name", str(event.type))
        debug_log(f"[{self.stream_title}] VLC_EVENT={event_name}")

    def _embed_video(self):
        self.video_frame.update_idletasks()
        handle = self.video_frame.winfo_id()
        if platform.system() == "Windows":
            self.player.set_hwnd(handle)
        elif platform.system() == "Darwin":
            self.player.set_nsobject(handle)
        else:
            self.player.set_xwindow(handle)
        debug_log(f"[{self.stream_title}] PLAY requested runtime={time.monotonic() - self._started_at:.1f}s")
        self.player.play()

    def toggle_play(self):
        if self.player.is_playing():
            self.player.pause()
            self.play_pause_btn.configure(text="▶")
        else:
            self.player.play()
            self.play_pause_btn.configure(text="⏸")

    def _on_slider_release(self, _event):
        length = self.player.get_length()
        if length > 0:
            self.player.set_time(int(self.time_slider.get() / 1000 * length))
        self._dragging = False

    def _update_loop(self):
        if self._stopped:
            return

        if not self._is_reconnecting:
            state = self.player.get_state()
            now = time.monotonic()
            position = self.player.get_time()
            state_name = getattr(state, "name", str(state))
            if state != self._last_state:
                debug_log(f"[{self.stream_title}] STATE={state_name} position={position / 1000:.1f}s")
                self._last_state = state
            elif self._last_logged_position is None or abs(position - self._last_logged_position) >= 1000:
                debug_log(f"[{self.stream_title}] PROGRESS state={state_name} position={position / 1000:.1f}s advancing={position != self._last_logged_position}")
            self._last_logged_position = position

            if state in (vlc.State.Ended, vlc.State.Error):
                self._start_reconnect(f"VLC_{state_name.upper()}")
            elif state == vlc.State.Buffering:
                if self._buffering_since is None:
                    self._buffering_since = now
                elif now - self._buffering_since > 5:
                    debug_log(f"[{self.stream_title}] BUFFER_STALL duration={now - self._buffering_since:.1f}s")
                    self._start_reconnect("BUFFER_TIMEOUT")
            else:
                self._buffering_since = None
                if self._last_position is None or position > self._last_position:
                    self._last_progress_at = now
                elif state == vlc.State.Playing and now - self._last_progress_at > 5:
                    debug_log(f"[{self.stream_title}] PLAYBACK_STALL duration={now - self._last_progress_at:.1f}s")
                    self._start_reconnect("POSITION_TIMEOUT")
                self._last_position = position

            if not self._dragging and not self._is_reconnecting:
                length = self.player.get_length()
                if length > 0:
                    self.time_slider.set(self.player.get_time() / length * 1000)

        if self.winfo_exists():
            self.after(500, self._update_loop)

    def _start_reconnect(self, reason="UNKNOWN"):
        if self._stopped or self._is_reconnecting:
            return
        self._is_reconnecting = True
        self._recovery_position = self.player.get_time()
        debug_log(f"[{self.stream_title}] RECONNECT reason={reason} runtime={time.monotonic() - self._started_at:.1f}s")
        self._restart_current_media()

    def _restart_current_media(self):
        """Give VLC a chance to recover without fetching a new URL."""
        try:
            debug_log(f"[{self.stream_title}] LOCAL_RESTART requested")
            self.player.stop()
            self.player.play()
            self.after(5000, self._check_local_recovery)
        except Exception as exc:
            print(f"[{self.original_url}] Local VLC recovery failed: {exc}")
            self.reconnect_stream()

    def _check_local_recovery(self):
        if self._stopped or not self.winfo_exists():
            return
        position = self.player.get_time()
        if self.player.get_state() == vlc.State.Playing and position > self._recovery_position:
            self._last_position = position
            self._last_progress_at = time.monotonic()
            self._is_reconnecting = False
            return
        self.reconnect_stream()

    def reconnect_stream(self):
        def _reconnect():
            try:
                debug_log(f"[{self.stream_title}] RESOLVE_RECONNECT starting original={redact_url(self.original_url)}")
                new_url, _, headers = resolve_stream(self.original_url)
                self.after(0, lambda: self._apply_reconnect(new_url, headers))
            except Exception as exc:
                print(f"[{self.original_url}] Reconnect failed: {exc}")
                debug_log(f"[{self.stream_title}] RESOLVE_RECONNECT failed error={exc!r}")
                self.after(5000, self._retry_reconnect)

        threading.Thread(target=_reconnect, daemon=True).start()

    def _retry_reconnect(self):
        if self._stopped or not self.winfo_exists():
            return
        self.reconnect_stream()

    def _apply_reconnect(self, new_url, headers):
        if self._stopped or not self.winfo_exists():
            return
        try:
            debug_log(f"[{self.stream_title}] RECONNECT_APPLY url={redact_url(new_url)}")
            self._set_media(new_url, headers)
            self.player.play()
            self._last_position = None
            self._last_progress_at = time.monotonic()
            self._buffering_since = None
        except Exception as exc:
            print(f"Failed to restart stream: {exc}")
        finally:
            self._is_reconnecting = False

    def stop(self):
        debug_log(f"[{self.stream_title}] STOP runtime={time.monotonic() - self._started_at:.1f}s")
        self._stopped = True
        self.player.stop()
        media = self.player.get_media()
        if media:
            media.release()
        self.destroy()


class PlayerWindow(ctk.CTkToplevel):
    """A resizable window containing any number of stream panes."""

    def __init__(self, master, title="Downlink Window", instance=None):
        super().__init__(master)
        self.title(title)
        self.geometry("1100x700")
        self.minsize(560, 360)
        self.vlc_instance = instance or master.vlc_instance
        self.panes = []
        self.grid_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.grid_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.grid_frame.grid_columnconfigure((0, 1), weight=1)
        self.bind("<FocusIn>", lambda _event: master.set_active_window(self))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def add_stream(self, stream_url, title, headers=None, original_url=""):
        if is_youtube_url(original_url):
            pane = YouTubePane(self.grid_frame, title, original_url)
        else:
            pane = StreamPane(self.grid_frame, stream_url, title, headers, original_url, self.vlc_instance)
        self.panes.append(pane)
        columns = 2 if len(self.panes) > 1 else 1
        for index, item in enumerate(self.panes):
            item.grid(row=index // columns, column=index % columns, sticky="nsew", padx=5, pady=5)
        for row in range((len(self.panes) + columns - 1) // columns):
            self.grid_frame.grid_rowconfigure(row, weight=1)

    def _on_close(self):
        for pane in self.panes:
            pane.stop()
        self.destroy()


class FeedTile(ctk.CTkFrame):
    """Saved feed tile with a thumbnail placeholder, name, and quantity controls."""

    def __init__(self, master, feed, on_add, on_delete, on_drag):
        super().__init__(master, width=190, height=190, fg_color="#1b1b1b", corner_radius=8)
        self.feed = feed
        self.on_add = on_add
        self.on_delete = on_delete
        self.on_drag = on_drag
        self.pack_propagate(False)
        self.thumbnail = ctk.CTkLabel(self, text="VIDEO", height=105, fg_color="#292929", corner_radius=5)
        self.thumbnail.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkLabel(self, text=feed["name"], anchor="w", font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", padx=10)
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=8, pady=6)
        ctk.CTkButton(controls, text="-", width=28, height=25, command=self.decrease).pack(side="left")
        self.quantity_label = ctk.CTkLabel(controls, text="1", width=25)
        self.quantity_label.pack(side="left")
        ctk.CTkButton(controls, text="+", width=28, height=25, command=self.increase).pack(side="left")
        ctk.CTkButton(controls, text="Add", width=55, height=25, command=lambda: on_add(self)).pack(side="right")
        ctk.CTkButton(self, text="Remove", width=65, height=22, fg_color="transparent", command=lambda: on_delete(self)).pack(pady=(0, 4))
        for widget in (self, self.thumbnail):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)

    def increase(self):
        self.feed["quantity"] += 1
        self.quantity_label.configure(text=str(self.feed["quantity"]))

    def decrease(self):
        self.feed["quantity"] = max(1, self.feed["quantity"] - 1)
        self.quantity_label.configure(text=str(self.feed["quantity"]))

    def _start_drag(self, event):
        self._drag_start = (event.x_root, event.y_root)

    def _drag(self, event):
        self.on_drag(self, event.x_root - self._drag_start[0], event.y_root - self._drag_start[1])


class MainWindow(ctk.CTk):
    """Hub for saved feeds and playback windows."""

    def __init__(self):
        super().__init__()
        self.title("Downlink Hub")
        self.geometry("900x650")
        self.minsize(700, 500)
        self.feeds = self._load_feeds()
        self.windows = []
        self.active_window = None
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 8))
        ctk.CTkLabel(header, text="Downlink Hub", font=ctk.CTkFont(size=26, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="+ New window", width=125, command=self.new_window).pack(side="right")
        ctk.CTkButton(header, text="+ Add feed", width=105, command=self.add_feed_dialog).pack(side="right", padx=8)
        ctk.CTkLabel(self, text="Save feeds here, then place one or more into any playback window.", text_color="#999999").pack(anchor="w", padx=26, pady=(0, 15))
        self.feed_area = ctk.CTkScrollableFrame(self, label_text="Saved feeds")
        self.feed_area.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        self.status_label = ctk.CTkLabel(self, text="", text_color="#999999")
        self.status_label.pack(pady=(0, 8))
        self._render_feeds()

    def _load_feeds(self):
        try:
            with open(FEEDS_FILE, "r", encoding="utf-8") as file:
                feeds = json.load(file)
            return [{"name": item["name"], "url": item["url"], "thumbnail": item.get("thumbnail"), "quantity": 1} for item in feeds]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def _save_feeds(self):
        with open(FEEDS_FILE, "w", encoding="utf-8") as file:
            json.dump([{"name": f["name"], "url": f["url"], "thumbnail": f.get("thumbnail")} for f in self.feeds], file, indent=2)

    def _render_feeds(self):
        for child in self.feed_area.winfo_children():
            child.destroy()
        for index, feed in enumerate(self.feeds):
            tile = FeedTile(self.feed_area, feed, self.add_to_window, self.delete_feed, self.drag_tile)
            tile.grid(row=index // 4, column=index % 4, padx=8, pady=8, sticky="n")
            self._load_thumbnail(tile, feed)

    def _load_thumbnail(self, tile, feed):
        thumbnail_url = feed.get("thumbnail")
        if not thumbnail_url:
            return

        def fetch():
            try:
                from PIL import Image
                image = Image.open(BytesIO(urlopen(thumbnail_url, timeout=8).read()))
                image.thumbnail((174, 100))
                self.after(0, lambda: self._set_thumbnail(tile, image))
            except Exception:
                pass

        threading.Thread(target=fetch, daemon=True).start()

    def _set_thumbnail(self, tile, image):
        if tile.winfo_exists():
            tile.thumbnail_image = ctk.CTkImage(light_image=image, dark_image=image, size=image.size)
            tile.thumbnail.configure(image=tile.thumbnail_image, text="")

    def add_feed_dialog(self):
        url = simpledialog.askstring("Add feed", "Stream URL:", parent=self)
        if not url or not url.strip():
            return
        name = simpledialog.askstring("Name feed", "Display name:", initialvalue=urlparse(url).netloc or "New feed", parent=self)
        if not name:
            return
        self.feeds.append({"name": name.strip(), "url": url.strip(), "quantity": 1})
        self._save_feeds()
        self._render_feeds()

    def delete_feed(self, tile):
        self.feeds.remove(tile.feed)
        self._save_feeds()
        self._render_feeds()

    def drag_tile(self, tile, dx, _dy):
        current = self.feeds.index(tile.feed)
        target = max(0, min(len(self.feeds) - 1, current + (1 if dx > 80 else -1 if dx < -80 else 0)))
        if target != current:
            self.feeds.insert(target, self.feeds.pop(current))
            self._save_feeds()
            self._render_feeds()

    def new_window(self):
        window = PlayerWindow(self, title=f"Downlink Window {len(self.windows) + 1}", instance=self.vlc_instance)
        self.windows.append(window)
        self.active_window = window

    def set_active_window(self, window):
        if window.winfo_exists():
            self.active_window = window

    def add_to_window(self, tile):
        if self.active_window is None or not self.active_window.winfo_exists():
            self.new_window()
        window = self.active_window
        self.status_label.configure(text=f"Resolving {tile.feed['name']}...", text_color="#e0a030")
        threading.Thread(target=self._resolve_and_add, args=(tile.feed, window), daemon=True).start()

    def resolve_and_add(self, feed, window):
        try:
            if is_youtube_url(feed["url"]):
                debug_log(f"[{feed['name']}] YOUTUBE_ROUTE browser original={redact_url(feed['url'])}")
                self.after(0, lambda: window.add_stream(
                    "", feed["name"], original_url=feed["url"]
                ))
                return
            debug_log(f"[{feed['name']}] RESOLVE_FEED starting original={redact_url(feed['url'])}")
            stream_url, thumbnail, headers = resolve_stream(feed["url"])
            if thumbnail and feed.get("thumbnail") != thumbnail:
                feed["thumbnail"] = thumbnail
                self.after(0, self.save_data)
            # Make sure headers=headers is passed here:
            self.after(0, lambda: window.add_stream(stream_url, feed["name"], headers=headers))
        except Exception as exc:
            debug_log(f"[{feed['name']}] RESOLVE_FEED failed error={exc!r}")
            error_message = str(exc)
            self.after(0, lambda error_message=error_message: messagebox.showerror(
                "Couldn't play that link", error_message, parent=self
            ))

    def _show_error(self, message):
        self.status_label.configure(text="")
        messagebox.showerror("Couldn't play that link", message)


class ThemedForm(ctk.CTkToplevel):
    def __init__(self, master, title, fields, on_submit):
        super().__init__(master)
        self.title(title)
        self.geometry("420x220")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.on_submit = on_submit
        self.entries = {}
        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w", padx=24, pady=(20, 12))
        for key, label, value in fields:
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=24, pady=4)
            ctk.CTkLabel(row, text=label, width=90, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(row)
            entry.insert(0, value)
            entry.pack(side="left", fill="x", expand=True)
            self.entries[key] = entry
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=16)
        ctk.CTkButton(actions, text="Cancel", fg_color="transparent", command=self.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(actions, text="Save", command=self.submit).pack(side="right")

    def submit(self):
        values = {key: entry.get().strip() for key, entry in self.entries.items()}
        if all(values.values()):
            self.grab_release()
            self.destroy()
            self.on_submit(values)


class FeedTile(ctk.CTkFrame):
    def __init__(self, master, feed, on_drop, on_delete, on_thumbnail):
        super().__init__(master, fg_color="#1b1b1b", corner_radius=8)
        self.feed = feed
        self.on_drop = on_drop
        self.thumbnail = ctk.CTkLabel(self, text="VIDEO", height=88, fg_color="#292929", corner_radius=5)
        self.thumbnail.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkLabel(self, text=feed["name"], anchor="w", font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", padx=10)
        ctk.CTkLabel(self, text="Drag to a playback", text_color="#888888", anchor="w").pack(fill="x", padx=10, pady=(2, 8))
        ctk.CTkButton(self, text="Remove", width=70, height=23, fg_color="transparent", command=lambda: on_delete(self)).pack(pady=(0, 8))
        for widget in (self, self.thumbnail):
            widget.bind("<ButtonPress-1>", lambda _event: self.configure(border_width=2, border_color="#3b82f6"))
            widget.bind("<ButtonRelease-1>", self._release)
        on_thumbnail(self, feed)

    def _release(self, event):
        self.configure(border_width=0)
        self.on_drop(self, event.x_root, event.y_root)


class PlaybackTile(ctk.CTkFrame):
    def __init__(self, master, playback, callbacks):
        super().__init__(master, fg_color="#1b1b1b", corner_radius=8)
        self.playback = playback
        self.callbacks = callbacks
        ctk.CTkLabel(self, text="PLAYBACK WINDOW", text_color="#6ba4ff").pack(anchor="w", padx=14, pady=(12, 0))
        ctk.CTkLabel(self, text=playback["name"], font=ctk.CTkFont(size=17, weight="bold")).pack(anchor="w", padx=14, pady=2)
        self.count = ctk.CTkLabel(self, text="")
        self.count.pack(anchor="w", padx=14)
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=12, pady=14)
        ctk.CTkButton(controls, text="-", width=30, height=28, command=lambda: callbacks["count"](self, -1)).pack(side="left")
        ctk.CTkButton(controls, text="+", width=30, height=28, command=lambda: callbacks["count"](self, 1)).pack(side="left", padx=5)
        ctk.CTkButton(controls, text="Open", width=65, height=28, command=lambda: callbacks["open"](self)).pack(side="right")
        ctk.CTkButton(controls, text="Rename", width=70, height=28, fg_color="transparent", command=lambda: callbacks["rename"](self)).pack(side="right", padx=5)
        ctk.CTkButton(self, text="Delete", width=60, height=21, fg_color="transparent", command=lambda: callbacks["delete"](self)).pack(pady=(0, 8))
        self.refresh()

    def refresh(self):
        self.count.configure(text=f"{len(self.playback['feeds'])} feeds assigned")


class MainWindow(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Downlink Hub")
        self.geometry("1050x680")
        self.minsize(800, 520)
        self.vlc_instance = vlc.Instance([
            "--no-xlib",
            "--avcodec-hw=none",
            "--network-caching=3000",
            "--live-caching=3000",
            "--clock-jitter=0",
            "--clock-synchro=0",
        ])
        if not self.vlc_instance:
            raise RuntimeError("Failed to initialize libVLC instance.")
        self.feeds, self.playbacks = self._load_data()
        self.windows = {}
        self._build_ui()
        self.render_feeds()
        self.render_playbacks()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=22, pady=(18, 10))
        ctk.CTkLabel(header, text="Downlink Hub", font=ctk.CTkFont(size=26, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="+ Playback", width=105, command=self.add_playback).pack(side="right")
        ctk.CTkButton(header, text="+ Feed", width=85, command=self.add_feed).pack(side="right", padx=8)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18)
        left = ctk.CTkFrame(body, width=235, fg_color="#141414")
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)
        ctk.CTkLabel(left, text="SAVED FEEDS", anchor="w", font=ctk.CTkFont(size=12, weight="bold")).pack(fill="x", padx=14, pady=14)
        self.feed_area = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.feed_area.pack(fill="both", expand=True, padx=5)
        center = ctk.CTkFrame(body, fg_color="transparent")
        center.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(center, text="PLAYBACK WINDOWS", anchor="w", font=ctk.CTkFont(size=12, weight="bold")).pack(fill="x", padx=4, pady=14)
        self.playback_area = ctk.CTkScrollableFrame(center, fg_color="transparent")
        self.playback_area.pack(fill="both", expand=True)
        self.status = ctk.CTkLabel(self, text="Drag a feed from the left onto a playback window.", text_color="#888888")
        self.status.pack(pady=8)

    def _load_data(self):
        try:
            with open(FEEDS_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, list):
                return data, []
            return data.get("feeds", []), data.get("playbacks", [])
        except (OSError, ValueError, TypeError):
            return [], []

    def save_data(self):
        with open(FEEDS_FILE, "w", encoding="utf-8") as file:
            json.dump({"feeds": self.feeds, "playbacks": self.playbacks}, file, indent=2)

    def render_feeds(self):
        for child in self.feed_area.winfo_children():
            child.destroy()
        for feed in self.feeds:
            FeedTile(self.feed_area, feed, self.drop_feed, self.delete_feed, self.load_thumbnail).pack(fill="x", padx=4, pady=6)

    def render_playbacks(self):
        for child in self.playback_area.winfo_children():
            child.destroy()
        callbacks = {"open": self.open_playback, "count": self.change_count, "delete": self.delete_playback, "rename": self.rename_playback}
        for playback in self.playbacks:
            PlaybackTile(self.playback_area, playback, callbacks).pack(fill="x", padx=8, pady=8)

    def load_thumbnail(self, tile, feed):
        if not feed.get("thumbnail"):
            return
        def fetch():
            try:
                from PIL import Image
                image = Image.open(BytesIO(urlopen(feed["thumbnail"], timeout=8).read()))
                image.thumbnail((194, 84))
                self.after(0, lambda: self.set_thumbnail(tile, image))
            except Exception:
                pass
        threading.Thread(target=fetch, daemon=True).start()

    def set_thumbnail(self, tile, image):
        if tile.winfo_exists():
            tile.thumbnail_image = ctk.CTkImage(light_image=image, dark_image=image, size=image.size)
            tile.thumbnail.configure(image=tile.thumbnail_image, text="")

    def add_feed(self):
        ThemedForm(self, "Add saved feed", [("name", "Name", ""), ("url", "Link", "https://...")], self.finish_feed)

    def finish_feed(self, values):
        self.feeds.append({"name": values["name"], "url": values["url"], "thumbnail": None})
        self.save_data()
        self.render_feeds()

    def delete_feed(self, tile):
        self.feeds.remove(tile.feed)
        self.save_data()
        self.render_feeds()

    def add_playback(self):
        ThemedForm(self, "New playback window", [("name", "Name", "Playback")], self.finish_playback)

    def finish_playback(self, values):
        self.playbacks.append({"name": values["name"], "feeds": []})
        self.save_data()
        self.render_playbacks()

    def rename_playback(self, tile):
        ThemedForm(self, "Rename playback", [("name", "Name", tile.playback["name"])], lambda values: self.finish_rename(tile, values))

    def finish_rename(self, tile, values):
        tile.playback["name"] = values["name"]
        self.save_data()
        self.render_playbacks()

    def delete_playback(self, tile):
        self.playbacks.remove(tile.playback)
        window = self.windows.pop(id(tile.playback), None)
        if window and window.winfo_exists():
            window._on_close()
        self.save_data()
        self.render_playbacks()

    def change_count(self, tile, amount):
        feeds = tile.playback["feeds"]
        if amount > 0 and feeds:
            feeds.append(feeds[-1])
        elif amount < 0 and feeds:
            feeds.pop()
        tile.refresh()
        self.save_data()

    def drop_feed(self, feed_tile, x, y):
        widget = self.winfo_containing(x, y)
        while widget is not None and not isinstance(widget, PlaybackTile):
            widget = widget.master
        if isinstance(widget, PlaybackTile):
            widget.playback["feeds"].append(feed_tile.feed["name"])
            widget.refresh()
            self.save_data()
            self.status.configure(text=f"Added {feed_tile.feed['name']} to {widget.playback['name']}.")

    def open_playback(self, tile):
        playback = tile.playback
        window = self.windows.get(id(playback))
        if window is None or not window.winfo_exists():
            window = PlayerWindow(self, title=playback["name"], instance=self.vlc_instance)
            self.windows[id(playback)] = window
        for feed_name in playback["feeds"]:
            feed = next((item for item in self.feeds if item["name"] == feed_name), None)
            if feed:
                threading.Thread(target=self.resolve_and_add, args=(feed, window), daemon=True).start()

    def set_active_window(self, window):
        if window.winfo_exists():
            self.active_window = window

    def resolve_and_add(self, feed, window):
        try:
            if is_youtube_url(feed["url"]):
                debug_log(f"[{feed['name']}] YOUTUBE_ROUTE browser original={redact_url(feed['url'])}")
                self.after(0, lambda: window.add_stream(
                    "", feed["name"], original_url=feed["url"]
                ))
                return
            debug_log(f"[{feed['name']}] RESOLVE_FEED starting original={redact_url(feed['url'])}")
            stream_url, thumbnail, headers = resolve_stream(feed["url"])
            if thumbnail and feed.get("thumbnail") != thumbnail:
                feed["thumbnail"] = thumbnail
                self.after(0, self.save_data)
            self.after(0, lambda: window.add_stream(
                stream_url, feed["name"], headers=headers, original_url=feed["url"]
            ))
        except Exception as exc:
            debug_log(f"[{feed['name']}] RESOLVE_FEED failed error={exc!r}")
            error_message = str(exc)
            self.after(0, lambda error_message=error_message: messagebox.showerror(
                "Couldn't play that link", error_message, parent=self
            ))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = MainWindow()
    app.mainloop()