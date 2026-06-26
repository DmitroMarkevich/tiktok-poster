"""'📡 Стан бота' — show where each account's bot currently is, with a fresh page
screenshot. Reads tiktok.live_state, which the commenting worker updates on every
phase. Lets the operator see live status instead of guessing."""
import os
import time

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto,
)

from tiktok import live_state

router = Router()

# Friendly labels for the internal phase keys pushed by _Progress.mark.
_PHASE_LABELS = {
    "goto+settle": "🌐 відкриває відео",
    "captcha#1": "🧩 капча (вхід)",
    "watch_video": "👀 дивиться відео",
    "open_comments": "💬 відкриває коментарі",
    "captcha#2": "🧩 капча (коментарі)",
    "read+like": "👍 читає/лайкає",
    "focus_input": "⌨️ фокус на полі",
    "type_text": "⌨️ друкує коментар",
    "submit+verify": "📤 публікує",
    "перевірка шедоубану": "🕵️ перевірка шедоубану",
}


def _label(phase: str) -> str:
    return _PHASE_LABELS.get(phase, phase or "—")


def _age(updated: float) -> str:
    if not updated:
        return ""
    s = max(0, int(time.time() - updated))
    return f"{s}с тому" if s < 90 else f"{s // 60}хв тому"


def _status_view():
    snap = live_state.snapshot()
    if not snap:
        text = "📡 <b>Стан бота</b>\n\nЗараз жоден акаунт не працює."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Оновити", callback_data="live_status")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
        ])
        return text, kb

    # Active accounts first, then most-recently-updated.
    items = sorted(snap.items(), key=lambda kv: (not kv[1].get("active"), -kv[1].get("updated", 0)))
    lines = ["📡 <b>Стан бота</b>\n"]
    shot_rows = []
    for acc_id, st in items:
        uname = st.get("username") or f"id{acc_id}"
        dot = "🟢" if st.get("active") else "⚪️"
        vid = (st.get("video_url") or "").rsplit("/", 1)[-1]
        vid_part = f" · 🎬 …{vid[-6:]}" if vid else ""
        lines.append(f"{dot} <b>@{uname}</b> — {_label(st.get('phase'))}{vid_part} · {_age(st.get('updated'))}")
        if st.get("screenshot") and os.path.exists(st["screenshot"]):
            shot_rows.append([InlineKeyboardButton(text=f"📷 @{uname}", callback_data=f"live_shot_{acc_id}")])

    kb_rows = shot_rows + [
        [InlineKeyboardButton(text="🔄 Оновити", callback_data="live_status")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "live_status")
async def show_status(callback: CallbackQuery):
    text, kb = _status_view()
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        # edit fails if the message is a photo or unchanged — send fresh instead.
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


def _shot_kb(acc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Оновити", callback_data=f"live_shot_{acc_id}"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="live_status"),
    ]])


@router.callback_query(F.data.startswith("live_shot_"))
async def send_screenshot(callback: CallbackQuery):
    acc_id = int(callback.data.rsplit("_", 1)[-1])
    st = live_state.get(acc_id)
    shot = st.get("screenshot") if st else None
    if not shot or not os.path.exists(shot):
        await callback.answer("Скрін ще не готовий", show_alert=True)
        return
    uname = (st.get("username") if st else None) or f"id{acc_id}"
    caption = f"📷 @{uname} — {_label(st.get('phase'))} · {_age(st.get('updated'))}"
    kb = _shot_kb(acc_id)
    photo = FSInputFile(shot)
    # If this came from the 🔄 on an existing screenshot message, update that message in
    # place (no stacking). Otherwise (first open from the status list) send a new photo.
    if callback.message.photo:
        try:
            await callback.message.edit_media(
                InputMediaPhoto(media=photo, caption=caption), reply_markup=kb
            )
            await callback.answer("Оновлено")
            return
        except Exception:
            await callback.answer("Без змін")  # identical image / too fast
            return
    try:
        await callback.message.answer_photo(photo, caption=caption, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"Не вдалося надіслати скрін: {e}", show_alert=True)
        return
    await callback.answer()
