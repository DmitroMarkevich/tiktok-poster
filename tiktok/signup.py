"""TikTok account auto-registrar via email (uses a pre-made Outlook mailbox).

Flow (web signup, 2025):
  1. /signup/phone-or-email/email
  2. Birthday (Month / Day / Year custom dropdowns)
  3. Email + password
  4. "Send code" → TikTok emails a 6-digit code → fetched from Outlook → entered
  5. CAPTCHA (slider/rotation) — solved via utils.captcha
  6. Success → save cookies as the account session

The verification code is obtained through `code_fetcher(email)`, an async callable
that returns the 6-digit string (see outlook.reader.fetch_tiktok_code).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import string
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from playwright.async_api import Page

from .browser import create_context, get_page, save_session
from config import CAPTCHA_API_KEY, CAPTCHA_SERVICE

logger = logging.getLogger(__name__)

SIGNUP_URL = "https://www.tiktok.com/signup/phone-or-email/email"

CodeFetcher = Callable[[str], Awaitable[Optional[str]]]


@dataclass
class TikTokCreds:
    username: str
    email: str
    password: str
    session_data: str


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


async def _type_slow(el, text: str):
    await el.click()
    for ch in text:
        await el.type(ch, delay=random.randint(40, 110))


async def _native_click(page: Page, selectors: list[str]) -> bool:
    """Click the first matching TikTok signup button (send-code / final submit), using the click
    method that actually triggers the React onClick for the CURRENT browser:

    - patchright (launched directly): the React handler only runs on the element's in-page native
      `.click()`; Playwright's synthetic MouseEvent / hardware mouse fire NOTHING.
    - antidetect browser over CDP (AdsPower etc.): the OPPOSITE — an in-page `el.click()` is an
      UNTRUSTED event and send_code never fires (confirmed: user's real hardware click sent the
      code, our el.click() did not). A Playwright `locator.click()` here dispatches through the
      CDP Input domain → isTrusted=true, exactly like a real click. So for a provider context we
      use the trusted Playwright click.
    """
    is_provider = getattr(getattr(page, "context", None), "_is_provider", False)

    if is_provider:
        # Antidetect browser over CDP. locator.click() (trusted via CDP Input) reliably fires the
        # send-code button's React handler — use it as the default. The final «Далі» submit is
        # pickier (see _provider_submit, which cycles click methods until the verify XHR fires).
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_enabled(timeout=2_000):
                    await loc.click(timeout=6_000)
                    logger.info("trusted-click hit: %s", sel)
                    return True
            except Exception:
                continue
        logger.error("trusted-click: no enabled selector matched")
        return False


async def _provider_click_mouse(page: Page, sel: str) -> bool:
    """A coordinate-based pointer click (move → down → up) over the button — an alternative the
    submit loop tries when locator.click() doesn't trigger the handler."""
    loc = page.locator(sel).first
    if not (await loc.count() and await loc.is_enabled(timeout=2_000)):
        return False
    await loc.scroll_into_view_if_needed(timeout=3_000)
    box = await loc.bounding_box()
    if not box:
        return False
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    await page.mouse.move(cx - 10, cy - 5, steps=5)
    await page.mouse.move(cx, cy, steps=8)
    await asyncio.sleep(random.uniform(0.05, 0.13))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.05, 0.11))
    await page.mouse.up()
    logger.info("mouse-click hit: %s", sel)
    return True


async def _provider_click_native(page: Page, sel: str) -> bool:
    """In-page native el.click() — the patchright-style fallback, tried last by the submit loop."""
    js = "(s)=>{const e=document.querySelector(s); if(e&&!e.disabled){e.click(); return true;} return false;}"
    try:
        if await page.evaluate(js, sel):
            logger.info("native-click hit: %s", sel)
            return True
    except Exception:
        pass
    return False


async def _provider_click_keyboard(page: Page, sel: str) -> bool:
    """Focus the button and activate it with the keyboard (Enter then Space). A keyboard activation
    on a focused <button> dispatches a real, trusted click event — works on the antidetect browser
    when CDP mouse/locator clicks don't reach TikTok's React handler."""
    loc = page.locator(sel).first
    if not (await loc.count() and await loc.is_enabled(timeout=2_000)):
        return False
    try:
        await loc.scroll_into_view_if_needed(timeout=3_000)
        await loc.focus(timeout=3_000)
        await asyncio.sleep(0.2)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)
        await page.keyboard.press("Space")
        logger.info("keyboard-click hit: %s", sel)
        return True
    except Exception:
        return False

    # patchright path — in-page native .click().
    js = """(sels) => {
        for (const s of sels) {
            const el = document.querySelector(s);
            if (el && !el.disabled) { el.click(); return s; }
        }
        return null;
    }"""
    try:
        hit = await page.evaluate(js, selectors)
        if hit:
            logger.info("native-click hit: %s", hit)
            return True
    except Exception as e:
        logger.error("native-click error: %s", e)
    return False


