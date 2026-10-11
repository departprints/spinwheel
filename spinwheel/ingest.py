"""Append a CLI pull to the database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from . import db
from .adapters import parse


def utc_iso(value: str | datetime | None = None) -> str:
    """Normalize to 'YYYY-MM-DDTHH:MM:SSZ' (naive inputs are taken as UTC)."""
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest_text(conn: sqlite3.Connection, text: str, *, pulled_at: str | datetime | None = None,
                command: str | None = None) -> dict:
    parsed = parse(text)
    if not parsed.videos and not parsed.channels and not parsed.comments:
        raise ValueError(
            f"nothing recognizable to store ({parsed.skipped} unrecognized object(s)). "
            "Expected yt-dlp JSON or YouTube Data API v3 JSON.")

    when = utc_iso(pulled_at)
    now = utc_iso()
    with conn:  # one transaction: a pull is stored completely or not at all
        pull_id = conn.execute(
            "INSERT INTO pulls(pulled_at, ingested_at, source, command, sha256) VALUES (?,?,?,?,?)",
            (when, now, parsed.source, command, hashlib.sha256(text.encode()).hexdigest()),
        ).lastrowid
        body, encoding = db.pack_raw(text)
        conn.execute("INSERT INTO raw_payloads(pull_id, body, encoding) VALUES (?,?,?)", (pull_id, body, encoding))

        for c in parsed.channels:
            conn.execute("INSERT OR IGNORE INTO channels(channel_id, first_seen_at) VALUES (?,?)",
                         (c.channel_id, when))
            conn.execute(
                "INSERT INTO channel_snapshots(pull_id, channel_id, captured_at, title, subscribers, "
                "view_count, video_count) VALUES (?,?,?,?,?,?,?)",
                (pull_id, c.channel_id, when, c.title, c.subscribers, c.view_count, c.video_count))

        for v in parsed.videos:
            conn.execute("INSERT OR IGNORE INTO videos(video_id, first_seen_at) VALUES (?,?)",
                         (v.video_id, when))
            conn.execute(
                "INSERT INTO video_snapshots(pull_id, video_id, captured_at, channel_id, published_at, "
                "title, description, tags, duration_s, view_count, like_count, comment_count, thumbnail_url, "
                "width, height, page_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pull_id, v.video_id, when, v.channel_id, v.published_at, v.title, v.description,
                 json.dumps(v.tags), v.duration_s, v.view_count, v.like_count, v.comment_count,
                 v.thumbnail_url, v.width, v.height, v.page_url))

        for c in parsed.comments:
            conn.execute("INSERT OR IGNORE INTO videos(video_id, first_seen_at) VALUES (?,?)", (c.video_id, when))
            conn.execute(
                "INSERT INTO comment_snapshots(pull_id, video_id, comment_id, parent_id, captured_at, text, "
                "like_count, published_at, by_creator, pinned) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pull_id, c.video_id, c.comment_id, c.parent_id, when, c.text, c.like_count, c.published_at,
                 int(c.by_creator), int(c.pinned)))

    return {"pull_id": pull_id, "source": parsed.source, "pulled_at": when,
            "videos": len(parsed.videos), "channels": len(parsed.channels), "comments": len(parsed.comments),
            "skipped": parsed.skipped}
