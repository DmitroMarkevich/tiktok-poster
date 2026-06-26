"""Resolve a proxy's EXIT country so the browser fingerprint (locale, timezone,
languages) can match it — a Ukrainian proxy should look like a Ukrainian user, not a
random German one. A locale/proxy-country mismatch is an anti-detection red flag.

We route a request THROUGH the proxy to a free IP-geo endpoint, so we get the proxy's
real exit country (not the proxy host's). Cached per proxy string for the process
lifetime — the lookup runs at most once per proxy."""
from __future__ import annotations

import json
import os

import aiohttp

# In-process cache for the current run, plus a JSON file so the lookup SURVIVES a bot
# restart (otherwise every proxy is re-resolved over the network on each cold start,
# adding seconds to the first run per proxy). Keyed by host:port only — never the
# credentials — since the exit country depends on the endpoint, not the login.
_cache: dict = {}
_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "geo_cache.json")


def _key(proxy: str) -> str:
    from tiktok.browser import _parse_proxy
    server, _creds = _parse_proxy(proxy)
    return server or proxy


def _load_cache() -> None:
    if _cache:
        return
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            _cache.update(json.load(f))
    except Exception:
        pass


def _save_cache() -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f)
    except Exception:
        pass


async def country_for_proxy(proxy: str | None) -> str | None:
    """Return the 2-letter country code of the proxy's exit IP, or None (no proxy /
    lookup failed). Best-effort: a failure just means we fall back to the default
    fingerprint locale, never an error."""
    if not proxy:
        return None
    _load_cache()
    key = _key(proxy)
    if key in _cache:
        return _cache[key]

    cc = None
    try:
        from tiktok.browser import normalize_proxy
        purl = normalize_proxy(proxy)
        async with aiohttp.ClientSession() as session:
            # ip-api.com is free over HTTP (no key) and returns the exit IP's country.
            async with session.get(
                "http://ip-api.com/json/?fields=status,countryCode",
                proxy=purl,
                # 5s, not 10: a proxy too slow to answer a tiny geo call in 5s is too slow
                # to wait on — fall back to the default fingerprint locale instead.
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                data = await r.json(content_type=None)
        if data.get("status") == "success":
            cc = (data.get("countryCode") or "").upper() or None
    except Exception:
        cc = None

    # Only persist a real hit — caching a None from a transient failure would wrongly
    # pin the proxy to "unknown" across restarts.
    _cache[key] = cc
    if cc:
        _save_cache()
    return cc
