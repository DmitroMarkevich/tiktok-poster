"""
TikTok sliding-puzzle CAPTCHA solver.

Primary: OpenCV template matching (free, ~50 ms).
Fallback: 2captcha / CapSolver (paid, ~10-30 s).

How it works:
  TikTok shows two images — a background with a hole and a small piece.
  OpenCV finds where the piece fits in the background (template matching on
  edge maps), giving the x-offset to drag.  The result is then applied via
  a human-like mouse drag on the slider bar.
"""
from __future__ import annotations

import asyncio
import base64
import random
from typing import Optional

import aiohttp
from playwright.async_api import Page


# ── Selectors ──────────────────────────────────────────────────────────────

_CAPTCHA_CONTAINERS = [
    "[data-testid='captcha-verify']",
    "#captcha-verify",
    "[class*='secsdk-captcha']",
    "[class*='captcha-verify']",
    "[class*='verify-captcha']",
    "[id*='captcha']",
]

# Background image: the large image with a cutout hole
_BG_SELECTORS = [
    "img[class*='bg']",
    "img[class*='background']",
    "[class*='drag-icon--bg']",
    "[class*='img-slide'] img",
    "[class*='captcha_verify_img'] img",
]

# Piece image: the small puzzle piece to be dragged
_PIECE_SELECTORS = [
    "img[class*='tip']",
    "img[class*='piece']",
    "[class*='drag-icon--tip']",
    "[class*='drag-icon--piece']",
    "[class*='captcha_verify_bar'] img",
    "[class*='slide-piece'] img",
]

# Slider button / drag handle.
# 2024+ TikTok captcha redesign uses id="captcha_slide_button" + a draggable
# div.cap-absolute inside a cap-rounded-full track — added first so the new layout
# is matched before the legacy secsdk selectors.
_SLIDER_SELECTORS = [
    "#captcha_slide_button",
    "[draggable='true'][class*='cap-absolute']",
    "button[id*='slide']",
    "[class*='secsdk-captcha-drag-icon']",
    "[class*='captcha_verify_slide_bar']",
    "[class*='drag-icon']:not(img)",
    "[class*='slider-btn']",
    "[class*='drag-btn']",
    "[class*='slide-btn']",
]

# Rotation-captcha attempt budgets. A brute-force grid can't feasibly cover the small
# (<8px) acceptance window, so solving hinges on a good CV estimate:
#   • high-confidence estimate → fine search, up to _MAX attempts (solves in 1-3).
#   • low-confidence estimate  → a few quick guesses then bail and skip the video, so a
#     bad estimate costs ~15s, not a full ~57-118s sweep.
# _ROTATION_CONF_HIGH is the peak-sharpness threshold (offline-validated: a live solve
# had sharpness 5.2, a live failure 3.3).
_ROTATION_MAX_ATTEMPTS = 16
_ROTATION_LOWCONF_ATTEMPTS = 4
_ROTATION_CONF_HIGH = 4.0

# URL patterns that identify CAPTCHA image requests
_CAPTCHA_URL_PATTERNS = (
    "captcha", "verify", "secsdk", "tiktokv.com/captcha",
    "lf-webcast", "lf16-webcast", "p16-webcast",
)


# ── Public API ─────────────────────────────────────────────────────────────

