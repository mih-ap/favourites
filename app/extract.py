"""Извлечение содержимого по ссылке: видео, соцсети, статьи.

Видео и соцсети — через yt-dlp (метаданные + субтитры/автосубтитры).
Статьи — httpx + trafilatura, с запасным разбором Open Graph.
"""
from __future__ import annotations

import glob
import html
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import config, db

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

SITE_NAMES = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "instagram.com": "Instagram",
    "tiktok.com": "TikTok", "x.com": "X", "twitter.com": "X", "t.me": "Telegram",
    "vk.com": "VK", "habr.com": "Хабр", "reddit.com": "Reddit", "medium.com": "Medium",
    "github.com": "GitHub", "vc.ru": "vc.ru", "telegra.ph": "Telegraph",
    "rutube.ru": "Rutube", "dzen.ru": "Дзен", "pikabu.ru": "Пикабу",
    "linkedin.com": "LinkedIn", "facebook.com": "Facebook",
}


def site_name(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    for domain, name in SITE_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host


def kind_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    if "youtube" in host or host == "youtu.be":
        return "youtube"
    if "instagram" in host:
        return "instagram"
    if "tiktok" in host:
        return "tiktok"
    if host in {"x.com", "twitter.com"} or host.endswith(".twitter.com"):
        return "twitter"
    if "telegram" in host or host in {"t.me", "telegram.me"}:
        return "telegram"
    if "vk.com" in host or "vkvideo" in host:
        return "vk"
    return "article"


def extract_urls(text: str, entities: list[dict] | None = None) -> list[str]:
    """Достаёт ссылки из текста сообщения и из entities Telegram (включая скрытые)."""
    found: list[str] = []
    for entity in entities or []:
        if entity.get("type") == "text_link" and entity.get("url"):
            found.append(entity["url"])
        elif entity.get("type") == "text_mention":
            continue
    found.extend(re.findall(r"https?://[^\s<>\"'«»]+", text or ""))
    seen, result = set(), []
    for raw in found:
        url = raw.rstrip(").,;!?»\"'")
        key = db.url_key(url)
        if key not in seen:
            seen.add(key)
            result.append(url)
    return result


def _meta(html_text: str, prop: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(prop)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.I | re.S)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def _decode(response: httpx.Response) -> str:
    """Декодирует ответ с учётом charset из заголовка или meta (часто windows-1251)."""
    raw = response.content
    candidates = []
    match = re.search(r"charset=([\w-]+)", response.headers.get("content-type", ""), re.I)
    if match:
        candidates.append(match.group(1))
    match = re.search(rb"charset=[\"']?([\w-]+)", raw[:8192], re.I)
    if match:
        candidates.append(match.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "cp1251"]
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _challenge(page: str) -> tuple[str, str, str] | None:
    """JS-защита вида document.cookie="RCPC=…"; location.replace("…?attempt=1")."""
    cookie = re.search(r'document\.cookie\s*=\s*"([^=;]+)=([^;"]+);', page)
    target = re.search(r'var\s+u\s*=\s*"([^"]+)"', page)
    if cookie and target:
        return cookie.group(1), cookie.group(2), target.group(1)
    return None


def _fetch_html(url: str) -> tuple[str, str]:
    last_error: Exception | None = None
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
               "Accept-Language": "ru,en;q=0.8"}
    with httpx.Client(follow_redirects=True, timeout=25, headers=headers) as client:
        for attempt in range(3):
            try:
                response = client.get(url)
            except Exception as exc:  # noqa: BLE001 — сеть
                last_error = exc
                time.sleep(1)
                continue
            if response.status_code < 400:
                return _decode(response), str(response.url)
            challenge = _challenge(response.text)
            if challenge and attempt < 2:
                name, value, target = challenge
                client.cookies.set(name, value)
                url = target
                continue
            if response.status_code == 429:
                last_error = RuntimeError("сайт ограничил частоту запросов (HTTP 429)")
            elif response.status_code in {401, 403}:
                last_error = RuntimeError(f"сайт закрыт для анонимного доступа (HTTP {response.status_code})")
            else:
                last_error = RuntimeError(f"HTTP {response.status_code} для {url}")
            break
    raise RuntimeError(str(last_error) if last_error else f"не удалось загрузить {url}")


def _vtt_text(raw: str) -> str:
    """VTT/SRT → плоский текст без таймкодов и дублей."""
    lines: list[str] = []
    for line in raw.splitlines():
        text = line.strip()
        if (not text or text.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE"))
                or "-->" in text or text.isdigit()):
            continue
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    # YouTube-автосубтитры повторяют предыдущую строку в следующем блоке — схлопываем.
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


