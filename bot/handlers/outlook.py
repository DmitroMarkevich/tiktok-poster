"""Telegram handler for Outlook auto-registration."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from bot.bg import spawn
from database import async_session_factory
from database.models import OutlookAccount
from database.repository import AccountRepo
from outlook.registrar import register_one, OutlookCreds
from outlook.reader import fetch_tiktok_code
from tiktok.signup import register_one as tt_register_one, TikTokCreds
from config import HEADLESS

logger = logging.getLogger(__name__)
router = Router()


class OutlookRegStates(StatesGroup):
    waiting_count  = State()
    waiting_proxy  = State()
    waiting_captcha = State()


def outlook_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Пошта + TikTok (під ключ)", callback_data="ol_full")],
        [InlineKeyboardButton(text="➕ Тільки пошти", callback_data="ol_register")],
        [InlineKeyboardButton(text="🎵 TikTok на готових поштах", callback_data="ol_tiktok")],
        [InlineKeyboardButton(text="📋 Список акків", callback_data="ol_list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])


def captcha_done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Капчу вирішено, продовжити", callback_data="ol_captcha_done")],
        [InlineKeyboardButton(text="❌ Пропустити цей акк", callback_data="ol_captcha_skip")],
    ])


@router.callback_query(F.data == "outlook")
async def outlook_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("📧 <b>Outlook авторегер</b>", reply_markup=outlook_menu_kb())


@router.callback_query(F.data.in_({"ol_register", "ol_full"}))
async def ol_start_register(callback: CallbackQuery, state: FSMContext):
    # "ol_full" = create mailbox AND register TikTok on it; "ol_register" = mailbox only.
    mode = "full" if callback.data == "ol_full" else "outlook"
    await state.update_data(mode=mode)
    await state.set_state(OutlookRegStates.waiting_count)
    title = "пошту + TikTok" if mode == "full" else "пошти"
    await callback.message.edit_text(
        f"Скільки акків зробити ({title})? (1–20)\n\nНадішли число:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="outlook")]
        ])
    )


@router.message(OutlookRegStates.waiting_count)
async def ol_got_count(message: Message, state: FSMContext):
    try:
        count = int(message.text.strip())
        if not 1 <= count <= 20:
            raise ValueError
    except ValueError:
        await message.answer("Введи число від 1 до 20.")
        return

    await state.update_data(count=count)
    await state.set_state(OutlookRegStates.waiting_proxy)
    await message.answer(
        "Надішли проксі для реєстрації (формат <code>host:port:user:pass</code>)\n"
        "або <b>без</b> — якщо без проксі:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Без проксі", callback_data="ol_no_proxy")],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="outlook")],
        ])
    )


@router.callback_query(F.data == "ol_no_proxy")
async def ol_no_proxy(callback: CallbackQuery, state: FSMContext):
    await state.update_data(proxy=None)
    data = await state.get_data()
    await state.clear()
    await callback.message.edit_text("⏳ Починаю реєстрацію...")
    job = _run_bulk_full if data.get("mode") == "full" else _run_bulk_register
    spawn(job(
        owner_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        bot=callback.bot,
        count=data["count"],
        proxy=None,
    ))


@router.message(OutlookRegStates.waiting_proxy)
async def ol_got_proxy(message: Message, state: FSMContext):
    proxy = message.text.strip()
    await state.update_data(proxy=proxy)
    data = await state.get_data()
    await state.clear()
    await message.answer("⏳ Починаю реєстрацію...")
    job = _run_bulk_full if data.get("mode") == "full" else _run_bulk_register
    spawn(job(
        owner_id=message.from_user.id,
        chat_id=message.chat.id,
        bot=message.bot,
        count=data["count"],
        proxy=proxy,
    ))


@router.callback_query(F.data == "ol_list")
async def ol_list(callback: CallbackQuery):
    async with async_session_factory() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(OutlookAccount).where(OutlookAccount.owner_id == callback.from_user.id)
            .order_by(OutlookAccount.id.desc()).limit(30)
        )).scalars().all()

    if not rows:
        await callback.message.edit_text(
            "Немає збережених Outlook акків.",
            reply_markup=outlook_menu_kb()
        )
        return

    lines = []
    for r in rows:
        icon = {"created": "🆕", "used": "✅", "banned": "❌"}.get(r.status, "❓")
        lines.append(f"{icon} <code>{r.email}</code>  |  {r.status}")

    await callback.message.edit_text(
        f"📧 <b>Outlook акки ({len(rows)}):</b>\n\n" + "\n".join(lines),
        reply_markup=outlook_menu_kb()
    )


# ── Background registration job ───────────────────────────────────────────────

_captcha_events: dict[int, asyncio.Event] = {}
_captcha_skip: dict[int, bool] = {}


@router.callback_query(F.data == "ol_captcha_done")
async def ol_captcha_done(callback: CallbackQuery):
    ev = _captcha_events.get(callback.from_user.id)
    if ev:
        _captcha_skip[callback.from_user.id] = False
        ev.set()
    await callback.answer("Продовжую...")


@router.callback_query(F.data == "ol_captcha_skip")
async def ol_captcha_skip_cb(callback: CallbackQuery):
    ev = _captcha_events.get(callback.from_user.id)
    if ev:
        _captcha_skip[callback.from_user.id] = True
        ev.set()
    await callback.answer("Пропускаю...")


async def _run_bulk_register(
    owner_id: int,
    chat_id: int,
    bot,
    count: int,
    proxy: str | None,
):
    success, failed = 0, 0

    for i in range(count):
        await bot.send_message(chat_id, f"⏳ Реєструю акк {i+1}/{count}...")

        ev = asyncio.Event()
        _captcha_events[owner_id] = ev
        _captcha_skip[owner_id] = False

        async def captcha_cb(page, email: str):
            await bot.send_message(
                chat_id,
                f"🧩 <b>Капча!</b> Акк <code>{email}</code>\n\n"
                f"Вирши капчу в браузері та натисни кнопку нижче:",
                reply_markup=captcha_done_kb(),
            )
            await asyncio.wait_for(ev.wait(), timeout=180)

        try:
            creds: OutlookCreds | None = await register_one(
                proxy=proxy,
                captcha_callback=captcha_cb,
                headless=HEADLESS,
            )
        except Exception as e:
            logger.error("register_one exception: %s", e)
            creds = None

        _captcha_events.pop(owner_id, None)
        _captcha_skip.pop(owner_id, None)

        if _captcha_skip.get(owner_id):
            await bot.send_message(chat_id, f"⏭ Акк {i+1} пропущено.")
            failed += 1
            continue

        if not creds:
            await bot.send_message(chat_id, f"❌ Акк {i+1} не вдалося зареєструвати.")
            failed += 1
        else:
            async with async_session_factory() as session:
                acc = OutlookAccount(
                    owner_id=owner_id,
                    email=creds.email,
                    password=creds.password,
                    first_name=creds.first_name,
                    last_name=creds.last_name,
                    birth_year=creds.birth_year,
                    proxy=creds.proxy,
                    status="created",
                )
                session.add(acc)
                await session.commit()

            await bot.send_message(
                chat_id,
                f"✅ <b>Акк {i+1} створено!</b>\n"
                f"📧 <code>{creds.email}</code>\n"
                f"🔑 <code>{creds.password}</code>"
            )
            success += 1

        # пауза між акками щоб не виглядати підозріло
        if i < count - 1:
            await asyncio.sleep(15)

    await bot.send_message(
        chat_id,
        f"🏁 <b>Реєстрація завершена</b>\n"
        f"✅ Успішно: {success}\n"
        f"❌ Невдало: {failed}",
        reply_markup=outlook_menu_kb(),
    )


# ── TikTok signup on created Outlook mailboxes ────────────────────────────────


@router.callback_query(F.data == "ol_tiktok")
async def ol_tiktok_start(callback: CallbackQuery):
    """Register TikTok accounts on every still-unused Outlook mailbox."""
    from sqlalchemy import select

    async with async_session_factory() as session:
        rows = (await session.execute(
            select(OutlookAccount)
            .where(OutlookAccount.owner_id == callback.from_user.id,
                   OutlookAccount.status == "created")
            .order_by(OutlookAccount.id.asc())
        )).scalars().all()
        # Detach plain values so we don't touch the session inside the bg job.
        pending = [
            {"id": r.id, "email": r.email, "password": r.password, "proxy": r.proxy}
            for r in rows
        ]

    if not pending:
        await callback.message.edit_text(
            "Немає вільних Outlook акків (status=created) для реєстрації TikTok.",
            reply_markup=outlook_menu_kb(),
        )
        return

    await callback.message.edit_text(
        f"⏳ Реєструю TikTok на {len(pending)} поштах..."
    )
    spawn(_run_bulk_tiktok_signup(
        owner_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        bot=callback.bot,
        mailboxes=pending,
    ))


async def _register_tiktok_for_mailbox(owner_id: int, chat_id: int, bot, mb: dict, label: str) -> bool:
    """Register ONE TikTok account on the given mailbox (dict: id/email/password/proxy).

    On success: persists the TikTok account (with its session cookies), marks the mailbox
    `used` + links `tiktok_id`, and reports the creds. Returns True/False. Shared by the
    standalone «TikTok на готових поштах» flow and the turnkey «Пошта + TikTok» flow."""
    email, password, proxy = mb["email"], mb["password"], mb["proxy"]

    # Code fetcher: log into THIS mailbox (via its own proxy) and read the freshest code.
    async def code_fetcher(addr: str, _pw=password, _proxy=proxy) -> str | None:
        try:
            return await fetch_tiktok_code(addr, _pw, proxy=_proxy, headless=HEADLESS)
        except Exception as e:
            logger.error("fetch_tiktok_code failed for %s: %s", addr, e)
            return None

    try:
        creds: TikTokCreds | None = await tt_register_one(
            email=email,
            code_fetcher=code_fetcher,
            proxy=proxy,
            account_id=800_000 + mb["id"],
        )
    except Exception as e:
        logger.error("tt_register_one exception for %s: %s", email, e)
        creds = None

    if not creds:
        await bot.send_message(chat_id, f"❌ TikTok {label} не вдалося ({email}).")
        return False

    async with async_session_factory() as session:
        repo = AccountRepo(session)
        acc = await repo.add(
            owner_id=owner_id,
            username=creds.username,
            password=creds.password,
            email=creds.email,
            proxy=proxy,
            session_data=creds.session_data,
        )
        # Mark the mailbox used and link it to the new TikTok account.
        from sqlalchemy import update as _update
        await session.execute(
            _update(OutlookAccount)
            .where(OutlookAccount.id == mb["id"])
            .values(status="used", tiktok_id=acc.id)
        )
        await session.commit()

    await bot.send_message(
        chat_id,
        f"✅ <b>TikTok {label} створено!</b>\n"
        f"👤 <code>@{creds.username}</code>\n"
        f"📧 <code>{creds.email}</code>\n"
        f"🔑 <code>{creds.password}</code>"
    )
    return True


async def _run_bulk_tiktok_signup(owner_id: int, chat_id: int, bot, mailboxes: list[dict]):
    success, failed = 0, 0
    total = len(mailboxes)

    for i, mb in enumerate(mailboxes):
        await bot.send_message(chat_id, f"🎵 TikTok {i+1}/{total}: <code>{mb['email']}</code>")
        ok = await _register_tiktok_for_mailbox(owner_id, chat_id, bot, mb, f"{i+1}/{total}")
        success += int(ok)
        failed += int(not ok)
        if i < total - 1:
            await asyncio.sleep(20)

    await bot.send_message(
        chat_id,
        f"🏁 <b>TikTok реєстрація завершена</b>\n"
        f"✅ Успішно: {success}\n"
        f"❌ Невдало: {failed}",
        reply_markup=outlook_menu_kb(),
    )


# ── Turnkey: create mailbox AND register TikTok on it, in one go ──────────────


async def _run_bulk_full(owner_id: int, chat_id: int, bot, count: int, proxy: str | None):
    """For each of `count`: create an Outlook mailbox, then immediately register TikTok on it.

    Reuses the same Telegram captcha-callback as the mailbox-only flow (PerimeterX on Outlook
    signup may need a manual solve), then chains straight into the TikTok signup which reads
    its verification code from the just-created mailbox."""
    ok_mail, ok_tt = 0, 0

    for i in range(count):
        await bot.send_message(chat_id, f"⏳ [{i+1}/{count}] Створюю пошту...")

        ev = asyncio.Event()
        _captcha_events[owner_id] = ev
        _captcha_skip[owner_id] = False

        async def captcha_cb(page, email: str):
            await bot.send_message(
                chat_id,
                f"🧩 <b>Капча (Outlook)!</b> Акк <code>{email}</code>\n\n"
                f"Вирши капчу в браузері та натисни кнопку нижче:",
                reply_markup=captcha_done_kb(),
            )
            await asyncio.wait_for(ev.wait(), timeout=180)

        try:
            creds: OutlookCreds | None = await register_one(
                proxy=proxy, captcha_callback=captcha_cb, headless=HEADLESS,
            )
        except Exception as e:
            logger.error("register_one exception: %s", e)
            creds = None

        skipped = _captcha_skip.get(owner_id)
        _captcha_events.pop(owner_id, None)
        _captcha_skip.pop(owner_id, None)

        if skipped or not creds:
            await bot.send_message(chat_id, f"❌ [{i+1}/{count}] Пошту не створено — пропускаю.")
            continue

        # Persist the mailbox and capture its id so we can link the TikTok account to it.
        async with async_session_factory() as session:
            acc = OutlookAccount(
                owner_id=owner_id, email=creds.email, password=creds.password,
                first_name=creds.first_name, last_name=creds.last_name,
                birth_year=creds.birth_year, proxy=creds.proxy, status="created",
            )
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
            mb = {"id": acc.id, "email": acc.email, "password": acc.password, "proxy": acc.proxy}

        ok_mail += 1
        await bot.send_message(
            chat_id,
            f"✅ [{i+1}/{count}] Пошта: <code>{creds.email}</code>\n"
            f"🔑 <code>{creds.password}</code>\n🎵 Реєструю TikTok..."
        )

        if await _register_tiktok_for_mailbox(owner_id, chat_id, bot, mb, f"{i+1}/{count}"):
            ok_tt += 1

        if i < count - 1:
            await asyncio.sleep(15)

    await bot.send_message(
        chat_id,
        f"🏁 <b>Готово (під ключ)</b>\n"
        f"📧 Пошт створено: {ok_mail}/{count}\n"
        f"🎵 TikTok створено: {ok_tt}/{count}",
        reply_markup=outlook_menu_kb(),
    )
