"""Optional: the official YouTube Data API v3, for the data scraping gets blocked on.

Scraping with yt-dlp needs no key but YouTube refuses many per-video requests from servers and busy
IPs ("confirm you're not a bot"): publish dates and comments are what goes missing. The API has no such
check. It does have a daily quota (10,000 units by default), so the calls are batched:

    search.list          100 units per query     (find videos by keyword, optionally only the last 24 h)
    videos.list           1 unit per 50 videos   (publish date, views, likes, duration)
    channels.list         1 unit per 50 channels (subscriber counts)
    commentThreads.list   1 unit per page of up to 100 threads (with up to 5 replies each)

The key is read from an environment variable (default YOUTUBE_API_KEY) and is never stored: stored
commands show a redacted description, and API responses do not contain the key.

NOTE: written against the documented response shapes and covered by tests with those shapes; it has
not been run against the live API (no key was available when it was written).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import comments as comments_mod
from . import discover
from .ingest import ingest_text, utc_iso

BASE = "https://www.googleapis.com/youtube/v3"
BATCH = 50
DEFAULT_KEY_ENV = "YOUTUBE_API_KEY"
Fetch = Callable[[str], str]


class ApiError(RuntimeError):
    pass


def _fetch(url: str) -> str:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "spinwheel/0.1"}), timeout=30) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise ApiError(describe_error(e.code, body)) from None
    except urllib.error.URLError as e:
        raise ApiError(f"network error: {e.reason}") from None


def describe_error(status: int, body: str) -> str:
    try:
        err = json.loads(body)["error"]
        reasons = {x.get("reason") for x in err.get("errors", [])}
        msg = err.get("message", "")
    except (ValueError, KeyError, TypeError):
        reasons, msg = set(), body[:200]
    if "quotaExceeded" in reasons or "dailyLimitExceeded" in reasons:
        return "daily quota used up (it resets at midnight Pacific time); try again tomorrow or use another key"
    if "keyInvalid" in reasons or "API key not valid" in msg:
        return "the API key was rejected: check that it is correct and that the YouTube Data API v3 is enabled for it"
    if "commentsDisabled" in reasons:
        return "comments are disabled for this video"
    return f"HTTP {status}: {msg or body[:200]}"


def get_key(env: str = DEFAULT_KEY_ENV) -> str:
    key = os.environ.get(env, "").strip()
    if not key:
        raise ApiError(f"no API key: set the environment variable {env} (never paste a key into a chat or commit it)")
    return key


def call(endpoint: str, params: dict, key: str, fetch: Fetch = _fetch) -> str:
    q = urllib.parse.urlencode({**params, "key": key})
    return fetch(f"{BASE}/{endpoint}?{q}")


def _describe(endpoint: str, params: dict) -> str:
    """What gets stored as the pull's command: the call without the key."""
    shown = {k: (v if k != "id" else f"<{len(str(v).split(','))} ids>") for k, v in params.items()}
    return f"youtube-api {endpoint} " + " ".join(f"{k}={v}" for k, v in shown.items()) + " (key redacted)"


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def videos(conn: sqlite3.Connection, ids: list[str], key: str, fetch: Fetch = _fetch) -> int:
    """Publish date, stats and duration for videos, 50 per call."""
    got = 0
    for batch in _chunks(ids, BATCH):
        params = {"part": "snippet,statistics,contentDetails", "id": ",".join(batch), "maxResults": BATCH}
        got += ingest_text(conn, call("videos", params, key, fetch), command=_describe("videos.list", params))["videos"]
    return got


def channels(conn: sqlite3.Connection, ids: list[str], key: str, fetch: Fetch = _fetch) -> int:
    """Subscriber counts, 50 per call. Channels that hide theirs come back without one and are skipped."""
    got = 0
    for batch in _chunks(ids, BATCH):
        params = {"part": "snippet,statistics", "id": ",".join(batch), "maxResults": BATCH}
        try:
            got += ingest_text(conn, call("channels", params, key, fetch), command=_describe("channels.list", params))["channels"]
        except ValueError:
            pass
    return got


def search(conn: sqlite3.Connection, query: str, key: str, *, within: str | None = None, max_results: int = 50,
           order: str = "viewCount", now: datetime | None = None, fetch: Fetch = _fetch) -> dict:
    """Keyword search (100 units), stored as that topic's results, then enriched with videos.list and channels.list."""
    params = {"part": "snippet", "q": query, "type": "video", "order": order, "maxResults": min(max_results, 50)}
    if within:
        if within not in discover.WITHIN_DAYS:
            raise ValueError(f"within must be one of {', '.join(discover.WITHIN_DAYS)}")
        after = (now or datetime.now(timezone.utc)) - timedelta(days=discover.WITHIN_DAYS[within])
        params["publishedAfter"] = after.strftime("%Y-%m-%dT%H:%M:%SZ")
    r = ingest_text(conn, call("search", params, key, fetch), command=_describe("search.list", params),
                    search_query=query, within=within)
    ids = [row[0] for row in conn.execute("SELECT video_id FROM searches WHERE pull_id = ? ORDER BY rank", (r["pull_id"],))]
    enriched = videos(conn, ids, key, fetch) if ids else 0
    chan_ids = [row[0] for row in conn.execute(
        "SELECT DISTINCT channel_id FROM video_snapshots WHERE video_id IN (%s) AND channel_id IS NOT NULL" % ",".join("?" * len(ids)), ids)] if ids else []
    return {"found": r["videos"], "enriched": enriched, "channels": channels(conn, chan_ids, key, fetch) if chan_ids else 0}


def thread_comments(conn: sqlite3.Connection, video_ids: list[str], key: str, *, max_comments: int = 100,
                    fetch: Fetch = _fetch, sleep: Callable[[float], None] = time.sleep, delay: float = 0.2) -> dict:
    """Top threads (with up to 5 replies each) per video, most relevant first: one page, 1 unit each."""
    stored, failed = {}, {}
    for i, vid in enumerate(video_ids):
        if i:
            sleep(delay)
        params = {"part": "snippet,replies", "videoId": vid, "order": "relevance", "maxResults": min(max_comments, 100),
                  "textFormat": "plainText"}
        try:
            r = ingest_text(conn, call("commentThreads", params, key, fetch), command=_describe("commentThreads.list", params))
            stored[vid] = r["comments"]
        except (ApiError, ValueError) as e:
            failed[vid] = str(e)
            if "quota" in str(e):
                break
    return {"stored": stored, "failed": failed}
