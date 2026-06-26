import asyncio
import json
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from config import SUPERADMIN_ID
from bot.states import AddAccount, ImportCookies
from bot.keyboards import accounts_menu, accounts_list_kb, account_actions_kb, cancel_kb, main_menu
from database import async_session_factory
from database.repository import AccountRepo, UploadRepo
from tiktok.browser import create_context, save_session
from tiktok.auth import verify_logged_in_robust

router = Router()


def _parse_cookies(raw: str) -> list:
    """Parse a cookies JSON string (Cookie-Editor export) → list of cookies.

    Accepts a bare list, a {"cookies": [...]} wrapper, or a dict whose values
    are lists. Raises ValueError on empty / invalid input.
    """
    if not raw:
        raise ValueError("Порожній вміст")
    parsed = json.loads(raw)
    cookies = []
    if isinstance(parsed, list):
        cookies = parsed
    elif isinstance(parsed, dict):
        if isinstance(parsed.get("cookies"), list):
            cookies = parsed["cookies"]
        else:
            for v in parsed.values():
                if isinstance(v, list):
                    cookies.extend(v)
    if not cookies:
        raise ValueError("Список cookies порожній")
    return cookies


@router.callback_query(F.data == "accounts")
async def accounts_home(callback: CallbackQuery):
    async with async_session_factory() as session:
        accs = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
        today = await UploadRepo(session).get_uploads_today(callback.from_user.id)

    total = len(accs)
    with_session = sum(1 for a in accs if a.session_data)
    with_proxy = sum(1 for a in accs if a.proxy)

    await callback.message.edit_text(
        f"<b>👥 Акаунти</b>\n\n"
        f"✅ Активних: <b>{total}</b>  |  🔑 З сесією: <b>{with_session}</b>\n"
        f"🌐 З проксі: <b>{with_proxy}</b>  |  📤 Сьогодні: <b>{today}</b>",
        reply_markup=accounts_menu()
    )


# ── Add account ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "account_add")
async def start_add_account(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddAccount.username)
    await callback.message.edit_text(
        "Введи <b>username</b> акаунту TikTok (для назви в списку):",
        reply_markup=cancel_kb()
    )


@router.message(AddAccount.username, F.text)
async def got_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text.strip().lstrip("@"))
    await state.set_state(AddAccount.cookies)
    await message.answer(
        "🍪 Тепер встав <b>cookies</b> акаунту:\n\n"
        "1. Залогінься в TikTok у браузері\n"
        "2. Розширення <b>Cookie-Editor</b> → «Export» → «Export as JSON»\n"
        "3. Встав JSON сюди (текстом або .json файлом):",
        reply_markup=cancel_kb()
    )


@router.message(AddAccount.cookies, F.text | F.document)
async def got_add_cookies(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()

    raw = ""
    if message.document:
        file = await bot.get_file(message.document.file_id)
        buf = await bot.download_file(file.file_path)
        raw = buf.read().decode("utf-8", errors="ignore").strip()
    elif message.text:
        raw = message.text.strip()

    try:
        cookies = _parse_cookies(raw)
    except Exception as e:
        await message.answer(
            f"❌ Невалідний JSON: {e}\nСпробуй ще раз або натисни «Скасувати».",
            reply_markup=cancel_kb()
        )
        return

    await state.clear()
    async with async_session_factory() as session:
        acc = await AccountRepo(session).add(
            owner_id=message.from_user.id,
            username=data["username"],
            session_data=json.dumps(cookies),
        )

    await message.answer(
        f"✅ Акаунт <b>@{acc.username}</b> додано з cookies (ID: {acc.id}).\n"
        "Проксі можна призначити в меню акаунту, сесію — перевірити кнопкою 🔑.",
        reply_markup=accounts_menu()
    )


# ── List & view ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "account_list")
async def list_accounts(callback: CallbackQuery):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)

    if not accounts:
        await callback.message.edit_text("Акаунтів ще немає.", reply_markup=accounts_menu())
        return

    await callback.message.edit_text("Твої акаунти:", reply_markup=accounts_list_kb(accounts))


