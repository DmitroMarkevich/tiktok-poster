"""Read a TikTok email-verification code from an Outlook inbox via the web UI.

Personal Outlook accounts have basic-auth IMAP disabled, so instead of IMAP/Graph
we just log into outlook.live.com in a throwaway Playwright context and scrape the
newest TikTok message. Reuses the PerimeterX press-and-hold solver from registrar
because the Outlook login itself can show that challenge.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Optional

try:
    from patchright.async_api import async_playwright, Page
except ImportError:
    from playwright.async_api import async_playwright, Page

from .registrar import _proxy_settings, _slow_type, _solve_press_and_hold, _captcha_still_present
from tiktok.browser import _common_args
from utils.fingerprint import get_fingerprint
from utils.stealth import apply_stealth

logger = logging.getLogger(__name__)

LOGIN_URL = "https://login.live.com/login.srf"
INBOX_URL = "https://outlook.live.com/mail/0/"
# TikTok verification mail very often lands in Junk for a brand-new mailbox; scan it too.
JUNK_URL = "https://outlook.live.com/mail/0/junkemail"

# TikTok codes are 6 digits; match in subject/body next to TikTok/verification words.
_CODE_RE = re.compile(r"\b(\d{6})\b")


async def _dismiss_stay_signed_in(page: Page) -> None:
    for label in ("Yes", "Так", "No", "Ні"):
        try:
            btn = page.locator(f'button:has-text("{label}"), input[value="{label}"]').first
            if await btn.is_visible(timeout=2_000):
                await btn.click()
                await asyncio.sleep(1.5)
                return
        except Exception:
            continue


async def _extract_code_from_page(page: Page, exclude: Optional[str] = None) -> Optional[str]:
    """Return the TikTok 6-digit code from the NEWEST verification email.

    Scraping the whole inbox preview text is unreliable when the mailbox holds several
    TikTok codes (repeated sign-up attempts) — the preview can be truncated or an OLD
    code can sit above the fresh one, and entering a stale code makes TikTok reject it
    with "code expired or incorrect". So we instead OPEN the topmost (newest) TikTok
    message and read the code from its own body, falling back to the old whole-page scan
    only if the message list can't be opened.

    `exclude` is a code we already tried and TikTok REJECTED (error 1704/1705). We must
    never return it again — when the only code on the page equals `exclude`, treat it as
    "no fresh code yet" (return None) so the caller keeps polling for the genuinely new
    one instead of re-submitting a dead code in a loop."""
    # Outlook lists messages newest-first as role="option" rows in the message list.
    try:
        rows = page.locator('div[role="option"], [aria-label*="message list"] [role="listitem"]')
        n = await rows.count()
        for i in range(min(n, 6)):  # only the few newest rows matter
            row = rows.nth(i)
            try:
                label = (await row.inner_text(timeout=1_500)).lower()
            except Exception:
                continue
            # Require a genuine TikTok sender/subject. A fresh mailbox's only message is the
            # Microsoft welcome/security email, which ALSO contains a 6-digit number and the
            # word "code" — matching it returned a bogus code (228435) that TikTok rejected.
            if "tiktok" not in label:
                continue
            # Open it and read the code from the opened message body (freshest, full text).
            try:
                await row.click(timeout=3_000)
                await page.wait_for_timeout(1_500)
                body = await page.locator(
                    '[role="main"], [aria-label*="Reading"], div[id*="ReadingPane"]'
                ).first.inner_text(timeout=4_000)
            except Exception:
                body = label
            code = _best_code(body, exclude)
            if code:
                return code
    except Exception:
        pass

    # Fallback: whole-page scan — but ONLY when a TikTok message is actually on the page.
    # Without this guard the scan grabs the 6-digit number from the Microsoft welcome/security
    # email (the infamous 228435 — identical for every fresh mailbox), which TikTok then rejects.
    try:
        text = await page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return None
    if "tiktok" not in text.lower():
        return None
    return _best_code(text, exclude)


def _best_code(text: str, exclude: Optional[str] = None) -> Optional[str]:
    """Pick a 6-digit code, preferring one adjacent to a TikTok/verification keyword.

    Never return `exclude` (an already-rejected code): skip it so a stale code that still
    sits in the mailbox can't be handed back over and over."""
    if not text:
        return None
    keyworded = None
    for m in re.finditer(r"\b(\d{6})\b", text):
        if exclude and m.group(1) == exclude:
            continue
        s, e = m.start(1), m.end(1)
        # Look on BOTH sides: subjects read "134568 est ton code…" (keyword after the digits)
        # while bodies read "code de vérification … 134568" (keyword before). Either counts.
        ctx = (text[max(0, s - 40):s] + " " + text[e:e + 40]).lower()
        if "tiktok" in ctx or "verif" in ctx or "код" in ctx or "code" in ctx:
            keyworded = m.group(1)
            break
    if keyworded:
        return keyworded
    for m in re.finditer(r"\b(\d{6})\b", text):
        if not (exclude and m.group(1) == exclude):
            return m.group(1)
    return None


