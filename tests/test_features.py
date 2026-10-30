"""Playlists and slideshows, web links, the password check and the GUI, against the fake device."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from hccast import cli, device, dlna, gui, player, weblink
from test_hccast import fake  # noqa: F401  (pytest fixture)


def _media_folder(tmp_path):
    for name in ["b.jpg", "a.png", "c.mp4", "notes.txt", "D.mp3"]:
        (tmp_path / name).write_bytes(b"x" * 64)
    (tmp_path / "sub").mkdir()
    return tmp_path


def _uris(fake):
    return [body.split("<CurrentURI>")[1].split("</CurrentURI>")[0] for action, body in list(fake.calls)
            if action == "SetAVTransportURI"]


def _player(fake, items, **kw):
    kw = {"slideshow": 0.01, "poll": 0.01, "idle_after": 0.02, "start_timeout": 1, "on_event": lambda m: None, **kw}
    return player.Player(fake.renderer(), items, **kw)


def _use_fake_page(fake, monkeypatch):
    monkeypatch.setattr(device, "_get", lambda ip, path, timeout=5.0: urllib.request.urlopen(fake.base + path.lstrip("/")).read())


# --- playlists, folders, slideshows ---------------------------------------------------------

def test_collect_folder_and_intervals(tmp_path):
    folder = _media_folder(tmp_path)
    assert [p.name for p in player.collect(folder)] == ["a.png", "b.jpg", "c.mp4", "D.mp3"]
    assert player.collect(folder / "c.mp4") == [folder / "c.mp4"]
    with pytest.raises(FileNotFoundError):
        player.collect(folder / "missing")
    with pytest.raises(FileNotFoundError):
        player.collect(folder / "sub")
    assert player.parse_interval("5s") == 5 and player.parse_interval("2m") == 120 and player.parse_interval("1.5") == 1.5
    with pytest.raises(ValueError):
        player.parse_interval("soon")


def test_slideshow_plays_photos_in_order(fake, tmp_path):
    photos = [p for p in player.collect(_media_folder(tmp_path)) if player.is_image(p)]
    _player(fake, photos).run()
    assert [u.rsplit("/", 1)[1] for u in _uris(fake)] == ["a.png", "b.jpg"]
    assert "object.item.imageItem.photo" in fake.calls[1][1]


def test_playlist_waits_for_each_video_to_finish(fake):
    # Each video plays for a few polls, then the device reports it stopped.
    fake.states = ["PLAYING", "PLAYING", "STOPPED", "STOPPED", "STOPPED", "STOPPED"] * 2 + ["STOPPED"] * 20
    _player(fake, ["http://example.test/1.mp4", "http://example.test/2.mp4"]).run()
    assert _uris(fake) == ["http://example.test/1.mp4", "http://example.test/2.mp4"]
    polls = [a for a, _ in fake.calls].count("GetTransportInfo")
    assert polls >= 6  # it waited for the first video instead of skipping straight on


def test_shuffle_and_loop(fake, tmp_path):
    photos = [p for p in player.collect(_media_folder(tmp_path)) if player.is_image(p)]
    p = _player(fake, photos, shuffle=True, loop=True)
    t = threading.Thread(target=p.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while len(_uris(fake)) < 6 and time.monotonic() < deadline:
        time.sleep(0.01)
    p.stop()
    t.join(2)
    names = [u.rsplit("/", 1)[1] for u in _uris(fake)]
    assert len(names) >= 6 and not t.is_alive()
    assert sorted(names[:2]) == ["a.png", "b.jpg"] and sorted(names[2:4]) == ["a.png", "b.jpg"]


def test_cli_cast_folder(fake, tmp_path, monkeypatch, capsys):
    folder = _media_folder(tmp_path)
    (folder / "c.mp4").unlink()
    (folder / "D.mp3").unlink()
    monkeypatch.setattr(dlna, "find", lambda ip, timeout=3.0: fake.renderer())
    assert cli.main(["cast", "127.0.0.1", str(folder), "--slideshow", "0.01"]) == 0
    assert len(_uris(fake)) == 2
    assert "2 files" in capsys.readouterr().out


# --- web links ------------------------------------------------------------------------------

def test_direct_links_skip_yt_dlp():
    assert weblink.is_direct("https://cdn.example.test/v/clip.MP4?sig=1")
    assert not weblink.is_direct("https://www.youtube.com/watch?v=abc")
    s = weblink.resolve("https://cdn.example.test/live/index.m3u8")
    assert s.mime == "application/vnd.apple.mpegurl" and s.title == "index.m3u8"


def test_page_link_without_yt_dlp_explains(monkeypatch):
    def no_module(url):
        raise ImportError("yt_dlp")

    monkeypatch.setattr(weblink, "_run_module", no_module)
    monkeypatch.setattr(weblink.shutil, "which", lambda name: None)
    with pytest.raises(weblink.WebLinkError, match="pip install yt-dlp"):
        weblink.resolve("https://www.youtube.com/watch?v=abc")


def test_page_link_with_yt_dlp(monkeypatch):
    info = {"title": "A video", "url": "https://media.example.test/v.mp4", "ext": "mp4", "vcodec": "avc1"}
    monkeypatch.setattr(weblink, "_run_module", lambda url: info)
    s = weblink.resolve("https://www.youtube.com/watch?v=abc")
    assert (s.url, s.title, s.mime) == ("https://media.example.test/v.mp4", "A video", "video/mp4")
    with pytest.raises(weblink.WebLinkError, match="separate audio and video"):
        weblink.stream_from_info({"title": "x", "requested_formats": [{}, {}]})
    assert weblink.stream_from_info({"_type": "playlist", "entries": [info]}).title == "A video"


def test_cli_cast_page_link(fake, monkeypatch):
    monkeypatch.setattr(dlna, "find", lambda ip, timeout=3.0: fake.renderer())
    monkeypatch.setattr(weblink, "_run_module",
                        lambda url: {"title": "Clip", "url": "https://media.example.test/c.mp4", "ext": "mp4"})
    assert cli.main(["cast", "127.0.0.1", "https://www.youtube.com/watch?v=abc"]) == 0
    assert _uris(fake) == ["https://media.example.test/c.mp4"]


# --- Wi-Fi Direct password ------------------------------------------------------------------

def test_set_password_only_reads_and_explains(fake, monkeypatch, capsys):
    _use_fake_page(fake, monkeypatch)
    calls = device.page_calls("127.0.0.1")
    assert calls == ["misc_submit?type=restart", "setting_get", "upgrade_submit?url="]
    assert device.password_calls(calls) == []
    assert device.password_calls(["password_submit?pwd=", "password_get"]) == ["password_submit?pwd="]
    assert cli.main(["set-password", "127.0.0.1"]) == 1
    assert "no way to change the Wi-Fi Direct password" in capsys.readouterr().out
    assert fake.calls == []  # nothing was sent to the device besides reading its page


# --- GUI ------------------------------------------------------------------------------------

def test_gui_api_requires_token_and_controls_device(fake):
    ctl = gui.Controller()
    ctl.renderers["127.0.0.1"] = fake.renderer()
    httpd, url = gui.make_server(controller=ctl)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base, token = url.split("#")

    def post(name, body, tok=token):
        req = urllib.request.Request(base + "api/" + name, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Token": tok, "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    try:
        with urllib.request.urlopen(base) as resp:
            assert b"<title>hccast</title>" in resp.read()
        with pytest.raises(urllib.error.HTTPError) as exc:
            post("status", {"ip": "127.0.0.1"}, tok="wrong")
        assert exc.value.code == 403

        st = post("status", {"ip": "127.0.0.1"})
        assert st["state"] == "PLAYING" and st["volume"] == 35
        post("control", {"ip": "127.0.0.1", "action": "volume", "value": "20"})
        assert "<DesiredVolume>20</DesiredVolume>" in fake.calls[-1][1]

        assert post("cast", {"ip": "127.0.0.1", "source": "https://media.example.test/c.mp4"})["count"] == 1
        deadline = time.monotonic() + 5
        while not _uris(fake) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _uris(fake) == ["https://media.example.test/c.mp4"]
        post("control", {"ip": "127.0.0.1", "action": "stop"})

        with pytest.raises(urllib.error.HTTPError) as exc:
            post("control", {"ip": "127.0.0.1", "action": "explode"})
        assert exc.value.code == 400
    finally:
        ctl.stop_player()
        httpd.shutdown()
