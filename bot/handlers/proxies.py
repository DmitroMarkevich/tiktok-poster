import asyncio
import json
import time

import aiohttp
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.fsm.context import FSMContext
from sqlalchemy import update

from config import SUPERADMIN_ID
from bot.states import ProxyManage
from bot.keyboards import proxies_menu_kb, proxy_account_kb, cancel_kb
from database import async_session_factory
from database.repository import AccountRepo
from database.models import Account
from tiktok.browser import _parse_proxy, create_context

router = Router()

_PROXY_CHECK_SEMAPHORE = asyncio.Semaphore(10)


async def _check_proxy(proxy_str: str, timeout: float = 12.0, attempts: int = 3) -> dict:
    """Test a proxy by fetching the exit IP through it (no browser needed —
    just a plain HTTP request). Returns {"ok": True, "ip", "latency_ms"} on
    success or {"ok": False, "error"} on failure (timeout, auth, refused, etc).

    Retries on failure: datacenter proxies flake (the same transient ERR_TUNNEL that
    `_goto_retry` exists for), and a SINGLE failed probe must not permanently reject a
    working proxy — that previously rejected a live proxy in bulk-assignment with "нічого
    не призначено". Only declares a proxy dead if EVERY attempt fails."""
    proxy_server, credentials = _parse_proxy(proxy_str)
    if not proxy_server or proxy_server.startswith(":"):
        return {"ok": False, "error": "не вдалось розпарсити адресу"}

    proxy_url = f"http://{proxy_server}"
    proxy_auth = aiohttp.BasicAuth(credentials["username"], credentials["password"]) if credentials else None
    last_err = "no response"
    for attempt in range(attempts):
        start = time.monotonic()
        try:
            async with _PROXY_CHECK_SEMAPHORE:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.ipify.org?format=json",
                        proxy=proxy_url, proxy_auth=proxy_auth,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp:
                        data = await resp.json(content_type=None)
                        latency_ms = round((time.monotonic() - start) * 1000)
                        return {"ok": True, "ip": data.get("ip"), "latency_ms": latency_ms}
        except Exception as e:
            last_err = str(e)[:70]
            if attempt < attempts - 1:
                await asyncio.sleep(1.5)
    return {"ok": False, "error": last_err}


@router.callback_query(F.data == "proxies")
async def proxies_home(callback: CallbackQuery):
    await callback.message.edit_text(
        "🌐 <b>Управління проксі</b>",
        reply_markup=proxies_menu_kb()
    )


@router.callback_query(F.data == "proxy_list")
async def proxy_list_view(callback: CallbackQuery):
    async with async_session_factory() as session:
        accs = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)

    if not accs:
        await callback.answer("Акаунтів немає.", show_alert=True)
        return

    lines = ["<b>🌐 Проксі по акаунтах:</b>\n"]
    for acc in accs:
        proxy = f"<code>{acc.proxy}</code>" if acc.proxy else "❌ немає"
        rotation = ""
        if acc.proxy_list:
            try:
                n = len(json.loads(acc.proxy_list))
                rotation = f" <i>({n} в ротації)</i>"
            except Exception:
                pass
        lines.append(f"• @{acc.username}: {proxy}{rotation}")

    await callback.message.edit_text("\n".join(lines), reply_markup=proxies_menu_kb())


@router.callback_query(F.data == "proxy_check_all")
async def proxy_check_all(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    async with async_session_factory() as session:
        accs = await AccountRepo(session).list_by_owner(SUPERADMIN_ID)
    if not accs:
        await callback.answer("Акаунтів немає.", show_alert=True)
        return

    # Collect unique proxies → which accounts/roles use each one
    proxy_users: dict[str, list] = {}
    for acc in accs:
        if acc.proxy:
            proxy_users.setdefault(acc.proxy, []).append(f"@{acc.username}")
        if acc.proxy_list:
            try:
                for p in json.loads(acc.proxy_list):
                    proxy_users.setdefault(p, []).append(f"@{acc.username}[ротація]")
            except Exception:
                pass

    if not proxy_users:
        await callback.message.edit_text("❌ Жодного проксі не призначено акаунтам.", reply_markup=proxies_menu_kb())
        return

    status_msg = await callback.message.edit_text(f"🔄 Перевіряю {len(proxy_users)} проксі...")

    proxies = list(proxy_users.keys())
    checked = await asyncio.gather(*[_check_proxy(p) for p in proxies])

    ok_count = sum(1 for r in checked if r["ok"])
    lines = [f"🌐 <b>Перевірка проксі:</b> {ok_count}/{len(proxies)} робочих\n"]
    for proxy, res in zip(proxies, checked):
        users = ", ".join(proxy_users[proxy])
        if res["ok"]:
            lines.append(f"✅ <code>{proxy}</code>\n   {res['latency_ms']}мс · IP {res['ip']} · {users}")
        else:
            lines.append(f"❌ <code>{proxy}</code>\n   {res['error']} · {users}")

    text = "\n".join(lines)
    if len(text) > 3500:
        await status_msg.edit_text(
            f"🌐 Перевірка завершена: <b>{ok_count}/{len(proxies)}</b> робочих.\nДеталі — у файлі нижче.",
            reply_markup=proxies_menu_kb()
        )
        await status_msg.answer_document(
            BufferedInputFile(text.encode("utf-8"), filename="proxy_check.txt"),
            caption="📄 Результати перевірки проксі"
        )
    else:
        await status_msg.edit_text(text, reply_markup=proxies_menu_kb())


# ── Bulk assignment from file ─────────────────────────────────────────────────

@router.callback_query(F.data == "proxy_bulk_upload")
async def proxy_bulk_upload_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProxyManage.waiting_bulk_file)
    await callback.message.edit_text(
        "📁 <b>Масове призначення проксі</b>\n\n"
        "Надішли файл або текст — по одному проксі на рядок.\n"
        "Проксі призначаються акаунтам по порядку (циклічно).\n\n"
        "Формат:\n"
        "<code>http://user:pass@host:port</code>\n"
        "або\n"
        "<code>host:port:user:pass</code>",
        reply_markup=cancel_kb()
    )