SUBTITLE_LANGS = ["ru", "ru-RU", "en", "en-US", "en-orig"]


def _download_captions(info: dict) -> str:
    """Скачивает дорожку субтитров напрямую по URL из метаданных.

    yt-dlp'шный загрузчик субтитров падает целиком на 429; здесь мы просто
    пропускаем неудачную дорожку и пробуем следующую.
    """
    sources: dict[str, list] = {}
    for key in ("subtitles", "automatic_captions"):
        for lang, formats in (info.get(key) or {}).items():
            sources.setdefault(lang, formats)
    ordered = [lang for lang in SUBTITLE_LANGS if lang in sources]
    ordered += [lang for lang in sources if lang not in ordered]
    for lang in ordered[:4]:
        formats = sources.get(lang) or []
        track = (next((f for f in formats if f.get("ext") == "vtt"), None)
                 or next((f for f in formats if f.get("ext") in {"srv1", "ttml", "srt", "json3"}), None))
        if not track or not track.get("url"):
            continue
        for attempt in range(2):
            try:
                response = httpx.get(track["url"], timeout=25, headers={"User-Agent": UA})
                if response.status_code == 429:
                    time.sleep(3)
                    continue
                response.raise_for_status()
                text = _vtt_text(response.text)
                if len(text) > 40:
                    return text
            except Exception:  # noqa: BLE001 — пробуем следующую дорожку
                time.sleep(1)
    return ""


def _product_images(product_info: dict) -> list[str]:
    """Картинки поста из ответа Instagram: слайды карусели или единственное фото.

    У каждого медиа есть image_versions2.candidates — Instagram отдаёт их от
    наибольшего размера к наименьшему, поэтому берём первый. Для видеослайда
    это обложка, другого изображения у него нет. Ссылки CDN живут часами,
    поэтому скачивать их нужно сразу, а не при открытии карточки.
    """
    slides = product_info.get("carousel_media") or [product_info]
    found: list[str] = []
    for slide in slides:
        candidates = ((slide.get("image_versions2") or {}).get("candidates") or [])
        url = next((c.get("url") for c in candidates if c.get("url")), "")
        if url and url not in found:
            found.append(url)
    return found


def _instagram_image_hook(images: list[str]):
    """Перехватывает метаданные поста внутри yt-dlp.

    yt-dlp для фото и каруселей падает («There is no video in this post»), но
    к этому моменту уже получил product_info с картинками — берём их оттуда.
    Хук ставится на класс на время одного вызова (воркер однопоточный).
    """
    from yt_dlp.extractor.instagram import InstagramBaseIE

    original = InstagramBaseIE._extract_product

    def capture(self, product_info, video_id=None, get_comments=True):
        for url in _product_images(product_info):
            if url not in images:
                images.append(url)
        return original(self, product_info, video_id=video_id, get_comments=get_comments)

    InstagramBaseIE._extract_product = capture
    return lambda: setattr(InstagramBaseIE, "_extract_product", original)


def _ytdlp(url: str, images: list[str] | None = None) -> dict:
    """Метаданные + субтитры без скачивания медиа.

    `images` — необязательный список, куда складываются ссылки на фото поста;
    он заполняется, даже если yt-dlp затем упадёт на посте без видео.
    """
    import yt_dlp

    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "noprogress": True,
    }
    if config.COOKIES_FILE.exists():
        opts["cookiefile"] = str(config.COOKIES_FILE)
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    undo = None
    if images is not None and kind_of(url) == "instagram":
        try:
            undo = _instagram_image_hook(images)
        except ImportError:  # другая раскладка пакетов yt-dlp
            undo = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        if undo:
            undo()
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0] or info

    transcript = _download_captions(info)
    description = info.get("description") or ""
    title = info.get("title") or ""
    if kind_of(url) == "instagram":
        # yt-dlp отдаёт заголовок «Video by <user>», а подпись — в description.
        description = _clean_ig(description)
        title = _clean_ig(title) or next((line.strip() for line in description.splitlines()
                                          if line.strip()), "")
        # Description у Instagram дублирует строки — оставляем уникальные.
        description = "\n".join(dict.fromkeys(line for line in description.splitlines()))
    published = str(info.get("upload_date") or "")
    if len(published) == 8:
        published = f"{published[:4]}-{published[4:6]}-{published[6:]}"
    return {
        "title": title,
        "author": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
        "description": description,
        "duration": int(info.get("duration") or 0),
        "published": published,
        "thumb": info.get("thumbnail") or "",
        "transcript": transcript,
        "site": site_name(url),
        "kind": kind_of(url),
        "text": "\n\n".join(part for part in (description, transcript) if part),
    }