async def solve_captcha_if_present(
    page: Page,
    api_key: str = "",
    service: str = "2captcha",
    max_attempts: int = 3,
) -> bool:
    """
    Detect TikTok CAPTCHA and solve it.
    Returns True when no CAPTCHA found or successfully solved.
    Handles both sliding-puzzle and rotation CAPTCHA types.
    """
    for attempt in range(max_attempts):
        captcha_frame, captcha_el = await _find_captcha(page)
        if not captcha_el:
            return True

        captcha_type = await _detect_captcha_type(page, captcha_frame)

        # Always snapshot the captcha images up front (regardless of type / whether the
        # solve later succeeds) so we can tune the solver on real samples.
        await _save_captcha_images(page, captcha_frame, label=captcha_type)
        # TEMP: dump DOM so we can fix _img_scope when a captcha layout slips it.
        await _dump_captcha_dom(captcha_frame, captcha_el, label=captcha_type)

        if captcha_type == "rotation":
            solved = await _solve_rotation(page, captcha_frame)
        else:
            # Sliding puzzle: try OpenCV first
            offset = await _opencv_offset(page, captcha_frame)
            if offset is not None:
                solved = await _apply_offset(page, captcha_frame, offset)
            elif api_key:
                # Paid fallback. An invalid/expired key makes the service raise
                # (e.g. ERROR_KEY_DOES_NOT_EXIST) — swallow it so a dead key never
                # kills the whole solve; we just fall through to solved=False and
                # let the OpenCV-only retries run.
                screenshot_b64 = await _screenshot_b64(captcha_el, page)
                try:
                    if service == "capsolver":
                        offset = await _capsolver_solve(screenshot_b64, api_key)
                    else:
                        offset = await _twocaptcha_solve(screenshot_b64, api_key)
                except Exception as e:
                    print(f"[captcha] paid solver ({service}) failed: {e}", flush=True)
                    offset = None
                solved = await _apply_offset(page, captcha_frame, offset) if offset else False
            else:
                solved = False

        await asyncio.sleep(1.5)
        # Verify captcha is gone
        _, still_present = await _find_captcha(page)
        if not still_present:
            return True

    return False


def _img_scope(captcha_frame):
    """Locator for images INSIDE the captcha widget only. Critical: the video page
    has avatars/thumbnails (e.g. 720×720) that are LARGER than the captcha images
    (~347/211), so an unscoped 'img' query picks the wrong ones → wrong captcha-type
    detection and garbage rotation estimates."""
    return captcha_frame.locator(
        ".captcha-verify-container img, #captcha-verify-container-main-page img, "
        "[class*='captcha-verify'] img, [class*='secsdk-captcha'] img"
    )


