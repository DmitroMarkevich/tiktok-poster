from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN: str       = os.getenv("BOT_TOKEN", "")
SUPERADMIN_ID: int   = int(os.getenv("SUPERADMIN_ID") or "0")
DATABASE_URL: str    = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./tiktok_bot.db")

BROWSERS_DIR = os.path.join(os.path.dirname(__file__), "browsers")
VIDEOS_DIR   = os.path.join(os.path.dirname(__file__), "videos")

CAPTCHA_SERVICE: str = os.getenv("CAPTCHA_SERVICE", "2captcha")
CAPTCHA_API_KEY: str = os.getenv("CAPTCHA_API_KEY", "")

# Free AI for comment variation (Google Gemini — generous free tier, good Ukrainian).
# Get a key at https://aistudio.google.com/apikey and put it in .env as GEMINI_API_KEY.
# Empty key → bot silently falls back to local spintax variation.
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str    = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

os.makedirs(BROWSERS_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR,   exist_ok=True)
