"""Settings and paths.

Values are looked up in this order:
  1. settings saved by the admin on the Settings page (database `settings` table)
  2. environment variables (Railway Variables) / .env file (local)
"""
import glob
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
STATIC = ROOT / "static"
MUSIC = ROOT / "music"
FONTS = ROOT / "fonts"


_dotenv: dict = {"mtime": None, "vals": {}}


def _dotenv_vals() -> dict:
    """The .env file's values, re-read when the file changes (edits apply without a restart)."""
    try:
        m = ENV_FILE.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _dotenv["mtime"] != m:
        _dotenv["vals"] = {k: (v or "") for k, v in dotenv_values(ENV_FILE).items()}
        _dotenv["mtime"] = m
    return _dotenv["vals"]


def env(name: str, default: str = "") -> str:
    """Real environment variables win; .env fills in what isn't set. Blank values never override."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    return (_dotenv_vals().get(name) or "").strip() or default


# All persistent data lives under DATA_DIR (a Railway volume in production).
DATA = Path(env("DATA_DIR") or ROOT)
OUTPUT = DATA / "work"            # temporary render folders (finished videos go to storage)
MUSIC.mkdir(parents=True, exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)

# Legacy single-user files (imported into the first admin account, then ignored)
LEGACY_OUTPUT = DATA / "output"
LEGACY_CHANNELS = DATA / "channels.json"
LEGACY_TOKENS = DATA / "tokens"
LEGACY_SETTINGS = DATA / "settings.json"

# Keys the admin can edit on the Settings page. Infrastructure (DATABASE_URL, bucket, DATA_DIR) is env-only.
EDITABLE = {
    "ALLOW_SIGNUP": {"group": "users", "label": "Allow new sign-ups (true/false)", "secret": False},
    "REQUIRE_APPROVAL": {"group": "users", "label": "New accounts need admin approval (true/false)", "secret": False},
    "DAILY_VIDEO_LIMIT": {"group": "users", "label": "Videos per user per 24h (0 = unlimited; admins unlimited)", "secret": False},
    "GEMINI_API_KEY": {"group": "ai", "label": "Gemini API key", "secret": True},
    "GROQ_API_KEY": {"group": "ai", "label": "Groq API key", "secret": True},
    "OPENROUTER_API_KEY": {"group": "ai", "label": "OpenRouter API key (free models; picks DeepSeek when one is free)", "secret": True},
    "DEEPSEEK_API_KEY": {"group": "ai", "label": "DeepSeek API key (direct; free starter credit, then very cheap)", "secret": True},
    "LLM_ORDER": {"group": "ai", "label": "Order to try (e.g. gemini,groq,openrouter,deepseek)", "secret": False},
    "PIXABAY_API_KEY": {"group": "media", "label": "Pixabay API key", "secret": True},
    "PEXELS_API_KEY": {"group": "media", "label": "Pexels API key", "secret": True},
    "CLOUDFLARE_ACCOUNT_ID": {"group": "media", "label": "Cloudflare account ID", "secret": False},
    "CLOUDFLARE_API_TOKEN": {"group": "media", "label": "Cloudflare API token", "secret": True},
    "NVIDIA_API_KEY": {"group": "media", "label": "NVIDIA API key (Cosmos3-nano AI video clips: auto-used once NVIDIA opens the API)", "secret": True},
    "COSMOS_ENABLED": {"group": "media", "label": "Use Cosmos AI clips when available (true/false)", "secret": False},
    "COSMOS_MAX_SCENES": {"group": "media", "label": "Max Cosmos AI clips per video (default 3)", "secret": False},
    "GOOGLE_CLIENT_ID": {"group": "youtube", "label": "Google OAuth client ID", "secret": False},
    "GOOGLE_CLIENT_SECRET": {"group": "youtube", "label": "Google OAuth client secret", "secret": True},
    "TIKTOK_CLIENT_KEY": {"group": "tiktok", "label": "TikTok client key", "secret": False},
    "TIKTOK_CLIENT_SECRET": {"group": "tiktok", "label": "TikTok client secret", "secret": True},
    "INSTAGRAM_APP_ID": {"group": "instagram", "label": "Instagram app ID", "secret": False},
    "INSTAGRAM_APP_SECRET": {"group": "instagram", "label": "Instagram app secret", "secret": True},
    "PUBLIC_URL": {"group": "general", "label": "Public URL of this app", "secret": False},
    "CONTACT_EMAIL": {"group": "general", "label": "Contact email (privacy/terms pages)", "secret": False},
    "KEEP_VIDEOS_DAYS": {"group": "general", "label": "Delete video files after N days (0 = never)", "secret": False},
    "GEMINI_MODEL": {"group": "advanced", "label": "Force Gemini model", "secret": False},
    "GROQ_MODEL": {"group": "advanced", "label": "Force Groq model", "secret": False},
    "OPENROUTER_MODEL": {"group": "advanced", "label": "Force OpenRouter model (blank = best free model, DeepSeek preferred)", "secret": False},
    "DEEPSEEK_MODEL": {"group": "advanced", "label": "Force DeepSeek model (default deepseek-chat)", "secret": False},
    "CLOUDFLARE_IMAGE_MODEL": {"group": "advanced", "label": "Force Cloudflare image model", "secret": False},
    "INSTAGRAM_API_VERSION": {"group": "advanced", "label": "Instagram Graph API version", "secret": False},
}

KEYS = [
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
]

_cache: dict = {"at": 0.0, "data": {}}
_cache_lock = threading.Lock()


def overrides() -> dict:
    """Admin-saved settings from the database, cached for a few seconds."""
    with _cache_lock:
        if time.time() - _cache["at"] > 5:
            from . import db  # lazy: db imports config
            try:
                with db.session() as s:
                    _cache["data"] = {r.key: r.value for r in s.query(db.Setting).all()}
            except Exception:
                pass  # tables not created yet
            _cache["at"] = time.time()
        return _cache["data"]


def save_overrides(changes: dict) -> None:
    from . import db
    with db.session() as s:
        for k, v in changes.items():
            if k not in EDITABLE and not k.startswith("_"):
                continue
            v = str(v or "").strip()
            row = s.get(db.Setting, k)
            if v:
                if row:
                    row.value = v
                else:
                    s.add(db.Setting(key=k, value=v))
            elif row:
                s.delete(row)
    with _cache_lock:
        _cache["at"] = 0.0


def source(name: str) -> str | None:
    if overrides().get(name):
        return "app"
    if env(name):
        return "env"
    return None


def get(name: str, default: str = "") -> str:
    v = overrides().get(name)
    if v:
        return str(v).strip()
    return env(name, default)


def flag(name: str, default: bool) -> bool:
    v = get(name).lower()
    return default if not v else v in ("1", "true", "yes", "on")


def secret_key() -> bytes:
    """Random per-install key (stored in the database) for signing temporary video links."""
    key = overrides().get("_secret_key")
    if not key:
        key = secrets.token_hex(32)
        save_overrides({"_secret_key": key})
    return key.encode()


def _find_bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    pattern = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
        "*FFmpeg*", "**", f"{name}.exe",
    )
    hits = glob.glob(pattern, recursive=True)
    return hits[0] if hits else ""


FFMPEG = _find_bin("ffmpeg")
FFPROBE = _find_bin("ffprobe")
