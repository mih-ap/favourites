"""Настройки сервиса Favourites: пути, ключи, лимиты.

Все значения читаются из окружения; `.env` проекта подхватывается автоматически,
а недостающие ключи (например, ключ LiteLLM) берутся из ~/.hermes/.env.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Читает .env проекта, затем ~/.hermes/.env; существующие переменные не трогает."""
    for path in (ROOT / ".env", Path.home() / ".hermes" / ".env"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


_load_env()


def _path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


DATA_DIR = _path("FAV_DATA_DIR", ROOT / "data")
DB_PATH = DATA_DIR / "favourites.db"
VAULT_DIR = _path("FAV_VAULT", ROOT / "vault")
VAULT_NAME = (os.environ.get("FAV_VAULT_NAME") or VAULT_DIR.name).strip()
LIBRARY_DIR = VAULT_DIR / "library"
STATIC_DIR = ROOT / "web" / "static"

COOKIES_FILE = _path("FAV_COOKIES_FILE", DATA_DIR / "instagram-cookies.txt")

TELEGRAM_TOKEN = (os.environ.get("FAV_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
ALLOWED_CHATS = {
    int(chunk)
    for chunk in re.split(r"[,\s]+", os.environ.get("FAV_ALLOWED_CHATS", ""))
    if chunk.strip().lstrip("-").isdigit()
}

PUBLIC_URL = os.environ.get("FAV_PUBLIC_URL", "http://localhost:8090").rstrip("/")
WEB_HOST = os.environ.get("FAV_WEB_HOST", "0.0.0.0")
WEB_PORT = _int("FAV_WEB_PORT", 8090)

LLM_BASE_URL = os.environ.get("FAV_LLM_BASE_URL", "https://litellm.cdek.tech/v1").rstrip("/")
LLM_API_KEY = (
    os.environ.get("FAV_LLM_API_KEY")
    or os.environ.get("HERMES_CUSTOM_LITELLM_CDEK_API_KEY", "")
).strip()
# Бесплатная модель в LiteLLM (input/output cost 0, контекст 65k). Карточки на ней
# чуть суше, чем на deepseek-v4.1-flash, но платить за разбор не нужно.
LLM_MODEL = os.environ.get("FAV_LLM_MODEL", "Gemma-4-12B-it-qat-w4a16-ct")
LLM_MAX_INPUT = _int("FAV_LLM_MAX_INPUT", 14000)
LLM_TIMEOUT = _int("FAV_LLM_TIMEOUT", 180)

MAX_ATTEMPTS = _int("FAV_MAX_ATTEMPTS", 3)

# Ниже этого порога «содержания» (символы без «Video by», хештегов и дублей) модель
# не спрашивают: из одного заголовка она сочинит правдоподобную, но вымышленную карточку.
MIN_SUBSTANCE = _int("FAV_MIN_SUBSTANCE", 200)

# Расшифровка звука для роликов без субтитров и подписи (faster-whisper на CPU).
ASR_ENABLED = (os.environ.get("FAV_ASR_ENABLED", "1").strip().lower()
               not in {"0", "false", "no", "off"})
ASR_MODEL_NAME = os.environ.get("FAV_ASR_MODEL", "small")
ASR_COMPUTE_TYPE = os.environ.get("FAV_ASR_COMPUTE", "int8")
ASR_THREADS = _int("FAV_ASR_THREADS", 2)
ASR_MAX_SECONDS = _int("FAV_ASR_MAX_SECONDS", 600)
ASR_MODEL_DIR = _path("FAV_ASR_MODEL_DIR", DATA_DIR / "models")
ASR_TMP_DIR = _path("FAV_ASR_TMP_DIR", DATA_DIR / "tmp")

# Размер текста, который храним в БД для поиска (полный текст остаётся в карточке).
SEARCH_TEXT_LIMIT = _int("FAV_SEARCH_TEXT_LIMIT", 60000)

# Фото постов (фото/карусель) копируются в vault рядом с карточкой.
IMAGES_ENABLED = (os.environ.get("FAV_IMAGES", "1").strip().lower()
                  not in {"0", "false", "no", "off"})
IMAGES_MAX = _int("FAV_IMAGES_MAX", 20)
IMAGE_MAX_BYTES = _int("FAV_IMAGE_MAX_BYTES", 12 * 1024 * 1024)
IMAGES_DIR_NAME = "attachments"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    ASR_TMP_DIR.mkdir(parents=True, exist_ok=True)
