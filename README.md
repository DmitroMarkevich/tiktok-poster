# tiktok-poster

Telegram-керований бот для TikTok: автозавантаження відео та коментування під хештегами з кількох акаунтів. Керування через aiogram-бота, браузерна автоматизація на Playwright.

## Можливості

- **Завантаження відео** на один / вибрані / усі акаунти (bulk).
- **Коментування** під відео за хештегом/ключовим словом, з AI-варіацією тексту (Gemini) проти тіньового фільтра.
- **Акаунти** — додавання через cookies (Cookie-Editor JSON), перевірка валідності сесії.
- **Проксі** — один або список для ротації, перевірка через браузер.
- **CAPTCHA** — вбудований розв'язувач ротаційної капчі (OpenCV) + опційно 2captcha / CapSolver.

## Стек

- Python 3.9, [aiogram](https://github.com/aiogram/aiogram) (Telegram), [Playwright](https://playwright.dev/python/) (браузер)
- SQLAlchemy (async) + SQLite
- aiohttp, OpenCV

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env      # заповни BOT_TOKEN, за бажанням GEMINI_API_KEY
python main.py
```

## Налаштування (`.env`)

| Змінна | Опис |
|---|---|
| `BOT_TOKEN` | токен Telegram-бота (@BotFather) |
| `ADMIN_IDS` | Telegram ID адмінів через кому |
| `DATABASE_URL` | за замовч. `sqlite+aiosqlite:///./tiktok_bot.db` |
| `GEMINI_API_KEY` | (опц.) ключ Google AI Studio для AI-варіації коментарів |
| `GEMINI_MODEL` | модель Gemini (за замовч. `gemini-2.5-flash-lite`) |

## Структура

```
main.py            точка входу (запуск бота)
config.py          конфіг із .env
bot/               aiogram: handlers, keyboards, states, middlewares
tiktok/            браузерна автоматизація: uploader, commenter, browser, auth
database/          моделі + репозиторії (SQLAlchemy)
utils/             captcha, ai (Gemini), stealth, ін.
```

## Безпека

Не коміть `.env`, `*.db` і `browsers/` — там живі токени та сесії акаунтів (вже в `.gitignore`).
