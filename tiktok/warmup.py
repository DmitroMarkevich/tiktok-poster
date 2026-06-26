"""TikTok account warmup via an organic state machine — async port.

Ported from multicombine's warmup.py (sync + ADS Power CDP) onto this project's
async Playwright + per-account cookie context. Simulates organic browsing so a fresh
or cold account ages naturally before it does uploads/comments — the single biggest
lever against the shadow-filter.

States: FEED → WATCHING → (COMMENTS | BACK | PROFILE_VISIT | FOLLOW) → … → DONE.
Routes: "search" | "hashtag" | "foryou".

Philosophy (kept from the original): if a CAPTCHA/block appears, the account is being
scrutinised — STOP and back off rather than fight it. No captcha-solving here.
"""
import asyncio
import enum
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

from playwright.async_api import BrowserContext, Page

from .browser import get_page

# Safe topic pools for warmup (no financial/crypto/banned terms — don't burn the
# account during warmup itself).
SAFE_WARMUP_TOPICS = [
    "продуктивність", "студентське життя", "підприємництво", "саморозвиток",
    "особистий розвиток", "тайм-менеджмент", "навчання онлайн", "мотивація цілі",
    "здоровий спосіб життя", "кар'єра і розвиток",
]

_WARMUP_EMPTY_SELECTORS = (
    '[data-e2e="no-result"]',
    'div[data-e2e="search-no-result"]',
    'div[class*="EmptyState"]',
    'div[class*="DivNoResultsContainer"]',
)

_CYRILLIC_RE = re.compile(r'[а-яА-ЯіІїЇєЄґҐ]')

_DESC_SELECTORS = [
    'div[data-e2e="browse-video-desc"]', 'span[data-e2e="browse-video-desc"]',
    'h1[data-e2e="browse-video-desc"]', 'div[data-e2e="video-desc"]',
    'div[class*="VideoDescContainer"]', 'div[class*="video-desc"]',
]

_LOGIN_SELECTORS = ('[data-e2e="top-login-button"]', 'a[href="/login"]',
                    'div[data-e2e="login-button"]')

_CAPTCHA_SELECTORS = (
    'div[class*="captcha_verify"]', 'div[class*="secsdk-captcha"]',
    'div[class*="puzzle-captcha"]', 'div[data-e2e="captcha"]',
    'iframe[src*="captcha"]',
)


def _log(label, msg):
    print(f"[warmup] [{label}] {msg}", flush=True)


# ── low-level async helpers ─────────────────────────────────────────────────

async def _safe_goto(page: Page, url: str) -> bool:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        return True
    except Exception:
        return False


async def _is_logged_in(page: Page) -> bool:
    for sel in _LOGIN_SELECTORS:
        try:
            if await page.locator(sel).first.count() > 0:
                return False
        except Exception:
            pass
    return True


async def _detect_block(page: Page) -> Optional[str]:
    for sel in _CAPTCHA_SELECTORS:
        try:
            if await page.locator(sel).first.count() > 0:
                return "captcha"
        except Exception:
            pass
    return None


async def _organic_sleep(lo: float, hi: float, ctx: "WarmupContext" = None) -> None:
    """Sleep a random duration in micro-chunks (responsive to stop_event)."""
    total = random.uniform(lo, hi)
    chunks = random.randint(2, 5)
    chunk = total / chunks
    for _ in range(chunks):
        if ctx is not None and ctx.should_stop():
            return
        await asyncio.sleep(max(0.0, chunk + random.uniform(-chunk * 0.1, chunk * 0.1)))


async def _detect_empty_page(page: Page) -> bool:
    for sel in _WARMUP_EMPTY_SELECTORS:
        try:
            if await page.locator(sel).first.count() > 0:
                return True
        except Exception:
            pass
    return False


async def _has_cyrillic_description(page: Page) -> bool:
    for sel in _DESC_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                text = (await el.inner_text(timeout=2000)).strip()
                if text:
                    return bool(_CYRILLIC_RE.search(text))
        except Exception:
            continue
    return True  # unknown → don't skip


