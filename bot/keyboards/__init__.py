from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def main_menu(is_superadmin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="📤 Завантажити", callback_data="upload_menu"),
            InlineKeyboardButton(text="💬 Коментувати", callback_data="comment"),
        ],
        [
            InlineKeyboardButton(text="👥 Акаунти", callback_data="accounts"),
            InlineKeyboardButton(text="🌐 Проксі", callback_data="proxies"),
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
            InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings"),
        ],
    ]
    if is_superadmin:
        rows.append([InlineKeyboardButton(text="👑 Адміни", callback_data="admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admins_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_list")],
        [InlineKeyboardButton(text="➕ Додати", callback_data="admin_add")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])


def admins_list_kb(admins: list) -> InlineKeyboardMarkup:
    rows = []
    for u in admins:
        label = f"@{u.username}" if u.username else u.first_name or str(u.user_id)
        label += f" ({u.user_id})"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"admin_info_{u.user_id}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"admin_del_{u.user_id}"),
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Головне меню", callback_data="main_menu")]
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="main_menu")]
    ])


# ── Upload ────────────────────────────────────────────────────────────────────

def upload_menu_kb(active_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📤📤 На всі акаунти ({active_count})",
            callback_data="upload_all"
        )],
        [InlineKeyboardButton(text="☑️ Вибрати акаунти", callback_data="upload_select")],
        [InlineKeyboardButton(text="📤 Один акаунт", callback_data="upload_one")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])


def select_accounts_kb(accounts: list, selected_ids: set) -> InlineKeyboardMarkup:
    rows = []
    for acc in accounts:
        icon = "✅" if acc.id in selected_ids else "☐"
        rows.append([InlineKeyboardButton(
            text=f"{icon} @{acc.username}",
            callback_data=f"toggle_acc_{acc.id}"
        )])
    rows.append([
        InlineKeyboardButton(text="✅ Всі", callback_data="select_all_accs"),
        InlineKeyboardButton(text="◻️ Скинути", callback_data="deselect_all_accs"),
    ])
    n = len(selected_ids)
    rows.append([InlineKeyboardButton(
        text=f"➡️ Далі ({n} обрано)" if n else "➡️ Далі",
        callback_data="upload_selected_confirm"
    )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="upload_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def comment_menu_kb(active_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💬💬 На всі акаунти ({active_count})",
            callback_data="cmt_all"
        )],
        [InlineKeyboardButton(text="☑️ Вибрати акаунти", callback_data="cmt_select")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])


def select_cmt_accounts_kb(accounts: list, selected_ids: set) -> InlineKeyboardMarkup:
    rows = []
    for acc in accounts:
        icon = "✅" if acc.id in selected_ids else "☐"
        rows.append([InlineKeyboardButton(
            text=f"{icon} @{acc.username}",
            callback_data=f"toggle_cmt_acc_{acc.id}"
        )])
    rows.append([
        InlineKeyboardButton(text="✅ Всі", callback_data="select_all_cmt_accs"),
        InlineKeyboardButton(text="◻️ Скинути", callback_data="deselect_all_cmt_accs"),
    ])
    n = len(selected_ids)
    rows.append([InlineKeyboardButton(
        text=f"➡️ Далі ({n} обрано)" if n else "➡️ Далі",
        callback_data="cmt_selected_confirm"
    )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="comment")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def privacy_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Публічне", callback_data="privacy_public")],
        [InlineKeyboardButton(text="👥 Друзі", callback_data="privacy_friends")],
        [InlineKeyboardButton(text="🔒 Приватне", callback_data="privacy_private")],
    ])


# ── Accounts ──────────────────────────────────────────────────────────────────

def accounts_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Додати акаунт (cookies)", callback_data="account_add")],
        [InlineKeyboardButton(text="✅✅ Перевірити всі сесії", callback_data="account_check_all")],
        [InlineKeyboardButton(text="📋 Список акаунтів", callback_data="account_list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])


def accounts_list_kb(accounts: list) -> InlineKeyboardMarkup:
    rows = []
    for acc in accounts:
        sess = "🔑" if acc.session_data else "❌"
        prx  = " 🌐" if acc.proxy else ""
        rows.append([InlineKeyboardButton(
            text=f"{sess} @{acc.username}{prx}  ·  {acc.upload_count or 0} upl",
            callback_data=f"account_view_{acc.id}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="accounts")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_actions_kb(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔑 Перевірити сесію", callback_data=f"account_check_{account_id}"),
            InlineKeyboardButton(text="🍪 Оновити cookies", callback_data=f"account_cookies_{account_id}"),
        ],
        [
            InlineKeyboardButton(text="🌐 Проксі", callback_data=f"account_proxy_{account_id}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"account_delete_{account_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="account_list")],
    ])


def choose_account_kb(accounts: list, action: str) -> InlineKeyboardMarkup:
    rows = []
    for acc in accounts:
        rows.append([InlineKeyboardButton(
            text=f"@{acc.username}",
            callback_data=f"{action}_{acc.id}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Скасувати", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Proxies ───────────────────────────────────────────────────────────────────

def proxies_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список по акаунтах", callback_data="proxy_list")],
        [InlineKeyboardButton(text="✅ Перевірити проксі", callback_data="proxy_check_all")],
        [InlineKeyboardButton(text="📁 Масове призначення", callback_data="proxy_bulk_upload")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])


def proxy_account_kb(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Один проксі", callback_data=f"proxy_set_one_{account_id}")],
        [InlineKeyboardButton(text="📋 Список для ротації", callback_data=f"proxy_set_list_{account_id}")],
        [InlineKeyboardButton(text="🚫 Вимкнути проксі", callback_data=f"proxy_disable_{account_id}")],
        [InlineKeyboardButton(text="🔍 Перевірити IP браузера", callback_data=f"proxy_check_browser_{account_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"account_view_{account_id}")],
    ])


# ── Settings ──────────────────────────────────────────────────────────────────

def settings_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 CAPTCHA сервіс", callback_data="captcha_settings")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])


def captcha_service_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="2captcha", callback_data="captcha_svc_2captcha")],
        [InlineKeyboardButton(text="CapSolver", callback_data="captcha_svc_capsolver")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")],
    ])
