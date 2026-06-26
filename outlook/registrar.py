"""Outlook (Hotmail) account auto-registrar via Playwright.

Current Microsoft signup flow (2025):
  1. Enter full email (@outlook.com) → Next
  2. Enter password → Next
  3. Choose Country + Birthdate (custom dropdowns) → Next
  4. Captcha (Arkose Labs) — pause for manual solve via Telegram
  5. Redirect to outlook.com = success
"""

from __future__ import annotations

import asyncio
import logging
import random
import string
from dataclasses import dataclass
from typing import Optional

try:
    from patchright.async_api import async_playwright, BrowserContext, Page
except ImportError:
    from playwright.async_api import async_playwright, BrowserContext, Page

from tiktok.browser import _common_args, _parse_proxy as _split_proxy
from tiktok.device_provider import get_provider
from utils.fingerprint import get_fingerprint
from utils.stealth import apply_stealth
from utils.humanize import human_move, human_type, human_click, new_persona

logger = logging.getLogger(__name__)

SIGNUP_URL = "https://signup.live.com/signup"
INBOX_URL = "https://outlook.live.com/mail/0/"

NAMES_FIRST = [
    "Oleksandr", "Dmytro", "Andriy", "Mykola", "Vasyl", "Ivan", "Serhiy",
    "Bohdan", "Taras", "Ruslan", "Artem", "Yevhen", "Vitaliy", "Roman",
    "Olena", "Natalia", "Iryna", "Yulia", "Oksana", "Tetyana", "Svitlana",
]
NAMES_LAST = [
    "Kovalenko", "Shevchenko", "Bondarenko", "Kravchenko", "Tkachenko",
    "Petrenko", "Moroz", "Lysenko", "Marchenko", "Savchenko", "Ponomarenko",
    "Kovalchuk", "Melnyk", "Karpenko", "Hrytsenko", "Rudenko",
]


@dataclass
class OutlookCreds:
    email: str
    password: str
    first_name: str
    last_name: str
    birth_year: int
    proxy: Optional[str] = None
    oauth_token: Optional[str] = None  # OAuth2 refresh_token for browserless IMAP code reads


def _gen_password(length: int = 14) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    pwd = (
        random.choice(string.ascii_uppercase)
        + random.choice(string.ascii_lowercase)
        + random.choice(string.digits)
        + random.choice("!@#$%")
        + "".join(random.choices(chars, k=length - 4))
    )
    return "".join(random.sample(pwd, len(pwd)))


def _gen_email_local(first: str, last: str) -> str:
    suffix = random.randint(1000, 9999)
    sep = random.choice([".", "_", ""])
    return f"{first.lower()}{sep}{last.lower()}{suffix}"


def _proxy_settings(proxy_str: str | None) -> dict | None:
    """Build Playwright's native `proxy=` dict, reusing the TikTok-side parser so every
    supported format (host:port, host:port:user:pass, scheme://user:pass@host:port) is
    handled identically — credentials MUST go through proxy= (not Chrome args) or Chrome
    fails the CONNECT tunnel with 407 / ERR_TUNNEL_CONNECTION_FAILED."""
    server, creds = _split_proxy(proxy_str)
    if not server:
        return None
    settings = {"server": f"http://{server}"}
    if creds:
        settings["username"] = creds["username"]
        settings["password"] = creds["password"]
    return settings


async def _slow_type(page: Page, selector: str, text: str):
    # Focus the field the way a person does — curved cursor move + click on the actual input —
    # then type with persona-driven cadence (variable rhythm, thinking pauses, rare typos).
    # The old fixed 0.05–0.15s/char loop with an instant page.click was a mechanical signature.
    loc = page.locator(selector).first
    if not await human_click(page, loc):
        await page.click(selector, timeout=5_000)
    await human_type(page, text)


