"""Free AI comment variation via Google Gemini.

One call returns a pool of natural, varied rewrites of the user's seed comment;
the commenter rotates through that pool so no two comments are byte-identical
(the main TikTok shadow-filter trigger). Any failure returns [] and the caller
falls back to the local spintax variation in commenter.render_comment.
"""
from __future__ import annotations
import asyncio
import re
import time

import aiohttp

import config

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Strip leading list markers ("1.", "-", "•", quotes) the model sometimes adds.
_LIST_PREFIX = re.compile(r"^\s*(?:\d+[.)]\s*|[-*•]\s*)")

# Meta/preamble lines the model sometimes prepends ("Ось 8 варіантів...:") — these
# are NOT comments and must never be posted.
_META_LINE = re.compile(r"(?:варіант|перефраз|ось\s+\d|here are|variants?)", re.IGNORECASE)


# Distinct human "voices" the variants are spread across. This is about TONE only —
# every voice must keep the SAME meaning and call-to-action as the seed. A single uniform
# style across all comments/accounts is itself a fingerprint; mixing voices makes the
# pool read like many different real people, which is what we want against the filter.
_VOICES = [
    "невимушено, наче пишеш другу в коментах",
    "коротко й недбало, як побіжна репліка",
    "з легким приколом/гумором",
    "по-доброму й тепло, простими словами",
    "жваво й емоційно, з енергією",
    "ніби мимохідь кинув, лінькувато-розслаблено",
]


# ── Shared Gemini transport: cooldown + 429 backoff + session circuit breaker ──────
# Ported from multicombine's core/gemini_client. Without it, a single 429 (free-tier
# rate limit) silently dropped every AI variation for the rest of the run; now we serialise
# calls, back off on quota errors, and — once the daily quota is truly gone — flip a
# breaker so the rest of the session skips Gemini instantly and falls back to local spintax
# (no more pointless 20s timeouts per video).
_GEMINI_LOCK = asyncio.Lock()
_COOLDOWN_SEC = 4.0                 # min gap between calls (free tier is ~15 rpm)
_BACKOFF_STEPS = [30, 90, 180]     # seconds to wait after a 429 before retrying
_last_call: float = 0.0
_quota_exhausted: bool = False     # circuit breaker — set once quota is gone for the session


def gemini_quota_exhausted() -> bool:
    """True once a 429 has tripped the breaker — callers may skip Gemini entirely."""
    return _quota_exhausted


async def _gemini_request(body: dict, *, timeout: float = 20,
                          fast_fail: bool = False) -> dict | None:
    """Serialised Gemini POST with inter-call cooldown, 429 backoff and a session-wide
    circuit breaker. Returns the parsed JSON dict, or None on no-key / open-breaker /
    quota-exhausted / HTTP / network error, so every caller can fall back gracefully.

    fast_fail=True (replies, optional work): one quick retry instead of the 30/90/180s
    schedule, so an optional call never blocks a run for minutes.
    """
    global _last_call, _quota_exhausted
    api_key = config.GEMINI_API_KEY
    if not api_key:
        return None
    if _quota_exhausted:           # breaker open — don't even try
        return None

    url = _GEMINI_URL.format(model=config.GEMINI_MODEL)
    backoff_schedule = [0, 5] if fast_fail else [0, *_BACKOFF_STEPS]
    ct = aiohttp.ClientTimeout(total=timeout)

    async with _GEMINI_LOCK:
        # Enforce the inter-request cooldown (the lock makes all Gemini calls serial).
        elapsed = time.monotonic() - _last_call
        if elapsed < _COOLDOWN_SEC:
            await asyncio.sleep(_COOLDOWN_SEC - elapsed)

        for attempt, backoff in enumerate(backoff_schedule, start=1):
            if backoff:
                print(f"[ai] Gemini backoff {attempt}/{len(backoff_schedule)} — "
                      f"sleeping {backoff}s", flush=True)
                await asyncio.sleep(backoff)
            try:
                async with aiohttp.ClientSession(timeout=ct) as session:
                    async with session.post(url, params={"key": api_key}, json=body) as resp:
                        _last_call = time.monotonic()
                        if resp.status == 200:
                            return await resp.json()
                        text = (await resp.text())[:300]
                        is_quota = (resp.status == 429 or "RESOURCE_EXHAUSTED" in text
                                    or "quota" in text.lower())
                        if is_quota:
                            print(f"[ai] Gemini 429/quota: {text[:120]}", flush=True)
                            if attempt >= len(backoff_schedule):
                                _quota_exhausted = True
                                print("[ai] Gemini circuit breaker tripped — local "
                                      "fallback for the rest of the session", flush=True)
                                return None
                            continue   # retry after the next backoff step
                        # Any other HTTP error is not a rate limit — don't retry.
                        print(f"[ai] Gemini HTTP {resp.status}: {text[:120]}", flush=True)
                        return None
            except Exception as e:
                # Network/timeout blip: fail fast (no long backoff), caller uses fallback.
                print(f"[ai] Gemini error: {e}", flush=True)
                return None
    return None


def _clean_line(line: str) -> str:
    line = _LIST_PREFIX.sub("", line.strip())
    return line.strip().strip('"').strip("'").strip()


def _is_meta(line: str) -> bool:
    """A header/preamble line, not an actual comment variant."""
    return line.endswith(":") or bool(_META_LINE.search(line))


