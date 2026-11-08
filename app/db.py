"""SQLite-очередь и каталог закладок.

Таблица `items` — единственный источник истины о состоянии обработки.
Markdown-карточка в vault — долговременное хранилище текста.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL,
    url_key      TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'queued',
    kind         TEXT,
    title        TEXT,
    author       TEXT,
    site         TEXT,
    published    TEXT,
    duration     INTEGER,
    lang         TEXT,
    category     TEXT,
    summary      TEXT,
    key_points   TEXT,
    quotes       TEXT,
    tags         TEXT,
    useful_for   TEXT,
    quality      INTEGER,
    transcript_chars INTEGER DEFAULT 0,
    raw_text     TEXT,
    thumb        TEXT,
    file_path    TEXT,
    model        TEXT,
    tokens       INTEGER DEFAULT 0,
    error        TEXT,
    attempts     INTEGER DEFAULT 0,
    chat_id      INTEGER,
    message_id   INTEGER,
    note         TEXT,
    created      TEXT NOT NULL,
    updated      TEXT NOT NULL,
    started      TEXT,
    finished     TEXT,
    retry_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status  ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created DESC);
CREATE INDEX IF NOT EXISTS idx_items_kind    ON items(kind);
"""

TRACKING = re.compile(
    r"^(utm_|igsh|ig_|fbclid|gclid|yclid|si$|ref$|ref_src$|ref_url$|share_id$|mibextid$|feature$)",
    re.I,
)


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_after(seconds: int) -> str:
    moment = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_url(url: str) -> str:
    """Убирает хвосты трекинга, чтобы одна и та же ссылка не дублировалась."""
    url = url.strip().strip("<>\"'«».,)")
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    if host in {"instagram.com", "instagr.am"}:
        # У Instagram все query-параметры — маркеры шэринга (igsh, psln, dlrf, xtok…),
        # идентификатор поста живёт в пути; /reels/ и /reel/ — одна и та же страница.
        path = re.sub(r"^/reels/", "/reel/", path)
        return urlunsplit((parts.scheme.lower(), "www.instagram.com", path, "", ""))
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def url_key(url: str) -> str:
    return normalize_url(url)


def _row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    cur = conn.execute(sql, params)
    return cur.fetchone()


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "retry_at" not in columns:  # миграция баз, созданных до backoff
        conn.execute("ALTER TABLE items ADD COLUMN retry_at TEXT")
    return conn


def add(url: str, chat_id: int | None = None, message_id: int | None = None,
        note: str | None = None, kind: str | None = None) -> tuple[sqlite3.Row, bool]:
    """Добавляет ссылку в очередь. Возвращает (запись, создана_ли)."""
    key = url_key(url)
    ts = now()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _row(conn, "SELECT * FROM items WHERE url_key = ?", (key,))
        if existing:
            conn.execute("COMMIT")
            return existing, False
        cur = conn.execute(
            "INSERT INTO items (url, url_key, status, kind, note, chat_id, message_id, created, updated)"
            " VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
            (normalize_url(url), key, kind, note, chat_id, message_id, ts, ts),
        )
        item_id = cur.lastrowid
        row = _row(conn, "SELECT * FROM items WHERE id = ?", (item_id,))
        conn.execute("COMMIT")
        return row, True
    finally:
        conn.close()