async def _click_first(page: Page, selectors: list[str], timeout: int = 5_000) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=timeout):
                await el.click()
                return True
        except Exception:
            continue
    return False


async def _pick_dropdown(page: Page, kind: str, trigger_index: int,
                         *, option_index: int | None = None,
                         option_text: str | None = None) -> bool:
    """Pick a TikTok birthday value, LANGUAGE-PROOF.

    The three birthday selects render in order Month / Day / Year as
    `div[role="combobox"]` triggers (class `DivSelector`) — we open them by POSITION
    (`trigger_index`), never by their visible placeholder text, because TikTok localises
    that text ("Month"→"Mois"→"Monat"…) to the browser locale and a text match silently fails.

    Each listbox holds options `<div id="{kind}-options-item-N">` whose `id` prefix
    ("Month"/"Day"/"Year") and ORDER are language-independent, so we pick the option by its
    index (`option_index`, 0-based) — or by numeric text for Day/Year."""
    triggers = page.locator('div[role="combobox"]')
    try:
        trig = triggers.nth(trigger_index)
        await trig.wait_for(state="visible", timeout=5_000)
        await trig.click()
    except Exception as e:
        logger.info("dropdown %s open (idx %s) failed: %s", kind, trigger_index, e)
        return False
    await asyncio.sleep(0.5)

    if option_text is not None:
        # Day / Year are plain NUMBERS — identical in every locale, so match by text and
        # don't depend on listbox order (TikTok lists years descending).
        opt = page.locator(f'div[id^="{kind}-options-item-"]:text-is("{option_text}")').first
    else:
        opt = page.locator(f'div[id^="{kind}-options-item-"]').nth(option_index)
    try:
        await opt.scroll_into_view_if_needed(timeout=3_000)
        await opt.click(timeout=4_000)
        await asyncio.sleep(0.3)
        return True
    except Exception as e:
        logger.info("dropdown %s idx=%s failed: %s", kind, option_index, e)
        return False


async def _wait_and_solve_captcha(page: Page, *, appear_timeout: float = 18.0) -> bool:
    """Wait for TikTok's goofy-captcha (slider/rotation) to actually RENDER, then solve it.

    Clicking "Send code" fires an async chain (age-gate → send_code → captcha SDK paints
    a beat later), so a fixed sleep + immediate solve often runs BEFORE the widget exists —
    the solver then sees "no captcha", returns True, and the flow proceeds without a code.
    Here we poll until the captcha container appears (or `appear_timeout` elapses); if it
    never appears we assume none was required. The free OpenCV solver in utils.captcha
    handles both slider and rotation, so no paid API key is needed."""
    from utils.captcha import solve_captcha_if_present, _find_captcha

    deadline = asyncio.get_event_loop().time() + appear_timeout
    appeared = False
    while asyncio.get_event_loop().time() < deadline:
        _, el = await _find_captcha(page)
        if el:
            appeared = True
            break
        await asyncio.sleep(0.5)

    if not appeared:
        logger.info("No captcha appeared within %.0fs", appear_timeout)
        return True

    logger.info("Captcha appeared — solving (free OpenCV solver)")
    solved = await solve_captcha_if_present(page, CAPTCHA_API_KEY, CAPTCHA_SERVICE, max_attempts=5)
    logger.info("Captcha solve result: %s", solved)
    return solved


async def _set_birthday(page: Page) -> None:
    """TikTok birthday = 3 custom dropdowns (Month / Day / Year)."""
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    year = random.randint(1990, 2002)

    # Month: pick by index (names are localised). Day/Year: pick by numeric text.
    ok_m = await _pick_dropdown(page, "Month", 0, option_index=month - 1)
    ok_d = await _pick_dropdown(page, "Day", 1, option_text=str(day))
    ok_y = await _pick_dropdown(page, "Year", 2, option_text=str(year))
    if not (ok_m and ok_d and ok_y):
        await page.screenshot(path="/tmp/tt_birthday_debug.png", full_page=True)
        logger.info("birthday set: month=%s day=%s year=%s", ok_m, ok_d, ok_y)


