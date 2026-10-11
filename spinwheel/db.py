"""SQLite storage. Every pull is appended; nothing is ever updated or deleted.

Layout:
  pulls             one row per ingest run (when, from what command, content hash)
  raw_payloads      the exact bytes that came back, so any parser bug can be fixed later
  videos, channels  identity only (id, first seen); everything descriptive lives in snapshots
  video_snapshots   one row per video per pull: title, views, likes, ... as of that pull
  channel_snapshots one row per channel per pull: subscribers, total views, ...
  search_suggestions YouTube autocomplete results per fetch, so new phrases can be spotted over time

Snapshot, payload and pull tables are protected by triggers that abort UPDATE and
DELETE, so "not overwritten" is enforced by the database, not by convention.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB = "spinwheel.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pulls (
    id          INTEGER PRIMARY KEY,
    pulled_at   TEXT NOT NULL,           -- ISO-8601 UTC, when the data was fetched
    ingested_at TEXT NOT NULL,           -- ISO-8601 UTC, when we stored it
    source      TEXT NOT NULL,           -- 'yt-dlp' | 'youtube-api' | 'mixed'
    command     TEXT,                    -- the command that produced it, if known
    sha256      TEXT NOT NULL            -- hash of the raw payload
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    pull_id INTEGER PRIMARY KEY REFERENCES pulls(id),
    body    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id    TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_snapshots (
    id           INTEGER PRIMARY KEY,
    pull_id      INTEGER NOT NULL REFERENCES pulls(id),
    channel_id   TEXT NOT NULL REFERENCES channels(channel_id),
    captured_at  TEXT NOT NULL,
    title        TEXT,
    subscribers  INTEGER,
    view_count   INTEGER,
    video_count  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chsnap ON channel_snapshots(channel_id, captured_at);

CREATE TABLE IF NOT EXISTS video_snapshots (
    id            INTEGER PRIMARY KEY,
    pull_id       INTEGER NOT NULL REFERENCES pulls(id),
    video_id      TEXT NOT NULL REFERENCES videos(video_id),
    captured_at   TEXT NOT NULL,
    channel_id    TEXT,
    published_at  TEXT,                  -- ISO-8601 (date-only when the source has no time)
    title         TEXT,
    description   TEXT,
    tags          TEXT,                  -- JSON array
    duration_s    INTEGER,
    view_count    INTEGER,
    like_count    INTEGER,
    comment_count INTEGER,
    thumbnail_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_vsnap ON video_snapshots(video_id, captured_at);

CREATE TABLE IF NOT EXISTS search_suggestions (
    id          INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,
    seed        TEXT NOT NULL,           -- the keyword the user asked about
    query       TEXT NOT NULL,           -- what was actually typed (seed, or seed + a letter when expanding)
    suggestion  TEXT NOT NULL,
    rank        INTEGER NOT NULL         -- 0 = first suggestion; relevance order, NOT search volume
);
CREATE INDEX IF NOT EXISTS idx_sugg ON search_suggestions(seed, captured_at);
"""

# Tables whose rows may never be modified or removed once written.
_PROTECTED = ("pulls", "raw_payloads", "channel_snapshots", "video_snapshots", "videos", "channels",
              "search_suggestions")


def _guards() -> str:
    parts = []
    for t in _PROTECTED:
        for op in ("UPDATE", "DELETE"):
            parts.append(
                f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
            )
    return "\n".join(parts)


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.executescript(_guards())
    return conn