async def generate_comment_variants(seed: str, n: int = 12) -> list:
    """Ask Gemini for `n` short rewrites of `seed`, in the SAME language as the seed.

    Returns a list of distinct comment strings, or [] on any problem (no key,
    network error, bad response) so the caller can fall back gracefully.
    """
    api_key = config.GEMINI_API_KEY
    if not api_key or not seed.strip():
        return []

    # Spread the requested variants across distinct human voices (round-robin), so the
    # pool reads like many different people rather than one templated style.
    voices = ", ".join(f"«{_VOICES[i % len(_VOICES)]}»" for i in range(min(n, len(_VOICES))))
    prompt = (
        "Перефразуй коментар нижче кількома способами для TikTok. Головне завдання — "
        "щоб варіанти НЕ були байт-в-байт однакові (це головний тригер тіньового фільтра), "
        "але кожен зберігав ТОЙ САМИЙ зміст і заклик, що й оригінал.\n\n"
        f"Оригінальний коментар: \"{seed.strip()}\"\n\n"
        f"Це коментарі під відео в TikTok — пиши як звичайний юзер у коментах, недбало й живо.\n"
        f"Згенеруй {n} варіантів РІЗНИМИ голосами/тоном, ніби це різні люди "
        f"(наприклад: {voices}). Голос змінює лише ТОН і добір слів — НЕ зміст.\n"
        "Правила:\n"
        "- ЗБЕРІГАЙ РЕГІСТР І СТИЛЬ ОРИГІНАЛУ: якщо написано з маленької літери, сленгом, "
        "без розділових знаків чи з помилками — лишай так само. НЕ «вилизуй» граматику, "
        "НЕ роби офіційним, НЕ додавай зайвих ком/крапок/знаків оклику;\n"
        "- зберігай рівень неформальності: сленг лишай сленгом ('шо', 'работяги', 'го', "
        "'норм' тощо), не заміняй на літературні відповідники;\n"
        "- ЛЕГКИЙ перефраз: та сама думка і заклик — лише інші слова/порядок/синоніми/тон;\n"
        "- НЕ вигадуй нову ідею, НЕ змінюй тему, НЕ перетворюй на 'інтригу' чи питання, "
        "якщо в оригіналі цього не було;\n"
        "- зберігай приблизну довжину оригіналу (не роздувай і не обрізай сенс);\n"
        "- та сама мова, що й в оригіналі; емодзі лише якщо вони були в оригіналі;\n"
        "- БЕЗ хештегів, БЕЗ нумерації, БЕЗ лапок; кожен варіант з нового рядка;\n"
        "- НЕ пиши жодних вступних чи пояснювальних рядків — одразу самі варіанти."
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        # Moderately high temperature: enough spread for distinct voices, still faithful.
        "generationConfig": {"temperature": 0.85, "topP": 0.95},
    }

    try:
        data = await _gemini_request(body, timeout=20)
        if not data:
            return []

        text = data["candidates"][0]["content"]["parts"][0]["text"]
        variants = []
        seen = set()
        for raw in text.splitlines():
            line = _clean_line(raw)
            if line and not _is_meta(line) and line.lower() not in seen:
                seen.add(line.lower())
                variants.append(line)

        # Length guard: prefer short comments (long promo gets shadow-filtered hardest).
        # Drop > MAX_LEN, but if that leaves too few, keep the full set as a fallback.
        MAX_LEN = 160
        short = [v for v in variants if len(v) <= MAX_LEN]
        if len(short) >= 3:
            variants = short

        print(f"[ai] Gemini generated {len(variants)} variants", flush=True)
        return variants
    except Exception as e:
        print(f"[ai] Gemini error: {e}", flush=True)
        return []


async def generate_reply(comment: str) -> str:
    """Short, friendly reply to a comment under our own video (engagement boost).
    Returns "" on no key / failure so the caller can skip that comment."""
    api_key = config.GEMINI_API_KEY
    if not api_key or not comment.strip():
        return ""

    prompt = (
        "Ти — власник TikTok-акаунту і відповідаєш на коментар під своїм відео. "
        "Відповідай ДУЖЕ коротко (1-2 речення), дружньо і природно, тією ж мовою, що й "
        "коментар. БЕЗ реклами, посилань, хештегів і лапок. Лише сам текст відповіді.\n\n"
        f"Коментар: \"{comment.strip()}\"\n"
        "Відповідь:"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8, "topP": 0.95},
    }
    try:
        # Replies are optional engagement — fast_fail so a 429 never stalls the run.
        data = await _gemini_request(body, timeout=20, fast_fail=True)
        if not data:
            return ""
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return _clean_line(text.splitlines()[0] if text.splitlines() else text)[:150]
    except Exception as e:
        print(f"[ai] reply error: {e}", flush=True)
        return ""


async def generate_analytics_report(summary: str) -> str:
    """Ask Gemini for a short, actionable recommendation based on the account stats
    summary. Returns "" if no key / failure so the caller can skip the section."""
    api_key = config.GEMINI_API_KEY
    if not api_key or not summary.strip():
        return ""

    prompt = (
        "Ти — аналітик TikTok-автоматизації. Нижче статистика роботи бота по акаунтах "
        "(завантаження, коментарі, прогрів). Дай КОРОТКИЙ звіт українською (макс 6-8 рядків):\n"
        "- 2-3 спостереження (що добре, що погано);\n"
        "- 2-3 конкретні поради (прогрів, проксі, ризик тіньового бану, рівномірність активності);\n"
        "- без води, без повторення цифр дослівно, по суті.\n\n"
        f"Статистика:\n{summary.strip()}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "topP": 0.9},
    }
    try:
        data = await _gemini_request(body, timeout=25)
        if not data:
            return ""
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[ai] report error: {e}", flush=True)
        return ""
