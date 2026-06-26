from __future__ import annotations
import functools
import os
import platform as _platform
import re
import random
import shutil
import subprocess
from dataclasses import dataclass


@functools.lru_cache(maxsize=1)
def host_platform() -> str | None:
    """The navigator.platform value patchright will PIN to the real host OS.

    patchright (the stealth Playwright fork we launch) deliberately keeps navigator.platform,
    navigator.userAgentData.platform and the WebGL UNMASKED_* strings consistent with the real
    browser/OS, and it re-applies that AFTER our add_init_script — so our JS spoof of these
    OS-tied fields is silently overwritten with the host value. Spoofing a Windows UA on a Mac
    host therefore yields navigator.platform "MacIntel" ↔ UA "Windows", an instant cross-check
    tell. We can't hide the OS under patchright, so we MATCH it: lock the fingerprint's
    platform/UA/WebGL to the host. Returns None on an unrecognised OS (caller keeps its pick)."""
    sysname = _platform.system()
    if sysname == "Darwin":
        return "MacIntel"
    if sysname == "Windows":
        return "Win32"
    if sysname == "Linux":
        return "Linux x86_64"
    return None


@functools.lru_cache(maxsize=1)
def real_chrome_version() -> str | None:
    """Major version of the actual Chrome binary Playwright launches (channel='chrome').
    The spoofed UA MUST match it: navigator.userAgent is overridable, but the binary's real
    H2/TLS stack and internal build version are not — a UA a few versions off the real binary
    is a cross-check tell. Cached; returns None if Chrome can't be located."""
    candidates = [
        shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
        shutil.which("chromium"), shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
    ]
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            out = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=5)
            m = re.search(r"(\d+)\.", out.stdout or "")
            if m:
                return m.group(1)
        except Exception:
            continue
    return None


# Screen pools MUST be platform-specific. A Mac never reports a Windows-scaled resolution
# like 1536x864 / 1366x768 — fv.pro (and others) flag "screen is not real" when navigator
# says macOS but screen.width/height is a Windows value. availHeight leaves room for the
# macOS menu bar (~25px) + Dock, or the Windows taskbar (~40px).
_SCREENS_WIN = [
    (1920, 1080, 1040),
    (1366, 768,  728),
    (1536, 864,  824),
    (1280, 720,  680),
    (1600, 900,  860),
    (2560, 1440, 1400),
]
# Real Apple-silicon / retina Mac logical resolutions (points, what window.screen reports).
_SCREENS_MAC = [
    (1440, 900,  870),    # MacBook Air/Pro 13" scaled
    (1512, 982,  952),    # MacBook Air 13" M2/M3 native points
    (1680, 1050, 1020),   # 13" "more space" scaling
    (1728, 1117, 1087),   # 16" MacBook Pro native points
    (2560, 1440, 1400),   # external QHD
]

_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Los_Angeles", "America/Denver",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Warsaw", "Europe/Madrid",
    "Asia/Tokyo", "Asia/Seoul", "Asia/Bangkok", "Asia/Singapore",
]