IG_SHORTCODE = re.compile(r"instagram\.com/(?:reels?|p|tv)/([A-Za-z0-9_-]+)")
IG_VIDEO_CREDIT = re.compile(r"^\s*Video by\s+\S+\s*$", re.I)


def _clean_ig(text: str) -> str:
    """Убирает служебную строку «Video by <user>», которую подставляет yt-dlp."""
    lines = [line for line in (text or "").splitlines() if not IG_VIDEO_CREDIT.match(line)]
    return "\n".join(lines).strip()


def _instagram_oembed(url: str) -> dict:
    """Публичный oEmbed отдаёт подпись поста без логина: title = текст подписи.

    Прежний фолбэк скрейпил /embed/captioned/ и ломался, как только Instagram
    менял разметку (класса Caption в странице больше нет).
    """
    shortcode = IG_SHORTCODE.search(url)
    if not shortcode:
        return {}
    endpoint = ("https://www.instagram.com/api/v1/oembed/"
                f"?url=https://www.instagram.com/p/{shortcode.group(1)}/")
    try:
        response = httpx.get(endpoint, timeout=20, follow_redirects=True,
                             headers={"User-Agent": UA, "Accept": "application/json"})
        if response.status_code != 200:
            return {}
        payload = response.json()
    except Exception:  # noqa: BLE001 — фолбэк не обязателен
        return {}
    caption = _clean_ig(str(payload.get("title") or ""))
    author = str(payload.get("author_name") or "")
    title = next((line.strip() for line in caption.splitlines() if line.strip()), "")
    return {"title": title[:300], "author": author, "description": caption,
            "duration": 0, "published": "", "thumb": str(payload.get("thumbnail_url") or ""),
            "transcript": "", "site": "Instagram", "kind": "instagram",
            "text": caption, "caption": caption, "shortcode": shortcode.group(1)}


def _instagram_fallback(url: str) -> dict:
    data = _instagram_oembed(url)
    if data:
        return data
    return {"title": "", "author": "", "description": "", "duration": 0,
            "published": "", "thumb": "", "transcript": "", "site": "Instagram",
            "kind": "instagram", "text": ""}


TG_PHOTO = re.compile(
    r"tgme_widget_message_photo_wrap[^>]*?background-image:\s*url\('(https://[^']+)'\)", re.S)


def _telegram_images(page: str) -> list[str]:
    """Фото поста t.me: у альбома в разметке несколько photo_wrap-блоков."""
    found = [url for url in TG_PHOTO.findall(page) if not url.startswith("data:")]
    if not found:
        og = _meta(page, "og:image")
        found = [og] if og.startswith("http") else []
    return list(dict.fromkeys(found))


def _article(url: str) -> dict:
    import trafilatura

    page, final_url = _fetch_html(url)
    text = trafilatura.extract(
        page, url=final_url, include_comments=False, include_tables=True,
        favor_recall=True, output_format="txt",
    ) or ""
    meta = trafilatura.extract_metadata(page) if page else None
    title = (meta.title if meta and meta.title else "") or _meta(page, "og:title")
    author = (meta.author if meta and meta.author else "") or _meta(page, "article:author")
    published = (meta.date if meta and meta.date else "") or _meta(page, "article:published_time")
    description = (meta.description if meta and meta.description else "") or _meta(page, "og:description")
    if not text:
        text = description
    return {
        "title": title.strip(),
        "author": (author or "").strip(),
        "description": description.strip(),
        "duration": 0,
        "published": (published or "")[:10],
        "thumb": _meta(page, "og:image"),
        "transcript": "",
        "site": (meta.sitename if meta and getattr(meta, "sitename", "") else "") or site_name(url),
        "kind": kind_of(url),
        "text": text.strip(),
        "canonical": final_url,
        "images": _telegram_images(page) if kind_of(url) == "telegram" else [],
    }


IG_ERROR_HINTS = (
    ("There is no video in this post",
     "в посте нет видео — это фото или карусель, доступна только подпись"),
    ("No video formats found",
     "в посте нет видео — это фото или карусель, доступна только подпись и картинки"),
    ("HTTP Error 429", "Instagram ограничил частоту запросов (HTTP 429)"),
    ("Too Many Requests", "Instagram ограничил частоту запросов (HTTP 429)"),
    ("login required", "Instagram требует вход в аккаунт"),
    ("Empty media response", "Instagram вернул пустой ответ — нужны cookies"),
    ("Unable to extract", "Instagram не отдал данные поста"),
)

# Признаки поста без видео: расшифровывать звук нечего, зато есть картинки.
NO_VIDEO_HINTS = ("there is no video in this post", "no video formats found")


def _friendly_error(raw: str) -> str:
    """Технический текст yt-dlp → понятная причина для карточки и уведомления."""
    for token, hint in IG_ERROR_HINTS:
        if token.lower() in raw.lower():
            return hint
    return raw


