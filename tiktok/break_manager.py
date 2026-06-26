"""Human-schedule break manager — async port of multicombine's break_manager.

Gives the autopilot a believable rhythm: coffee breaks every 3-5 h (15-60 min) and a
deep sleep after 24 h of continuous work (6-8 h). Call `check_and_apply()` at the start
of each cycle; it sleeps in-place when a break is due and returns True if it paused.
"""
import asyncio
import random
import time
from typing import Callable, Optional

_BREAK_EVERY_MIN = (3 * 60, 5 * 60)        # minutes between coffee breaks
_BREAK_DURATION_MIN = (15, 60)             # coffee break length (minutes)
_DEEP_SLEEP_THRESHOLD_MIN = 24 * 60        # continuous work before deep sleep
_DEEP_SLEEP_HOURS = (6, 8)


async def interruptible_sleep(stop_event, seconds: float) -> None:
    """Sleep `seconds`, waking early if stop_event is set."""
    end = time.time() + seconds
    while time.time() < end:
        if stop_event and stop_event.is_set():
            return
        await asyncio.sleep(min(15, max(0.1, end - time.time())))


class BreakManager:
    def __init__(self, notify: Optional[Callable[[str], None]], stop_event):
        self._notify = notify
        self.stop_event = stop_event
        self._session_start = time.time()
        self._next_break = time.time() + random.uniform(*_BREAK_EVERY_MIN) * 60

    def _say(self, msg: str):
        if self._notify:
            try:
                self._notify(msg)
            except Exception:
                pass

    async def check_and_apply(self) -> bool:
        if self.stop_event and self.stop_event.is_set():
            return False
        now = time.time()
        session_min = (now - self._session_start) / 60

        if session_min >= _DEEP_SLEEP_THRESHOLD_MIN:
            hours = random.randint(*_DEEP_SLEEP_HOURS)
            self._say(f"💤 24 год роботи — глибокий сон на {hours} год.")
            await interruptible_sleep(self.stop_event, hours * 3600)
            self._session_start = time.time()
            self._next_break = time.time() + random.uniform(*_BREAK_EVERY_MIN) * 60
            return True

        if now >= self._next_break:
            mins = random.randint(*_BREAK_DURATION_MIN)
            self._say(f"☕ Кава-брейк: відпочиваю {mins} хв...")
            await interruptible_sleep(self.stop_event, mins * 60)
            self._next_break = time.time() + random.uniform(*_BREAK_EVERY_MIN) * 60
            return True

        return False
