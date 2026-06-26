from __future__ import annotations
import json
import os
from urllib.parse import urlparse
# patchright is a patched, undetected fork of Playwright: it strips the `--enable-automation`
# switch (the "Chrome is being controlled by automated test software" banner), hides
# navigator.webdriver and the Runtime.enable/CDP leaks that TikTok's anti-bot fingerprints.
# Plain Playwright here was getting the session silently flagged — TikTok then NO-OPs the
# send_code request (no network call, no captcha, no email) even on a manual click. The reader
# already uses patchright; the registration context must too, or it's detected on arrival.
try:
    from patchright.async_api import async_playwright, BrowserContext, Page
except ImportError:
    from playwright.async_api import async_playwright, BrowserContext, Page
from utils.stealth import apply_stealth
from utils.fingerprint import get_fingerprint
from config import BROWSERS_DIR, HEADLESS


def _profile_dir(account_id: int) -> str:
    path = os.path.join(BROWSERS_DIR, str(account_id))
    os.makedirs(path, exist_ok=True)
    return path


def _purge_service_worker(profile_dir: str) -> None:
    """Delete the profile's persisted Service Worker store before launch.

    A registered Service Worker (TikTok registers one) in a PERSISTENT Chromium profile
    breaks proxy CONNECT authentication: the SW fires network requests during startup
    before/around the proxy-auth handshake, and the tunnel comes back
    ERR_TUNNEL_CONNECTION_FAILED / ERR_INVALID_AUTH_CREDENTIALS even though the proxy and
    credentials are perfectly valid (confirmed by bisection — removing ONLY this folder
    fixes a profile that otherwise fails every navigation). It's a pure cache: clearing it
    loses NO session state (cookies / localStorage / IndexedDB are separate) and the SW
    simply re-registers on next load, so it's safe to purge on every launch."""
    import shutil
    for sub in ("Service Worker",):
        for base in (os.path.join(profile_dir, "Default", sub), os.path.join(profile_dir, sub)):
            try:
                shutil.rmtree(base, ignore_errors=True)
            except Exception:
                pass


def _common_args(fp) -> list[str]:
    """Chrome launch args shared by the account and guest contexts.

    The WebRTC flags are the important part: by default headless Chrome gathers ICE
    candidates over a direct UDP socket that bypasses the HTTP proxy, leaking the
    SERVER's real IP via JS (`RTCPeerConnection`). Every account would then share one
    real IP regardless of its proxy — an instant multi-account cluster signal that
    defeats all the per-account geo/fingerprint work. `disable_non_proxied_udp` forces
    WebRTC through the proxy (and stealth.py also neuters RTCPeerConnection in JS)."""
    return [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--ignore-certificate-errors",
        f"--user-agent={fp.ua}",
        f"--window-size={fp.screen_w},{fp.screen_h}",
        "--autoplay-policy=no-user-gesture-required",
        f"--lang={fp.locale}",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]


def normalize_proxy(raw: str | None) -> str | None:
    """Canonicalise any supported proxy format to the URL form `_parse_proxy` understands.

    Providers hand out proxies as `host:port:user:pass` or `host:port` (colon-separated),
    while Playwright/urlparse need `http://user:pass@host:port`. Run EVERY proxy through
    here so a stored raw string (added via any path) can't reach urlparse and blow up with
    'Port could not be cast to integer value as ...'."""
    if not raw:
        return raw
    raw = raw.strip()
    if not raw:
        return raw

    # Strip any scheme first, so a user who pasted `http://host:port:user:pass` (scheme +
    # the colon format) is handled too — the scheme prefix must NOT short-circuit parsing,
    # which was the bug: urlparse then read `port:user:pass` as the port and blew up.
    scheme = "http"
    body = raw
    if "://" in raw:
        scheme, body = raw.split("://", 1)

    # Already in proper `user:pass@host:port` authority form — leave it (re-attach scheme).
    if "@" in body:
        return f"{scheme}://{body}"

    parts = body.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"{scheme}://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        host, port = parts
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{body}"


def _parse_proxy(proxy_str: str | None) -> tuple[str | None, dict | None]:
    if not proxy_str:
        return None, None
    proxy_str = normalize_proxy(proxy_str)
    parsed = urlparse(proxy_str)
    host = parsed.hostname or ""
    port = parsed.port or 8080
    user = parsed.username
    pwd  = parsed.password
    proxy_server = f"{host}:{port}"
    credentials  = {"username": user, "password": pwd} if user else None
    return proxy_server, credentials