def content_weight(text: str) -> int:
    """Сколько в тексте собственно содержания: без «Video by», хештегов и дублей."""
    seen: set[str] = set()
    weight = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or IG_VIDEO_CREDIT.match(line) or line in seen:
            continue
        if all(token.startswith(("#", "@")) for token in line.split()):
            continue
        seen.add(line)
        weight += len(line)
    return weight


VIDEO_KINDS = {"youtube", "instagram", "tiktok", "twitter", "vk"}


def _maybe_transcribe(data: dict, url: str, warning: str) -> tuple[dict, str]:
    """Если текста от источника нет — расшифровываем звук ролика."""
    if not config.ASR_ENABLED or data.get("no_audio"):
        return data, warning
    duration = int(data.get("duration") or 0)
    if duration and duration > config.ASR_MAX_SECONDS:
        return data, warning
    from . import asr

    result = asr.transcribe(url)
    if result.get("text"):
        text = result["text"]
        data["transcript"] = text
        data["asr"] = True
        data["text"] = "\n\n".join(part for part in (data.get("text") or "", text) if part)
        if not data.get("duration"):
            data["duration"] = int(result.get("seconds") or 0)
        return data, warning
    if result.get("error"):
        return data, " · ".join(part for part in (warning, result["error"]) if part)
    return data, warning


def fetch(url: str, note: str | None = None) -> dict:
    """Возвращает нормализованный словарь с содержимым страницы/видео."""
    kind = kind_of(url)
    data: dict
    warning = ""
    images: list[str] = []
    if kind in {"youtube", "instagram", "tiktok", "twitter", "vk"}:
        try:
            data = _ytdlp(url, images)
        except Exception as exc:  # noqa: BLE001 — yt-dlp падает по-разному
            if kind == "instagram":
                data = _instagram_fallback(url)
                warning = f"yt-dlp: {_friendly_error(str(exc))}"
                if any(hint in str(exc).lower() for hint in NO_VIDEO_HINTS):
                    data["no_audio"] = True  # фото/карусель: расшифровывать нечего
            else:
                data = {"title": "", "author": "", "description": "", "duration": 0,
                        "published": "", "thumb": "", "transcript": "", "site": site_name(url),
                        "kind": kind, "text": ""}
                warning = f"yt-dlp: {str(exc)[:200]}"
        data["images"] = images
        if kind == "instagram":
            # Instagram не отдаёт субтитры: подпись из oEmbed — единственный текст.
            if content_weight(data.get("description") or data.get("text") or "") < 200:
                extra = _instagram_oembed(url)
                if extra and len(extra.get("caption") or "") > len(data.get("text") or ""):
                    data = {**extra, **{k: v for k, v in data.items()
                                        if k in {"author", "published", "thumb", "duration",
                                                 "images"} and v}}
                    data["kind"] = kind
                    warning = warning or "yt-dlp без подписи: текст взят из oEmbed"
            images = data.get("images") or images
            data["images"] = images
            if not images and data.get("no_audio") and data.get("thumb"):
                # Метаданные поста перехватить не удалось — остаётся обложка из oEmbed.
                data["images"] = [data["thumb"]]
        if content_weight(data.get("text") or "") < config.MIN_SUBSTANCE:
            data, warning = _maybe_transcribe(data, url, warning)
    else:
        data = _article(url)

    data.setdefault("images", [])
    data["images"] = list(dict.fromkeys(u for u in data["images"] if u.startswith("http")))[: config.IMAGES_MAX]
    data.setdefault("kind", kind)
    data.setdefault("site", site_name(url))
    data.setdefault("duration", 0)
    data["url"] = url
    data["warning"] = warning
    data["note"] = note or ""

    pieces = [data.get("title", ""), data.get("description", ""), data.get("text", "")]
    if note:
        pieces.append(note)
    # Заголовок и подпись часто повторяются внутри text (yt-dlp кладёт их и в description) —
    # оставляем каждый абзац один раз, иначе модель видит один и тот же текст дважды.
    paragraphs: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        for chunk in str(piece or "").split("\n\n"):
            normalized = re.sub(r"\s+", " ", chunk.strip()).lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            paragraphs.append(chunk.strip())
    data["text"] = "\n\n".join(paragraphs)[: config.SEARCH_TEXT_LIMIT]
    if not data["title"] and kind == "instagram":
        data["title"] = (f"Instagram: {data.get('author') or ''}".strip(": ")
                         or "Instagram: без подписи")
    if not data["title"]:
        data["title"] = (data.get("description") or data["text"] or url).strip().split("\n")[0][:120]
    return data
