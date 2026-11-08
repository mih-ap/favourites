"""Веб-справочник избранного: stdlib HTTP-сервер + статический SPA."""
from __future__ import annotations

import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import config, db, vault

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
}

MEDIA_TYPES = {suffix: CONTENT_TYPES[suffix] for suffix in vault.IMAGE_SUFFIXES}


def inline_media(content: str, file_path: str) -> str:
    """Относительные ссылки на фото карточки → URL /media/, иначе браузер их не найдёт.

    В заметке картинки лежат относительно самой карточки (`attachments/<имя>/…`),
    так их видит Obsidian; веб-страница живёт на другом пути и требует префикс
    каталога месяца внутри library.
    """
    if not content or "](attachments/" not in content:
        return content
    folder = Path(vault.relative_path(file_path)).parent.as_posix().strip("/.")
    prefix = f"/media/{folder}/" if folder and folder != "." else "/media/"
    return content.replace("](attachments/", f"]({prefix}attachments/")


def public_item(row, with_content: bool = False) -> dict:
    item = {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"] or row["url"],
        "summary": row["summary"] or "",
        "kind": row["kind"] or "",
        "site": row["site"] or "",
        "author": row["author"] or "",
        "published": row["published"] or "",
        "saved": (row["finished"] or row["created"] or "")[:10],
        "category": row["category"] or "Другое",
        "quality": row["quality"] or 0,
        "duration": row["duration"] or 0,
        "lang": row["lang"] or "",
        "status": row["status"],
        "tags": [],
        "file": vault.relative_path(row["file_path"]) if row["file_path"] else "",
        "vault": config.VAULT_NAME,
    }
    try:
        item["tags"] = json.loads(row["tags"] or "[]")
    except ValueError:
        pass
    if with_content:
        item["key_points"] = json.loads(row["key_points"]) if row["key_points"] else []
        item["quotes"] = json.loads(row["quotes"]) if row["quotes"] else []
        item["useful_for"] = row["useful_for"] or ""
        content = ""
        if row["file_path"]:
            path = vault.resolve_path(row["file_path"])
            if path.exists():
                text = path.read_text(encoding="utf-8")
                content = text.split("---", 2)[-1].strip() if text.startswith("---") else text
                content = inline_media(content, row["file_path"])
        item["content"] = content
    return item


class Handler(BaseHTTPRequestHandler):
    server_version = "Favourites/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}

        if path == "/api/items":
            self.api_items(query)
        elif path == "/api/item":
            item_id = int(query.get("id", "0") or 0)
            row = db.get(item_id)
            if not row:
                self.json({"error": "not found"}, 404)
                return
            self.json(public_item(row, with_content=True))
        elif path == "/api/tags":
            self.json({"tags": db.tags_cloud()})
        elif path == "/api/facets":
            self.json(db.facets())
        elif path == "/api/stats":
            self.json(db.stats())
        elif path == "/api/health":
            self.json({"ok": True, "db": str(config.DB_PATH), "library": str(config.LIBRARY_DIR),
                       "stats": db.stats()})
        elif path.startswith("/static/"):
            self.serve_static(path.removeprefix("/static/"))
        elif path.startswith("/media/"):
            self.serve_media(path.removeprefix("/media/"))
        elif path in {"/", "/index.html"}:
            self.serve_static("index.html")
        elif path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

    def api_items(self, query: dict) -> None:
        try:
            limit = max(1, min(int(query.get("limit", 40)), 200))
            offset = max(0, int(query.get("offset", 0)))
        except ValueError:
            limit, offset = 40, 0
        rows = db.list_items(
            q=query.get("q", "").strip(),
            tag=query.get("tag", "").strip(),
            category=query.get("category", "").strip(),
            source=query.get("source", "").strip(),
            status=query.get("status", "done"),
            limit=limit + 1,
            offset=offset,
        )
        self.json({
            "items": [public_item(row) for row in rows[:limit]],
            "has_more": len(rows) > limit,
            "offset": offset,
        })

    def serve_static(self, relative: str) -> None:
        target = (config.STATIC_DIR / relative).resolve()
        root = config.STATIC_DIR.resolve()
        if (root not in target.parents and target != root) or not target.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        suffix = target.suffix.lower()
        ctype = CONTENT_TYPES.get(suffix) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        cache = "no-cache" if suffix in {".html", ".js", ".css"} else "public, max-age=86400"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype, cache)

    def serve_media(self, relative: str) -> None:
        """Фото карточек из library/ — только известные форматы и только внутри vault."""
        root = config.LIBRARY_DIR.resolve()
        target = (root / relative).resolve()
        suffix = target.suffix.lower()
        if (root not in target.parents or not target.is_file()
                or suffix not in MEDIA_TYPES):
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        self._send(HTTPStatus.OK, target.read_bytes(), MEDIA_TYPES[suffix],
                   "public, max-age=604800")

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if parts.path == "/api/requeue":
            item_id = int(query.get("id", "0") or 0)
            if not db.get(item_id):
                self.json({"error": "not found"}, 404)
                return
            db.requeue(item_id)
            self.json({"ok": True, "id": item_id})
        elif parts.path == "/api/delete":
            item_id = int(query.get("id", "0") or 0)
            row = db.get(item_id)
            if not row:
                self.json({"error": "not found"}, 404)
                return
            vault.trash_card(row)
            db.delete(item_id)
            vault.rebuild_index()
            self.json({"ok": True, "id": item_id})
        elif parts.path == "/api/rebuild":
            self.json({"ok": True, "index": str(vault.rebuild_index())})
        else:
            self.json({"error": "not found"}, 404)


def main() -> None:
    config.ensure_dirs()
    server = ThreadingHTTPServer((config.WEB_HOST, config.WEB_PORT), Handler)
    server.daemon_threads = True
    print(f"web: http://{config.WEB_HOST}:{config.WEB_PORT} → {config.LIBRARY_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
