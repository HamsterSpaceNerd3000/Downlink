"""Downlink: a hub for saved feeds and multi-stream VLC windows."""
import json
import math
import multiprocessing
import platform
import queue
import threading
import time
from io import BytesIO
from urllib.request import urlopen

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
import vlc

from app_support import (
    APP_TITLE,
    FEEDS_FILE,
    ICON_FILE,
    debug_log,
)
from streaming import is_youtube_url, redact_url, resolve_stream, youtube_video_id
from youtube_player import YouTubeBrowser, run_youtube_account, youtube_signed_in
from youtube_player import RollingGifBuffer, export_webp

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class StreamPane(ctk.CTkFrame):
    """One independently playable stream inside a window."""

    def __init__(self, master, stream_url: str, title: str, headers=None, original_url: str = "", instance=None, box_number=None):
        super().__init__(master, fg_color="#080808", corner_radius=6)
        debug_log(f"[{title}] STREAM_START original={redact_url(original_url or stream_url)}")
        self.box_number = box_number
        self.stream_title = title
        self.original_url = original_url or stream_url
        self.headers = headers or {}
        self._stopped = False
        self._dragging = False
        self._is_reconnecting = False
        self._hide_toolbar_id = None

        self.video_frame = tk.Frame(self, bg="black")
        self.video_frame.pack(fill="both", expand=True, padx=2, pady=(2, 0))

        controls = ctk.CTkFrame(self, fg_color="#171717", height=38)
        controls.pack(fill="x", padx=2, pady=2)

        ctk.CTkLabel(controls, text=title, anchor="w").pack(side="left", fill="x", expand=True, padx=8)
        self.play_pause_btn = ctk.CTkButton(controls, text="||", width=34, height=26, command=self.toggle_play)
        self.play_pause_btn.pack(side="left", padx=(2, 4), pady=4)
        ctk.CTkButton(controls, text="[]", width=34, height=26, command=self.stop).pack(side="left", padx=2, pady=4)

        self.time_slider = ctk.CTkSlider(controls, from_=0, to=1000, command=lambda _value: None)
        self.time_slider.pack(side="left", fill="x", expand=True, padx=4, pady=4)

        self.hover_toolbar = ctk.CTkFrame(
            self, fg_color="#252a31", corner_radius=4, border_width=1, border_color="#4a525e"
        )
        ctk.CTkButton(self.hover_toolbar, text="Link", width=58, height=26, command=self.copy_source_link).pack(padx=5, pady=(5, 2))
        ctk.CTkButton(self.hover_toolbar, text="Quick", width=58, height=26, command=self.create_quick_webp).pack(padx=5, pady=2)
        ctk.CTkButton(self.hover_toolbar, text="WebP", width=58, height=26, command=self.open_webp_options).pack(padx=5, pady=(2, 5))
        self._bind_hover_events(self)

        vlc_args = [
            "--no-xlib",
            "--avcodec-hw=none",
            "--vout=direct3d11",
            "--network-caching=500",
            "--live-caching=500",
        ]
        user_agent = self.headers.get("User-Agent") or self.headers.get("user-agent")
        if user_agent:
            vlc_args.append(f"--http-user-agent={user_agent}")
        self.instance = instance or vlc.Instance(vlc_args)
        if not self.instance:
            raise RuntimeError("Failed to initialize libVLC instance.")

        self.player = self.instance.media_player_new()
        self._set_media(stream_url, self.headers)
        self.gif_buffer = RollingGifBuffer(self.original_url)
        self.gif_buffer.start()

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

    def _bind_hover_events(self, widget):
        widget.bind("<Enter>", self._show_hover_toolbar, add="+")
        widget.bind("<Leave>", self._schedule_hide_hover_toolbar, add="+")
        for child in widget.winfo_children():
            if child is not self.hover_toolbar:
                self._bind_hover_events(child)
        self.hover_toolbar.bind("<Enter>", self._show_hover_toolbar, add="+")
        self.hover_toolbar.bind("<Leave>", self._schedule_hide_hover_toolbar, add="+")

    def _show_hover_toolbar(self, _event=None):
        if self._hide_toolbar_id is not None:
            self.after_cancel(self._hide_toolbar_id)
            self._hide_toolbar_id = None
        self.hover_toolbar.place(relx=0.5, y=10, anchor="n")

    def _schedule_hide_hover_toolbar(self, _event=None):
        if self._hide_toolbar_id is None:
            self._hide_toolbar_id = self.after(100, self._hide_hover_toolbar)

    def _hide_hover_toolbar(self):
        self._hide_toolbar_id = None
        pointer_x, pointer_y = self.winfo_pointerxy()
        inside_pane = (
            self.winfo_rootx() <= pointer_x < self.winfo_rootx() + self.winfo_width()
            and self.winfo_rooty() <= pointer_y < self.winfo_rooty() + self.winfo_height()
        )
        if not inside_pane:
            self.hover_toolbar.place_forget()

    def copy_source_link(self):
        credit_text = f"[ Credit: [{self.stream_title}](<{self.original_url}>) ]"
        window = self.winfo_toplevel()
        window.clipboard_clear()
        window.clipboard_append(credit_text)
        window.update()

    def create_quick_webp(self):
        self._create_webp(30, False)

    def open_webp_options(self):
        WebpOptionsDialog(self, self._create_webp)

    def _create_webp(self, duration, speed_up):
        threading.Thread(
            target=self._export_webp,
            args=(duration, speed_up),
            daemon=True,
        ).start()

    def _export_webp(self, duration, speed_up):
        try:
            filename = export_webp(self.gif_buffer, duration, speed_up)
            debug_log(f"[{self.stream_title}] WEBP_SAVED file={filename}")
        except Exception as exc:
            debug_log(f"[{self.stream_title}] WEBP_EXPORT_FAILED error={exc!r}")

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
        
        # Stream buffering and transport options
        media.add_option(":network-caching=500")
        media.add_option(":live-caching=500")
        if stream_url.lower().startswith("rtsp://"):
            media.add_option(":rtsp-tcp")
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
            self.play_pause_btn.configure(text=">")
        else:
            self.player.play()
            self.play_pause_btn.configure(text="||")

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
            is_rtsp = self.current_stream_url.lower().startswith("rtsp://")

            if state in (vlc.State.Ended, vlc.State.Error):
                self._start_reconnect(f"VLC_{state_name.upper()}")
            elif state == vlc.State.Buffering and not is_rtsp:
                if self._buffering_since is None:
                    self._buffering_since = now
                elif now - self._buffering_since > 5:
                    debug_log(f"[{self.stream_title}] BUFFER_STALL duration={now - self._buffering_since:.1f}s")
                    self._start_reconnect("BUFFER_TIMEOUT")
            else:
                self._buffering_since = None
                if not is_rtsp:
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
        is_rtsp = self.current_stream_url.lower().startswith("rtsp://")
        position_changed = position != self._recovery_position
        if self.player.get_state() == vlc.State.Playing and (is_rtsp or position_changed):
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
        self.gif_buffer.stop()
        self.player.stop()
        media = self.player.get_media()
        if media:
            media.release()
        self.destroy()


