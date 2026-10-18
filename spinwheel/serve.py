"""Serve the dashboard locally so its Refresh button can pull new data and rebuild the page.

The static `dashboard.html` cannot run commands. `spinwheel serve` renders the same page on each
request and adds POST /track (add a topic or channel and fetch it) and POST /refresh, which re-runs what you already track:
  1. every channel/playlist pull stored before (the yt-dlp command is kept with each pull),
  2. every tracked search topic,
  3. subscriber counts older than a day,
  4. publish dates that were refused earlier.
Comments and autocomplete suggestions are slow and rate-limited, so they stay separate commands.
Only 127.0.0.1 is bound and /refresh needs a custom header, so other web pages cannot trigger it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import dashboard, db, discover
from .analysis import SHORT_MAX_SECONDS
from .ingest import ingest_text

HOST = "127.0.0.1"
GUARD_HEADER = "X-Spinwheel"
PULL_TIMEOUT = 600
_SEARCH_MARKERS = ("ytsearch", "search_query", "--skip-download", "--playlist-items")


def stored_pull_commands(conn) -> list[list[str]]:
    """Distinct yt-dlp commands from earlier `pull`s, oldest first. Search, enrich and channel-count
    commands are stored too, but those have their own refresh steps, so they are left out here."""
    seen: dict[str, list[str]] = {}
    for (command,) in conn.execute("SELECT command FROM pulls WHERE command IS NOT NULL ORDER BY id"):
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        if not argv or os.path.basename(argv[0]) not in ("yt-dlp", "yt-dlp.exe") or "-J" not in argv:
            continue
        if any(m in command for m in _SEARCH_MARKERS):
            continue
        seen[command] = argv
    return list(seen.values())


def refresh(db_path: str, *, yt_dlp: str = "yt-dlp", extra: list[str] | None = None,
            run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[dict]:
    """Run every refresh step. A failing step is reported and the rest still run; nothing here raises."""
    extra = extra or []
    conn = db.connect(db_path)
    steps: list[dict] = []

    def step(name: str, fn: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, detail = fn()
        except Exception as e:  # one broken step must not stop the others
            ok, detail = False, str(e)
        steps.append({"step": name, "ok": ok, "detail": detail})

    def pulls() -> tuple[bool, str]:
        cmds = stored_pull_commands(conn)
        if not cmds:
            return True, "no earlier channel pulls to repeat (run `spinwheel pull` first)"
        done, failed = 0, []
        for argv in cmds:
            cmd = [yt_dlp, *extra, *argv[1:]]
            proc = run(cmd, capture_output=True, text=True, timeout=PULL_TIMEOUT)
            if proc.returncode != 0:
                failed.append(f"{argv[-1]}: exited {proc.returncode}")
                continue
            ingest_text(conn, proc.stdout, command=shlex.join(argv))
            done += 1
        return not failed, f"{done} of {len(cmds)} pull(s) repeated" + (f"; {'; '.join(failed)}" if failed else "")

    def searches() -> tuple[bool, str]:
        topics = discover.tracked_topics(conn)
        if not topics:
            return True, "no tracked topics"
        r = discover.search(conn, topics, within="today", yt_dlp=yt_dlp, extra=extra)
        bad = [f"{q}: {e}" for q, e in r["failed"].items()]
        return not bad, f"{len(topics) - len(bad)} of {len(topics)} topic(s) searched" + (f"; {'; '.join(bad)}" if bad else "")

    def channels() -> tuple[bool, str]:
        todo = discover.stale_channels(conn, refresh_days=1)
        if not todo:
            return True, "subscriber counts are under a day old"
        r = discover.refresh_channels(conn, todo, yt_dlp=yt_dlp, extra=extra)
        return not r["failed"], f"{len(r['stored'])} updated, {len(r['failed'])} failed"

    def enrich() -> tuple[bool, str]:
        ids = discover.undated_videos(conn, 50)
        if not ids:
            return True, "every video has a publish date"
        r = discover.enrich(conn, ids, yt_dlp=yt_dlp, extra=extra)
        return r["enriched"] > 0, f"{r['enriched']} of {r['requested']} enriched" + (f"; {r['error']}" if r["error"] else "")

    step("Channel pulls", pulls)
    step("Tracked topics", searches)
    step("Subscriber counts", channels)
    step("Publish dates", enrich)
    conn.close()
    return steps


_HANDLE = re.compile(r"^@[\w.\-]{1,60}$")
_CHANNEL_PATH = re.compile(r"^/(@[^/]+|(?:channel|c|user)/[^/]+)")
CHANNEL_PULL_LIMIT = 50


def channel_url(value: str) -> str:
    """A channel's /videos page from an @handle or a YouTube channel link; anything else is rejected."""
    v = value.strip()
    if _HANDLE.match(v):
        return f"https://www.youtube.com/{v}/videos"
    p = urllib.parse.urlparse(v if "://" in v else "https://" + v)
    host = (p.hostname or "").lower()
    m = _CHANNEL_PATH.match(urllib.parse.unquote(p.path))
    if p.scheme not in ("http", "https") or not (host == "youtube.com" or host.endswith(".youtube.com")) or not m:
        raise ValueError("that doesn't look like a YouTube channel link or @handle")
    return f"https://www.youtube.com/{urllib.parse.quote(m.group(1), safe='/@')}/videos"