async def _click_submit(page: Page):
    # Drive a real curved cursor path to the button (the ABSENCE of a mousemove stream
    # before a click is a strong behavioural bot tell), THEN dispatch via locator.click().
    # We do NOT use a raw coordinate click here: Microsoft overlays a <label> on top of the
    # submit button (same reason _select_dropdown_option needs force=), so a pixel click can
    # land on the overlay and silently fail to submit — locator.click() targets the element.
    btn = page.locator('button[type="submit"]')
    try:
        await btn.wait_for(state="visible", timeout=8_000)
        box = await btn.bounding_box()
        if box:
            await human_move(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await asyncio.sleep(random.uniform(0.1, 0.3))
    except Exception:
        pass
    await btn.click(timeout=8_000)


async def _select_dropdown_index(page: Page, btn_selector: str, index: int):
    """Open a Fluent UI combobox and click the option at position `index` (0-based).

    We select by POSITION, never by localized text: the signup UI language follows the
    browser locale (a de-DE fingerprint → German month names «Januar…»), so matching a
    Ukrainian/English month string silently times out. The option ORDER (Jan→Dec, 1→31)
    is identical in every language, so an index is locale-proof. force=True because a
    <label> overlays the combobox button."""
    await page.click(btn_selector, force=True, timeout=5_000)
    await asyncio.sleep(0.6)
    await page.locator('[role="option"]').nth(index).click(timeout=8_000)


async def _press_and_hold_target(page: Page):
    """Return (frame, bounding_box) for the PerimeterX/HUMAN press-and-hold widget.

    The widget lives in an iframe on iframe.hsprotect.net; inside it the clickable
    area is #px-captcha. We resolve the bbox in *page viewport* coordinates so we can
    drive it with raw mouse events (more human-like than element.click)."""
    for f in page.frames:
        if "hsprotect" in (f.url or ""):
            try:
                loc = f.locator("#px-captcha")
                await loc.wait_for(state="visible", timeout=4_000)
                box = await loc.bounding_box()
                if box and box["width"] > 0:
                    return f, box
            except Exception:
                continue
    return None, None


async def _captcha_still_present(page: Page) -> bool:
    """True while the press-and-hold challenge is still on screen."""
    for f in page.frames:
        if "hsprotect" in (f.url or ""):
            try:
                loc = f.locator("#px-captcha")
                if await loc.is_visible(timeout=1_500):
                    return True
            except Exception:
                pass
    # also catch the visible Ukrainian/English hold prompt on the host page
    try:
        txt = page.locator(':text("утримуйте"), :text("Press and hold"), :text("Press & Hold")')
        if await txt.first.is_visible(timeout=1_000):
            return True
    except Exception:
        pass
    return False


async def _quick_captcha_present(page: Page) -> bool:
    """Fast, non-blocking check of whether #px-captcha is still up (short timeouts).
    Used to poll for completion DURING the hold without stalling the drift loop."""
    for f in page.frames:
        if "hsprotect" in (f.url or ""):
            try:
                if await f.locator("#px-captcha").is_visible(timeout=300):
                    return True
            except Exception:
                pass
    return False


async def _hold_drift(page: Page, cx: float, cy: float, max_hold: float) -> bool:
    """Hold the button while drifting the cursor organically, and RELEASE as soon as the
    challenge clears (or after max_hold).

    Two things make this pass where the old fixed-time hold failed on an otherwise clean IP:
      1) Adaptive duration — PerimeterX fills a progress ring and dismisses the widget on
         completion. A human releases right AFTER it completes, not at a hardcoded 4.5–9s.
         So we poll and lift the moment the widget disappears.
      2) Correlated drift — real hand tremor is a smooth low-frequency wander, not the
         per-step white-noise jitter we emitted before. We integrate a damped random walk so
         the motion has momentum (a detector that looks at the velocity spectrum sees 1/f-ish
         drift, not uniform noise)."""
    vx = vy = dx = dy = 0.0
    elapsed = 0.0
    last_poll = 0.0
    while elapsed < max_hold:
        step = random.uniform(0.08, 0.18)
        await asyncio.sleep(step)
        elapsed += step
        # damped random walk → smooth, correlated micro-motion with momentum
        vx = vx * 0.82 + random.uniform(-0.5, 0.5)
        vy = vy * 0.82 + random.uniform(-0.5, 0.5)
        dx = max(-3.5, min(3.5, dx + vx))
        dy = max(-3.5, min(3.5, dy + vy))
        try:
            await page.mouse.move(cx + dx, cy + dy, steps=1)
        except Exception:
            pass
        # poll for completion ~every 0.4s, but only after the ring has had time to fill
        if elapsed > 2.0 and elapsed - last_poll > 0.4:
            last_poll = elapsed
            if not await _quick_captcha_present(page):
                # human reaction lag between "ring full" and lifting the finger
                await asyncio.sleep(random.uniform(0.12, 0.38))
                return True
    return False


async def _solve_press_and_hold(page: Page, attempts: int = 3) -> bool:
    """Attempt the PerimeterX press-and-hold gesture with human-like motion.

    Returns True if the challenge disappears. Reliability still depends on IP/fingerprint
    reputation — but on a borderline-clean IP (where a human hold passes) the adaptive hold
    + organic drift below is what closes the gap vs. the old fixed-duration gesture."""
    for attempt in range(attempts):
        frame, box = await _press_and_hold_target(page)
        if not box:
            return not await _captcha_still_present(page)

        # Press slightly off dead-centre — humans don't land pixel-perfect.
        cx = box["x"] + box["width"] / 2 + random.uniform(-4, 4)
        cy = box["y"] + box["height"] / 2 + random.uniform(-3, 3)

        # Approach along a real curved Bézier path (persona-aware), then a brief aim dwell.
        await human_move(page, cx, cy)
        await asyncio.sleep(random.uniform(0.18, 0.5))

        await page.mouse.down()
        # Slow PX variants fill the ring in up to ~10s; give it headroom, release on completion.
        cleared = await _hold_drift(page, cx, cy, max_hold=random.uniform(9.0, 13.0))
        await page.mouse.up()

        await asyncio.sleep(random.uniform(1.2, 2.5))
        if cleared or not await _captcha_still_present(page):
            logger.info("Press-and-hold solved on attempt %d", attempt + 1)
            return True
        logger.info("Press-and-hold attempt %d failed, retrying", attempt + 1)
        # Varied, longer backoff — instant re-press is itself a non-human signature.
        await asyncio.sleep(random.uniform(2.5, 5.0))

    return False


async def register_one(
    proxy: Optional[str] = None,
    *,
    captcha_callback=None,
    headless: bool = False,
) -> Optional[OutlookCreds]:
    """Register a single Outlook account. Returns credentials on success.

    captcha_callback: async callable(page, email) — called when captcha appears.
    """
    first = random.choice(NAMES_FIRST)
    last = random.choice(NAMES_LAST)
    email_local = _gen_email_local(first, last)
    email = f"{email_local}@outlook.com"
    password = _gen_password()
    birth_year = random.randint(1988, 2000)
    birth_month_idx = random.randint(0, 11)
    birth_day = random.randint(1, 28)

    proxy_dict = _proxy_settings(proxy)

    # Resolve the proxy's EXIT country so locale/timezone/languages match the IP — a UA
    # proxy must look like a UA user, not a hardcoded uk-UA browser on a German exit. Then
    # build a full geo-matched fingerprint (per-registration seed → each account distinct).
    country = None
    if proxy:
        try:
            from utils.geo import country_for_proxy
            country = await country_for_proxy(proxy)
        except Exception:
            country = None
    seed = random.randint(1, 2_000_000_000)
    fp = get_fingerprint(seed, country)
    # Give this registration its own behavioural personality (typing speed, pause/typo rate,
    # mouse jitter) so its action-timing DISTRIBUTION differs from every other account, not
    # just the per-action noise. Seeded by the same value as the fingerprint → stable persona.
    new_persona(seed)

    # If an antidetect browser (AdsPower/Dolphin) is configured, each registration runs on a
    # genuinely DISTINCT device over CDP — the single biggest lever against HUMAN/PerimeterX,
    # which otherwise clusters every Outlook signup as the same host device + JA3. Else fall
    # back to plain patchright on this host (single device).
    provider = get_provider()
    device_handle = None

    async with async_playwright() as pw:
        if provider is not None:
            # Provider owns the fingerprint at the native (C++/kernel) level — distinct canvas/
            # WebGL/audio/WebRTC per profile, tz/locale derived from the proxy IP. We get a ready
            # context (Camoufox) or attach over CDP (AdsPower/Dolphin) and apply NO JS stealth.
            from tiktok.device_provider import open_provider_context
            ctx, device_handle = await open_provider_context(provider, pw, seed, proxy, country)
            page: Page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        else:
            # patchright patches CDP detection signals; channel="chrome" runs the REAL Chrome
            # binary so the TLS/H2 fingerprint matches the spoofed UA (bundled Chromium has a
            # different JA3 — a cross-check tell). _common_args adds the WebRTC-leak guards so
            # RTCPeerConnection can't expose the server's real IP past the proxy.
            browser = await pw.chromium.launch(
                headless=headless,
                channel="chrome",
                proxy=proxy_dict,
                args=_common_args(fp) + ["--use-fake-ui-for-media-stream"],
            )
            ctx = await browser.new_context(
                user_agent=fp.ua,
                # innerHeight is always smaller than the screen (tab strip + omnibox eat ~79px);
                # viewport == screen height would make innerHeight > availHeight, a bot tell.
                viewport={"width": fp.screen_w, "height": max(400, fp.screen_avail_h - 79)},
                locale=fp.locale,
                timezone_id=fp.timezone,
                extra_http_headers=fp.sec_ch_ua(),  # Client-Hints consistent with UA/platform
            )
            page = await ctx.new_page()
            # Inject the JS stealth layer (navigator.webdriver, canvas/WebGL, RTCPeerConnection
            # neutering, masked toString) — the single biggest lever against HUMAN's scoring.
            # Real Chrome on a real GPU → don't JS-spoof canvas/WebGL (it's a PerimeterX tell here).
            await apply_stealth(page, fp, spoof_gpu=False)

        try:
            # ── Step 1: open signup, enter full email ─────────────────────────
            # Flaky datacenter proxies often time out the first navigation; retry rather than
            # failing the whole registration on a single 30s goto timeout.
            for gattempt in range(4):
                try:
                    await page.goto(SIGNUP_URL, timeout=30_000, wait_until="domcontentloaded")
                    break
                except Exception as e:
                    if gattempt == 3:
                        raise
                    logger.warning("registrar goto SIGNUP_URL timed out (try %d): %s — retrying",
                                   gattempt + 1, e)
                    await asyncio.sleep(3)
            await asyncio.sleep(random.uniform(1.5, 2.5))

            await page.wait_for_selector('input[name="email"]', timeout=15_000)
            await _slow_type(page, 'input[name="email"]', email)
            await asyncio.sleep(0.5)
            await _click_submit(page)
            await asyncio.sleep(random.uniform(2.0, 3.0))

            # ── Step 2: password ──────────────────────────────────────────────
            try:
                await page.wait_for_selector('input[type="password"]', timeout=10_000)
                await _slow_type(page, 'input[type="password"]', password)
                await asyncio.sleep(0.4)
                await _click_submit(page)
                await asyncio.sleep(random.uniform(2.0, 3.0))
            except Exception as e:
                await page.screenshot(path="/tmp/ol_debug_pwd.png")
                logger.error("Password step failed: %s | URL: %s", e, page.url)
                return None

            # ── Step 3: birthdate (Fluent UI comboboxes) ──────────────────────
            try:
                await page.wait_for_selector('#BirthMonthDropdown', timeout=10_000)

                # Select by position (locale-proof): month idx 0=January, day list starts at 1.
                await _select_dropdown_index(page, '#BirthMonthDropdown', birth_month_idx)
                await asyncio.sleep(0.4)

                await _select_dropdown_index(page, '#BirthDayDropdown', birth_day - 1)
                await asyncio.sleep(0.4)

                await page.fill('input[name="BirthYear"]', str(birth_year))
                await asyncio.sleep(0.4)

                await _click_submit(page)
                await asyncio.sleep(random.uniform(2.0, 3.5))
            except Exception as e:
                await page.screenshot(path="/tmp/ol_debug_bday.png")
                logger.error("Birthdate step failed: %s | URL: %s", e, page.url)
                return None

            # ── Step 4: name (optional step — appears after birthdate) ───────
            try:
                name_inputs = page.locator('input[type="text"]')
                await name_inputs.first.wait_for(timeout=5_000)
                inputs_count = await name_inputs.count()
                if inputs_count >= 1:
                    await _slow_type(page, 'input[type="text"]:first-of-type', first)
                    await asyncio.sleep(0.3)
                if inputs_count >= 2:
                    await page.locator('input[type="text"]').nth(1).fill(last)
                    await asyncio.sleep(0.3)
                await _click_submit(page)
                await asyncio.sleep(random.uniform(2.0, 3.0))
            except Exception:
                pass  # step not present

            # ── Step 6: captcha (optional) ────────────────────────────────────
            # Microsoft uses HUMAN Security (hsprotect.net) press-and-hold captcha.
            # Behavioral analysis makes automation unreliable — pause for manual solve.
            # The captcha iframe (hsprotect.net) attaches a beat AFTER the name step submits —
            # a single synchronous frame scan races it and misses (the form then sits unsubmitted
            # and Step 7 times out with no "Captcha detected" log, exactly what we saw). Poll for
            # up to ~12s for either the iframe OR the host-page "press and hold" prompt (localized).
            captcha_detected = False
            for _ in range(12):
                for frame in page.frames:
                    u = frame.url or ""
                    if "hsprotect" in u or "arkoselabs" in u or "funcaptcha" in u or "perimeterx" in u:
                        captcha_detected = True
                        break
                if captcha_detected:
                    break
                try:
                    if await page.locator(
                        'iframe[src*="hsprotect"], iframe[src*="arkoselabs"], '
                        ':text("Press and hold"), :text("утримуйте"), #px-captcha'
                    ).first.is_visible(timeout=1_000):
                        captcha_detected = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0)

            if captcha_detected:
                logger.info("Captcha detected for %s — trying auto press-and-hold", email)
                solved = await _solve_press_and_hold(page)
                if not solved:
                    logger.info("Auto-solve failed for %s — falling back to manual", email)
                    if captcha_callback:
                        await captcha_callback(page, email)
                    else:
                        await asyncio.sleep(180)
                await asyncio.sleep(3.0)

            # ── Step 6.5: dismiss the passkey / FIDO enrollment ──────────────
            # After signup Microsoft often pushes login.microsoft.com/consumers/fido/create
            # ("Use Touch ID to sign in?"), which auto-pops a NATIVE OS Touch ID dialog the bot
            # cannot click — the flow then stalls forever on that URL (this is exactly what broke
            # the no-proxy run). The account is ALREADY created at this point, so just leave the
            # passkey page: click the web "Skip for now" if present, else navigate to the inbox.
            try:
                for _ in range(2):
                    if "fido" not in page.url.lower() and "passkey" not in page.url.lower():
                        break
                    clicked = False
                    for sel in ('button:has-text("Skip for now")', 'a:has-text("Skip for now")',
                                'button:has-text("Пропустити")', ':text("Skip for now")',
                                'button:has-text("Maybe later")', '#iShowSkip', '[data-testid="secondaryButton"]'):
                        try:
                            await page.locator(sel).first.click(timeout=2_000)
                            clicked = True
                            logger.info("FIDO/passkey skipped via %s for %s", sel, email)
                            break
                        except Exception:
                            continue
                    if not clicked:
                        await page.goto(INBOX_URL, timeout=30_000, wait_until="domcontentloaded")
                        logger.info("FIDO/passkey bypassed by navigating to inbox for %s", email)
                    await asyncio.sleep(2.5)
            except Exception:
                pass

            # ── Step 7: success check ─────────────────────────────────────────
            # Microsoft's anti-abuse engine can BLOCK creation after the captcha
            # ("Створення облікового запису заблоковано" / "unusual activity").
            # Detect it explicitly so we never persist a non-existent mailbox.
            try:
                blocked = await page.locator(
                    ':text("заблоковано"), :text("unusual activity"), '
                    ':text("незвичну активність"), :text("can\'t create")'
                ).first.is_visible(timeout=2_000)
            except Exception:
                blocked = False
            if blocked:
                await page.screenshot(path="/tmp/ol_debug_blocked.png")
                logger.warning("Account creation BLOCKED by Microsoft for %s "
                               "(flagged IP/automation — use a residential proxy)", email)
                return None

            # Real success = we actually land on the mailbox / account dashboard.
            # NOTE: a bare login.live.com / signup.live.com URL is NOT success.
            try:
                await page.wait_for_url(
                    lambda url: "outlook.live.com" in url
                    or "outlook.com/mail" in url
                    or "account.microsoft.com" in url,
                    timeout=60_000,
                )
                logger.info("Registered OK: %s | final_url=%s", email, page.url)
            except Exception:
                current = page.url
                await page.screenshot(path="/tmp/ol_debug_final.png")
                logger.warning("Registration did not complete for %s | final_url=%s",
                               email, current)
                return None

            # Provision + verify the MAILBOX. Landing on a privacy-notice / account.microsoft.com
            # page means the ACCOUNT exists, but the outlook.com MAILBOX may not be provisioned
            # yet — a fresh-session login then fails with "we couldn't find your account" and the
            # whole TikTok pipeline stalls. Open the inbox IN THIS authenticated session: it both
            # triggers provisioning and confirms the mailbox is real before we return success.
            try:
                await page.goto(INBOX_URL, timeout=45_000, wait_until="domcontentloaded")
                await page.wait_for_selector(
                    'div[role="option"], [aria-label*="Створити"], [aria-label*="New mail"], '
                    'div[role="listbox"]',
                    timeout=45_000,
                )
                logger.info("Mailbox provisioned + verified: %s", email)
                await asyncio.sleep(2)
            except Exception:
                await page.screenshot(path="/tmp/ol_debug_mailbox.png")
                logger.warning("Mailbox not provisioned for %s | url=%s (account made but no "
                               "usable inbox — discarding)", email, page.url)
                return None

            # Grab an OAuth2 refresh_token NOW, while this session is authenticated, so every later
            # TikTok-code read goes over IMAP (no browser re-login → no Outlook 429). Best-effort:
            # if consent doesn't complete we fall back to the web reader at read time.
            oauth_token = None
            try:
                from outlook.oauth import capture_refresh_token, enable_imap_setting
                # Enable IMAP on the fresh mailbox (fixes "authenticated but not connected"), then
                # grab the token — both while the session is authenticated & not yet IP-flagged.
                try:
                    await enable_imap_setting(page)
                except Exception as e:
                    logger.warning("enable_imap step failed for %s: %s", email, e)
                oauth_token = await capture_refresh_token(page, proxy, email=email, password=password)
            except Exception as e:
                logger.warning("OAuth refresh_token capture failed for %s: %s", email, e)

            return OutlookCreds(
                email=email,
                password=password,
                first_name=first,
                last_name=last,
                birth_year=birth_year,
                proxy=proxy,
                oauth_token=oauth_token,
            )

        except Exception as e:
            await page.screenshot(path="/tmp/ol_debug_exception.png")
            logger.error("Registration failed for %s: %s", email, e)
            return None
        finally:
            if device_handle is not None:
                # Stop + delete the throwaway antidetect profile (conveyor teardown).
                try:
                    await provider.close_device(device_handle, delete=True)
                except Exception:
                    pass
            else:
                await browser.close()