# WebGL vendor/renderer MUST be consistent with the OS. A Direct3D11 ANGLE string on a Mac
# (or an Apple Metal string on Windows) is physically impossible and an instant bot tell, so
# the pool is keyed by platform. Modern Chrome reports the ANGLE-wrapped string for the real
# GPU; these mirror what genuine Win/Mac clients expose.
_WEBGL_BY_OS = {
    "Win32": [
        ("Google Inc. (Intel)",  "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (Intel)",  "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (AMD)",    "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ],
    "MacIntel": [
        ("Google Inc. (Apple)",  "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"),
        ("Google Inc. (Apple)",  "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)"),
        ("Google Inc. (Intel)",  "ANGLE (Intel Inc., Intel(R) Iris(TM) Plus Graphics OpenGL Engine, OpenGL 4.1)"),
    ],
    "Linux x86_64": [
        ("Google Inc. (Intel)",  "ANGLE (Intel, Mesa Intel(R) UHD Graphics (CML GT2), OpenGL 4.6)"),
        ("Google Inc. (AMD)",    "ANGLE (AMD, AMD Radeon RX 580 Series (polaris10, LLVM 15.0.7), OpenGL 4.6)"),
    ],
}

_HW_CONCURRENCY = [4, 6, 8, 10, 12, 16]
# Keep the Chrome major version close to the real stable channel — an outdated UA (a year
# behind reality) is a cluster tell in itself. Bump this a few times a year; UA, Sec-CH-UA
# and userAgentData.fullVersionList all derive from it, so this one constant drives them all.
_CHROME_VERS    = ["149", "150", "151"]


def _device_memory_for(cores: int, rng: random.Random) -> int:
    """Pick a RAM size plausible for the core count (Chrome clamps deviceMemory to 8, so the
    only realistic desktop values are 4 and 8). Choosing cores and RAM independently produced
    absurd combos like 16 cores + 4 GB; a 4-core box may be 4 or 8 GB, anything bigger is 8."""
    return rng.choice([4, 8]) if cores <= 4 else 8

# Proxy exit-country → (locale, timezone, navigator.languages). Used to align the
# fingerprint with the proxy's geography (a UA proxy looks like a UA user). Unknown
# countries fall back to the timezone-derived default below.
_COUNTRY = {
    "UA": ("uk-UA", "Europe/Kyiv",    ["uk-UA", "uk", "en-US"]),
    "RU": ("ru-RU", "Europe/Moscow",  ["ru-RU", "ru", "en-US"]),
    "PL": ("pl-PL", "Europe/Warsaw",  ["pl-PL", "pl", "en-US"]),
    "DE": ("de-DE", "Europe/Berlin",  ["de-DE", "de", "en-US"]),
    "FR": ("fr-FR", "Europe/Paris",   ["fr-FR", "fr", "en-US"]),
    "ES": ("es-ES", "Europe/Madrid",  ["es-ES", "es", "en-US"]),
    "GB": ("en-GB", "Europe/London",  ["en-GB", "en"]),
    "US": ("en-US", "America/New_York", ["en-US", "en"]),
    "CA": ("en-CA", "America/Toronto", ["en-CA", "en", "fr-CA"]),
    "NL": ("nl-NL", "Europe/Amsterdam", ["nl-NL", "nl", "en-US"]),
    "IT": ("it-IT", "Europe/Rome",    ["it-IT", "it", "en-US"]),
    "CZ": ("cs-CZ", "Europe/Prague",  ["cs-CZ", "cs", "en-US"]),
    "KZ": ("ru-RU", "Asia/Almaty",    ["ru-RU", "ru", "kk", "en-US"]),
    "TR": ("tr-TR", "Europe/Istanbul", ["tr-TR", "tr", "en-US"]),
}


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
    chrome_version: str          # major version used in the UA (e.g. "135")
    canvas_seed: int             # deterministic per-account canvas/WebGL noise seed

    @property
    def ua_platform(self) -> str:
        """The `Sec-CH-UA-Platform` value matching `platform` (Client-Hints must agree
        with navigator.platform, or the JS↔HTTP mismatch is a clear bot tell)."""
        return {"Win32": "Windows", "MacIntel": "macOS"}.get(self.platform, "Linux")

    def sec_ch_ua(self) -> dict:
        """Client-Hints request headers consistent with this fingerprint's Chrome
        version + platform. Chrome sends these on every navigation; if they disagree
        with the spoofed navigator.userAgent/platform, TikTok flags the mismatch."""
        v = self.chrome_version
        brands = (
            f'"Chromium";v="{v}", "Google Chrome";v="{v}", "Not=A?Brand";v="99"'
        )
        return {
            "Sec-CH-UA": brands,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": f'"{self.ua_platform}"',
        }


def get_fingerprint(account_id: int, country: str | None = None) -> Fingerprint:
    rng = random.Random(account_id * 0xDEADBEEF)

    tz   = rng.choice(_TIMEZONES)
    hw   = rng.choice(_HW_CONCURRENCY)
    mem  = _device_memory_for(hw, rng)
    # Pin the UA version to the REAL installed Chrome (same binary for every account, so they
    # all share the true version) — fall back to the static pool only if detection fails.
    ver  = real_chrome_version() or rng.choice(_CHROME_VERS)

    cc = (country or "").upper()
    if cc in _COUNTRY:
        # Proxy country known → match locale/timezone/languages to it. Platform is still
        # per-account deterministic (weighted to Windows, the common TikTok-web client).
        locale, tz, lang = _COUNTRY[cc]
        platform = rng.choice(["Win32", "Win32", "Win32", "MacIntel"])
    # Otherwise derive locale + platform from the (random) timezone region as before.
    elif tz.startswith("America"):
        locale, platform, lang = "en-US", "Win32",         ["en-US", "en"]
    elif tz.startswith("Europe/London"):
        locale, platform, lang = "en-GB", "MacIntel",      ["en-GB", "en"]
    elif tz.startswith("Europe"):
        # locale MUST match the SPECIFIC timezone, not be picked at random — es-ES on
        # Europe/Warsaw (Spanish locale, Polish tz) is a contradiction CreepJS/amiunique flag.
        _tz_locale = {
            "Europe/Paris":  ("fr-FR", ["fr-FR", "fr", "en-US"]),
            "Europe/Berlin": ("de-DE", ["de-DE", "de", "en-US"]),
            "Europe/Warsaw": ("pl-PL", ["pl-PL", "pl", "en-US"]),
            "Europe/Madrid": ("es-ES", ["es-ES", "es", "en-US"]),
        }
        locale, lang = _tz_locale.get(tz, ("en-GB", ["en-GB", "en"]))
        platform = "Win32"
    else:
        locale, platform, lang = "en-US", "MacIntel",      ["en-US", "en"]

    # patchright pins navigator.platform/userAgentData/WebGL to the REAL host OS and overrides
    # our spoof, so an OS that differs from the host is a guaranteed cross-check tell. Lock the
    # OS-tied fields (platform → UA → WebGL below) to the host. Geo fields (locale/timezone/
    # languages) are NOT pinned by patchright, so they stay matched to the proxy country above.
    _host = host_platform()
    if _host:
        platform = _host

    # Screen pool keyed by the FINAL platform — a macOS navigator with a Windows-only
    # resolution (1536x864) is flagged "screen is not real". Picked here, after platform
    # is pinned to the host, so a Mac host always gets a Mac-plausible resolution.
    _screen_pool = _SCREENS_MAC if platform == "MacIntel" else _SCREENS_WIN
    sw, sh, sah = rng.choice(_screen_pool)

    # Pick a GPU consistent with the chosen OS (see _WEBGL_BY_OS).
    wv, wr = rng.choice(_WEBGL_BY_OS.get(platform, _WEBGL_BY_OS["Win32"]))

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
        chrome_version=ver,
        # Stable per-account noise seed so the canvas/WebGL hash is consistent across
        # this account's sessions but differs between accounts (a shared identical hash
        # across accounts is itself a cluster tell).
        canvas_seed=(account_id * 2654435761) & 0xFFFFFFFF,
    )
