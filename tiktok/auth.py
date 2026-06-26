import asyncio
from playwright.async_api import BrowserContext
from .browser import get_page, save_session
from config import CAPTCHA_API_KEY, CAPTCHA_SERVICE


TIKTOK_LOGIN_URL = "https://www.tiktok.com/login/phone-or-email/email"

_CAPTCHA_SELECTORS = [
    "[data-testid='captcha-verify']",
    "#captcha-verify",
    "[class*='secsdk-captcha']",
    "[class*='captcha-verify']",
]

# Actual field selectors confirmed from live page inspection
_USERNAME_SELECTORS = [
    "input[name='username']",
    "input[placeholder*='mail']",
    "input[placeholder*='sername']",
    "input[type='text']",
]
_PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[placeholder*='assword']",
]
_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button[data-e2e='login-button']",
    "button:has-text('Log in')",
    "button:has-text('Увійти')",
]


async def login(context: BrowserContext, email: str, password: str) -> str:
    """Logs in with email/password, auto-solves CAPTCHA via OpenCV or service."""
    from utils.captcha import solve_captcha_if_present, CaptchaImageInterceptor

    page = await get_page(context)

    # Attach network interceptor BEFORE navigation to capture CAPTCHA images
    interceptor = CaptchaImageInterceptor(page)
    await interceptor.attach()

    # Clear all cookies first so stale sessions don't redirect us away from login
    await context.clear_cookies()

    # domcontentloaded is reliable; networkidle hangs on TikTok due to analytics
    await page.goto(TIKTOK_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(3)

    # Accept cookie consent banner if present
    for btn_text in ("Allow all", "Accept all", "Прийняти всі"):
        try:
            btn = page.locator(f"button:has-text('{btn_text}')").first
            if await btn.is_visible(timeout=2_000):
                await btn.click()
                await asyncio.sleep(1)
                break
        except Exception:
            pass

    # If redirected away from login (e.g. already logged in somehow), navigate back
    if "login" not in page.url:
        await page.goto(TIKTOK_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(3)

    # Find and fill username
    username_el = None
    for sel in _USERNAME_SELECTORS:
        el = page.locator(sel).first
        if await el.is_visible(timeout=5_000):
            username_el = el
            break
    if username_el is None:
        await page.screenshot(path="/tmp/tiktok_login_debug.png", full_page=True)
        raise RuntimeError(
            "Не знайдено поле email/username на сторінці логіну.\n"
            "Скріншот: /tmp/tiktok_login_debug.png"
        )

    await username_el.click()
    await asyncio.sleep(0.3)
    await username_el.type(email, delay=40)
    await asyncio.sleep(0.5)

    # Find and fill password
    password_el = None
    for sel in _PASSWORD_SELECTORS:
        el = page.locator(sel).first
        if await el.is_visible(timeout=5_000):
            password_el = el
            break
    if password_el is None:
        raise RuntimeError("Не знайдено поле пароля на сторінці логіну.")

    await password_el.click()
    await asyncio.sleep(0.3)
    await password_el.type(password, delay=40)
    await asyncio.sleep(0.5)

    # Submit
    for sel in _SUBMIT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=3_000):
                await btn.click()
                break
        except Exception:
            continue

    await asyncio.sleep(4)

    # Detect rate-limit / too many attempts
    for err_text in ("Maximum number of attempts", "Too many attempts", "Try again later"):
        if await page.locator(f"text={err_text}").count() > 0:
            raise RuntimeError(
                "TikTok заблокував спробу входу: забагато спроб поспіль.\n"
                "Зачекай 30–60 хвилин і спробуй знову,\n"
                "або скористайся «🍪 Вставити cookies»."
            )

    # Solve CAPTCHA if it appeared
    captcha_visible = False
    for sel in _CAPTCHA_SELECTORS:
        if await page.locator(sel).count() > 0:
            captcha_visible = True
            break

    if captcha_visible:
        solved = await solve_captcha_if_present(page, CAPTCHA_API_KEY, CAPTCHA_SERVICE)
        if not solved:
            if not CAPTCHA_API_KEY:
                raise RuntimeError(
                    "Не вдалося розв'язати CAPTCHA (OpenCV не знайшов зображень).\n"
                    "Додай CAPTCHA_API_KEY у .env або скористайся «🍪 Вставити cookies»."
                )
            raise RuntimeError(
                "Не вдалося розв'язати CAPTCHA автоматично.\n"
                "Спробуй ще раз або скористайся «🍪 Вставити cookies»."
            )
        await asyncio.sleep(3)

    await interceptor.detach()

    # Verify login
    try:
        await page.wait_for_url(
            lambda url: "login" not in url,
            timeout=15_000,
        )
    except Exception:
        if "login" in page.url:
            await page.screenshot(path="/tmp/tiktok_login_failed.png", full_page=True)
            raise RuntimeError(
                "Вхід не вдався — перевір email і пароль.\n"
                "Скріншот: /tmp/tiktok_login_failed.png"
            )

    return await save_session(context)


async def verify_logged_in(context: BrowserContext) -> bool:
    page = await get_page(context)
    await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30_000)
    await asyncio.sleep(3)
    login_btn = await page.locator("[data-e2e='top-login-button']").count()
    return login_btn == 0


async def verify_logged_in_robust(context: BrowserContext, attempts: int = 3,
                                  delay: float = 2.5) -> bool:
    """verify_logged_in with retries — only False if EVERY attempt fails.

    A single check flakes constantly (slow load, transient redirect, a captcha on the check
    page); a false negative makes the user re-import perfectly good cookies. One success at
    any attempt means the session is fine. Shared by the health loop AND the manual
    «Перевірити сесії» path so neither can declare a live account dead on one flaky read."""
    for attempt in range(attempts):
        try:
            if await verify_logged_in(context):
                return True
        except Exception:
            pass
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
    return False
