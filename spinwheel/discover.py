"""Find videos by keyword (topic discovery) and keep subscriber counts fresh, using yt-dlp.

A keyword search is run in two steps because YouTube sometimes bot-checks individual videos:
  1. a flat search always works and gives each result's rank, title, views and channel id. This is
     stored with the keyword, so every video is mapped to the topic that surfaced it;
  2. an enrichment pass fetches publish date, likes and the channel's subscriber count for those
     videos. Some may fail ("confirm you're not a bot"); the rest are kept and the shortfall is reported.
Subscriber counts are also refreshed per channel (cheap, and independent of per-video failures).
Pass extra yt-dlp arguments (for example --cookies-from-browser firefox) when YouTube asks for a login.
"""

from __future__ import annotations

import shlex
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from .comments import Runner, run_command
from .ingest import ingest_text, utc_iso

FIELDS = ("id,title,description,timestamp,upload_date,duration,view_count,like_count,comment_count,channel_id,"
          "channel,channel_follower_count,tags,thumbnail,width,height,webpage_url")
ENRICH_BATCH = 25


def search_command(query: str, n: int, yt_dlp: str = "yt-dlp", extra: list[str] | None = None) -> list[str]:
    return [yt_dlp, "-J", "--flat-playlist", *(extra or []), f"ytsearch{n}:{query}"]


def enrich_command(video_ids: list[str], yt_dlp: str = "yt-dlp", extra: list[str] | None = None) -> list[str]:
    return [yt_dlp, "--skip-download", "--print", f"%(.{{{FIELDS}}})j", *(extra or []),
            *[f"https://www.youtube.com/watch?v={v}" for v in video_ids]]


def channel_command(channel_id: str, yt_dlp: str = "yt-dlp", extra: list[str] | None = None) -> list[str]:
    # zero playlist items: just the channel page header, which carries the subscriber count
    return [yt_dlp, "-J", "--flat-playlist", "--playlist-items", "0", *(extra or []),
            f"https://www.youtube.com/channel/{channel_id}/videos"]


