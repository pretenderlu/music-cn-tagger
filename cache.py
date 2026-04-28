"""SQLite-backed key-value cache for encyclopedia API lookups.

Used by encyclopedia.py to avoid hitting MusicBrainz / Wikidata
repeatedly for the same artist when scanning many albums by the same
performer. Values are JSON-serialized; entries older than 30 days are
treated as misses.

Path resolution order:
  1. MUSIC_CN_TAGGER_CACHE env var (full path to .db file)
  2. ~/.music-cn-tagger/cache.db (default)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

CACHE_TTL_SECONDS = 30 * 24 * 3600

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None


def _db_path() -> Path:
    env = os.environ.get("MUSIC_CN_TAGGER_CACHE")
    if env:
        return Path(env)
    return Path.home() / ".music-cn-tagger" / "cache.db"


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS cache (
            ns TEXT NOT NULL,
            k  TEXT NOT NULL,
            v  TEXT NOT NULL,
            ts INTEGER NOT NULL,
            PRIMARY KEY (ns, k)
        );
        """
    )
    _CONN = c
    return c


def get(ns: str, key: str) -> Any:
    """Return cached value for (ns, key), or None if missing/expired."""
    with _LOCK:
        cur = _conn().execute(
            "SELECT v, ts FROM cache WHERE ns = ? AND k = ?", (ns, key)
        )
        row = cur.fetchone()
    if not row:
        return None
    v, ts = row
    if time.time() - ts > CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return None


def set(ns: str, key: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    with _LOCK:
        _conn().execute(
            "INSERT OR REPLACE INTO cache (ns, k, v, ts) VALUES (?, ?, ?, ?)",
            (ns, key, payload, int(time.time())),
        )


def stats() -> dict:
    """Cache size + breakdown by namespace. Useful for debugging."""
    with _LOCK:
        rows = _conn().execute(
            "SELECT ns, COUNT(*) FROM cache GROUP BY ns"
        ).fetchall()
    return {"path": str(_db_path()), "by_ns": dict(rows)}
