"""Minimal UPnP/DLNA MediaRenderer client: discovery, playback control, volume."""

from __future__ import annotations

import html
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

SSDP_ADDR = ("239.255.255.250", 1900)
RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RCS = "urn:schemas-upnp-org:service:RenderingControl:1"
_NS = {"d": "urn:schemas-upnp-org:device-1-0"}


class DlnaError(RuntimeError):
    pass


@dataclass
class Renderer:
    ip: str
    location: str
    server: str = ""
    name: str = ""
    manufacturer: str = ""
    model: str = ""
    control: dict[str, str] = field(default_factory=dict)  # service type -> absolute control URL

    @property
    def is_hccast(self) -> bool:
        return "hccast" in f"{self.server} {self.name}".lower()


def parse_ssdp(data: bytes) -> dict[str, str]:
    headers = {}
    for line in data.decode("latin-1").split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers


def search(timeout: float = 3.0, st: str = RENDERER, target_ip: str | None = None) -> list[dict[str, str]]:
    """Send an SSDP M-SEARCH and collect the responses."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode()
    found: dict[str, dict[str, str]] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.5)
        s.sendto(msg, SSDP_ADDR)
        if target_ip:
            s.sendto(msg, (target_ip, 1900))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            h = parse_ssdp(data)
            if "location" in h:
                h["ip"] = addr[0]
                found[h["location"]] = h
    return list(found.values())


def parse_description(xml_text: str, location: str, ip: str, server: str = "") -> Renderer:
    root = ET.fromstring(xml_text)
    base = root.findtext("d:URLBase", default="", namespaces=_NS) or location
    dev = root.find("d:device", _NS)
    if dev is None:
        raise DlnaError("Device description has no <device> element.")
    r = Renderer(
        ip=ip, location=location, server=server,
        name=dev.findtext("d:friendlyName", default="", namespaces=_NS),
        manufacturer=dev.findtext("d:manufacturer", default="", namespaces=_NS),
        model=dev.findtext("d:modelName", default="", namespaces=_NS),
    )
    for svc in dev.iterfind(".//d:service", _NS):
        stype = svc.findtext("d:serviceType", default="", namespaces=_NS)
        ctrl = svc.findtext("d:controlURL", default="", namespaces=_NS)
        if stype and ctrl:
            r.control[stype] = urllib.parse.urljoin(base, ctrl)
    return r


def describe(location: str, ip: str, server: str = "", timeout: float = 5.0) -> Renderer:
    with urllib.request.urlopen(location, timeout=timeout) as resp:
        return parse_description(resp.read().decode("utf-8", "replace"), location, ip, server)


def discover(timeout: float = 3.0, target_ip: str | None = None) -> list[Renderer]:
    renderers = []
    for h in search(timeout, target_ip=target_ip):
        if target_ip and h["ip"] != target_ip:
            continue
        try:
            renderers.append(describe(h["location"], h["ip"], h.get("server", "")))
        except (OSError, ET.ParseError, DlnaError):
            continue
    return renderers


def find(ip: str, timeout: float = 3.0) -> Renderer:
    for r in discover(timeout, target_ip=ip):
        if r.ip == ip:
            return r
    raise DlnaError(f"No DLNA renderer answered at {ip}. Is the device on and in DLNA/cast mode?")


# --- SOAP -------------------------------------------------------------------------------

def soap_envelope(service: str, action: str, args: dict[str, str]) -> bytes:
    body = "".join(f"<{k}>{html.escape(str(v), quote=False)}</{k}>" for k, v in args.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode("utf-8")


def parse_soap_response(xml_text: str) -> dict[str, str]:
    root = ET.fromstring(xml_text)
    body = next((el for el in root.iter() if el.tag.endswith("Body")), None)
    if body is None or not len(body):
        return {}
    resp = body[0]
    if resp.tag.endswith("Fault"):
        code = next((el.text for el in resp.iter() if el.tag.endswith("errorCode")), "?")
        desc = next((el.text for el in resp.iter() if el.tag.endswith("errorDescription")), "")
        raise DlnaError(f"Device refused the request (UPnP error {code} {desc or ''}).".replace(" )", ")"))
    return {child.tag.split("}")[-1]: (child.text or "") for child in resp}


def call(r: Renderer, service: str, action: str, timeout: float = 8.0, **args: str) -> dict[str, str]:
    url = r.control.get(service)
    if not url:
        raise DlnaError(f"{r.name or r.ip} does not offer {service.split(':')[-2]}.")
    req = urllib.request.Request(url, data=soap_envelope(service, action, args), method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{service}#{action}"',
        "User-Agent": "hccast-tools",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parse_soap_response(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:  # UPnP faults come back as HTTP 500 with a SOAP body
        return parse_soap_response(exc.read().decode("utf-8", "replace"))


def didl(url: str, title: str, mime: str) -> str:
    upnp_class = {"video": "object.item.videoItem", "audio": "object.item.audioItem.musicTrack",
                  "image": "object.item.imageItem.photo"}.get(mime.split("/")[0], "object.item")
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<item id="1" parentID="0" restricted="1"><dc:title>{html.escape(title)}</dc:title>'
        f"<upnp:class>{upnp_class}</upnp:class>"
        f'<res protocolInfo="http-get:*:{mime}:*">{html.escape(url)}</res></item></DIDL-Lite>'
    )


def play_url(r: Renderer, url: str, title: str = "hccast-tools", mime: str = "video/mp4") -> None:
    try:
        call(r, AVT, "Stop", InstanceID="0")
    except DlnaError:
        pass  # nothing was playing
    call(r, AVT, "SetAVTransportURI", InstanceID="0", CurrentURI=url, CurrentURIMetaData=didl(url, title, mime))
    call(r, AVT, "Play", InstanceID="0", Speed="1")


def pause(r: Renderer) -> None:
    call(r, AVT, "Pause", InstanceID="0")


def resume(r: Renderer) -> None:
    call(r, AVT, "Play", InstanceID="0", Speed="1")


def stop(r: Renderer) -> None:
    call(r, AVT, "Stop", InstanceID="0")


def seek(r: Renderer, position: str) -> None:
    if not re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", position):
        raise ValueError("Position must look like H:MM:SS, e.g. 0:05:30")
    call(r, AVT, "Seek", InstanceID="0", Unit="REL_TIME", Target=position)


def status(r: Renderer) -> dict[str, str]:
    info = call(r, AVT, "GetTransportInfo", InstanceID="0")
    try:
        pos = call(r, AVT, "GetPositionInfo", InstanceID="0")
    except DlnaError:
        pos = {}
    return {
        "state": info.get("CurrentTransportState", "?"),
        "position": pos.get("RelTime", ""),
        "duration": pos.get("TrackDuration", ""),
        "uri": pos.get("TrackURI", ""),
    }


def get_volume(r: Renderer) -> int:
    return int(call(r, RCS, "GetVolume", InstanceID="0", Channel="Master").get("CurrentVolume", "0") or 0)


def set_volume(r: Renderer, level: int) -> None:
    if not 0 <= level <= 100:
        raise ValueError("Volume must be 0-100.")
    call(r, RCS, "SetVolume", InstanceID="0", Channel="Master", DesiredVolume=str(level))