async def _try_swipe_down(page: Page) -> None:
    try:
        await page.keyboard.press("ArrowDown")
    except Exception:
        pass
    if random.random() < 0.4:
        try:
            await page.mouse.wheel(0, random.randint(80, 260))
        except Exception:
            pass


async def _is_video_element_visible(page: Page) -> bool:
    for sel in ('div[data-e2e="browse-video"]', 'div[class*="DivVideoPlayerContainer"]', 'video'):
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                return True
        except Exception:
            continue
    return False


async def _try_like(page: Page, probability: float = 0.18) -> bool:
    if random.random() >= probability:
        return False
    try:
        await page.mouse.dblclick(random.randint(350, 650), random.randint(250, 550))
        return True
    except Exception:
        return False


async def _try_open_comments(page: Page) -> bool:
    for sel in ('[data-e2e="comment-icon"]', 'button[aria-label*="comment"]',
                'span[data-e2e="comment-icon"]'):
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


async def _try_close_comments(page: Page) -> None:
    for sel in ('[data-e2e="comment-close"]', 'button[aria-label*="Close"]',
                'button[aria-label*="Закрити"]'):
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                await btn.click(timeout=2000)
                return
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _try_visit_profile(page: Page) -> bool:
    for sel in ('a[data-e2e="video-author-avatar"]', 'a[data-e2e="browse-username"]',
                'a[href^="/@"]'):
        try:
            link = page.locator(sel).first
            if await link.count() > 0 and await link.is_visible():
                await link.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


async def _try_follow(page: Page, label) -> bool:
    for sel in ('button[data-e2e="follow-button"]', 'button[data-e2e="video-author-follow"]',
                'button[data-e2e="browse-follow"]', '[class*="StyledFollowButton"]',
                'button:has-text("Follow")'):
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0 or not await btn.is_visible():
                continue
            text = (await btn.inner_text(timeout=1000)).strip().lower()
            if any(kw in text for kw in ("following", "friends", "підписані")):
                return False
            await btn.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


async def _collect_search_urls(page: Page, ctx: "WarmupContext", limit: int = 12) -> list:
    link_selectors = [
        'div[data-e2e="search_top-item"] a[href*="/video/"]',
        'div[data-e2e="search-video-item"] a[href*="/video/"]',
        'div[data-e2e="search_video-item"] a[href*="/video/"]',
        'div[data-e2e*="search-item"] a[href*="/video/"]',
        'div[data-e2e="challenge-item"] a[href*="/video/"]',
        'a[href*="/video/"]',
    ]
    collected: list = []
    for lsel in link_selectors:
        try:
            for lnk in await page.locator(lsel).all():
                href = await lnk.get_attribute('href', timeout=500) or ""
                if not href or '/live' in href or '/video/' not in href:
                    continue
                if ctx.my_username and f"/@{ctx.my_username}/" in href.lower():
                    continue
                full = href if href.startswith('http') else f"https://www.tiktok.com{href}"
                m = re.search(r'/video/(\d+)', full)
                if m and m.group(1) in ctx.watched_video_ids:
                    continue
                if full not in collected:
                    collected.append(full)
            if len(collected) >= limit:
                break
        except Exception:
            continue
    return collected[:limit]


# ── state machine ───────────────────────────────────────────────────────────

class WarmupState(enum.Enum):
    FEED = "feed"; WATCHING = "watching"; COMMENTS = "comments"
    BACK = "back"; PROFILE_VISIT = "profile_visit"; FOLLOW = "follow"; DONE = "done"


