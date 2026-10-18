"""Serve the dashboard locally so its Refresh button can pull new data and rebuild the page.

The static `dashboard.html` cannot run commands. `spinwheel serve` renders the same page on each
request and adds one endpoint, POST /refresh, which re-runs what you already track:
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
import shlex
import subprocess
import threading
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
        r = discover.search(conn, topics, yt_dlp=yt_dlp, extra=extra)
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


def make_handler(db_path: str, short_max: int, yt_dlp: str, extra: list[str]):
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

        def do_POST(self):
            if not self._host_ok() or self.headers.get(GUARD_HEADER) != "1":
                return self._json(403, {"error": "forbidden"})
            if self.path != "/refresh":
                return self._json(404, {"error": "not found"})
            if not lock.acquire(blocking=False):
                return self._json(409, {"error": "a refresh is already running"})
            try:
                steps = refresh(db_path, yt_dlp=yt_dlp, extra=extra)
            finally:
                lock.release()
            self._json(200, {"steps": steps, "ok": all(s["ok"] for s in steps)})

        def log_message(self, fmt, *args):
            print("  " + fmt % args)

    return Handler


def serve(db_path: str, *, port: int = 8765, short_max: int = SHORT_MAX_SECONDS, yt_dlp: str = "yt-dlp",
          extra: list[str] | None = None, open_browser: bool = False) -> None:
    server = ThreadingHTTPServer((HOST, port), make_handler(db_path, short_max, yt_dlp, extra or []))
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
