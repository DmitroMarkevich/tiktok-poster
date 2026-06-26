"""Shadowban detection — verify a posted comment is visible to OTHER viewers.

The commenter's `appeared_locally` success signal only proves the comment rendered in
the AUTHOR's own session. A shadowbanned account still sees its own comment there, so
"OK" can be a false positive. This module re-opens the video in a LOGGED-OUT (guest)
context and checks whether the comment text is actually present for a third party.

Verdict:
  True  → visible to guests (survived)
  False → not found for a guest (shadowbanned / server-filtered)
  None  → inconclusive (page/comments failed to load) — do NOT penalise the account
"""
from __future__ import annotations

import asyncio
import re
import shutil
from typing import Optional

from tiktok.browser import create_guest_context

# Hard ceiling per video check (seconds). goto already caps at 30s, but a guest-side
# redirect/challenge or a slow proxy can stall the scroll/eval phase past that.
_CHECK_TIMEOUT = 40


def _signature(text: str) -> str:
    """A short, normalised fingerprint of the comment for substring matching. The full
    text is unreliable (TikTok truncates long comments with '… more' and collapses
    whitespace), so we match the first ~40 visible chars instead."""
    norm = re.sub(r"\s+", " ", (text or "").strip())
    return norm[:40]


async def _open_comments(page) -> None:
    """Make the comment list visible: click the comment icon if it isn't open yet."""
    for sel in ("[data-e2e='comment-icon']", "[data-e2e='comment-count']"):
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await asyncio.sleep(1.5)
                return
        except Exception:
            continue


async def _comment_list_text(page) -> str:
    """Scroll the comment list a few times to lazy-load comments, return its text."""
    text = ""
    for _ in range(4):
        try:
            text = await page.evaluate(
                "() => (document.querySelector(\"[data-e2e='comment-list']\")"
                " || document.body).innerText"
            )
        except Exception:
            text = text or ""
        try:
            lst = page.locator("[data-e2e='comment-list']").first
            await lst.evaluate("el => el.scrollBy(0, 600)")
        except Exception:
            try:
                await page.mouse.wheel(0, 600)
            except Exception:
                pass
        await asyncio.sleep(0.8)
    return re.sub(r"\s+", " ", text or "")


async def _check_one(page, video_url: str, comment_text: str) -> Optional[bool]:
    sig = _signature(comment_text)
    if not sig:
        return None
    if not video_url.startswith("http"):
        video_url = "https://www.tiktok.com" + video_url
    try:
        await page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return None  # couldn't load — inconclusive, don't blame the account
    await asyncio.sleep(2)
    await _open_comments(page)
    haystack = await _comment_list_text(page)
    if not haystack or len(haystack) < 5:
        return None  # comments never loaded — inconclusive
    return sig in haystack


async def verify_comments_visible(proxy: Optional[str], items: list) -> dict:
    """Batch-check a run's comments from a single guest context.

    items: list of (video_url, comment_text). Returns {video_url: verdict} where verdict
    is True / False / None. One throwaway guest browser is reused for the whole batch;
    a per-item failure never aborts the rest.
    """
    results: dict = {}
    if not items:
        return results
    pw = ctx = None
    try:
        pw, ctx = await create_guest_context(proxy)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for video_url, comment_text in items:
            try:
                # Hard per-video cap. A slow proxy or a guest-side redirect/challenge can
                # otherwise make a single check run for minutes despite goto's own
                # timeout — bound it so verification never dominates the run.
                results[video_url] = await asyncio.wait_for(
                    _check_one(page, video_url, comment_text), timeout=_CHECK_TIMEOUT
                )
            except Exception:
                results[video_url] = None  # timeout or error → inconclusive
    except Exception:
        # Whole guest context failed to launch — everything inconclusive.
        for video_url, _ in items:
            results.setdefault(video_url, None)
    finally:
        profile = getattr(ctx, "_guest_profile_dir", None) if ctx else None
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        if profile:
            shutil.rmtree(profile, ignore_errors=True)
    return results
