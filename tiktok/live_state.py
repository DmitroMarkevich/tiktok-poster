"""Live per-account run state — so the Telegram UI can show WHERE each bot is without
asking the operator.

The commenting browser runs in a worker thread with its own event loop; the Telegram
handlers run in the main loop. A Playwright page is bound to its own loop, so the UI
cannot screenshot it on demand across that boundary. Instead the worker pushes its
current phase here and writes a fresh screenshot to disk at every phase transition;
the handler just reads this dict and sends the (at most one-phase-old) image file.

Plain dict guarded by a Lock — written from the worker thread, read from the main loop.
"""
import threading
import time

_lock = threading.Lock()
_state: dict = {}   # account_id -> {username, phase, video_url, screenshot, updated, active}

SCREENSHOT_DIR = "/tmp/bot_screens"


def update(account_id, username=None, phase=None, video_url=None,
           screenshot=None, active=True):
    """Upsert an account's live state. Only non-None fields overwrite."""
    with _lock:
        s = _state.setdefault(account_id, {})
        if username is not None:
            s["username"] = username
        if phase is not None:
            s["phase"] = phase
        if video_url is not None:
            s["video_url"] = video_url
        if screenshot is not None:
            s["screenshot"] = screenshot
        s["active"] = active
        s["updated"] = time.time()


def finish(account_id, phase="завершено"):
    """Mark an account idle (run ended). Keeps the last screenshot for review."""
    with _lock:
        s = _state.get(account_id)
        if s:
            s["phase"] = phase
            s["active"] = False
            s["updated"] = time.time()


def snapshot() -> dict:
    """Deep-ish copy of all account states for the UI to render safely."""
    with _lock:
        return {k: dict(v) for k, v in _state.items()}


def get(account_id):
    with _lock:
        v = _state.get(account_id)
        return dict(v) if v else None
