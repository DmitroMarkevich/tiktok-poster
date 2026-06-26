"""Account warmup — Telegram flow + bulk runner.

Mirrors the comment flow (bot/handlers/comments.py): pick accounts → route → topic →
duration → run. Each account warms up in its own browser context inside a worker-thread
event loop (same pattern as commenting), so long warmups don't block the bot loop.
"""
import asyncio
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.states import Warmup
from bot.keyboards import (
    warmup_menu_kb, select_warmup_accounts_kb, warmup_route_kb, warmup_duration_kb,
    cancel_kb, main_menu,
)
from database import async_session_factory
from database.repository import AccountRepo
from config import SUPERADMIN_ID

router = Router()

_running: set = set()
_WARMUP_SEMAPHORE = asyncio.Semaphore(2)  # warmup is long & heavy — keep concurrency low


async def _safe_edit(callback: CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


# ── Entry / account selection ───────────────────────────────────────────────

@router.callback_query(F.data == "warmup")
async def start_warmup(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    if callback.from_user.id in _running:
        await callback.message.answer("⏳ Прогрів уже виконується. Зачекай.")
        return
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    if not accounts:
        await _safe_edit(callback, "Спочатку додай акаунт.", main_menu())
        return
    await _safe_edit(
        callback,
        "🔥 <b>Прогрів акаунтів</b>\n\n"
        "Органічна імітація перегляду стрічки — щоб «оживити» акаунт перед "
        "завантаженням/коментуванням і зменшити тіньовий бан.\n\nОбери режим:",
        warmup_menu_kb(len(accounts)),
    )


@router.callback_query(F.data == "wu_all")
async def wu_all(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    if not accounts:
        await _safe_edit(callback, "Немає активних акаунтів.", main_menu())
        return
    await state.update_data(account_ids=[a.id for a in accounts])
    await _safe_edit(
        callback,
        f"🔥 Прогрів на <b>{len(accounts)}</b> акаунтів.\n\nОбери маршрут:",
        warmup_route_kb(),
    )


@router.callback_query(F.data == "wu_select")
async def wu_select(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    if not accounts:
        await _safe_edit(callback, "Немає активних акаунтів.", main_menu())
        return
    await state.set_state(Warmup.select_accounts)
    await state.update_data(selected_ids=[])
    await _safe_edit(callback, "☑️ Вибери акаунти для прогріву:",
                     select_warmup_accounts_kb(accounts, set()))


@router.callback_query(F.data.startswith("toggle_wu_acc_"), Warmup.select_accounts)
async def toggle_wu_account(callback: CallbackQuery, state: FSMContext):
    toggled = int(callback.data.split("_")[-1])
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    selected.discard(toggled) if toggled in selected else selected.add(toggled)
    await state.update_data(selected_ids=list(selected))
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    await callback.message.edit_reply_markup(reply_markup=select_warmup_accounts_kb(accounts, selected))
    await callback.answer()


@router.callback_query(F.data == "select_all_wu_accs", Warmup.select_accounts)
async def select_all_wu(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    selected = {a.id for a in accounts}
    await state.update_data(selected_ids=list(selected))
    await callback.message.edit_reply_markup(reply_markup=select_warmup_accounts_kb(accounts, selected))
    await callback.answer()


@router.callback_query(F.data == "deselect_all_wu_accs", Warmup.select_accounts)
async def deselect_all_wu(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    await state.update_data(selected_ids=[])
    await callback.message.edit_reply_markup(reply_markup=select_warmup_accounts_kb(accounts, set()))
    await callback.answer()


@router.callback_query(F.data == "wu_selected_confirm", Warmup.select_accounts)
async def wu_confirm_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_ids", [])
    if not selected:
        await callback.answer("Обери хоча б один акаунт!", show_alert=True)
        return
    await state.set_state(None)
    await state.update_data(account_ids=selected)
    await _safe_edit(
        callback,
        f"🔥 Прогрів на <b>{len(selected)}</b> акаунтів.\n\nОбери маршрут:",
        warmup_route_kb(),
    )


# ── Route → topic → duration ─────────────────────────────────────────────────

@router.callback_query(F.data == "wu_route_foryou")
async def wu_route_foryou(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(route="foryou", topic=None)
    await _safe_edit(callback, "⏱ Скільки хвилин прогрівати кожен акаунт?", warmup_duration_kb())


@router.callback_query(F.data.in_({"wu_route_search", "wu_route_hashtag"}))
async def wu_route_topic(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    route = "search" if callback.data.endswith("search") else "hashtag"
    await state.update_data(route=route)
    await state.set_state(Warmup.topic)
    word = "тему для пошуку" if route == "search" else "хештег (без #)"
    await _safe_edit(
        callback,
        f"✍️ Введи {word}.\n\n"
        "Або надішли <code>-</code> щоб обрати <b>безпечну тему автоматично</b> "
        "(саморозвиток, продуктивність тощо — не палить акаунт).",
        cancel_kb(),
    )


@router.message(Warmup.topic, F.text)
async def got_warmup_topic(message: Message, state: FSMContext):
    raw = message.text.strip().lstrip("#")
    topic = None if raw in ("-", "") else raw
    await state.set_state(None)
    await state.update_data(topic=topic)
    label = f"тема: <b>{topic}</b>" if topic else "<b>безпечна авто-тема</b>"
    await message.answer(
        f"✅ Маршрут готовий ({label}).\n\n⏱ Скільки хвилин прогрівати кожен акаунт?",
        reply_markup=warmup_duration_kb(),
    )


@router.callback_query(F.data.startswith("wu_dur_"))
async def wu_launch(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    if user_id in _running:
        await callback.message.answer("⏳ Прогрів уже виконується. Зачекай.")
        return
    minutes = int(callback.data.split("_")[-1])
    data = await state.get_data()
    account_ids = data.get("account_ids")
    if not account_ids:
        await _safe_edit(callback, "Сесія застаріла — почни спочатку.", main_menu())
        await state.clear()
        return
    route = data.get("route", "search")
    topic = data.get("topic")
    await state.clear()

    await _safe_edit(
        callback,
        f"🔥 Прогрів {len(account_ids)} акаунтів по {minutes} хв запущено...",
    )
    status_msg = await callback.message.answer(f"⏳ Прогрес: <b>0/{len(account_ids)}</b>")
    _running.add(user_id)
    from bot.bg import spawn
    spawn(_run_bulk_warmup(status_msg, user_id, account_ids, minutes, topic, route))


# ── Browser work (worker-thread event loop, same pattern as commenting) ───────

def _warmup_work_sync(acc_id, proxy, session_data, minutes, topic, route, username):
    import asyncio as _a
    from tiktok.locks import account_session
    with account_session(acc_id):  # don't clash with a comment/check on the same profile
        loop = _a.new_event_loop()
        _a.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                _warmup_work_async(acc_id, proxy, session_data, minutes, topic, route, username)
            )
        finally:
            loop.close()


async def _warmup_work_async(acc_id, proxy, session_data, minutes, topic, route, username):
    from tiktok.browser import create_context as _cc, save_session as _ss
    from tiktok.warmup import warmup_account as _wu

    pw, ctx = None, None
    try:
        pw, ctx = await _cc(acc_id, proxy, session_data)
        stats = await _wu(ctx, minutes=minutes, topic=topic, route=route, my_username=username)
        saved = await _ss(ctx)
        return stats, saved
    finally:
        if ctx:
            try: await ctx.close()
            except Exception: pass
        if pw:
            try: await pw.stop()
            except Exception: pass


# ── Bulk runner ───────────────────────────────────────────────────────────────

async def _run_bulk_warmup(status_msg, owner_id: int, account_ids: list,
                           minutes: int, topic, route: str):
    results = {"ok": [], "fail": []}
    total = len(account_ids)
    completed = 0
    timeout = minutes * 60 + 180  # warmup self-limits via end_time; add margin

    async def _one(acc_id: int, pre_delay: float):
        nonlocal completed
        await asyncio.sleep(pre_delay)
        async with _WARMUP_SEMAPHORE:
            async with async_session_factory() as session:
                acc = await AccountRepo(session).get_by_id(acc_id)
            if not acc:
                results["fail"].append(f"id={acc_id}: не знайдено")
                return
            try:
                loop = asyncio.get_event_loop()
                future = loop.run_in_executor(
                    None, _warmup_work_sync,
                    acc.id, acc.proxy, acc.session_data, minutes, topic, route, acc.username,
                )
                stats, session_data = await asyncio.wait_for(future, timeout=timeout)

                if session_data:
                    async with async_session_factory() as session:
                        await AccountRepo(session).update_session(acc.id, session_data)
                async with async_session_factory() as session:
                    await AccountRepo(session).mark_warmed(acc.id)

                watched = stats.get("videos_watched", 0)
                reason = stats.get("reason", "")
                if stats.get("shadowbanned") or reason not in ("time", ""):
                    results["fail"].append(f"@{acc.username}: {reason} (відео: {watched})")
                else:
                    results["ok"].append(f"@{acc.username}: {watched} відео, {stats.get('follows_done', 0)} підп.")
            except asyncio.TimeoutError:
                results["fail"].append(f"@{acc.username}: таймаут")
            except Exception as e:
                results["fail"].append(f"@{acc.username}: {str(e)[:50]}")
            finally:
                completed += 1
                try:
                    await status_msg.edit_text(
                        f"⏳ Прогрес: <b>{completed}/{total}</b>\n"
                        f"✅ {len(results['ok'])}  ❌ {len(results['fail'])}"
                    )
                except Exception:
                    pass

    # Staggered starts so accounts don't hit TikTok in a synchronized burst.
    import random as _r
    await asyncio.gather(*[_one(aid, i * _r.uniform(3, 8)) for i, aid in enumerate(account_ids)])

    lines = [f"<b>🔥 Прогрів завершено ({total} акаунтів)</b>\n"]
    if results["ok"]:
        lines.append(f"✅ Успішно: <b>{len(results['ok'])}</b>")
        lines += [f"  • {x}" for x in results["ok"]]
    if results["fail"]:
        lines.append(f"❌ Проблеми: <b>{len(results['fail'])}</b>")
        lines += [f"  • {x}" for x in results["fail"]]

    try:
        await status_msg.edit_text("\n".join(lines), reply_markup=main_menu())
    except Exception:
        await status_msg.answer("\n".join(lines), reply_markup=main_menu())
    _running.discard(owner_id)
