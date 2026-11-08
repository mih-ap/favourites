"""Тонкая обёртка над Telegram Bot API (без сторонних SDK)."""
from __future__ import annotations

import html

import httpx

from . import config


def api(method: str, timeout: float = 70, **params):
    if not config.TELEGRAM_TOKEN:
        raise RuntimeError("FAV_TELEGRAM_TOKEN не задан")
    response = httpx.post(
        f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/{method}",
        json=params,
        timeout=timeout,
    )
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method}: {payload.get('description')}")
    return payload.get("result")


def send(chat_id: int, text: str, preview: bool = False) -> None:
    try:
        api("sendMessage", chat_id=chat_id, text=text[:4000],
            parse_mode="HTML", disable_web_page_preview=not preview)
    except Exception:  # noqa: BLE001 — уведомление не должно ломать обработку
        pass


def _link(item_id: int) -> str:
    return f"{config.PUBLIC_URL}/#id={item_id}"


def notify_done(item) -> None:
    if not item["chat_id"]:
        return
    title = html.escape(item["title"] or item["url"])
    parts = [f"✅ <b>{title}</b>"]
    if item["summary"]:
        parts.append(html.escape(item["summary"][:500]))
    tags = []
    try:
        import json
        tags = json.loads(item["tags"] or "[]")
    except ValueError:
        pass
    if tags:
        parts.append(" ".join(f"#{html.escape(t)}" for t in tags) + f" · {item['category'] or ''}")
    if not item["quality"] and item["error"]:
        parts.append(f"⚠️ Заметка неполная: <code>{html.escape(item['error'][:200])}</code>")
    parts.append(f'<a href="{_link(item["id"])}">Открыть в справочнике</a>')
    send(item["chat_id"], "\n\n".join(parts))


def notify_failed(item, error: str) -> None:
    if not item["chat_id"]:
        return
    send(item["chat_id"],
         f"⚠️ Не получилось разобрать <a href=\"{html.escape(item['url'])}\">ссылку</a>\n"
         f"<code>{html.escape(error[:300])}</code>\n\n"
         f"Можно повторить позже: <code>/retry {item['id']}</code>")
