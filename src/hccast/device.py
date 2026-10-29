"""Talk to an HCCast device's built-in web server (port 80)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_PASSWORDS = {"12345678", "88888888", "00000000", "11111111"}

# Model names that are normally sold as Android projectors. Finding one of these names on
# HCCast (Linux casting) firmware usually means the unit is not the Android version advertised.
ANDROID_MODEL_HINTS = ("HY300PRO", "HY300 PRO", "HY320", "HY350", "HY310", "HY330")


class DeviceError(RuntimeError):
    pass


def _get(ip: str, path: str, timeout: float = 5.0) -> bytes:
    url = f"http://{ip}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={"User-Agent": "hccast-tools"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise DeviceError(f"Could not reach {url}: {exc}") from exc


def server_header(ip: str, timeout: float = 3.0) -> str:
    req = urllib.request.Request(f"http://{ip}/", headers={"User-Agent": "hccast-tools"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.headers.get("Server", "")
    except (urllib.error.URLError, OSError):
        return ""


def settings(ip: str, timeout: float = 5.0) -> dict:
    raw = _get(ip, "setting_get", timeout)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise DeviceError(f"{ip} answered, but not with HCCast settings JSON.") from exc


@dataclass
class Report:
    product: str = ""
    version: str = ""
    vendor: str = ""
    name: str = ""
    mac: str = ""
    firmware: str = ""
    runs_android: bool = False
    verdict: str = ""
    warnings: list[str] = field(default_factory=list)


def firmware_family(data: dict) -> str:
    """Infer the firmware family from the update URL and fields the device reports."""
    hint = " ".join(str(data.get(k, "")) for k in ("upgrade_api", "upgrade_server", "product")).lower()
    if "linux" in hint:
        return "HCCast on Linux (hcprojector_linux)"
    if "rtos" in hint:
        return "HCCast on RTOS"
    if str(data.get("vendor", "")).lower() == "hichip":
        return "HCCast (HiChip)"
    return "unknown"


def analyze(data: dict) -> Report:
    r = Report(
        product=str(data.get("product", "")),
        version=str(data.get("version", "")),
        vendor=str(data.get("vendor", "")),
        name=str(data.get("device_name", "")),
        mac=str(data.get("mac", "")),
        firmware=firmware_family(data),
    )
    product_key = r.product.upper().replace("_", "").replace("-", "")
    sold_as_android = any(h.replace(" ", "") in product_key for h in ANDROID_MODEL_HINTS)

    r.verdict = (
        "This is an HCCast screen-mirroring device. It receives Miracast, AirPlay and DLNA, "
        "but it does not run Android and cannot install apps."
    )
    if sold_as_android:
        r.warnings.append(
            f"The product name '{r.product}' matches a model usually sold with Android, "
            "but this unit runs HCCast casting firmware. If the listing promised Android or apps, "
            "it is not as described."
        )
    if str(data.get("device_password", "")) in DEFAULT_PASSWORDS:
        r.warnings.append(
            "The Wi-Fi Direct password is a factory default "
            f"('{data['device_password']}'). Anyone nearby can connect and cast to it."
        )
    if "device_password" in data:
        r.warnings.append(
            "The settings page shows the Wi-Fi Direct password to anyone on your network, without a login."
        )
    return r


def restart(ip: str) -> None:
    """Reboot the device (the same call its own settings page makes)."""
    _get(ip, "misc_submit?type=restart")
