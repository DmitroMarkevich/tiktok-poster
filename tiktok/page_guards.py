"""Cheap pre-comment guards on the open video page.

Both checks are early-exit filters run right after a video loads, BEFORE we spend time
opening the comment panel / typing:

• is_live_stream  — LIVE pages have a different comment UI and commenting there is both
  flaky and pointless; skip them.
• has_target_language — posting a Ukrainian comment under an unrelated foreign-language
  video is wasted effort AND a spam signal (irrelevant comment → shadow-filter trigger).
  Mirrors warmup's own cyrillic check so the commenter applies the same relevance gate.
"""
from __future__ import annotations
import re
from playwright.async_api import Page

_CYRILLIC_RE = re.compile(r'[а-яА-ЯіІїЇєЄґҐ]')

_DESC_SELECTORS = (
    'div[data-e2e="browse-video-desc"]', 'span[data-e2e="browse-video-desc"]',
    'h1[data-e2e="browse-video-desc"]', 'div[data-e2e="video-desc"]',
    'div[class*="VideoDescContainer"]', 'div[class*="video-desc"]',
)

# Only precise data-e2e markers — broad class/aria selectors would false-positive on a
# normal video that merely has a LIVE button in the nav.
_LIVE_SELECTORS = (
    'button[data-e2e="LivePage-GetCoin"]',
    'div[data-e2e="live-room"]',
    'div[data-e2e="live-room-info"]',
    'div[data-e2e="live-room-chat"]',
)


async def is_live_stream(page: Page) -> bool:
    """True if the current page is a LIVE broadcast (URL is the most reliable tell)."""
    try:
        if "/live" in page.url:
            return True
    except Exception:
        pass
    for sel in _LIVE_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


async def has_cyrillic_description(page: Page) -> bool:
    """True if the video description contains Cyrillic (target-language relevance gate).

    Returns True when no description is found — a missing desc shouldn't block an
    otherwise-fine video (safe fallback, same as warmup)."""
    for sel in _DESC_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                text = (await el.inner_text(timeout=2000)).strip()
                if text:
                    return bool(_CYRILLIC_RE.search(text))
        except Exception:
            continue
    return True
