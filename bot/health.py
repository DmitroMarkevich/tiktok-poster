"""Background session-health monitor.

Periodically verifies that each account's cookies are still logged in and DMs the
superadmin when an account needs attention (dead cookies / invalid session). Accounts
whose browser profile is currently in use (commenting/warmup) are skipped via the
per-account lock — never launch a second Chrome on a live profile.
"""
import asyncio

from config import SUPERADMIN_ID
from database import async_session_factory
from database.repository import AccountRepo
from tiktok.browser import create_context
from tiktok.auth import verify_logged_in_robust
from tiktok.locks import get_account_lock


async def _check_account(acc) -> str:
    """Returns "" if healthy, else a short problem description."""
    if not acc.session_data:
        return "немає cookies"
    lock = get_account_lock(acc.id)
    if not lock.acquire(blocking=False):
        return ""  # busy with another job — skip, not a problem
    pw = ctx = None
    try:
        pw, ctx = await create_context(acc.id, acc.proxy, acc.session_data)
        # Retry before declaring a session dead — a single check flakes (see
        # verify_logged_in_robust). Only report a problem if EVERY attempt fails.
        if await verify_logged_in_robust(ctx):
            return ""
        return "сесія недійсна"
    except Exception:
        return ""  # transient launch/network error — don't false-alarm
    finally:
        if ctx:
            try: await ctx.close()
            except Exception: pass
        if pw:
            try: await pw.stop()
            except Exception: pass
        lock.release()


async def _run_health_check(bot):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    problems = []
    for acc in accounts:
        issue = await _check_account(acc)
        if issue:
            problems.append(f"• @{acc.username}: {issue}")
    if problems:
        try:
            await bot.send_message(
                SUPERADMIN_ID,
                "🩺 <b>Перевірка акаунтів</b>\nПотрібна увага:\n" + "\n".join(problems) +
                "\n\nОнови cookies через 🍪 у меню акаунта.",
            )
        except Exception:
            pass


async def session_health_loop(bot, interval_hours: float = 6, first_delay_min: float = 10):
    """Run a health check every `interval_hours`, starting `first_delay_min` after boot."""
    await asyncio.sleep(first_delay_min * 60)
    while True:
        try:
            await _run_health_check(bot)
        except Exception:
            pass
        await asyncio.sleep(interval_hours * 3600)
