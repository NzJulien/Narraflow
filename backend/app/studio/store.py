"""Durable JSON-document store for studio stories (SQLite, stdlib only).

Each story is one JSON document; concurrent writers (voice tool calls and
background image jobs) are serialised with a process-wide lock and every
write bumps `version` so the live-update stream can tell something changed.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, List, Optional

from .models import Story, now

_lock = threading.RLock()


def data_dir() -> str:
    base = os.environ.get(
        "NARRAFLOW_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "data")
    )
    os.makedirs(base, exist_ok=True)
    return os.path.abspath(base)


def _db_path() -> str:
    return os.path.join(data_dir(), "studio.db")


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path(), timeout=10)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS studio_stories ("
            " id TEXT PRIMARY KEY, doc TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def save(story: Story) -> Story:
    with _lock:
        story.version += 1
        story.updated_at = now()
        with _conn() as c:
            c.execute(
                "INSERT INTO studio_stories(id, doc, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET doc=excluded.doc, updated_at=excluded.updated_at",
                (story.id, story.model_dump_json(), story.updated_at),
            )
    return story


def get(story_id: str) -> Optional[Story]:
    with _lock, _conn() as c:
        row = c.execute("SELECT doc FROM studio_stories WHERE id=?", (story_id,)).fetchone()
    return Story.model_validate_json(row[0]) if row else None


def update(story_id: str, mutate: Callable[[Story], None]) -> Optional[Story]:
    """Read-modify-write under the lock so concurrent tool calls and image
    jobs can't clobber each other."""
    with _lock:
        story = get(story_id)
        if story is None:
            return None
        mutate(story)
        return save(story)


def list_recent(limit: int = 20) -> List[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT doc FROM studio_stories ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for (doc,) in rows:
        s = Story.model_validate_json(doc)
        out.append({"id": s.id, "title": s.title, "scenes": len(s.scenes), "status": s.status,
                    "updated_at": s.updated_at})
    return out