@router.callback_query(F.data.startswith("account_view_"))
async def view_account(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[-1])
    async with async_session_factory() as session:
        acc = await AccountRepo(session).get_by_id(account_id)

    if not acc or acc.owner_id != SUPERADMIN_ID:
        await callback.answer("Акаунт не знайдено.", show_alert=True)
        return

    proxy_text = f"<code>{acc.proxy}</code>" if acc.proxy else "❌ немає"
    rotation = ""
    if acc.proxy_list:
        try:
            n = len(json.loads(acc.proxy_list))
            rotation = f" ({n} в ротації)"
        except Exception:
            pass

    session_text = "✅ є" if acc.session_data else "❌ немає"

    await callback.message.edit_text(
        f"<b>@{acc.username}</b>  (ID: {acc.id})\n\n"
        f"📧 {acc.email or '—'}\n"
        f"🌐 Проксі: {proxy_text}{rotation}\n"
        f"🔑 Сесія: {session_text}\n"
        f"📤 Uploads: <b>{acc.upload_count or 0}</b>",
        reply_markup=account_actions_kb(account_id)
    )


# ── Check session ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("account_check_") & (F.data != "account_check_all"))
async def check_login(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[-1])
    await callback.message.edit_text("⏳ Перевіряю сесію...")

    async with async_session_factory() as session:
        acc = await AccountRepo(session).get_by_id(account_id)

    if not acc or acc.owner_id != SUPERADMIN_ID:
        await callback.message.edit_text("Акаунт не знайдено.")
        return

    if not acc.session_data:
        await callback.message.edit_text(
            f"❌ @{acc.username} — немає cookies. Додай їх кнопкою 🍪.",
            reply_markup=account_actions_kb(account_id)
        )
        return

    from tiktok.locks import get_account_lock
    lock = get_account_lock(acc.id)
    if not lock.acquire(blocking=False):
        await callback.message.edit_text(
            f"🔌 @{acc.username} зараз зайнятий іншою задачею — спробуй пізніше.",
            reply_markup=account_actions_kb(account_id)
        )
        return

    pw, ctx = None, None
    try:
        pw, ctx = await create_context(acc.id, acc.proxy, acc.session_data)

        if await verify_logged_in_robust(ctx):
            await callback.message.edit_text(
                f"✅ @{acc.username} — сесія активна.",
                reply_markup=account_actions_kb(account_id)
            )
        else:
            await callback.message.edit_text(
                f"⚠️ @{acc.username} — сесія недійсна. Онови cookies кнопкою 🍪.",
                reply_markup=account_actions_kb(account_id)
            )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Помилка: {e}", reply_markup=account_actions_kb(account_id)
        )
    finally:
        if ctx: await ctx.close()
        if pw:  await pw.stop()
        lock.release()


@router.callback_query(F.data == "account_check_all")
async def check_all_sessions(callback: CallbackQuery):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)

    if not accounts:
        await callback.answer("Немає акаунтів.", show_alert=True)
        return

    msg = await callback.message.edit_text(
        f"⏳ Перевіряю сесії {len(accounts)} акаунтів..."
    )
    from bot.bg import spawn
    spawn(_bulk_check_sessions(msg, accounts))


