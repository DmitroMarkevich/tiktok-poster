from __future__ import annotations
import asyncio
import json
import os
from playwright.async_api import BrowserContext, Frame
from .browser import get_page

UPLOAD_URLS = [
    "https://www.tiktok.com/tiktokstudio/upload",
    "https://www.tiktok.com/creator-center/upload",
]

# Selectors for the clickable upload zone
UPLOAD_ZONE_SELECTORS = [
    "[class*='upload-btn']",
    "[class*='upload-card']",
    "[class*='drag-upload']",
    "[class*='UploaderContainer']",
    "[class*='upload-area']",
    "div[class*='upload'] svg",
    "div[class*='Upload'] svg",
    "label[class*='upload']",
]

CAPTION_SELECTORS = [
    "[data-e2e='caption-input']",
    ".public-DraftEditor-content",
    "div[contenteditable='true']",
    "[class*='caption'] [contenteditable]",
    "[class*='editor'] [contenteditable]",
]

POST_BTN_SELECTORS = [
    "button[data-e2e='post_video_button']",
    "button[data-e2e='post-button']",
    "button:has-text('Post')",
    "button:has-text('Publish')",
    "button:has-text('Опублікувати')",
]


async def _debug_screenshot(page, name: str):
    try:
        path = f"/tmp/tiktok_debug_{name}.png"
        await page.screenshot(path=path, full_page=True)
        print(f"[DEBUG] Screenshot saved: {path}")
    except Exception:
        pass


