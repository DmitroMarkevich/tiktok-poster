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
from tiktok import dedup
from utils.policy_filter import risk_warning
from config import BROWSERS_DIR, SUPERADMIN_ID

router = Router()

_running: set[int] = set()
# Detached background shadowban verifications. Kept in a module-level set so the running
# loop holds a reference (a bare ensure_future could be GC'd mid-flight); auto-discarded
# when each finishes.
_BG_VERIFY_TASKS: set = set()
# Concurrent accounts commenting at once. Their long human-like pauses (watch/type/
# between-comments + propagation) overlap, so raising this multiplies throughput nearly
# linearly — bounded by the host (CPU/RAM/one Chrome per account) and the proxy pool.
_COMMENT_SEMAPHORE = asyncio.Semaphore(6)
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
        # Anchor the match to the profile path FOLLOWED BY a space or end-of-line. Without
        # the anchor, `--user-data-dir=.../browsers/2` is a substring of `.../browsers/29`
        # (and 20, 21, 2xx…), so killing account 2 would also kill accounts 20-29 — and the
        # comment flow runs up to 6 accounts CONCURRENTLY, so one timeout would nuke other
        # accounts' live Chrome/sessions. The trailing ( |$) makes the profile id exact.
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-9", "-f", f"--user-data-dir={profile}( |$)",
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
        "✍️ Введи текст коментаря.\n\n"
        "Бот сам згенерує кілька схожих варіантів (AI), зберігаючи зміст — "
        "щоб коментарі не були однакові й не чіплялись під shadow-фільтр.\n\n"
        "💡 За бажанням можна задати варіанти вручну:\n"
        "• кілька рядків — кожен як окремий варіант\n"
        "• spintax: <code>{привіт|хай|вітаю}</code> → випадкове слово",
        reply_markup=cancel_kb(),
    )


@router.message(CommentTask.comment_text, F.text)
async def got_comment_text(message: Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(comment_text=text)
    await state.set_state(CommentTask.count)

    warning = risk_warning(text)
    if warning:
        await message.answer(warning)
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
    from bot.bg import spawn
    spawn(_run_bulk_comments(
        status_msg, user_id, account_ids, data["hashtag"], data["comment_text"], count
    ))


def _browser_work_sync(acc_id, proxy, session_data, hashtag, comment_text, count, video_urls=None, username="", defer_shadowban=False):
    import asyncio as _a, tempfile, os
    from tiktok.locks import account_session
    debug_path = tempfile.mktemp(suffix=".txt")
    with account_session(acc_id):  # one browser per account profile at a time
        loop = _a.new_event_loop()
        _a.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                _browser_work_async(acc_id, proxy, session_data, hashtag, comment_text, count, debug_path, video_urls, username, defer_shadowban)
            )
            return result, debug_path
        finally:
            loop.close()


def _shadowban_sync(proxy, acc_id, username, posted):
    """Run the guest-shadowban verification for one account's posted comments in its own
    event loop. Called via run_in_executor AFTER the comment job released the account lock
    + concurrency slot, so the ~40s of propagation-sleep + verify no longer blocks others.
    Uses a guest browser (no account profile) → needs no account_session lock."""
    import asyncio as _a
    from tiktok.commenter import verify_posted_shadowban
    loop = _a.new_event_loop()
    _a.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            verify_posted_shadowban(proxy, acc_id, username, posted)
        )
    finally:
        loop.close()


