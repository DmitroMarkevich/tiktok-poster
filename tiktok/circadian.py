"""Per-account circadian gating + daily action caps.

The single biggest temporal bot tell for a fleet is *synchronised* activity: 50 "different
people" in different countries all acting (and sleeping) on the SAME server clock. Real
users act during their OWN local waking hours and go quiet at night.

This module gates each account by the LOCAL time of its geo (the fingerprint's timezone,
which is already proxy-matched), with a per-account phase offset so the fleet doesn't all
wake/sleep at the same instant — plus a rolling daily cap that ramps with account maturity
and drops when an account's shadow-ban survival rate falls.

Everything degrades safe: if a timezone can't be resolved we treat the account as awake,
so gating can never silently freeze the whole autopilot.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from utils.fingerprint import get_fingerprint


def _local_hour(account_id: int, country: str | None) -> float | None:
    """Account's current local hour as a float (e.g. 13.5), or None if tz unknown."""
    tz = get_fingerprint(account_id, country).timezone
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        return None
    return now.hour + now.minute / 60.0


def _phase_offset(account_id: int) -> float:
    """Deterministic per-account shift (±1.5 h) of the wake/sleep boundaries, so accounts
    in the same timezone don't all flip awake/asleep at the exact same minute."""
    return (random.Random(account_id * 0x9E3779B1).random() - 0.5) * 3.0


def activity_probability(account_id: int, country: str | None = None) -> float:
    """Probability this account should ACT right now, by its local time.

    Curve (before the per-account offset): deep night 00–06 → 0; waking 06–08 ramps 0→1;
    day 08–22 → 1; winding-down 22–24 ramps 1→0.2. A genuine person tapers activity at the
    edges of the day rather than switching on/off, so the ramps matter as much as the floor."""
    h = _local_hour(account_id, country)
    if h is None:
        return 1.0
    h = (h - _phase_offset(account_id)) % 24.0
    if h < 6:
        return 0.0
    if h < 8:
        return (h - 6) / 2.0           # 0 → 1 across 06:00–08:00
    if h < 22:
        return 1.0
    return max(0.2, 1.0 - (h - 22) / 2.0 * 0.8)   # 1 → 0.2 across 22:00–24:00


def should_act(account_id: int, country: str | None = None,
               rng: random.Random | None = None) -> bool:
    """Roll the local-time activity probability. False → let the account rest this cycle."""
    p = activity_probability(account_id, country)
    if p >= 1.0:
        return True
    if p <= 0.0:
        return False
    return (rng or random).random() < p


def daily_cap(account_id: int) -> int:
    """Max comments per rolling 24 h for this account, ramped by maturity and trust.

    A brand-new account doing an aged account's volume on day one is a strong ban trigger,
    so caps start low and grow with total verified history; they also shrink when the
    account's shadow-ban survival rate is poor (it's already under suspicion — back off)."""
    from tiktok.dedup import account_trust, comments_in_last

    survived, banned, checked, pct = account_trust(account_id)
    total = comments_in_last(account_id, hours=24 * 3650)   # lifetime proxy for "age"

    if total < 10:
        base = 5
    elif total < 50:
        base = 12
    else:
        base = 25

    # Punish a poor survival rate (only once there's enough signal to trust the number).
    if checked >= 5:
        if pct < 50:
            base = max(2, base // 3)
        elif pct < 75:
            base = max(3, base // 2)

    # ±20% deterministic per-account jitter so the whole fleet doesn't share one round cap.
    jit = 0.8 + random.Random(account_id * 0x85EBCA77).random() * 0.4
    return max(2, int(round(base * jit)))


def daily_cap_reached(account_id: int) -> tuple[bool, int, int]:
    """(reached, done_last_24h, cap). True → account hit its rolling-24h comment cap."""
    from tiktok.dedup import comments_in_last
    cap = daily_cap(account_id)
    done = comments_in_last(account_id, hours=24)
    return done >= cap, done, cap
