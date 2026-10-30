"""Play one file, a folder or a list of URLs on a renderer, one item after another."""

from __future__ import annotations

import random
import re
import threading
import time
from pathlib import Path
from typing import Callable, Union

from hccast import dlna
from hccast.serve import FileServer, lan_ip_for, mime_for

VIDEO = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".ts", ".wmv", ".flv", ".mpg", ".mpeg"}
AUDIO = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".wma"}
IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
MEDIA = VIDEO | AUDIO | IMAGE
IDLE = ("STOPPED", "NO_MEDIA_PRESENT")

Item = Union[Path, str]  # a local file, or an http(s) URL


def collect(source: Path) -> list[Path]:
    """A file as-is, or the media files of a folder in name order (not recursive)."""
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise FileNotFoundError(f"File or folder not found: {source}")
    files = sorted((p for p in source.iterdir() if p.is_file() and p.suffix.lower() in MEDIA),
                   key=lambda p: p.name.lower())
    if not files:
        raise FileNotFoundError(f"No video, music or photo files in {source}")
    return files


def parse_interval(text: str) -> float:
    """'5s', '2m', '1.5' (seconds) -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|sec|m|min)?\s*", text.lower())
    if not m:
        raise ValueError(f"Interval must look like 5s or 2m, not {text!r}")
    value = float(m.group(1)) * (60 if (m.group(2) or "s").startswith("m") else 1)
    if value <= 0:
        raise ValueError("Interval must be more than 0.")
    return value


def is_image(item: Item) -> bool:
    return isinstance(item, Path) and item.suffix.lower() in IMAGE


class Player:
    """Plays `items` in order (or shuffled), waiting for each to finish; photos stay up `slideshow` seconds."""

    def __init__(self, renderer: dlna.Renderer, items: list[Item], *, slideshow: float = 5.0,
                 shuffle: bool = False, loop: bool = False, title: str | None = None, mime: str | None = None,
                 port: int = 0, on_event: Callable[[str], None] = print,
                 poll: float = 2.0, idle_after: float = 4.0, start_timeout: float = 30.0):
        self.r = renderer
        self.items = list(items)
        self.slideshow = slideshow
        self.shuffle = shuffle
        self.loop = loop
        self.title = title  # only used when there is a single item
        self.mime = mime
        self.port = port
        self.on_event = on_event
        self.poll = poll
        self.idle_after = idle_after
        self.start_timeout = start_timeout
        self._stop = threading.Event()
        self.current: Item | None = None

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:
        while not self._stop.is_set():
            order = self.items[:]
            if self.shuffle:
                random.shuffle(order)
            for item in order:
                if self._stop.is_set():
                    return
                self.current = item
                self._play(item)
            if not self.loop:
                return

    def _label(self, item: Item) -> str:
        if self.title and len(self.items) == 1:
            return self.title
        return item.stem if isinstance(item, Path) else item.rsplit("/", 1)[-1].split("?")[0] or item

    def _play(self, item: Item) -> None:
        title = self._label(item)
        if isinstance(item, str):
            dlna.play_url(self.r, item, title=title, mime=self.mime or "video/mp4")
            self.on_event(f"Playing {title}")
            self._wait(image=False)
            return
        with FileServer(item, lan_ip_for(self.r.ip), allowed_client=self.r.ip, port=self.port) as srv:
            mime = self.mime if len(self.items) == 1 and self.mime else mime_for(item)
            dlna.play_url(self.r, srv.url, title=title, mime=mime)
            self.on_event(f"Playing {item.name}")
            self._wait(image=is_image(item))

    def _wait(self, image: bool) -> None:
        """Return when the item has finished (or, for a photo, when its slideshow time is up)."""
        if image:
            self._stop.wait(self.slideshow)
            return
        started = time.monotonic()
        seen_playing = False
        idle_since = None
        while not self._stop.wait(self.poll):
            try:
                state = dlna.status(self.r)["state"]
            except (dlna.DlnaError, OSError):
                continue  # one missed poll is not the end of the video
            if state in IDLE:
                idle_since = idle_since or time.monotonic()
                if seen_playing and time.monotonic() - idle_since >= self.idle_after:
                    return
            else:
                idle_since = None
                seen_playing = seen_playing or state == "PLAYING"
            if not seen_playing and time.monotonic() - started > self.start_timeout:
                self.on_event(f"The device did not start {self._label(self.current)}; skipping it.")
                return