@router.message(ProxyManage.waiting_bulk_file, F.document | F.text)
async def handle_proxy_bulk(message: Message, bot: Bot, state: FSMContext):
    await state.clear()

    if message.document:
        file = await bot.get_file(message.document.file_id)
        buf = await bot.download_file(file.file_path)
        raw = buf.read().decode("utf-8", errors="ignore")
    else:
        raw = message.text.strip()

    proxies = _parse_proxy_lines(raw)
    if not proxies:
        await message.answer("❌ Не знайдено жодного проксі.", reply_markup=proxies_menu_kb())
        return

    status_msg = await message.answer(f"🔄 Перевіряю {len(proxies)} проксі перед призначенням...")
    checked = await asyncio.gather(*[_check_proxy(p) for p in proxies])
    working = [p for p, r in zip(proxies, checked) if r["ok"]]
    broken = len(proxies) - len(working)

    if not working:
        await status_msg.edit_text(
            f"❌ Жодне з {len(proxies)} проксі не пройшло перевірку — нічого не призначено.",
            reply_markup=proxies_menu_kb()
        )
        return

    async with async_session_factory() as session:
        repo = AccountRepo(session)
        accs = await repo.list_by_owner(SUPERADMIN_ID)

        if not accs:
            await status_msg.edit_text("❌ Немає акаунтів.", reply_markup=proxies_menu_kb())
            return

        for i, acc in enumerate(accs):
            proxy = working[i % len(working)]
            await session.execute(
                update(Account).where(Account.id == acc.id).values(proxy=proxy)
            )
        await session.commit()

    note = f"\n⚠️ Відсіяно непрацюючих: <b>{broken}</b>" if broken else ""
    await status_msg.edit_text(
        f"✅ Призначено проксі до <b>{len(accs)}</b> акаунтів.\n"
        f"Робочих проксі використано: <b>{len(working)}</b>/{len(proxies)}{note}",
        reply_markup=proxies_menu_kb()
    )


# ── Per-account proxy ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("account_proxy_"))
async def account_proxy_menu(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[-1])
    async with async_session_factory() as session:
        acc = await AccountRepo(session).get_by_id(account_id)

    proxy_text = f"\nПоточний: <code>{acc.proxy}</code>" if acc and acc.proxy else ""
    await callback.message.edit_text(
        f"🌐 <b>Проксі для @{acc.username}</b>{proxy_text}\n\nОбери дію:",
        reply_markup=proxy_account_kb(account_id)
    )


@router.callback_query(F.data.startswith("proxy_check_browser_"))
async def proxy_check_browser_ip(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[-1])
    await callback.answer()

    async with async_session_factory() as session:
        acc = await AccountRepo(session).get_by_id(account_id)
    if not acc or acc.owner_id != SUPERADMIN_ID:
        await callback.message.edit_text("Акаунт не знайдено.")
        return
    if not acc.proxy:
        await callback.answer("Цьому акаунту не призначено проксі.", show_alert=True)
        return

    await callback.message.edit_text(f"🔍 Перевіряю IP браузера для @{acc.username}...")

    # Baseline: server's own public IP with NO proxy — if the browser's exit IP
    # (through the assigned proxy) differs from this, the proxy is genuinely applied.
    server_ip = None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.ipify.org?format=json", timeout=aiohttp.ClientTimeout(total=10)) as r:
                server_ip = (await r.json(content_type=None)).get("ip")
    except Exception:
        pass

    pw, ctx = None, None
    try:
        pw, ctx = await create_context(acc.id, acc.proxy, acc.session_data)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=30000)
        body = await page.evaluate("() => document.body.innerText")
        browser_ip = json.loads(body).get("ip")

        if browser_ip and server_ip and browser_ip != server_ip:
            verdict = "✅ Проксі застосовано — IP браузера відрізняється від серверного."
        elif browser_ip and server_ip and browser_ip == server_ip:
            verdict = "⚠️ IP браузера збігається з серверним — проксі НЕ змінює адресу!"
        else:
            verdict = "❓ Не вдалось порівняти (один з запитів не відповів)."

        await callback.message.edit_text(
            f"🌐 <b>Перевірка IP для @{acc.username}</b>\n\n"
            f"Проксі: <code>{acc.proxy}</code>\n"
            f"IP сервера (без проксі): <code>{server_ip or '?'}</code>\n"
            f"IP браузера (через проксі): <code>{browser_ip or '?'}</code>\n\n"
            f"{verdict}",
            reply_markup=proxy_account_kb(account_id)
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Помилка під час перевірки: {e}", reply_markup=proxy_account_kb(account_id)
        )
    finally:
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


