"""Human-like pointer behaviour for the highest-scrutiny clicks.

Playwright's `locator.click()` warps the cursor straight onto the target and fires the
click with no preceding `mousemove` stream. Real users always emit a burst of `mousemove`
events along a curved path (with acceleration and a small overshoot) before a click — its
ABSENCE is one of the strongest behavioural bot tells, and behaviour is what drives
TikTok's shadow-ban scoring far more than any static fingerprint.

`human_click` drives a real Bézier cursor path to the element, hovers, pauses a beat
("aiming"), then issues the native trusted click at the resolved coordinates. It degrades
gracefully: if anything about the geometry fails it falls back to a plain `.click()`, so it
can safely replace a hot `.click()` without ever blocking an upload.
"""
from __future__ import annotations

import asyncio
import math
import random


def _bezier(p0, p1, p2, p3, t):
    """Cubic Bézier point at t∈[0,1]."""
    u = 1 - t
    x = (u * u * u * p0[0] + 3 * u * u * t * p1[0]
         + 3 * u * t * t * p2[0] + t * t * t * p3[0])
    y = (u * u * u * p0[1] + 3 * u * u * t * p1[1]
         + 3 * u * t * t * p2[1] + t * t * t * p3[1])
    return x, y


# Module-level "last known cursor position" so consecutive moves start where the previous
# one ended (a cursor that always starts from the same spot is itself a pattern). Seeded to a
# plausible mid-screen idle position.
_last_xy = [640.0, 400.0]


# A per-session BEHAVIOURAL PERSONA. Randomising each action is not enough: if every run draws
# from the SAME distribution (same mean typing speed, same pause rate), that distribution is
# itself a stable signature across accounts. A persona shifts the whole distribution per run —
# one account is a fast confident typist with few pauses, another is slow and hesitant — so the
# behaviour CLUSTERS differently per account the way real people do. Defaults to neutral (1.0)
# so callers that never set a persona behave exactly as before.
_persona = {
    "speed": 1.0,        # <1 faster, >1 slower — scales typing delay + mouse dwell
    "pause_rate": 0.06,  # P(a "thinking" pause after a keystroke)
    "typo_rate": 0.03,   # P(a fat-finger + backspace correction per alpha char)
    "overshoot": 0.25,   # P(cursor overshoots the target and corrects)
    "jitter": 1.0,       # multiplier on per-step mouse jitter amplitude
}


def new_persona(seed: int | None = None) -> dict:
    """Roll a fresh behavioural persona for the next session and reset the cursor origin.

    Call once per account/registration right after the fingerprint is built. Each field is
    drawn from a plausible human spread; `seed` makes it deterministic per account (same
    account → same "personality" across its sessions, which is itself realistic)."""
    rng = random.Random(seed) if seed is not None else random
    _persona.update(
        speed=rng.uniform(0.7, 1.5),
        pause_rate=rng.uniform(0.03, 0.12),
        typo_rate=rng.uniform(0.01, 0.06),
        overshoot=rng.uniform(0.12, 0.38),
        jitter=rng.uniform(0.7, 1.4),
    )
    # Start the cursor somewhere plausible and DIFFERENT each run, not always (640, 400).
    _last_xy[0] = rng.uniform(300, 1000)
    _last_xy[1] = rng.uniform(200, 600)
    return dict(_persona)


async def human_move(page, x: float, y: float, *, steps: int | None = None) -> None:
    """Move the cursor to (x, y) along a curved, eased path with per-step jitter."""
    sx, sy = _last_xy
    dist = math.hypot(x - sx, y - sy)
    if steps is None:
        # More steps for longer travel; humans don't move in 2 jumps across the screen.
        steps = max(12, min(40, int(dist / 12) + random.randint(6, 14)))

    # Two control points pulled off the straight line → a natural arc, not a ruler line.
    off = max(20.0, dist * random.uniform(0.12, 0.3))
    nx, ny = -(y - sy), (x - sx)            # normal to the travel direction
    nlen = math.hypot(nx, ny) or 1.0
    nx, ny = nx / nlen, ny / nlen
    c1 = (sx + (x - sx) * 0.3 + nx * off * random.uniform(-1, 1),
          sy + (y - sy) * 0.3 + ny * off * random.uniform(-1, 1))
    c2 = (sx + (x - sx) * 0.7 + nx * off * random.uniform(-1, 1),
          sy + (y - sy) * 0.7 + ny * off * random.uniform(-1, 1))

    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in-out so the cursor accelerates then decelerates near the target.
        te = 3 * t * t - 2 * t * t * t
        px, py = _bezier((sx, sy), c1, c2, (x, y), te)
        _j = _persona["jitter"]
        px += random.uniform(-1.2, 1.2) * _j
        py += random.uniform(-1.2, 1.2) * _j
        try:
            await page.mouse.move(px, py)
        except Exception:
            break
        await asyncio.sleep(random.uniform(0.006, 0.02) * _persona["speed"])

    _last_xy[0], _last_xy[1] = x, y


