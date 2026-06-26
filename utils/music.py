"""Fetch background music for slideshows via yt-dlp (YouTube audio search).

Downloads the best audio for a track name, transcodes to mp3, and caches it in
assets/music/ so the same track is fetched at most once. make_slideshow falls back to
fetching a random track from DEFAULT_TRACKS when assets/music/ is empty.

Network/yt-dlp failures are swallowed (return None) — a slideshow without music is fine,
it must never block an upload.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

MUSIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "music")

# Default rotation pulled on demand when assets/music/ is empty. Edit freely — each entry is
# a plain YouTube search query ("artist - title"); the first audio hit is downloaded.
DEFAULT_TRACKS = [
    "MIA BOYKA - Аэропорты",
    "Dead Blonde - Бесприданница",
    "DEAD BLONDE - Банкомат",
    "SAYAN - Мальборо",
    "Mr Lambo - Mango",
    "Ace of Base - Happy Nation",
    "Jah Khalib - На параллельных",
    "MIA BOYKA - Зомби",
    "Dead Blonde - Мальчик на девятке",
    "INSTASAMKA - За деньги да",
    "Mr Lambo - Лагуна",
    "SAYAN - Дисциплина",
    "Три дня дождя - Демоны",
    "ANNA ASTI - Феникс",
    "Macan - Кричу",
    "Айсберг - Карина",
    "JONY - Комета",
    "Niletto - Любимка",
    "Markul - Между нами",
    "GAYAZOV BROTHER - Малиновая Лада",
    "Egor Kreed - Сердцеедка",
    "Mona - Falling",
    "Звонкий - Босяк",
    "HENSY - Краш",
]

_MUSIC_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".ogg")


def _slug(query: str) -> str:
    """Filesystem-safe cache key for a search query."""
    s = re.sub(r"[^\w\-]+", "_", query.strip().lower(), flags=re.UNICODE)
    return s.strip("_")[:80] or "track"


def _cached_path(query: str) -> str | None:
    """Return an already-downloaded file for this query, or None."""
    base = os.path.join(MUSIC_DIR, _slug(query))
    for ext in _MUSIC_EXTS:
        if os.path.exists(base + ext):
            return base + ext
    return None


async def fetch_track(query: str, timeout: float = 90) -> str | None:
    """Download the first YouTube audio hit for `query` as mp3 into assets/music/.
    Returns the cached/downloaded path, or None on any failure."""
    os.makedirs(MUSIC_DIR, exist_ok=True)
    cached = _cached_path(query)
    if cached:
        return cached

    out_tmpl = os.path.join(MUSIC_DIR, _slug(query) + ".%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--no-playlist", "--no-warnings", "--quiet",
        # The default `web` player client frequently returns HTTP 403 on the audio stream;
        # the android/ios clients hand back working URLs, so prefer them.
        "--extractor-args", "youtube:player_client=android,ios,web",
        # Pick a short-ish official-ish result; ytsearch1 = first hit for the query.
        "-o", out_tmpl,
        f"ytsearch1:{query} audio",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            print(f"[music] timeout fetching: {query}", flush=True)
            return None
    except Exception as e:
        print(f"[music] yt-dlp error for '{query}': {e}", flush=True)
        return None

    path = _cached_path(query)
    if not path:
        tail = (stderr or b"").decode(errors="ignore")[-300:]
        print(f"[music] no file produced for '{query}': {tail}", flush=True)
        return None
    return path


async def ensure_random_track() -> str | None:
    """Pick a random track from DEFAULT_TRACKS and ensure it's downloaded.
    Returns its path, or None if the fetch failed (caller renders a silent slideshow)."""
    import random
    for query in random.sample(DEFAULT_TRACKS, len(DEFAULT_TRACKS)):
        path = await fetch_track(query)
        if path:
            return path
    return None
