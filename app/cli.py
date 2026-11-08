"""Небольшой CLI для отладки и ручных операций."""
from __future__ import annotations

import argparse
import json

from . import db, extract, vault


def main() -> None:
    parser = argparse.ArgumentParser(prog="fav", description="Favourites: закладки из Telegram")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="добавить ссылку в очередь")
    add.add_argument("url")
    add.add_argument("--note", default=None, help="подпись/контекст к ссылке")
    add.add_argument("--chat", type=int, default=None)

    show = sub.add_parser("list", help="показать последние записи")
    show.add_argument("--limit", type=int, default=20)
    show.add_argument("--status", default="all")

    item = sub.add_parser("show", help="одна запись целиком")
    item.add_argument("id", type=int)

    sub.add_parser("stats", help="счётчики")
    sub.add_parser("rebuild", help="пересобрать library/_index.md")

    args = parser.parse_args()
    if args.command == "add":
        row, created = db.add(args.url, chat_id=args.chat, note=args.note,
                              kind=extract.kind_of(args.url))
        print(("добавлено" if created else "уже есть"), row["id"], row["url"], row["status"])
    elif args.command == "list":
        for row in db.list_items(status=args.status, limit=args.limit):
            print(f'{row["id"]:>4}  {row["status"]:<10} {row["kind"] or "-":<10} '
                  f'{(row["title"] or row["url"])[:70]}')
    elif args.command == "show":
        row = db.get(args.id)
        print(json.dumps(dict(row) if row else {}, ensure_ascii=False, indent=2))
    elif args.command == "stats":
        print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    elif args.command == "rebuild":
        print(vault.rebuild_index())


if __name__ == "__main__":
    main()
