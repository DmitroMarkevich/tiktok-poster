import asyncio
import random
import re
from typing import Optional, Tuple

import aiohttp
from playwright.async_api import BrowserContext, Page
from .browser import get_page
from utils.captcha import solve_captcha_if_present
from utils.ai import generate_comment_variants
import config


_BLOCKED_RESOURCE_TYPES = ("image", "media", "font")

_SPINTAX_RE = re.compile(r"\{([^{}]*)\}")


def render_comment(raw: str) -> str:
    """Turn one raw comment template into a unique-per-call string.

    Posting the *identical* text on every video and from every account is the
    single biggest trigger for TikTok's comment shadow-filter (the comment shows
    only to its own author, hidden from everyone else). Two cheap defences:

    1. Multiple variants — the user can put one alternative per line; we pick a
       random line each time.
    2. Spintax — any `{a|b|c}` group inside a line expands to one random choice,
       so even a single template yields many distinct surface strings.

    A plain text with no newlines and no `{...}` just passes through unchanged.
    """
    variants = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not variants:
        return raw.strip()
    chosen = random.choice(variants)

    def _expand(m: re.Match) -> str:
        options = [o.strip() for o in m.group(1).split("|")]
        return random.choice(options) if options else ""

    # Expand repeatedly so nested groups collapse fully.
    prev = None
    out = chosen
    while prev != out:
        prev = out
        out = _SPINTAX_RE.sub(_expand, out)
    return out.strip()


async def block_heavy_resources(context: BrowserContext):
    """Abort image/video/font requests for the lifetime of this context.

    The bot only needs to read text and click buttons — it never looks at a
    thumbnail or watches a video. But every one of those bytes still has to
    cross the (slow, proxied) network first. Cutting them out shrinks page-load
    weight by the bulk of what TikTok actually serves, which matters far more
    when every byte is paying proxy latency."""
    async def _handler(route, request):
        if request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()
    await context.route("**/*", _handler)


async def _goto_retry(page: Page, url: str, retries: int = 3, **kwargs):
    """page.goto with retries on transient proxy/tunnel errors (net::ERR_TUNNEL_
    CONNECTION_FAILED, ERR_PROXY_CONNECTION_FAILED, etc). The datacenter proxy
    occasionally drops the CONNECT tunnel on the first attempt — a plain retry
    after a short pause succeeds nearly every time (observed live: attempt 0
    fails, attempt 1 returns 200)."""
    last_err = None
    for attempt in range(retries):
        try:
            return await page.goto(url, **kwargs)
        except Exception as e:
            last_err = e
            if "ERR_TUNNEL_CONNECTION_FAILED" not in str(e) and "ERR_PROXY_CONNECTION_FAILED" not in str(e):
                raise
            if attempt < retries - 1:
                await asyncio.sleep(2 + attempt * 2)
    raise last_err


