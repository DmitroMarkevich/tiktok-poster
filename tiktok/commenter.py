import asyncio
import random
import re
from typing import Optional, Tuple

import aiohttp
from playwright.async_api import BrowserContext, Page
from .browser import get_page
from . import dedup
from utils.captcha import solve_captcha_if_present
from utils.ai import generate_comment_variants
from utils.humanize import human_click, human_scroll, human_type
import config


# Always block images (thumbnails — the byte bulk, and not a viewing signal). VIDEO ("media")
# is blocked ONLY when config.BLOCK_VIDEO_MEDIA is set: an account that dwells on a video and
# likes it but streams 0 CDN segments is a server-side "not a real viewer" tell that feeds the
# shadow-filter, so the realistic default lets video through. Fonts are never blocked (small,
# and a page with every webfont failing is itself an abnormal, detectable pattern).
_BLOCKED_RESOURCE_TYPES = ("image", "media") if config.BLOCK_VIDEO_MEDIA else ("image",)

# How many of a run's comments to guest-verify for shadowban. Shadowban is an
# account-level state, so a small random sample estimates trust just as well as
# checking every comment, while keeping verification time roughly constant.
_SHADOWBAN_SAMPLE = 4

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


async def get_videos_tikwm(keyword: str, want: int = 15) -> list:
    """Get up to ~`want` video URLs from tikwm.com keyword search (no browser needed).

    This is scrape_hashtag_videos' PRIMARY source, and it's a plain HTTP call — no
    browser, proxy or CAPTCHA. Exposed (not `_`-private) so the bulk handler can try it
    BEFORE launching a scout browser, and only pay for Chrome when it comes up short.

    `want` is the desired pool size, NOT the number of comments to post. A single tikwm
    page returns at most ~29 results, so we PAGINATE via the cursor to reach `want`:
      • even a comment-on-1 run needs a buffer — videos get skipped (already commented,
        claimed by another account) or fail (no comment box, CAPTCHA, timeout), so a pool
        of 5 often can't yield even 1 success;
      • a bulk run needs one DISTINCT video per account (the global claim stops two
        accounts piling onto the same video → shadow-filter), so the pool must scale past
        a single page.
    Returns whatever was collected (possibly partial) — never raises."""
    urls: list = []
    seen: set = set()
    cursor = 0
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(6):  # ~29/page → up to ~170; capped so a sparse keyword can't loop forever
                async with session.get(
                    "https://www.tikwm.com/api/feed/search",
                    params={"keywords": keyword, "count": 30, "cursor": cursor, "web": 1},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    data = await r.json(content_type=None)
                d = data.get("data", {}) or {}
                videos = d.get("videos", []) or []
                for v in videos:
                    vid_id = v.get("video_id") or v.get("id")
                    author = (v.get("author") or {}).get("unique_id", "")
                    if vid_id and author:
                        u = f"https://www.tiktok.com/@{author}/video/{vid_id}"
                        if u not in seen:
                            seen.add(u)
                            urls.append(u)
                if len(urls) >= want or not videos or not d.get("hasMore"):
                    break
                cursor = d.get("cursor") or (cursor + len(videos))
    except Exception:
        pass
    return urls


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

    # 1. tikwm keyword search — relevance-ranked, no browser/CAPTCHA needed. Pull several
    # times `count` so there's a buffer for videos that get skipped/fail downstream.
    video_urls = await get_videos_tikwm(tag, max(count * 5, 15))
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
    account_id: Optional[int] = None,
    proxy: Optional[str] = None,
    verify_shadowban: bool = True,
    username: str = "",
    engage: bool = True,
    defer_shadowban: bool = False,
    posted_out: Optional[list] = None,
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
    posted = []  # (url, vid, text) for each OK — fed to the shadowban check below
    skipped_dup = 0
    for i, url in enumerate(video_urls):
        if commented >= count:
            break

        # ── Anti-duplication (skip cheap, before opening a page) ──────────────
        # Two layers ported from multicombine: per-account history (never comment
        # the same video twice) + global claim (one account per video, so a cluster
        # of accounts doesn't pile onto identical videos → shadow-filter trigger).
        vid = dedup.extract_video_id(url)
        if account_id is not None:
            if dedup.already_commented(account_id, vid):
                skipped_dup += 1
                dbg.append(f"↷ вже коментував {vid}")
                continue
            if not dedup.try_claim(vid, account_id, tag):
                skipped_dup += 1
                dbg.append(f"↷ зайнято іншим акаунтом {vid}")
                continue

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
                    _post_comment_with_captcha(video_page, url, rendered,
                                               account_id=account_id, username=username),
                    timeout=150,
                )
            except asyncio.TimeoutError:
                ok, reason = False, "таймаут відео (>150с) — пропускаю"
            print(f"[comment] {'OK' if ok else 'FAIL'} {url} — {reason}", flush=True)
            if ok:
                commented += 1
                commented_urls.append(url)
                posted.append((url, vid, rendered))
                dbg.append(f"✓ {url}")
                if account_id is not None:
                    dedup.record_comment(account_id, vid, comment_text=rendered)
                    dedup.mark_success(vid)
                await asyncio.sleep(random.uniform(8, 20))
            else:
                dbg.append(f"✗ {url} — {reason}")
                if account_id is not None:
                    dedup.mark_failed(vid)  # release claim so another account may try
        except Exception as e:
            # Surface the real error: this branch used to swallow exceptions silently
            # (only into the Telegram dbg list), so a crash mid-post looked like the
            # video was just skipped — no [comment] line, no comment posted.
            import traceback
            print(f"[comment] ERROR {url} — {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            dbg.append(f"✗ {e}")
            if account_id is not None:
                dedup.mark_failed(vid)
        finally:
            try:
                await video_page.close()
            except Exception:
                pass

    # ── Shadowban check ────────────────────────────────────────────────────────
    # "OK" only proves the comment rendered in the author's own session. Re-open each
    # posted video as a logged-out guest and confirm the comment is visible to others;
    # an account-shadowbanned comment is present for its author but absent for everyone
    # else. Verdicts: survived / shadowbanned / inconclusive (don't blame the account).
    survived = banned = inconclusive = 0
    deferred = False
    if posted and defer_shadowban and posted_out is not None:
        # Hand the posted comments back to the caller, which runs the guest-verify in the
        # BACKGROUND after releasing the account lock + concurrency slot — so this account's
        # ~40s of propagation-sleep + verification no longer blocks the next account.
        posted_out.extend(posted)
        deferred = True
    elif verify_shadowban and posted:
        survived, banned, inconclusive = await verify_posted_shadowban(
            proxy, account_id, username, posted, dbg=dbg
        )

    # ── Engagement ─────────────────────────────────────────────────────────────
    # After posting, an account that also replies to comments on its OWN latest video
    # looks like a real creator (extra organic signal), not a one-way comment bot.
    # Best-effort — never let it fail the run.
    replied = 0
    if engage and commented > 0:
        if account_id is not None:
            from tiktok import live_state
            live_state.update(account_id, username=username, phase="engagement (відповіді)")
        try:
            from tiktok.engagement import reply_to_latest_video_comments
            replied = await reply_to_latest_video_comments(context)
        except Exception as e:
            dbg.append(f"engagement error: {e}")

    debug_lines = [
        f"Хештег: #{tag}",
        f"Знайдено відео: {len(video_urls)}",
        f"Пропущено (дублі/зайнято): {skipped_dup}",
        f"Прокоментовано: {commented}",
    ]
    if engage and commented > 0:
        debug_lines.append(f"Відповідей під своїм відео: {replied}")
    if deferred:
        debug_lines.append(f"Перевірка шедоубану: у фоні ({len(posted)} коментар(ів))")
    elif verify_shadowban and posted:
        debug_lines.append(
            f"Перевірка (вибірка {survived + banned + inconclusive}/{len(posted)}): "
            f"Вижило {survived} · Шедоубан {banned} · Невизначено {inconclusive}"
        )
        if account_id is not None:
            s, b, checked, pct = dedup.account_trust(account_id)
            if checked:
                debug_lines.append(f"Trust акаунта: {pct:.0f}% ({s}/{checked} видимих)")
    debug_lines += ["Відео:"] + [f"  {u}" for u in commented_urls] + [f"Log: {dbg}"]
    if account_id is not None:
        from tiktok import live_state
        live_state.finish(account_id, phase=f"завершено · {commented} коментарів")
    return commented, "\n".join(debug_lines)


async def verify_posted_shadowban(proxy, account_id, username, posted, dbg=None):
    """Guest-verify a SAMPLE of posted comments for shadowban and write the verdicts to
    dedup. Returns (survived, banned, inconclusive). Sampling keeps verification time
    roughly constant (shadowban is an account-level state, so a handful estimates trust
    as well as checking all). Used both inline and as the deferred background pass."""
    from tiktok.shadowban import verify_comments_visible
    survived = banned = inconclusive = 0
    if not posted:
        return 0, 0, 0
    if account_id is not None:
        from tiktok import live_state
        live_state.update(account_id, username=username, phase="перевірка шедоубану")
    sample = posted if len(posted) <= _SHADOWBAN_SAMPLE else random.sample(posted, _SHADOWBAN_SAMPLE)
    await asyncio.sleep(20)  # let the freshest comment propagate server-side
    try:
        verdicts = await verify_comments_visible(proxy, [(u, t) for (u, _v, t) in sample])
    except Exception as e:
        verdicts = {}
        if dbg is not None:
            dbg.append(f"shadowban check error: {e}")
    for url, vid, _text in sample:
        verdict = verdicts.get(url)
        if account_id is not None:
            dedup.set_visibility(account_id, vid, verdict)
        if verdict is True:
            survived += 1
        elif verdict is False:
            banned += 1
            if dbg is not None:
                dbg.append(f"👻 shadowban: {url}")
        else:
            inconclusive += 1
    if account_id is not None:
        from tiktok import live_state
        live_state.finish(account_id, phase="перевірка завершена")
    return survived, banned, inconclusive


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
            await human_scroll(page, random.uniform(120, 320))
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
                # Curved cursor move + click (human_click caps its own waits, so the
                # like — which is optional — can't hang 30s on a blocked icon).
                await human_click(page, el, timeout=4000)
                return
        except Exception:
            pass


async def _read_comments(page: Page):
    """Scroll the comment list a little, as if reading the room, before posting.

    Uses page.evaluate with a null-safe querySelector, NOT locator.evaluate: a locator
    auto-waits 30s for the element to attach, and the comment LIST often renders a beat
    after the input box — profiling caught this as a 31s 'read+like' phase. Null-safe
    JS just no-ops when the list isn't there yet, so reading stays cosmetic and cheap."""
    for _ in range(random.randint(1, 2)):
        try:
            scrolled = await page.evaluate(
                "() => { const el = document.querySelector(\"[data-e2e='comment-list']\");"
                " if (el) { el.scrollBy(0, 250); return true; } return false; }"
            )
            if not scrolled:
                await human_scroll(page, 250)
        except Exception:
            pass
        await _human_delay(0.5, 1.0)


async def _human_type(page: Page, text: str):
    """Type with variable cadence, thinking pauses, and rare typo+backspace corrections.
    Thin wrapper over utils.humanize.human_type (shared with the uploader/engagement)."""
    await human_type(page, text)


class _Timer:
    """Lightweight per-phase profiler. mark() prints the time since the previous mark,
    so a run's log shows exactly where the seconds go (goto / watch / captcha / type…).
    Temporary instrumentation — strip once the bottleneck is found."""
    def __init__(self, label: str):
        import time
        self._time = time.monotonic
        self.label = label
        self.t = self._time()
    def mark(self, phase: str):
        now = self._time()
        print(f"[timing] {self.label} {phase}: {now - self.t:.1f}s", flush=True)
        self.t = now


class _Progress:
    """Per-video timing + live status + screenshot, so the Telegram '📊 Статус' view can
    show where each account's bot is. On every phase mark it prints the timing, writes a
    fresh page screenshot to disk, and pushes the phase into live_state. account_id=None
    (no live tracking wanted) makes it behave like a plain timer."""
    def __init__(self, page, video_url, account_id=None, username=""):
        self.page = page
        self.account_id = account_id
        self.username = username
        self.video_url = video_url
        self.tm = _Timer(video_url.rsplit("/", 1)[-1])

    async def mark(self, phase: str):
        self.tm.mark(phase)
        if self.account_id is None:
            return
        try:
            import os
            from tiktok import live_state
            os.makedirs(live_state.SCREENSHOT_DIR, exist_ok=True)
            shot = f"{live_state.SCREENSHOT_DIR}/{self.account_id}.png"
            try:
                await self.page.screenshot(path=shot, timeout=3000)
            except Exception:
                pass  # keep the previous screenshot file if this capture fails
            live_state.update(self.account_id, username=self.username, phase=phase,
                              video_url=self.video_url, screenshot=shot)
        except Exception:
            pass


async def _post_comment_with_captcha(page: Page, video_url: str, text: str,
                                     account_id=None, username="") -> Tuple[bool, str]:
    """Navigate to video URL, solve CAPTCHA if needed, post comment.
    Returns (success, reason) — reason explains exactly where it stopped, so
    debug reports can pinpoint the failing step instead of a generic "no comment box"."""
    if not video_url.startswith("http"):
        video_url = "https://www.tiktok.com" + video_url

    prog = _Progress(page, video_url, account_id=account_id, username=username)
    await _goto_retry(page, video_url, wait_until="domcontentloaded")
    await asyncio.sleep(2)
    await prog.mark("goto+settle")

    # Cheap relevance/skip guards BEFORE spending time on captcha/typing:
    #  • LIVE pages have a different comment UI — commenting there is flaky and pointless;
    #  • a foreign-language video is an irrelevant target — posting a Ukrainian comment
    #    there is wasted and a spam signal (shadow-filter). Skip both early.
    from tiktok.page_guards import is_live_stream, has_cyrillic_description
    if await is_live_stream(page):
        return False, "лайв-стрім — пропускаю"
    if not await has_cyrillic_description(page):
        return False, "інша мова відео — пропускаю"

    # Solve initial CAPTCHA (may appear immediately on video page)
    await _solve_captcha_blocking(page)
    await asyncio.sleep(0.5)
    await prog.mark("captcha#1")

    # Behave like a viewer first: watch a bit before going for the comment box.
    await _watch_video(page)
    await prog.mark("watch_video")

    # Open comment panel — click icon if input isn't visible yet
    comment_box = await _find_comment_input(page)
    if not comment_box:
        for btn_sel in ["[data-e2e='comment-icon']", "[data-e2e='comment-count']"]:
            try:
                btn = page.locator(btn_sel).first
                if await btn.is_visible(timeout=2000):
                    # Explicit timeout: a blocked click otherwise hangs on Playwright's
                    # 30s default — profiling caught two of these stacking to 60s on a
                    # video whose comment box wasn't accessible (login-walled / comments
                    # off). Fail fast and skip instead.
                    await btn.click(timeout=4000)
                    await asyncio.sleep(1.2)
                    break
            except Exception:
                pass

    await prog.mark("open_comments")
    # Solve CAPTCHA that may have appeared after clicking icon
    await _solve_captcha_blocking(page)
    await asyncio.sleep(0.5)
    await prog.mark("captcha#2")

    # Re-find comment input after CAPTCHA is gone
    comment_box = await _find_comment_input(page)
    if not comment_box:
        return False, "не знайдено поле коментаря"

    # Read the room and (sometimes) like — engagement that a commenting human does.
    await _read_comments(page)
    await _maybe_like(page, prob=0.5)
    await prog.mark("read+like")

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
            # Curved cursor path → hover → trusted click (not a teleport) on the comment
            # box: the comment flow is the hottest shadow-ban path, so the focus click must
            # carry a real mousemove stream. Falls back to a plain click internally.
            if await human_click(page, comment_box, timeout=4000):
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
    await prog.mark("focus_input")

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
    await prog.mark("type_text")

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
                submitted = await human_click(page, btn, timeout=5000)
                if submitted:
                    break
            await asyncio.sleep(0.5)

    if not submitted:
        await page.keyboard.press("Enter")

    # Local check: did the comment register client-side after Post? If it never even
    # appears locally, the submission failed (server rejected it outright).
    # Match a NORMALISED PREFIX, not the exact full text: TikTok collapses whitespace and
    # truncates long comments with "… more", so `text in page_text` false-negatives on long
    # comments — and a false negative is dangerous here: it returns failure, the claim is
    # released and the comment is NOT recorded, so a re-run lets the SAME account comment the
    # SAME video twice (a strong shadow-ban trigger). The prefix match mirrors shadowban._signature.
    _norm = lambda s: re.sub(r"\s+", " ", (s or "").strip())
    needle = _norm(text)[:40]
    appeared_locally = False
    elapsed = 0.0
    while elapsed < 10.0:
        try:
            page_text = await page.evaluate(
                "() => (document.querySelector(\"[data-e2e='comment-list']\") || document.body).innerText"
            )
            if needle and needle in _norm(page_text):
                appeared_locally = True
                break
        except Exception:
            pass
        await asyncio.sleep(0.8)
        elapsed += 0.8

    await prog.mark("submit+verify")
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