def tracked_topics(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT query FROM searches GROUP BY query ORDER BY MIN(id)")]


def search(conn: sqlite3.Connection, queries: list[str], *, max_results: int = 20, fast: bool = False,
           delay: float = 3.0, yt_dlp: str = "yt-dlp", extra: list[str] | None = None, runner: Runner = run_command,
           sleep: Callable[[float], None] = time.sleep,
           on_progress: Callable[[str], None] = lambda s: None) -> dict:
    """Run each keyword search. Returns {query: {found, enriched}} plus failures."""
    done, failed = {}, {}
    for i, q in enumerate(dict.fromkeys(x.strip() for x in queries if x.strip())):
        if i:
            sleep(delay)
        cmd = search_command(q, max_results, yt_dlp, extra)
        try:
            code, out, err = runner(cmd)
        except OSError as e:
            failed[q] = str(e)
            break
        if code != 0 or not out.strip():
            failed[q] = (err.strip().splitlines() or [f"exit {code}"])[-1][:200]
            on_progress(f"  {q}: FAILED ({failed[q]})")
            continue
        try:
            r = ingest_text(conn, out, command=shlex.join(cmd), search_query=q)
        except ValueError as e:
            failed[q] = str(e)
            on_progress(f"  {q}: FAILED ({e})")
            continue
        found = r["videos"]
        enriched = 0
        if not fast and found:
            ids = [row[0] for row in conn.execute(
                "SELECT video_id FROM searches WHERE pull_id = ? ORDER BY rank", (r["pull_id"],))]
            for j in range(0, len(ids), ENRICH_BATCH):
                batch = ids[j:j + ENRICH_BATCH]
                cmd2 = enrich_command(batch, yt_dlp, extra)
                try:
                    code2, out2, _ = runner(cmd2)
                except OSError:
                    break
                if out2.strip():             # partial success is normal: keep whatever came back
                    try:
                        enriched += ingest_text(conn, out2, command=shlex.join(cmd2))["videos"]
                    except ValueError:
                        pass
        done[q] = {"found": found, "enriched": enriched}
        on_progress(f"  {q}: {found} videos" + ("" if fast else f", {enriched} with publish date and subscribers"
                    + (f" ({found - enriched} could not be fetched; YouTube may be asking for a login)" if enriched < found else "")))
    return {"done": done, "failed": failed}


def undated_videos(conn: sqlite3.Connection, limit: int | None = 50) -> list[str]:
    """Videos that have never had a publish date (found by a flat search, or enrichment was blocked)."""
    rows = conn.execute(
        "SELECT video_id FROM video_snapshots GROUP BY video_id HAVING MAX(published_at IS NOT NULL) = 0 "
        "ORDER BY MAX(view_count) DESC").fetchall()
    ids = [r[0] for r in rows]
    return ids[:limit] if limit else ids


def enrich(conn: sqlite3.Connection, video_ids: list[str], *, delay: float = 3.0, yt_dlp: str = "yt-dlp",
           extra: list[str] | None = None, runner: Runner = run_command,
           sleep: Callable[[float], None] = time.sleep) -> dict:
    """Fetch publish date, likes and subscribers for videos; partial success is normal and is kept."""
    got = 0
    for j in range(0, len(video_ids), ENRICH_BATCH):
        if j:
            sleep(delay)
        cmd = enrich_command(video_ids[j:j + ENRICH_BATCH], yt_dlp, extra)
        try:
            _, out, _ = runner(cmd)
        except OSError as e:
            return {"enriched": got, "requested": len(video_ids), "error": str(e)}
        if out.strip():
            try:
                got += ingest_text(conn, out, command=shlex.join(cmd))["videos"]
            except ValueError:
                pass
    return {"enriched": got, "requested": len(video_ids), "error": None}


def stale_channels(conn: sqlite3.Connection, *, refresh_days: float = 7, limit: int | None = None,
                   now: datetime | None = None) -> list[str]:
    """Channels with videos on record whose subscriber count is missing or older than `refresh_days`,
    never-fetched first."""
    cutoff = utc_iso((now or datetime.now(timezone.utc)) - timedelta(days=refresh_days))
    rows = conn.execute(
        """
        SELECT c.channel_id, s.last
        FROM (SELECT channel_id FROM video_snapshots WHERE channel_id IS NOT NULL
              UNION SELECT channel_id FROM channel_snapshots) c
        LEFT JOIN (SELECT channel_id, MAX(captured_at) AS last FROM channel_snapshots
                   WHERE subscribers IS NOT NULL GROUP BY channel_id) s ON s.channel_id = c.channel_id
        WHERE c.channel_id LIKE 'UC%' AND (s.last IS NULL OR s.last < ?)
        ORDER BY s.last IS NOT NULL, s.last, c.channel_id
        """, (cutoff,)).fetchall()
    ids = [r[0] for r in rows]
    return ids[:limit] if limit else ids


def refresh_channels(conn: sqlite3.Connection, channel_ids: list[str], *, delay: float = 1.0, yt_dlp: str = "yt-dlp",
                     extra: list[str] | None = None, runner: Runner = run_command,
                     sleep: Callable[[float], None] = time.sleep,
                     on_progress: Callable[[str], None] = lambda s: None) -> dict:
    stored, failed = [], {}
    for i, cid in enumerate(channel_ids):
        if i:
            sleep(delay)
        cmd = channel_command(cid, yt_dlp, extra)
        try:
            code, out, err = runner(cmd)
        except OSError as e:
            failed[cid] = str(e)
            break
        if code != 0 or not out.strip():
            failed[cid] = (err.strip().splitlines() or [f"exit {code}"])[-1][:200]
            continue
        try:
            r = ingest_text(conn, out, command=shlex.join(cmd))
        except ValueError:      # a channel page with no subscriber count has nothing to store
            failed[cid] = "no subscriber count returned (it may be hidden)"
            continue
        if r["channels"]:
            stored.append(cid)
        else:
            failed[cid] = "no subscriber count returned (it may be hidden)"
    return {"stored": stored, "failed": failed}