def _attach_passport_sniffer(page: Page) -> dict:
    """Capture the bodies of TikTok's passport endpoints so a failure surfaces its REASON.

    The real blocker has been "TikTok никогда не присылает код" — but the flow only inferred
    that postfactum (no sessionid cookie). The actual cause lives in the JSON response of
    `POST /passport/web/email/send_code/` (and the age-gate before it): TikTok returns a
    `message`/`error_code`/`description` there ("internal server error", risk-control, etc).
    We stash the last response per endpoint so the caller can log/raise the concrete reason
    instead of guessing IP vs rate-limit vs email-domain."""
    captured: dict = {}

    async def _on_response(resp):
        url = resp.url
        if "register_verify_login" in url or "register_verify" in url:
            key = "verify"
        elif "send_code" in url or "/passport/web/email/register/" in url:
            key = "send_code"
        elif "/register/verification/age/" in url:
            key = "age"
        elif "/passport/" in url or "/verification/" in url or "captcha" in url:
            # Catch-all so we SEE every auth-related endpoint TikTok hits around the click —
            # if send_code fires under a different path, it shows up here instead of vanishing.
            key = "other:" + url.split("?")[0].split("tiktok")[-1][-60:]
        else:
            return
        try:
            body = await resp.text()
        except Exception as e:
            body = f"<unreadable: {e}>"
        captured[key] = {"status": resp.status, "body": body[:2000]}
        logger.info("passport[%s] HTTP %s | %s", key, resp.status, body[:500])

    page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))
    return captured