async def create_context(account_id: int, proxy: str | None = None,
                         session_data: str | None = None) -> tuple:
    """Returns (playwright_instance, context).

    If DEVICE_PROVIDER is an antidetect browser (adspower/dolphin), the context comes from that
    browser over CDP so each account is a genuinely distinct device (see device_provider). Else
    we launch patchright directly (single host device)."""
    from tiktok.device_provider import get_provider
    provider = get_provider()
    if provider is not None:
        return await _create_context_via_provider(provider, account_id, proxy, session_data)

    pw      = await async_playwright().start()
    profile = _profile_dir(account_id)
    # Clear the persisted Service Worker store — it breaks proxy auth on persistent profiles.
    _purge_service_worker(profile)
    # Match the fingerprint's locale/timezone to the proxy's EXIT country (a UA proxy →
    # uk-UA + Europe/Kyiv) so the browser doesn't look like a German user on a Ukrainian
    # IP. Best-effort + cached; falls back to the default fingerprint if lookup fails.
    country = None
    try:
        from utils.geo import country_for_proxy
        country = await country_for_proxy(proxy)
    except Exception:
        country = None
    fp      = get_fingerprint(account_id, country)

    proxy_server, credentials = _parse_proxy(proxy)

    args = _common_args(fp) + ["--use-fake-ui-for-media-stream"]

    launch_kwargs: dict = {
        "user_data_dir": profile,
        "headless":      HEADLESS,
        "channel":       "chrome",
        "user_agent":    fp.ua,
        # no_viewport: let the REAL OS window drive innerWidth/innerHeight. Forcing a viewport
        # from fp.screen made Playwright render at e.g. 2560x1321 while the actual headful
        # window was only ~833px tall → innerHeight(1321) > outerHeight(833), a physically
        # impossible geometry that fv.pro flags as "screen/environment is not real". With the
        # real window driving it, inner is always < outer < screen — fully self-consistent.
        "no_viewport":   True,
        "locale":        fp.locale,
        "timezone_id":   fp.timezone,
        "extra_http_headers": fp.sec_ch_ua(),  # Client-Hints consistent with the UA/platform
        "args":          args,
    }

    # IMPORTANT: proxy credentials must go through Playwright's native `proxy=` launch
    # option (server/username/password), NOT `--proxy-server` arg + `http_credentials`.
    # `http_credentials` is HTTP Basic Auth for the TARGET site, not the proxy — passing
    # the proxy via Chrome args with no auth makes Chrome get 407 Proxy Authentication
    # Required on the CONNECT tunnel and fail with net::ERR_TUNNEL_CONNECTION_FAILED.
    if proxy_server:
        proxy_settings = {"server": f"http://{proxy_server}"}
        if credentials:
            proxy_settings["username"] = credentials["username"]
            proxy_settings["password"] = credentials["password"]
        launch_kwargs["proxy"] = proxy_settings

    context = await pw.chromium.launch_persistent_context(**launch_kwargs)
    context._account_id = account_id  # used by get_page → apply_stealth
    context._country = country        # so get_page rebuilds the SAME geo-matched fp

    if session_data:
        cookies = _normalize_cookies(json.loads(session_data))
        await context.add_cookies(cookies)

    return pw, context


async def _create_context_via_provider(provider, account_id: int, proxy: str | None,
                                       session_data: str | None) -> tuple:
    """Drive an antidetect browser: open a profile (distinct device + proxy), connect Playwright
    over its CDP endpoint, and return (pw, context) shaped like the patchright path so the rest of
    the flow is unchanged. The provider handles the device fingerprint, so we apply NO JS stealth."""
    country = None
    try:
        from utils.geo import country_for_proxy
        country = await country_for_proxy(proxy)
    except Exception:
        country = None

    from tiktok.device_provider import open_provider_context
    pw = await async_playwright().start()
    try:
        # Handles both provider shapes: CDP attach (AdsPower/Dolphin) or a ready context (Camoufox).
        context, handle = await open_provider_context(provider, pw, account_id, proxy, country)
    except Exception:
        await pw.stop()
        raise

    context._account_id = account_id
    context._country = country
    context._device_handle = handle          # teardown_device() stops+deletes / closes the device
    context._is_provider = True               # get_page skips our JS stealth for antidetect
    context._cdp_browser = handle.extra

    if session_data:
        cookies = _normalize_cookies(json.loads(session_data))
        await context.add_cookies(cookies)
    return pw, context


async def teardown_device(context: BrowserContext) -> None:
    """Stop + delete the antidetect profile after a run (no-op for patchright). Safe to call after
    context.close() — the handle is a plain attribute that outlives the closed CDP context."""
    handle = getattr(context, "_device_handle", None)
    if handle is None:
        return
    try:
        await handle.provider.close_device(handle, delete=True)
    except Exception:
        pass


