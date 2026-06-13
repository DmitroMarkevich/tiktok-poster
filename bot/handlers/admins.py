from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.keyboards import admins_menu_kb, admins_list_kb, back_main_kb
from bot.states import AdminManage
from database import async_session_factory
from database.repository import BotUserRepo

router = Router()


@router.callback_query(F.data == "admins")
async def admins_menu(callback: CallbackQuery, user_role: str = "admin"):
    if user_role != "superadmin":
        await callback.answer("⛔ Тільки для суперадміна.", show_alert=True)
        return
    await callback.message.edit_text("👑 Управління адмінами:", reply_markup=admins_menu_kb())


@router.callback_query(F.data == "admin_list")
async def admin_list(callback: CallbackQuery, user_role: str = "admin"):
    if user_role != "superadmin":
        await callback.answer("⛔ Тільки для суперадміна.", show_alert=True)
        return
    async with async_session_factory() as session:
        admins = await BotUserRepo(session).list_admins()
    if not admins:
        await callback.message.edit_text(
            "Список адмінів порожній.",
            reply_markup=admins_menu_kb()
        )
        return
    await callback.message.edit_text(
        f"Адміни ({len(admins)}):",
        reply_markup=admins_list_kb(admins)
    )


@router.callback_query(F.data == "admin_add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext, user_role: str = "admin"):
    if user_role != "superadmin":
        await callback.answer("⛔ Тільки для суперадміна.", show_alert=True)
        return
    await state.set_state(AdminManage.waiting_user_id)
    await callback.message.edit_text(
        "Введіть Telegram User ID нового адміна\n(число, напр. <code>123456789</code>):",
        reply_markup=back_main_kb()
    )


@router.message(AdminManage.waiting_user_id, F.text)
async def admin_add_handle(message: Message, state: FSMContext, user_role: str = "admin"):
    if user_role != "superadmin":
        await message.answer("⛔ Тільки для суперадміна.")
        await state.clear()
        return

    text = message.text.strip() if message.text else ""
    if not text.lstrip("-").isdigit():
        await message.answer("❌ Невірний формат. Введіть числовий User ID:")
        return

    new_id = int(text)
    async with async_session_factory() as session:
        repo = BotUserRepo(session)
        existing = await repo.get(new_id)
        if existing:
            await message.answer(
                f"Цей користувач вже є адміном ({existing.role}).",
                reply_markup=admins_menu_kb()
            )
            await state.clear()
            return
        user_info = message.from_user
        await repo.add(
            user_id=new_id,
            username=None,
            first_name=None,
            role="admin",
            added_by=user_info.id,
        )

    await state.clear()
    await message.answer(
        f"✅ Користувач <code>{new_id}</code> доданий як адмін.",
        reply_markup=admins_menu_kb()
    )


@router.callback_query(F.data.startswith("admin_del_"))
async def admin_delete(callback: CallbackQuery, user_role: str = "admin"):
    if user_role != "superadmin":
        await callback.answer("⛔ Тільки для суперадміна.", show_alert=True)
        return

    target_id = int(callback.data.removeprefix("admin_del_"))
    async with async_session_factory() as session:
        await BotUserRepo(session).delete(target_id)

    await callback.answer(f"Видалено {target_id}")
    async with async_session_factory() as session:
        admins = await BotUserRepo(session).list_admins()
    if not admins:
        await callback.message.edit_text("Список адмінів порожній.", reply_markup=admins_menu_kb())
    else:
        await callback.message.edit_text(f"Адміни ({len(admins)}):", reply_markup=admins_list_kb(admins))
