"""Анализ содержимого дешёвой моделью через LiteLLM (OpenAI-совместимый API)."""
from __future__ import annotations

import json
import re

import httpx

from . import config

CATEGORIES = ["ИИ", "Разработка", "Дизайн", "Бизнес", "Маркетинг", "Обучение",
              "Здоровье", "Инструменты", "Развлечения", "Новости", "Другое"]

# Ниже этого порога содержания (символы без «Video by», хештегов и дублей) модель
# не спрашиваем: из одного заголовка она придумает правдоподобную, но вымышленную карточку.
MIN_SUBSTANCE = config.MIN_SUBSTANCE


def substance(text: str) -> int:
    from .extract import content_weight
    return content_weight(text)


SYSTEM = (
    "Ты — библиотекарь личных закладок. Тебе дают содержимое сохранённой ссылки "
    "(статья, пост, расшифровка видео). Твоя задача — сжать его до карточки, по которой "
    "человек через год вспомнит, что это и зачем ему нужно.\n\n"
    "Пиши по-русски, конкретно, без воды и без вступлений вроде «в этой статье».\n"
    "Верни СТРОГО один JSON-объект без markdown-обёртки, поля:\n"
    '{"title_ru": "заголовок по-русски, до 90 символов",'
    ' "summary": "2–4 предложения: о чём это и главный вывод",'
    ' "key_points": ["3–7 тезисов, каждый до 180 символов"],'
    ' "tags": ["3–7 тегов, строчными буквами, без решётки"],'
    f' "category": "одна из: {", ".join(CATEGORIES)}",'
    ' "quotes": ["0–2 короткие дословные цитаты, если они есть в тексте"],'
    ' "useful_for": "1–2 предложения: что с этим делать, когда пригодится",'
    ' "quality": 1}'
    "\nquality — субъективная ценность материала от 1 до 5."
)


def _strip_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json(text: str) -> dict:
    text = _strip_fence(text)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _clean_list(value, limit: int, max_len: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, str):
            continue
        item = re.sub(r"\s+", " ", item).strip(" -•·")
        if item and item not in result:
            result.append(item[:max_len])
        if len(result) >= limit:
            break
    return result


def _fallback(data: dict, reason: str) -> dict:
    text = (data.get("text") or "").strip()
    summary = re.sub(r"\s+", " ", text)[:400] or "Не удалось извлечь текст — откройте ссылку."
    hashes = [t.lower() for t in re.findall(r"#([\wА-Яа-яЁё-]{2,30})", text)]
    tags = list(dict.fromkeys(hashes))[:7]
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    # Причина сбоя извлечения (429, «нет видео») важнее, чем «пустое содержимое».
    details = " · ".join(part for part in (data.get("warning"), reason) if part)
    return {
        "title_ru": (data.get("title") or "")[:90],
        "summary": summary,
        "key_points": [],
        "tags": tags,
        "category": "Другое",
        "quotes": [],
        "useful_for": "",
        "quality": 0,
        "lang": "ru" if cyr > len(text) * 0.15 else "en",
        "model": "fallback",
        "tokens": 0,
        "degraded": True,
        "warning": details,
    }


def _thin_card(data: dict, reason: str) -> dict:
    """Карточка без модели: контента слишком мало, достраивать его — выдумывать."""
    text = (data.get("text") or "").strip()
    caption = re.sub(r"\s+", " ", text)[:400]
    hashes = [t.lower() for t in re.findall(r"#([\wА-Яа-яЁё-]{2,30})", text)]
    notes = []
    if caption:
        notes.append(f"Есть только подпись: {caption}")
    notes.append("Что внутри самого материала (видео/карусели), из ссылки не видно — "
                 "текст там не отдаётся. Посмотрите источник или добавьте ссылку ещё раз "
                 "с комментарием о сути.")
    return {
        "title_ru": ((data.get("title") or "").strip()[:90]
                     or f"{data.get('site') or 'Источник'}: без заголовка"),
        "summary": " ".join(notes)[:1200],
        "key_points": [],
        "tags": list(dict.fromkeys(hashes))[:7],
        "category": "Другое",
        "quotes": [],
        "useful_for": "Только как напоминание открыть ссылку: по тексту заметку не собрать.",
        "quality": 0,
        "lang": "ru" if re.search(r"[А-Яа-яЁё]", text) else "en",
        "model": "no-text",
        "tokens": 0,
        "degraded": True,
        "warning": " · ".join(part for part in (data.get("warning"), reason) if part),
    }


def analyze(data: dict) -> dict:
    """Возвращает нормализованный анализ; при сбое модели — карточку без LLM."""
    text = (data.get("text") or "").strip()
    if not text:
        return _fallback(data, "пустое содержимое")
    if substance(text) < MIN_SUBSTANCE:
        return _thin_card(data, f"мало текста ({substance(text)} символов содержания)")

    payload_text = text[: config.LLM_MAX_INPUT]
    duration = int(data.get("duration") or 0)
    header = (
        f"Ссылка: {data.get('url', '')}\n"
        f"Источник: {data.get('site', '')} ({data.get('kind', '')})\n"
        f"Автор: {data.get('author', '') or '—'}\n"
        f"Дата: {data.get('published', '') or '—'}\n"
        f"Длительность: {duration} сек\n\n"
        f"Содержимое:\n{payload_text}"
    )
    body = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": header}],
        "temperature": 0.2,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    try:
        response = httpx.post(
            f"{config.LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
            json=body,
            timeout=config.LLM_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
        message = result["choices"][0]["message"]
        content = message.get("content") or message.get("reasoning_content") or ""
        parsed = _parse_json(content)
        tokens = int((result.get("usage") or {}).get("total_tokens") or 0)
    except Exception as exc:  # noqa: BLE001 — любая ошибка API даёт деградированную карточку
        return _fallback(data, f"LLM: {str(exc)[:300]}")

    analysis = {
        "title_ru": str(parsed.get("title_ru") or data.get("title") or "")[:120],
        "summary": re.sub(r"\s+", " ", str(parsed.get("summary") or ""))[:1200],
        "key_points": _clean_list(parsed.get("key_points"), 7, 300),
        "tags": [re.sub(r"\s+", "-", t.lower().lstrip("#"))[:40]
                 for t in _clean_list(parsed.get("tags"), 7, 40)],
        "category": parsed.get("category") if parsed.get("category") in CATEGORIES else "Другое",
        "quotes": _clean_list(parsed.get("quotes"), 2, 400),
        "useful_for": re.sub(r"\s+", " ", str(parsed.get("useful_for") or ""))[:600],
        "quality": max(0, min(5, int(parsed.get("quality") or 0))) if str(parsed.get("quality", "")).isdigit() else 0,
        "lang": "ru" if re.search(r"[А-Яа-яЁё]", payload_text[:2000]) else "en",
        "model": config.LLM_MODEL,
        "tokens": tokens,
        "degraded": False,
        "warning": "",
    }
    if not analysis["summary"]:
        analysis["summary"] = re.sub(r"\s+", " ", text)[:400]
    return analysis
