import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hccast import cli, device, dlna
from hccast.serve import FileServer, parse_range

# What a real HCCast projector sold as "HY300 Pro" reports (identifiers changed).
SETTINGS = {
    "product": "HY300PRO_MAG",
    "version": "2406150908",
    "vendor": "hichip",
    "device_name": "Hccast-123456",
    "device_password": "12345678",
    "upgrade_api": "http://203.0.113.10/upgrade_package/HY300PRO_MAG_hcprojector_linux_upgrade_config.jsonp",
    "mac": "00:1A:2B:3C:4D:5E",
}

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><specVersion><major>1</major><minor>0</minor></specVersion>
<device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
<friendlyName>Hccast-123456_dlna</friendlyName><manufacturer>ElfCast.</manufacturer><modelName>ElfCast</modelName>
<serviceList>
<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
<controlURL>/upnp/control/rendertransport1</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
<controlURL>/upnp/control/rendercontrol1</controlURL></service>
</serviceList></device><URLBase>{base}</URLBase></root>"""


class FakeDevice:
    """Local HTTP server imitating an HCCast device's web page and DLNA renderer."""

    def __init__(self):
        self.calls = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/xml"):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/setting_get":
                    self._send(200, json.dumps(SETTINGS), "application/json")
                elif self.path == "/description.xml":
                    self._send(200, DESCRIPTION.format(base=fake.base))
                elif self.path.startswith("/misc_submit"):
                    fake.calls.append(("GET", self.path))
                    self._send(200, "{}", "application/json")
                else:
                    self._send(404, "")

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                action = self.headers["SOAPACTION"].strip('"').split("#")[1]
                fake.calls.append((action, body))
                if action == "Seek":
                    self._send(500, "<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'><s:Body>"
                               "<s:Fault><detail><UPnPError><errorCode>710</errorCode>"
                               "<errorDescription>Seek mode not supported</errorDescription>"
                               "</UPnPError></detail></s:Fault></s:Body></s:Envelope>")
                    return
                out = {"GetTransportInfo": "<CurrentTransportState>PLAYING</CurrentTransportState>",
                       "GetPositionInfo": "<RelTime>0:00:10</RelTime><TrackDuration>0:01:00</TrackDuration>",
                       "GetVolume": "<CurrentVolume>35</CurrentVolume>"}.get(action, "")
                self._send(200, "<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'><s:Body>"
                                f"<u:{action}Response xmlns:u='x'>{out}</u:{action}Response></s:Body></s:Envelope>")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def renderer(self):
        return dlna.describe(self.base + "description.xml", "127.0.0.1", "HCLinux, UPnP/1.0, HCCast/v3.8")

    def actions(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def fake():
    f = FakeDevice()
    yield f
    f.httpd.shutdown()


def test_analyze_flags_fake_android_and_default_password():
    rep = device.analyze(SETTINGS)
    assert rep.firmware == "HCCast on Linux (hcprojector_linux)"
    assert not rep.runs_android
    text = " ".join(rep.warnings)
    assert "usually sold with Android" in text
    assert "factory default" in text


def test_analyze_clean_device():
    rep = device.analyze({"product": "MiniCast", "vendor": "hichip", "device_password": "S3cure-Pass"})
    assert rep.firmware == "HCCast (HiChip)"
    assert rep.warnings == ["The settings page shows the Wi-Fi Direct password to anyone on your network, without a login."]


def test_settings_and_restart_against_fake(fake, monkeypatch):
    monkeypatch.setattr(device, "_get", lambda ip, path, timeout=5.0: urllib.request.urlopen(fake.base + path.lstrip("/")).read())
    assert device.settings("127.0.0.1")["product"] == "HY300PRO_MAG"
    device.restart("127.0.0.1")
    assert fake.calls == [("GET", "/misc_submit?type=restart")]


def test_parse_ssdp():
    raw = b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.50:49595/description.xml\r\nSERVER: HCLinux, UPnP/1.0, HCCast/v3.8\r\n\r\n"
    h = dlna.parse_ssdp(raw)
    assert h["location"].endswith("description.xml") and "HCCast" in h["server"]


def test_description_parsing(fake):
    r = fake.renderer()
    assert r.is_hccast and r.name == "Hccast-123456_dlna"
    assert r.control[dlna.AVT] == fake.base + "upnp/control/rendertransport1"


def test_play_status_volume(fake):
    r = fake.renderer()
    dlna.play_url(r, "http://example.test/movie.mp4", title="Movie & <Friends>")
    assert fake.actions() == ["Stop", "SetAVTransportURI", "Play"]
    set_uri_body = fake.calls[1][1]
    assert "http://example.test/movie.mp4" in set_uri_body
    assert "&lt;Friends&gt;" in set_uri_body or "&amp;lt;Friends&amp;gt;" in set_uri_body
    assert dlna.status(r) == {"state": "PLAYING", "position": "0:00:10", "duration": "0:01:00", "uri": ""}
    assert dlna.get_volume(r) == 35
    dlna.set_volume(r, 50)
    assert "<DesiredVolume>50</DesiredVolume>" in fake.calls[-1][1]
    with pytest.raises(ValueError):
        dlna.set_volume(r, 101)


def test_upnp_fault_becomes_error(fake):
    with pytest.raises(dlna.DlnaError, match="710"):
        dlna.seek(fake.renderer(), "0:01:00")
    with pytest.raises(ValueError):
        dlna.seek(fake.renderer(), "90 seconds")


@pytest.mark.parametrize("header,expected", [
    ("bytes=0-99", (0, 99)), ("bytes=100-", (100, 999)), ("bytes=-100", (900, 999)),
    ("bytes=5000-", None), ("bytes=abc", None), ("bytes=-", None),
])
def test_parse_range(header, expected):
    assert parse_range(header, 1000) == expected


def test_file_server_serves_ranges_and_blocks_others(tmp_path):
    f = tmp_path / "clip one.mp4"
    f.write_bytes(bytes(range(256)) * 4)
    with FileServer(f, "127.0.0.1", allowed_client="127.0.0.1") as srv:
        assert srv.mime == "video/mp4" and srv.url.endswith("/clip%20one.mp4")
        req = urllib.request.Request(srv.url, headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 206 and resp.read() == bytes(range(10, 20))
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(srv.url.rsplit("/", 1)[0] + "/other.txt")
        assert exc.value.code == 404


def test_cli_optional_ip_rewrite():
    assert cli._fix_optional_ip(["cast", "movie.mp4"]) == ["cast", "", "movie.mp4"]
    assert cli._fix_optional_ip(["cast", "192.168.1.5", "movie.mp4"]) == ["cast", "192.168.1.5", "movie.mp4"]
    assert cli._fix_optional_ip(["control", "pause"]) == ["control", "", "pause"]
    assert cli._fix_optional_ip(["info"]) == ["info"]


def test_cli_info_output(capsys, monkeypatch):
    monkeypatch.setattr(device, "settings", lambda ip: SETTINGS)
    assert cli.main(["info", "192.168.1.50"]) == 0
    out = capsys.readouterr().out
    assert "HY300PRO_MAG" in out and "Runs Android  no" in out and "usually sold with Android" in out
