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

# Slider button / drag handle
_SLIDER_SELECTORS = [
    "[class*='secsdk-captcha-drag-icon']",
    "[class*='captcha_verify_slide_bar']",
    "[class*='drag-icon']:not(img)",
    "[class*='slider-btn']",
    "[class*='drag-btn']",
    "[class*='slide-btn']",
]

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

        if captcha_type == "rotation":
            solved = await _solve_rotation(page, captcha_frame)
        else:
            # Sliding puzzle: try OpenCV first
            offset = await _opencv_offset(page, captcha_frame)
            if offset is not None:
                solved = await _apply_offset(page, captcha_frame, offset)
            elif api_key:
                screenshot_b64 = await _screenshot_b64(captcha_el, page)
                if service == "capsolver":
                    offset = await _capsolver_solve(screenshot_b64, api_key)
                else:
                    offset = await _twocaptcha_solve(screenshot_b64, api_key)
                solved = await _apply_offset(page, captcha_frame, offset) if offset else False
            else:
                solved = False

        await asyncio.sleep(1.5)
        # Verify captcha is gone
        _, still_present = await _find_captcha(page)
        if not still_present:
            return True

    return False


async def _detect_captcha_type(page: Page, captcha_frame) -> str:
    """Returns 'rotation' or 'slider'.
    Rotation: two concentric images (same center, different sizes).
    Slider:   two images side by side.
    """
    try:
        imgs = await captcha_frame.locator("img").all()
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


async def _solve_rotation(page: Page, captcha_frame) -> bool:
    """
    Solve TikTok rotation CAPTCHA.
    Gets fresh slider position before each drag (TikTok resets it after wrong answer).
    """
    # Find slider element (keep reference, not just box)
    slider_el = None
    for sel in _SLIDER_SELECTORS + [
        "[class*='captcha'] button[class*='default']",
        "[class*='verify'] button[class*='default']",
    ]:
        try:
            el = captcha_frame.locator(sel).first
            if await el.is_visible(timeout=600):
                box = await el.bounding_box()
                if box and box["width"] < 100:
                    slider_el = el
                    break
        except Exception:
            continue

    if not slider_el:
        print("[captcha] _solve_rotation: slider element NOT found")
        return False

    initial_box = await slider_el.bounding_box()
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
    fraction = await _rotation_fraction_opencv(page, captcha_frame)
    print(f"[captcha] opencv fraction={fraction}")
    steps = list(range(1, 36))  # 2.78% intervals (~8px steps on a 284px track)
    if fraction is not None:
        center = max(1, min(35, round(fraction * 36)))
        steps.sort(key=lambda s: abs(s - center))
    fracs = ([fraction] if fraction is not None else []) + [s / 36 for s in steps]

    for i, frac in enumerate(fracs):
        # Get FRESH slider position after each reset
        box = await slider_el.bounding_box()
        if not box:
            print(f"[captcha] attempt {i}: bounding_box() is None (detached?)")
            break
        sx = box["x"] + box["width"] / 2
        sy = box["y"] + box["height"] / 2
        target_x = sx + track_width * frac
        print(f"[captcha] attempt {i}: frac={frac:.3f} drag {sx:.0f}→{target_x:.0f} (delta={target_x-sx:.0f}px)")

        await _human_drag(page, sx, sy, target_x, sy)
        await asyncio.sleep(1.4)

        _, still = await _find_captcha(page)
        if not still:
            print(f"[captcha] SOLVED on attempt {i} (frac={frac:.3f})")
            return True

        await asyncio.sleep(0.9)  # wait for TikTok auto-reset animation

    print("[captcha] _solve_rotation: all fractions exhausted, failed")
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
    # Collect all visible images with their sizes
    all_imgs = await captcha_frame.locator("img").all()
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
            return _gradient_upright_fraction(raw)
        return None

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
            return None

        ph, pw = piece.shape[:2]
        bh, bw = bg.shape[:2]

        # The background ring has a white hole in the center; the piece is the
        # content that belongs in that hole, rotated. Match them by their angular
        # colour profile near the piece/hole boundary.
        #
        # KEY FIX: average over a BAND of radii instead of a single 1px ring. A
        # one-pixel ring is noisy and routinely picked the wrong angle, which is
        # what forced the slow 36-step brute force. Averaging ~12 radii produces a
        # far sharper, more reliable minimum. Also step every 1° (not 2°).
        bg_cx, bg_cy = bw // 2, bh // 2
        piece_cx, piece_cy = pw // 2, ph // 2
        piece_r = min(pw, ph) // 2 - 2

        n_angles = 360

        def _angular_profile(img, cx, cy, radii):
            h, w = img.shape[:2]
            prof = np.zeros((n_angles, 3), np.float32)
            for k in range(n_angles):
                rad = 2 * np.pi * k / n_angles
                xs = np.clip((cx + radii * np.cos(rad)).astype(int), 0, w - 1)
                ys = np.clip((cy + radii * np.sin(rad)).astype(int), 0, h - 1)
                prof[k] = img[ys, xs].mean(axis=0)
            return prof

        # The background has a white hole of radius ≈ piece_r in its centre, so sample
        # the bg band just OUTSIDE the hole (real ring content), and the piece band
        # just INSIDE its edge. Sampling both at the same radii would read the bg's
        # white hole and wash the signal out entirely.
        radii_bg = np.arange(piece_r + 2, piece_r + 14)
        radii_pc = np.arange(max(3, piece_r - 14), piece_r - 2)
        bg_ring = _angular_profile(bg, bg_cx, bg_cy, radii_bg)
        piece_ring = _angular_profile(piece, piece_cx, piece_cy, radii_pc)

        # Remove per-channel brightness offset so the match is about pattern, not
        # overall exposure differences between the two rendered images.
        bg_ring -= bg_ring.mean(axis=0)
        piece_ring -= piece_ring.mean(axis=0)

        best_angle = 0
        best_score = float("inf")
        scores = []
        for deg in range(0, 360):
            rotated_ring = np.roll(piece_ring, deg, axis=0)
            diff = float(np.mean(np.abs(bg_ring - rotated_ring)))
            scores.append((diff, deg))
            if diff < best_score:
                best_score = diff
                best_angle = deg

        scores.sort()
        top5 = [(f"{d}°", f"{s:.1f}") for s, d in scores[:5]]
        print(f"[captcha] opencv boundary top5: {top5} (best={best_angle}°)")

        return best_angle / 360.0

    except ImportError:
        return None
    except Exception as e:
        print(f"[captcha] opencv error: {e}")
        return None


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