async def _get_videos_tikwm(keyword: str, count: int) -> list:
    """Fallback: get video URLs from tikwm.com (no browser needed)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://www.tikwm.com/api/feed/search",
                params={"keywords": keyword, "count": min(count * 5, 50), "cursor": 0, "web": 1},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json(content_type=None)
        videos = data.get("data", {}).get("videos", [])
        urls = []
        for v in videos:
            vid_id = v.get("video_id") or v.get("id")
            author = (v.get("author") or {}).get("unique_id", "")
            if vid_id and author:
                urls.append(f"https://www.tiktok.com/@{author}/video/{vid_id}")
        return urls
    except Exception:
        return []


async def _collect_dom_videos(page: Page, max_scroll: int = 20, target: int = None) -> list:
    """Scroll the current page and collect video hrefs from DOM.

    `target`, when given, lets us stop scrolling early once we have a few times
    more candidates than requested (buffer for ones that fail CAPTCHA/comment-box/
    persistence checks later). Each scroll triggers a lazy-load network fetch — on
    a slow/flaky proxy that's seconds, not the 0.4s sleep, so scrolling 20x for a
    `count=1` run was burning ~10 minutes before any commenting even began."""
    await page.mouse.move(640, 400)
    videos = set()
    stall_rounds = 0
    for _ in range(max_scroll):
        await page.mouse.wheel(0, 400)
        await asyncio.sleep(0.4)
        hrefs = await page.evaluate("""
            () => [...document.querySelectorAll('a')]
                  .map(a => a.href)
                  .filter(h => h && h.includes('/video/') && h.includes('tiktok.com'))
        """)
        before = len(videos)
        videos.update(hrefs)
        if target and len(videos) >= target:
            break
        stall_rounds = stall_rounds + 1 if len(videos) == before else 0
        if stall_rounds >= 3:  # nothing new for 3 scrolls — page stopped loading more
            break
    return list(videos)


async def scrape_hashtag_videos(context: BrowserContext, hashtag: str, count: int = 1) -> Tuple[list, str]:
    """Get video URLs for a hashtag/keyword. Split out from comment_on_hashtag_videos so
    the result can be scraped ONCE and shared across many accounts in a bulk run.

    PRIMARY source is the tikwm keyword SEARCH: it returns results actually matching the
    keyword, whereas the logged-in /tag/ page mixes in TikTok's personalised
    recommendations — which is why comments were landing on unrelated videos. It also
    needs no browser/CAPTCHA, so it's faster too. Falls back to scraping the tag page's
    DOM only when the search comes up short."""
    tag = hashtag.lstrip("#")
    dbg = []

    # 1. tikwm keyword search — relevance-ranked, no browser/CAPTCHA needed.
    video_urls = await _get_videos_tikwm(tag, count)
    dbg.append(f"tikwm search: {len(video_urls)}")
    if len(video_urls) >= count:
        return video_urls, "\n".join(dbg)

    # 2. Fallback: scrape the tag page DOM (browser + CAPTCHA). Less relevant, but a
    # safety net when tikwm is down/rate-limited or returns too few for this keyword.
    dbg.append("→ tikwm insufficient, falling back to DOM scrape")
    page = await get_page(context)

    # domcontentloaded, not "load": the video grid is client-rendered by JS anyway, so
    # "load" only adds the wait for trailing resources (slowest part through a proxy)
    # without giving us the anchors any sooner — the sleep below covers JS hydration.
    await _goto_retry(page, f"https://www.tiktok.com/tag/{tag}", wait_until="domcontentloaded")
    await asyncio.sleep(2.5)

    solved = await solve_captcha_if_present(
        page, api_key=config.CAPTCHA_API_KEY, service=config.CAPTCHA_SERVICE
    )
    dbg.append(f"hashtag CAPTCHA: {'ok' if solved else 'failed'}")

    if solved:
        await asyncio.sleep(1)

    dom_urls = await _collect_dom_videos(page, max_scroll=20, target=max(count * 5, 15))
    dbg.append(f"DOM videos: {len(dom_urls)}")
    # Keep any tikwm hits first (more relevant), then top up with DOM ones.
    video_urls = video_urls + [u for u in dom_urls if u not in video_urls]

    return video_urls, "\n".join(dbg)


async def comment_on_hashtag_videos(
    context: BrowserContext,
    hashtag: str,
    comment_text: str,
    count: int = 1,
    debug=None,
    send_screenshot=None,
    video_urls: Optional[list] = None,
) -> Tuple[int, str]:
    tag = hashtag.lstrip("#")
    dbg = []

    # ── 1-2. Get video URLs — reuse a pre-scraped shared list if the caller has one ──
    if video_urls:
        dbg.append(f"shared video list: {len(video_urls)}")
    else:
        video_urls, scrape_dbg = await scrape_hashtag_videos(context, hashtag, count)
        dbg.append(scrape_dbg)

    if not video_urls:
        return 0, f"Відео не знайдено для #{tag}\n" + "\n".join(dbg)

    # Shuffle a *copy* of the shared list so every account works through the videos
    # in its own order. With the shared cache, all accounts would otherwise comment
    # on the identical first-N videos — a clear multi-account bot-cluster fingerprint
    # that feeds the same shadow-filter. A different order per account spreads the
    # overlap out without needing a separate scrape per account.
    video_urls = list(video_urls)
    random.shuffle(video_urls)

    # AI variation pool (Gemini, free tier) — one call up front, then rotate a fresh
    # string per video so no two comments are byte-identical. Falls back silently to
    # the local spintax variation (render_comment) if no API key / API fails.
    ai_pool = await generate_comment_variants(comment_text, n=max(count * 2, 12))
    if ai_pool:
        random.shuffle(ai_pool)  # consumed in order below → distinct comment per video
        dbg.append(f"AI variants: {len(ai_pool)}")

    # ── 3. Comment on each video ───────────────────────────────────────────────
    # IMPORTANT: open each video in a FRESH tab, never page.goto() from the hashtag
    # page in-place. TikTok's SPA router fails to re-sync auth state on that kind of
    # client-side navigation and silently renders the video as a logged-out guest view
    # (no comment box at all, just "Sign in to comment") — which is why every comment
    # posted via in-place navigation was a no-op despite looking fine in the DOM.
    # Success = the comment showed up locally right after Post (the `appeared_locally`
    # check in _post_comment_with_captcha). The old full-reload persistence check was
    # removed: a freshly posted comment reaches TikTok's server-rendered response only
    # after a delay, so reloading seconds later reported false failures — the same way
    # a manually posted comment also appears only after a while.
    commented = 0
    commented_urls = []
    for i, url in enumerate(video_urls):
        if commented >= count:
            break
        video_page = await context.new_page()
        try:
            # Fresh, unique-per-video string: walk the shuffled AI pool in order (so
            # consecutive videos get DIFFERENT variants, no random repeats), or fall
            # back to local spintax. Identical text is the main shadow-filter trigger.
            rendered = ai_pool[i % len(ai_pool)] if ai_pool else render_comment(comment_text)
            print(f"[comment] → posting on {url}: {rendered!r}", flush=True)
            # Hard per-video watchdog: a single heavy/stuck page (spinner, runaway JS,
            # an element wait that never resolves) must not freeze the whole run. Abort
            # this video after 150s and move on.
            try:
                ok, reason = await asyncio.wait_for(
                    _post_comment_with_captcha(video_page, url, rendered), timeout=150
                )
            except asyncio.TimeoutError:
                ok, reason = False, "таймаут відео (>150с) — пропускаю"
            print(f"[comment] {'OK' if ok else 'FAIL'} {url} — {reason}", flush=True)
            if ok:
                commented += 1
                commented_urls.append(url)
                dbg.append(f"✓ {url}")
                await asyncio.sleep(random.uniform(8, 20))
            else:
                dbg.append(f"✗ {url} — {reason}")
        except Exception as e:
            dbg.append(f"✗ {e}")
        finally:
            try:
                await video_page.close()
            except Exception:
                pass

    debug_lines = [
        f"Хештег: #{tag}",
        f"Знайдено відео: {len(video_urls)}",
        f"Прокоментовано: {commented}",
        "Відео:",
    ] + [f"  {u}" for u in commented_urls] + [
        f"Log: {dbg}",
    ]
    return commented, "\n".join(debug_lines)


async def _human_delay(a: float, b: float):
    await asyncio.sleep(random.uniform(a, b))


async def _watch_video(page: Page):
    """Spend a human-like 'watch' interval on the video before interacting.

    A real viewer doesn't navigate to a video and instantly fire a comment — they
    watch for a few seconds, maybe scroll a touch. TikTok tracks dwell time and
    engagement through JS timers (not just media bytes), so this registers as a
    genuine view even with media requests blocked, and breaks the instant
    navigate→comment pattern that screams "bot"."""
    try:
        await _human_delay(1.2, 2.5)
        for _ in range(random.randint(1, 2)):
            await page.mouse.wheel(0, random.randint(120, 320))
            await _human_delay(0.5, 1.0)
        await _human_delay(2.0, 5.0)  # actual "watching"
    except Exception:
        pass


async def _maybe_like(page: Page, prob: float = 0.5):
    """Like the video roughly `prob` of the time. People who comment frequently
    like too; a comment with zero other engagement from the same session is a
    weak, bot-ish signal."""
    if random.random() > prob:
        return
    for sel in ["[data-e2e='like-icon']", "[data-e2e='browse-like-icon']"]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1500):
                await _human_delay(0.4, 1.2)
                await el.click()
                return
        except Exception:
            pass


async def _read_comments(page: Page):
    """Scroll the comment list a little, as if reading the room, before posting."""
    try:
        lst = page.locator("[data-e2e='comment-list']").first
        for _ in range(random.randint(1, 2)):
            try:
                await lst.evaluate("el => el.scrollBy(0, 250)")
            except Exception:
                await page.mouse.wheel(0, 250)
            await _human_delay(0.5, 1.0)
    except Exception:
        pass


async def _human_type(page: Page, text: str):
    """Type with a variable per-keystroke delay and occasional 'thinking' pauses,
    instead of a robotic fixed cadence."""
    for ch in text:
        await page.keyboard.type(ch, delay=random.uniform(45, 160))
        if random.random() < 0.06:
            await asyncio.sleep(random.uniform(0.3, 1.1))


async def _post_comment_with_captcha(page: Page, video_url: str, text: str) -> Tuple[bool, str]:
    """Navigate to video URL, solve CAPTCHA if needed, post comment.
    Returns (success, reason) — reason explains exactly where it stopped, so
    debug reports can pinpoint the failing step instead of a generic "no comment box"."""
    if not video_url.startswith("http"):
        video_url = "https://www.tiktok.com" + video_url

    await _goto_retry(page, video_url, wait_until="domcontentloaded")
    await asyncio.sleep(2)

    # Solve initial CAPTCHA (may appear immediately on video page)
    await _solve_captcha_blocking(page)
    await asyncio.sleep(0.5)

    # Behave like a viewer first: watch a bit before going for the comment box.
    await _watch_video(page)

    # Open comment panel — click icon if input isn't visible yet
    comment_box = await _find_comment_input(page)
    if not comment_box:
        for btn_sel in ["[data-e2e='comment-icon']", "[data-e2e='comment-count']"]:
            try:
                btn = page.locator(btn_sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1.2)
                    break
            except Exception:
                pass

    # Solve CAPTCHA that may have appeared after clicking icon
    await _solve_captcha_blocking(page)
    await asyncio.sleep(0.5)

    # Re-find comment input after CAPTCHA is gone
    comment_box = await _find_comment_input(page)
    if not comment_box:
        return False, "не знайдено поле коментаря"

    # Read the room and (sometimes) like — engagement that a commenting human does.
    await _read_comments(page)
    await _maybe_like(page, prob=0.5)

    # Focus the comment box. The blocker is usually NOT a CAPTCHA (cookie banner,
    # login-guide modal, joyride coach-marks, leftover TUXModal backdrop), so on ANY
    # click failure — intercept OR plain timeout — strip overlays + solve captcha,
    # then escalate: normal click → force click → JS focus/click (ignores overlays
    # entirely). Routing every failure (not just "intercepts") through this chain is
    # the fix: previously a bare Timeout skipped the fallback and returned early.
    await _dismiss_overlays(page)
    clicked = False
    for attempt in range(3):
        try:
            await comment_box.click(timeout=4000)
            clicked = True
            break
        except Exception:
            await _solve_captcha_blocking(page)
            await _dismiss_overlays(page)
            await asyncio.sleep(0.8)
            try:
                await comment_box.click(force=True, timeout=3000)
                clicked = True
                break
            except Exception:
                pass
    if not clicked:
        # Last resort: focus/click straight through the DOM, bypassing any overlay.
        try:
            await comment_box.evaluate(
                "el => { el.scrollIntoView({block:'center'}); el.click(); "
                "el.focus && el.focus(); }"
            )
            clicked = True
        except Exception:
            pass
    if not clicked:
        return False, "клік по полю заблоковано оверлеєм"

    # Type with retry — verify the text actually landed in the contenteditable.
    # A lost click/focus silently leaves the input empty and the Post button stays
    # logically disabled (data-disabled/aria-disabled), so the click below does nothing.
    typed = False
    for _ in range(3):
        await asyncio.sleep(0.5)
        await _human_type(page, text)
        await asyncio.sleep(0.5)
        try:
            current = await comment_box.inner_text()
        except Exception:
            current = ""
        if text.strip() and text.strip() in current:
            typed = True
            break
        # Re-focus and retry
        try:
            await comment_box.click(timeout=3000)
        except Exception:
            pass

    if not typed:
        return False, "текст не потрапив у поле (втрачено фокус)"

    # Beat before submitting — a person re-reads their comment, doesn't fire instantly.
    await _human_delay(0.4, 1.2)

    # Find the Post button and wait until it's actually enabled (not data-disabled/aria-disabled)
    btn = None
    for btn_sel in ["[data-e2e='comment-post']", "button:has-text('Post')"]:
        try:
            candidate = page.locator(btn_sel).first
            if await candidate.is_visible(timeout=3000):
                btn = candidate
                break
        except Exception:
            pass

    submitted = False
    if btn:
        for _ in range(10):
            try:
                disabled = await btn.evaluate(
                    "el => el.getAttribute('data-disabled') === 'true' || el.getAttribute('aria-disabled') === 'true' || el.disabled"
                )
            except Exception:
                disabled = False
            if not disabled:
                await btn.click()
                submitted = True
                break
            await asyncio.sleep(0.5)

    if not submitted:
        await page.keyboard.press("Enter")

    # Local check: did the comment register client-side after Post? If it never even
    # appears locally, the submission failed (server rejected it outright).
    appeared_locally = False
    elapsed = 0.0
    while elapsed < 10.0:
        try:
            page_text = await page.evaluate(
                "() => (document.querySelector(\"[data-e2e='comment-list']\") || document.body).innerText"
            )
            if text in page_text:
                appeared_locally = True
                break
        except Exception:
            pass
        await asyncio.sleep(0.8)
        elapsed += 0.8

    if not appeared_locally:
        return False, "кнопку Post натиснуто, але коментар не з'явився локально (сервер відхилив?)"

    # The comment showed up after clicking Post — that's our success signal.
    # The old full-reload _comment_persisted check is intentionally NOT used: a freshly
    # posted comment propagates to TikTok's server-rendered response with a delay, so
    # reloading a few seconds later often doesn't see it yet and reports a false failure —
    # exactly the same behaviour as a manually posted comment, which also appears only
    # after a while. Local appearance is the reliable signal.
    return True, "ok"


async def _solve_captcha_blocking(page: Page) -> bool:
    """Solve CAPTCHA and wait until TUXModal-overlay is gone."""
    # max_attempts=1: a "rotation" captcha sweeps all 36 evenly-spaced positions in
    # one pass already (±10px acceptance window / ~8px steps — one sweep covers the
    # whole track). Retrying the *same* 36 positions again on failure buys nothing
    # but multiplies cost — and through a slow proxy each position costs 3-5x more
    # (TikTok re-validates server-side on every wrong drag), so 3 sweeps turned a
    # ~1min solve into 10+ minutes for a single captcha.
    solved = await solve_captcha_if_present(
        page, api_key=config.CAPTCHA_API_KEY, service=config.CAPTCHA_SERVICE, max_attempts=1
    )
    if solved:
        # Extra wait for overlay animation to clear
        for _ in range(10):
            try:
                overlay = page.locator(".TUXModal-overlay").first
                if not await overlay.is_visible(timeout=300):
                    break
            except Exception:
                break
            await asyncio.sleep(0.5)
    return solved


async def _dismiss_overlays(page: Page) -> None:
    """Remove non-CAPTCHA overlays that intercept clicks on the comment box
    (cookie banner, login-guide modal, joyride coach-marks, leftover TUXModal
    backdrops). CAPTCHA overlays are handled separately by _solve_captcha_blocking.
    Never removes a container that holds the comment input itself."""
    try:
        await page.evaluate("""
        () => {
          const kill = el => {
            if (!el) return;
            if (el.querySelector && el.querySelector("[data-e2e='comment-input']")) return;
            el.style.pointerEvents = 'none';
            el.style.display = 'none';
            try { el.remove(); } catch (e) {}
          };
          document.querySelectorAll(
            '.react-joyride__overlay, .TUXModal-overlay, [class*="MaskLayer"],' +
            '[class*="modal-mask"], [class*="LoginContainer"], [class*="login-modal"],' +
            'tiktok-cookie-banner'
          ).forEach(kill);
          document.querySelectorAll('div[role="dialog"]').forEach(kill);
        }
        """)
    except Exception:
        pass


async def _find_comment_input(page: Page):
    """Find visible comment input element."""
    for sel in [
        "[data-e2e='comment-input']",
        "[contenteditable='true'][placeholder]",
        "[class*='CommentInput'] [contenteditable]",
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                return el
        except Exception:
            continue
    return None
