import asyncio
import os
import random
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile, BufferedInputFile
from aiogram.fsm.context import FSMContext

from bot.states import CommentTask
from bot.keyboards import (
    comment_menu_kb, select_cmt_accounts_kb, cancel_kb, main_menu,
)
from database import async_session_factory
from database.repository import AccountRepo, CommentRepo
from tiktok.browser import create_context, save_session
from tiktok.commenter import comment_on_hashtag_videos
from config import BROWSERS_DIR, SUPERADMIN_ID

router = Router()

_running: set[int] = set()
_COMMENT_SEMAPHORE = asyncio.Semaphore(3)
# Base: hashtag-page CAPTCHA solve + DOM scrape (~20-30s observed).
# Per-video: new tab + navigate + CAPTCHA + type/post + full-reload persistence check
# (with its own CAPTCHA + scroll search) — live runs measured up to ~480s for ONE video,
# so per-video budget must cover that worst case with margin, not the earlier 180s guess.
COMMENT_TIMEOUT_BASE = 180
COMMENT_TIMEOUT_PER_VIDEO = 900


async def _kill_profile_chrome(account_id: int):
    """Force-kill any Chrome processes still holding this account's profile dir.

    asyncio.wait_for cancels the *awaiting coroutine* on timeout, but the actual
    browser work runs inside a thread-pool executor (loop.run_in_executor) — and
    Python cannot forcibly stop a running thread. A timed-out comment run leaves
    the browser (and its SingletonLock) alive forever, leaking processes and
    blocking every subsequent attempt on that profile with launch errors like
    'Target page, context or browser has been closed'."""
    profile = os.path.join(BROWSERS_DIR, str(account_id))
    try:
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-9", "-f", f"--user-data-dir={profile}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(profile, name))
        except OSError:
            pass


async def _safe_edit(callback: CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "comment")
async def start_comment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id

    if user_id in _running:
        await callback.message.answer("⏳ Вже виконується коментування. Зачекай.")
        return

    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)

    if not accounts:
        await _safe_edit(callback, "Спочатку додай акаунт.", main_menu())
        return

    await _safe_edit(callback, "💬 <b>Коментування</b>\n\nОбери режим:", comment_menu_kb(len(accounts)))


