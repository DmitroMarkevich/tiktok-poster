import os
import asyncio
import random
from bot.bg import spawn
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from utils.video import fingerprint_video, make_slideshow

from bot.states import UploadVideo
from bot.keyboards import (
    upload_menu_kb, select_accounts_kb, choose_account_kb,
    privacy_kb, cancel_kb, main_menu,
    media_choice_kb, photos_done_kb,
)
from database import async_session_factory
from database.repository import AccountRepo, UploadRepo
from tiktok.browser import create_context, save_session
from tiktok.uploader import upload_video
from config import VIDEOS_DIR, SUPERADMIN_ID

router = Router()

_UPLOAD_SEMAPHORE = asyncio.Semaphore(3)
# Per-user lock serialising carousel photo collection so an album (many messages at once)
# can't race the image_paths read-append-write and drop photos.
_photo_locks: dict = {}


# ── Menu entry ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "upload_menu")
async def upload_menu_view(callback: CallbackQuery):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    await callback.message.edit_text(
        "📤 <b>Завантажити відео</b>\n\nОбери режим:",
        reply_markup=upload_menu_kb(len(accounts))
    )


# ── Mode selection ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "upload_all")
async def upload_all(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    if not accounts:
        await callback.message.edit_text("Немає активних акаунтів.", reply_markup=upload_menu_kb(0))
        return
    await state.update_data(bulk=True, account_ids=[a.id for a in accounts])
    await _ask_media_type(callback.message, state, len(accounts))


@router.callback_query(F.data == "upload_one")
async def upload_one(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    if not accounts:
        await callback.message.edit_text("Немає активних акаунтів.", reply_markup=upload_menu_kb(0))
        return
    await state.update_data(bulk=False)
    await state.set_state(UploadVideo.choose_account)
    await callback.message.edit_text(
        "Обери акаунт:", reply_markup=choose_account_kb(accounts, "upload_acc")
    )


@router.callback_query(F.data == "upload_select")
async def upload_select(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    if not accounts:
        await callback.answer("Немає активних акаунтів.", show_alert=True)
        return
    await state.set_state(UploadVideo.select_accounts)
    await state.update_data(bulk=True, selected_ids=[])
    await callback.message.edit_text(
        "☑️ Вибери акаунти для завантаження:",
        reply_markup=select_accounts_kb(accounts, set())
    )


# ── Account multi-select ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("toggle_acc_"), UploadVideo.select_accounts)
async def toggle_account(callback: CallbackQuery, state: FSMContext):
    toggled_id = int(callback.data.split("_")[-1])
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    selected.discard(toggled_id) if toggled_id in selected else selected.add(toggled_id)
    await state.update_data(selected_ids=list(selected))

    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    await callback.message.edit_reply_markup(reply_markup=select_accounts_kb(accounts, selected))
    await callback.answer()


@router.callback_query(F.data == "select_all_accs", UploadVideo.select_accounts)
async def select_all(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    selected = {a.id for a in accounts}
    await state.update_data(selected_ids=list(selected))
    await callback.message.edit_reply_markup(reply_markup=select_accounts_kb(accounts, selected))
    await callback.answer()


@router.callback_query(F.data == "deselect_all_accs", UploadVideo.select_accounts)
async def deselect_all(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        accounts = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    await state.update_data(selected_ids=[])
    await callback.message.edit_reply_markup(reply_markup=select_accounts_kb(accounts, set()))
    await callback.answer()


@router.callback_query(F.data == "upload_selected_confirm", UploadVideo.select_accounts)
async def confirm_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_ids", [])
    if not selected:
        await callback.answer("Обери хоча б один акаунт!", show_alert=True)
        return
    await state.update_data(account_ids=selected)
    await _ask_media_type(callback.message, state, len(selected))


# ── Single-account selection ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("upload_acc_"), UploadVideo.choose_account)
async def chose_account(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split("_")[-1])
    await state.update_data(account_id=account_id, bulk=False)
    await _ask_media_type(callback.message, state, 1)


# ── Media type: video vs photo carousel ───────────────────────────────────────

async def _ask_media_type(message: Message, state: FSMContext, n_accounts: int):
    await state.update_data(n_accounts=n_accounts)
    await state.set_state(UploadVideo.choose_media)
    await message.edit_text(
        f"📦 Ціль: <b>{n_accounts}</b> акаунт(ів).\n\nЩо публікуємо?",
        reply_markup=media_choice_kb(),
    )


@router.callback_query(F.data == "media_video", UploadVideo.choose_media)
async def media_video(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(media_type="video")
    await state.set_state(UploadVideo.send_video)
    await callback.message.edit_text("Надішли відеофайл (.mp4):", reply_markup=cancel_kb())


@router.callback_query(F.data == "media_carousel", UploadVideo.choose_media)
async def media_carousel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    await state.update_data(media_type="carousel")
    await _start_photo_collection(callback.message, state, data.get("n_accounts", 1))


# ── Photo carousel: collect images ────────────────────────────────────────────

async def _start_photo_collection(message: Message, state: FSMContext, n_accounts: int):
    # Keep ONE control message and just edit its counter as photos arrive — instead of
    # spamming a fresh message+keyboard per photo (ugly for a 10-photo album).
    await state.set_state(UploadVideo.send_photos)
    await message.edit_text(
        f"🖼 Карусель на <b>{n_accounts}</b> акаунт(ів).\n\n"
        "Надсилай фото — <b>по одному або пачкою (альбомом)</b>, 2–35 шт. "
        "Коли все — тисни «✅ Готово» (один раз).\n\n"
        "Зібрано: <b>0</b> фото",
        reply_markup=photos_done_kb(0),
    )
    await state.update_data(
        image_paths=[], n_accounts=n_accounts,
        control_chat_id=message.chat.id, control_msg_id=message.message_id,
    )


@router.message(UploadVideo.send_photos, F.photo | F.document)
async def got_photo(message: Message, state: FSMContext, bot: Bot):
    # Works for BOTH a single photo and a batch (album): Telegram delivers every album
    # item as its own F.photo message. Those arrive ~simultaneously and aiogram processes
    # updates concurrently, so the image_paths read-append-write is serialised under a
    # per-user lock — otherwise concurrent appends race and silently drop photos.
    if message.photo:
        file_id = message.photo[-1].file_id  # largest size variant
        ext = "jpg"
    else:
        doc = message.document
        if not (doc.mime_type or "").startswith("image/"):
            await message.answer("Це не зображення. Надішли фото.")
            return
        file_id = doc.file_id
        ext = (doc.file_name or "img.jpg").rsplit(".", 1)[-1][:5] or "jpg"

    # Download first (slow, no shared state). message_id is unique per message → no
    # filename collision even when a whole album lands at once.
    file_name = f"car_{message.from_user.id}_{message.message_id}.{ext}"
    file_path = os.path.join(VIDEOS_DIR, file_name)
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, destination=file_path)

    lock = _photo_locks.setdefault(message.from_user.id, asyncio.Lock())
    async with lock:
        data = await state.get_data()
        paths = list(data.get("image_paths", []))
        if len(paths) >= 35:
            try:
                os.remove(file_path)
            except OSError:
                pass
            await message.answer("Максимум 35 фото в каруселі — решту пропускаю.")
            return
        paths.append(file_path)
        # Album photos arrive as separate messages and are downloaded concurrently BEFORE
        # this lock, so append order = whichever finished downloading first, NOT the order
        # the user sent them. Carousel order is critical, so re-sort by the message_id baked
        # into the filename (car_<user>_<message_id>.ext) — it increases with send order.
        paths.sort(key=_carousel_msg_id)
        await state.update_data(image_paths=paths)
        count = len(paths)
        n_accounts = data.get("n_accounts", 1)
        chat_id = data.get("control_chat_id")
        msg_id = data.get("control_msg_id")

    # Update the single control message's counter instead of sending a new message.
    if chat_id and msg_id:
        try:
            await bot.edit_message_text(
                f"🖼 Карусель на <b>{n_accounts}</b> акаунт(ів).\n\n"
                "Надсилай фото — <b>по одному або пачкою (альбомом)</b>, 2–35 шт. "
                "Коли все — тисни «✅ Готово» (один раз).\n\n"
                f"Зібрано: <b>{count}</b> фото",
                chat_id=chat_id, message_id=msg_id,
                reply_markup=photos_done_kb(count),
            )
            return
        except Exception:
            pass  # control message gone/uneditable → fall back to a light ack
    await message.answer(f"➕ Зібрано {count} фото.", reply_markup=photos_done_kb(count))


@router.callback_query(F.data == "carousel_done", UploadVideo.send_photos)
async def carousel_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    paths = data.get("image_paths", [])
    if len(paths) < 2:
        await callback.answer("Карусель потребує щонайменше 2 фото.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(UploadVideo.caption)
    await callback.message.edit_text(
        f"🖼 {len(paths)} фото зібрано.\nВведи підпис (або /skip):", reply_markup=cancel_kb()
    )


# ── Common upload flow ────────────────────────────────────────────────────────

@router.message(UploadVideo.send_video, F.video | F.document)
async def got_video(message: Message, state: FSMContext, bot: Bot):
    file_id = message.video.file_id if message.video else message.document.file_id
    file_name = f"{message.from_user.id}_{message.message_id}.mp4"
    file_path = os.path.join(VIDEOS_DIR, file_name)

    await message.answer("⏳ Завантажую файл...")
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, destination=file_path)

    await state.update_data(video_path=file_path)
    await state.set_state(UploadVideo.caption)
    await message.answer("Введи підпис до відео (або /skip):", reply_markup=cancel_kb())


@router.message(UploadVideo.caption, F.text)
async def got_caption(message: Message, state: FSMContext):
    caption = "" if message.text.strip().lower() in ("skip", "-") else message.text.strip()
    await state.update_data(caption=caption)
    await state.set_state(UploadVideo.hashtags)
    await message.answer(
        "Введи хештеги через пробіл (або /skip):\n<code>fyp viral dance</code>",
        reply_markup=cancel_kb()
    )


@router.message(UploadVideo.hashtags, F.text)
async def got_hashtags(message: Message, state: FSMContext):
    hashtags = "" if message.text.strip().lower() in ("skip", "-") else message.text.strip()
    await state.update_data(hashtags=hashtags)
    await state.set_state(UploadVideo.privacy)
    await message.answer("Оберіть приватність:", reply_markup=privacy_kb())


@router.callback_query(F.data.startswith("privacy_"), UploadVideo.privacy)
async def got_privacy(callback: CallbackQuery, state: FSMContext):
    privacy = callback.data.split("_")[1]
    data = await state.get_data()
    await state.clear()

    is_bulk = data.get("bulk", False)
    is_carousel = data.get("media_type") == "carousel"

    if is_carousel:
        image_paths = data.get("image_paths", [])
        caption = data.get("caption", "")
        hashtags = data.get("hashtags", "")
        # The web has no photo upload, so we render the photos into a flip-through
        # slideshow video and publish it through the normal (working) video flow.
        await callback.message.edit_text(f"🎞 Унікалізую фото й збираю слайдшоу ({len(image_paths)})...")
        try:
            # Uniquification (geometry/colour/noise + randomized timing/transitions/audio)
            # happens inside make_slideshow in a single ffmpeg pass, so TikTok's dedup
            # doesn't flag re-used images. No separate per-image re-encode needed.
            video_path = await make_slideshow(image_paths)
        except Exception as e:
            _cleanup_images(image_paths)
            await callback.message.edit_text(
                f"❌ Не вдалося зібрати слайдшоу: {e}", reply_markup=main_menu()
            )
            return
        _cleanup_images(image_paths)
        # From here on it's an ordinary video upload — reuse the proven runners.
        if is_bulk:
            account_ids = data.get("account_ids", [])
            msg = await callback.message.edit_text(
                f"⏳ Публікую слайдшоу на <b>{len(account_ids)}</b> акаунтів..."
            )
            spawn(_run_bulk_upload(
                msg, owner_id=callback.from_user.id, account_ids=account_ids,
                video_path=video_path, caption=caption, hashtags=hashtags, privacy=privacy,
            ))
        else:
            await callback.message.edit_text("⏳ Публікую слайдшоу, зачекай...")
            async with async_session_factory() as session:
                task = await UploadRepo(session).create(
                    account_id=data["account_id"], owner_id=callback.from_user.id,
                    video_path=video_path, caption=caption, hashtags=hashtags, privacy=privacy,
                )
                acc = await AccountRepo(session).get_by_id(data["account_id"])
            spawn(_run_upload(
                callback, task.id, acc, video_path, caption, hashtags, privacy
            ))
        return

    if is_bulk:
        account_ids = data.get("account_ids", [])
        msg = await callback.message.edit_text(
            f"⏳ Запускаю завантаження на <b>{len(account_ids)}</b> акаунтів..."
        )
        spawn(_run_bulk_upload(
            msg,
            owner_id=callback.from_user.id,
            account_ids=account_ids,
            video_path=data["video_path"],
            caption=data.get("caption", ""),
            hashtags=data.get("hashtags", ""),
            privacy=privacy,
        ))
    else:
        await callback.message.edit_text("⏳ Публікую відео, зачекай...")
        async with async_session_factory() as session:
            task = await UploadRepo(session).create(
                account_id=data["account_id"],
                owner_id=callback.from_user.id,
                video_path=data["video_path"],
                caption=data.get("caption", ""),
                hashtags=data.get("hashtags", ""),
                privacy=privacy,
            )
            acc = await AccountRepo(session).get_by_id(data["account_id"])
        spawn(_run_upload(
            callback, task.id, acc, data["video_path"],
            data.get("caption", ""), data.get("hashtags", ""), privacy
        ))


# ── Upload runners ────────────────────────────────────────────────────────────

async def _run_upload(callback: CallbackQuery, task_id: int, acc,
                      video_path: str, caption: str, hashtags: str, privacy: str):
    # Hold the per-account browser lock so a health/session check or warmup can't open a
    # second Chrome on this profile mid-upload (SingletonLock collision / session corruption).
    from tiktok.locks import async_account_session
    pw, ctx = None, None
    async with async_account_session(acc.id):
        try:
            async with async_session_factory() as session:
                await UploadRepo(session).set_status(task_id, "running")

            async with async_session_factory() as session:
                active_proxy = await AccountRepo(session).rotate_proxy(acc.id)

            fp_path = await fingerprint_video(video_path, acc.id)
            pw, ctx = await create_context(acc.id, active_proxy or acc.proxy, acc.session_data)
            await upload_video(ctx, fp_path, caption, hashtags, privacy)

            async with async_session_factory() as session:
                await UploadRepo(session).set_status(task_id, "done")

            session_data = await save_session(ctx)
            async with async_session_factory() as session:
                await AccountRepo(session).update_session(acc.id, session_data)

            await callback.message.edit_text(
                f"✅ Відео опубліковано з @{acc.username}!", reply_markup=main_menu()
            )
        except Exception as e:
            async with async_session_factory() as session:
                await UploadRepo(session).set_status(task_id, "failed", str(e))
            await callback.message.edit_text(
                f"❌ Помилка (@{acc.username}): {e}", reply_markup=main_menu()
            )
        finally:
            if ctx: await ctx.close()
            if pw:  await pw.stop()
            try: os.remove(video_path)
            except OSError: pass


async def _run_bulk_upload(status_msg, owner_id: int, account_ids: list,
                           video_path: str, caption: str, hashtags: str, privacy: str):
    results = {"ok": [], "fail": []}
    total = len(account_ids)
    completed = 0

    async def _one(acc_id: int, pre_delay: float):
        nonlocal completed
        await asyncio.sleep(pre_delay)
        async with _UPLOAD_SEMAPHORE:
            async with async_session_factory() as session:
                acc = await AccountRepo(session).get_by_id(acc_id)
            if not acc:
                results["fail"].append(f"id={acc_id}: not found")
                return

            # Per-account browser lock (shared with health/comment/warmup) so nothing opens a
            # second Chrome on this profile mid-upload. Acquired off-loop via to_thread.
            from tiktok.locks import get_account_lock
            _lock = get_account_lock(acc_id)
            await asyncio.to_thread(_lock.acquire)
            pw, ctx = None, None
            fp_path = video_path
            try:
                async with async_session_factory() as session:
                    active_proxy = await AccountRepo(session).rotate_proxy(acc_id)

                fp_path = await fingerprint_video(video_path, acc_id)
                pw, ctx = await create_context(acc_id, active_proxy or acc.proxy, acc.session_data)
                await upload_video(ctx, fp_path, caption, hashtags, privacy)

                session_data = await save_session(ctx)
                async with async_session_factory() as session:
                    await AccountRepo(session).update_session(acc_id, session_data)

                results["ok"].append(acc.username)
                await asyncio.sleep(random.uniform(30, 90))
            except Exception as e:
                results["fail"].append(f"@{acc.username}: {str(e)[:60]}")
            finally:
                if ctx: await ctx.close()
                if pw:  await pw.stop()
                _lock.release()
                if fp_path != video_path:
                    try: os.remove(fp_path)
                    except OSError: pass
                completed += 1
                try:
                    await status_msg.edit_text(
                        f"⏳ Прогрес: <b>{completed}/{total}</b>\n"
                        f"✅ {len(results['ok'])}  ❌ {len(results['fail'])}"
                    )
                except Exception:
                    pass

    await asyncio.gather(*[
        _one(aid, i * random.uniform(20, 50))
        for i, aid in enumerate(account_ids)
    ])

    try: os.remove(video_path)
    except OSError: pass

    lines = [f"📊 Bulk upload завершено (<b>{total}</b> акаунтів):\n"]
    if results["ok"]:
        lines.append(f"✅ Успішно ({len(results['ok'])}):")
        lines += [f"  • @{u}" for u in results["ok"]]
    if results["fail"]:
        lines.append(f"\n❌ Помилки ({len(results['fail'])}):")
        lines += [f"  • {e}" for e in results["fail"]]

    await status_msg.edit_text("\n".join(lines), reply_markup=main_menu())


# ── Photo carousel helpers ────────────────────────────────────────────────────

def _cleanup_images(paths: list):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _carousel_msg_id(path: str) -> int:
    """Extract the Telegram message_id from a carousel filename (car_<user>_<mid>.<ext>),
    used to restore the user's original send order. Falls back to 0 (keeps relative order
    for any unexpected name) so sorting never raises."""
    try:
        stem = os.path.basename(path).rsplit(".", 1)[0]
        return int(stem.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        return 0