async def login_outlook(page: Page, email: str, password: str) -> None:
    """Log a given page into Outlook web and land on the inbox. Works on ANY page — its own
    throwaway browser (fetch_tiktok_code) OR a second tab inside the TikTok signup browser, so we
    can keep ONE logged-in Outlook tab open instead of re-logging-in for every code read."""
    for gattempt in range(4):
        try:
            await page.goto(LOGIN_URL, timeout=30_000, wait_until="domcontentloaded")
            break
        except Exception as e:
            if gattempt == 3:
                raise
            logger.warning("reader goto LOGIN_URL timed out (try %d): %s — retrying", gattempt + 1, e)
            await asyncio.sleep(3)
    await asyncio.sleep(2)

    await page.wait_for_selector('input[type="email"], input[name="loginfmt"]', timeout=15_000)
    # A freshly-created mailbox may not be propagated yet → no password field; retry the email step
    # language-agnostically (success == password field appeared).
    password_ready = False
    for attempt in range(6):
        await _slow_type(page, 'input[type="email"], input[name="loginfmt"]', email)
        await page.click('button[type="submit"], #idSIButton9', timeout=8_000)
        try:
            await page.wait_for_selector('input[type="password"]', timeout=7_000)
            password_ready = True
            break
        except Exception:
            pass
        wait = 15 * (attempt + 1)
        logger.info("Mailbox %s not propagated yet (no password field) — Next retry in %ss", email, wait)
        await asyncio.sleep(wait)
        try:
            await page.fill('input[type="email"], input[name="loginfmt"]', "")
        except Exception:
            pass

    if not password_ready:
        await page.wait_for_selector('input[type="password"]', timeout=15_000)
    await _slow_type(page, 'input[type="password"]', password)
    await page.click('button[type="submit"], #idSIButton9', timeout=8_000)
    await asyncio.sleep(3)

    if await _captcha_still_present(page):
        await _solve_press_and_hold(page)
    await _dismiss_stay_signed_in(page)

    for gattempt in range(4):
        try:
            await page.goto(INBOX_URL, timeout=30_000, wait_until="domcontentloaded")
            break
        except Exception as e:
            if gattempt == 3:
                raise
            logger.warning("reader goto INBOX_URL timed out (try %d): %s — retrying", gattempt + 1, e)
            await asyncio.sleep(3)


def _on_mailbox(page: Page) -> bool:
    """True only when we're actually looking at the Outlook web mailbox. When Outlook
    rate-limits (HTTP 429) or the session drops, `outlook.live.com/mail/...` 302-redirects
    to the `microsoft.com/.../outlook?deeplink=...` MARKETING page — reloading THAT forever
    (the bug we hit) never shows mail. Detect it so we re-login/back off instead."""
    u = (page.url or "").lower()
    return "outlook.live.com/mail" in u or "outlook.office" in u


