"""Запись карточек в Obsidian-vault и сборка индекса раздела `library/`."""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config, db

# Instagram CDN отдаёт картинки только запросу с браузерным User-Agent.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}

README = """# library — избранное

Раздел собирается сервисом **Favourites** (Telegram-бот → анализ → карточка).
Один материал = один markdown-файл в подкаталоге месяца.

- `_index.md` — сгенерированный указатель, править бесполезно: перезапишется.
- Править текст карточки руками можно, но повторная обработка ссылки перезапишет файл.
- Теги в frontmatter работают в Obsidian как обычные теги.
- Фото постов лежат рядом в `attachments/<имя карточки>/` и вставлены в заметку.
- Веб-версия раздела: {url}
"""

# Форматы, которые умеет отдавать веб-справочник (см. `web.py`).
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}

IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

IMAGE_TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/avif": ".avif",
}


def slugify(text: str, limit: int = 60) -> str:
    text = (text or "").lower()
    text = "".join(TRANSLIT.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:limit].rstrip("-") or "item")


def _yaml(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def relative_path(value: str) -> str:
    """Путь внутри library/: в БД храним относительный, чтобы host и контейнер совпадали."""
    path = Path(value)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.relative_to(config.LIBRARY_DIR).as_posix()
    except ValueError:
        pass
    parts = path.parts
    if "library" in parts:
        marker = len(parts) - 1 - parts[::-1].index("library")
        return Path(*parts[marker + 1:]).as_posix()
    return path.name


def resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() and path.exists():
        return path
    return config.LIBRARY_DIR / relative_path(value)


def _safe(name: str) -> str:
    return re.sub(r'[*"<>|?\\/:]+', " ", name or "").strip()


def card_path(item_id: int, title: str, created: str) -> Path:
    try:
        day = datetime.strptime(created[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        day = datetime.now(timezone.utc)
    folder = config.LIBRARY_DIR / day.strftime("%Y-%m")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{day:%Y-%m-%d}-{slugify(title)}-{item_id}.md"


def _render_status(analysis: dict) -> str:
    return "partial" if analysis.get("degraded") else "done"


def _image_suffix(content: bytes, content_type: str) -> str:
    """Расширение по сигнатуре файла, затем по Content-Type.

    Content-Type одного мало: CDN иногда отдаёт текст ошибки как image/jpeg.
    """
    for signature, suffix in IMAGE_SIGNATURES:
        if content.startswith(signature):
            return suffix
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content[4:12] in {b"ftypavif", b"ftypavis"}:
        return ".avif"
    return IMAGE_TYPES.get((content_type or "").split(";")[0].strip().lower(), "")


def save_images(card: Path, urls: list[str], client=None) -> tuple[list[str], list[str]]:
    """Скачивает фото поста рядом с карточкой.

    Возвращает (пути относительно каталога карточки, ошибки). Ссылки Instagram
    CDN одноразовые, поэтому копия нужна локальная — и Obsidian, и веб читают
    её из vault, а не из сети.
    """
    if not urls or not config.IMAGES_ENABLED:
        return [], []
    folder = card.parent / config.IMAGES_DIR_NAME / card.stem
    folder.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    errors: list[str] = []
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=45, follow_redirects=True,
                              headers={"User-Agent": USER_AGENT})
    try:
        for index, url in enumerate(urls[: config.IMAGES_MAX], start=1):
            try:
                response = client.get(url, headers={"Accept": "image/*,*/*;q=0.8"})
                response.raise_for_status()
                content = response.content
                if len(content) > config.IMAGE_MAX_BYTES:
                    raise RuntimeError(f"больше {config.IMAGE_MAX_BYTES // (1024 * 1024)} МБ")
                suffix = _image_suffix(content, response.headers.get("content-type", ""))
                if not suffix:
                    raise RuntimeError("ответ не похож на картинку")
                target = folder / f"{index:02d}{suffix}"
                target.write_bytes(content)
                saved.append(target)
            except Exception as exc:  # noqa: BLE001 — одно битое фото не должно ронять карточку
                errors.append(str(exc)[:120])
    finally:
        if own_client:
            client.close()
    # Файлы от прошлой обработки: в карусели могло стать меньше слайдов.
    for stale in folder.iterdir():
        if stale.suffix.lower() in IMAGE_SUFFIXES and stale not in saved:
            stale.unlink(missing_ok=True)
    return [path.relative_to(card.parent).as_posix() for path in saved], errors


def _render_photos(data: dict) -> list[str]:
    photos = data.get("image_paths") or []
    if not photos:
        return []
    total = len(photos)
    lines = ["## Фото", ""]
    for index, relative in enumerate(photos, start=1):
        alt = f"Фото {index} из {total}" if total > 1 else "Фото"
        lines += [f"![{alt}]({relative})", ""]
    return lines


def render(item, data: dict, analysis: dict) -> str:
    tags = analysis.get("tags") or []
    photos = data.get("image_paths") or []
    front = [
        "---",
        "type: favorite",
        f"title: {_yaml(analysis.get('title_ru') or data.get('title') or '')}",
        f"source: {_yaml(data.get('kind') or '')}",
        f"url: {_yaml(data.get('url') or item['url'])}",
        f"author: {_yaml(data.get('author') or '')}",
        f"site: {_yaml(data.get('site') or '')}",
        f"published: {_yaml(str(data.get('published') or ''))}",
        f"saved: {_yaml(item['created'])}",
        f"category: {_yaml(analysis.get('category') or 'Другое')}",
        f"tags: {_yaml(tags)}",
        f"lang: {_yaml(analysis.get('lang') or '')}",
        f"quality: {int(analysis.get('quality') or 0)}",
        f"duration: {int(data.get('duration') or 0)}",
        f"status: {_yaml(_render_status(analysis))}",
    ]
    if photos:
        front.insert(-1, f"photos: {len(photos)}")
    lines = front + [
        "---",
        "",
        f"# {analysis.get('title_ru') or data.get('title') or item['url']}",
        "",
    ]
    if analysis.get("degraded"):
        reason = re.sub(r"\s+", " ", analysis.get("warning") or "")
        lines += [f"**⚠️ Заметка неполная:** {reason or 'источник не отдал текст.'}", ""]
    if analysis.get("summary"):
        lines += [f"> {analysis['summary']}", ""]

    meta = [f"**Источник:** {data.get('site') or '—'}"]
    if data.get("author"):
        meta.append(f"**Автор:** {data['author']}")
    if data.get("published"):
        meta.append(f"**Опубликовано:** {data['published']}")
    meta.append(f"**Сохранено:** {item['created'][:10]}")
    if data.get("duration"):
        meta.append(f"**Длительность:** {data['duration'] // 60}:{data['duration'] % 60:02d}")
    if data.get("asr"):
        meta.append("**Расшифровка:** машинная (whisper)")
    meta.append(f"**Категория:** {analysis.get('category') or 'Другое'}")
    lines += [" · ".join(meta), ""]

    if data.get("image_warning"):
        lines += [f"**⚠️ Фото не сохранились:** {data['image_warning']}", ""]

    lines += _render_photos(data)

    if analysis.get("key_points"):
        lines += ["## Ключевые тезисы", ""]
        lines += [f"- {point}" for point in analysis["key_points"]]
        lines += [""]

    if analysis.get("useful_for"):
        lines += ["## Зачем мне это", "", analysis["useful_for"], ""]

    if analysis.get("quotes"):
        lines += ["## Цитаты", ""]
        for quote in analysis["quotes"]:
            lines += [f"> {quote}", ""]

    lines += ["## Ссылка", "", data.get("url") or item["url"], ""]

    body = (data.get("text") or "").strip()
    if body:
        if len(body) > 20000:
            body = body[:20000] + "\n\n… (текст обрезан при сохранении)"
        lines += ["## Текст", "", "<details><summary>Развернуть</summary>", "",
                  body, "", "</details>", ""]

    if tags:
        lines += ["## Теги", "", " ".join(f"#{t}" for t in tags), ""]
    return "\n".join(lines)


def write_card(item, data: dict, analysis: dict) -> Path:
    config.ensure_dirs()
    if item["file_path"]:
        path = config.LIBRARY_DIR / relative_path(item["file_path"])
    else:
        path = card_path(item["id"], analysis.get("title_ru") or data.get("title") or "item", item["created"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # Фото скачиваем до рендера: в карточку попадают только реально сохранённые файлы.
    data["image_paths"], errors = save_images(path, data.get("images") or [])
    if errors:
        data["image_warning"] = "; ".join(errors[:3])
        print(f"[{item['id']}] фото: {len(errors)} не скачалось: {errors[0]}", flush=True)
    path.write_text(render(item, data, analysis), encoding="utf-8")
    _ensure_readme()
    return path


def _ensure_readme() -> None:
    path = config.LIBRARY_DIR / "_README.md"
    if not path.exists():
        path.write_text(README.format(url=config.PUBLIC_URL), encoding="utf-8")


def trash_card(item) -> None:
    """Убирает карточку (и её фото) из vault в data/trash, чтобы их не было в Obsidian."""
    if not item["file_path"]:
        return
    path = resolve_path(item["file_path"])
    trash = config.DATA_DIR / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # /vault и /data — разные точки монтирования, rename между ними невозможен
        shutil.move(str(path), str(trash / f"{item['id']}-{path.name}"))
    photos = path.parent / config.IMAGES_DIR_NAME / path.stem
    if photos.exists():
        shutil.move(str(photos), str(trash / f"{item['id']}-{path.stem}-{config.IMAGES_DIR_NAME}"))


def rebuild_index(limit: int = 1000) -> Path:
    """Пересобирает `library/_index.md` из базы."""
    rows = db.list_items(status="done", limit=min(limit, 200))
    lines = [
        "---",
        "type: index",
        "status: active",
        "tags: [index, favorites]",
        "---",
        "# Избранное — указатель",
        "",
        f"Сгенерировано {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
        "Руками не редактировать, карточки лежат рядом по месяцам.",
        "",
        f"Всего: {len(rows)}. Веб-версия: {config.PUBLIC_URL}",
        "",
        "| Дата | Материал | Категория | Теги |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        if row["file_path"]:
            rel = Path(relative_path(row["file_path"])).with_suffix("").as_posix()
            link = f"[[{rel}|{_safe(row['title'] or row['url'])}]]"
        else:
            link = f"[{_safe(row['title'] or row['url'])}]({row['url']})"
        try:
            tags = ", ".join(json.loads(row["tags"] or "[]"))
        except ValueError:
            tags = ""
        date = (row["finished"] or row["created"] or "")[:10]
        lines.append(f"| {date} | {link} | {row['category'] or '—'} | {tags} |")
    path = config.LIBRARY_DIR / "_index.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
