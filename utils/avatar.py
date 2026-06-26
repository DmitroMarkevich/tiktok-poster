"""Fetch a random, casual profile photo and make it perceptually UNIQUE per account.

TikTok can ban accounts that share an identical avatar (same file/perceptual hash), so every
account must get a different picture. We pull a fresh random photo from a public source each
time (infinite, never repeats) and run it through the existing ffmpeg uniquifier so even two
downloads of the same image end up byte- and pHash-distinct.
"""
from __future__ import annotations

import logging
import os
import random
import tempfile

import aiohttp

from utils.video import uniquify_image

logger = logging.getLogger(__name__)

# Casual, non-face vibes similar to the references the user gave (flowers / kittens / aesthetic).
_TAGS = ["flowers", "kitten", "cat", "nature", "aesthetic", "flower,pink", "puppy", "sky,sunset"]

# Public, key-less random-image sources. loremflickr returns a real Flickr photo matching tags;
# picsum is a generic random photo fallback.
def _sources(n: int) -> list[str]:
    tag = random.choice(_TAGS)
    return [
        f"https://loremflickr.com/640/640/{tag}?random={n}",
        f"https://picsum.photos/640?random={n}",
    ]


async def get_unique_avatar(out_dir: str | None = None) -> str | None:
    """Download a random photo and return a path to a uniquified JPEG, or None on failure."""
    out_dir = out_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    n = random.randint(1, 1_000_000)
    raw = os.path.join(out_dir, f"avatar_raw_{n}.jpg")

    data = None
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in _sources(n):
            try:
                async with session.get(url, allow_redirects=True) as r:
                    if r.status == 200:
                        body = await r.read()
                        if len(body) > 2_000:  # guard against error/placeholder pages
                            data = body
                            break
            except Exception as e:
                logger.warning("avatar fetch failed (%s): %s", url, e)
    if not data:
        logger.error("could not download a random avatar")
        return None

    with open(raw, "wb") as f:
        f.write(data)

    # Uniquify (geometry + colour + noise + re-encode) so the hash differs every time.
    try:
        uniq = await uniquify_image(raw, os.path.join(out_dir, f"avatar_{n}.jpg"))
    except Exception as e:
        logger.warning("avatar uniquify failed (%s) — using raw", e)
        uniq = raw
    return uniq
