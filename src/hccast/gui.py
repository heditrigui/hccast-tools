"""`hccast gui`: a small control page served on this computer and opened in the browser."""

from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hccast import device, dlna, player, weblink


class Controller:
    """What the page can do. Holds the found devices and the item currently playing."""

    def __init__(self):
        self.renderers: dict[str, dlna.Renderer] = {}
        self.player: player.Player | None = None
        self.events: list[str] = []
        self.lock = threading.Lock()

    def _log(self, msg: str) -> None:
        self.events = (self.events + [msg])[-20:]

    def renderer(self, ip: str) -> dlna.Renderer:
        if ip not in self.renderers:
            self.renderers[ip] = dlna.find(ip)
        return self.renderers[ip]

    def discover(self, _: dict) -> dict:
        found = dlna.discover()
        self.renderers.update({r.ip: r for r in found})
        found.sort(key=lambda r: (not r.is_hccast, r.ip))
        return {"devices": [{"ip": r.ip, "name": r.name, "hccast": r.is_hccast,
                             "model": f"{r.manufacturer} {r.model}".strip()} for r in found]}

    def info(self, q: dict) -> dict:
        data = device.settings(q["ip"])
        rep = device.analyze(data)
        return {"rows": [["Product", rep.product], ["Firmware", rep.version], ["Platform", rep.firmware],
                         ["Name", rep.name], ["Chip vendor", rep.vendor], ["MAC", rep.mac],
                         ["Runs Android", "no"]],
                "verdict": rep.verdict, "warnings": rep.warnings}

    def cast(self, q: dict) -> dict:
        r = self.renderer(q["ip"])
        source = q["source"].strip().strip('"')
        if source.startswith(("http://", "https://")):
            stream = weblink.resolve(source)
            items: list = [stream.url]
            title, mime = stream.title, stream.mime
        else:
            items = player.collect(Path(source).expanduser())
            title, mime = None, None
        self.stop_player()
        p = player.Player(r, items, slideshow=player.parse_interval(q.get("slideshow") or "5s"),
                          shuffle=bool(q.get("shuffle")), loop=bool(q.get("loop")), title=title, mime=mime,
                          on_event=self._log)

        def run():
            try:
                p.run()
            except Exception as exc:  # shown on the page instead of killing the thread silently
                self._log(f"Error: {exc}")

        with self.lock:
            self.player = p
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "count": len(items)}

    def stop_player(self) -> None:
        with self.lock:
            if self.player:
                self.player.stop()
            self.player = None

    def control(self, q: dict) -> dict:
        r = self.renderer(q["ip"])
        action = q["action"]
        if action == "stop":
            self.stop_player()
            dlna.stop(r)
        elif action == "pause":
            dlna.pause(r)
        elif action == "play":
            dlna.resume(r)
        elif action == "seek":
            dlna.seek(r, q["value"])
        elif action == "volume":
            dlna.set_volume(r, int(q["value"]))
        else:
            raise ValueError(f"Unknown action {action!r}")
        return {"ok": True}

    def status(self, q: dict) -> dict:
        r = self.renderer(q["ip"])
        st = dlna.status(r)
        try:
            st["volume"] = dlna.get_volume(r)
        except dlna.DlnaError:
            st["volume"] = None
        with self.lock:
            cur = self.player.current if self.player and not self.player.stopped else None
        st["current"] = (cur.name if isinstance(cur, Path) else cur) if cur else None
        st["events"] = self.events
        return st

    def browse(self, q: dict) -> dict:
        """Open the system's file picker on this computer (the page itself can't see file paths)."""
        try:
            import tkinter
            from tkinter import filedialog
        except ImportError:
            raise ValueError("No file picker available here; type the path instead.") from None
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if q.get("folder"):
                path = filedialog.askdirectory(title="Pick a folder to play")
            else:
                path = filedialog.askopenfilename(title="Pick a file to play")
        finally:
            root.destroy()
        return {"path": path or ""}


def make_server(port: int = 0, controller: Controller | None = None) -> tuple[ThreadingHTTPServer, str]:
    """Build the page server on 127.0.0.1. Returns the server and the page URL (which carries an access token)."""
    ctl = controller or Controller()
    token = secrets.token_urlsafe(16)
    page = (Path(__file__).with_name("gui.html")).read_text(encoding="utf-8")
    routes = {"discover": ctl.discover, "info": ctl.info, "cast": ctl.cast, "control": ctl.control,
              "status": ctl.status, "browse": ctl.browse}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/":
                self._send(200, page.encode(), "text/html; charset=utf-8")
            else:
                self._send(404, b"", "text/plain")

        def do_POST(self):
            # Other web pages open in the browser can't read the token, so they can't drive the projector.
            if self.headers.get("X-Token") != token:
                self._send(403, b'{"error": "bad token"}', "application/json")
                return
            fn = routes.get(urllib.parse.urlsplit(self.path).path.removeprefix("/api/"))
            if not fn:
                self._send(404, b'{"error": "unknown call"}', "application/json")
                return
            try:
                q = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                out, code = fn(q), 200
            except (device.DeviceError, dlna.DlnaError, weblink.WebLinkError, FileNotFoundError,
                    ValueError, KeyError, OSError) as exc:
                out, code = {"error": str(exc)}, 400
            self._send(code, json.dumps(out).encode(), "application/json")

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/#{token}"


def serve(port: int = 0, open_browser: bool = True) -> None:
    httpd, url = make_server(port)
    print(f"hccast control page: {url}")
    print("Press Ctrl+C to quit.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        httpd.server_close()