async def _bulk_check_sessions(msg, accounts: list):
    from tiktok.locks import get_account_lock
    sem = asyncio.Semaphore(2)
    results = {"ok": [], "invalid": [], "failed": [], "busy": []}
    total = len(accounts)
    done = 0

    async def _check_one(acc):
        nonlocal done
        async with sem:
            # Skip accounts whose profile is in use (commenting/warmup) — launching a
            # second Chrome on the same profile corrupts the live session.
            lock = get_account_lock(acc.id)
            if not lock.acquire(blocking=False):
                results["busy"].append(acc.username)
                done += 1
                return
            pw, ctx = None, None
            try:
                if not acc.session_data:
                    results["invalid"].append(f"@{acc.username}: немає cookies")
                    return
                pw, ctx = await create_context(acc.id, acc.proxy, acc.session_data)
                if await verify_logged_in_robust(ctx):
                    results["ok"].append(acc.username)
                else:
                    results["invalid"].append(f"@{acc.username}: сесія недійсна")
            except Exception as e:
                results["failed"].append(f"@{acc.username}: {str(e)[:40]}")
            finally:
                if ctx: await ctx.close()
                if pw:  await pw.stop()
                lock.release()
                done += 1
                try:
                    await msg.edit_text(
                        f"⏳ Перевіряю [{done}/{total}]...\n"
                        f"✅ {len(results['ok'])}  "
                        f"⚠️ {len(results['invalid'])}  "
                        f"🔌 {len(results['busy'])}  "
                        f"❌ {len(results['failed'])}"
                    )
                except Exception:
                    pass

    await asyncio.gather(*[_check_one(acc) for acc in accounts])

    lines = [f"<b>✅ Перевірка завершена ({total} акаунтів)</b>\n"]
    if results["ok"]:
        lines.append(f"✅ Активних: <b>{len(results['ok'])}</b>")
    if results["invalid"]:
        lines.append(f"⚠️ Потрібні нові cookies: <b>{len(results['invalid'])}</b>")
        lines += [f"  • {e}" for e in results["invalid"]]
    if results["busy"]:
        lines.append(f"🔌 Зайняті (пропущено): <b>{len(results['busy'])}</b>")
        lines += [f"  • @{u}" for u in results["busy"]]
    if results["failed"]:
        lines.append(f"❌ Помилки: <b>{len(results['failed'])}</b>")
        lines += [f"  • {e}" for e in results["failed"]]

    await msg.edit_text("\n".join(lines), reply_markup=accounts_menu())


# ── Cookies import ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("account_cookies_"))
async def start_import_cookies(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split("_")[-1])
    await state.set_state(ImportCookies.waiting_cookies)
    await state.update_data(account_id=account_id)
    await callback.message.edit_text(
        "🍪 <b>Імпорт cookies</b>\n\n"
        "1. Відкрий TikTok у браузері та залогінься\n"
        "2. Встанови розширення <b>Cookie-Editor</b>\n"
        "3. Натисни «Export» → «Export as JSON»\n"
        "4. Встав JSON сюди:",
        reply_markup=cancel_kb()
    )


@router.message(ImportCookies.waiting_cookies, F.text | F.document)
async def got_cookies(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    account_id = data["account_id"]
    await state.clear()

    raw = ""
    if message.document:
        file = await bot.get_file(message.document.file_id)
        buf = await bot.download_file(file.file_path)
        raw = buf.read().decode("utf-8", errors="ignore").strip()
    elif message.text:
        raw = message.text.strip()

    try:
        cookies = _parse_cookies(raw)
    except Exception as e:
        await message.answer(
            f"❌ Невалідний JSON: {e}",
            reply_markup=account_actions_kb(account_id)
        )
        return

    async with async_session_factory() as session:
        acc = await AccountRepo(session).get_by_id(account_id)
        if not acc or acc.owner_id != SUPERADMIN_ID:
            await message.answer("Акаунт не знайдено.")
            return
        await AccountRepo(session).update_session(account_id, json.dumps(cookies))

    await message.answer(
        f"✅ Cookies збережено для @{acc.username}!",
        reply_markup=account_actions_kb(account_id)
    )


# ── Delete ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("account_delete_"))
async def delete_account(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[-1])
    async with async_session_factory() as session:
        acc = await AccountRepo(session).get_by_id(account_id)
        if acc and acc.owner_id == SUPERADMIN_ID:
            await AccountRepo(session).delete(account_id)

    await callback.message.edit_text("🗑 Акаунт видалено.", reply_markup=accounts_menu())
