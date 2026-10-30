# hccast-tools

**Bought an "Android" mini projector that has no apps? Find out what it really is, and control it from your PC.**

Many cheap projectors and cast dongles (often sold as *HY300 Pro*, *HY320*, "Android 11 smart projector", etc.) actually run **HCCast**, a small Linux casting firmware on a HiChip chip. They can mirror your screen, but they have no Android, no app store and no way to install apps. In Windows they show up as `Hccast-XXXXXX` when you press <kbd>Win</kbd>+<kbd>K</kbd>.

`hccast` talks to these devices over your Wi-Fi to:

- 🔍 **Reveal the truth.** Real product name, firmware, chip vendor, and whether it runs Android.
- 🔐 **Audit it.** Flags default Wi-Fi Direct passwords and settings exposed without a login.
- ▶️ **Cast to it from the command line or a simple control page.** Play local videos, music and photos, whole folders as playlists or slideshows, and video links (including YouTube with yt-dlp). No phone app needed.
- ⏯️ **Control playback.** Play, pause, stop, seek, volume, status.
- 🔄 **Restart it** remotely.

```
$ hccast info
Address       192.168.1.50
Name          Hccast-123456
Product       HY300PRO_MAG
Firmware      2406150908
Platform      HCCast on Linux (hcprojector_linux)
Chip vendor   hichip
MAC           00:1A:2B:3C:4D:5E
Runs Android  no

This is an HCCast screen-mirroring device. It receives Miracast, AirPlay and DLNA, but it does not run Android and cannot install apps.
  ! The product name 'HY300PRO_MAG' matches a model usually sold with Android, but this unit runs HCCast casting firmware. If the listing promised Android or apps, it is not as described.
  ! The Wi-Fi Direct password is a factory default ('12345678'). Anyone nearby can connect and cast to it.
```

## Install

```bash
pip install git+https://github.com/heditrigui/hccast-tools
```

Pure Python 3.9+, no dependencies, works on Windows, macOS and Linux.

To play YouTube and other web video pages, also install [yt-dlp](https://github.com/yt-dlp/yt-dlp) (optional):

```bash
pip install "hccast-tools[web] @ git+https://github.com/heditrigui/hccast-tools"
```

> **Connect the projector to your Wi-Fi first** (in its own Network settings menu). Win+K / Miracast alone uses a direct link that this tool can't reach.

## Usage

```bash
hccast discover                       # list HCCast and other DLNA devices on the network
hccast info                           # what is this device, really?
hccast info --json                    # raw settings it reports
hccast audit                          # security check (exit code 1 if problems)

hccast cast movie.mp4                 # stream a local file (keeps a tiny file server running)
hccast cast ./Holiday                 # play a folder in name order: videos, music and photos
hccast cast ./Photos --slideshow 8s --shuffle --loop
hccast cast https://example.com/v.mp4 # play a direct media link
hccast cast "https://www.youtube.com/watch?v=..."   # web video page (needs yt-dlp)
hccast control pause                  # play | pause | stop | status | seek 0:05:30
hccast volume 40                      # set volume; `hccast volume` shows it

hccast restart                        # reboot (asks first; -y to skip)
hccast set-password                   # explains how to change the Wi-Fi Direct password

hccast gui                            # control page in your browser
```

If there is exactly one HCCast device on the network, you can leave out its IP. Otherwise pass it: `hccast info 192.168.1.50`.

### Folders, playlists and slideshows

`hccast cast <folder>` plays the folder's video, music and photo files (not subfolders) in name order. Each video or song plays to the end before the next starts; each photo stays up for `--slideshow` (default `5s`, also `2m`). Add `--shuffle` for random order and `--loop` to start over after the last file.

### Web video links

A link that ends in a media file (`.mp4`, `.mp3`, `.m3u8`, …) is sent to the projector as it is. Any other link (YouTube, Vimeo, news sites…) is handed to yt-dlp, if installed, to find the video's direct stream, which the projector then plays. Only formats with sound and picture in one stream work, so the quality is often limited to 720p. Use `--direct` to skip yt-dlp for a link without a file extension.

### Control page

`hccast gui` opens a page in your browser to find devices, cast a file, folder or link (with *File…* / *Folder…* pickers), control playback and volume, and see the device info. It runs only on this computer (`127.0.0.1`) and uses no extra packages. Keep the window open while files are playing.

### Wi-Fi Direct password

HCCast's web page shows the password but has no call to change it, so `hccast set-password` cannot change it. It checks the device's own page for such a call (only reading it, never guessing) and otherwise tells you to change the password from the projector's on-screen settings with the remote.

## Is my projector "fake"?

Run `hccast info`. If **Platform** says *HCCast* and **Runs Android** says *no*, the device cannot install apps, whatever the box says. If you were sold an Android projector, the output (plus a photo of its home screen showing only Miracast / AirPlay / DLNA / HDMI / USB) is useful evidence for a "not as described" return.

Want apps anyway? Plug a streaming stick or Android TV box into the projector's HDMI port.

## How it works

| Feature | Protocol |
|---|---|
| Discovery | SSDP multicast (`M-SEARCH` for `MediaRenderer`), identifies HCCast by its `SERVER` header |
| Info / audit / restart | The device's built-in web server: `GET /setting_get`, `GET /misc_submit?type=restart` (the same calls its own settings page makes) |
| Password check | Reads the device's own web page and scripts and lists the calls they make; sends none of them |
| Web links | yt-dlp (optional) finds a single-file stream URL; the projector fetches it directly |
| Control page | `http.server` on `127.0.0.1` with a per-run token, so other web pages can't drive it |
| Casting and control | UPnP AVTransport + RenderingControl (SOAP) |
| Local files | A temporary HTTP server with byte-range support, bound to your LAN address and only answering the projector |

No firmware is modified and nothing is installed on the device.

## Tested with

- Magcubic-branded "HY300PRO_MAG", HCCast v3.8 firmware `2406150908`

Got another HCCast device? Please open an issue with the output of `hccast info --json` (remove the MAC and password) so support can be added.

## Development

```bash
pip install -e ".[dev]"
pytest          # uses a local fake HCCast device, no hardware needed
```

## Disclaimer

Not affiliated with HiChip, Magcubic or any projector brand. Only use it on devices you own.

## License

MIT
