"""YouTube search autocomplete, stored append-only.

Autocomplete shows what people actually type after a keyword. It is the best free signal of search
demand, but it is relevance-ordered: it says nothing about volume. The endpoint is unofficial
(the one the YouTube search box calls), so expect it to be rate-limited or to change; failures are
reported per seed and never store partial junk.
"""

from __future__ import annotations

import json
import sqlite3
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .ingest import utc_iso

ENDPOINT = "https://suggestqueries.google.com/complete/search"


def fetch(query: str, *, timeout: float = 15) -> list[str]:
    url = ENDPOINT + "?" + urllib.parse.urlencode({"client": "firefox", "ds": "yt", "hl": "en", "q": query})
    req = urllib.request.Request(url, headers={"User-Agent": "spinwheel/0.1 (+local research tool)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_response(resp.read().decode("utf-8", "replace"))


def parse_response(text: str) -> list[str]:
    """Body looks like: ["query", ["suggestion", ...], [], {...}]"""
    try:
        data = json.loads(text)
        items = data[1]
    except (json.JSONDecodeError, IndexError, TypeError, KeyError):
        raise ValueError("unexpected autocomplete response") from None
    return [s.strip() for s in items if isinstance(s, str) and s.strip()]


def queries_for(seed: str, expand: bool) -> list[str]:
    return [seed] + ([f"{seed} {c}" for c in string.ascii_lowercase] if expand else [])


def collect(conn: sqlite3.Connection, seeds: list[str], *, expand: bool = False, delay: float = 1.0,
            fetch_fn: Callable[[str], list[str]] = fetch, sleep: Callable[[float], None] = time.sleep,
            captured_at: str | None = None) -> dict:
    """Fetch and store suggestions. A seed is stored all-or-nothing; one failing seed doesn't stop the rest."""
    when = utc_iso(captured_at)
    stored, failed, first = {}, {}, True
    for seed in dict.fromkeys(s.strip().lower() for s in seeds if s.strip()):
        rows, error = [], None
        for q in queries_for(seed, expand):
            if not first:
                sleep(delay)
            first = False
            try:
                for rank, sug in enumerate(fetch_fn(q)):
                    rows.append((when, seed, q, sug, rank))
            except (urllib.error.URLError, OSError, ValueError) as e:
                error = str(e)
                break
        if error:
            failed[seed] = error
            continue
        with conn:
            conn.executemany("INSERT INTO search_suggestions(captured_at, seed, query, suggestion, rank) "
                             "VALUES (?,?,?,?,?)", rows)
        stored[seed] = len({r[3] for r in rows})
    return {"captured_at": when, "stored": stored, "failed": failed}