@router.callback_query(F.data == "cmt_all")
async def cmt_all(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    if user_id in _running:
        await callback.message.answer("⏳ Вже виконується коментування. Зачекай.")
        return

    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    if not accounts:
        await _safe_edit(callback, "Немає активних акаунтів.", main_menu())
        return

    await state.update_data(bulk=True, account_ids=[a.id for a in accounts])
    await state.set_state(CommentTask.hashtag)
    await _safe_edit(
        callback,
        f"💬 Коментування на <b>{len(accounts)}</b> акаунтів.\n\n"
        "Введи хештег або ключове слово для пошуку відео:\n"
        "Приклад: <code>dance</code> або <code>#fyp</code>",
        cancel_kb()
    )


@router.callback_query(F.data == "cmt_select")
async def cmt_select(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    if user_id in _running:
        await callback.message.answer("⏳ Вже виконується коментування.")
        return

    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    if not accounts:
        await _safe_edit(callback, "Немає активних акаунтів.", main_menu())
        return

    await state.set_state(CommentTask.select_accounts)
    await state.update_data(bulk=True, selected_ids=[])
    await _safe_edit(callback, "☑️ Вибери акаунти для коментування:", select_cmt_accounts_kb(accounts, set()))


@router.callback_query(F.data.startswith("toggle_cmt_acc_"), CommentTask.select_accounts)
async def toggle_cmt_account(callback: CallbackQuery, state: FSMContext):
    toggled_id = int(callback.data.split("_")[-1])
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    selected.discard(toggled_id) if toggled_id in selected else selected.add(toggled_id)
    await state.update_data(selected_ids=list(selected))

    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    await callback.message.edit_reply_markup(reply_markup=select_cmt_accounts_kb(accounts, selected))
    await callback.answer()


@router.callback_query(F.data == "select_all_cmt_accs", CommentTask.select_accounts)
async def select_all_cmt(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    selected = {a.id for a in accounts}
    await state.update_data(selected_ids=list(selected))
    await callback.message.edit_reply_markup(reply_markup=select_cmt_accounts_kb(accounts, selected))
    await callback.answer()


@router.callback_query(F.data == "deselect_all_cmt_accs", CommentTask.select_accounts)
async def deselect_all_cmt(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    await state.update_data(selected_ids=[])
    await callback.message.edit_reply_markup(reply_markup=select_cmt_accounts_kb(accounts, set()))
    await callback.answer()


@router.callback_query(F.data == "cmt_selected_confirm", CommentTask.select_accounts)
async def cmt_confirm_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_ids", [])
    if not selected:
        await callback.answer("Обери хоча б один акаунт!", show_alert=True)
        return

    await state.update_data(account_ids=selected)
    await state.set_state(CommentTask.hashtag)
    await _safe_edit(
        callback,
        f"💬 Коментування на <b>{len(selected)}</b> акаунтів.\n\n"
        "Введи хештег або ключове слово для пошуку відео:\n"
        "Приклад: <code>dance</code> або <code>#fyp</code>",
        cancel_kb()
    )


@router.message(CommentTask.hashtag, F.text)
async def got_hashtag(message: Message, state: FSMContext):
    await state.update_data(hashtag=message.text.strip().lstrip("#"))
    await state.set_state(CommentTask.comment_text)
    await message.answer(
        "Введи текст коментаря.\n\n"
        "💡 Щоб уникнути shadow-фільтра TikTok, додай варіативність:\n"
        "• <b>Кілька варіантів</b> — кожен з нового рядка (вибереться випадковий)\n"
        "• <b>Spintax</b> — <code>{привіт|хай|вітаю}</code> розгорнеться у випадкове слово\n\n"
        "Приклад:\n"
        "<code>{вогонь|супер|клас}🔥 {дуже круто|топ контент}</code>",
        reply_markup=cancel_kb(),
    )


@router.message(CommentTask.comment_text, F.text)
async def got_comment_text(message: Message, state: FSMContext):
    await state.update_data(comment_text=message.text.strip())
    await state.set_state(CommentTask.count)
    await message.answer("На скільки відео залишити коментар? (1–10):", reply_markup=cancel_kb())


@router.message(CommentTask.count, F.text)
async def got_count(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if user_id in _running:
        await message.answer("⏳ Вже виконується коментування. Зачекай.")
        await state.clear()
        return

    try:
        count = max(1, min(10, int(message.text.strip())))
    except ValueError:
        await message.answer("Введи число від 1 до 10.")
        return

    data = await state.get_data()
    await state.clear()

    account_ids = data["account_ids"]
    await message.answer(
        f"⏳ Починаю коментування під #{data['hashtag']} на <b>{len(account_ids)}</b> акаунтів..."
    )
    status_msg = await message.answer(f"⏳ Прогрес: <b>0/{len(account_ids)}</b>")
    _running.add(user_id)
    asyncio.ensure_future(_run_bulk_comments(
        status_msg, user_id, account_ids, data["hashtag"], data["comment_text"], count
    ))


def _browser_work_sync(acc_id, proxy, session_data, hashtag, comment_text, count, video_urls=None):
    import asyncio as _a, tempfile, os
    debug_path = tempfile.mktemp(suffix=".txt")
    loop = _a.new_event_loop()
    _a.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            _browser_work_async(acc_id, proxy, session_data, hashtag, comment_text, count, debug_path, video_urls)
        )
        return result, debug_path
    finally:
        loop.close()


async def _browser_work_async(acc_id, proxy, session_data, hashtag, comment_text, count, debug_path, video_urls=None):
    from tiktok.browser import create_context as _cc, save_session as _ss
    from tiktok.commenter import comment_on_hashtag_videos as _cmt, block_heavy_resources as _block

    pw, ctx = None, None
    try:
        pw, ctx = await _cc(acc_id, proxy, session_data)
        await _block(ctx)
        commented, debug_info = await _cmt(ctx, hashtag, comment_text, count, video_urls=video_urls)
        saved = await _ss(ctx)
        with open(debug_path, "w") as f:
            f.write(debug_info)
        return commented, saved
    finally:
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


def _scrape_videos_sync(acc_id, proxy, session_data, hashtag, count):
    import asyncio as _a
    loop = _a.new_event_loop()
    _a.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            _scrape_videos_async(acc_id, proxy, session_data, hashtag, count)
        )
    finally:
        loop.close()


async def _scrape_videos_async(acc_id, proxy, session_data, hashtag, count):
    from tiktok.browser import create_context as _cc, save_session as _ss
    from tiktok.commenter import scrape_hashtag_videos as _scrape, block_heavy_resources as _block

    pw, ctx = None, None
    try:
        pw, ctx = await _cc(acc_id, proxy, session_data)
        await _block(ctx)
        video_urls, _dbg = await _scrape(ctx, hashtag, count)
        saved = await _ss(ctx)
        return video_urls, saved
    finally:
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


async def _run_bulk_comments(status_msg, owner_id: int, account_ids: list,
                             hashtag: str, comment_text: str, count: int):
    results = {"ok": [], "fail": []}
    total = len(account_ids)
    completed = 0
    timeout = COMMENT_TIMEOUT_BASE + count * COMMENT_TIMEOUT_PER_VIDEO

    # Scrape the hashtag's video list ONCE (using the first account) and share it
    # across every account in this run — every account rescanning the same hashtag
    # page is identical, wasted work (~8-30s of navigation+CAPTCHA+scroll each).
    video_urls = None
    try:
        async with async_session_factory() as session:
            scout = await AccountRepo(session).get_by_id(account_ids[0])
        if scout:
            try:
                await status_msg.edit_text(f"🔎 Збираю список відео під #{hashtag}...")
            except Exception:
                pass
            async with async_session_factory() as session:
                scout_proxy = await AccountRepo(session).rotate_proxy(scout.id)
            loop = asyncio.get_event_loop()
            video_urls, scout_session = await loop.run_in_executor(
                None, _scrape_videos_sync,
                scout.id, scout_proxy or scout.proxy, scout.session_data, hashtag, count
            )
            async with async_session_factory() as session:
                await AccountRepo(session).update_session(scout.id, scout_session)
            try:
                await status_msg.edit_text(
                    f"⏳ Прогрес: <b>0/{total}</b>\n🔎 Знайдено відео: {len(video_urls or [])}"
                )
            except Exception:
                pass
    except Exception:
        video_urls = None  # fall back to per-account scraping if the shared scrape fails

    async def _one(acc_id: int, pre_delay: float):
        nonlocal completed
        await asyncio.sleep(pre_delay)
        async with _COMMENT_SEMAPHORE:
            async with async_session_factory() as session:
                acc = await AccountRepo(session).get_by_id(acc_id)
            if not acc:
                results["fail"].append(f"id={acc_id}: not found")
                return

            task_id = None
            try:
                async with async_session_factory() as session:
                    task = await CommentRepo(session).create(
                        account_id=acc_id,
                        owner_id=owner_id,
                        hashtag=hashtag,
                        comment_text=comment_text,
                        count=count,
                    )
                    task_id = task.id
                    await CommentRepo(session).set_status(task_id, "running")

                async with async_session_factory() as session:
                    active_proxy = await AccountRepo(session).rotate_proxy(acc_id)

                loop = asyncio.get_event_loop()
                future = loop.run_in_executor(
                    None,
                    _browser_work_sync,
                    acc.id, active_proxy or acc.proxy, acc.session_data, hashtag, comment_text, count, video_urls
                )
                (commented, session_data), debug_path = await asyncio.wait_for(future, timeout=timeout)

                video_links = []
                skip_reasons = []
                if debug_path and os.path.exists(debug_path):
                    try:
                        debug_text = open(debug_path).read()
                        video_links = [
                            line.strip() for line in debug_text.splitlines()
                            if line.strip().startswith("https://www.tiktok.com")
                        ]
                        skip_reasons = [
                            line.strip() for line in debug_text.splitlines()
                            if line.strip().startswith("✗")
                        ]
                    except OSError:
                        pass
                    finally:
                        try:
                            os.remove(debug_path)
                        except OSError:
                            pass

                async with async_session_factory() as session:
                    await AccountRepo(session).update_session(acc_id, session_data)
                    await CommentRepo(session).set_status(task_id, "done")

                results["ok"].append((acc.username, commented, video_links, skip_reasons))
                await asyncio.sleep(random.uniform(20, 60))
            except asyncio.TimeoutError:
                await _kill_profile_chrome(acc.id)
                if task_id:
                    async with async_session_factory() as session:
                        await CommentRepo(session).set_status(task_id, "failed", "timeout")
                results["fail"].append(f"@{acc.username}: timeout")
            except Exception as e:
                if task_id:
                    async with async_session_factory() as session:
                        await CommentRepo(session).set_status(task_id, "failed", str(e))
                results["fail"].append(f"@{acc.username}: {str(e)[:60]}")
            finally:
                completed += 1
                try:
                    await status_msg.edit_text(
                        f"⏳ Прогрес: <b>{completed}/{total}</b>\n"
                        f"✅ {len(results['ok'])}  ❌ {len(results['fail'])}"
                    )
                except Exception:
                    pass

    try:
        await asyncio.gather(*[
            _one(aid, i * random.uniform(20, 50))
            for i, aid in enumerate(account_ids)
        ])

        total_commented = sum(c for _, c, _, _ in results["ok"])
        summary = [
            f"📊 Bulk коментування завершено (<b>{total}</b> акаунтів) під #{hashtag}.",
            f"✅ Успішно: {len(results['ok'])} акаунтів, {total_commented} коментарів",
            f"❌ Помилки: {len(results['fail'])}",
        ]
        await status_msg.edit_text("\n".join(summary), reply_markup=main_menu())

        # Detailed per-video breakdown goes to a file, not chat — with up to ~100
        # comments per run an inline list becomes unreadable wall-of-text.
        report_lines = [f"Звіт по коментуванню під #{hashtag}", f"Акаунтів: {total}", ""]
        if results["ok"]:
            report_lines.append(f"Успішно ({len(results['ok'])} акаунтів, {total_commented} коментарів):")
            for username, commented, video_links, skip_reasons in results["ok"]:
                report_lines.append(f"@{username} — прокоментовано {commented}:")
                report_lines += [f"  {url}" for url in video_links]
                if skip_reasons:
                    report_lines.append("  Пропущено:")
                    report_lines += [f"    {r}" for r in skip_reasons]
            report_lines.append("")
        if results["fail"]:
            report_lines.append(f"Помилки ({len(results['fail'])}):")
            report_lines += [f"  {e}" for e in results["fail"]]

        try:
            await status_msg.answer_document(
                BufferedInputFile("\n".join(report_lines).encode("utf-8"), filename=f"comments_{hashtag}.txt"),
                caption="📄 Детальний звіт: акаунти + посилання на відео"
            )
        except Exception:
            pass
    finally:
        _running.discard(owner_id)