@dataclass
class WarmupContext:
    label: str
    end_time: float
    topic: Optional[str] = None
    stop_event: Optional[object] = None        # asyncio.Event
    state: WarmupState = WarmupState.FEED
    videos_watched: int = 0
    follows_done: int = 0
    max_follows: int = field(default_factory=lambda: random.randint(1, 3))
    watched_video_ids: set = field(default_factory=set)
    route: str = field(default="search", repr=False)
    shadowbanned: bool = field(default=False, repr=False)
    _last_was_ua: bool = field(default=True, repr=False)
    _comment_w: float = field(default=0.13, repr=False)
    _visit_w: float = field(default=0.07, repr=False)
    _back_w: float = field(default=0.10, repr=False)
    _follow_w: float = field(default=0.02, repr=False)
    my_username: Optional[str] = field(default=None, repr=False)
    _video_queue: list = field(default_factory=list)
    _search_url: Optional[str] = field(default=None, repr=False)
    _consecutive_load_failures: int = field(default=0, repr=False)
    _consecutive_duplicates: int = field(default=0, repr=False)

    def should_stop(self) -> bool:
        if time.time() >= self.end_time:
            return True
        return bool(self.stop_event and self.stop_event.is_set())

    def _evolve_weights(self) -> None:
        if self.videos_watched > 0 and self.videos_watched % random.randint(6, 9) == 0:
            self._comment_w = min(0.30, self._comment_w + 0.02)
            self._visit_w = min(0.15, self._visit_w + 0.01)

    def advance(self) -> WarmupState:
        if self.should_stop():
            self.state = WarmupState.DONE
            return self.state
        if self.state in (WarmupState.COMMENTS, WarmupState.PROFILE_VISIT, WarmupState.FOLLOW):
            self.state = WarmupState.WATCHING
            return self.state
        if self.state == WarmupState.FEED:
            self.state = WarmupState.WATCHING
            return self.state
        self._evolve_weights()
        r = random.random()
        cumulative = self._comment_w
        if r < cumulative:
            self.state = WarmupState.COMMENTS
            return self.state
        cumulative += self._visit_w
        if r < cumulative:
            self.state = WarmupState.PROFILE_VISIT
            return self.state
        cumulative += self._back_w
        if r < cumulative:
            self.state = WarmupState.BACK
            return self.state
        if (self.topic and self._last_was_ua
                and self.follows_done < self.max_follows
                and r < cumulative + self._follow_w):
            self.state = WarmupState.FOLLOW
            return self.state
        self.state = WarmupState.WATCHING
        return self.state


# ── state handlers ──────────────────────────────────────────────────────────