async def create_guest_context(proxy: str | None = None) -> tuple:
    """Returns (playwright_instance, context) for a LOGGED-OUT viewer — a throwaway
    profile with no account cookies. Used by the shadowban check to see a comment the
    way a third party (or anyone but its author) sees it: an account-shadowbanned
    comment is visible to its author but absent for every logged-out viewer.

    Routes through `proxy` to keep the same geo/region as the posting account (so a
    region-gated video still loads), but carries NO session, so TikTok cannot tie the
    viewer to the author's account."""
    import tempfile
    pw  = await async_playwright().start()
    fp  = get_fingerprint(0)  # neutral fingerprint — not tied to any real account
    profile = tempfile.mkdtemp(prefix="guest_")

    proxy_server, credentials = _parse_proxy(proxy)
    args = _common_args(fp)
    launch_kwargs: dict = {
        "user_data_dir": profile,
        "headless":      HEADLESS,
        "channel":       "chrome",
        "user_agent":    fp.ua,
        # no_viewport: let the REAL OS window drive innerWidth/innerHeight. Forcing a viewport
        # from fp.screen made Playwright render at e.g. 2560x1321 while the actual headful
        # window was only ~833px tall → innerHeight(1321) > outerHeight(833), a physically
        # impossible geometry that fv.pro flags as "screen/environment is not real". With the
        # real window driving it, inner is always < outer < screen — fully self-consistent.
        "no_viewport":   True,
        "locale":        fp.locale,
        "timezone_id":   fp.timezone,
        "extra_http_headers": fp.sec_ch_ua(),
        "args":          args,
    }
    if proxy_server:
        proxy_settings = {"server": f"http://{proxy_server}"}
        if credentials:
            proxy_settings["username"] = credentials["username"]
            proxy_settings["password"] = credentials["password"]
        launch_kwargs["proxy"] = proxy_settings

    context = await pw.chromium.launch_persistent_context(**launch_kwargs)
    context._account_id = 0
    context._guest_profile_dir = profile  # so the caller can clean it up
    return pw, context


_SAME_SITE_MAP = {
    "strict": "Strict", "lax": "Lax", "none": "None",
    "no_restriction": "None", "unspecified": "None",
}
_ALLOWED_COOKIE_FIELDS = {
    "name", "value", "url", "domain", "path",
    "expires", "httpOnly", "secure", "sameSite",
}


def _normalize_cookies(cookies: list) -> list:
    result = []
    for c in cookies:
        nc = {k: v for k, v in c.items() if k in _ALLOWED_COOKIE_FIELDS}
        if "sameSite" in nc:
            nc["sameSite"] = _SAME_SITE_MAP.get(str(nc["sameSite"]).lower(), "None")
        # Cookie-Editor (and most browser-extension exports) use `expirationDate`,
        # not Playwright's `expires`. Without this the expiry is dropped and every
        # cookie becomes session-only — convert it so imported cookies keep their TTL.
        if "expires" not in nc and c.get("expirationDate"):
            try:
                nc["expires"] = int(float(c["expirationDate"]))
            except (TypeError, ValueError):
                pass
        result.append(nc)
    return result


async def save_session(context: BrowserContext) -> str:
    cookies = await context.cookies()
    return json.dumps(cookies)


async def get_page(context: BrowserContext) -> Page:
    page = context.pages[0] if context.pages else await context.new_page()
    # Antidetect browser (adspower/dolphin) already injects a complete, self-consistent device
    # fingerprint at the C++ level. Layering our JS stealth on top would DOUBLE-spoof (e.g. our
    # canvas noise over theirs) and re-introduce the very inconsistency pixelscan flags. So for a
    # provider context, return the page as-is.
    if getattr(context, "_is_provider", False):
        return page
    account_id = getattr(context, "_account_id", 0)
    country = getattr(context, "_country", None)
    fp = get_fingerprint(account_id, country)  # same geo-matched fp as create_context
    # spoof_gpu=False on purpose. The earlier theory was that per-account canvas/WebGL/audio
    # noise makes each account a distinct DEVICE and dodges TikTok's device-fingerprint rate
    # limit. Pixelscan disproved it: with spoof ON the profile is flagged "Masking detected" /
    # fingerprint inconsistent (the faked WebGL renderer string contradicts the real Apple-Metal
    # GL params, and the canvas pixel-noise yields a 100%-unique, non-native toDataURL — both
    # are antidetect tells). A "masked" fingerprint is exactly what gets dumped into the
    # suspicious cluster that returns error_code 7 on the FIRST verify of a fresh mailbox+IP.
    # So we present the genuine, self-consistent real-Mac GPU fingerprint (passes pixelscan as
    # consistent) and take per-account distinctness from the proxy/IP instead of from
    # detectable JS masking. (Outlook/PerimeterX already used spoof_gpu=False for the same
    # reason — see outlook/registrar.py and reader.py.)
    await apply_stealth(page, fp, spoof_gpu=False)
    return page
