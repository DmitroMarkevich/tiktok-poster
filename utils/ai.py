"""Free AI comment variation via Google Gemini.

One call returns a pool of natural, varied rewrites of the user's seed comment;
the commenter rotates through that pool so no two comments are byte-identical
(the main TikTok shadow-filter trigger). Any failure returns [] and the caller
falls back to the local spintax variation in commenter.render_comment.
"""
import asyncio
import re

import aiohttp

import config

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Strip leading list markers ("1.", "-", "•", quotes) the model sometimes adds.
_LIST_PREFIX = re.compile(r"^\s*(?:\d+[.)]\s*|[-*•]\s*)")

# Meta/preamble lines the model sometimes prepends ("Ось 8 варіантів...:") — these
# are NOT comments and must never be posted.
_META_LINE = re.compile(r"(?:варіант|перефраз|ось\s+\d|here are|variants?)", re.IGNORECASE)


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

    prompt = (
        "Перефразуй коментар нижче кількома способами для TikTok. Головне завдання — "
        "щоб варіанти НЕ були байт-в-байт однакові (це головний тригер тіньового фільтра), "
        "але кожен зберігав ТОЙ САМИЙ зміст і заклик, що й оригінал.\n\n"
        f"Оригінальний коментар: \"{seed.strip()}\"\n\n"
        f"Згенеруй {n} варіантів. Правила:\n"
        "- ЛЕГКИЙ перефраз: ті самі думка, тон і заклик — лише інші слова/порядок/синоніми;\n"
        "- НЕ вигадуй нову ідею, НЕ змінюй тему, НЕ перетворюй на 'інтригу' чи питання, "
        "якщо в оригіналі цього не було;\n"
        "- зберігай приблизну довжину оригіналу (не роздувай і не обрізай сенс);\n"
        "- звучи природно, як жива людина;\n"
        "- та сама мова, що й в оригіналі; емодзі лише якщо вони були в оригіналі;\n"
        "- БЕЗ хештегів, БЕЗ нумерації, БЕЗ лапок; кожен варіант з нового рядка;\n"
        "- НЕ пиши жодних вступних чи пояснювальних рядків — одразу самі варіанти."
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        # Lower temperature: variants must stay faithful to the seed, just not identical.
        "generationConfig": {"temperature": 0.7, "topP": 0.9},
    }
    url = _GEMINI_URL.format(model=config.GEMINI_MODEL)

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, params={"key": api_key}, json=body) as resp:
                if resp.status != 200:
                    print(f"[ai] Gemini HTTP {resp.status}: {(await resp.text())[:200]}", flush=True)
                    return []
                data = await resp.json()

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
