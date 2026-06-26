"""OAuth2 + IMAP(XOAUTH2) access to personal Outlook mailboxes — no browser re-login.

Personal Microsoft accounts have Basic-Auth IMAP disabled (LOGIN fails), but IMAP with
XOAUTH2 works. So instead of re-logging into outlook.live.com on every code read (which
rate-limits the IP with HTTP 429 → bounced to the marketing page), we:

  1. ONCE, during registration (the browser is already authenticated), run the OAuth2
     auth-code+PKCE flow using Thunderbird's PUBLIC client_id (Microsoft allows it IMAP
     access on consumer accounts — no Azure app of our own to register). The already-signed-in
     session consents silently and redirects to https://localhost?code=…; we read the code
     from page.url and exchange it for a refresh_token. `capture_refresh_token`.
  2. FOREVER after, `read_tiktok_code_imap(email, refresh_token)` mints a short-lived access
     token from the refresh_token and pulls the TikTok code over IMAP. Zero web logins → no 429.

Thunderbird client_id is a well-known public client trusted by Microsoft for IMAP/SMTP.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import imaplib
import logging
import os
import re
from email import message_from_bytes
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp

logger = logging.getLogger(__name__)

CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"  # Mozilla Thunderbird (public client)
REDIRECT = "https://localhost"
SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
# `consumers` tenant (NOT `common`): personal Outlook accounts auth at login.live.com, which
# accepts passwords. `common` routed them through the AAD endpoint that refuses password sign-in
# ("Вхід за допомогою пароля недоступний"). consumers also SSOs off the existing login.live.com
# session, so no re-login is needed.
AUTHORIZE = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
IMAP_HOST = "outlook.office365.com"


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


async def enable_imap_setting(page) -> bool:
    """Best-effort: turn ON 'Let devices and apps use IMAP' in the (authenticated) Outlook web
    settings. A freshly-created mailbox often has IMAP OFF → a valid XOAUTH2 token still gets
    'User is authenticated but not connected'. Returns True if a toggle was switched (or already on).
    Screenshots /tmp/imap_settings.png for debugging when selectors miss."""
    SETTINGS = "https://outlook.live.com/mail/0/options/mail/accounts/popImap"
    try:
        await page.goto(SETTINGS, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)
    except Exception as e:
        logger.warning("enable_imap: goto settings failed: %s", e)
        return False
    # The popImap deep-link lands on the Mail settings ROOT (left menu), not the IMAP panel —
    # click the "Forwarding and IMAP" / "Пересилання та IMAP" menu entry to open the panel first.
    for sel in ('button:has-text("Пересилання та IMAP")', 'div:has-text("Пересилання та IMAP")',
                'button:has-text("Forwarding and IMAP")', 'button:has-text("POP and IMAP")',
                ':text("Пересилання та IMAP")', ':text("Forwarding and IMAP")'):
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=800):
                await el.click(timeout=3_000)
                await asyncio.sleep(2)
                break
        except Exception:
            continue
    try:
        await page.screenshot(path="/tmp/imap_settings.png")
    except Exception:
        pass
    # Find a toggle/checkbox associated with IMAP and switch it on if off.
    toggled = False
    candidates = (
        'button[role="switch"]', 'input[type="checkbox"]', '[role="switch"]',
    )
    for sel in candidates:
        try:
            locs = page.locator(sel)
            n = await locs.count()
            for i in range(min(n, 12)):
                el = locs.nth(i)
                try:
                    label = (await el.get_attribute("aria-label") or "")
                except Exception:
                    label = ""
                checked = (await el.get_attribute("aria-checked")) or (await el.get_attribute("checked"))
                # Heuristic: any switch whose label mentions IMAP and isn't already on.
                if "imap" in label.lower() and str(checked).lower() not in ("true", "checked"):
                    await el.click(timeout=3_000)
                    toggled = True
                    await asyncio.sleep(1)
            if toggled:
                break
        except Exception:
            continue
    # Save if there's a save button.
    for sel in ('button:has-text("Save")', 'button:has-text("Зберегти")',
                'button[type="submit"]'):
        try:
            b = page.locator(sel).first
            if await b.is_visible(timeout=800):
                await b.click(timeout=3_000)
                await asyncio.sleep(1.5)
                break
        except Exception:
            continue
    logger.info("enable_imap: toggled=%s", toggled)
    return toggled


async def capture_refresh_token(page, proxy: Optional[str] = None,
                                email: Optional[str] = None,
                                password: Optional[str] = None) -> Optional[str]:
    """Run the OAuth2 auth-code+PKCE consent flow on `page` and return a refresh_token (or None).

    The authorize endpoint (login.microsoftonline.com) does NOT share cookies with the
    outlook.live.com / login.live.com session, so it usually re-prompts for sign-in even when
    the browser is already logged into the mailbox. Pass `email`/`password` so we can complete
    that login form in-flow; consent is then auto-approved and redirects to https://localhost?code=…"""
    verifier, challenge = _pkce()
    params = urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    # Intercept the final redirect to https://localhost?code=… at the NETWORK layer. Navigating
    # to localhost just fails to load (connection refused) and does NOT reliably update page.url,
    # so polling the URL missed the code. Routing the request lets us read `code` from the
    # request URL directly and short-circuit the dead navigation.
    holder: dict = {}

    async def _route(route):
        url = route.request.url
        qs = parse_qs(urlparse(url).query)
        if "code" in qs:
            holder["code"] = qs["code"][0]
        elif "error" in qs:
            holder["error"] = qs.get("error_description", qs.get("error", ["?"]))[0]
        try:
            await route.fulfill(status=200, content_type="text/plain", body="ok")
        except Exception:
            pass

    await page.route("https://localhost/**", _route)
    try:
        try:
            await page.goto(f"{AUTHORIZE}?{params}", wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            pass
        code = await _await_redirect_code(page, holder, email=email, password=password)
    finally:
        try:
            await page.unroute("https://localhost/**", _route)
        except Exception:
            pass

    if not code:
        if holder.get("error"):
            logger.warning("OAuth consent error: %s", holder["error"])
        else:
            logger.warning("OAuth: no auth code captured (consent not completed)")
        try:
            await page.screenshot(path="/tmp/oauth_debug.png")
        except Exception:
            pass
        return None
    rt = await _exchange_code(code, verifier, proxy)
    if rt:
        logger.info("OAuth: captured refresh_token (len=%d)", len(rt))
    return rt


async def _await_redirect_code(page, holder: dict, timeout_s: int = 60,
                               email: Optional[str] = None,
                               password: Optional[str] = None) -> Optional[str]:
    """Wait for the routed localhost redirect to deposit `code` in `holder`, driving the MS
    sign-in form (email → password) and any consent/account screens in between."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    # NB: deliberately NO #idSIButton9 here — on the passkey screen that id is the PRIMARY
    # "Далі/Next" which opens the native passkey dialog and hangs us. The real OAuth consent
    # "Accept" is #idBtn_Accept / text Yes·Accept·Прийняти.
    consent_sels = (
        '#idBtn_Accept', 'input[type="submit"][value="Yes"]',
        'input[type="submit"][value="Accept"]', 'button:has-text("Accept")',
        'button:has-text("Yes")', 'button:has-text("Прийняти")', 'button:has-text("Так")',
    )
    typed_email = typed_pwd = False
    while asyncio.get_event_loop().time() < deadline:
        if "code" in holder:
            return holder["code"]
        if "error" in holder:
            return None
        # 0) passkey/FIDO enrollment interstitial (incl. the "Ваш пристрій відкривав вікно
        # системи безпеки" native-dialog state): clicking the PRIMARY "Далі/Next" opens a native
        # OS passkey dialog the bot can't complete and we hang forever. Detect this screen by text
        # and click ONLY the SECONDARY Cancel/Skip/Back. Gated on passkey wording so we never
        # Cancel the real consent screen (there Cancel = deny). Loop here until it clears.
        try:
            body = (await page.locator("body").inner_text(timeout=400)).lower()
        except Exception:
            body = ""
        if ("ключ" in body and "доступ" in body) or "passkey" in body \
                or "security key" in body or "windows hello" in body \
                or ("вікно системи безпеки" in body):
            clicked = False
            for sel in ('button:has-text("Скасувати")', 'button:has-text("Cancel")',
                        'button:has-text("Skip for now")', 'button:has-text("Пропустити")',
                        'button:has-text("Maybe later")', 'button:has-text("Назад")',
                        '#iCancel', '[data-testid="secondaryButton"]', '#idBtn_Back'):
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=400):
                        await el.click(timeout=2_000)
                        clicked = True
                        await asyncio.sleep(1.2)
                        break
                except Exception:
                    continue
            if not clicked:
                # last resort: Escape can close the native-security-window prompt
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
            await asyncio.sleep(0.8)
            continue
        # 1) sign-in: email field
        if email and not typed_email:
            try:
                e = page.locator('input[type="email"], input[name="loginfmt"]').first
                if await e.is_visible(timeout=500):
                    await e.fill(email)
                    await page.click('button[type="submit"], #idSIButton9', timeout=4_000)
                    typed_email = True
                    await asyncio.sleep(1.5)
                    continue
            except Exception:
                pass
        # 2) sign-in: password field
        if password and not typed_pwd:
            try:
                p = page.locator('input[type="password"]').first
                if await p.is_visible(timeout=500):
                    await p.fill(password)
                    await page.click('button[type="submit"], #idSIButton9', timeout=4_000)
                    typed_pwd = True
                    await asyncio.sleep(1.5)
                    continue
            except Exception:
                pass
        # 3) consent / "stay signed in" screens
        for sel in consent_sels:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=400):
                    await el.click(timeout=2_000)
                    await asyncio.sleep(1.2)
                    break
            except Exception:
                continue
        await asyncio.sleep(1.0)
    return None


async def _exchange_code(code: str, verifier: str, proxy: Optional[str]) -> Optional[str]:
    data = {
        "client_id": CLIENT_ID, "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "code_verifier": verifier, "scope": SCOPE,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(TOKEN, data=data) as r:
                j = await r.json(content_type=None)
        if "refresh_token" not in j:
            logger.warning("OAuth token exchange failed: %s", j.get("error_description") or j)
        return j.get("refresh_token")
    except Exception as e:
        logger.warning("OAuth token exchange error: %s", e)
        return None


async def get_access_token(refresh_token: str) -> Optional[str]:
    data = {
        "client_id": CLIENT_ID, "grant_type": "refresh_token",
        "refresh_token": refresh_token, "scope": SCOPE,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(TOKEN, data=data) as r:
                j = await r.json(content_type=None)
        if "access_token" not in j:
            logger.warning("OAuth refresh failed: %s", j.get("error_description") or j)
        return j.get("access_token")
    except Exception as e:
        logger.warning("OAuth refresh error: %s", e)
        return None


# ── IMAP ────────────────────────────────────────────────────────────────────────────
_CODE_RE = re.compile(r"\b(\d{6})\b")


def _msg_text(raw: bytes) -> str:
    try:
        msg = message_from_bytes(raw)
    except Exception:
        return ""
    parts = []
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() in ("text/plain", "text/html"):
                try:
                    parts.append(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8",
                                                                    "ignore"))
                except Exception:
                    pass
    else:
        try:
            parts.append(msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8",
                                                             "ignore"))
        except Exception:
            pass
    subj = str(msg.get("Subject", ""))
    frm = str(msg.get("From", ""))
    return f"{frm}\n{subj}\n" + "\n".join(parts)


def _best_code(text: str, exclude: Optional[str]) -> Optional[str]:
    if not text or "tiktok" not in text.lower():
        return None
    for m in re.finditer(r"\b(\d{6})\b", text):
        if exclude and m.group(1) == exclude:
            continue
        s, e = m.start(1), m.end(1)
        ctx = (text[max(0, s - 40):s] + " " + text[e:e + 40]).lower()
        if "tiktok" in ctx or "verif" in ctx or "код" in ctx or "code" in ctx:
            return m.group(1)
    for m in re.finditer(r"\b(\d{6})\b", text):
        if not (exclude and m.group(1) == exclude):
            return m.group(1)
    return None


def _imap_scan(email_addr: str, access_token: str, exclude: Optional[str]) -> Optional[str]:
    """Blocking IMAP read of the newest TikTok code from INBOX + Junk. Run in an executor."""
    auth = f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01"
    M = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    try:
        M.authenticate("XOAUTH2", lambda _: auth.encode())
    except Exception as e:
        M.logout()
        raise RuntimeError(f"IMAP XOAUTH2 auth failed: {e}")
    try:
        for folder in ("INBOX", "Junk"):
            try:
                M.select(folder, readonly=True)
            except Exception:
                continue
            try:
                typ, data = M.search(None, "ALL")
            except Exception:
                continue
            ids = data[0].split()
            for mid in reversed(ids[-15:]):  # newest first
                try:
                    typ, msg = M.fetch(mid, "(RFC822)")
                    raw = msg[0][1]
                except Exception:
                    continue
                code = _best_code(_msg_text(raw), exclude)
                if code:
                    return code
        return None
    finally:
        try:
            M.logout()
        except Exception:
            pass


async def read_tiktok_code_imap(email_addr: str, refresh_token: str,
                                exclude: Optional[str] = None,
                                timeout_s: int = 120) -> Optional[str]:
    """Poll the mailbox over IMAP for the newest TikTok code (≠ exclude), no browser/login."""
    access = await get_access_token(refresh_token)
    if not access:
        return None
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        try:
            code = await loop.run_in_executor(None, _imap_scan, email_addr, access, exclude)
        except Exception as e:
            logger.warning("IMAP read error for %s: %s", email_addr, e)
            code = None
            access = await get_access_token(refresh_token) or access  # token may have expired
        if code:
            logger.info("Got TikTok code %s for %s (IMAP)", code, email_addr)
            return code
        if exclude:
            logger.info("Only stale code %s over IMAP for %s — waiting for a fresh one",
                        exclude, email_addr)
        await asyncio.sleep(5)
    logger.warning("No %sTikTok code over IMAP for %s within %ss",
                   "fresh " if exclude else "", email_addr, timeout_s)
    return None
