from __future__ import annotations
import json
import os
from urllib.parse import urlparse
from playwright.async_api import async_playwright, BrowserContext, Page
from utils.stealth import apply_stealth
from utils.fingerprint import get_fingerprint
from config import BROWSERS_DIR


def _profile_dir(account_id: int) -> str:
    path = os.path.join(BROWSERS_DIR, str(account_id))
    os.makedirs(path, exist_ok=True)
    return path


def _parse_proxy(proxy_str: str | None) -> tuple[str | None, dict | None]:
    if not proxy_str:
        return None, None
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
    """Returns (playwright_instance, context)."""
    pw      = await async_playwright().start()
    profile = _profile_dir(account_id)
    fp      = get_fingerprint(account_id)

    proxy_server, credentials = _parse_proxy(proxy)

    args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--ignore-certificate-errors",
        f"--user-agent={fp.ua}",
        f"--window-size={fp.screen_w},{fp.screen_h}",
        "--autoplay-policy=no-user-gesture-required",
        "--use-fake-ui-for-media-stream",
        f"--lang={fp.locale}",
    ]

    launch_kwargs: dict = {
        "user_data_dir": profile,
        "headless":      True,
        "channel":       "chrome",
        "user_agent":    fp.ua,
        "viewport":      {"width": fp.screen_w, "height": fp.screen_h},
        "locale":        fp.locale,
        "timezone_id":   fp.timezone,
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

    if session_data:
        cookies = _normalize_cookies(json.loads(session_data))
        await context.add_cookies(cookies)

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
    account_id = getattr(context, "_account_id", 0)
    fp = get_fingerprint(account_id)
    await apply_stealth(page, fp)
    return page
