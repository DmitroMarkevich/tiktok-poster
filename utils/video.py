from __future__ import annotations
import asyncio
import datetime
import os
import random
import string
import subprocess
import tempfile


def _meta_args(rng: random.Random) -> list:
    """ffmpeg args that scrub the container fingerprint.

    ffmpeg otherwise stamps every output with the SAME `encoder=LavfXX.X.X` atom — a constant
    "made by ffmpeg on this box" signature shared across all our videos. `-fflags +bitexact`
    drops that atom (ffmpeg overrides a custom -metadata encoder=, so removal is the only
    reliable option); `-map_metadata -1` strips any inherited tags; and a randomised recent
    `creation_time` (which survives bitexact) gives each upload a distinct, plausible date.
    (The generic x264 SEI options string is left as-is: it's identical for any default x264
    encode worldwide, so it can't cluster *our* accounts and removing it needs bitstream
    surgery for no real gain.)"""
    dt = datetime.datetime.utcnow() - datetime.timedelta(
        days=rng.randint(0, 30), seconds=rng.randint(0, 86399))
    ct = dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return ["-fflags", "+bitexact", "-flags:v", "+bitexact",
            "-map_metadata", "-1",
            "-metadata", f"creation_time={ct}"]


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


_FFMPEG_OK = _ffmpeg_available()


MUSIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "music")
_MUSIC_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".ogg")


def _pick_music() -> str:
    """Random track from assets/music/, or '' if the folder is empty/absent. TikTok's own
    licensed catalog can't be baked into an uploaded video, so we overlay our own files."""
    try:
        tracks = [os.path.join(MUSIC_DIR, f) for f in os.listdir(MUSIC_DIR)
                  if f.lower().endswith(_MUSIC_EXTS)]
    except OSError:
        return ""
    return random.choice(tracks) if tracks else ""


# Slide transitions we rotate through so every slideshow has a different visual rhythm.
_TRANSITIONS = (
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "slideleft", "slideright", "fade", "fadeblack",
    "circleopen", "circleclose", "radial", "dissolve",
)


def _uniquify_chain(rng: random.Random) -> str:
    """Build a per-image ffmpeg filter chain that makes a photo perceptually UNIQUE while
    staying visually near-identical. Unlike cosmetic brightness/hue tweaks (which TikTok's
    pHash is robust to), this moves the geometry — zoom-crop, optional mirror, micro-rotate,
    off-centre padding — which is what actually shifts a perceptual hash. Output is always
    normalised to a 1080×1920 vertical frame so xfade inputs stay identical.
    """
    # --- geometry (the part pHash actually notices) ---
    # NB: no horizontal flip — photos may contain text, and mirroring makes it unreadable.

    # Wider, ASYMMETRIC zoom (separate x/y) — a non-uniform crop changes the aspect of the
    # retained region, which shifts a perceptual hash far more than a uniform zoom in a narrow
    # band. Kept ≤20% so the photo stays clearly intact and any text remains readable.
    zfx = round(rng.uniform(0.80, 0.94), 4)         # zoom in 6–20% horizontally
    zfy = round(rng.uniform(0.80, 0.94), 4)         # …and a different amount vertically
    ox = round(rng.uniform(0, 1 - zfx), 4)          # random crop window position
    oy = round(rng.uniform(0, 1 - zfy), 4)

    ang = round(rng.uniform(-2.2, 2.2), 3)          # micro-rotate, degrees

    fx = round(rng.uniform(0.25, 0.75), 3)          # off-centre pad position
    fy = round(rng.uniform(0.25, 0.75), 3)

    # --- imperceptible colour / texture noise ---
    brightness = round(rng.uniform(-0.03, 0.03), 4)
    contrast   = round(rng.uniform(0.97, 1.03), 4)
    saturation = round(rng.uniform(0.95, 1.05), 4)
    gamma      = round(rng.uniform(0.97, 1.03), 4)
    hue_h      = round(rng.uniform(-4, 4), 2)
    noise      = rng.randint(3, 6)
    sharp      = round(rng.uniform(0.2, 0.6), 3)

    return (
        f"crop=iw*{zfx}:ih*{zfy}:iw*{ox}:ih*{oy},"
        f"rotate={ang}*PI/180:fillcolor=black,"
        # chop the black corners the rotation introduced
        f"crop=iw*0.94:ih*0.94:(iw-iw*0.94)/2:(ih-ih*0.94)/2,"
        f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}:gamma={gamma},"
        f"hue=h={hue_h},"
        # Spatial-only noise (allf=u): a per-account static grain. NOT temporal (t) — that
        # regenerates every frame, killing inter-frame compression and bloating the mp4 3×
        # (slow uploads over a proxy) for no real dedup gain on a still slideshow frame.
        f"noise=alls={noise}:allf=u,"
        f"unsharp=5:5:{sharp}:5:5:0.0,"
        f"scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad=1080:1920:(ow-iw)*{fx}:(oh-ih)*{fy}:black,"
        f"setsar=1,fps=30,format=yuv420p"
    )


