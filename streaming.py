"""Stream URL classification and resolution."""

import re
from urllib.parse import urlparse

from app_support import debug_log

import yt_dlp

DIRECT_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
    ".mp3",
    ".wav",
    ".flac",
    ".m4a",
    ".ogg",
    ".m3u8",
}


def redact_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "<direct-url>"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def is_direct_stream_url(url: str) -> bool:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    return scheme in {"rtsp", "rtp", "udp"} or looks_like_direct_media(url)


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
            return (
                parsed.query.split("v=", 1)[1].split("&", 1)[0]
                if "v=" in parsed.query
                else None
            )
        match = re.match(r"^/(?:live|embed|shorts)/([^/?]+)", parsed.path)
        return match.group(1) if match else None
    return None


def is_youtube_url(url: str) -> bool:
    return youtube_video_id(url) is not None


def resolve_stream(url: str):
    debug_log(f"RESOLVE starting original={redact_url(url)}")

    # Check for raw direct stream links (.m3u8, rtsp://, etc.)
    if is_direct_stream_url(url):
        return url, None, {}

    # Standard yt-dlp extraction options optimized for live streams & VLC playback
    ydl_opts = {
        "format": "best[protocol^=m3u8]/bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "live_from_start": False,
        "skip_download": True,
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                raise RuntimeError("Could not extract stream metadata.")

            # If yt-dlp returns multiple entries (playlist or live channel stream)
            if "entries" in info:
                info = info["entries"][0]

            stream_url = info.get("url")
            thumbnail = info.get("thumbnail")
            headers = info.get("http_headers", {})

            if not stream_url:
                playable_formats = [item for item in info.get("formats", []) if item.get("url")]
                if playable_formats:
                    selected_format = max(playable_formats, key=lambda item: item.get("height") or 0)
                    stream_url = selected_format["url"]
                    headers = selected_format.get("http_headers", headers)

            if not stream_url:
                raise RuntimeError("No valid playback URL found in metadata.")

            return stream_url, thumbnail, headers

    except Exception as exc:
        debug_log(f"RESOLVE failed for {redact_url(url)}: {exc}")
        raise RuntimeError(
            f"Could not extract a playable stream from that link: {exc}"
        ) from exc