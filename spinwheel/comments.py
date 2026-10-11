"""Fetch top-level comments for videos that are worth the cost, and store them append-only.

Comments are slow to fetch (a request or more per video), so only videos with more than `min_views`
views are fetched, most-viewed first, and a video fetched recently is skipped. Each fetch is a new set of
snapshots, so like counts and new comments can be compared between fetches.
"""

from __future__ import annotations

import shlex
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from .analysis import SHORT_MAX_SECONDS, classify_format
from .ingest import ingest_text, utc_iso

DEFAULT_MIN_VIEWS = 10_000
DEFAULT_REPLIES = 150          # replies fetched per video in total (0 = none), at most REPLIES_PER_THREAD each
REPLIES_PER_THREAD = 3
Runner = Callable[[list[str]], tuple[int, str, str]]


def run_command(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def eligible(conn: sqlite3.Connection, *, min_views: int = DEFAULT_MIN_VIEWS, refresh_days: float = 14,
             limit: int | None = 25, now: datetime | None = None, include_shorts: bool = False,
             short_max: int = SHORT_MAX_SECONDS) -> list[dict]:
    """Long-form videos (Shorts too with include_shorts) whose latest known view count is more than
    `min_views` and whose comments have not been fetched in the last `refresh_days`, most-viewed first."""
    now = now or datetime.now(timezone.utc)
    cutoff = utc_iso(now - timedelta(days=refresh_days))
    rows = conn.execute(
        """
        SELECT s.video_id, s.view_count AS views, s.title,
               (SELECT MAX(captured_at) FROM comment_snapshots c WHERE c.video_id = s.video_id) AS last_fetch
        FROM video_snapshots s
        WHERE s.view_count IS NOT NULL
          AND s.id = (SELECT id FROM video_snapshots x WHERE x.video_id = s.video_id AND x.view_count IS NOT NULL
                      ORDER BY x.captured_at DESC, x.id DESC LIMIT 1)
          AND s.view_count > ?
        ORDER BY s.view_count DESC
        """, (min_views,)).fetchall()
    out = [dict(r) for r in rows if not r["last_fetch"] or r["last_fetch"] < cutoff]
    if not include_shorts:
        out = [r for r in out if not _is_short(conn, r["video_id"], short_max)]
    return out[:limit] if limit else out


def _is_short(conn: sqlite3.Connection, video_id: str, short_max: int) -> bool:
    snaps = conn.execute("SELECT duration_s, width, height, page_url FROM video_snapshots WHERE video_id = ? "
                         "ORDER BY captured_at, id", (video_id,)).fetchall()
    return classify_format(snaps, short_max)[0] == "short"


def command_for(video_id: str, max_comments: int, yt_dlp: str = "yt-dlp", replies: int = DEFAULT_REPLIES,
                extra: list[str] | None = None) -> list[str]:
    # max_comments=<total>,<parents>,<replies>,<replies per thread>, most liked threads first. Replies are only
    # fetched to see whether the channel answered a question, so they are few per thread and capped overall.
    per = REPLIES_PER_THREAD if replies > 0 else 0
    return [yt_dlp, "-J", "--skip-download", "--write-comments", *(extra or []), "--extractor-args",
            f"youtube:max_comments={max_comments + replies},{max_comments},{replies},{per};comment_sort=top",
            f"https://www.youtube.com/watch?v={video_id}"]


def collect(conn: sqlite3.Connection, videos: list[dict], *, max_comments: int = 100, delay: float = 3.0,
            replies: int = DEFAULT_REPLIES, yt_dlp: str = "yt-dlp", extra: list[str] | None = None, runner: Runner = run_command, sleep: Callable[[float], None] = time.sleep,
            on_progress: Callable[[str], None] = lambda s: None) -> dict:
    """One failing video never stops the rest, and a failure stores nothing for that video."""
    stored, failed = {}, {}
    for i, v in enumerate(videos):
        if i:
            sleep(delay)
        cmd = command_for(v["video_id"], max_comments, yt_dlp, replies, extra)
        try:
            code, out, err = runner(cmd)
        except OSError as e:                     # yt-dlp not installed / not executable
            failed[v["video_id"]] = str(e)
            break
        if code != 0 or not out.strip():
            failed[v["video_id"]] = (err.strip().splitlines() or [f"exit {code}"])[-1][:200]
            on_progress(f"  {v['video_id']}: FAILED ({failed[v['video_id']]})")
            continue
        try:
            r = ingest_text(conn, out, command=shlex.join(cmd))
        except ValueError as e:
            failed[v["video_id"]] = str(e)
            on_progress(f"  {v['video_id']}: FAILED ({e})")
            continue
        stored[v["video_id"]] = r["comments"]
        on_progress(f"  {v['video_id']}: {r['comments']} comments ({v['views']:,} views)")
    return {"stored": stored, "failed": failed}
