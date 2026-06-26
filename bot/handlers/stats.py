from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import SUPERADMIN_ID
from bot.keyboards import back_main_kb, main_menu
from database import async_session_factory
from database.repository import AccountRepo, UploadRepo, AnalyticsRepo
from utils.ai import generate_analytics_report

router = Router()


@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    async with async_session_factory() as session:
        accs = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
        s = await UploadRepo(session).get_stats(callback.from_user.id)
        cs = await AnalyticsRepo(session).comment_summary(SUPERADMIN_ID)

    total_accs = len(accs)
    with_session = sum(1 for a in accs if a.session_data)
    with_proxy = sum(1 for a in accs if a.proxy)
    warmed = sum(1 for a in accs if a.last_warmup_at)

    text = (
        f"<b>📊 Статистика</b>\n\n"
        f"<b>Акаунти</b>\n"
        f"✅ Активних: <b>{total_accs}</b>  |  🔑 Сесія: <b>{with_session}</b>  |  🌐 Проксі: <b>{with_proxy}</b>  |  🔥 Прогріто: <b>{warmed}</b>\n\n"
        f"<b>Завантаження</b>\n"
        f"📤 Всього: <b>{s['total']}</b>  |  📅 Сьогодні: <b>{s['today']}</b>  |  ✅ <b>{s['success']}</b>  ❌ <b>{s['failed']}</b>\n\n"
        f"<b>Коментарі</b>\n"
        f"💬 Всього: <b>{cs['total']}</b>  |  📅 Сьогодні: <b>{cs['today']}</b>  |  🗓 За тиждень: <b>{cs['week']}</b>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 По акаунтах", callback_data="stats_accounts")],
        [InlineKeyboardButton(text="🤖 AI-звіт", callback_data="stats_ai")],
        [InlineKeyboardButton(text="📋 Останні задачі", callback_data="tasks")],
        [InlineKeyboardButton(text="◀️ Головне меню", callback_data="main_menu")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


def _fmt_dashboard(rows: list) -> str:
    if not rows:
        return "<b>👥 По акаунтах</b>\n\nДаних ще немає."
    lines = ["<b>👥 Метрики по акаунтах</b>\n"]
    for d in rows:
        warm = d["last_warmup"].strftime("%d.%m") if d["last_warmup"] else "—"
        flags = ("🔑" if d["has_session"] else "·") + ("🌐" if d["has_proxy"] else "·")
        lines.append(
            f"<b>@{d['username']}</b> {flags}\n"
            f"  📤 {d['uploads']}  💬 {d['comments']}  🔥 {d['warmups']} (ост. {warm})"
        )
    return "\n".join(lines)


@router.callback_query(F.data == "stats_accounts")
async def show_account_dashboard(callback: CallbackQuery):
    async with async_session_factory() as session:
        rows = await AnalyticsRepo(session).account_dashboard(SUPERADMIN_ID)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="stats")]
    ])
    await callback.message.edit_text(_fmt_dashboard(rows), reply_markup=kb)


@router.callback_query(F.data == "stats_ai")
async def show_ai_report(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("🤖 Готую AI-звіт...")

    async with async_session_factory() as session:
        rows = await AnalyticsRepo(session).account_dashboard(SUPERADMIN_ID)
        cs = await AnalyticsRepo(session).comment_summary(SUPERADMIN_ID)

    summary_lines = [
        f"Коментарів: всього {cs['total']}, сьогодні {cs['today']}, тиждень {cs['week']}.",
        f"Акаунтів: {len(rows)}.",
    ]
    for d in rows:
        summary_lines.append(
            f"@{d['username']}: uploads={d['uploads']}, comments={d['comments']}, "
            f"warmups={d['warmups']}, proxy={'так' if d['has_proxy'] else 'ні'}, "
            f"session={'так' if d['has_session'] else 'ні'}"
        )
    report = await generate_analytics_report("\n".join(summary_lines))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="stats")]
    ])
    if not report:
        await callback.message.edit_text(
            "🤖 AI-звіт недоступний (не задано GEMINI_API_KEY або помилка API).",
            reply_markup=kb,
        )
        return
    await callback.message.edit_text(f"<b>🤖 AI-звіт</b>\n\n{report}", reply_markup=kb)


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