class WebpOptionsDialog(ctk.CTkToplevel):
    """Native animated WebP settings dialog for an individual stream."""

    def __init__(self, master, on_create):
        super().__init__(master)
        self.title("Create WebP")
        self.geometry("300x180")
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.grab_set()
        self.on_create = on_create
        self.speed_up = tk.BooleanVar(value=False)

        ctk.CTkLabel(self, text="Create WebP", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", padx=20, pady=(18, 10))
        ctk.CTkLabel(self, text="Duration (seconds)").pack(anchor="w", padx=20)
        self.duration_entry = ctk.CTkEntry(self)
        self.duration_entry.insert(0, "30")
        self.duration_entry.pack(fill="x", padx=20, pady=(4, 8))
        ctk.CTkCheckBox(self, text="Speed up", variable=self.speed_up).pack(anchor="w", padx=20)
        ctk.CTkButton(self, text="Create", command=self.create).pack(side="right", padx=20, pady=14)

    def create(self):
        try:
            duration = max(1, min(60, int(self.duration_entry.get())))
        except ValueError:
            messagebox.showerror("Invalid duration", "Enter a whole number from 1 to 60.", parent=self)
            return
        self.grab_release()
        self.destroy()
        self.on_create(duration, self.speed_up.get())


class PlayerWindow(ctk.CTkToplevel):
    """A resizable window containing any number of stream/placeholder panes."""

    def __init__(self, master, title=f"{APP_TITLE} Window", instance=None):
        super().__init__(master)
        self.title(title)
        self.iconbitmap(ICON_FILE)
        self.geometry("380x280")
        self.minsize(380, 280)
        self.vlc_instance = instance or master.vlc_instance
        self.panes = []
        self._open_generation = 0
        self._layout_size_count = 1
        self.grid_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.grid_frame.pack(fill="both", expand=True, padx=0, pady=0)
        self.bind("<FocusIn>", lambda _event: master.set_active_window(self))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def add_stream(self, stream_url, title, headers=None, original_url="", slot_index=None):
        box_number = (slot_index + 1) if slot_index is not None else None
        pane = StreamPane(
            self.grid_frame, stream_url, title, headers, original_url, self.vlc_instance, box_number=box_number
        )
        self.panes.append(pane)
        self._configure_layout()

    def add_placeholder(self, box_number):
        pane = PlaceholderPane(self.grid_frame, self.title(), box_number)
        self.panes.append(pane)
        self._configure_layout()

    def add_youtube_feeds(self, feed_slots, slot_count, generation=None):
        if generation is not None and getattr(self, "_open_generation", None) != generation:
            return
        yt_pane = YouTubeBrowser(self.grid_frame, feed_slots, slot_count, self.title())
        self.panes.append(yt_pane)
        self._configure_layout()

    def set_layout_size_count(self, feed_count):
        self._layout_size_count = max(1, feed_count)

    def _configure_layout(self):
        self.grid_frame.update_idletasks()
        pane_count = len(self.panes)
        if pane_count == 0:
            return

        # Use the TRUE total slot count (not len(self.panes)) for the grid
        # math -- the combined YouTube pane is a single widget in self.panes
        # but visually represents several slots at once, so basing the grid
        # on len(self.panes) undercounts how many cells the window actually
        # needs and throws off every other pane's proportions.
        total_slots = max(self._layout_size_count, pane_count)
        columns, rows = self._grid_dimensions(total_slots)
        cell_width = 1.0 / columns
        cell_height = 1.0 / rows

        for item in self.panes:
            item.place_forget()

        # The combined YouTube pane (if present) already lays its own feeds
        # out internally using this same slot_count/grid math, so it should
        # span the WHOLE grid area rather than being squeezed into "1 of N
        # panes". Every other real pane (a VLC stream or an empty-slot
        # placeholder) is then placed on top of it at its own true slot
        # position, covering whatever the combined pane renders underneath
        # at that particular cell.
        youtube_pane = next((item for item in self.panes if isinstance(item, YouTubeBrowser)), None)
        other_panes = [item for item in self.panes if item is not youtube_pane]

        if youtube_pane is not None:
            youtube_pane.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)

        for item in other_panes:
            box_number = getattr(item, "box_number", None)
            if box_number is None:
                continue
            index = box_number - 1
            row = index // columns
            col = index % columns
            item.place(
                relx=col * cell_width,
                rely=row * cell_height,
                relwidth=cell_width,
                relheight=cell_height,
            )
            item.lift()

        self.resize_for_count(total_slots)

    def _grid_dimensions(self, pane_count):
        if pane_count <= 1:
            return 1, 1
        if pane_count <= 3:
            return pane_count, 1
        if pane_count == 4:
            return 2, 2
        columns = 3
        return columns, math.ceil(pane_count / columns)

    def resize_for_count(self, pane_count):
        columns, rows = self._grid_dimensions(pane_count)
        window_width = columns * 360
        window_height = rows * 260
        self.geometry(f"{window_width}x{window_height}")

    def _on_close(self):
        self._open_generation += 1
        for pane in self.panes:
            if hasattr(pane, "stop"):
                pane.stop()
        self.destroy()

