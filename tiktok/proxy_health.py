"""Lightweight dead-proxy tracking + reachability preflight.

`rotate_proxy` cycles an account's proxy list blindly, so a dead endpoint (e.g.
ERR_TUNNEL_CONNECTION_FAILED / 407) keeps getting handed out — the account silently
fails every cycle and, worse, looks inconsistent to TikTok. This module keeps a small
JSON-backed health store (keyed by host:port only, never credentials — same convention as
geo_cache) of CONSECUTIVE failures per proxy, plus a cheap "is this proxy reachable right
now?" check the autopilot runs before committing an account to a run.

Everything is best-effort and never raises: a health-store hiccup must not break a run.
"""
from __future__ import annotations

import json
import os
import time

import aiohttp

_FAIL_THRESHOLD = 3          # consecutive failures before a proxy is considered dead
_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "proxy_health.json")
_store: dict = {}
_loaded = False


def _key(proxy: str | None) -> str | None:
    if not proxy:
        return None
    try:
        from tiktok.browser import _parse_proxy
        server, _creds = _parse_proxy(proxy)
        return server or proxy
    except Exception:
        return proxy


def _load() -> None:
    global _loaded
    if _loaded:
        return
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            _store.update(json.load(f))
    except Exception:
        pass
    _loaded = True


def _save() -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_store, f)
    except Exception:
        pass


def record_failure(proxy: str | None) -> int:
    """Bump the consecutive-failure counter for `proxy`. Returns the new count."""
    _load()
    k = _key(proxy)
    if not k:
        return 0
    rec = _store.get(k) or {}
    rec["fails"] = int(rec.get("fails", 0)) + 1
    rec["last"] = time.time()
    _store[k] = rec
    _save()
    return rec["fails"]


def record_success(proxy: str | None) -> None:
    """Reset the failure counter — the proxy answered, so it's healthy again."""
    _load()
    k = _key(proxy)
    if not k:
        return
    if _store.get(k, {}).get("fails"):
        _store[k] = {"fails": 0, "last": time.time()}
        _save()


def is_dead(proxy: str | None) -> bool:
    """True if `proxy` has hit the consecutive-failure threshold."""
    _load()
    k = _key(proxy)
    if not k:
        return False
    return int(_store.get(k, {}).get("fails", 0)) >= _FAIL_THRESHOLD


async def check(proxy: str | None, *, timeout: float = 8.0) -> bool:
    """Quick reachability preflight through the proxy. Records the outcome (success resets /
    failure increments the counter) and returns alive bool. No proxy → treated as alive.

    Tests an HTTPS endpoint on purpose: the real workload is an HTTPS CONNECT tunnel to
    tiktok.com:443, and a proxy can serve plain HTTP yet fail HTTPS CONNECT — an HTTP-only
    probe would mark such a proxy alive and let the autopilot waste a cycle on it."""
    if not proxy:
        return True
    try:
        from tiktok.browser import _parse_proxy
        server, creds = _parse_proxy(proxy)
        auth = aiohttp.BasicAuth(creds["username"], creds["password"]) if creds else None
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.ipify.org?format=json",
                proxy=f"http://{server}", proxy_auth=auth,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                ok = r.status == 200
    except Exception:
        ok = False
    if ok:
        record_success(proxy)
    else:
        record_failure(proxy)
    return ok