async def _browser_work_async(acc_id, proxy, session_data, hashtag, comment_text, count, debug_path, video_urls=None, username="", defer_shadowban=False):
    from tiktok.browser import create_context as _cc, save_session as _ss
    from tiktok.commenter import comment_on_hashtag_videos as _cmt, block_heavy_resources as _block

    from tiktok import proxy_health
    pw, ctx = None, None
    posted: list = []  # when deferred: filled by _cmt → verified in the background later
    try:
        pw, ctx = await _cc(acc_id, proxy, session_data)
        await _block(ctx)
        commented, debug_info = await _cmt(ctx, hashtag, comment_text, count,
                                           video_urls=video_urls, account_id=acc_id,
                                           proxy=proxy, username=username,
                                           defer_shadowban=defer_shadowban, posted_out=posted)
        proxy_health.record_success(proxy)  # context + run succeeded → proxy is healthy
        saved = await _ss(ctx)
        with open(debug_path, "w") as f:
            f.write(debug_info)
        return commented, saved, posted
    except Exception as e:
        # A proxy/tunnel failure during the real run feeds the dead-proxy counter too — the
        # tiny preflight can pass while TikTok's heavier CONNECT fails on a flaky endpoint.
        if "ERR_TUNNEL_CONNECTION_FAILED" in str(e) or "ERR_PROXY_CONNECTION_FAILED" in str(e):
            proxy_health.record_failure(proxy)
        raise
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
    from tiktok.locks import account_session
    with account_session(acc_id):
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
        try:
            await status_msg.edit_text(f"🔎 Збираю список відео під #{hashtag}...")
        except Exception:
            pass

        # PRIMARY: browserless tikwm search. It's the primary source inside
        # scrape_hashtag_videos anyway and needs no browser/proxy/CAPTCHA, so trying it
        # here FIRST skips the scout Chrome launch entirely in the common case. Launching a
        # full persistent context just to make this one HTTP call was the bottleneck of the
        # "🔎 Збираю список відео" phase: ~8-15s of playwright-start + proxy geo-lookup +
        # Chrome launch before any commenting, versus ~1.5s for the search itself.
        from tiktok.commenter import get_videos_tikwm
        # Pool sized for the WHOLE run, not just `count`: every account needs its own
        # distinct videos (global claim → no two accounts share one), plus a buffer for
        # videos that get skipped/fail downstream. Capped so a huge run can't hammer tikwm.
        want = min(max(total * count * 3, 20), 100)
        video_urls = await get_videos_tikwm(hashtag.lstrip("#"), want)

        # FALLBACK: only when tikwm came up short do we pay for the browser DOM scrape
        # (scrape_hashtag_videos' own fallback), run once on the scout account and shared.
        if not video_urls or len(video_urls) < count:
            async with async_session_factory() as session:
                scout = await AccountRepo(session).get_by_id(account_ids[0])
            if scout:
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

    async def _verify_and_autopause(acc_id: int, username: str, proxy, posted: list):
        """Background shadowban verification for one account — runs AFTER the semaphore is
        released, so it doesn't hold a concurrency slot during the ~40s of propagation +
        verify. Auto-pauses an account whose verified visibility is poor."""
        try:
            loop = asyncio.get_event_loop()
            # Hard cap: a guest browser stuck on a slow/dead proxy must not run forever
            # (this is detached now, but a leaked Chrome still eats resources).
            await asyncio.wait_for(
                loop.run_in_executor(None, _shadowban_sync, proxy, acc_id, username, posted),
                timeout=120,
            )
        except Exception:
            pass
        # Require a minimum verified sample so one bad/inconclusive run can't sideline a
        # healthy account. Disabled accounts are skipped until the user re-warms.
        try:
            s, b, checked, pct = dedup.account_trust(acc_id)
            if checked >= 5 and pct < 40:
                async with async_session_factory() as session:
                    await AccountRepo(session).set_account_active(acc_id, False)
                await status_msg.answer(
                    f"⏸️ <b>@{username}</b> на паузі — шедоубан "
                    f"(видимих {pct:.0f}%, {s}/{checked}).\n"
                    f"Прожени через 🔥 warmup і знову активуй у меню акаунта."
                )
        except Exception:
            pass

    async def _one(acc_id: int, pre_delay: float):
        nonlocal completed
        await asyncio.sleep(pre_delay)
        async with async_session_factory() as session:
            acc = await AccountRepo(session).get_by_id(acc_id)
        if not acc:
            results["fail"].append(f"id={acc_id}: not found")
            return

        task_id = None
        active_proxy = None
        try:
            async with _COMMENT_SEMAPHORE:
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
                    acc.id, active_proxy or acc.proxy, acc.session_data, hashtag, comment_text, count, video_urls, acc.username, True
                )
                (commented, session_data, posted), debug_path = await asyncio.wait_for(future, timeout=timeout)

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

            # ── semaphore released ── verify shadowban in the BACKGROUND (no slot held,
            # and NOT awaited before the report — verdicts land in the DB/trust, and the
            # auto-pause notice arrives whenever the check finishes).
            if posted:
                t = asyncio.ensure_future(
                    _verify_and_autopause(acc_id, acc.username, active_proxy or acc.proxy, posted)
                )
                _BG_VERIFY_TASKS.add(t)
                t.add_done_callback(_BG_VERIFY_TASKS.discard)
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