async def _handle_feed(ctx: WarmupContext, page: Page) -> None:
    if ctx.route == "hashtag" and ctx.topic:
        tag = ctx.topic.replace(" ", "")
        url = f"https://www.tiktok.com/tag/{tag}"
        ctx._search_url = url
        _log(ctx.label, f"#️⃣ хештег-маршрут: #{tag}")
        await _safe_goto(page, url)
        await _organic_sleep(3.0, 5.0, ctx)
        if not await _is_logged_in(page):
            _log(ctx.label, "не залогінений — стоп"); ctx.state = WarmupState.DONE; return
        if await _detect_block(page):
            ctx.state = WarmupState.DONE; return
        if await _detect_empty_page(page):
            _log(ctx.label, f"#{tag}: порожньо"); ctx.shadowbanned = True; ctx.state = WarmupState.DONE; return
        for _ in range(3):
            try: await page.mouse.wheel(0, 2200)
            except Exception: pass
            await _organic_sleep(1.8, 2.8, ctx)
        urls = await _collect_search_urls(page, ctx, limit=12)
        if urls:
            ctx._video_queue = urls
            _log(ctx.label, f"#{tag}: зібрано {len(urls)} відео")
            await _safe_goto(page, ctx._video_queue.pop(0))
            await _organic_sleep(2.0, 3.5, ctx)

    elif ctx.route == "search" and ctx.topic:
        encoded = ctx.topic.replace(' ', '%20')
        url = f"https://www.tiktok.com/search/video?q={encoded}"
        ctx._search_url = url
        _log(ctx.label, f"🔍 пошук: {ctx.topic}")
        await _safe_goto(page, url)
        await _organic_sleep(3.0, 5.0, ctx)
        logged_in = False
        for ri in range(4):
            if await _is_logged_in(page):
                logged_in = True; break
            if ri < 3:
                try:
                    await page.reload(timeout=15000, wait_until="domcontentloaded")
                    await _organic_sleep(2.5, 4.0, ctx)
                except Exception:
                    pass
        if not logged_in:
            _log(ctx.label, "не залогінений після 3 спроб — стоп"); ctx.state = WarmupState.DONE; return
        for tab_sel in ('div[data-e2e="search-video-tab"]', 'a[data-e2e="search-video-tab"]',
                        'span:text-is("Videos")', 'div[role="tab"]:has-text("Video")'):
            try:
                tab = page.locator(tab_sel).first
                if await tab.count() > 0 and await tab.is_visible():
                    await tab.click(); await _organic_sleep(1.5, 2.5, ctx); break
            except Exception:
                continue
        for _ in range(3):
            try: await page.mouse.wheel(0, 2200)
            except Exception: pass
            await _organic_sleep(1.8, 2.8, ctx)
        urls = await _collect_search_urls(page, ctx, limit=12)
        if urls:
            ctx._video_queue = urls
            _log(ctx.label, f"пошук '{ctx.topic}': зібрано {len(urls)} відео")
            await _safe_goto(page, ctx._video_queue.pop(0))
            await _organic_sleep(2.0, 3.5, ctx)
    else:
        _log(ctx.label, "📱 стрічка 'Для вас'")
        await _safe_goto(page, "https://www.tiktok.com/foryou")
        await _organic_sleep(3.0, 5.0, ctx)


async def _handle_watching(ctx: WarmupContext, page: Page) -> None:
    url = page.url or ""
    m = re.search(r'/video/(\d+)', url)
    if m:
        vid = m.group(1)
        if vid in ctx.watched_video_ids:
            ctx._consecutive_duplicates += 1
            if ctx._consecutive_duplicates >= 5:
                ctx.watched_video_ids.clear(); ctx._consecutive_duplicates = 0
                ctx._video_queue.clear(); ctx.state = WarmupState.FEED; return
            await _advance_to_next(ctx, page); ctx.videos_watched += 1; return
        ctx.watched_video_ids.add(vid); ctx._consecutive_duplicates = 0

    if ctx.my_username and f"/@{ctx.my_username}/" in url.lower():
        await _advance_to_next(ctx, page); ctx.videos_watched += 1; return

    is_ua = await _has_cyrillic_description(page)
    ctx._last_was_ua = is_ua
    if not is_ua:
        await _organic_sleep(2.0, 3.0, ctx)
        ctx.videos_watched += 1; await _advance_to_next(ctx, page); return

    watch_time = max(5.0, min(45.0, random.gauss(mu=14.0, sigma=6.0)))
    if await _try_like(page):
        _log(ctx.label, "❤️ лайк")
    await _organic_sleep(watch_time * 0.6, watch_time, ctx)
    if random.random() < 0.05:
        extra = random.uniform(12.0, 28.0)
        await _organic_sleep(extra * 0.8, extra, ctx)
    ctx.videos_watched += 1
    await _advance_to_next(ctx, page)


