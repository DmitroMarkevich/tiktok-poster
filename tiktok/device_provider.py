"""Antidetect-browser device providers.

patchright on a single host pins GPU/platform/WebGL to the real machine and shares one TLS/JA3
across every account, so all accounts look like ONE device and TikTok's register_verify_login
throttles with error_code 7 no matter the proxy (proven 2026-06-21 across datacenter, residential
and mobile IPs — see memory project_error7_is_device_not_ip).

A real antidetect browser injects a DISTINCT, self-consistent device fingerprint per profile at
the C++/kernel level (canvas/WebGL/audio/fonts/WebRTC all differ AND pass pixelscan). We drive it
over its Local API: create a profile bound to the proxy, START it (it returns a CDP websocket),
and connect Playwright/patchright via `connect_over_cdp`. The signup/reader automation is unchanged
because they just receive a ready Page. Each profile = a genuinely different device.

Free-tier conveyor: free plans cap CONCURRENT profiles, not total — create → register → save
session → delete the profile → repeat. close_device(delete=True) does that teardown each cycle.
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

from config import (DEVICE_PROVIDER, ADSPOWER_API, ADSPOWER_GROUP_ID,
                    DOLPHIN_API, DOLPHIN_TOKEN)

logger = logging.getLogger(__name__)


@dataclass
class DeviceHandle:
    provider: "DeviceProvider"
    profile_id: str
    cdp_endpoint: str          # ws:// endpoint for playwright.chromium.connect_over_cdp
    name: str = ""             # profile name we assigned (tt_<id>) — delete-safety guard
    context: object = None     # for providers that hand back a ready context (Camoufox), not CDP
    extra: object = None       # provider-private teardown handle (e.g. the Camoufox manager)


def _os_for_profile(account_id: int, country: str | None) -> list[str]:
    """Pick ONE self-consistent OS for this profile (never a Mac+Win mix in one random_ua —
    that risks a platform/screen-resolution contradiction, the tell pixelscan flagged). The OS
    is chosen deterministically from account_id so the SAME account always re-opens on the same
    OS, and weighted by region: Windows dominates desktop web globally, macOS skews to US/CA/UK/AU.
    """
    import random as _r
    rng = _r.Random(f"os-{account_id}")
    mac_heavy = {"US", "CA", "GB", "AU", "DE", "NL", "SE", "DK", "NO", "CH"}
    mac_share = 0.35 if (country or "").upper() in mac_heavy else 0.12
    if rng.random() < mac_share:
        return ["Mac OS X"]
    return ["Windows 10", "Windows 11"]  # both Windows → consistent platform, just version spread


def _proxy_parts(proxy: str | None) -> dict | None:
    """`host:port:user:pass` / url → dict the antidetect APIs expect, or None for no proxy."""
    if not proxy:
        return None
    from tiktok.browser import normalize_proxy
    p = urlparse(normalize_proxy(proxy))
    return {
        "host": p.hostname or "",
        "port": str(p.port or ""),
        "user": p.username or "",
        "pwd":  p.password or "",
    }


class DeviceProvider:
    async def open_device(self, account_id: int, proxy: str | None,
                          country: str | None = None) -> DeviceHandle: ...
    async def close_device(self, handle: DeviceHandle, delete: bool = True) -> None: ...


class AdsPowerProvider:
    """AdsPower Local API. Requires the AdsPower desktop app running with the Local API enabled
    (Settings → Local API). Default endpoint http://local.adspower.net:50325. The Local API is
    rate-limited to ~1 request/second, so we serialise calls with a small delay."""

    def __init__(self):
        self.base = ADSPOWER_API.rstrip("/")
        self._lock = asyncio.Lock()

    async def _req(self, session, method, path, **kw):
        # AdsPower throttles to 1 req/s — hold a lock + delay so we never get "Too many requests".
        async with self._lock:
            await asyncio.sleep(1.1)
            async with session.request(method, f"{self.base}{path}", **kw) as r:
                data = await r.json(content_type=None)
        if data.get("code") != 0:
            raise RuntimeError(f"AdsPower {path} failed: {data.get('msg')} | {data}")
        return data.get("data", {})

    async def open_device(self, account_id, proxy, country=None) -> DeviceHandle:
        pp = _proxy_parts(proxy)
        if pp:
            proxy_cfg = {
                "proxy_soft": "other", "proxy_type": "http",
                "proxy_host": pp["host"], "proxy_port": pp["port"],
                "proxy_user": pp["user"], "proxy_password": pp["pwd"],
            }
        else:
            proxy_cfg = {"proxy_soft": "no_proxy"}

        # A fresh, RANDOM, self-consistent device per profile. automatic_timezone derives the tz
        # from the proxy IP (so it matches geo); canvas/webgl/audio/webrtc noise make the hardware
        # fingerprint distinct yet consistent — the whole point vs our JS spoof.
        fp_cfg = {
            "automatic_timezone": "1",
            "webrtc": "proxy",
            "canvas": "1",
            "webgl_image": "1",
            "webgl": "3",
            "audio": "1",
            "media_devices": "1",
            "client_rects": "1",
            "random_ua": {"ua_browser": ["chrome"], "ua_system_version": _os_for_profile(account_id, country)},
        }
        body = {
            "name": f"tt_{account_id}",
            "group_id": ADSPOWER_GROUP_ID,
            "user_proxy_config": proxy_cfg,
            "fingerprint_config": fp_cfg,
        }
        async with aiohttp.ClientSession() as session:
            created = await self._req(session, "POST", "/api/v1/user/create", json=body)
            profile_id = created["id"]
            logger.info("AdsPower profile created: %s (acct %s)", profile_id, account_id)
            try:
                started = await self._req(session, "GET", "/api/v1/browser/start",
                                          params={"user_id": profile_id, "headless": "0", "open_tabs": "0"})
            except Exception:
                # start failed (e.g. free-tier daily open limit) — the profile was already
                # created, so delete it now or it leaks as an orphan tt_ profile.
                try:
                    await self._req(session, "POST", "/api/v1/user/delete",
                                    json={"user_ids": [profile_id]})
                    logger.warning("AdsPower start failed — deleted orphan profile %s", profile_id)
                except Exception as de:
                    logger.warning("AdsPower start failed AND orphan cleanup failed for %s: %s",
                                   profile_id, de)
                raise
        ws = (started.get("ws") or {}).get("puppeteer")
        if not ws:
            raise RuntimeError(f"AdsPower start returned no CDP ws endpoint: {started}")
        logger.info("AdsPower profile %s started, CDP=%s", profile_id, ws)
        return DeviceHandle(provider=self, profile_id=profile_id, cdp_endpoint=ws,
                            name=body["name"])

    async def close_device(self, handle: DeviceHandle, delete: bool = True) -> None:
        # SAFETY: only ever delete throwaway profiles WE created (named tt_<id>). Never touch the
        # user's own profiles (per feedback_no_autodelete_adspower).
        if delete and not handle.name.startswith("tt_"):
            logger.warning("Refusing to delete non-tt_ profile %s (%s) — stop only",
                           handle.profile_id, handle.name)
            delete = False
        async with aiohttp.ClientSession() as session:
            try:
                await self._req(session, "GET", "/api/v1/browser/stop",
                                params={"user_id": handle.profile_id})
            except Exception as e:
                logger.warning("AdsPower stop %s: %s", handle.profile_id, e)
            if delete:
                try:
                    await self._req(session, "POST", "/api/v1/user/delete",
                                    json={"user_ids": [handle.profile_id]})
                    logger.info("AdsPower profile deleted: %s", handle.profile_id)
                except Exception as e:
                    logger.warning("AdsPower delete %s: %s", handle.profile_id, e)


class DolphinProvider:
    """Dolphin{anty} Local API (desktop app running, default port 3001). Profiles are created via
    the remote API (api.dolphin-anty.com) then started locally; for a minimal local-only setup we
    create+start through the local automation endpoint. Token from DOLPHIN_TOKEN."""

    def __init__(self):
        self.base = DOLPHIN_API.rstrip("/")
        self.remote = "https://dolphin-anty-api.com"
        self.token = DOLPHIN_TOKEN

    async def open_device(self, account_id, proxy, country=None) -> DeviceHandle:
        headers = {"Authorization": f"Bearer {self.token}"}
        pp = _proxy_parts(proxy)
        payload = {
            "name": f"tt_{account_id}",
            "platform": "macos",
            "browserType": "anty",
            "mainWebsite": "tiktok",
            "useragent": {"mode": "manual"} if False else {"mode": "random"},
            "webrtc": {"mode": "altered"},
            "canvas": {"mode": "real"},
            "webgl": {"mode": "real"},
            "webglInfo": {"mode": "automatic"},
            "timezone": {"mode": "auto"},
            "locale": {"mode": "auto"},
        }
        if pp:
            payload["proxy"] = {"type": "http", "host": pp["host"], "port": int(pp["port"]),
                                "login": pp["user"], "password": pp["pwd"]}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.remote}/browser_profiles", json=payload, headers=headers) as r:
                created = await r.json(content_type=None)
            profile_id = str((created.get("browserProfileId") or created.get("data", {}).get("id")))
            # Start locally → returns automation port + ws endpoint
            async with session.get(f"{self.base}/v1.0/browser_profiles/{profile_id}/start",
                                   params={"automation": "1"}) as r:
                started = await r.json(content_type=None)
        auto = started.get("automation", {})
        port = auto.get("port"); ws_id = auto.get("wsEndpoint")
        if not port:
            raise RuntimeError(f"Dolphin start returned no automation port: {started}")
        ws = f"ws://127.0.0.1:{port}{ws_id if ws_id else ''}"
        logger.info("Dolphin profile %s started, CDP=%s", profile_id, ws)
        return DeviceHandle(provider=self, profile_id=profile_id, cdp_endpoint=ws)

    async def close_device(self, handle: DeviceHandle, delete: bool = True) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with aiohttp.ClientSession() as session:
            try:
                await session.get(f"{self.base}/v1.0/browser_profiles/{handle.profile_id}/stop")
            except Exception as e:
                logger.warning("Dolphin stop %s: %s", handle.profile_id, e)
            if delete:
                try:
                    await session.delete(f"{self.remote}/browser_profiles/{handle.profile_id}", headers=headers)
                    logger.info("Dolphin profile deleted: %s", handle.profile_id)
                except Exception as e:
                    logger.warning("Dolphin delete %s: %s", handle.profile_id, e)


class CamoufoxProvider:
    """Camoufox — a FREE, open-source, fully-local antidetect browser (patched Firefox). No
    account, no daily limit (unlike AdsPower free tier). It injects a distinct, self-consistent
    fingerprint per launch at the engine level (canvas/WebGL/fonts/screen/navigator/AudioContext)
    and, with geoip=True, derives timezone/locale/geolocation from the proxy's exit IP. Because it
    spoofs at the C++ level there's no JS-stealth tell — we apply NO apply_stealth (the _is_provider
    path). Unlike AdsPower/Dolphin it hands back a ready Playwright context directly (not CDP), so
    open_device sets handle.context and there's no profile to create/delete (conveyor is implicit:
    every launch is a fresh throwaway device)."""

    def __init__(self):
        self._seq = 0
        self._geoip_warned = False

    def _pick_headless(self):
        """Choose the launch mode that has the FEWEST detection tells on THIS host.

        PerimeterX/HUMAN (Outlook's captcha) scores the render backend + rAF/compositor
        timings hardest of all detectors, and headless Firefox — even Camoufox — still
        leaks those. So we avoid plain headless whenever possible:
          • Linux  → headless="virtual": Camoufox spins up its own Xvfb and runs a REAL
            headful Firefox inside it (real GL render path, no headless tells, no visible
            window). This is the single biggest Camoufox-specific lever for Outlook.
          • macOS/Windows → real headful (visible window); "virtual" needs Xvfb (Linux only).
          • Only if the operator explicitly forces HEADLESS=1 on a non-Linux box do we fall
            back to true headless (last resort — expect a lower PerimeterX pass rate).
        """
        import sys
        from config import HEADLESS
        if not HEADLESS:
            return False                      # operator wants a visible browser → headful
        if sys.platform.startswith("linux"):
            return "virtual"                  # headful-in-Xvfb: no window, no headless tells
        return False                          # Mac/Win: prefer headful over tell-heavy headless

    def _geoip_arg(self, proxy) -> bool:
        """geoip=True aligns tz/geolocation with the proxy EXIT IP — but it silently no-ops
        (leaving a tz that contradicts the IP, a PerimeterX tell) if the geoip DB was never
        fetched. Warn ONCE so a missing `camoufox fetch` is visible instead of mysterious
        blocks. NB use camoufox.locale.MMDB_FILE — the same path Camoufox reads at runtime
        (camoufox.pkgman.get_path points at the .app bundle, NOT where fetch stores the DB)."""
        if not proxy:
            return False
        try:
            from camoufox.locale import MMDB_FILE
            if not MMDB_FILE.exists() and not self._geoip_warned:
                self._geoip_warned = True
                logger.warning("Camoufox geoip DB missing — run `camoufox fetch`. "
                               "tz will NOT match the proxy IP (PerimeterX tell).")
        except Exception:
            pass
        return True

    # Statistically plausible desktop locale per country (language[,fallback]-REGION). We set
    # this EXPLICITLY instead of trusting Camoufox's geoip locale, which derives the language
    # from the IP block's *registered_country* — a UA residential IP registered to a Hong-Kong
    # holding company then gets `yue-HK` (Cantonese): the Outlook UI loads in Chinese (tofu
    # glyphs, no CJK fonts), text selectors miss, AND a UA-geo browser speaking Cantonese is a
    # blatant geo↔locale inconsistency PerimeterX scores against. The explicit locale below
    # matches the ACTUAL exit country (from utils.geo.country_for_proxy) so tz/geo (kept from
    # geoip) and language agree.
    _COUNTRY_LOCALE = {
        "UA": "uk-UA,ru-UA", "RU": "ru-RU", "BY": "ru-BY", "KZ": "ru-KZ",
        "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU", "IE": "en-IE",
        "DE": "de-DE", "AT": "de-AT", "CH": "de-CH", "FR": "fr-FR", "NL": "nl-NL",
        "PL": "pl-PL", "ES": "es-ES", "IT": "it-IT", "PT": "pt-PT", "SE": "sv-SE",
        "NO": "nb-NO", "DK": "da-DK", "FI": "fi-FI", "CZ": "cs-CZ", "RO": "ro-RO",
    }

    def _locale_for_country(self, country: str | None) -> str | None:
        if not country:
            return None
        return self._COUNTRY_LOCALE.get(country.upper(), "en-US")

    async def open_device(self, account_id, proxy, country=None) -> DeviceHandle:
        from camoufox.async_api import AsyncCamoufox

        launch: dict = {
            "headless": self._pick_headless(),
            # geoip aligns tz/locale/geo with the proxy exit; without a proxy it uses the real IP.
            "geoip": self._geoip_arg(proxy),
            # OS is chosen consistently per launch; bias by region like the AdsPower path.
            "os": ("macos" if _os_for_profile(account_id, country) == ["Mac OS X"] else "windows"),
            # we drive our own human pacing (utils.humanize) — don't double up Camoufox's cursor sim.
            "humanize": False,
            # WebRTC can leak the host's real IP past the HTTP proxy via JS (one real IP shared
            # across every "distinct" device = an instant multi-account cluster signal). Block it
            # at the engine level rather than relying only on proxy-mode ICE.
            "block_webrtc": True,
            # Pin a realistic desktop window so innerWidth/Height stay self-consistent with the
            # spoofed screen (Camoufox derives a matching screen); avoids the tiny-default-window
            # geometry that looks automated.
            "window": (1366, 768),
            # Re-use the persisted browser cache across the run so a freshly-"installed" Firefox
            # with an empty HTTP cache (a cold-visitor tell) isn't the norm for every launch.
            "enable_cache": True,
        }
        # Override Camoufox's registered_country-derived locale with one matching the ACTUAL
        # exit country (keeps tz/geo from geoip, fixes the yue-HK-on-a-UA-IP tofu bug).
        loc = self._locale_for_country(country)
        if loc:
            launch["locale"] = loc
        pp = _proxy_parts(proxy)
        if pp:
            launch["proxy"] = {
                "server": f"http://{pp['host']}:{pp['port']}",
                "username": pp["user"], "password": pp["pwd"],
            }
        cam = AsyncCamoufox(**launch)
        browser = await cam.__aenter__()
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        self._seq += 1
        logger.info("Camoufox device launched (acct %s, os=%s, proxy=%s)",
                    account_id, launch["os"], bool(pp))
        return DeviceHandle(provider=self, profile_id=f"camoufox_{account_id}_{self._seq}",
                            cdp_endpoint="", name="", context=context, extra=cam)

    async def close_device(self, handle: DeviceHandle, delete: bool = True) -> None:
        cam = handle.extra
        if cam is None:
            return
        try:
            await cam.__aexit__(None, None, None)
            logger.info("Camoufox device closed: %s", handle.profile_id)
        except Exception as e:
            logger.warning("Camoufox close %s: %s", handle.profile_id, e)


async def open_provider_context(provider, pw, account_id: int, proxy: str | None, country):
    """Open a device on `provider` and return (context, handle), hiding the two shapes a provider
    can take: a ready context (Camoufox) or a CDP endpoint to attach to (AdsPower/Dolphin). `pw` is
    an already-started Playwright used for the CDP attach; it is unused for context-style providers.
    """
    handle = await provider.open_device(account_id, proxy, country)
    if handle.context is not None:
        return handle.context, handle
    browser = await pw.chromium.connect_over_cdp(handle.cdp_endpoint)
    handle.extra = browser  # keep a ref so callers can close the CDP browser if they want
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    return context, handle


_PROVIDERS: dict[str, DeviceProvider] = {}


def get_provider(name: str | None = None) -> DeviceProvider | None:
    """Return the configured antidetect provider, or None for plain patchright (single device)."""
    name = (name or DEVICE_PROVIDER).lower()
    if name in ("patchright", "", "none"):
        return None
    if name not in _PROVIDERS:
        if name == "adspower":
            _PROVIDERS[name] = AdsPowerProvider()
        elif name == "dolphin":
            _PROVIDERS[name] = DolphinProvider()
        elif name == "camoufox":
            _PROVIDERS[name] = CamoufoxProvider()
        else:
            raise ValueError(f"Unknown DEVICE_PROVIDER: {name!r}")
    return _PROVIDERS[name]