MAX_ADD = 30


def track(db_path: str, kind: str, value: str, *, yt_dlp: str = "yt-dlp", extra: list[str] | None = None,
          run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[dict]:
    """Add topics (keyword searches, remembered) or channels (pulls, repeated by Refresh), one per line, and fetch them now."""
    extra = extra or []
    values = list(dict.fromkeys(v.strip() for v in value.splitlines() if v.strip()))
    if not values or any(len(v) > 200 or any(ord(c) < 32 for c in v) for v in values):
        raise ValueError("type a topic or a channel first")
    if len(values) > MAX_ADD:
        raise ValueError(f"add at most {MAX_ADD} at a time")
    if kind not in ("topic", "channel"):
        raise ValueError("kind must be topic or channel")
    urls = {v: channel_url(v) for v in values} if kind == "channel" else {}  # validate everything before fetching anything
    conn = db.connect(db_path)
    try:
        if kind == "topic":
            r = discover.search(conn, values, within="today", yt_dlp=yt_dlp, extra=extra)
            return [{"step": f"Topic: {v}", "ok": v not in r["failed"], "detail": r["failed"].get(v, "searched and stored")} for v in values]
        steps = []
        for v in values:
            stored = [yt_dlp, "-J", "--flat-playlist", "--playlist-end", str(CHANNEL_PULL_LIMIT), urls[v]]
            proc = run([stored[0], *extra, *stored[1:]], capture_output=True, text=True, timeout=PULL_TIMEOUT)
            if proc.returncode != 0:
                tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no details"]
                steps.append({"step": f"Channel: {v}", "ok": False, "detail": f"yt-dlp exited {proc.returncode}: {tail[0]}"})
                continue
            ingest_text(conn, proc.stdout, command=shlex.join(["yt-dlp", *stored[1:]]))
            steps.append({"step": f"Channel: {v}", "ok": True, "detail": "pulled and stored"})
        return steps
    finally:
        conn.close()


def make_handler(db_path: str, short_max: int, yt_dlp: str, extra: list[str], output: str | None = None):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _host_ok(self) -> bool:  # blocks DNS-rebinding: only our own address is a valid Host
            return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            if self.path.split("?")[0] not in ("/", "/index.html", "/dashboard.html"):
                return self._json(404, {"error": "not found"})
            conn = db.connect(db_path)
            try:
                page = dashboard.render(conn, short_max, live=True)
            finally:
                conn.close()
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def _cors(self) -> None:
            # a saved dashboard.html opened from disk has the origin "null"; allow only that, only for /refresh
            if self.headers.get("Origin") == "null":
                self.send_header("Access-Control-Allow-Origin", "null")

        def do_OPTIONS(self):
            self.send_response(204)
            if self._host_ok() and self.path in ("/refresh", "/track"):
                self._cors()
                self.send_header("Access-Control-Allow-Methods", "POST")
                self.send_header("Access-Control-Allow-Headers", f"{GUARD_HEADER}, Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if not self._host_ok() or self.headers.get(GUARD_HEADER) != "1":
                return self._json(403, {"error": "forbidden"})
            if self.path not in ("/refresh", "/track"):
                return self._json(404, {"error": "not found"})
            job = None
            if self.path == "/track":
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                    req = json.loads(self.rfile.read(min(size, 10_000)) or b"{}")
                    kind, value = str(req.get("kind", "")), str(req.get("value", ""))
                except (ValueError, AttributeError):
                    return self._json(400, {"error": "bad request"})
                job = lambda: track(db_path, kind, value, yt_dlp=yt_dlp, extra=extra)
            if not lock.acquire(blocking=False):
                return self._json(409, {"error": "a refresh is already running"})
            try:
                try:
                    steps = (job or (lambda: refresh(db_path, yt_dlp=yt_dlp, extra=extra)))()
                except ValueError as e:
                    return self._send_cors_json(400, {"error": str(e)})
                if output:  # so a dashboard.html opened from disk shows the new data on reload
                    conn = db.connect(db_path)
                    try:
                        dashboard.write(conn, output, short_max)
                    finally:
                        conn.close()
            finally:
                lock.release()
            self._send_cors_json(200, {"steps": steps, "ok": all(s["ok"] for s in steps)})

        def _send_cors_json(self, status: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            print("  " + fmt % args)

    return Handler


def serve(db_path: str, *, port: int = 8765, short_max: int = SHORT_MAX_SECONDS, yt_dlp: str = "yt-dlp",
          extra: list[str] | None = None, open_browser: bool = False, output: str | None = None) -> None:
    server = ThreadingHTTPServer((HOST, port), make_handler(db_path, short_max, yt_dlp, extra or [], output))
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"dashboard at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