async def upload_video(
    context: BrowserContext,
    video_path: str,
    caption: str = "",
    hashtags: str = "",
    privacy: str = "public",
) -> None:
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    # Fake icon_info → can_show=true so TikTok's post flow doesn't wait forever
    # for the cover thumbnail (which never loads in headless mode).
    _icon_info_body = json.dumps({
        "status_code": 0, "status_msg": "",
        "data": {"can_show": True, "is_cache": False},
        "extra": {"fatal_item_ids": [], "logid": "fake"},
        "log_pb": {"impr_id": "fake"},
    })
    async def _fake_icon_info(route):
        if "icon_info" in route.request.url:
            await route.fulfill(status=200, content_type="application/json",
                                body=_icon_info_body)
        else:
            await route.continue_()

    await context.route("**/tiktok_creator/**", _fake_icon_info)
    await context.route("**/tiktok/web/**",      _fake_icon_info)

    page = await get_page(context)

    # Navigate to upload page
    for url in UPLOAD_URLS:
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(4)
            if resp and resp.ok:
                break
        except Exception:
            continue

    await _debug_screenshot(page, "01_after_goto")

    # Dismiss "Continue editing?" draft banner + confirmation dialog if present
    try:
        discard = page.locator("button:has-text('Discard')").first
        if await discard.is_visible(timeout=3_000):
            await discard.click()
            await asyncio.sleep(1)
            # TikTok may show a second confirmation "Discard this post?"
            confirm = page.locator("button:has-text('Discard')").first
            if await confirm.is_visible(timeout=2_000):
                await confirm.click()
                await asyncio.sleep(1)
    except Exception:
        pass

    # Detect login page redirect — session expired or missing
    current_url = page.url
    if "login" in current_url or await page.locator("[data-e2e='top-login-button']").count() > 0:
        raise RuntimeError(
            "Акаунт не залогінений у TikTok.\n"
            "Зайди в меню акаунту → 🍪 Вставити cookies або 🔑 Перевірити вхід."
        )

    # Get the upload frame (iframe or main frame)
    upload_frame = await _get_upload_frame(page)

    await _debug_screenshot(page, "02_got_frame")

    # --- Upload file via filechooser interception ---
    # This is more reliable than finding hidden input[type=file]
    file_set = False

    # Method 1: filechooser event while clicking upload zone
    for selector in UPLOAD_ZONE_SELECTORS:
        try:
            zone = upload_frame.locator(selector).first
            visible = await zone.is_visible()
            if not visible:
                continue
            async with page.expect_file_chooser(timeout=8_000) as fc_info:
                await zone.click()
            fc = await fc_info.value
            await fc.set_files(video_path)
            file_set = True
            break
        except Exception:
            continue

    # Method 2: try hidden input directly via JS evaluation
    if not file_set:
        try:
            # Make all file inputs visible and accessible
            await page.evaluate("""
                document.querySelectorAll('input[type=file]').forEach(el => {
                    el.style.display = 'block';
                    el.style.visibility = 'visible';
                    el.style.opacity = '1';
                    el.style.width = '10px';
                    el.style.height = '10px';
                });
            """)
            # Also try inside iframes
            for frame in page.frames:
                try:
                    await frame.evaluate("""
                        document.querySelectorAll('input[type=file]').forEach(el => {
                            el.style.display = 'block';
                            el.removeAttribute('hidden');
                        });
                    """)
                except Exception:
                    pass

            await asyncio.sleep(1)
            file_input = page.locator("input[type='file']").first
            await file_input.set_input_files(video_path, timeout=10_000)
            file_set = True
        except Exception:
            pass

    if not file_set:
        await _debug_screenshot(page, "03_file_not_set")
        raise RuntimeError(
            "Не вдалося знайти зону завантаження.\n"
            "Скріншот збережено: /tmp/tiktok_debug_03_file_not_set.png\n"
            "Перевір чи акаунт залогінений через 'Перевірити вхід'."
        )

    await _debug_screenshot(page, "04_file_set")

    # Wait for Post button to appear AND become truly enabled.
    # TikTok uses data-disabled="true"/aria-disabled="true" (not HTML disabled) while
    # server-side transcoding and cover generation are still running.
    for _ in range(90):  # 90 × 2s = 3 min max
        await asyncio.sleep(2)
        try:
            ready = await page.evaluate("""
                (() => {
                    const btn = document.querySelector('[data-e2e="post_video_button"]')
                             || document.querySelector('[data-e2e="post-button"]');
                    if (!btn) return false;
                    return btn.getAttribute('data-disabled') !== 'true'
                        && btn.getAttribute('aria-disabled') !== 'true'
                        && !btn.disabled;
                })()
            """)
            if ready:
                break
        except Exception:
            pass

    # Fill caption
    full_caption = caption
    if hashtags:
        tags = " ".join(f"#{t.strip().lstrip('#')}" for t in hashtags.split())
        full_caption = f"{caption} {tags}".strip()

    if full_caption:
        for selector in CAPTION_SELECTORS:
            try:
                el = upload_frame.locator(selector).first
                await el.wait_for(state="visible", timeout=5_000)
                await el.click()
                await asyncio.sleep(0.3)
                # Use type instead of fill for contenteditable
                await el.type(full_caption, delay=30)
                break
            except Exception:
                continue

    await asyncio.sleep(1)

    # Privacy
    privacy_map = {"public": 0, "friends": 1, "private": 2}
    privacy_index = privacy_map.get(privacy, 0)
    try:
        privacy_buttons = upload_frame.locator("[data-e2e='radio-item']")
        if await privacy_buttons.count() > privacy_index:
            await privacy_buttons.nth(privacy_index).click()
    except Exception:
        pass

    await asyncio.sleep(0.5)

    # Dismiss any modal dialogs / tutorial overlays that block the Post button.
    _DISMISS_TEXTS = ("Got it", "Later", "Not now", "Skip", "Close")
    for _ in range(5):
        dismissed = False
        for text in _DISMISS_TEXTS:
            try:
                btn = page.locator(f"button:has-text('{text}')").first
                if await btn.is_visible(timeout=1_500):
                    await btn.click()
                    await asyncio.sleep(1)
                    dismissed = True
                    break
            except Exception:
                pass
        if not dismissed:
            break

    # Remove react-joyride tutorial overlay — it intercepts all pointer events and
    # never auto-dismisses in headless mode.
    await page.evaluate("""
        const portal = document.getElementById('react-joyride-portal');
        if (portal) portal.remove();
        document.querySelectorAll('.react-joyride__overlay, [data-test-id="overlay"]')
            .forEach(el => el.remove());
    """)
    await asyncio.sleep(0.5)

    await _debug_screenshot(page, "05_before_post")

    # Click Post via JS dispatchEvent — bypasses any remaining visual overlays
    # while honouring React's synthetic event system.
    posted = False
    try:
        result = await page.evaluate("""
            (() => {
                const btn = document.querySelector('[data-e2e="post_video_button"]')
                         || document.querySelector('[data-e2e="post-button"]');
                if (!btn) return false;
                btn.scrollIntoView();
                btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                return true;
            })()
        """)
        if result:
            posted = True
    except Exception:
        pass

    # Fallback: Playwright locator click with force=True to ignore overlays
    if not posted:
        for selector in POST_BTN_SELECTORS:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2_000):
                    await btn.click(force=True, timeout=5_000)
                    posted = True
                    break
            except Exception:
                continue

    if not posted:
        raise RuntimeError("Не знайдено кнопку 'Post'. Скріншот: /tmp/tiktok_debug_05_before_post.png")

    await _debug_screenshot(page, "06_after_click")

    # Wait for Post to complete.
    # TikTok uses async_post=1 for this account — the video is queued on the server
    # immediately after click, but the browser shows a spinner without redirecting.
    # Success = URL changes OR spinner runs ≥30s without a VISIBLE error toast.
    # Failure = a visible "Something went wrong" toast appears.
    # NOTE: page.locator("text=…").count() finds HIDDEN elements too (false positive).
    #       Always use .filter(visible=True) for error detection.
    error_found = False
    spinner_ticks = 0

    for tick in range(24):  # 24 × 5s = 120s max
        await asyncio.sleep(5)

        # Success: page navigated to content/manage
        cur = page.url
        if any(kw in cur for kw in ("content", "manage", "/profile", "studio/post")):
            break

        # Check for VISIBLE error toast only
        for err in ["Something went wrong", "Щось пішло не так",
                    "replace it with a different video"]:
            try:
                if await page.locator(f"text={err}").filter(visible=True).count() > 0:
                    error_found = True
                    break
            except Exception:
                pass
        if error_found:
            await _debug_screenshot(page, "06_error_state")
            break

        # Track spinner state
        try:
            btn_text = await page.evaluate("""
                (() => {
                    const btn = document.querySelector('[data-e2e="post_video_button"]');
                    if (!btn) return 'GONE';
                    return btn.innerText.trim() || 'SPINNER';
                })()
            """)
        except Exception:
            btn_text = "SPINNER"

        if btn_text in ("SPINNER", "GONE", ""):
            spinner_ticks += 1
            # After 30s of spinner with no visible error → async post was accepted
            if spinner_ticks * 5 >= 30:
                break
        else:
            # Spinner stopped (button shows text again), no error → done
            break

    # Clean up route interception
    try:
        await context.unroute("**/tiktok_creator/**", _fake_icon_info)
        await context.unroute("**/tiktok/web/**",      _fake_icon_info)
    except Exception:
        pass

    if error_found:
        raise RuntimeError(
            "TikTok відхилив відео: 'Something went wrong'.\n"
            "Можливі причини:\n"
            "• Акаунт потребує підтвердження телефону\n"
            "• Відео вже є в профілі (дублікат)\n"
            "• TikTok виявив автоматизацію\n"
            "Скріншот: /tmp/tiktok_debug_06_error_state.png"
        )

    await asyncio.sleep(2)


async def _get_upload_frame(page) -> Frame:
    await asyncio.sleep(1)

    # Try real Frame objects first
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        if any(kw in (frame.url or "") for kw in ("upload", "creator", "studio")):
            return frame

    # Try first iframe via content_frame
    try:
        iframe_el = page.locator("iframe").first
        await iframe_el.wait_for(state="attached", timeout=10_000)
        frame = await iframe_el.content_frame()
        if frame:
            return frame
    except Exception:
        pass

    return page.main_frame
