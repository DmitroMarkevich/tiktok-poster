from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from config import SUPERADMIN_ID
from database import async_session_factory
from database.repository import BotUserRepo


class RoleMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        if SUPERADMIN_ID and user.id == SUPERADMIN_ID:
            data["user_role"] = "superadmin"
            return await handler(event, data)

        async with async_session_factory() as session:
            bot_user = await BotUserRepo(session).get(user.id)

        if not bot_user:
            if isinstance(event, Message):
                await event.answer("⛔ Немає доступу. Зверніться до адміністратора.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Немає доступу.", show_alert=True)
            return

        data["user_role"] = bot_user.role
        return await handler(event, data)
