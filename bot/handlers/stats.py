from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import SUPERADMIN_ID
from bot.keyboards import back_main_kb, main_menu
from database import async_session_factory
from database.repository import AccountRepo, UploadRepo

router = Router()


@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    async with async_session_factory() as session:
        accs = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
        s = await UploadRepo(session).get_stats(callback.from_user.id)

    total_accs = len(accs)
    with_session = sum(1 for a in accs if a.session_data)
    with_proxy = sum(1 for a in accs if a.proxy)

    top = sorted(accs, key=lambda a: a.upload_count or 0, reverse=True)[:5]
    top_lines = [
        f"  • @{a.username} — {a.upload_count} upl"
        for a in top if (a.upload_count or 0) > 0
    ]

    text = (
        f"<b>📊 Статистика</b>\n\n"
        f"<b>Акаунти</b>\n"
        f"✅ Активних: <b>{total_accs}</b>  |  🔑 З сесією: <b>{with_session}</b>  |  🌐 З проксі: <b>{with_proxy}</b>\n\n"
        f"<b>Завантаження</b>\n"
        f"📤 Всього: <b>{s['total']}</b>  |  📅 Сьогодні: <b>{s['today']}</b>\n"
        f"✅ Успішних: <b>{s['success']}</b>  |  ❌ Помилок: <b>{s['failed']}</b>"
    )
    if top_lines:
        text += "\n\n<b>Топ акаунтів:</b>\n" + "\n".join(top_lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Останні задачі", callback_data="tasks")],
        [InlineKeyboardButton(text="◀️ Головне меню", callback_data="main_menu")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "tasks")
async def show_tasks(callback: CallbackQuery):
    async with async_session_factory() as session:
        tasks = await UploadRepo(session).list_by_owner(callback.from_user.id)

    if not tasks:
        await callback.message.edit_text("Задач ще немає.", reply_markup=main_menu())
        return

    icons = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌"}
    lines = ["<b>📋 Останні задачі</b>\n"]
    for t in tasks:
        icon = icons.get(t.status, "?")
        lines.append(f"{icon} #{t.id} — {t.status} ({t.created_at.strftime('%d.%m %H:%M')})")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="stats")]
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