async def uniquify_image(src_path: str, out_path: str = None,
                         seed: int = None) -> str:
    """Return a path to a visually-near-identical but byte/perceptually-UNIQUE copy of an
    image, with fresh randomness each call (so the same photo posted twice looks different
    to TikTok's dedup). Moves the geometry — optional mirror, zoom-crop, micro-rotate — which
    is what actually shifts a perceptual hash, plus tiny brightness/contrast/saturation/gamma/
    hue tweaks, film grain, light sharpen, strips EXIF and re-encodes at a random JPEG quality.
    Preserves the original aspect ratio. Falls back to the original if ffmpeg fails/absent.
    """
    if not _FFMPEG_OK:
        return src_path

    rng = random.Random(seed)  # seed=None → different every run

    # No horizontal flip — photos may contain text, which mirroring would make unreadable.
    # Asymmetric x/y zoom (see _uniquify_chain): a non-uniform crop shifts pHash much more
    # than a uniform zoom in a narrow band, while staying visually intact (≤20%).
    zfx = round(rng.uniform(0.80, 0.94), 4)         # zoom in 6–20% horizontally
    zfy = round(rng.uniform(0.80, 0.94), 4)         # …different amount vertically
    ox = round(rng.uniform(0, 1 - zfx), 4)
    oy = round(rng.uniform(0, 1 - zfy), 4)
    ang = round(rng.uniform(-2.2, 2.2), 3)          # micro-rotate, degrees
    brightness = round(rng.uniform(-0.03, 0.03), 4)
    contrast   = round(rng.uniform(0.97, 1.03), 4)
    saturation = round(rng.uniform(0.95, 1.05), 4)
    gamma      = round(rng.uniform(0.97, 1.03), 4)
    hue_h      = round(rng.uniform(-4, 4), 2)
    noise      = rng.randint(3, 6)
    sharp      = round(rng.uniform(0.2, 0.6), 3)
    quality    = rng.randint(3, 7)          # ffmpeg -q:v (lower = better); varies file hash

    # Geometry first (zoom-crop + rotate, then chop the rotation corners), then colour/noise.
    # Aspect ratio is preserved — no forced resize.
    vf = (
        f"crop=iw*{zfx}:ih*{zfy}:iw*{ox}:ih*{oy},"
        f"rotate={ang}*PI/180:fillcolor=black,"
        f"crop=iw*0.94:ih*0.94:(iw-iw*0.94)/2:(ih-ih*0.94)/2,"
        f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}:gamma={gamma},"
        f"hue=h={hue_h},"
        f"noise=alls={noise}:allf=t+u,"
        f"unsharp=5:5:{sharp}:5:5:0.0"
    )

    if out_path is None:
        fd, out_path = tempfile.mkstemp(suffix=".jpg", prefix="uniq_",
                                        dir=os.path.dirname(src_path))
        os.close(fd)

    cmd = ["ffmpeg", "-y", "-i", src_path, "-vf", vf,
           "-map_metadata", "-1", "-q:v", str(quality), out_path]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    if proc.returncode != 0 or not os.path.exists(out_path) or not os.path.getsize(out_path):
        try: os.remove(out_path)
        except OSError: pass
        return src_path
    return out_path