async def register_one(
    email: str,
    code_fetcher: CodeFetcher,
    *,
    password: Optional[str] = None,
    proxy: Optional[str] = None,
    account_id: int = 999_999,
    mailbox_password: Optional[str] = None,
    set_avatar: bool = False,
) -> Optional[TikTokCreds]:
    """Register a TikTok account on the given Outlook email. Returns creds on success.

    If `mailbox_password` is given, the verification code is read from a SECOND TAB in this same
    browser (Outlook logged in once, then just reloaded) instead of spawning a separate reader
    browser that re-logs-in every time — much faster and avoids the flaky re-login. Falls back to
    `code_fetcher` if the in-tab login fails. With `set_avatar`, a unique random profile photo is
    uploaded after registration.
    """
    password = password or _gen_password()
    outlook_tab = None  # 2nd tab kept logged into Outlook for fast in-browser code reads

    pw, context = await create_context(account_id, proxy=proxy)
    try:
        from utils.captcha import solve_captcha_if_present
        page = await get_page(context)
        passport = _attach_passport_sniffer(page)
        # Capture JS errors — if TikTok's request-signing SDK (webmssdk / X-Bogus) throws,
        # the send_code XHR is never assembled (no request, no captcha, no email), which looks
        # exactly like "the button does nothing". Surface that instead of guessing.
        page.on("pageerror", lambda e: logger.error("PAGE JS ERROR: %s", str(e)[:300]))
        page.on("console", lambda m: logger.info("console[%s]: %s", m.type, m.text[:200])
                if m.type in ("error", "warning") else None)
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(3)

        # cookie consent — MUST be dismissed or the banner overlays the form's lower half
        # (Send-code button, captcha). Labels are localised, so cover the common locales.
        await _click_first(page, [
            "button:has-text('Allow all')", "button:has-text('Accept all')",
            "button:has-text('Прийняти всі')", "button:has-text('Tout autoriser')",
            "button:has-text('Alle akzeptieren')", "button:has-text('Aceptar todo')",
            "tiktok-cookie-banner button[data-type='accept']",
        ], timeout=4_000)
        await asyncio.sleep(0.6)

        # ── birthday ─────────────────────────────────────────────────────────
        await _set_birthday(page)
        await asyncio.sleep(0.5)

        # ── email + password ─────────────────────────────────────────────────
        email_el = page.locator("input[name='email'], input[type='text']").first
        await email_el.wait_for(timeout=15_000)
        await _type_slow(email_el, email)
        await asyncio.sleep(0.4)

        pwd_el = page.locator("input[type='password']").first
        await pwd_el.wait_for(timeout=10_000)
        await _type_slow(pwd_el, password)
        await asyncio.sleep(0.4)

        # ── open + log into a SECOND Outlook tab (once) ──────────────────────
        # Keep the inbox logged-in alongside the TikTok tab so reading the code is a fast reload,
        # not a brand-new browser + re-login each time. Falls back to code_fetcher if this fails.
        if mailbox_password:
            try:
                from outlook.reader import login_outlook
                outlook_tab = await context.new_page()
                await login_outlook(outlook_tab, email, mailbox_password)
                logger.info("Outlook tab logged in for %s — code reads will be in-browser", email)
                await page.bring_to_front()
            except Exception as e:
                logger.warning("Outlook tab login failed (%s) — falling back to code_fetcher", e)
                try:
                    if outlook_tab:
                        await outlook_tab.close()
                except Exception:
                    pass
                outlook_tab = None

        async def _get_code(exclude: Optional[str] = None):
            """Read the code from the in-browser Outlook tab if we have it, else the external reader.

            `exclude` = a code TikTok already rejected; never hand it back (poll for a fresh one),
            so the heal loop can't re-submit the same dead code over and over."""
            if outlook_tab is not None:
                from outlook.reader import poll_code_on_page
                try:
                    await outlook_tab.bring_to_front()  # foreground so Outlook doesn't throttle
                except Exception:
                    pass
                async def _relogin_tab():
                    from outlook.reader import login_outlook
                    await login_outlook(outlook_tab, email, mailbox_password)
                code = await poll_code_on_page(outlook_tab, email, 170, exclude=exclude,
                                               relogin=_relogin_tab)
                try:
                    await page.bring_to_front()  # back to the TikTok form to enter the code
                except Exception:
                    pass
                return code
            # External fetcher: pass `exclude` through if the callable accepts it; otherwise call it
            # repeatedly and DROP a result equal to `exclude` (never return an already-rejected code).
            import inspect
            try:
                accepts_exclude = "exclude" in inspect.signature(code_fetcher).parameters
            except (ValueError, TypeError):
                accepts_exclude = False
            if accepts_exclude:
                return await code_fetcher(email, exclude=exclude)
            if not exclude:
                return await code_fetcher(email)
            for _ in range(6):  # ~ up to 6 reads waiting for a code != the rejected one
                c = await code_fetcher(email)
                if c and c != exclude:
                    return c
                logger.info("fetcher returned stale/rejected code %s — waiting for a fresh one", exclude)
                await asyncio.sleep(8)
            return None

        # ── send code ────────────────────────────────────────────────────────
        # data-e2e is language-independent; the localized text variants are belt-and-braces.
        # The form fills fine, but the send_code request has been silently NOT firing — so
        # log the button's real state and whether the click landed (disabled button / an
        # overlay intercepting the click are the prime suspects, not the captcha).
        send_btn = page.locator(
            "button[data-e2e='send-code-button'], "
            "button:has-text('Send code'), button:has-text('Envoyer le code'), "
            "button:has-text('Надіслати код'), button:has-text('Code senden')"
        ).first
        try:
            await send_btn.wait_for(state="visible", timeout=8_000)
            disabled = await send_btn.is_disabled()
            # Confirm React actually holds the field values (a DOM value set without firing
            # React onChange leaves state empty → onClick validation no-ops with no request).
            try:
                ev = await page.locator("input[name='email'], input[type='text']").first.input_value()
                pv = await page.locator("input[type='password']").first.input_value()
                logger.info("field check: email=%r pwd_len=%d", ev, len(pv))
            except Exception:
                pass
            logger.info("send-code button: visible=True disabled=%s", disabled)
        except Exception as e:
            logger.error("send-code button not found: %s", e)
            disabled = True

        # Send the code. On the antidetect browser a single locator.click() is FLAKY — it fires
        # send_code some runs and silently nothing on others (same React-handler quirk as «Далі»).
        # So cycle click METHODS (locator → mouse → native) until the send_code XHR is actually
        # observed. CRUCIAL: only advance to the next method when NO request fired yet — once a
        # send_code response appears we stop, so we never trigger two codes (the 2nd invalidates
        # the 1st). A captcha may render between click and the actual send, so solve it each round.
        send_sel = "button[data-e2e='send-code-button']"
        send_methods = [
            ("locator", lambda p, s: _native_click(p, [s])),
            ("mouse", _provider_click_mouse),
            ("keyboard", _provider_click_keyboard),
            ("native", _provider_click_native),
        ]
        for attempt in range(5):
            if passport.get("send_code") is not None:
                break
            name, method = send_methods[attempt % len(send_methods)]
            logger.info("send-code attempt %d via %s", attempt + 1, name)
            try:
                await method(page, send_sel)
            except Exception as e:
                logger.warning("send-code %s failed: %s", name, e)
            await asyncio.sleep(2)
            await _wait_and_solve_captcha(page)
            for _ in range(10):
                if passport.get("send_code") is not None:
                    break
                await asyncio.sleep(0.5)
            if passport.get("send_code") is not None:
                logger.info("send_code fired (attempt %d via %s)", attempt + 1, name)
                break
            logger.warning("send-code attempt %d (%s): no send_code XHR — next method", attempt + 1, name)
        await asyncio.sleep(1)
        await page.screenshot(path="/tmp/tt_after_sendcode.png", full_page=True)

        # If TikTok rejected send_code (internal error / risk-control), no email is ever sent —
        # surface the concrete reason from the response body instead of waiting on the reader.
        sc = passport.get("send_code")
        if sc is not None:
            body = sc["body"]
            ok = '"error_code":0' in body or '"status_code":0' in body or '"message":"success"' in body
            if not ok:
                logger.error("send_code REJECTED for %s | HTTP %s | %s",
                             email, sc["status"], body)
        else:
            logger.warning("send_code response never observed for %s "
                           "(captcha not solved or request not fired)", email)

        # ── fetch + enter code ───────────────────────────────────────────────
        code = await _get_code()
        if not code:
            await page.screenshot(path="/tmp/tt_signup_nocode.png", full_page=True)
            reason = passport.get("send_code", {}).get("body", "<no send_code response seen>")
            logger.error("No verification code for %s | send_code: %s", email, reason)
            return None

        code_el = page.locator(
            "input[name='code'], input[placeholder*='code'], input[placeholder*='код'], input[maxlength='6']"
        ).first
        await code_el.wait_for(timeout=10_000)
        await _type_slow(code_el, code)
        await asyncio.sleep(0.4)
        # Confirm React actually registered the code. el.type() fires real keystrokes, but if the
        # value didn't land (focus lost, paste-guard) the "Далі" button stays disabled and we'd
        # click the wrong submit. Re-key via fill+input event if the field doesn't hold the code.
        try:
            cur = await code_el.input_value()
        except Exception:
            cur = ""
        if cur.strip() != code:
            logger.warning("code field holds %r, not %r — re-entering", cur, code)
            await code_el.fill("")
            await code_el.click()
            await code_el.type(code, delay=70)
            await asyncio.sleep(0.4)

        # ── submit signup ────────────────────────────────────────────────────
        # The previous code fell straight to a generic `button[type='submit']` fallback when the
        # real signup button hadn't enabled yet — that hit the WRONG button and register_verify_login
        # never fired (no sessionid, no error_code, just silence). Now: wait for the REAL signup
        # button to enable, click it, and CONFIRM the verify XHR actually went out — retry if not.
        submit_sels = [
            "button[data-e2e='signup-button']",
            "button:has-text('Sign up'):not([disabled])",
            "button:has-text('Зареєструватися'):not([disabled])",
            "button:has-text('Далі'):not([disabled])",
            "button:has-text('Next'):not([disabled])",
            "button:has-text('Continue'):not([disabled])",
        ]
        # Wait (up to ~8s) for the real signup button to become enabled before clicking.
        for _ in range(16):
            try:
                btn = page.locator("button[data-e2e='signup-button']").first
                if await btn.count() and await btn.is_enabled():
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        passport.pop("verify", None)  # clear any stale capture before we click

        async def _verify_fired() -> bool:
            return "verify" in passport

        # DIAGNOSTIC: pause for a manual click to isolate click-mechanism from page/secsdk gating.
        if os.environ.get("TT_MANUAL_SUBMIT") == "1":
            logger.warning("MANUAL SUBMIT MODE — click «Далі» yourself now; watching for verify (180s)")
            for _ in range(360):
                if await _verify_fired():
                    logger.info("register_verify_login fired via MANUAL click")
                    break
                await asyncio.sleep(0.5)
            else:
                logger.error("MANUAL: register_verify_login still never fired after 180s")
            await asyncio.sleep(3)
            submitted = await _verify_fired()
            for sel in ("[role='alert']", "[class*='error-text']", "[class*='ErrorText']"):
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=800):
                        logger.error("MANUAL post-submit error: %r", (await el.inner_text())[:200])
                except Exception:
                    pass
            if passport.get("verify"):
                logger.error("MANUAL verify body: %s", passport.get("verify"))
            await page.screenshot(path="/tmp/tt_manual_submit.png", full_page=True)
            return None

        # Resolve which submit selector is actually present+enabled, then cycle click METHODS on it
        # until the verify XHR fires. The «Далі» React handler ignored locator.click() in testing
        # (zero XHR) yet fired on a real hardware click — so we try locator.click → coordinate mouse
        # click → in-page native click, confirming via the register_verify_login response each time.
        async def _pick_submit_sel() -> Optional[str]:
            for s in submit_sels:
                try:
                    loc = page.locator(s).first
                    if await loc.count() and await loc.is_enabled(timeout=1_500):
                        return s
                except Exception:
                    continue
            return None

        async def _try_click(method, sel) -> bool:
            try:
                return await method(page, sel)
            except Exception as e:
                logger.warning("submit click method failed: %s", e)
                return False

        submitted = False
        methods = [
            ("locator", lambda p, s: _native_click(p, [s])),
            ("mouse", _provider_click_mouse),
            ("keyboard", _provider_click_keyboard),
            ("native", _provider_click_native),
        ]
        for attempt in range(6):
            sel = await _pick_submit_sel()
            if not sel:
                logger.warning("submit: no enabled signup button yet (attempt %d)", attempt + 1)
                await asyncio.sleep(1.5)
                continue
            name, method = methods[attempt % len(methods)]
            logger.info("submit attempt %d via %s on %s", attempt + 1, name, sel)
            await _try_click(method, sel)
            # Wait for the verify XHR to actually leave the page.
            for _ in range(14):
                if await _verify_fired():
                    submitted = True
                    break
                await asyncio.sleep(0.5)
            if submitted:
                logger.info("register_verify_login fired (attempt %d via %s)", attempt + 1, name)
                break
            logger.warning("submit attempt %d (%s): no verify XHR — trying next method", attempt + 1, name)
            try:
                await code_el.press("Enter")
            except Exception:
                pass
            await asyncio.sleep(1.0)
        if not submitted:
            logger.error("submit: register_verify_login never fired after all methods for %s", email)
        await asyncio.sleep(4)

        # ── self-heal a rejected code ────────────────────────────────────────
        # error_code 1704 (invalid) / 1705 (expired) = the code we typed was wrong, NOT a device
        # block. Happens when the mailbox holds a stale code or propagation lagged. Resend a fresh
        # code, re-read the NEWEST one, retype and resubmit — up to twice.
        rejected_code = code  # the code TikTok just rejected — never type it again
        for heal in range(2):
            vb = (passport.get("verify") or {}).get("body", "")
            if not ('"error_code":1704' in vb or '"error_code":1705' in vb):
                break
            logger.warning("verify rejected code %s (%s) — resending + waiting for a NEW code (heal %d)",
                           rejected_code, "expired" if '1705' in vb else "invalid", heal + 1)
            await _native_click(page, [
                "button:has-text('Надіслати код повторно')",
                "button:has-text('Resend')", "button:has-text('Renvoyer')",
                "button[data-e2e='send-code-button']",
            ])
            await asyncio.sleep(3)
            # Demand a code DIFFERENT from the one just rejected. Re-submitting the same code is
            # what burned all the verify attempts ("Кількість спроб максимальна") — _get_code now
            # polls until a genuinely fresh code lands (or returns None).
            new_code = await _get_code(exclude=rejected_code)
            if not new_code or new_code == rejected_code:
                logger.error("heal %d: no NEW code (still %s) — aborting heal instead of re-typing "
                             "a dead code", heal + 1, rejected_code)
                break
            rejected_code = new_code  # if THIS one also gets rejected, exclude it next round too
            try:
                await code_el.click()
                await code_el.fill("")
                await code_el.type(new_code, delay=70)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning("heal retype failed: %s", e)
            passport.pop("verify", None)
            sel = await _pick_submit_sel()
            if sel:
                await _native_click(page, [sel])
            for _ in range(14):
                if await _verify_fired():
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(3)

        # CAPTCHA again after submit
        await _wait_and_solve_captcha(page, appear_timeout=12.0)
        await asyncio.sleep(3)

        # ── set username ─────────────────────────────────────────────────────
        # After a successful verify, TikTok shows a "Create username" screen with a pre-filled
        # suggestion (e.g. user5650348851812) and a confirm button. Replace it with our own handle
        # and confirm — otherwise the account keeps the throwaway userNNNN name.
        chosen_username = await _set_username(page, email)
        if chosen_username:
            logger.info("username set to %s", chosen_username)
            await asyncio.sleep(3)

        # ── set a unique avatar ──────────────────────────────────────────────
        if set_avatar:
            try:
                from utils.avatar import get_unique_avatar
                img = await get_unique_avatar()
                if img:
                    ok = await _set_avatar(page, img)
                    logger.info("avatar set=%s (%s)", ok, img)
            except Exception as e:
                logger.warning("avatar step failed for %s: %s", email, e)

        # Capture any inline error TikTok shows next to the code field (e.g. "code expired
        # or incorrect", "internal server error") — this is the concrete reason a submit fails
        # while still landing on tiktok.com as a guest, which the cookie check alone can't tell.
        # Scope to real error/alert containers only — a broad `div:has-text('expir')` also
        # matches unrelated homepage feed text once we've navigated, logging a bogus error.
        for sel in ("[role='alert']", "[class*='error-text']", "[class*='ErrorText']",
                    "[class*='-error']", "[class*='Error-']"):
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=800):
                    txt = (await el.inner_text()).strip()
                    if txt:
                        logger.error("post-submit error for %s: %r", email, txt[:200])
                        break
            except Exception:
                continue
        await page.screenshot(path="/tmp/tt_post_submit.png", full_page=True)

        # ── success check + username ─────────────────────────────────────────
        try:
            await page.wait_for_url(
                lambda url: "signup" not in url and "login" not in url,
                timeout=30_000,
            )
        except Exception:
            pass

        await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)

        # DEFINITIVE success signal: a real logged-in TikTok session sets a `sessionid`
        # cookie. The absence-of-login-button heuristic gives FALSE POSITIVES — a failed
        # registration ("internal server error") still lands on tiktok.com as a GUEST with
        # no login button, so we'd wrongly save a dead account that doesn't actually exist.
        cookies = await context.cookies()
        names = {c.get("name") for c in cookies}
        if not ({"sessionid", "sid_tt", "sessionid_ss"} & names):
            await page.screenshot(path="/tmp/tt_signup_failed.png", full_page=True)
            logger.error("TikTok signup FAILED (no sessionid cookie) for %s | url=%s | cookies=%s "
                         "| verify=%s | send_code=%s | age=%s",
                         email, page.url, sorted(names),
                         passport.get("verify"), passport.get("send_code"), passport.get("age"))
            return None

        username = chosen_username or await _read_username(page) or email.split("@")[0]
        session_data = await save_session(context)
        logger.info("TikTok registered: %s (%s)", username, email)
        # Optional: keep the SAME browser/profile open so the account can be inspected in the very
        # device+proxy it was born on (logging in from a different device instantly flags it).
        if os.environ.get("TT_KEEP_OPEN") == "1":
            try:
                await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                pass
            logger.warning("TT_KEEP_OPEN: leaving browser open for %s — Ctrl-C to close", username)
            try:
                await asyncio.sleep(3600)
            except Exception:
                pass
        return TikTokCreds(username=username, email=email, password=password,
                           session_data=session_data)
    except Exception as e:
        try:
            page = await get_page(context)
            await page.screenshot(path="/tmp/tt_signup_exception.png", full_page=True)
        except Exception:
            pass
        logger.error("TikTok signup failed for %s: %s", email, e)
        return None
    finally:
        try:
            if outlook_tab is not None:
                await outlook_tab.close()
        except Exception:
            pass
        try:
            await context.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass
        # If an antidetect profile backed this run, stop + delete it (free-tier conveyor:
        # cycle the profile slot each registration). No-op for plain patchright.
        try:
            from .browser import teardown_device
            await teardown_device(context)
        except Exception:
            pass


