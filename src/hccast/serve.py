"""Serve one local file over HTTP (with Range support) so a DLNA renderer can stream it."""

from __future__ import annotations

import mimetypes
import os
import re
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/mp4", ".m4v")


def lan_ip_for(target: str) -> str:
    """The local address the OS would use to reach `target` (no packets are sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((target, 9))
        return s.getsockname()[0]


def mime_for(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:  # suffix range: last N bytes
        start, end = max(0, size - int(m.group(2))), size - 1
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


def _handler(path: Path, allowed_client: str | None):
    size = path.stat().st_size
    mime = mime_for(path)
    route = "/" + urllib.parse.quote(path.name)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _headers(self, code: int, length: int, extra: dict[str, str] | None = None):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("transferMode.dlna.org", "Streaming")
            self.send_header("contentFeatures.dlna.org", "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _check(self) -> bool:
            if allowed_client and self.client_address[0] not in (allowed_client, "127.0.0.1"):
                self.send_error(403)
                return False
            if urllib.parse.urlsplit(self.path).path != route:
                self.send_error(404)
                return False
            return True

        def do_HEAD(self):
            if self._check():
                self._headers(200, size)

        def do_GET(self):
            if not self._check():
                return
            rng = parse_range(self.headers.get("Range", ""), size) if self.headers.get("Range") else None
            if self.headers.get("Range") and rng is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = rng or (0, size - 1)
            length = end - start + 1
            if rng:
                self._headers(206, length, {"Content-Range": f"bytes {start}-{end}/{size}"})
            else:
                self._headers(200, length)
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(256 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (ConnectionError, OSError):
                pass  # the renderer closed the connection (seek, stop) - normal

    return Handler


class FileServer:
    """Serve `path` on `bind_ip`; only `allowed_client` (the renderer) may fetch it."""

    def __init__(self, path: str | os.PathLike, bind_ip: str, allowed_client: str | None = None, port: int = 0):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.httpd = ThreadingHTTPServer((bind_ip, port), _handler(self.path, allowed_client))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/{urllib.parse.quote(self.path.name)}"

    @property
    def mime(self) -> str:
        return mime_for(self.path)

    def __enter__(self) -> "FileServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
