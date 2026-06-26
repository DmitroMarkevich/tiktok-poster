"""Per-account browser locks — prevent two operations from opening the same
Chrome profile at once.

Each TikTok account uses a persistent profile dir (browsers/<id>). Launching a second
`launch_persistent_context` on a profile that's already open fails (SingletonLock) and
can corrupt the session — exactly what happens when e.g. a session-check runs while a
comment job is using the same account.

These are `threading.Lock`s (NOT asyncio): browser work runs in worker-thread event
loops (run_in_executor) while other callers run on the main loop, so the primitive must
coordinate across threads/loops. Long jobs acquire blocking (wait their turn); lightweight
checks use `is_busy()` and skip a busy account instead of clashing.
"""
import asyncio
import threading
from contextlib import contextmanager, asynccontextmanager

_registry_lock = threading.Lock()
_locks: dict = {}


def get_account_lock(account_id: int) -> threading.Lock:
    with _registry_lock:
        lock = _locks.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _locks[account_id] = lock
        return lock


def is_busy(account_id: int) -> bool:
    """True if the account's profile is currently in use by another operation."""
    lock = get_account_lock(account_id)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


@contextmanager
def account_session(account_id: int):
    """Hold the account's browser lock for the duration of a browser job (blocking —
    waits its turn if the account is busy)."""
    lock = get_account_lock(account_id)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


@asynccontextmanager
async def async_account_session(account_id: int):
    """Same per-account browser lock, for callers running on the MAIN event loop (uploads
    run their Playwright work in the loop, not a worker thread). Acquires the threading lock
    via a thread so it never blocks the loop, then coordinates with the warmup/comment/
    health paths that use the SAME lock — without this, an upload shares no lock with them
    and the health monitor could open a second Chrome on the profile mid-upload."""
    lock = get_account_lock(account_id)
    await asyncio.to_thread(lock.acquire)
    try:
        yield
    finally:
        lock.release()
