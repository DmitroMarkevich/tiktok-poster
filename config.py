from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN: str       = os.getenv("BOT_TOKEN", "")
SUPERADMIN_ID: int   = int(os.getenv("SUPERADMIN_ID") or "0")
DATABASE_URL: str    = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./tiktok_bot.db")

# Headless mode. Headless Chrome carries a class of tells (render backend, rAF timings,
# missing window events) that no JS patch fully hides. On a Linux server run the bot under
# Xvfb (a virtual display) with HEADLESS=0 — that launches a REAL headful Chrome with a real
# GL render path, far harder to detect. Default stays headless for a plain dev box / no Xvfb.
HEADLESS: bool = (os.getenv("HEADLESS", "1").strip().lower() not in ("0", "false", "no"))

BROWSERS_DIR = os.path.join(os.path.dirname(__file__), "browsers")
VIDEOS_DIR   = os.path.join(os.path.dirname(__file__), "videos")

# Block VIDEO bytes on the comment flow to save proxy bandwidth. Default OFF: an account
# that "watches" a video for ~14s and likes it but fetches ZERO video segments from the CDN
# is a server-side bot tell (real viewers stream the media) that feeds the shadow-filter.
# Set BLOCK_VIDEO_MEDIA=1 only if proxy bandwidth is the binding constraint. Images are
# always blocked (thumbnails are the bulk of the bytes and not a viewing signal).
BLOCK_VIDEO_MEDIA: bool = (os.getenv("BLOCK_VIDEO_MEDIA", "0").strip().lower() in ("1", "true", "yes"))

CAPTCHA_SERVICE: str = os.getenv("CAPTCHA_SERVICE", "2captcha")
CAPTCHA_API_KEY: str = os.getenv("CAPTCHA_API_KEY", "")

# Antidetect-browser device provider. patchright on one host pins GPU/platform/WebGL to the
# real machine and shares the same TLS/JA3 across every account, so all accounts look like ONE
# device — TikTok's register_verify_login then throttles with error_code 7 regardless of proxy.
# A real antidetect browser injects a DISTINCT, self-consistent device fingerprint per profile
# at the C++/kernel level (passes pixelscan, varies canvas/WebGL/audio/fonts), letting each
# account be a genuinely different device. We drive it over its Local API + CDP.
#   DEVICE_PROVIDER = "patchright" (default, single-device) | "adspower" | "dolphin"
#                   | "camoufox" (FREE, local, no account, no daily limit — patched Firefox)
DEVICE_PROVIDER: str = os.getenv("DEVICE_PROVIDER", "patchright").strip().lower()
# AdsPower Local API (the desktop app must be running with the Local API enabled). Default port
# 50325 is AdsPower's local endpoint. No key needed for the local API on most versions.
ADSPOWER_API: str    = os.getenv("ADSPOWER_API", "http://local.adspower.net:50325")
ADSPOWER_GROUP_ID: str = os.getenv("ADSPOWER_GROUP_ID", "0")
# Dolphin{anty} Local API (desktop app running). Default local port 3001.
DOLPHIN_API: str     = os.getenv("DOLPHIN_API", "http://localhost:3001")
DOLPHIN_TOKEN: str   = os.getenv("DOLPHIN_TOKEN", "")

# Free AI for comment variation (Google Gemini — generous free tier, good Ukrainian).
# Get a key at https://aistudio.google.com/apikey and put it in .env as GEMINI_API_KEY.
# Empty key → bot silently falls back to local spintax variation.
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str    = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

os.makedirs(BROWSERS_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR,   exist_ok=True)
