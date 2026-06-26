"""Autopilot — hands-off loop: comment cycle → warmup → human breaks → repeat.

Ported concept from multicombine's conveyor/night_autopilot, adapted to this project's
async runners. Each cycle: (optional break) → comment on the configured hashtag across
all active accounts → short warmup per account → wait, then repeat until stopped.
Reuses the tested executor work functions from the comment & warmup handlers.
"""
import asyncio
import random
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.states import Autopilot
from bot.keyboards import autopilot_menu_kb, autopilot_warmup_kb, cancel_kb, main_menu
from database import async_session_factory
from database.repository import AccountRepo, AutopilotRepo
from utils.policy_filter import risk_warning
from tiktok.break_manager import BreakManager, interruptible_sleep
from config import SUPERADMIN_ID

router = Router()

# owner_id → asyncio.Event used to stop that owner's autopilot loop
_stop_events: dict = {}


async def _safe_edit(callback: CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


# ── Menu / config flow ───────────────────────────────────────────────────────

@router.callback_query(F.data == "autopilot")
async def autopilot_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    running = callback.from_user.id in _stop_events
    status = "🟢 <b>працює</b>" if running else "⚪️ зупинено"
    await _safe_edit(
        callback,
        "🤖 <b>Автопілот</b>\n\n"
        "Безперервний цикл: коментування → прогрів → людські перерви (кава кожні "
        "3-5 год, сон після 24 год) → повтор. Працює на всіх активних акаунтах.\n\n"
        f"Статус: {status}",
        autopilot_menu_kb(running),
    )


@router.callback_query(F.data == "ap_stop")
async def autopilot_stop(callback: CallbackQuery):
    await callback.answer()
    ev = _stop_events.get(callback.from_user.id)
    if ev:
        ev.set()
        await _safe_edit(callback, "⏹ Зупиняю автопілот після поточної дії...", main_menu())
    else:
        await _safe_edit(callback, "Автопілот не запущено.", main_menu())


@router.callback_query(F.data == "ap_start")
async def autopilot_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id in _stop_events:
        await callback.message.answer("Автопілот уже працює.")
        return
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    if not accounts:
        await _safe_edit(callback, "Спочатку додай акаунт.", main_menu())
        return
    await state.set_state(Autopilot.hashtag)
    await _safe_edit(callback, "Введи хештег/ключове слово для коментування:", cancel_kb())


@router.message(Autopilot.hashtag, F.text)
async def ap_got_hashtag(message: Message, state: FSMContext):
    await state.update_data(hashtag=message.text.strip().lstrip("#"))
    await state.set_state(Autopilot.comment_text)
    await message.answer("Введи текст коментаря (можна spintax / кілька рядків):",
                         reply_markup=cancel_kb())


@router.message(Autopilot.comment_text, F.text)
async def ap_got_text(message: Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(comment_text=text)
    await state.set_state(Autopilot.count)
    warning = risk_warning(text)
    if warning:
        await message.answer(warning)
    await message.answer("Скільки коментарів за один цикл? (1–10):", reply_markup=cancel_kb())


@router.message(Autopilot.count, F.text)
async def ap_got_count(message: Message, state: FSMContext):
    try:
        count = max(1, min(10, int(message.text.strip())))
    except ValueError:
        await message.answer("Введи число від 1 до 10.")
        return
    await state.update_data(count=count)
    await state.set_state(None)
    await message.answer("🔥 Прогрів між циклами — скільки хвилин на акаунт?",
                         reply_markup=autopilot_warmup_kb())


@router.callback_query(F.data.startswith("ap_wm_"))
async def ap_launch(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    if user_id in _stop_events:
        await callback.message.answer("Автопілот уже працює.")
        return
    warmup_min = int(callback.data.split("_")[-1])
    data = await state.get_data()
    await state.clear()
    if not data.get("hashtag"):
        await _safe_edit(callback, "Сесія застаріла — почни спочатку.", main_menu())
        return

    cfg = {
        "hashtag": data["hashtag"],
        "comment_text": data["comment_text"],
        "count": data["count"],
        "warmup_min": warmup_min,
    }
    stop_event = asyncio.Event()
    _stop_events[user_id] = stop_event

    chat_id = callback.message.chat.id
    async with async_session_factory() as session:
        await AutopilotRepo(session).save(user_id, chat_id, cfg, active=True)

    await _safe_edit(
        callback,
        f"🤖 <b>Автопілот запущено</b>\n\n"
        f"#{cfg['hashtag']} · {cfg['count']} коментарів/цикл · "
        f"прогрів {warmup_min} хв · на всіх акаунтах.\n\n"
        "Зупинити — кнопкою «🤖 Автопілот» → «⏹ Зупинити».",
        autopilot_menu_kb(True),
    )
    bot = callback.bot
    chat_id = callback.message.chat.id
    from bot.bg import spawn
    spawn(_run_autopilot(bot, chat_id, user_id, cfg, stop_event))


# ── The loop ──────────────────────────────────────────────────────────────────

async def _run_autopilot(bot, chat_id: int, owner_id: int, cfg: dict, stop_event: asyncio.Event):
    def notify(msg: str):
        asyncio.ensure_future(_safe_send(bot, chat_id, msg))

    brk = BreakManager(notify, stop_event)
    cycle = 0
    try:
        while not stop_event.is_set():
            await brk.check_and_apply()
            if stop_event.is_set():
                break

            cycle += 1
            async with async_session_factory() as session:
                accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
            if not accounts:
                notify("⚠️ Немає акаунтів — зупиняю автопілот.")
                break
            acc_ids = [a.id for a in accounts]

            notify(f"🔄 <b>Цикл {cycle}</b>: коментую #{cfg['hashtag']} ({len(acc_ids)} акаунтів)")
            posted, skip_night, skip_cap, skip_proxy = await _comment_phase(acc_ids, cfg, stop_event)
            msg = f"💬 Цикл {cycle}: залишено ~{posted} коментарів"
            extra = []
            if skip_night:
                extra.append(f"😴 спить (ніч): {skip_night}")
            if skip_cap:
                extra.append(f"🛑 ліміт/день: {skip_cap}")
            if skip_proxy:
                extra.append(f"🔌 мертва проксі: {skip_proxy}")
            if extra:
                msg += "\n" + " · ".join(extra)
            notify(msg)
            if stop_event.is_set():
                break

            if cfg["warmup_min"] > 0:
                notify(f"🔥 Цикл {cycle}: прогрів {cfg['warmup_min']} хв/акаунт")
                await _warmup_phase(acc_ids, cfg["warmup_min"], stop_event)
            if stop_event.is_set():
                break

            wait_min = random.uniform(20, 40)
            notify(f"😴 Пауза {wait_min:.0f} хв до наступного циклу")
            await interruptible_sleep(stop_event, wait_min * 60)
    except Exception as e:
        notify(f"❌ Автопілот зупинено через помилку: {str(e)[:80]}")
    finally:
        _stop_events.pop(owner_id, None)
        try:
            async with async_session_factory() as session:
                await AutopilotRepo(session).set_active(owner_id, False)
        except Exception:
            pass
        notify(f"⏹ Автопілот зупинено (виконано циклів: {cycle}).")


async def resume_autopilots(bot):
    """Re-start autopilots that were active when the bot was last shut down.
    Called once at startup."""
    try:
        async with async_session_factory() as session:
            active = await AutopilotRepo(session).list_active()
    except Exception:
        return
    for st in active:
        if st.owner_id in _stop_events:
            continue
        cfg = {"hashtag": st.hashtag, "comment_text": st.comment_text,
               "count": st.count, "warmup_min": st.warmup_min}
        stop_event = asyncio.Event()
        _stop_events[st.owner_id] = stop_event
        try:
            await bot.send_message(
                st.chat_id, f"🤖 Відновлюю автопілот після перезапуску (#{cfg['hashtag']})."
            )
        except Exception:
            pass
        from bot.bg import spawn
        spawn(_run_autopilot(bot, st.chat_id, st.owner_id, cfg, stop_event))


async def _comment_phase(acc_ids: list, cfg: dict, stop_event: asyncio.Event) -> int:
    """Scrape once with a scout, then comment per account (sequential, gentle)."""
    from bot.handlers.comments import _scrape_videos_sync, _browser_work_sync

    loop = asyncio.get_event_loop()
    hashtag, comment_text, count = cfg["hashtag"], cfg["comment_text"], cfg["count"]

    video_urls = None
    try:
        async with async_session_factory() as session:
            scout = await AccountRepo(session).get_by_id(acc_ids[0])
            scout_proxy = await AccountRepo(session).rotate_proxy(scout.id)
        video_urls, scout_session = await loop.run_in_executor(
            None, _scrape_videos_sync,
            scout.id, scout_proxy or scout.proxy, scout.session_data, hashtag, count,
        )
        async with async_session_factory() as session:
            await AccountRepo(session).update_session(scout.id, scout_session)
    except Exception:
        video_urls = None

    # Process accounts in a fresh random order each cycle so the same account isn't always
    # the first/last to act — a stable per-cycle ordering is itself a weak cluster pattern.
    acc_ids = list(acc_ids)
    random.shuffle(acc_ids)

    from tiktok import circadian, proxy_health
    try:
        from utils.geo import country_for_proxy
    except Exception:
        country_for_proxy = None

    total_posted = 0
    skipped_night = skipped_cap = skipped_proxy = 0
    for acc_id in acc_ids:
        if stop_event.is_set():
            break
        async with async_session_factory() as session:
            acc = await AccountRepo(session).get_by_id(acc_id)
            active_proxy = await AccountRepo(session).rotate_proxy(acc_id)
        if not acc:
            continue

        # Dead-proxy preflight: a quick reachability check through the assigned proxy. If it's
        # unreachable, rotate to the next one in the account's list and try once more; if that
        # also fails, skip the account this cycle (using a dead tunnel just fails every video
        # AND looks inconsistent to TikTok). No proxy_list → nothing to rotate to → skip.
        if active_proxy and not await proxy_health.check(active_proxy):
            async with async_session_factory() as session:
                active_proxy = await AccountRepo(session).rotate_proxy(acc_id)
            if not active_proxy or not await proxy_health.check(active_proxy):
                skipped_proxy += 1
                continue

        # Resolve the account's geo so circadian gating uses the SAME timezone the browser
        # will spoof (proxy-matched). Best-effort + cached; None → treated as awake.
        country = None
        if country_for_proxy:
            try:
                country = await country_for_proxy(active_proxy or acc.proxy)
            except Exception:
                country = None

        # Skip accounts that are in their local night — a fleet active around the clock on
        # server time is a temporal cluster tell. The probability ramps at dawn/dusk.
        if not circadian.should_act(acc_id, country):
            skipped_night += 1
            continue
        # Skip accounts that already hit their rolling-24h comment cap (maturity/trust-based).
        reached, done, cap = circadian.daily_cap_reached(acc_id)
        if reached:
            skipped_cap += 1
            continue

        try:
            # defer_shadowban omitted (False) → shadowban verified inline here, as before.
            (commented, session_data, _posted), _dbg = await loop.run_in_executor(
                None, _browser_work_sync,
                acc.id, active_proxy or acc.proxy, acc.session_data,
                hashtag, comment_text, count, video_urls, acc.username,
            )
            total_posted += commented or 0
            if session_data:
                async with async_session_factory() as session:
                    await AccountRepo(session).update_session(acc.id, session_data)
        except Exception:
            pass
        # Wider, account-specific gap so the fleet's activity is smeared across minutes
        # instead of a tight back-to-back burst from one server (a temporal cluster tell).
        await interruptible_sleep(stop_event, random.uniform(40, 180))
    return total_posted, skipped_night, skipped_cap, skipped_proxy


async def _warmup_phase(acc_ids: list, minutes: int, stop_event: asyncio.Event):
    from bot.handlers.warmup import _warmup_work_sync
    from tiktok import circadian
    try:
        from utils.geo import country_for_proxy
    except Exception:
        country_for_proxy = None

    loop = asyncio.get_event_loop()
    for acc_id in acc_ids:
        if stop_event.is_set():
            break
        async with async_session_factory() as session:
            acc = await AccountRepo(session).get_by_id(acc_id)
        if not acc:
            continue

        # Don't browse the feed in the account's deep local night either (a 4am-local
        # "viewer" is as suspicious as a 4am commenter). Reuse acc.proxy's geo.
        country = None
        if country_for_proxy:
            try:
                country = await country_for_proxy(acc.proxy)
            except Exception:
                country = None
        if circadian.activity_probability(acc_id, country) <= 0.0:
            continue

        # Per-account warmup length: ±40% jitter around the configured minutes so the whole
        # fleet doesn't browse for an identical, robotic duration every cycle.
        acc_minutes = max(1, int(round(minutes * random.uniform(0.6, 1.4))))
        try:
            # Reuse acc.proxy — the comment phase already rotated + persisted it this cycle, so
            # this is the SAME IP the account just commented from (no mid-session IP hop).
            stats, session_data = await loop.run_in_executor(
                None, _warmup_work_sync,
                acc.id, acc.proxy, acc.session_data, acc_minutes, None, "foryou", acc.username,
            )
            if session_data:
                async with async_session_factory() as session:
                    await AccountRepo(session).update_session(acc.id, session_data)
            async with async_session_factory() as session:
                await AccountRepo(session).mark_warmed(acc.id)
        except Exception:
            pass


async def _safe_send(bot, chat_id: int, msg: str):
    try:
        await bot.send_message(chat_id, msg)
    except Exception:
        pass