async def make_slideshow(
    image_paths: list,
    out_path: str = None,
    seconds_per_image: float = None,
    transition: str = None,
    transition_dur: float = None,
    music_path: str = None,
    uniquify: bool = True,
    seed: int = None,
) -> str:
    """Turn a list of images into a vertical (1080×1920) TikTok slideshow video where
    each photo is shown for ~`seconds_per_image` and flips to the next with a transition.
    Returns the output mp4 path. Raises on ffmpeg failure / missing ffmpeg.

    Uniquification (default on): each photo is run through a randomized geometry/colour/
    noise chain (see `_uniquify_chain`) IN THIS SAME PASS — no separate per-image encode —
    and the timing, transitions and music (tempo + random start offset) are randomized too,
    so the same set of photos posted twice yields a different perceptual + audio fingerprint.
    Pass `seed` for a reproducible result, or `uniquify=False` for a plain centred slideshow.

    Background music: if `music_path` is given it's overlaid; if None, a random track is
    picked from assets/music/ (silent video when that folder is empty).

    This is how we publish "carousels" — the web has no photo upload, so we render the
    photos to a video and post it through the normal (working) video upload flow.
    """
    if not _FFMPEG_OK:
        raise RuntimeError("ffmpeg недоступний — не можу зібрати слайдшоу.")
    if len(image_paths) < 2:
        raise ValueError("Слайдшоу потребує щонайменше 2 фото.")

    rng = random.Random(seed)  # seed=None → different every run

    if out_path is None:
        fd, out_path = tempfile.mkstemp(suffix=".mp4", prefix="slideshow_",
                                        dir=os.path.dirname(image_paths[0]))
        os.close(fd)

    # Randomize timing so the video duration / rhythm differs per post.
    seg = seconds_per_image if seconds_per_image is not None else (
        round(rng.uniform(2.8, 3.6), 2) if uniquify else 3.0)
    Tdur = transition_dur if transition_dur is not None else (
        round(rng.uniform(0.45, 0.75), 2) if uniquify else 0.6)
    T = min(Tdur, seg - 0.1)  # transition must be shorter than a segment
    total_dur = round(len(image_paths) * seg - (len(image_paths) - 1) * T, 3)

    if music_path is None:
        music_path = _pick_music()
        # assets/music/ empty → pull a track from YouTube (yt-dlp) and cache it there.
        if not music_path:
            try:
                from utils.music import ensure_random_track
                music_path = await ensure_random_track()
            except Exception as e:
                print(f"[video] music fetch failed, silent slideshow: {e}", flush=True)
                music_path = ""

    # Each image: looped for `seg`s, then normalized so every clip shares size/fps/format
    # (xfade requires identical inputs).
    cmd = ["ffmpeg", "-y"]
    for p in image_paths:
        cmd += ["-loop", "1", "-t", f"{seg}", "-i", p]
    music_offset = round(rng.uniform(0, 30), 2) if uniquify else 0
    if music_path:
        # Loop the track infinitely so it always outlasts the video (trimmed via -shortest).
        # The random start offset is applied later as an audio-FILTER (atrim) on this already-
        # infinite stream — NOT as an input -ss, which interacts badly with -stream_loop and
        # could truncate the whole video (and drop carousel photos).
        cmd += ["-stream_loop", "-1", "-i", music_path]

    if uniquify:
        parts = [f"[{i}:v]{_uniquify_chain(rng)}[v{i}]" for i in range(len(image_paths))]
    else:
        norm = (
            "scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p"
        )
        parts = [f"[{i}:v]{norm}[v{i}]" for i in range(len(image_paths))]

    # Chain xfade: offset of the k-th transition = k*(seg - T). Pick a (possibly different)
    # transition per boundary when uniquifying.
    last = "v0"
    for k in range(1, len(image_paths)):
        offset = round(k * (seg - T), 3)
        out = f"x{k}"
        trans = transition or (rng.choice(_TRANSITIONS) if uniquify else "smoothleft")
        parts.append(
            f"[{last}][v{k}]xfade=transition={trans}:duration={T}:offset={offset}[{out}]"
        )
        last = out

    cmd += ["-filter_complex"]
    if music_path:
        music_idx = len(image_paths)
        fade_st = max(total_dur - 0.8, 0)
        # Random start offset (on the infinite looped stream → safe, never truncates) +
        # slight tempo shift both perturb the audio fingerprint.
        pre = (f"atrim=start={music_offset},asetpts=PTS-STARTPTS,"
               f"atempo={round(rng.uniform(0.95, 1.05), 4)},") if uniquify else ""
        parts.append(
            f"[{music_idx}:a]{pre}afade=t=in:st=0:d=0.5,"
            f"afade=t=out:st={fade_st}:d=0.8[aud]"
        )
        cmd += [";".join(parts), "-map", f"[{last}]", "-map", "[aud]",
                "-c:a", "aac", "-b:a", "160k", "-shortest"]
    else:
        cmd += [";".join(parts), "-map", f"[{last}]"]

    cmd += [
        # CRF 18 (visually lossless-ish) + a slower preset → noticeably crisper photos
        # than the old crf22/veryfast, at the cost of a few extra seconds of encode time.
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        *_meta_args(rng),
        out_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(out_path) or not os.path.getsize(out_path):
        try: os.remove(out_path)
        except OSError: pass
        tail = (stderr or b"").decode(errors="ignore")[-400:]
        raise RuntimeError(f"ffmpeg не зібрав слайдшоу: {tail}")

    return out_path


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
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac",
        # Strip inherited metadata, then stamp randomised encoder/creation_time + the
        # per-account title/comment noise (order matters: -map_metadata -1 wipes first).
        *_meta_args(rng),
        "-metadata", f"title={title}",
        "-metadata", f"comment={comment}",
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
