"""Воркер очереди: извлечь → проанализировать → записать карточку → уведомить."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from . import analyze, config, db, extract, telegram, vault

STOP = False


def _stop(*_args) -> None:
    global STOP
    STOP = True


def process(item) -> str:
    """Обрабатывает одну запись; возвращает статус."""
    db.set_fields(item["id"], status="processing", error=None)
    data = extract.fetch(item["url"], item["note"])
    analysis = analyze.analyze(data)
    path = vault.write_card(item, data, analysis)

    title = analysis.get("title_ru") or data.get("title") or item["url"]
    db.finish(
        item["id"],
        kind=data.get("kind"),
        title=title[:300],
        author=(data.get("author") or "")[:200],
        site=(data.get("site") or "")[:120],
        published=str(data.get("published") or "")[:32],
        duration=int(data.get("duration") or 0),
        lang=analysis.get("lang"),
        category=analysis.get("category"),
        summary=analysis.get("summary"),
        key_points=json.dumps(analysis.get("key_points") or [], ensure_ascii=False),
        quotes=json.dumps(analysis.get("quotes") or [], ensure_ascii=False),
        tags=json.dumps(analysis.get("tags") or [], ensure_ascii=False),
        useful_for=analysis.get("useful_for"),
        quality=int(analysis.get("quality") or 0),
        transcript_chars=len(data.get("transcript") or ""),
        raw_text=(data.get("text") or "")[: config.SEARCH_TEXT_LIMIT],
        thumb=(data.get("thumb") or "")[:500],
        file_path=path.relative_to(config.LIBRARY_DIR).as_posix(),
        model=analysis.get("model"),
        tokens=int(analysis.get("tokens") or 0),
        error=analysis.get("warning") or data.get("image_warning") or None,
    )
    vault.rebuild_index()
    row = db.get(item["id"])
    if row:
        telegram.notify_done(row)
    return "done"


def _run(item) -> None:
    try:
        process(item)
        print(f"[{item['id']}] ok: {item['url']}", flush=True)
    except Exception as exc:  # noqa: BLE001 — любая ошибка уходит в очередь/статус
        status, delay = db.fail(item["id"], str(exc))
        if status == "failed":
            print(f"[{item['id']}] failed: {exc}", file=sys.stderr, flush=True)
            row = db.get(item["id"])
            if row:
                telegram.notify_failed(row, str(exc))
        else:
            print(f"[{item['id']}] retry через {delay}с: {exc}", file=sys.stderr, flush=True)


def loop(interval: float = 3.0) -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"worker: запущен, база {config.DB_PATH}", flush=True)
    while not STOP:
        item = db.claim_next()
        if item:
            _run(item)
            continue
        time.sleep(interval)
    print("worker: остановлен", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Обработчик очереди Favourites")
    parser.add_argument("--once", action="store_true", help="обработать очередь и выйти")
    parser.add_argument("--id", type=int, help="обработать конкретную запись")
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()
    if args.id:
        item = db.get(args.id)
        if not item:
            raise SystemExit(f"нет записи {args.id}")
        if item["status"] != "queued":
            db.requeue(args.id)
            item = db.get(args.id)
        _run(item)
    elif args.once:
        while (item := db.claim_next()) is not None:
            _run(item)
    else:
        loop(args.interval)


if __name__ == "__main__":
    main()
