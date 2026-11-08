"""Telegram-бот: принимает ссылки, ставит в очередь, отвечает на команды."""
from __future__ import annotations

import json
import sys
import time

import httpx

from . import config, db, extract, telegram

HELP = (
    "Пришлите ссылку на статью, пост или видео — сохраню в избранное и разберу.\n\n"
    "Команды:\n"
    "/list — последние 10 материалов\n"
    "/stats — что в очереди и сколько всего\n"
    "/retry N — повторить обработку записи N\n"
    "/help — эта подсказка"
)


def _allowed(chat_id: int) -> bool:
    return not config.ALLOWED_CHATS or chat_id in config.ALLOWED_CHATS


def _reply(chat_id: int, text: str) -> None:
    telegram.send(chat_id, text)


def handle_message(message: dict) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []

    if not _allowed(chat_id):
        _reply(chat_id, f"Этот чат не в списке разрешённых.\nВаш chat_id: <code>{chat_id}</code>\n"
                        "Добавьте его в FAV_ALLOWED_CHATS в .env и перезапустите бота.")
        return

    if text.startswith("/"):
        command = text.split()[0].split("@")[0]
        if command in {"/start", "/help"}:
            _reply(chat_id, HELP)
        elif command == "/stats":
            stats = db.stats()
            by = stats["by_status"]
            lines = [f"В справочнике: {stats['total_done']} материалов",
                     f"В очереди: {by.get('queued', 0)} · в работе: {by.get('processing', 0)}"
                     f" · с ошибкой: {by.get('failed', 0)}"]
            failed = db.list_items(status="failed", limit=3)
            for row in failed:
                lines.append(f"⚠️ #{row['id']} {row['url'][:60]}\n"
                             f"<code>{(row['error'] or '')[:120]}</code> · /retry {row['id']}")
            _reply(chat_id, "\n".join(lines))
        elif command == "/list":
            rows = db.list_items(status="done", limit=10)
            if not rows:
                _reply(chat_id, "Пока пусто.")
                return
            lines = ["<b>Последние материалы</b>"]
            for row in rows:
                title = (row["title"] or row["url"])[:80].replace("<", "&lt;")
                lines.append(f'• <a href="{config.PUBLIC_URL}/#id={row["id"]}">{title}</a>')
            _reply(chat_id, "\n".join(lines))
        elif command == "/retry":
            parts = text.split()
            if len(parts) < 2 or not parts[1].isdigit():
                _reply(chat_id, "Использование: <code>/retry 12</code>")
                return
            item_id = int(parts[1])
            row = db.get(item_id)
            if not row:
                _reply(chat_id, f"Записи {item_id} нет.")
                return
            db.requeue(item_id)
            _reply(chat_id, f"♻️ Поставил в очередь: {item_id}")
        else:
            _reply(chat_id, HELP)
        return

    urls = extract.extract_urls(text, entities)
    if not urls:
        _reply(chat_id, "Нужна ссылка: https://… — пришлите её сообщением или через «Поделиться».")
        return

    note = text.strip() if text.strip() and text.strip() not in urls else None
    reply_lines = []
    created_count = 0
    for url in urls[:10]:
        item, created = db.add(url, chat_id=chat_id, message_id=message.get("message_id"),
                               note=note, kind=extract.kind_of(url))
        if created:
            created_count += 1
            reply_lines.append(f"📥 Принял: {url}")
        elif item["status"] == "done":
            reply_lines.append(f"🔁 Уже в избранном: «{(item['title'] or '')[:80]}»\n"
                               f"{config.PUBLIC_URL}/#id={item['id']}")
        elif item["status"] == "processing":
            reply_lines.append(f"⏳ Уже в работе: {url}")
        elif item["status"] == "failed":
            reply_lines.append(f"🔁 Эта ссылка падала раньше, повторю: {url}")
            db.requeue(item["id"])
            created_count += 1
        else:
            reply_lines.append(f"🔁 Уже в очереди: {url}")
    pending = db.pending_count()
    if created_count:
        reply_lines.append("Разберу и пришлю ссылку на карточку.")
        if pending > created_count:
            reply_lines.append(f"Перед ней ещё {pending - created_count} в очереди.")
    _reply(chat_id, "\n\n".join(reply_lines))


def run_polling() -> None:
    if not config.TELEGRAM_TOKEN:
        raise SystemExit("FAV_TELEGRAM_TOKEN не задан — положите токен в .env")
    offset = 0
    print("bot: polling started", flush=True)
    while True:
        try:
            with httpx.Client(timeout=90) as client:
                response = client.get(
                    f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getUpdates",
                    params={"offset": offset, "timeout": 50,
                            "allowed_updates": json.dumps(["message", "channel_post"])},
                )
            payload = response.json()
            if not payload.get("ok"):
                print(f"bot: getUpdates error: {payload}", file=sys.stderr, flush=True)
                time.sleep(5)
                continue
            for update in payload.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                message = update.get("message") or update.get("channel_post")
                if message:
                    try:
                        handle_message(message)
                    except Exception as exc:  # noqa: BLE001 — бот не должен падать от одного сообщения
                        print(f"bot: handle error: {exc}", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 — сеть/таймауты
            print(f"bot: poll error: {exc}", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    run_polling()