async def _scoped_captcha_images(captcha_frame, wait_ms: int = 2500):
    """Image element handles inside the captcha widget; falls back to all page
    images only if the scoped query never reaches two.

    Polls up to wait_ms because _find_captcha matches the captcha CONTAINER as soon
    as it appears, but the widget's own <img> elements paint a beat later. Querying
    immediately finds 0-1 captcha images → falls back to page avatars/thumbnails →
    the rotation captcha gets misclassified as 'slider' and never reaches the free
    OpenCV solver. Waiting for the real images fixes that without any paid service."""
    for _ in range(max(1, wait_ms // 250)):
        try:
            imgs = await _img_scope(captcha_frame).all()
            if len(imgs) >= 2:
                return imgs
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return await captcha_frame.locator("img").all()


async def _dump_captcha_dom(captcha_frame, captcha_el, label: str = "captcha") -> None:
    """TEMP DIAGNOSTIC: dump the captcha container's HTML + every <img> (inside the
    widget AND in the whole frame) with class/id/natural size/src to
    /tmp/captcha_debug/<ts>_<label>_dom.txt. Lets us discover the real <img>
    selectors when _img_scope picks page junk instead of the captcha images.
    Best-effort; remove once scoping covers all captcha layouts."""
    try:
        import os, time
        out_dir = "/tmp/captcha_debug"
        os.makedirs(out_dir, exist_ok=True)
        ts = int(time.time())
        lines = []
        try:
            html = await captcha_el.evaluate("el => el.outerHTML.slice(0, 6000)")
            lines.append("=== CONTAINER outerHTML (6k) ===")
            lines.append(html)
        except Exception as e:
            lines.append(f"[container html failed: {e}]")

        js = (
            "els => Array.from(els).map(i => JSON.stringify({"
            "cls: i.className, id: i.id, w: i.naturalWidth, h: i.naturalHeight,"
            "src: (i.src||'').slice(0,90)}))"
        )
        try:
            inside = await captcha_el.evaluate(f"el => ({js})(el.querySelectorAll('img'))")
            lines.append(f"\n=== <img> INSIDE container ({len(inside)}) ===")
            lines.extend(inside)
        except Exception as e:
            lines.append(f"[container img enum failed: {e}]")
        try:
            allimg = await captcha_frame.evaluate(f"() => ({js})(document.querySelectorAll('img'))")
            lines.append(f"\n=== ALL <img> in frame ({len(allimg)}) ===")
            lines.extend(allimg)
        except Exception as e:
            lines.append(f"[frame img enum failed: {e}]")

        path = f"{out_dir}/{ts}_{label}_dom.txt"
        with open(path, "w") as f:
            f.write("\n".join(lines))
        print(f"[captcha] dumped DOM → {path}", flush=True)
    except Exception:
        pass


async def _save_captcha_images(page: Page, captcha_frame, label: str = "captcha") -> None:
    """Save every visible captcha image to /tmp/captcha_debug (best-effort). Runs as
    soon as a captcha is detected so we always capture real samples for solver tuning."""
    try:
        import os, time
        out_dir = "/tmp/captcha_debug"
        os.makedirs(out_dir, exist_ok=True)
        ts = int(time.time())
        n = 0
        for el in await _scoped_captcha_images(captcha_frame):
            try:
                if not await el.is_visible(timeout=300):
                    continue
                src = await el.get_attribute("src") or ""
                if not src:
                    continue
                if src.startswith("data:"):
                    raw = base64.b64decode(src.split(",", 1)[1])
                else:
                    resp = await page.request.get(src)
                    raw = await resp.body() if resp.ok else None
                if raw:
                    with open(f"{out_dir}/{ts}_{label}_{n}.webp", "wb") as f:
                        f.write(raw)
                    n += 1
            except Exception:
                continue
        if n:
            print(f"[captcha] saved {n} image(s) → {out_dir}/{ts}_{label}_*.webp", flush=True)
    except Exception:
        pass


async def _detect_captcha_type(page: Page, captcha_frame) -> str:
    """Returns 'rotation' or 'slider'.
    Rotation: two concentric images (same center, different sizes).
    Slider:   two images side by side.
    """
    try:
        imgs = await _scoped_captcha_images(captcha_frame)
        boxes = []
        for img in imgs:
            try:
                if await img.is_visible(timeout=400):
                    box = await img.bounding_box()
                    if box:
                        boxes.append(box)
            except Exception:
                pass

        if len(boxes) == 1:
            return "rotation"

        if len(boxes) >= 2:
            # Check if two largest images are concentric (same center ≈ rotation)
            boxes.sort(key=lambda b: b["width"] * b["height"], reverse=True)
            b1, b2 = boxes[0], boxes[1]
            c1x = b1["x"] + b1["width"] / 2
            c1y = b1["y"] + b1["height"] / 2
            c2x = b2["x"] + b2["width"] / 2
            c2y = b2["y"] + b2["height"] / 2
            if abs(c1x - c2x) < 25 and abs(c1y - c2y) < 25:
                return "rotation"
            return "slider"

    except Exception:
        pass
    return "slider"


async def _find_rotation_slider(captcha_frame):
    """Locate the rotation captcha's small drag handle (width < 100). Returns a
    locator or None. Used both initially and to RE-ACQUIRE the slider after TikTok
    reloads the captcha mid-sweep (the old slider detaches → a fresh one renders)."""
    for sel in _SLIDER_SELECTORS + [
        "[class*='captcha'] button[class*='default']",
        "[class*='verify'] button[class*='default']",
    ]:
        try:
            el = captcha_frame.locator(sel).first
            if await el.is_visible(timeout=600):
                box = await el.bounding_box(timeout=2000)
                if box and box["width"] < 100:
                    return el
        except Exception:
            continue
    return None


async def _solve_rotation(page: Page, captcha_frame) -> bool:
    """
    Solve TikTok rotation CAPTCHA.
    Gets fresh slider position before each drag (TikTok resets it after wrong answer).
    """
    slider_el = await _find_rotation_slider(captcha_frame)
    if not slider_el:
        print("[captcha] _solve_rotation: slider element NOT found")
        return False

    initial_box = await slider_el.bounding_box(timeout=3000)
    if not initial_box:
        print("[captcha] _solve_rotation: slider bounding_box() is None")
        return False

    print(f"[captcha] slider box: x={initial_box['x']:.0f} y={initial_box['y']:.0f} w={initial_box['width']:.0f} h={initial_box['height']:.0f}")
    track_width = await _get_slider_track_width(captcha_frame, initial_box)
    print(f"[captcha] track_width={track_width:.0f}px")

    # Build list of fractions to try: OpenCV estimate first, then the 36 evenly
    # spaced fallback positions ORDERED BY DISTANCE from that estimate. If OpenCV is
    # close but not pixel-exact, the correct position is now hit in 1-3 tries instead
    # of the old linear 1→35 sweep (which once landed only on attempt 32, ~100s).
    fraction, conf = await _rotation_fraction_opencv(page, captcha_frame)
    print(f"[captcha] opencv fraction={fraction} conf={conf:.1f}")

    # Strategy by CONFIDENCE (offline-validated: peak sharpness predicts correctness).
    # The acceptance window is < ~8px, so a brute-force grid would need ~70 positions to
    # guarantee a hit — too slow. Reliable solving therefore depends on a GOOD estimate:
    #   • high conf → fine ~3px search around the estimate + its mirror (the CV gives the
    #     angle, not the drag direction), capped low → solves in 1-3 tries.
    #   • low conf  → the estimate can't be trusted and brute force won't pay off, so try
    #     only a few cheap guesses then bail FAST and skip the video (plenty more exist).
    px = 1.0 / max(track_width, 1.0)               # one pixel as a fraction of the track
    if fraction is not None and conf >= _ROTATION_CONF_HIGH:
        centers = [fraction % 1.0, (1.0 - fraction) % 1.0]
        fine_px = (0, 3, -3, 6, -6, 9, -9, 12, -12)   # land inside the window
        fracs = [(c + off * px) % 1.0 for c in centers for off in fine_px]
        cap = _ROTATION_MAX_ATTEMPTS
    else:
        # Untrusted estimate: a handful of quick guesses (estimate, mirror, a couple of
        # neighbours) then give up — don't burn a full sweep on a bad lead.
        if fraction is not None:
            centers = [fraction % 1.0, (1.0 - fraction) % 1.0]
            fracs = [(c + off * px) % 1.0 for c in centers for off in (0, 4, -4)]
        else:
            fracs = [s / 36 for s in range(1, 36)]   # no estimate at all → even sweep
        cap = _ROTATION_LOWCONF_ATTEMPTS
    # Drop near-duplicates (< ~2px apart) and cap the attempt budget.
    seen_fr, deduped = [], []
    for fr in fracs:
        if all(abs(fr - p) > 2.0 * px for p in seen_fr):
            seen_fr.append(fr)
            deduped.append(fr)
    fracs = deduped[:cap]
    print(f"[captcha] strategy={'fine' if (fraction is not None and conf >= _ROTATION_CONF_HIGH) else 'lowconf-bail'} attempts={len(fracs)}")

    for i, frac in enumerate(fracs):
        # Get FRESH slider position after each reset. Short timeout: a detached slider
        # must fail in ~3s, not block on Playwright's 30s default and crash the post.
        try:
            box = await slider_el.bounding_box(timeout=3000)
        except Exception:
            box = None
        if not box:
            # Slider vanished. Either the captcha closed (solved) or it reloaded after
            # several wrong drags (old handle detaches, a fresh one renders). Don't
            # crash the whole sweep — check which, and re-acquire if it's a reload.
            _, still = await _find_captcha(page)
            if not still:
                print(f"[captcha] SOLVED (slider gone) after attempt {i}")
                return True
            slider_el = await _find_rotation_slider(captcha_frame)
            if slider_el is None:
                print(f"[captcha] attempt {i}: slider lost, captcha still present — abort")
                break
            try:
                box = await slider_el.bounding_box(timeout=3000)
            except Exception:
                box = None
            if not box:
                print(f"[captcha] attempt {i}: re-acquired slider has no box — abort")
                break
        sx = box["x"] + box["width"] / 2
        sy = box["y"] + box["height"] / 2
        target_x = sx + track_width * frac
        print(f"[captcha] attempt {i}: frac={frac:.3f} drag {sx:.0f}→{target_x:.0f} (delta={target_x-sx:.0f}px)")

        await _human_drag(page, sx, sy, target_x, sy)
        await asyncio.sleep(0.8)

        _, still = await _find_captcha(page)
        if not still:
            print(f"[captcha] SOLVED on attempt {i} (frac={frac:.3f})")
            return True

        await asyncio.sleep(0.4)  # wait for TikTok auto-reset animation

    print(f"[captcha] _solve_rotation: {len(fracs)} attempts exhausted, failed")
    return False


async def _get_slider_track_width(captcha_frame, slider_box: dict) -> float:
    """Estimate the draggable track width in pixels."""
    # Try common track selectors
    for sel in [
        "[class*='track']",
        "[class*='cap-h-40']",
        "[class*='cap-rounded-full']",
        "[class*='slide-bar']",
        "[class*='drag-area']",
        "[class*='bar']:not(button)",
    ]:
        try:
            el = captcha_frame.locator(sel).first
            if await el.is_visible(timeout=400):
                box = await el.bounding_box()
                if box and box["width"] > 80:
                    # Draggable range = track width minus slider button width
                    return max(box["width"] - slider_box["width"], 50.0)
        except Exception:
            continue
    # Fallback: use the parent container width
    # TikTok modal is 380px wide with ~32px padding each side
    # Track ≈ 380 - 64 (padding) - 64 (button) = 252px
    return 252.0


async def _rotation_fraction_opencv(page: Page, captcha_frame) -> Optional[float]:
    """
    For TikTok rotation CAPTCHA: find best rotation angle by template-matching
    the inner rotating piece against the outer background ring.
    Returns fraction [0,1] of slider to drag, or None on failure.
    """
    # Collect captcha images (scoped to the widget — page avatars can be larger).
    all_imgs = await _scoped_captcha_images(captcha_frame)
    img_data = []
    for el in all_imgs:
        try:
            if not await el.is_visible(timeout=400):
                continue
            src = await el.get_attribute("src") or ""
            if not src:
                continue
            box = await el.bounding_box()
            if src.startswith("data:"):
                _, b64 = src.split(",", 1)
                raw = base64.b64decode(b64)
            else:
                resp = await page.request.get(src)
                raw = await resp.body() if resp.ok else None
            if raw and box:
                img_data.append((box["width"] * box["height"], raw, box))
        except Exception:
            continue

    if len(img_data) < 2:
        if len(img_data) == 1:
            raw = img_data[0][1]
            return _gradient_upright_fraction(raw), 0.0
        return None, 0.0

    # Sort by area: larger = background ring, smaller = rotating piece
    img_data.sort(key=lambda x: x[0], reverse=True)
    bg_bytes = img_data[0][1]
    piece_bytes = img_data[1][1]

    # Save images for debugging
    try:
        import os, time
        dbg_dir = "/tmp/captcha_debug"
        os.makedirs(dbg_dir, exist_ok=True)
        ts = int(time.time())
        with open(f"{dbg_dir}/{ts}_bg.webp", "wb") as f:
            f.write(bg_bytes)
        with open(f"{dbg_dir}/{ts}_piece.webp", "wb") as f:
            f.write(piece_bytes)
        print(f"[captcha] saved images to {dbg_dir}/{ts}_*.webp")
    except Exception:
        pass

    try:
        import cv2
        import numpy as np

        bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
        piece = cv2.imdecode(np.frombuffer(piece_bytes, np.uint8), cv2.IMREAD_COLOR)
        if bg is None or piece is None:
            return None, 0.0
        deg, conf = estimate_rotation_degrees(bg, piece)
        if deg is None:
            return None, 0.0
        print(f"[captcha] opencv estimate: {deg:.1f}° → frac={deg / 360.0:.3f} conf={conf:.1f}")
        return (deg % 360) / 360.0, conf

    except ImportError:
        return None, 0.0
    except Exception as e:
        print(f"[captcha] opencv error: {e}")
        return None, 0.0


def _angular_profiles(img, cx, cy, radii):
    """Sample image along concentric circles → (intensity[360], edge[360]) profiles,
    each averaged over the radii band. Edge profile = Sobel gradient magnitude
    (alignment of edges is a strong, brightness-invariant cue)."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    edge = cv2.magnitude(gx, gy)
    h, w = gray.shape
    n = 360
    inten = np.zeros(n, np.float32)
    edg = np.zeros(n, np.float32)
    for k in range(n):
        rad = 2 * np.pi * k / n
        xs = np.clip((cx + radii * np.cos(rad)).astype(int), 0, w - 1)
        ys = np.clip((cy + radii * np.sin(rad)).astype(int), 0, h - 1)
        inten[k] = gray[ys, xs].mean()
        edg[k] = edge[ys, xs].mean()
    return inten, edg


def _norm(a):
    import numpy as np
    a = a - a.mean()
    s = a.std()
    return a / s if s > 1e-6 else a


def _circular_xcorr(a, b):
    """Circular cross-correlation via FFT. Returns cc[θ] = how well b rotated by θ
    matches a. O(n log n) — far better than the old O(n²) MAD sweep."""
    import numpy as np
    A = np.fft.rfft(a)
    B = np.fft.rfft(b)
    return np.fft.irfft(A * np.conj(B), n=len(a))


def estimate_rotation_degrees(bg, piece):
    """Returns (degrees 0..360, confidence) — the rotation the inner piece must turn
    to align with the background ring, plus a peak-sharpness confidence score.
    Multi-feature (intensity + edge) circular cross-correlation, band-averaged, with
    parabolic sub-degree refinement. Returns (None, 0.0) if the piece is too small.

    Pure function over two BGR arrays so it can be unit-tested on synthetic data.
    """
    import numpy as np
    bh, bw = bg.shape[:2]
    ph, pw = piece.shape[:2]
    bg_cx, bg_cy = bw / 2.0, bh / 2.0
    pc_cx, pc_cy = pw / 2.0, ph / 2.0
    piece_r = min(pw, ph) // 2 - 2
    if piece_r < 8:
        return None, 0.0

    # bg sampled just OUTSIDE the white hole; piece just INSIDE its edge.
    radii_bg = np.arange(piece_r + 2, piece_r + 16)
    radii_pc = np.arange(max(3, piece_r - 16), piece_r - 2)

    bg_int, bg_edge = _angular_profiles(bg, bg_cx, bg_cy, radii_bg)
    pc_int, pc_edge = _angular_profiles(piece, pc_cx, pc_cy, radii_pc)

    # Sum normalized cross-correlations of both features → sharp, robust peak.
    cc = _circular_xcorr(_norm(bg_int), _norm(pc_int)) + \
        _circular_xcorr(_norm(bg_edge), _norm(pc_edge))

    peak = int(np.argmax(cc))
    # Peak-to-mean ratio = how sharp/confident the alignment is. Offline-validated
    # against live solves: high sharpness → the estimate is right (solves in 1-2 tries);
    # low → ambiguous, low-texture boundary → estimate unreliable. The caller uses it to
    # decide whether to trust the estimate or skip the captcha fast.
    sharpness = float(cc.max() / (np.abs(cc).mean() + 1e-9))
    # Parabolic interpolation around the peak for sub-degree precision.
    y0, y1, y2 = cc[(peak - 1) % 360], cc[peak], cc[(peak + 1) % 360]
    denom = (y0 - 2 * y1 + y2)
    delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-9 else 0.0
    return float((peak + delta) % 360.0), sharpness  # plain float — never leak np.float32


def _gradient_upright_fraction(img_bytes: bytes) -> Optional[float]:
    """Estimate correct upright angle for a single image using gradient analysis."""
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        h, w = img.shape
        cx, cy = w // 2, h // 2
        best_angle, best_score = 0, -1.0
        for deg in range(0, 360, 5):
            M = cv2.getRotationMatrix2D((cx, cy), deg, 1.0)
            rot = cv2.warpAffine(img, M, (w, h))
            hy = cv2.Sobel(rot, cv2.CV_64F, 0, 1)
            score = float(np.mean(np.abs(hy)))
            if score > best_score:
                best_score, best_angle = score, deg
        return best_angle / 360.0
    except Exception:
        return None


# ── OpenCV solver ──────────────────────────────────────────────────────────

async def _opencv_offset(page: Page, captcha_frame) -> Optional[int]:
    """
    Download CAPTCHA images and locate the hole using multi-signal detection.
    Returns pixel offset to drag, or None if images couldn't be obtained.

    Algorithm: scan every candidate window in the background and score it by
      score = (variance + 2*gray_deviation + 3*mean_saturation) / boundary_edges
    The hole scores lowest because it is:
      • uniform  → low variance
      • gray     → low gray deviation from 128, low HSV saturation
      • distinct → strong Sobel edges at its left/right boundary
    """
    bg_bytes, piece_bytes = await _get_image_bytes(page, captcha_frame)
    if not bg_bytes or not piece_bytes:
        return None

    try:
        import cv2
        import numpy as np

        bg    = cv2.imdecode(np.frombuffer(bg_bytes,    np.uint8), cv2.IMREAD_COLOR)
        piece = cv2.imdecode(np.frombuffer(piece_bytes, np.uint8), cv2.IMREAD_COLOR)

        if bg is None or piece is None:
            return None

        ph, pw = piece.shape[:2]
        h,  w  = bg.shape[:2]

        if pw >= w or ph >= h:
            return None

        gray  = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
        hsv   = cv2.cvtColor(bg, cv2.COLOR_BGR2HSV)
        sat   = hsv[:, :, 1].astype(float)   # 0 = gray, high = colorful
        sobel = np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3))
        gf    = gray.astype(float)

        best_x, best_score = 0, float('inf')
        for x in range(2, w - pw - 2):
            region_g   = gf[:, x:x+pw]
            region_sat = sat[:, x:x+pw]
            var        = float(np.var(region_g))
            mean_dev   = float(abs(np.mean(region_g) - 128))
            mean_sat   = float(np.mean(region_sat))
            boundary   = max(
                float(np.sum(sobel[:, x-2:x+2])) +
                float(np.sum(sobel[:, x+pw-2:x+pw+2])),
                1.0,
            )
            score = (var + mean_dev * 2 + mean_sat * 3) / boundary
            if score < best_score:
                best_score = score
                best_x = x

        return best_x

    except ImportError:
        return None
    except Exception:
        return None


async def _get_image_bytes(
    page: Page, captcha_frame
) -> tuple[Optional[bytes], Optional[bytes]]:
    """
    Try two strategies to obtain background and piece image bytes:
    1. Read src attribute from img elements and fetch via browser context.
    2. Intercept in-flight network responses (already captured by listener).
    """
    bg_bytes = await _fetch_img_src(page, captcha_frame, _BG_SELECTORS)
    piece_bytes = await _fetch_img_src(page, captcha_frame, _PIECE_SELECTORS)
    return bg_bytes, piece_bytes


async def _fetch_img_src(page: Page, frame, selectors: list[str]) -> Optional[bytes]:
    """Find an <img> by selector and download it using the browser's cookies."""
    for sel in selectors:
        try:
            el = frame.locator(sel).first
            if not await el.is_visible(timeout=800):
                continue

            src = await el.get_attribute("src") or ""
            if not src or src.startswith("data:"):
                # data URL — decode directly
                if src.startswith("data:"):
                    _, b64 = src.split(",", 1)
                    return base64.b64decode(b64)
                continue

            # Fetch via browser (sends session cookies automatically)
            resp = await page.request.get(src)
            if resp.ok:
                return await resp.body()
        except Exception:
            continue
    return None


# ── Network image interceptor (attach before login) ────────────────────────

class CaptchaImageInterceptor:
    """
    Attach to a page before login to catch CAPTCHA images as they arrive.
    Usage:
        interceptor = CaptchaImageInterceptor(page)
        await interceptor.attach()
        # ... trigger login / navigation ...
        bg, piece = await interceptor.get_images()
    """

    def __init__(self, page: Page):
        self._page = page
        self._images: list[bytes] = []

    async def attach(self) -> None:
        self._page.on("response", self._on_response)

    async def detach(self) -> None:
        self._page.remove_listener("response", self._on_response)

    async def _on_response(self, response) -> None:
        try:
            url = response.url
            if not any(kw in url for kw in _CAPTCHA_URL_PATTERNS):
                return
            ct = response.headers.get("content-type", "")
            if "image" not in ct:
                return
            body = await response.body()
            if len(body) > 500:  # skip tiny icons
                self._images.append(body)
        except Exception:
            pass

    async def get_images(self) -> tuple[Optional[bytes], Optional[bytes]]:
        """Return (background, piece) sorted by size (bg is larger)."""
        if len(self._images) < 2:
            return None, None
        sorted_imgs = sorted(self._images, key=len, reverse=True)
        return sorted_imgs[0], sorted_imgs[1]


# ── Drag application ───────────────────────────────────────────────────────

async def _apply_offset(page: Page, captcha_frame, offset_px: int) -> bool:
    """Find slider button and perform human-like drag by offset_px pixels."""
    for sel in _SLIDER_SELECTORS:
        try:
            slider = captcha_frame.locator(sel).first
            if not await slider.is_visible(timeout=1500):
                continue
            box = await slider.bounding_box()
            if not box:
                continue
            sx = box["x"] + box["width"] / 2
            sy = box["y"] + box["height"] / 2
            await _human_drag(page, sx, sy, sx + offset_px, sy)
            await asyncio.sleep(2)
            return True
        except Exception:
            continue
    return False


async def _human_drag(
    page: Page, x1: float, y1: float, x2: float, y2: float
) -> None:
    """Ease-out cubic drag with per-step micro-jitter (looks human)."""
    # Coerce to plain Python floats. Rotation fractions flow from numpy
    # (estimate_rotation_degrees), so x/y can arrive as np.float32 — which Playwright
    # cannot JSON-serialize ("Object of type float32 is not JSON serializable"),
    # crashing every rotation-captcha drag and silently failing the whole post.
    x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
    steps = 45 + random.randint(0, 20)
    await page.mouse.move(x1, y1)
    await asyncio.sleep(0.2 + random.uniform(0, 0.12))
    await page.mouse.down()
    await asyncio.sleep(0.06 + random.uniform(0, 0.04))

    for i in range(1, steps + 1):
        t = i / steps
        ease = 1 - (1 - t) ** 3          # ease-out cubic
        jx = random.uniform(-1.5, 1.5) * (1 - t)
        jy = random.uniform(-0.8, 0.8)
        await page.mouse.move(x1 + (x2 - x1) * ease + jx, y1 + jy)
        await asyncio.sleep(random.uniform(0.006, 0.020))

    await asyncio.sleep(0.10 + random.uniform(0, 0.08))
    await page.mouse.up()


# ── Helpers ────────────────────────────────────────────────────────────────

async def _find_captcha(page: Page):
    """Return (frame, element) of the CAPTCHA container, or (None, None)."""
    contexts = [page] + [f for f in page.frames if f != page.main_frame]
    for ctx in contexts:
        for sel in _CAPTCHA_CONTAINERS:
            try:
                el = ctx.locator(sel).first
                if await el.is_visible(timeout=600):
                    return ctx, el
            except Exception:
                continue
    return None, None


async def _screenshot_b64(el, page: Page) -> str:
    try:
        data = await el.screenshot()
    except Exception:
        data = await page.screenshot()
    return base64.b64encode(data).decode()


# ── Paid service solvers ───────────────────────────────────────────────────

async def _twocaptcha_solve(screenshot_b64: str, api_key: str) -> Optional[int]:
    """Coordinates task: workers click the hole center. Returns x-offset."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://2captcha.com/in.php",
            data={
                "key": api_key,
                "method": "base64",
                "body": screenshot_b64,
                "coordinatescaptcha": 1,
                "textinstructions": (
                    "TikTok sliding puzzle. Click the CENTER of the hole "
                    "where the puzzle piece should be placed."
                ),
                "json": 1,
            },
        ) as r:
            data = await r.json(content_type=None)

        if data.get("status") != 1:
            raise RuntimeError(f"2captcha: {data.get('request')}")
        task_id = data["request"]

        for _ in range(24):
            await asyncio.sleep(5)
            async with session.get(
                "https://2captcha.com/res.php",
                params={"key": api_key, "action": "get", "id": task_id, "json": 1},
            ) as r:
                res = await r.json(content_type=None)

            if res.get("status") == 1:
                raw: str = res.get("request", "")
                parts = dict(
                    p.split("=") for p in raw.replace(";", ",").split(",") if "=" in p
                )
                return int(float(parts.get("x", 0)))
            if res.get("request") not in ("CAPCHA_NOT_READY", ""):
                raise RuntimeError(f"2captcha: {res.get('request')}")
    return None


async def _capsolver_solve(screenshot_b64: str, api_key: str) -> Optional[int]:
    """CapSolver ImageToTextTask. Returns x-offset."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.capsolver.com/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "ImageToTextTask",
                    "body": screenshot_b64,
                    "module": "common",
                },
            },
        ) as r:
            data = await r.json(content_type=None)

        if data.get("errorId") != 0:
            raise RuntimeError(f"CapSolver: {data.get('errorDescription')}")
        task_id = data["taskId"]

        for _ in range(24):
            await asyncio.sleep(5)
            async with session.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            ) as r:
                res = await r.json(content_type=None)

            if res.get("status") == "ready":
                text: str = res.get("solution", {}).get("text", "")
                if "=" in text:
                    parts = dict(
                        p.split("=") for p in text.replace(";", ",").split(",") if "=" in p
                    )
                    return int(float(parts.get("x", 0)))
                try:
                    return int(float(text.strip()))
                except ValueError:
                    return None
            if res.get("errorId", 0) != 0:
                raise RuntimeError(f"CapSolver: {res.get('errorDescription')}")
    return None