def _gen_username(email: str) -> str:
    """A plausible TikTok handle: email local part (alnum/underscore/dot only) + random digits,
    clamped to TikTok's 2–24 char limit."""
    base = "".join(c for c in email.split("@")[0].lower() if c.isalnum() or c in "._")
    base = base[:16] or "user"
    return f"{base}{random.randint(100, 99999)}"[:24]


async def _set_username(page: Page, email: str) -> Optional[str]:
    """On the post-verify 'Create username' screen, replace TikTok's suggested handle with ours
    and confirm. Returns the username we set, or None if the screen never appeared."""
    sels = ("input[name='username']", "input[placeholder*='sername']",
            "input[placeholder*='користувача']", "input[maxlength='24']")
    field = None
    for sel in sels:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=4_000):
                field = loc
                break
        except Exception:
            continue
    if field is None:
        return None  # no username screen (already assigned / different flow)
    username = _gen_username(email)
    try:
        await field.click()
        # Clear TikTok's pre-filled suggestion before typing ours.
        await field.fill("")
        await field.type(username, delay=random.randint(40, 100))
        await asyncio.sleep(0.6)
    except Exception as e:
        logger.warning("username field type failed: %s", e)
        return None
    # Confirm. The button on the username screen is labelled «Реєстрація» (Sign up) — NOT «Далі» —
    # so include it, else nothing gets clicked, the screen idles and the sub-step token expires
    # ("Сеанс входу скінчився"). Cycle click methods like the main submit (locator → mouse → native).
    confirm_sels = [
        "button[data-e2e='signup-button']:not([disabled])",
        "button:has-text('Реєстрація'):not([disabled])",
        "button:has-text('Зареєструватися'):not([disabled])",
        "button:has-text('Sign up'):not([disabled])",
        "button:has-text('Confirm'):not([disabled])",
        "button[type='submit']:not([disabled])",
    ]
    for sel in confirm_sels:
        try:
            loc = page.locator(sel).first
            if not (await loc.count() and await loc.is_enabled(timeout=1_500)):
                continue
        except Exception:
            continue
        for method in (lambda p, s: _native_click(p, [s]), _provider_click_mouse,
                       _provider_click_keyboard, _provider_click_native):
            try:
                await method(page, sel)
            except Exception:
                continue
            await asyncio.sleep(2)
            # Confirmed once we leave the username screen (the field is gone) or land logged-in.
            try:
                if not await field.is_visible(timeout=1_500):
                    return username
            except Exception:
                return username
        break
    await asyncio.sleep(2)
    return username