async def poll_code_on_page(page: Page, email: str, timeout_s: int = 120,
                            exclude: Optional[str] = None, relogin=None) -> Optional[str]:
    """Poll a logged-in Outlook inbox page for the newest TikTok code.

    `exclude` = a previously-rejected code we must NOT return again — keep polling until a
    DIFFERENT code lands. `relogin` = optional async callable() that re-authenticates the page;
    called when Outlook bounces us off the mailbox (429/redirect) instead of dumbly reloading
    the marketing landing page. Junk is scanned less often than Inbox to avoid tripping 429."""
    deadline = asyncio.get_event_loop().time() + timeout_s

    async def _recover() -> bool:
        """We're off the mailbox (429 / logged out). Re-login if we can, else back off and
        try to navigate straight back to the inbox once."""
        logger.warning("reader bounced off mailbox for %s (429/redirect, url=%s) — recovering",
                       email, page.url)
        await asyncio.sleep(random.uniform(8, 15))   # let any 429 window cool down
        if relogin is not None:
            try:
                await relogin()
                return _on_mailbox(page)
            except Exception as e:
                logger.warning("reader relogin failed: %s", e)
        try:
            await page.goto(INBOX_URL, timeout=25_000, wait_until="domcontentloaded")
        except Exception:
            pass
        return _on_mailbox(page)

    async def _scan(url: str) -> Optional[str]:
        # Navigate ONLY when we're not already on this folder. Never page.reload() the mailbox:
        # a hard reload of outlook.live.com/mail reliably 302s to the microsoft.com marketing page
        # (even on a clean IP). Outlook pushes new mail into the live DOM on its own, so for the
        # folder we're already on we just RE-READ the DOM — new messages appear without a reload.
        try:
            if page.url.split("?")[0].rstrip("/") != url.rstrip("/"):
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except Exception:
            pass
        if not _on_mailbox(page):     # got redirected to the marketing page → don't scrape it
            return None
        try:
            await page.wait_for_selector('div[role="option"]', timeout=8_000)
        except Exception:
            pass
        await page.wait_for_timeout(1_500)
        return await _extract_code_from_page(page, exclude)

    cycle = 0
    while asyncio.get_event_loop().time() < deadline:
        if not _on_mailbox(page):
            if not await _recover():
                await asyncio.sleep(random.uniform(6, 10))
                cycle += 1
                continue
        # Inbox every cycle; Junk only every 3rd (it's where fresh-mailbox TikTok mail often
        # lands, but checking it each pass doubles navigations and helps trip 429).
        urls = [INBOX_URL] + ([JUNK_URL] if cycle % 3 == 2 else [])
        for url in urls:
            code = await _scan(url)
            if code:
                logger.info("Got TikTok code %s for %s (folder=%s)", code, email,
                            "junk" if "junk" in url else "inbox")
                return code
        if exclude:
            logger.info("Only stale/rejected code %s in mailbox for %s — waiting for a fresh one",
                        exclude, email)
        cycle += 1
        await asyncio.sleep(random.uniform(6, 9))   # gentler cadence → fewer 429s
    logger.warning("No %sTikTok code found for %s within %ss (url=%s)",
                   "fresh " if exclude else "", email, timeout_s, page.url)
    return None


async def fetch_tiktok_code(
    email: str,
    password: str,
    proxy: Optional[str] = None,
    *,
    headless: bool = False,
    timeout_s: int = 120,
    exclude: Optional[str] = None,
) -> Optional[str]:
    """Log into Outlook web and return the latest TikTok verification code.

    `exclude` = a code already rejected by TikTok; never return it — poll for a fresh one."""
    proxy_dict = _proxy_settings(proxy)

    # Geo-match the fingerprint to the proxy's exit country (same rationale as registration).
    # Seeded by the mailbox email so the SAME account presents a STABLE fingerprint across
    # logins — a mailbox whose fp changes every read is itself a suspicious signal.
    country = None
    if proxy:
        try:
            from utils.geo import country_for_proxy
            country = await country_for_proxy(proxy)
        except Exception:
            country = None
    fp = get_fingerprint(abs(hash(email)) % 2_000_000_000, country)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            channel="chrome",
            proxy=proxy_dict,
            args=_common_args(fp) + ["--use-fake-ui-for-media-stream"],
        )
        ctx = await browser.new_context(
            user_agent=fp.ua,
            viewport={"width": fp.screen_w, "height": max(400, fp.screen_avail_h - 79)},
            locale=fp.locale,
            timezone_id=fp.timezone,
            extra_http_headers=fp.sec_ch_ua(),
        )
        page = await ctx.new_page()
        await apply_stealth(page, fp, spoof_gpu=False)  # real GPU; JS spoof is a PerimeterX tell
        try:
            await login_outlook(page, email, password)
            # Give poll a way to re-authenticate if Outlook 429s us off the mailbox mid-poll.
            async def _relogin():
                await login_outlook(page, email, password)
            return await poll_code_on_page(page, email, timeout_s, exclude=exclude,
                                           relogin=_relogin)
        except Exception as e:
            await page.screenshot(path="/tmp/ol_reader_debug.png")
            logger.error("fetch_tiktok_code failed for %s: %s | url=%s", email, e, page.url)
            return None
        finally:
            await browser.close()