class FeedSidebarTile(ctk.CTkFrame):
    """Compact saved-feed card used by the Hub sidebar."""

    def __init__(self, master, feed, app):
        super().__init__(master, fg_color="#24272d", corner_radius=5, height=96)
        self.feed = feed
        self.app = app
        self.pack_propagate(False)

        self.thumbnail = ctk.CTkLabel(
            self, text="", width=72, height=78, fg_color="#15171a", corner_radius=3
        )
        self.thumbnail.pack(side="left", padx=(5, 7), pady=5)

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, pady=7)
        ctk.CTkLabel(
            info,
            text=feed.get("name", "Unnamed feed"),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(fill="x", padx=(0, 3), pady=(3, 0))
        ctk.CTkLabel(
            info,
            text="Drag to an output",
            anchor="w",
            text_color="#7f8792",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=(0, 3), pady=(2, 0))

        self._menu_button = ctk.CTkButton(
            self, text="...", width=24, height=26, fg_color="transparent",
            hover_color="#343942", command=self.show_menu
        )
        self._menu_button.place(relx=1.0, x=-4, y=4, anchor="ne")

        self._bind_drag_events(self)

    def _bind_drag_events(self, widget):
        widget.bind("<ButtonPress-1>", self._press, add="+")
        widget.bind("<B1-Motion>", self._motion, add="+")
        widget.bind("<ButtonRelease-1>", self._release, add="+")
        widget.bind("<Button-3>", self.show_menu, add="+")
        for child in widget.winfo_children():
            if child is not getattr(self, "_menu_button", None):
                self._bind_drag_events(child)

    def _press(self, event):
        self.app.begin_feed_drag(self.feed, event.x_root, event.y_root)

    def _motion(self, event):
        self.app.update_feed_drag(event.x_root, event.y_root)

    def _release(self, event):
        self.app.end_feed_drag(event.x_root, event.y_root)

    def show_menu(self, event=None):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Add to output...", command=lambda: self.app.quick_add_feed(self.feed))
        menu.add_command(label="Edit feed", command=lambda: self.app.edit_feed(self.feed))
        menu.add_command(label="Delete feed", command=lambda: self.app.delete_feed(self))
        try:
            x_root = event.x_root if event is not None else self.winfo_pointerx()
            y_root = event.y_root if event is not None else self.winfo_pointery()
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()
        return "break"


class LoadingPopup(tk.Toplevel):
    """Dark modal popup showing progress while a playback is loading."""

    def __init__(self, master, total_streams: int, target_window=None):
        super().__init__(master)

        self.target_window = target_window
        self.configure(bg="#202329")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.transient(master)

        self.total = max(1, total_streams)
        self.completed = 0
        self._poll_id = None
        self._closing = False
        self._done_queue = queue.Queue()

        self.protocol("WM_DELETE_WINDOW", lambda: None)

        outer = ctk.CTkFrame(self, fg_color="#202329", corner_radius=12, border_width=1, border_color="#343a42")
        outer.pack(fill="both", expand=True, padx=0, pady=0)

        header = ctk.CTkFrame(outer, fg_color="#1b1d22", corner_radius=12)
        header.pack(fill="x", padx=0, pady=0)

        ctk.CTkLabel(
            header,
            text="Loading Playback...",
            text_color="#e7ebf1",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w"
        ).pack(side="left", padx=(12, 0), pady=(8, 7), fill="x", expand=True)

        close_button = ctk.CTkButton(
            header,
            text="X",
            width=24,
            height=22,
            corner_radius=10,
            fg_color="#cc4d4d",
            hover_color="#e56060",
            text_color="#ffffff",
            border_width=0,
            command=self.close,
        )
        close_button.pack(side="right", padx=(0, 10), pady=(8, 7))

        content = ctk.CTkFrame(outer, fg_color="#202329")
        content.pack(fill="both", expand=True, padx=0, pady=0)

        ctk.CTkLabel(
            content,
            text="This should take just a moment!",
            text_color="#ffffff",
            font=ctk.CTkFont(size=15, weight="bold")
        ).pack(anchor="w", padx=20, pady=(16, 5))

        self.status_label = ctk.CTkLabel(
            content,
            text="Preparing playback...",
            text_color="#7f8792"
        )
        self.status_label.pack(anchor="w", padx=20, pady=(0, 8))

        self.progress_bar = ctk.CTkProgressBar(
            content,
            width=300,
            height=14,
            fg_color="#30343b",
            progress_color="#2f8cff"
        )
        self.progress_bar.set(0)
        self.progress_bar.pack(padx=20, pady=(0, 15))

        self.update_idletasks()

        parent_x = master.winfo_rootx()
        parent_y = master.winfo_rooty()
        parent_w = master.winfo_width()
        parent_h = master.winfo_height()

        popup_w = 340
        popup_h = 130

        x = parent_x + max(0, (parent_w - popup_w) // 2)
        y = parent_y + max(0, (parent_h - popup_h) // 2)
        self.geometry(f"{popup_w}x{popup_h}+{x}+{y}")
        self.deiconify()
        self.update_idletasks()
        self.update()
        self.lift()

        try:
            self.focus_force()
        except tk.TclError:
            pass

        try:
            self.grab_set()
        except tk.TclError:
            pass

        self._poll_id = self.after(50, self._poll_updates)

    def mark_done(self):
        if self._closing or not self.winfo_exists():
            return
        try:
            self._done_queue.put_nowait(1)
        except Exception:
            pass

    def _poll_updates(self):
        if self._closing or not self.winfo_exists():
            return

        while True:
            try:
                self._done_queue.get_nowait()
            except queue.Empty:
                break
            self.completed = min(self.completed + 1, self.total)

        self.status_label.configure(text="Preparing playback...")
        self.progress_bar.set(self.completed / self.total)

        if self.completed >= self.total:
            self._closing = True
            if self.target_window is not None and self.target_window.winfo_exists():
                self.after(0, lambda: self.master.show_playback_window(self.target_window))
            self.after(200, self.close)
            return

        self._poll_id = self.after(50, self._poll_updates)

    def close(self):
        if self._closing is False:
            self._closing = True

        try:
            self.grab_release()
        except tk.TclError:
            pass

        try:
            if self._poll_id is not None:
                self.after_cancel(self._poll_id)
                self._poll_id = None
        except (tk.TclError, AttributeError):
            pass

        try:
            if self.winfo_exists():
                self.withdraw()
        except tk.TclError:
            pass

        try:
            self.after(120, self._final_destroy)
        except tk.TclError:
            pass

    def _final_destroy(self):
        try:
            if self.winfo_exists():
                self.destroy()
        except tk.TclError:
            pass


class PlaceholderPane(ctk.CTkFrame):
    """Placeholder pane for grid slots without an assigned feed."""

    def __init__(self, master, window_title: str, box_number: int):
        super().__init__(master, fg_color="#080808", corner_radius=6)
        self.box_number = box_number

        container = ctk.CTkFrame(self, fg_color="#121212", corner_radius=4)
        container.pack(fill="both", expand=True, padx=2, pady=2)

        ctk.CTkLabel(
            container,
            text=f"{window_title}: {box_number}",
            text_color="#555555",
            font=ctk.CTkFont(size=20, weight="bold")
        ).pack(expand=True)

    def stop(self):
        self.destroy()

class OutputTile(ctk.CTkFrame):
    """Output card showing a thumbnail grid of the feeds assigned to it."""

    def __init__(self, master, playback, app):
        super().__init__(master, fg_color="#25282e", corner_radius=7)
        self.playback = playback
        self.app = app
        self.playback["feeds"] = list(self.playback.get("feeds", []))
        if not self.playback["feeds"]:
            self.playback["feeds"] = [None]
        self.preview = ctk.CTkFrame(self, fg_color="#17191d", corner_radius=2)
        self.preview.pack(fill="x", padx=10, pady=(10, 0))
        self._highlighted = False
        self._slot_cells = {}

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(8, 3))
        ctk.CTkButton(
            header, text="Open", width=42, height=25, fg_color="transparent",
            hover_color="#343942", command=self.open_output
        ).pack(side="left")
        ctk.CTkLabel(
            header, text=playback.get("name", "Output"), anchor="w",
            font=ctk.CTkFont(size=16, weight="bold")
        ).pack(side="left", padx=3)
        ctk.CTkButton(
            header, text="...", width=27, height=25, fg_color="transparent",
            hover_color="#343942", command=self.show_menu
        ).pack(side="right")

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=12, pady=(2, 10))
        ctk.CTkLabel(footer, text="Feeds:", anchor="w").pack(side="left")
        ctk.CTkButton(
            footer, text="-", width=28, height=25,
            fg_color="#30343b", hover_color="#3b4049",
            command=self.remove_last_feed
        ).pack(side="left", padx=(5, 3))
        self.count_var = tk.StringVar(value=str(len(self.playback["feeds"])))
        self.count_entry = ctk.CTkEntry(
            footer,
            width=36,
            height=25,
            textvariable=self.count_var,
            justify="center",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.count_entry.pack(side="left")
        self.count_entry.bind("<Return>", self._apply_feed_count)
        self.count_entry.bind("<FocusOut>", self._apply_feed_count)
        ctk.CTkButton(
            footer, text="+", width=28, height=25,
            fg_color="#30343b", hover_color="#3b4049",
            command=self.add_next_feed
        ).pack(side="left", padx=(3, 10))
        ctk.CTkLabel(
            footer, text="Drag feeds here", text_color="#6f7782",
            anchor="w"
        ).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            footer, text="Edit", width=55, height=25, fg_color="transparent",
            hover_color="#343942", command=lambda: self.app.edit_playback(self.playback)
        ).pack(side="right")
        self.app.output_tiles.append(self)
        self.render()

    def contains_root_point(self, x, y):
        if not self.winfo_exists():
            return False
        left = self.winfo_rootx()
        top = self.winfo_rooty()
        return left <= x <= left + self.winfo_width() and top <= y <= top + self.winfo_height()

    def slot_at_point(self, x, y):
        for index, cell in self._slot_cells.items():
            if not cell.winfo_exists():
                continue
            left = cell.winfo_rootx()
            top = cell.winfo_rooty()
            if left <= x <= left + cell.winfo_width() and top <= y <= top + cell.winfo_height():
                return index
        return None

    def set_slot_highlight(self, index):
        for slot_index, cell in self._slot_cells.items():
            cell.configure(
                border_width=2 if slot_index == index else 0,
                border_color="#2f8cff"
            )

    def set_drop_highlight(self, enabled):
        if enabled == self._highlighted:
            return
        self._highlighted = enabled
        self.configure(
            border_width=2 if enabled else 0,
            border_color="#2f8cff" if enabled else self.cget("fg_color")
        )

    def render(self):
        for child in self.preview.winfo_children():
            child.destroy()

        feed_names = self.playback.setdefault("feeds", [None])
        self.count_var.set(str(len(feed_names)))
        self._slot_cells = {}

        playback_name = self.playback.get("name", "Output")
        columns = 3

        for index, feed_name in enumerate(feed_names):
            cell = ctk.CTkFrame(self.preview, fg_color="#0f1012", corner_radius=1)
            cell.grid(row=index // columns, column=index % columns, sticky="nsew", padx=1, pady=1)
            self._slot_cells[index] = cell

            feed = next((f for f in self.app.feeds if f.get("name") == feed_name), None) if feed_name else None

            if not feed:
                placeholder_box = ctk.CTkFrame(cell, fg_color="#191b1f", corner_radius=2)
                placeholder_box.pack(fill="both", expand=True)
                ctk.CTkLabel(
                    placeholder_box,
                    text=f"{playback_name}: {index + 1}",
                    text_color="#666d78",
                    font=ctk.CTkFont(size=11, weight="bold")
                ).pack(fill="both", expand=True, padx=4, pady=12)
                continue

            label = ctk.CTkLabel(cell, text="", height=82, fg_color="#191b1f")
            label.pack(fill="both", expand=True)
            ctk.CTkLabel(
                cell, text=feed.get("name", "Feed"), anchor="w",
                font=ctk.CTkFont(size=9, weight="bold")
            ).pack(fill="x", padx=4, pady=(2, 3))
            self.app.load_thumbnail(label, feed, (150, 82))
            self._bind_slot_drag(cell, feed, index)

        rows = max(1, (len(feed_names) + columns - 1) // columns)
        for column in range(columns):
            self.preview.grid_columnconfigure(column, weight=1, uniform="preview")
        for row in range(rows):
            self.preview.grid_rowconfigure(row, weight=1, uniform="preview")

    def _bind_slot_drag(self, widget, feed, index):
        widget.bind(
            "<ButtonPress-1>",
            lambda event: self.app.begin_feed_drag(
                feed, event.x_root, event.y_root,
                source=(self.playback, index),
            ),
            add="+",
        )
        widget.bind(
            "<B1-Motion>",
            lambda event: self.app.update_feed_drag(event.x_root, event.y_root),
            add="+",
        )
        widget.bind(
            "<ButtonRelease-1>",
            lambda event: self.app.end_feed_drag(event.x_root, event.y_root),
            add="+",
        )
        widget.bind(
            "<Button-3>",
            lambda event: self._show_feed_menu(feed, event),
            add="+",
        )
        for child in widget.winfo_children():
            self._bind_slot_drag(child, feed, index)

    def _show_feed_menu(self, feed, event):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Edit feed", command=lambda: self.app.edit_feed(feed))
        menu.add_command(label="Delete feed", command=lambda: self.app.delete_feed(feed))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def add_next_feed(self):
        assigned = self.playback.setdefault("feeds", [None])
        assigned.append(None)
        self.app.save_data()
        self.app.render_playbacks()
        self.app.refresh_open_playback(self.playback)

    def _apply_feed_count(self, _event=None):
        try:
            count = max(1, int(self.count_var.get()))
        except ValueError:
            self.count_var.set(str(len(self.playback.get("feeds", [None]))))
            return

        assigned = self.playback.setdefault("feeds", [None])
        if count > len(assigned):
            assigned.extend([None] * (count - len(assigned)))
        elif count < len(assigned):
            del assigned[count:]
        else:
            return

        self.app.save_data()
        self.app.render_feeds()
        self.app.render_playbacks()
        self.app.refresh_open_playback(self.playback)

    def remove_last_feed(self):
        assigned = self.playback.setdefault("feeds", [None])
        if len(assigned) > 1:
            assigned.pop()
            self.app.save_data()
            self.app.render_playbacks()
            self.app.refresh_open_playback(self.playback)

    def open_output(self):
        self.app.open_playback(self.playback)

    def show_menu(self):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Open output", command=self.open_output)
        menu.add_command(label="Rename", command=lambda: self.app.rename_playback(self.playback))
        menu.add_command(label="Export feeds...", command=lambda: self.app.export_playback_feeds(self.playback))
        menu.add_command(label="Clear feeds", command=lambda: self.app.clear_output(self.playback))
        menu.add_separator()
        menu.add_command(label="Delete output", command=lambda: self.app.delete_playback(self.playback))
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

class MainWindow(ctk.CTk):
    """Downlink Hub redesigned around a feed sidebar and output dashboard."""

    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} Hub")
        self.youtube_signed_in = youtube_signed_in()
        self.iconbitmap(ICON_FILE)
        self.geometry("1180x760")
        self.minsize(900, 600)
        self.vlc_instance = vlc.Instance([
            "--no-xlib",
            "--avcodec-hw=none",
            "--vout=direct3d11",
            "--network-caching=500",
            "--live-caching=500",
        ])
        if not self.vlc_instance:
            raise RuntimeError("Failed to initialize libVLC instance.")

        self.feeds, self.playbacks = self._load_data()
        self.windows = {}
        self.active_window = None
        self._thumbnail_refs = []
        self._thumbnail_cache = {}

        self.output_tiles = []
        self._drag_feed = None
        self._drag_source = None
        self._drag_feed_start = None
        self._drag_active = False
        self._drag_ghost = None
        self._build_ui()
        self.render_feeds()
        self.render_playbacks()

    def toggle_youtube_account(self):
        if self.youtube_signed_in:
            self.sign_out_youtube()
        else:
            self.sign_in_youtube()

    def sign_in_youtube(self):
        self.youtube_account_button.configure(
            text="Signing in...",
            state="disabled",
        )
        self.status.configure(
            text="Sign in to YouTube in the window that opened."
        )

        self._youtube_account_process = multiprocessing.Process(
            target=run_youtube_account,
            args=("signin",),
            daemon=True,
        )
        self._youtube_account_process.start()
        self._poll_youtube_account("signin")

    def sign_out_youtube(self):
        self.youtube_signed_in = False
        self.youtube_account_button.configure(
            text="Signing out...",
            state="disabled",
        )
        self.status.configure(text="Signing out of YouTube...")

        self._youtube_account_process = multiprocessing.Process(
            target=run_youtube_account,
            args=("signout",),
            daemon=True,
        )
        self._youtube_account_process.start()
        self._poll_youtube_account("signout")

    def _poll_youtube_account(self, action):
        signed_in = youtube_signed_in()
        process = getattr(self, "_youtube_account_process", None)

        if action == "signin" and signed_in:
            self.youtube_signed_in = True
            self.youtube_account_button.configure(
                text="Sign out",
                state="normal",
            )
            self.status.configure(text="YouTube account signed in.")
            return

        if action == "signout" and not signed_in:
            self.youtube_signed_in = False
            self.youtube_account_button.configure(
                text="Sign in",
                state="normal",
            )
            self.status.configure(text="YouTube account signed out.")
            return

        if process is not None and process.is_alive():
            self.after(500, lambda: self._poll_youtube_account(action))
            return

        # The browser process closed without reaching the expected state.
        # Re-read the state one last time, then restore the appropriate button.
        self.youtube_signed_in = youtube_signed_in()
        self.youtube_account_button.configure(
            text="Sign out" if self.youtube_signed_in else "Sign in",
            state="normal",
        )

        if action == "signin":
            self.status.configure(
                text="YouTube sign-in was not detected."
                if not self.youtube_signed_in
                else "YouTube account signed in."
            )
        else:
            self.status.configure(
                text="YouTube account signed out."
                if not self.youtube_signed_in
                else "YouTube sign-out was not completed."
            )

    def _refresh_youtube_button(self):
        """Refresh the header button from the persistent session state."""
        self.youtube_signed_in = youtube_signed_in()
        self.youtube_account_button.configure(
            text="Sign out" if self.youtube_signed_in else "Sign in",
            state="normal",
        )

    def _build_ui(self):
        self.configure(fg_color="#202329")

        top = ctk.CTkFrame(self, fg_color="#202329", height=42, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)
        ctk.CTkLabel(
            top, text=APP_TITLE, anchor="w",
            font=ctk.CTkFont(size=16, weight="bold")
        ).pack(side="left", padx=16)
        self.add_playback_button = ctk.CTkButton(
            top, text="+ Playback", width=105, height=28,
            command=self.show_add_playback_menu
        )
        self.add_playback_button.pack(side="right", padx=8, pady=7)
        self.youtube_account_button = ctk.CTkButton(
            top,
            text="Sign out" if self.youtube_signed_in else "Sign in",
            width=75,
            height=28,
            fg_color="#2f8cff",
            hover_color="#1f6fc9",
            command=self.toggle_youtube_account,
        )
        self.youtube_account_button.pack(
            side="right",
            padx=(0, 4),
            pady=7,
        )
        ctk.CTkButton(
            top, text="+ Feed", width=80, height=28,
            command=self.add_feed
        ).pack(side="right", pady=7)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)

        sidebar = ctk.CTkFrame(body, width=275, fg_color="#191b20", corner_radius=0)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        ctk.CTkLabel(
            sidebar, text="Feeds", anchor="w",
            font=ctk.CTkFont(size=27, weight="bold")
        ).pack(fill="x", padx=20, pady=(18, 12))

        self.add_feed_card = ctk.CTkButton(
            sidebar, text="+", width=220, height=78,
            fg_color="#292d34", hover_color="#333840",
            border_width=1, border_color="#69717d",
            font=ctk.CTkFont(size=28), command=self.add_feed
        )
        self.add_feed_card.pack(fill="x", padx=20, pady=(0, 10))

        self.feed_area = ctk.CTkScrollableFrame(sidebar, fg_color="transparent")
        self.feed_area.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        separator = ctk.CTkFrame(body, width=2, fg_color="#586b90", corner_radius=0)
        separator.pack(side="left", fill="y")

        main = ctk.CTkFrame(body, fg_color="#202329", corner_radius=0)
        main.pack(side="left", fill="both", expand=True)

        ctk.CTkLabel(
            main, text="Outputs", anchor="w",
            font=ctk.CTkFont(size=31, weight="bold")
        ).pack(fill="x", padx=32, pady=(20, 12))

        self.output_area = ctk.CTkScrollableFrame(main, fg_color="transparent")
        self.output_area.pack(fill="both", expand=True, padx=22, pady=(0, 10))

        self.status = ctk.CTkLabel(main, text="", text_color="#7f8792")
        self.status.pack(fill="x", padx=30, pady=(0, 8))

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

        assigned_names = {
            name
            for playback in self.playbacks
            for name in playback.get("feeds", [])
            if name
        }
        groups = {}
        for feed in self.feeds:
            if feed.get("name") in assigned_names:
                continue
            group = feed.get("group") or "Feeds"
            groups.setdefault(group, []).append(feed)

        for group_name, feeds in groups.items():
            ctk.CTkLabel(
                self.feed_area, text=group_name, anchor="w",
                font=ctk.CTkFont(size=16, weight="bold")
            ).pack(fill="x", padx=5, pady=(8, 4))
            for feed in feeds:
                tile = FeedSidebarTile(self.feed_area, feed, self)
                tile.pack(fill="x", padx=2, pady=4)
                self.load_thumbnail(tile.thumbnail, feed, (72, 78))

    def render_playbacks(self):
        for child in self.output_area.winfo_children():
            child.destroy()

        self.output_tiles = []

        if not self.playbacks:
            empty = ctk.CTkLabel(
                self.output_area,
                text="No outputs yet\nCreate one with + Playback, then drag feeds here.",
                text_color="#777f8b", font=ctk.CTkFont(size=15)
            )
            empty.pack(expand=True, pady=120)
            return

        for playback in self.playbacks:
            OutputTile(self.output_area, playback, self).pack(fill="x", padx=8, pady=9)

    def load_thumbnail(self, widget, feed, size):
        thumbnail_url = feed.get("thumbnail")
        if not thumbnail_url and is_youtube_url(feed.get("url", "")):
            video_id = youtube_video_id(feed["url"])
            thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
        if not thumbnail_url:
            widget.configure(text="LIVE", text_color="#666d78")
            return

        cached_image = self._thumbnail_cache.get(thumbnail_url)
        if cached_image is not None:
            self.set_thumbnail(widget, cached_image)
            return

        def fetch():
            try:
                from PIL import Image
                image_data = None
                loaded_url = thumbnail_url
                for candidate_url in (
                    thumbnail_url,
                    thumbnail_url.replace("/maxresdefault.jpg", "/hqdefault.jpg"),
                ):
                    try:
                        image_data = urlopen(candidate_url, timeout=8).read()
                        loaded_url = candidate_url
                        break
                    except Exception:
                        continue
                if image_data is None:
                    raise OSError("thumbnail unavailable")
                image = Image.open(BytesIO(image_data)).convert("RGB")
                image.thumbnail(size)
                self._thumbnail_cache[thumbnail_url] = image
                if not feed.get("thumbnail") and is_youtube_url(feed.get("url", "")):
                    feed["thumbnail"] = loaded_url
                    self.after(0, self.save_data)
                self.after(0, lambda: self.set_thumbnail(widget, image))
            except Exception:
                self.after(0, lambda: widget.configure(text="LIVE", text_color="#666d78"))

        threading.Thread(target=fetch, daemon=True).start()

    def set_thumbnail(self, widget, image):
        if widget.winfo_exists():
            ref = ctk.CTkImage(light_image=image, dark_image=image, size=image.size)
            widget.image_ref = ref
            widget.configure(image=ref, text="")

    def add_feed(self):
        ThemedForm(
            self, "Add saved feed",
            [("name", "Name", ""), ("url", "Link", "https://..."), ("group", "Group", "Feeds")],
            self.finish_feed
        )

    def finish_feed(self, values):
        self.feeds.append({
            "name": values["name"],
            "url": values["url"],
            "group": values.get("group") or "Feeds",
            "thumbnail": None,
        })
        self.save_data()
        self.render_feeds()

    def edit_feed(self, feed):
        ThemedForm(
            self,
            "Edit saved feed",
            [
                ("name", "Name", feed.get("name", "")),
                ("url", "Link", feed.get("url", "")),
                ("group", "Group", feed.get("group", "Feeds")),
            ],
            lambda values: self.finish_edit_feed(feed, values),
        )

    def finish_edit_feed(self, feed, values):
        if feed not in self.feeds:
            return
        old_name = feed.get("name")
        new_name = values["name"]
        feed.update({
            "name": new_name,
            "url": values["url"],
            "group": values.get("group") or "Feeds",
        })
        if old_name != new_name:
            for playback in self.playbacks:
                playback["feeds"] = [new_name if name == old_name else name for name in playback.get("feeds", [])]
        self.save_data()
        self.render_feeds()
        self.render_playbacks()
        for playback in self.playbacks:
            self.refresh_open_playback(playback)

    def delete_feed(self, tile_or_feed):
        feed = getattr(tile_or_feed, "feed", tile_or_feed)
        if feed not in self.feeds:
            return
        self.feeds.remove(feed)
        # Remove deleted feed from all outputs too.
        name = feed.get("name")
        for playback in self.playbacks:
            playback["feeds"] = [None if n == name else n for n in playback.get("feeds", [])]
        self.save_data()
        self.render_feeds()
        self.render_playbacks()
        for playback in self.playbacks:
            self.refresh_open_playback(playback)

    def add_playback(self):
        ThemedForm(
            self, "New output",
            [("name", "Name", f"Output {len(self.playbacks) + 1}")],
            self.finish_playback
        )

    def show_add_playback_menu(self):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="New playback", command=self.add_playback)
        menu.add_command(label="Import playback...", command=self.import_playback)
        try:
            x = self.add_playback_button.winfo_rootx()
            y = self.add_playback_button.winfo_rooty() + self.add_playback_button.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def import_playback(self):
        input_path = filedialog.askopenfilename(
            parent=self,
            title="Import playback",
            filetypes=[("Downlink playback", "*.dwnlnk")],
        )
        if not input_path:
            return
        if not input_path.lower().endswith(".dwnlnk"):
            messagebox.showerror(
                "Import failed",
                "Only .dwnlnk playback files can be imported.",
                parent=self,
            )
            return
        try:
            with open(input_path, "r", encoding="utf-8") as file:
                import_data = json.load(file)
            if import_data.get("format") != "downlink-playback" or import_data.get("version") != 1:
                raise ValueError("This is not a supported Downlink playback export.")
            imported_playback = import_data["playback"]
            imported_feeds = import_data["feeds"]
            if not isinstance(imported_playback.get("name"), str) or not isinstance(imported_playback.get("feeds"), list):
                raise ValueError("The playback data is incomplete.")
            if not isinstance(imported_feeds, list) or not all(
                isinstance(feed, dict) and isinstance(feed.get("name"), str) and isinstance(feed.get("url"), str)
                for feed in imported_feeds
            ):
                raise ValueError("The feed data is incomplete.")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            messagebox.showerror("Import failed", str(exc), parent=self)
            return

        existing_feed_names = {feed.get("name") for feed in self.feeds}
        new_feeds = [feed for feed in imported_feeds if feed["name"] not in existing_feed_names]
        self.feeds.extend(new_feeds)
        base_name = imported_playback["name"] or "Imported playback"
        playback_name = base_name
        existing_playback_names = {playback.get("name") for playback in self.playbacks}
        suffix = 2
        while playback_name in existing_playback_names:
            playback_name = f"{base_name} ({suffix})"
            suffix += 1
        self.playbacks.append({"name": playback_name, "feeds": list(imported_playback["feeds"])})
        self.save_data()
        self.render_feeds()
        self.render_playbacks()
        self.status.configure(text=f"Imported {playback_name} with {len(new_feeds)} new feed(s)")

    def finish_playback(self, values):
        self.playbacks.append({"name": values["name"], "feeds": [None]})
        self.save_data()
        self.render_playbacks()

    def export_playback_feeds(self, playback):
        feed_names = playback.get("feeds", [])
        assigned_feeds = [
            feed for feed in self.feeds
            if feed.get("name") in feed_names
        ]
        default_name = playback.get("name", "playback").replace("/", "-").replace("\\", "-")
        output_path = filedialog.asksaveasfilename(
            parent=self,
            title="Export playback feeds",
            initialfile=f"{default_name}",
            defaultextension=".dwnlnk",
            filetypes=[("Downlink playback", "*.dwnlnk")],
        )
        if not output_path:
            return
        export_data = {
            "format": "downlink-playback",
            "version": 1,
            "playback": {
                "name": playback.get("name", "Output"),
                "feeds": list(feed_names),
            },
            "feeds": assigned_feeds,
        }
        try:
            with open(output_path, "w", encoding="utf-8") as file:
                json.dump(export_data, file, indent=2)
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        self.status.configure(text=f"Exported {len(assigned_feeds)} feed(s) to {output_path}")

    def rename_playback(self, playback):
        ThemedForm(
            self, "Rename output", [("name", "Name", playback["name"])],
            lambda values: self.finish_rename(playback, values)
        )

    def edit_playback(self, playback):
        ThemedForm(
            self,
            "Edit output",
            [
                ("name", "Name", playback.get("name", "Output")),
                ("feed_count", "Feeds", str(len(playback.get("feeds", [None])))),
            ],
            lambda values: self.finish_edit_playback(playback, values),
        )

    def finish_edit_playback(self, playback, values):
        try:
            feed_count = max(1, int(values["feed_count"]))
        except ValueError:
            messagebox.showerror("Invalid feed count", "Feeds must be a whole number.", parent=self)
            return

        old_name = playback.get("name")
        new_name = values["name"]
        assigned = playback.setdefault("feeds", [None])
        if feed_count > len(assigned):
            assigned.extend([None] * (feed_count - len(assigned)))
        else:
            del assigned[feed_count:]

        playback["name"] = new_name
        window = self.windows.pop(old_name, None)
        if window is not None and window.winfo_exists():
            window.title(new_name)
            if hasattr(window, "_titlebar"):
                window._titlebar.title_label.configure(text=new_name)
            self.windows[new_name] = window

        self.save_data()
        self.render_feeds()
        self.render_playbacks()
        self.refresh_open_playback(playback)

    def finish_rename(self, playback, values):
        playback["name"] = values["name"]
        self.save_data()
        self.render_playbacks()

    def clear_output(self, playback):
        playback["feeds"] = [None] * max(1, len(playback.get("feeds", [])))
        self.save_data()
        self.render_playbacks()
        self.refresh_open_playback(playback)

    def delete_playback(self, playback):
        if playback not in self.playbacks:
            return
        self.playbacks.remove(playback)
        window = self.windows.pop(id(playback), None)
        if window and window.winfo_exists():
            window._on_close()
        self.save_data()
        self.render_playbacks()

    def begin_feed_drag(self, feed, x, y, source=None):
        self._drag_feed = feed
        self._drag_source = source
        self._drag_feed_start = (x, y)
        self._drag_active = False

    def update_feed_drag(self, x, y):
        if self._drag_feed is None or self._drag_feed_start is None:
            return

        sx, sy = self._drag_feed_start
        if not self._drag_active:
            if abs(x - sx) + abs(y - sy) < 10:
                return
            self._drag_active = True
            self._create_drag_ghost(self._drag_feed.get("name", "Feed"))

        if self._drag_ghost is not None:
            self._drag_ghost.geometry(f"+{x + 14}+{y + 14}")
        self._update_drop_target(x, y)

    def end_feed_drag(self, x, y):
        if self._drag_feed is None:
            return
        if self._drag_active:
            self._drop_feed_at_point(self._drag_feed, x, y)
        self._clear_drop_highlights()
        self._destroy_drag_ghost()
        self._drag_feed = None
        self._drag_source = None
        self._drag_feed_start = None
        self._drag_active = False

    def _create_drag_ghost(self, name):
        self._destroy_drag_ghost()
        ghost = ctk.CTkToplevel(self)
        ghost.overrideredirect(True)
        ghost.attributes("-topmost", True)
        ghost.attributes("-alpha", 0.88)
        ghost.configure(fg_color="#2f8cff")
        ctk.CTkLabel(
            ghost, text=f"  {name}  ", text_color="white", fg_color="#2f8cff",
            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(padx=1, pady=1)
        ghost.update_idletasks()
        self._drag_ghost = ghost

    def _destroy_drag_ghost(self):
        if self._drag_ghost is not None:
            try:
                self._drag_ghost.destroy()
            except tk.TclError:
                pass
            self._drag_ghost = None

    def _update_drop_target(self, x, y):
        target = None
        for output in self.output_tiles:
            slot = output.slot_at_point(x, y)
            if slot is not None:
                target = (output, slot)
                break
        for output in self.output_tiles:
            output.set_drop_highlight(False)
            output.set_slot_highlight(target[1] if target and output is target[0] else None)

    def _clear_drop_highlights(self):
        for output in self.output_tiles:
            output.set_drop_highlight(False)
            output.set_slot_highlight(None)

    def _drop_feed_at_point(self, feed, x, y):
        target = next(
            ((output, output.slot_at_point(x, y)) for output in self.output_tiles
             if output.slot_at_point(x, y) is not None),
            None
        )
        if target is None:
            if self._drag_source is not None:
                self._clear_source_slot()
                self.save_data()
                self.status.configure(text=f"Returned {feed.get('name')} to the feed list.")
                self.render_playbacks()
                self.render_feeds()
                self.refresh_open_playback(self._drag_source[0])
            else:
                self.status.configure(text="Drop the feed onto a preset slot.")
            return
        self.drop_feed_at(target[0].playback, target[1])

    def _clear_source_slot(self):
        if self._drag_source is None:
            return
        source_playback, source_index = self._drag_source
        assigned = source_playback.setdefault("feeds", [None])
        if 0 <= source_index < len(assigned):
            assigned[source_index] = None

    def drop_feed_at(self, playback, slot_index=None):
        if self._drag_feed is None or slot_index is None:
            return
        feed = self._drag_feed
        name = feed.get("name")
        assigned = playback.setdefault("feeds", [None])
        if slot_index >= len(assigned):
            return
        if self._drag_source is not None:
            source_playback, source_index = self._drag_source
            if source_playback is playback and source_index == slot_index:
                return
            self._clear_source_slot()
        assigned[slot_index] = name
        self.save_data()
        self.status.configure(text=f"Placed {name} in slot {slot_index + 1} of {playback['name']}.")
        self.render_playbacks()
        self.render_feeds()
        self.refresh_open_playback(playback)
        if self._drag_source is not None and self._drag_source[0] is not playback:
            self.refresh_open_playback(self._drag_source[0])

    def quick_add_feed(self, feed):
        for playback in self.playbacks:
            assigned = playback.setdefault("feeds", [None])
            try:
                slot_index = assigned.index(None)
            except ValueError:
                continue
            assigned[slot_index] = feed.get("name")
            self.save_data()
            self.status.configure(text=f"Placed {feed.get('name')} in slot {slot_index + 1} of {playback['name']}.")
            self.render_playbacks()
            self.render_feeds()
            self.refresh_open_playback(playback)
            return
        self.status.configure(text="Add a preset slot before assigning this feed.")

    def open_playback(self, playback):
        # Use a stable key (e.g., playback name or unique ID) instead of id(playback)
        playback_id = playback.get("name", id(playback))
        
        window = self.windows.get(playback_id)
        if window is None or not window.winfo_exists():
            window = PlayerWindow(self, title=playback["name"], instance=self.vlc_instance)
            self.windows[playback_id] = window
        else:
            # Increment generation counter to invalidate any pending async resolves
            window._open_generation += 1
            
            # Destroy active pane widgets explicitly, not just clear the list
            for pane in list(window.panes):
                if hasattr(pane, "stop"):
                    pane.stop()
                if hasattr(pane, "destroy"):
                    pane.destroy()
            window.panes.clear()

        generation = window._open_generation
        slots = playback.get("feeds", [])
        slot_feeds = [
            next((item for item in self.feeds if item.get("name") == feed_name), None)
            if feed_name else None
            for feed_name in slots
        ]
        youtube_feeds = [
            (index, feed) for index, feed in enumerate(slot_feeds)
            if feed and is_youtube_url(feed.get("url", ""))
        ]

        window.set_layout_size_count(len(slots))
        window.resize_for_count(len(slots))

        assigned_feeds = [
            next((item for item in self.feeds if item.get("name") == feed_name), None)
            for feed_name in slots
        ]
        assigned_feeds = [feed for feed in assigned_feeds if feed and feed.get("url")]

        loading_popup = LoadingPopup(self, len(assigned_feeds), window) if assigned_feeds else None

        if loading_popup is not None:
            window.withdraw()
            loading_popup.update_idletasks()
            loading_popup.update()
            loading_popup.focus_force()
        else:
            window.deiconify()
            window.focus_force()

        for index, feed_name in enumerate(slots):
            box_number = index + 1
            if not feed_name:
                window.add_placeholder(box_number)
                continue

            feed = slot_feeds[index]
            if not feed:
                window.add_placeholder(box_number)
                continue

            if is_youtube_url(feed.get("url", "")):
                if loading_popup is not None:
                    loading_popup.mark_done()
            else:
                threading.Thread(
                    target=self.resolve_and_add,
                    args=(feed, window, generation, loading_popup, index),
                    daemon=True,
                ).start()

        if youtube_feeds:
            window.add_youtube_feeds(youtube_feeds, len(slots), generation=generation)

    def show_playback_window(self, window):
        if window is None or not window.winfo_exists():
            return
        try:
            window.deiconify()
        except tk.TclError:
            pass
        try:
            window.focus_force()
        except tk.TclError:
            pass

    def refresh_open_playback(self, playback):
        # Use a stable key (e.g., playback name or unique ID) instead of id(playback)
        playback_id = playback.get("name", id(playback))
        window = self.windows.get(playback_id)
        if window is not None and window.winfo_exists():
            self.open_playback(playback)

    def set_active_window(self, window):
        if window.winfo_exists():
            self.active_window = window

    def resolve_and_add(self, feed, window, generation, loading_popup=None, slot_index=None):
        try:
            debug_log(f"[{feed['name']}] RESOLVE_FEED starting original={redact_url(feed['url'])}")
            stream_url, thumbnail, headers = resolve_stream(feed["url"])

            if not self._is_current_playback(window, generation):
                return

            if thumbnail and feed.get("thumbnail") != thumbnail:
                feed["thumbnail"] = thumbnail
                self.after(0, self.save_data)
                self.after(0, self.render_feeds)
                self.after(0, self.render_playbacks)

            self.after(0, lambda: self._add_resolved_stream(
                window, generation, stream_url, feed["name"], headers, feed["url"], slot_index
            ))
        except Exception as exc:
            debug_log(f"[{feed['name']}] RESOLVE_FEED failed error={exc!r}")

            if not self._is_current_playback(window, generation):
                return

            error_message = str(exc)
            self.after(0, lambda msg=error_message: messagebox.showerror(
                "Couldn't play that link", msg, parent=window
            ))
        finally:
            if loading_popup is not None:
                self.after(0, loading_popup.mark_done)

    def _is_current_playback(self, window, generation):
        return (
            window.winfo_exists()
            and window._open_generation == generation
            and any(current is window for current in self.windows.values())
        )

    def _add_resolved_stream(self, window, generation, stream_url, title, headers, original_url, slot_index=None):
        if self._is_current_playback(window, generation):
            window.add_stream(stream_url, title, headers=headers, original_url=original_url, slot_index=slot_index)


class ThemedForm(ctk.CTkToplevel):
    def __init__(self, master, title, fields, on_submit):
        super().__init__(master)
        self.title(title)
        self.geometry("460x270")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.on_submit = on_submit
        self.entries = {}
        ctk.CTkLabel(
            self, text=title,
            font=ctk.CTkFont(size=20, weight="bold")
        ).pack(anchor="w", padx=24, pady=(20, 12))
        for key, label, value in fields:
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=24, pady=5)
            ctk.CTkLabel(row, text=label, width=80, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(row)
            entry.insert(0, value)
            entry.pack(side="left", fill="x", expand=True)
            self.entries[key] = entry
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=16)
        ctk.CTkButton(
            actions, text="Cancel", fg_color="transparent", command=self.destroy
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(actions, text="Save", command=self.submit).pack(side="right")

    def submit(self):
        values = {key: entry.get().strip() for key, entry in self.entries.items()}
        if all(values.values()):
            self.grab_release()
            self.destroy()
            self.on_submit(values)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = MainWindow()
    app.mainloop()