async def _set_avatar(page: Page, image_path: str) -> bool:
    """Upload a profile photo on TikTok web: open Edit profile, feed the file input, confirm the
    crop, and save. Best-effort + language-proof (matches the confirm/save buttons by several
    locales). Returns True if it got through the save step."""
    # Open the profile editor.
    try:
        await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)
    except Exception:
        pass
    opened = await _click_first(page, [
        "[data-e2e='edit-profile-entrance']",
        "button:has-text('Edit profile')", "button:has-text('Редагувати профіль')",
        "div:has-text('Edit profile')", "a:has-text('Edit profile')",
    ], timeout=6_000)
    if not opened:
        # Try via the profile page directly.
        try:
            await page.goto("https://www.tiktok.com/profile", wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(2)
            await _click_first(page, ["[data-e2e='edit-profile-entrance']",
                                      "button:has-text('Edit profile')",
                                      "button:has-text('Редагувати профіль')"], timeout=6_000)
        except Exception:
            pass
    await asyncio.sleep(2)

    # Feed the avatar file input (it's usually hidden; set_input_files works without a click).
    fed = False
    for sel in ("input[type='file']", "input[accept*='image']"):
        try:
            fi = page.locator(sel).first
            if await fi.count():
                await fi.set_input_files(image_path)
                fed = True
                break
        except Exception:
            continue
    if not fed:
        logger.warning("avatar: no file input found")
        return False
    await asyncio.sleep(2)

    # Confirm the crop dialog, then save the profile (buttons localised → match several).
    await _native_click(page, [
        "button:has-text('Apply'):not([disabled])", "button:has-text('Застосувати'):not([disabled])",
        "button:has-text('Confirm'):not([disabled])", "button:has-text('Підтвердити'):not([disabled])",
        "[data-e2e='profile-edit-upload-confirm']",
    ]) if getattr(getattr(page, "context", None), "_is_provider", False) else await _click_first(page, [
        "button:has-text('Apply')", "button:has-text('Застосувати')",
        "button:has-text('Confirm')", "button:has-text('Підтвердити')",
    ], timeout=5_000)
    await asyncio.sleep(2)
    saved = await _click_first(page, [
        "[data-e2e='edit-profile-save']",
        "button:has-text('Save'):not([disabled])", "button:has-text('Зберегти'):not([disabled])",
        "button:has-text('Save')", "button:has-text('Зберегти')",
    ], timeout=6_000)
    await asyncio.sleep(2)
    return saved or fed


async def _read_username(page: Page) -> Optional[str]:
    try:
        await page.goto("https://www.tiktok.com/profile", wait_until="domcontentloaded", timeout=20_000)
        await asyncio.sleep(2)
        el = page.locator("[data-e2e='user-title'], [data-e2e='profile-username']").first
        if await el.is_visible(timeout=4_000):
            return (await el.inner_text()).strip().lstrip("@")
    except Exception:
        pass
    return None