def claim_next() -> sqlite3.Row | None:
    """Атомарно забирает одну задачу из очереди, уважая паузу после сбоя."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, "SELECT * FROM items WHERE status = 'queued'"
                         " AND (retry_at IS NULL OR retry_at <= ?) ORDER BY id LIMIT 1", (now(),))
        if not row:
            conn.execute("COMMIT")
            return None
        ts = now()
        conn.execute(
            "UPDATE items SET status='processing', started=?, updated=?, retry_at=NULL,"
            " attempts=attempts+1 WHERE id=?",
            (ts, ts, row["id"]),
        )
        row = _row(conn, "SELECT * FROM items WHERE id = ?", (row["id"],))
        conn.execute("COMMIT")
        return row
    finally:
        conn.close()


def set_fields(item_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = connect()
    try:
        conn.execute(f"UPDATE items SET {cols} WHERE id = ?", (*fields.values(), item_id))
    finally:
        conn.close()


def finish(item_id: int, **fields) -> None:
    fields.setdefault("status", "done")
    fields.setdefault("finished", now())
    fields.setdefault("error", None)
    set_fields(item_id, **fields)


def fail(item_id: int, error: str) -> tuple[str, int]:
    """Помечает попытку неудачной. Возвращает (статус, секунд_до_повтора)."""
    conn = connect()
    try:
        row = _row(conn, "SELECT attempts FROM items WHERE id = ?", (item_id,))
        attempts = row["attempts"] if row else 0
        if attempts >= config.MAX_ATTEMPTS:
            ts = now()
            conn.execute(
                "UPDATE items SET status='failed', error=?, updated=?, finished=?, retry_at=NULL WHERE id=?",
                (error[:2000], ts, ts, item_id),
            )
            return "failed", 0
        # пауза растёт: 60с после первой неудачи, 5 минут после второй
        delay = 60 if attempts <= 1 else 300
        conn.execute(
            "UPDATE items SET status='queued', error=?, updated=?, retry_at=? WHERE id=?",
            (error[:2000], now(), _iso_after(delay), item_id),
        )
        return "queued", delay
    finally:
        conn.close()


def delete(item_id: int) -> bool:
    """Мягкое удаление: запись уходит из выдачи, файл переносит vault."""
    row = get(item_id)
    if not row:
        return False
    set_fields(item_id, status="deleted")
    return True


def requeue(item_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE items SET status='queued', attempts=0, error=NULL, updated=?, retry_at=NULL,"
            " started=NULL, finished=NULL WHERE id=?",
            (now(), item_id),
        )
    finally:
        conn.close()


def get(item_id: int) -> sqlite3.Row | None:
    conn = connect()
    try:
        return _row(conn, "SELECT * FROM items WHERE id = ?", (item_id,))
    finally:
        conn.close()


def pending_count() -> int:
    conn = connect()
    try:
        row = _row(conn, "SELECT COUNT(*) AS n FROM items WHERE status IN ('queued','processing')")
        return int(row["n"]) if row else 0
    finally:
        conn.close()


ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def list_items(q: str = "", tag: str = "", category: str = "", source: str = "",
               status: str = "done", limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
    where, params = [], []
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if tag:
        where.append("tags LIKE ? ESCAPE '\\'")
        params.append(f'%"{tag.translate(ESCAPE)}"%')
    if category:
        where.append("category = ?")
        params.append(category)
    if source:
        where.append("kind = ?")
        params.append(source)
    if q:
        where.append("(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\'"
                     " OR tags LIKE ? ESCAPE '\\' OR raw_text LIKE ? ESCAPE '\\'"
                     " OR author LIKE ? ESCAPE '\\')")
        like = f"%{q.translate(ESCAPE)}%"
        params.extend([like] * 5)
    sql = "SELECT * FROM items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(finished, created) DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(limit, 200)), max(0, offset)])
    conn = connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def tags_cloud() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT tags FROM items WHERE status='done' AND tags IS NOT NULL").fetchall()
    finally:
        conn.close()
    counts: dict[str, int] = {}
    for row in rows:
        try:
            for tag in json.loads(row["tags"] or "[]"):
                counts[tag] = counts.get(tag, 0) + 1
        except (TypeError, ValueError):
            continue
    return [{"tag": t, "count": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def facets() -> dict:
    conn = connect()
    try:
        categories = conn.execute(
            "SELECT COALESCE(NULLIF(category,''),'Другое') AS name, COUNT(*) AS n"
            " FROM items WHERE status='done' GROUP BY name ORDER BY n DESC"
        ).fetchall()
        sources = conn.execute(
            "SELECT COALESCE(NULLIF(kind,''),'other') AS name, COUNT(*) AS n"
            " FROM items WHERE status='done' GROUP BY name ORDER BY n DESC"
        ).fetchall()
        return {
            "categories": [{"name": r["name"], "count": r["n"]} for r in categories],
            "sources": [{"name": r["name"], "count": r["n"]} for r in sources],
        }
    finally:
        conn.close()


def stats() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status").fetchall()
        done = conn.execute("SELECT COUNT(*) AS n FROM items WHERE status='done'").fetchone()
        tokens = conn.execute("SELECT COALESCE(SUM(tokens),0) AS n FROM items").fetchone()
        last = conn.execute("SELECT COALESCE(MAX(finished),'') AS t FROM items").fetchone()
        return {
            "by_status": {r["status"]: r["n"] for r in rows},
            "total_done": int(done["n"]) if done else 0,
            "tokens": int(tokens["n"]) if tokens else 0,
            "last_finished": last["t"] if last else "",
        }
    finally:
        conn.close()