async def human_scroll(page, total: float | None = None, *, up: bool = False) -> None:
    """Scroll roughly `total` px the way a real wheel does: several ticks whose deltas
    decay with momentum (a flick, not a constant feed), with small irregular pauses and
    an occasional tiny reverse correction. A stream of identical `wheel(0, 250)` calls is
    a mechanical signature; this breaks it. Best-effort — swallows all errors."""
    if total is None:
        total = random.uniform(300, 700)
    sign = -1 if up else 1
    remaining = total
    # 3–6 ticks, front-loaded (momentum): first ticks big, then tapering.
    ticks = random.randint(3, 6)
    for k in range(ticks):
        frac = (ticks - k) / sum(range(1, ticks + 1))   # decaying weight
        delta = max(18.0, remaining * frac * random.uniform(0.8, 1.2))
        remaining = max(0.0, remaining - delta)
        try:
            await page.mouse.wheel(0, sign * delta)
        except Exception:
            return
        await asyncio.sleep(random.uniform(0.05, 0.22))
    # ~15% chance of a small over-scroll correction back the other way.
    if random.random() < 0.15:
        try:
            await page.mouse.wheel(0, -sign * random.uniform(20, 70))
        except Exception:
            pass


async def human_type(page, text: str) -> None:
    """Type `text` with variable cadence, occasional thinking pauses, and rare typo +
    backspace corrections — the keystroke pattern of a person, not a fixed-delay loop."""
    spd = _persona["speed"]
    for ch in text:
        # Fat-finger rate is persona-driven: a neighbour char, pause, backspace, then the real
        # one. Real typing carries a low but non-zero, person-specific correction rate.
        if ch.isalpha() and random.random() < _persona["typo_rate"]:
            wrong = random.choice("йцукенгшщзфывапролдячсмить")
            try:
                await page.keyboard.type(wrong, delay=random.uniform(45, 160) * spd)
                await asyncio.sleep(random.uniform(0.15, 0.5) * spd)
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.1, 0.3) * spd)
            except Exception:
                pass
        try:
            await page.keyboard.type(ch, delay=random.uniform(45, 160) * spd)
        except Exception:
            return
        if random.random() < _persona["pause_rate"]:
            await asyncio.sleep(random.uniform(0.3, 1.1) * spd)


async def human_click(page, locator, *, timeout: float = 5_000) -> bool:
    """Curved cursor move → hover → aim pause → trusted click on `locator`.

    Returns True on success. Falls back to a plain `.click()` if the geometry can't be
    resolved (e.g. element offscreen), so it never silently no-ops an important action."""
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.scroll_into_view_if_needed(timeout=timeout)
        box = await locator.bounding_box()
        if not box:
            await locator.click(timeout=timeout)
            return True

        # Aim for a random point in the element's inner area (humans don't hit dead-centre).
        tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)

        await human_move(page, tx, ty)

        # Persona-driven chance of an overshoot + correction, the classic human "miss".
        if random.random() < _persona["overshoot"]:
            await human_move(page, tx + random.uniform(-12, 12),
                             ty + random.uniform(-10, 10), steps=random.randint(4, 8))
            await human_move(page, tx, ty, steps=random.randint(4, 8))

        # "aiming" beat before pressing, scaled by how deliberate this persona is.
        await asyncio.sleep(random.uniform(0.08, 0.4) * _persona["speed"])
        try:
            await page.mouse.click(tx, ty)
        except Exception:
            await locator.click(timeout=timeout)
        return True
    except Exception:
        try:
            await locator.click(timeout=timeout)
            return True
        except Exception:
            return False
