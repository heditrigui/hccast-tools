"""Turn a web page link (YouTube, Vimeo, ...) into a direct stream URL with yt-dlp, if it is installed."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from hccast.player import MEDIA
from hccast.serve import mime_for

# The projector can only fetch one URL, so ask for a format with audio and video in a single file,
# preferring MP4 (H.264) which these devices decode best.
FORMAT = "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best"

MISSING = (
    "This looks like a web page, not a video file. To play videos from YouTube and other sites, "
    "install yt-dlp:  pip install yt-dlp  (or pass a direct link to a .mp4/.mp3 file)."
)


class WebLinkError(RuntimeError):
    pass


@dataclass
class Stream:
    url: str
    title: str
    mime: str


def is_direct(url: str) -> bool:
    """True if the link already points at a media file (by its extension) or an HLS playlist."""
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(".m3u8") or any(path.endswith(ext) for ext in MEDIA)


def _mime(info: dict) -> str:
    ext = info.get("ext") or ""
    if info.get("protocol", "").startswith("m3u8"):
        return "application/vnd.apple.mpegurl"
    if info.get("vcodec") == "none":
        return mime_for(Path(f"x.{ext}")) if ext else "audio/mpeg"
    return mime_for(Path(f"x.{ext}")) if ext else "video/mp4"


def stream_from_info(info: dict) -> Stream:
    """Pick the single playable URL out of yt-dlp's JSON for one video."""
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise WebLinkError("That link is an empty playlist.")
        info = entries[0]
    url = info.get("url")
    if not url:
        # Without a single combined format, yt-dlp lists separate audio and video streams.
        raise WebLinkError("This site only offers separate audio and video streams, which the projector can't combine.")
    return Stream(url=url, title=info.get("title") or "Web video", mime=_mime(info))


def _run_module(page_url: str) -> dict:
    import yt_dlp  # type: ignore[import-not-found]

    opts = {"format": FORMAT, "quiet": True, "no_warnings": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.sanitize_info(ydl.extract_info(page_url, download=False))


def _run_exe(exe: str, page_url: str) -> dict:
    proc = subprocess.run([exe, "-f", FORMAT, "--no-playlist", "-J", "--", page_url],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        msg = (proc.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise WebLinkError(f"yt-dlp could not read that link: {msg}")
    return json.loads(proc.stdout)


def resolve(page_url: str) -> Stream:
    """Direct links pass through; web pages go through yt-dlp (Python package or command)."""
    if is_direct(page_url):
        name = urllib.parse.unquote(urllib.parse.urlsplit(page_url).path.rsplit("/", 1)[-1])
        mime = "application/vnd.apple.mpegurl" if name.lower().endswith(".m3u8") else mime_for(Path(name or "x"))
        return Stream(url=page_url, title=name or page_url, mime=mime)
    try:
        info = _run_module(page_url)
    except ImportError:
        exe = shutil.which("yt-dlp")
        if not exe:
            raise WebLinkError(MISSING) from None
        info = _run_exe(exe, page_url)
    except Exception as exc:  # yt-dlp raises its own DownloadError types
        if isinstance(exc, WebLinkError):
            raise
        raise WebLinkError(f"yt-dlp could not read that link: {exc}") from exc
    return stream_from_info(info)
