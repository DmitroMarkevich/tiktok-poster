from __future__ import annotations
import asyncio
import os
import random
import string
import subprocess
import tempfile


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


_FFMPEG_OK = _ffmpeg_available()


async def fingerprint_video(src_path: str, account_id: int) -> str:
    """
    Return a path to a visually-identical but mathematically unique copy of
    the video, seeded from account_id.  If ffmpeg is unavailable, returns
    src_path unchanged.
    """
    if not _FFMPEG_OK:
        return src_path

    rng = random.Random(account_id ^ 0xC0FFEE)

    # Crop offsets: remove 1–3 px from a random pair of edges.
    # Each account gets a different (dx, dy, ox, oy) so the pixel hash differs.
    dx = rng.randint(1, 3)
    dy = rng.randint(1, 3)
    ox = rng.choice([0, dx])
    oy = rng.choice([0, dy])

    # Imperceptible brightness tweak: ±0.01
    brightness = round(rng.uniform(-0.01, 0.01), 4)

    # Random metadata noise
    title   = "".join(rng.choices(string.ascii_letters + string.digits, k=12))
    comment = "".join(rng.choices(string.ascii_letters + string.digits, k=8))

    dst_fd, dst_path = tempfile.mkstemp(
        suffix=".mp4",
        dir=os.path.dirname(src_path),
        prefix=f"fp{account_id}_",
    )
    os.close(dst_fd)

    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-vf", f"crop=iw-{dx}:ih-{dy}:{ox}:{oy},eq=brightness={brightness}",
        "-metadata", f"title={title}",
        "-metadata", f"comment={comment}",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac",
        dst_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    if proc.returncode != 0 or not os.path.getsize(dst_path):
        try: os.remove(dst_path)
        except OSError: pass
        return src_path   # fallback to original on ffmpeg error

    return dst_path
