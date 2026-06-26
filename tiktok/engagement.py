"""Post-run engagement: reply to a couple of comments under our own latest video.

Async port of multicombine's engagement.py. After a commenting run, an account that
*also* replies on its own video looks like a real creator tending their page — extra
organic signal (SEO/reach) and a believable activity pattern, not just a one-way
comment-spraying bot. Best-effort: any failure is swallowed, it never breaks a run.
"""
from __future__ import annotations
import asyncio
import random
from playwright.async_api import BrowserContext

from utils.ai import generate_reply
from tiktok.commenter import _human_type

_VIDEO_SELECTORS = (
    'div[data-e2e="user-post-item"] a[href*="/video/"]',
    'a[href*="/video/"]',
)
_COMMENT_TEXT_SELECTORS = (
    'p[data-e2e="comment-text"]',
    'div[data-e2e="comment-level-1"] p',
    'span[class*="SpanCommentContentV2"]',
)


async def reply_to_latest_video_comments(context: BrowserContext, max_replies: int = 2) -> int:
    """Open our own profile, find the latest video, reply to 1-2 of its comments via AI.
    Returns the number of replies posted (0 on any problem)."""
    replied = 0
    page = await context.new_page()
    try:
        await page.goto("https://www.tiktok.com/@me",
                        wait_until="domcontentloaded", timeout=20_000)
        await asyncio.sleep(random.uniform(2, 3))

        href = ""
        for sel in _VIDEO_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    href = await el.get_attribute("href") or ""
                    if href:
                        break
            except Exception:
                continue
        if not href:
            return 0

        url = href if href.startswith("http") else f"https://www.tiktok.com{href}"
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await asyncio.sleep(random.uniform(2, 3))

        comments: list[str] = []
        for sel in _COMMENT_TEXT_SELECTORS:
            try:
                els = await page.locator(sel).all()
                texts = []
                for e in els[:5]:
                    if await e.is_visible():
                        texts.append((await e.inner_text()).strip())
                if texts:
                    comments = [t for t in texts if t]
                    break
            except Exception:
                continue
        if not comments:
            return 0

        reply_btns = await page.locator('span[data-e2e="comment-reply-btn"]').all()
        # Reply to comments by POSITION so the AI reply matches the comment under whose
        # reply button it's posted. Sampling texts independently of buttons (the old code)
        # misaligned them — reply generated for comment X landed under comment Y.
        n = min(len(comments), len(reply_btns))
        if n == 0:
            return 0
        idxs = random.sample(range(n), min(max_replies, n))
        for idx in idxs:
            comment = comments[idx]
            reply_text = await generate_reply(comment)
            if not reply_text:
                continue
            try:
                await reply_btns[idx].click()
                await asyncio.sleep(random.uniform(0.8, 1.5))
                inp = page.locator('div[contenteditable="true"]').first
                # Type like a human (per-keystroke delay + thinking pauses) instead of an
                # instant fill() — fill fires no keydown/keyup and is a behavioural tell.
                await inp.click()
                await _human_type(page, reply_text)
                await asyncio.sleep(random.uniform(0.5, 1.0))
                await inp.press("Enter")
                await asyncio.sleep(random.uniform(1.5, 2.5))
                replied += 1
            except Exception:
                continue
    except Exception as e:
        print(f"[engagement] {type(e).__name__}: {e}", flush=True)
    finally:
        try:
            await page.close()
        except Exception:
            pass
    return replied
