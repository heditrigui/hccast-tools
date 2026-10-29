"""`hccast` command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from hccast import __version__, device, dlna
from hccast.serve import FileServer, lan_ip_for


def _pick(ip: str | None) -> str:
    """Use the given IP, or discover the single HCCast device on the network."""
    if ip:
        return ip
    found = [r for r in dlna.discover() if r.is_hccast]
    if not found:
        raise SystemExit("No HCCast device found. Make sure it is on and connected to the same Wi-Fi, or pass its IP.")
    if len(found) > 1:
        raise SystemExit("Several HCCast devices found; pass an IP: " + ", ".join(r.ip for r in found))
    return found[0].ip


def cmd_discover(args) -> int:
    renderers = dlna.discover(args.timeout)
    if not renderers:
        print("No DLNA renderers answered. The device must be on the same network as this computer.")
        return 1
    for r in sorted(renderers, key=lambda r: (not r.is_hccast, r.ip)):
        tag = "HCCast" if r.is_hccast else "DLNA  "
        print(f"{tag}  {r.ip:15}  {r.name}  ({r.manufacturer} {r.model})".rstrip())
    return 0


def cmd_info(args) -> int:
    ip = _pick(args.ip)
    data = device.settings(ip)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    rep = device.analyze(data)
    rows = [
        ("Address", ip), ("Name", rep.name), ("Product", rep.product), ("Firmware", rep.version),
        ("Platform", rep.firmware), ("Chip vendor", rep.vendor), ("MAC", rep.mac),
        ("Runs Android", "no" if not rep.runs_android else "yes"),
    ]
    for k, v in rows:
        print(f"{k:13} {v or '-'}")
    print(f"\n{rep.verdict}")
    for w in rep.warnings:
        print(f"  ! {w}")
    return 0


def cmd_audit(args) -> int:
    ip = _pick(args.ip)
    rep = device.analyze(device.settings(ip))
    if not rep.warnings:
        print("No problems found.")
        return 0
    for w in rep.warnings:
        print(f"! {w}")
    print("\nFix: change the Wi-Fi Direct password from the projector's own Network / Cast settings menu.")
    return 1


def cmd_cast(args) -> int:
    ip = _pick(args.ip)
    r = dlna.find(ip)
    source = args.source
    if source.startswith(("http://", "https://")):
        dlna.play_url(r, source, title=args.title or source.rsplit("/", 1)[-1], mime=args.mime or "video/mp4")
        print(f"Casting {source} to {r.name or ip}.")
        return 0

    path = Path(source)
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")
    with FileServer(path, lan_ip_for(ip), allowed_client=ip, port=args.port) as srv:
        dlna.play_url(r, srv.url, title=args.title or path.stem, mime=args.mime or srv.mime)
        print(f"Casting {path.name} to {r.name or ip}. Keep this window open; press Ctrl+C to stop.")
        try:
            idle_since = None
            while True:
                time.sleep(2)
                state = dlna.status(r)["state"]
                if state in ("STOPPED", "NO_MEDIA_PRESENT"):
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since > 10:
                        print("Playback finished.")
                        return 0
                else:
                    idle_since = None
        except KeyboardInterrupt:
            dlna.stop(r)
            print("\nStopped.")
    return 0


def cmd_control(args) -> int:
    r = dlna.find(_pick(args.ip))
    if args.action == "status":
        st = dlna.status(r)
        print(f"{st['state']}  {st['position']}/{st['duration']}  {st['uri']}".rstrip())
    elif args.action == "seek":
        if not args.value:
            raise SystemExit("Usage: hccast control <ip> seek 0:05:30")
        dlna.seek(r, args.value)
    else:
        {"play": dlna.resume, "pause": dlna.pause, "stop": dlna.stop}[args.action](r)
    return 0


def cmd_volume(args) -> int:
    r = dlna.find(_pick(args.ip))
    if args.level is None:
        print(dlna.get_volume(r))
    else:
        dlna.set_volume(r, args.level)
    return 0


def cmd_restart(args) -> int:
    ip = _pick(args.ip)
    if not args.yes:
        if not sys.stdin.isatty() or input(f"Restart the device at {ip}? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return 1
    device.restart(ip)
    print("Restart requested.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hccast", description="Inspect and control HCCast projectors and cast dongles.")
    p.add_argument("--version", action="version", version=f"hccast-tools {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("discover", help="find HCCast and other DLNA devices on the network")
    s.add_argument("--timeout", type=float, default=3.0)
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("info", help="show what the device really is (model, firmware, Android or not)")
    s.add_argument("ip", nargs="?")
    s.add_argument("--json", action="store_true", help="print the raw settings the device reports")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("audit", help="check for default passwords and exposed settings")
    s.add_argument("ip", nargs="?")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("cast", help="play a local video/audio/image file or a URL on the device")
    s.add_argument("ip", nargs="?")
    s.add_argument("source", help="path to a file, or an http(s) URL")
    s.add_argument("--title")
    s.add_argument("--mime", help="override the media type, e.g. video/mp4")
    s.add_argument("--port", type=int, default=0, help="local port for the file server (default: random)")
    s.set_defaults(func=cmd_cast)

    s = sub.add_parser("control", help="play, pause, stop, seek or show playback status")
    s.add_argument("ip", nargs="?")
    s.add_argument("action", choices=["play", "pause", "stop", "status", "seek"])
    s.add_argument("value", nargs="?", help="seek position, e.g. 0:05:30")
    s.set_defaults(func=cmd_control)

    s = sub.add_parser("volume", help="show or set the volume (0-100)")
    s.add_argument("ip", nargs="?")
    s.add_argument("level", nargs="?", type=int)
    s.set_defaults(func=cmd_volume)

    s = sub.add_parser("restart", help="reboot the device")
    s.add_argument("ip", nargs="?")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    s.set_defaults(func=cmd_restart)
    return p


def _fix_optional_ip(argv: list[str]) -> list[str]:
    """Allow `hccast cast movie.mp4` and `hccast control pause` without an IP."""
    if len(argv) >= 2 and argv[0] in ("cast", "control", "volume") and not _looks_like_ip(argv[1]):
        return [argv[0], "", *argv[1:]]
    return argv


def _looks_like_ip(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def main(argv: list[str] | None = None) -> int:
    argv = _fix_optional_ip(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    if getattr(args, "ip", None) == "":
        args.ip = None
    try:
        return args.func(args)
    except (device.DeviceError, dlna.DlnaError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