@router.callback_query(F.data.startswith("proxy_disable_"))
async def proxy_disable(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[-1])
    async with async_session_factory() as session:
        acc = await AccountRepo(session).get_by_id(account_id)
        if not acc or acc.owner_id != SUPERADMIN_ID:
            await callback.answer("Акаунт не знайдено.", show_alert=True)
            return
        # Clear proxy_list too — rotate_proxy() would otherwise refill `proxy`
        # from the saved rotation list on the next upload/comment run.
        await session.execute(
            update(Account).where(Account.id == account_id).values(
                proxy=None, proxy_list=None, proxy_index=0
            )
        )
        await session.commit()
        acc = await AccountRepo(session).get_by_id(account_id)

    await callback.answer("Проксі вимкнено.")
    await callback.message.edit_text(
        f"🌐 <b>Проксі для @{acc.username}</b>\n\nПроксі вимкнено — браузер працюватиме напряму, без проксі.\n\nОбери дію:",
        reply_markup=proxy_account_kb(account_id)
    )


@router.callback_query(F.data.startswith("proxy_set_one_"))
async def proxy_set_one_start(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split("_")[-1])
    await state.set_state(ProxyManage.waiting_proxy)
    await state.update_data(account_id=account_id)
    await callback.message.edit_text(
        "Введи проксі в одному з форматів:\n"
        "<code>http://user:pass@host:port</code>\n"
        "<code>host:port:user:pass</code>\n"
        "<code>host:port</code>\n\n"
        "або /skip щоб прибрати проксі",
        reply_markup=cancel_kb()
    )


@router.callback_query(F.data.startswith("proxy_set_list_"))
async def proxy_set_list_start(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split("_")[-1])
    await state.set_state(ProxyManage.waiting_proxy_list)
    await state.update_data(account_id=account_id)
    await callback.message.edit_text(
        "Введи список проксі для ротації (по одному на рядок):\n\n"
        "<code>http://user:pass@host1:port\n"
        "http://user:pass@host2:port</code>",
        reply_markup=cancel_kb()
    )


@router.message(ProxyManage.waiting_proxy, F.text)
async def handle_proxy_one(message: Message, state: FSMContext):
    data = await state.get_data()
    account_id = data["account_id"]
    await state.clear()

    skip = message.text.strip().lower() in ("/skip", "skip", "-", "")
    proxy = None if skip else _normalize_proxy(message.text.strip())

    async with async_session_factory() as session:
        await session.execute(
            update(Account).where(Account.id == account_id).values(proxy=proxy)
        )
        await session.commit()
        acc = await AccountRepo(session).get_by_id(account_id)

    status = f"<code>{proxy}</code>" if proxy else "прибрано"
    await message.answer(
        f"✅ Проксі для @{acc.username}: {status}",
        reply_markup=proxies_menu_kb()
    )


@router.message(ProxyManage.waiting_proxy_list, F.text)
async def handle_proxy_list(message: Message, state: FSMContext):
    data = await state.get_data()
    account_id = data["account_id"]
    await state.clear()

    proxies = _parse_proxy_lines(message.text)
    if not proxies:
        await message.answer("❌ Список порожній.", reply_markup=proxies_menu_kb())
        return

    async with async_session_factory() as session:
        repo = AccountRepo(session)
        await repo.set_proxy_list(account_id, proxies)
        acc = await repo.get_by_id(account_id)

    await message.answer(
        f"✅ Збережено <b>{len(proxies)}</b> проксі для @{acc.username}.\n"
        f"Проксі міняються після кожного завантаження.",
        reply_markup=proxies_menu_kb()
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_proxy(raw: str) -> str:
    """Canonicalise a proxy string to URL form. Thin wrapper over the single source of
    truth in browser.py (which `_parse_proxy` now also calls) so every entry path and the
    browser launch agree on the format."""
    from tiktok.browser import normalize_proxy
    return normalize_proxy(raw) or raw


def _parse_proxy_lines(raw: str) -> list:
    result = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        result.append(_normalize_proxy(line))
    return result
