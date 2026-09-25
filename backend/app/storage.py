"""
Storage
-------
Durable persistence for narrated stories: SQLite via the stdlib `sqlite3`
module (no new dependency), one connection per call so we don't have to
reason about sharing a connection across FastAPI's threadpool.

This is the one place in the app that survives a process restart - the
in-memory `_worlds` dict in memory.py is fine for the ephemeral fiction
pipeline, but narrated stories are personal data the user expects to still
be there after closing and reopening the app (see main.py's /narration
routes), so they go through here instead.

The database file lives in backend/data/ (created on first use), which is
git-ignored - it will contain real transcripts and, once audio is wired up,
real recordings, so it must never be committed.
"""

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

DATA_DIR = os.environ.get(
    "NARRAFLOW_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data")
)
DB_PATH = os.path.join(DATA_DIR, "narraflow.db")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Creates the data dir, audio dir, and stories table if missing. Safe to call every startup."""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stories (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                transcript TEXT NOT NULL DEFAULT '',
                story_text TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL DEFAULT 'recording',
                duration_seconds REAL,
                audio_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_story(language: str = "en", story_id: Optional[str] = None) -> dict:
    story_id = story_id or str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO stories (id, language, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (story_id, language, now, now),
        )
    return get_story(story_id)


def get_story(story_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    return dict(row) if row else None


def list_stories() -> list:
    """Newest first. Returns library-card fields only (no full transcript/story_text)."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, title, story_text, transcript, language, status,
                   duration_seconds, audio_path, created_at, updated_at
            FROM stories ORDER BY created_at DESC
            """
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        preview_source = d.pop("story_text") or d.pop("transcript") or ""
        d["preview"] = preview_source.strip()[:140]
        d["has_audio"] = bool(d.pop("audio_path"))
        out.append(d)
    return out


ALLOWED_FIELDS = {
    "title", "transcript", "story_text", "status", "duration_seconds", "audio_path",
}


def update_story(story_id: str, **fields) -> Optional[dict]:
    """Partial update of allowed fields; always bumps updated_at. No-op fields are ignored."""
    fields = {k: v for k, v in fields.items() if k in ALLOWED_FIELDS and v is not None}
    if not fields:
        return get_story(story_id)
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as conn:
        conn.execute(
            f"UPDATE stories SET {set_clause} WHERE id = ?",
            (*fields.values(), story_id),
        )
    return get_story(story_id)


def append_transcript(story_id: str, text: str) -> Optional[dict]:
    """Appends a finalized segment to the durable transcript immediately - called before any
    story-generation attempt, so a narration is never lost even if that call later fails."""
    story = get_story(story_id)
    if story is None:
        return None
    joined = f"{story['transcript']} {text}".strip() if story["transcript"] else text
    return update_story(story_id, transcript=joined)
