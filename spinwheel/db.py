"""SQLite storage. Every pull is appended; nothing is ever updated or deleted.

Layout:
  pulls             one row per ingest run (when, from what command, content hash)
  raw_payloads      the exact bytes that came back, so any parser bug can be fixed later
  videos, channels  identity only (id, first seen); everything descriptive lives in snapshots
  video_snapshots   one row per video per pull: title, views, likes, ... as of that pull
  channel_snapshots one row per channel per pull: subscribers, total views, ...
  search_suggestions YouTube autocomplete results per fetch, so new phrases can be spotted over time
  comment_snapshots  top-level comments per fetch with their like counts at that moment
  searches           which video appeared for which keyword search (topic discovery), and at what rank

Snapshot, payload and pull tables are protected by triggers that abort UPDATE and
DELETE, so "not overwritten" is enforced by the database, not by convention.
"""

from __future__ import annotations

import sqlite3
import zlib
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
    pull_id  INTEGER PRIMARY KEY REFERENCES pulls(id),
    body     TEXT NOT NULL,              -- the exact output, or zlib bytes of it when encoding = 'zlib'
    encoding TEXT                        -- NULL/'plain' or 'zlib' (large payloads are compressed, losslessly)
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
    thumbnail_url TEXT,
    width         INTEGER,               -- yt-dlp only; with height, tells a vertical Short from a landscape video
    height        INTEGER,
    page_url      TEXT                   -- a /shorts/ URL identifies a Short
);
CREATE INDEX IF NOT EXISTS idx_vsnap ON video_snapshots(video_id, captured_at);

CREATE TABLE IF NOT EXISTS comment_snapshots (
    id           INTEGER PRIMARY KEY,
    pull_id      INTEGER NOT NULL REFERENCES pulls(id),
    video_id     TEXT NOT NULL REFERENCES videos(video_id),
    comment_id   TEXT NOT NULL,
    parent_id    TEXT,                   -- NULL for a top-level comment
    captured_at  TEXT NOT NULL,
    text         TEXT NOT NULL,
    like_count   INTEGER,
    published_at TEXT,
    by_creator   INTEGER NOT NULL DEFAULT 0,   -- written by the channel itself; no author names are stored
    pinned       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_comments ON comment_snapshots(video_id, captured_at);

CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY,
    pull_id     INTEGER NOT NULL REFERENCES pulls(id),
    query       TEXT NOT NULL,           -- the tracked topic (lower-cased keyword search)
    video_id    TEXT NOT NULL REFERENCES videos(video_id),
    rank        INTEGER NOT NULL,        -- 0 = first search result
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_searches ON searches(query, captured_at);

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
              "search_suggestions", "comment_snapshots", "searches")


def _guards() -> str:
    parts = []
    for t in _PROTECTED:
        for op in ("UPDATE", "DELETE"):
            parts.append(
                f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
            )
    return "\n".join(parts)


# Columns added after the first release. Adding a column is not an UPDATE, so history stays untouched;
# rows stored before the column existed simply read NULL.
_ADDED_COLUMNS = {"video_snapshots": [("width", "INTEGER"), ("height", "INTEGER"), ("page_url", "TEXT")],
                  "raw_payloads": [("encoding", "TEXT")]}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


COMPRESS_OVER = 100_000      # bytes; below this a payload is stored as plain text


def pack_raw(text: str) -> tuple[str | bytes, str]:
    data = text.encode("utf-8")
    return (zlib.compress(data, 6), "zlib") if len(data) > COMPRESS_OVER else (text, "plain")


def read_raw(conn: sqlite3.Connection, pull_id: int) -> str:
    """The payload exactly as it was stored, whichever encoding it used."""
    body, enc = conn.execute("SELECT body, encoding FROM raw_payloads WHERE pull_id = ?", (pull_id,)).fetchone()
    return zlib.decompress(body).decode("utf-8") if enc == "zlib" else body


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(_guards())
    return conn
