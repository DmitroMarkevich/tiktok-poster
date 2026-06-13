from __future__ import annotations
import random
from dataclasses import dataclass


_SCREENS = [
    (1920, 1080, 1040),
    (1366, 768,  728),
    (1440, 900,  860),
    (1536, 864,  824),
    (1280, 800,  760),
    (1680, 1050, 1010),
    (2560, 1440, 1400),
]

_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Los_Angeles", "America/Denver",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Warsaw", "Europe/Madrid",
    "Asia/Tokyo", "Asia/Seoul", "Asia/Bangkok", "Asia/Singapore",
]

_WEBGL = [
    ("Intel Inc.",         "Intel Iris OpenGL Engine"),
    ("Intel Inc.",         "Intel(R) UHD Graphics 620"),
    ("Intel Inc.",         "Intel(R) HD Graphics 630"),
    ("NVIDIA Corporation", "NVIDIA GeForce GTX 1060/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 3060/PCIe/SSE2"),
    ("AMD",                "AMD Radeon RX 580"),
    ("Google Inc. (Intel)","ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)"),
]

_HW_CONCURRENCY = [4, 6, 8, 10, 12, 16]
_DEVICE_MEMORY  = [4, 8, 16]
_CHROME_VERS    = ["134", "135", "136"]


@dataclass
class Fingerprint:
    ua: str
    platform: str
    screen_w: int
    screen_h: int
    screen_avail_h: int
    timezone: str
    locale: str
    hardware_concurrency: int
    device_memory: int
    webgl_vendor: str
    webgl_renderer: str
    languages: list[str]


def get_fingerprint(account_id: int) -> Fingerprint:
    rng = random.Random(account_id * 0xDEADBEEF)

    sw, sh, sah = rng.choice(_SCREENS)
    tz   = rng.choice(_TIMEZONES)
    wv, wr = rng.choice(_WEBGL)
    hw   = rng.choice(_HW_CONCURRENCY)
    mem  = rng.choice(_DEVICE_MEMORY)
    ver  = rng.choice(_CHROME_VERS)

    # Derive locale + platform from timezone region
    if tz.startswith("America"):
        locale, platform, lang = "en-US", "Win32",         ["en-US", "en"]
    elif tz.startswith("Europe/London"):
        locale, platform, lang = "en-GB", "MacIntel",      ["en-GB", "en"]
    elif tz.startswith("Europe"):
        choices = [
            ("de-DE", "Win32",    ["de-DE", "de", "en-US"]),
            ("fr-FR", "Win32",    ["fr-FR", "fr", "en-US"]),
            ("pl-PL", "Win32",    ["pl-PL", "pl", "en-US"]),
            ("es-ES", "Win32",    ["es-ES", "es", "en-US"]),
        ]
        locale, platform, lang = rng.choice(choices)
    else:
        locale, platform, lang = "en-US", "MacIntel",      ["en-US", "en"]

    if platform == "MacIntel":
        ua = (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36")
    elif platform == "Win32":
        ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36")
    else:
        ua = (f"Mozilla/5.0 (X11; Linux x86_64) "
              f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36")

    return Fingerprint(
        ua=ua, platform=platform,
        screen_w=sw, screen_h=sh, screen_avail_h=sah,
        timezone=tz, locale=locale,
        hardware_concurrency=hw, device_memory=mem,
        webgl_vendor=wv, webgl_renderer=wr,
        languages=lang,
    )
