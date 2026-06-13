import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from config import BOT_TOKEN
from database import init_db
from bot.handlers import main_router
from bot.middlewares import RoleMiddleware


async def main():
    logging.basicConfig(level=logging.INFO)

    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.update.outer_middleware(RoleMiddleware())
    dp.include_router(main_router)

    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Головне меню"),
    ])

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
