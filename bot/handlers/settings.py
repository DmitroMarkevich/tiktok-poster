import os
import re
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.states import CaptchaSettings
from bot.keyboards import settings_menu_kb, captcha_service_kb, cancel_kb, main_menu

router = Router()


@router.callback_query(F.data == "settings")
async def settings_home(callback: CallbackQuery):
    await callback.message.edit_text(
        "⚙️ <b>Налаштування</b>",
        reply_markup=settings_menu_kb()
    )


@router.callback_query(F.data == "captcha_settings")
async def captcha_settings(callback: CallbackQuery):
    import config
    service = config.CAPTCHA_SERVICE
    key = config.CAPTCHA_API_KEY
    status = f"Сервіс: <b>{service}</b>\nAPI-ключ: {'✅ встановлено' if key else '❌ не встановлено'}"
    await callback.message.edit_text(
        f"⚙️ <b>CAPTCHA</b>\n\n{status}\n\nОбери сервіс:",
        reply_markup=captcha_service_kb()
    )


@router.callback_query(F.data.startswith("captcha_svc_"))
async def choose_captcha_service(callback: CallbackQuery, state: FSMContext):
    service = callback.data.split("captcha_svc_")[1]
    await state.set_state(CaptchaSettings.waiting_api_key)
    await state.update_data(service=service)

    links = {
        "2captcha": "2captcha.com → Balance → API Key",
        "capsolver": "capsolver.com → Dashboard → API Key",
    }
    hint = links.get(service, "")
    await callback.message.edit_text(
        f"Обрано: <b>{service}</b>\n\nВведи API-ключ ({hint}):",
        reply_markup=cancel_kb()
    )


@router.message(CaptchaSettings.waiting_api_key, F.text)
async def got_captcha_api_key(message: Message, state: FSMContext):
    data = await state.get_data()
    service = data["service"]
    api_key = message.text.strip()
    await state.clear()

    env_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    )

    with open(env_path, "r") as f:
        content = f.read()

    def _set_var(text: str, name: str, value: str) -> str:
        pattern = rf"^{name}=.*$"
        replacement = f"{name}={value}"
        if re.search(pattern, text, re.MULTILINE):
            return re.sub(pattern, replacement, text, flags=re.MULTILINE)
        return text.rstrip("\n") + f"\n{replacement}\n"

    content = _set_var(content, "CAPTCHA_SERVICE", service)
    content = _set_var(content, "CAPTCHA_API_KEY", api_key)

    with open(env_path, "w") as f:
        f.write(content)

    import config
    config.CAPTCHA_SERVICE = service
    config.CAPTCHA_API_KEY = api_key

    await message.answer(
        f"✅ <b>{service}</b> налаштовано!",
        reply_markup=settings_menu_kb()
    )
