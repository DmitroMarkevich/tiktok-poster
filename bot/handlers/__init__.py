from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext

from bot.keyboards import main_menu
from .accounts import router as accounts_router
from .upload import router as upload_router
from .comments import router as comments_router
from .proxies import router as proxies_router
from .stats import router as stats_router
from .settings import router as settings_router
from .admins import router as admins_router
from .warmup import router as warmup_router
from .autopilot import router as autopilot_router
from .status import router as status_router
from .outlook import router as outlook_router

main_router = Router()

main_router.include_router(admins_router)
main_router.include_router(accounts_router)
main_router.include_router(upload_router)
main_router.include_router(comments_router)
main_router.include_router(proxies_router)
main_router.include_router(stats_router)
main_router.include_router(settings_router)
main_router.include_router(warmup_router)
main_router.include_router(autopilot_router)
main_router.include_router(status_router)
main_router.include_router(outlook_router)


@main_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user_role: str = "admin"):
    await state.clear()
    await message.answer(
        f"Привіт, <b>{message.from_user.first_name}</b>! 👋\n\nОбери дію:",
        reply_markup=main_menu(is_superadmin=(user_role == "superadmin"))
    )


@main_router.callback_query(F.data == "main_menu")
async def go_main_menu(callback: CallbackQuery, state: FSMContext, user_role: str = "admin"):
    await state.clear()
    await callback.message.edit_text(
        "Головне меню:",
        reply_markup=main_menu(is_superadmin=(user_role == "superadmin"))
    )