async def _advance_to_next(ctx: WarmupContext, page: Page) -> None:
    if ctx.route in ("search", "hashtag"):
        if ctx._video_queue:
            await _safe_goto(page, ctx._video_queue.pop(0))
            await _organic_sleep(1.5, 2.5, ctx)
        elif ctx._search_url:
            await _safe_goto(page, ctx._search_url)
            await _organic_sleep(2.5, 4.0, ctx)
            extra = min(len(ctx.watched_video_ids) // 3, 8)
            for _ in range(4 + extra):
                try: await page.mouse.wheel(0, 2200)
                except Exception: pass
                await _organic_sleep(1.2, 2.0, ctx)
            for tab_sel in ('div[data-e2e="search-video-tab"]', 'a[data-e2e="search-video-tab"]',
                            'span:text-is("Videos")'):
                try:
                    tab = page.locator(tab_sel).first
                    if await tab.count() > 0 and await tab.is_visible():
                        await tab.click(); await _organic_sleep(1.0, 1.5, ctx); break
                except Exception:
                    continue
            new_urls = await _collect_search_urls(page, ctx, limit=12)
            if new_urls:
                ctx._video_queue = new_urls
                await _safe_goto(page, ctx._video_queue.pop(0))
                await _organic_sleep(1.5, 2.5, ctx)
            else:
                ctx.state = WarmupState.FEED
    else:
        await _try_swipe_down(page)
        await _organic_sleep(1.5, 3.5, ctx)


async def _handle_comments(ctx: WarmupContext, page: Page) -> None:
    if ctx.route == "foryou":
        return
    if not await _try_open_comments(page):
        return
    _log(ctx.label, "💬 читаю коментарі")
    for _ in range(random.randint(2, 5)):
        await _organic_sleep(1.5, 4.0, ctx)
        try: await page.mouse.wheel(0, random.randint(200, 600))
        except Exception: pass
    await _organic_sleep(1.0, 2.5, ctx)
    await _try_close_comments(page)
    await _organic_sleep(0.8, 1.8, ctx)


async def _handle_back(ctx: WarmupContext, page: Page) -> None:
    if ctx.route in ("search", "hashtag"):
        await _organic_sleep(random.uniform(4.0, 9.0), random.uniform(9.0, 18.0), ctx)
        return
    try: await page.keyboard.press("ArrowUp")
    except Exception: pass
    await _organic_sleep(2.0, 5.0, ctx)
    await _organic_sleep(random.uniform(3.0, 8.0), random.uniform(8.0, 18.0), ctx)
    await _try_swipe_down(page)


async def _handle_profile_visit(ctx: WarmupContext, page: Page) -> None:
    if not await _try_visit_profile(page):
        return
    _log(ctx.label, "👤 переглядаю профіль автора")
    await _organic_sleep(3.0, 7.0, ctx)
    try:
        await page.go_back(timeout=8000)
    except Exception:
        await _safe_goto(page, ctx._search_url or "https://www.tiktok.com/foryou")
    await _organic_sleep(1.5, 3.0, ctx)


async def _handle_follow(ctx: WarmupContext, page: Page) -> None:
    if await _try_follow(page, ctx.label):
        ctx.follows_done += 1
        _log(ctx.label, f"➕ підписався ({ctx.follows_done}/{ctx.max_follows})")
        await _organic_sleep(1.5, 3.0, ctx)


_STATE_HANDLERS = {
    WarmupState.FEED: _handle_feed,
    WarmupState.WATCHING: _handle_watching,
    WarmupState.COMMENTS: _handle_comments,
    WarmupState.BACK: _handle_back,
    WarmupState.PROFILE_VISIT: _handle_profile_visit,
    WarmupState.FOLLOW: _handle_follow,
}


async def _watching_guard(ctx: WarmupContext, page: Page, timeout_s: float = 10.0) -> bool:
    if ctx.route in ("search", "hashtag"):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if "/video/" in page.url:
                ctx._consecutive_load_failures = 0
                return True
            await asyncio.sleep(0.3)
        ctx._consecutive_load_failures += 1
        if ctx._consecutive_load_failures >= 3:
            ctx._consecutive_load_failures = 0
            ctx._video_queue.clear()
            ctx.state = WarmupState.FEED
        return False
    else:
        if await _is_video_element_visible(page):
            return True
        await _organic_sleep(2.0, 3.0, ctx)
        if await _is_video_element_visible(page):
            return True
        ctx._consecutive_load_failures += 1
        if ctx._consecutive_load_failures >= 3:
            ctx._consecutive_load_failures = 0
            await _safe_goto(page, "https://www.tiktok.com/foryou")
            await _organic_sleep(3.0, 5.0, ctx)
            ctx.state = WarmupState.FEED
        return False


# ── public API ──────────────────────────────────────────────────────────────

async def warmup_account(
    context: BrowserContext,
    *,
    minutes: float,
    topic: Optional[str] = None,
    route: str = "search",
    my_username: Optional[str] = None,
    stop_event: Optional[object] = None,
    on_progress: Optional[Callable[[int], None]] = None,
) -> dict:
    """Run an organic warmup session on an already-authenticated context.

    Returns a stats dict: {videos_watched, follows_done, reason, shadowbanned}.
    """
    if not topic and route != "foryou":
        topic = random.choice(SAFE_WARMUP_TOPICS)
    label = my_username or "acc"
    _log(label, f"🔥 старт прогріву ({minutes:.0f} хв | {'тема: ' + topic if topic else 'Для вас'})")

    ctx = WarmupContext(
        label=label,
        end_time=time.time() + minutes * 60,
        topic=None if route == "foryou" else topic,
        stop_event=stop_event,
        route=route,
        my_username=my_username,
    )
    reason = "time"

    try:
        page = await get_page(context)
        # Pre-flight: verify the profile doesn't leak (WebRTC/tz/geo) BEFORE TikTok sees it.
        # A mis-built antidetect profile that contradicts its proxy gets flagged fast — back
        # off now rather than warming it. Only meaningful when a real device/proxy is in play.
        try:
            from .device_check import self_test
            ok, problems = await self_test(page, getattr(context, "_country", None))
            if not ok:
                _log(label, f"🚫 self-test fail: {'; '.join(problems)} — стоп")
                return {"videos_watched": 0, "follows_done": 0,
                        "reason": f"профіль протікає: {problems[0]}", "shadowbanned": False}
        except Exception as exc:
            _log(label, f"self-test пропущено: {exc}")
        if not await _safe_goto(page, "https://www.tiktok.com/foryou"):
            return {"videos_watched": 0, "follows_done": 0, "reason": "не відкрився TikTok",
                    "shadowbanned": False}
        await _organic_sleep(2.0, 4.0, ctx)
        if not await _is_logged_in(page):
            return {"videos_watched": 0, "follows_done": 0, "reason": "не залогінений",
                    "shadowbanned": False}

        await _handle_feed(ctx, page)
        ctx.advance()  # FEED → WATCHING

        last_progress = 0
        while ctx.state != WarmupState.DONE:
            if ctx.should_stop():
                break
            issue = await _detect_block(page)
            if issue:
                _log(label, f"🚫 {issue} — стоп")
                reason = issue
                break

            if ctx.state == WarmupState.WATCHING:
                if not await _watching_guard(ctx, page):
                    if ctx.state == WarmupState.FEED:
                        await _handle_feed(ctx, page); ctx.advance(); continue
                    ctx.state = WarmupState.FEED
                    await _handle_feed(ctx, page); ctx.advance(); continue

            handler = _STATE_HANDLERS.get(ctx.state)
            if handler:
                await handler(ctx, page)
            ctx.advance()

            if ctx.videos_watched != last_progress:
                last_progress = ctx.videos_watched
                if ctx.videos_watched and ctx.videos_watched % 10 == 0:
                    _log(label, f"переглянуто {ctx.videos_watched} відео…")
                if on_progress:
                    try:
                        on_progress(ctx.videos_watched)
                    except Exception:
                        pass

        if ctx.shadowbanned:
            reason = "порожня стрічка (можливий shadowban)"
        _log(label, f"✅ завершено | переглянуто: {ctx.videos_watched} | підписок: {ctx.follows_done}")
    except Exception as exc:
        _log(label, f"❌ помилка: {exc}")
        reason = f"помилка: {str(exc)[:60]}"

    return {
        "videos_watched": ctx.videos_watched,
        "follows_done": ctx.follows_done,
        "reason": reason,
        "shadowbanned": ctx.shadowbanned,
    }
