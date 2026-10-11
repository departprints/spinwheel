"""Command line: ingest a pull, run a CLI and ingest its output, build the dashboard."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys

from . import analysis, dashboard, db, suggest
from .ingest import ingest_text


def _report(r: dict) -> None:
    print(f"pull #{r['pull_id']} stored ({r['source']}, {r['pulled_at']}): "
          f"{r['videos']} video snapshot(s), {r['channels']} channel snapshot(s)"
          + (f", {r['skipped']} unrecognized object(s) skipped" if r["skipped"] else ""))


def cmd_ingest(args) -> int:
    text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    conn = db.connect(args.db)
    _report(ingest_text(conn, text, pulled_at=args.pulled_at, command=f"ingest {args.file}"))
    return 0


def cmd_pull(args) -> int:
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        print("usage: spinwheel pull -- <command that prints JSON>", file=sys.stderr)
        return 2
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        print(f"{cmd[0]} exited {proc.returncode}; nothing stored", file=sys.stderr)
        return proc.returncode
    conn = db.connect(args.db)
    _report(ingest_text(conn, proc.stdout, pulled_at=args.pulled_at, command=shlex.join(cmd)))
    return 0


def cmd_dashboard(args) -> int:
    conn = db.connect(args.db)
    path = dashboard.write(conn, args.output, short_max=args.short_max)
    print(f"dashboard written to {path}")
    return 0


def cmd_suggest(args) -> int:
    conn = db.connect(args.db)
    seeds = list(args.seeds)
    if args.from_data:
        found = analysis.top_seeds(conn, args.from_data)
        if not found:
            print("no topics in the database yet to use as seeds; pull some channels first", file=sys.stderr)
        seeds += found
    if not seeds:
        print("usage: spinwheel suggest KEYWORD [KEYWORD ...]  or  --from-data N", file=sys.stderr)
        return 2
    n_q = len(seeds) * (27 if args.expand else 1)
    print(f"asking YouTube autocomplete about {len(seeds)} keyword(s), ~{n_q} request(s), {args.delay:g}s apart")
    r = suggest.collect(conn, seeds, expand=args.expand, delay=args.delay)
    for seed, n in r["stored"].items():
        print(f"  {seed}: {n} suggestion(s) stored")
    for seed, err in r["failed"].items():
        print(f"  {seed}: FAILED, nothing stored ({err})", file=sys.stderr)
    return 1 if r["failed"] else 0


def cmd_status(args) -> int:
    conn = db.connect(args.db)
    q = lambda sql: conn.execute(sql).fetchone()[0]
    n_sug = q('SELECT COUNT(DISTINCT seed) FROM search_suggestions')
    print(f"{args.db}: {q('SELECT COUNT(*) FROM pulls')} pulls, "
          f"{q('SELECT COUNT(DISTINCT video_id) FROM video_snapshots')} videos "
          f"({q('SELECT COUNT(*) FROM video_snapshots')} snapshots), "
          f"{q('SELECT COUNT(DISTINCT channel_id) FROM channel_snapshots')} channels "
          f"({q('SELECT COUNT(*) FROM channel_snapshots')} snapshots), {n_sug} searched keyword(s)")
    last = conn.execute("SELECT pulled_at, source, command FROM pulls ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        print(f"last pull: {last['pulled_at']} via {last['source']}" + (f" ({last['command']})" if last["command"] else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="spinwheel", description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB, help=f"database file (default {db.DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="store JSON from a file (or - for stdin)")
    p.add_argument("file")
    p.add_argument("--pulled-at", help="when the data was fetched, ISO-8601 (default: now)")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("pull", help="run a command that prints JSON, then store its output")
    p.add_argument("--pulled-at", help="override the pull time, ISO-8601 (default: now)")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- yt-dlp -J https://youtube.com/@channel/videos")
    p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("dashboard", help="build the HTML dashboard")
    p.add_argument("-o", "--output", default="dashboard.html")
    p.add_argument("--short-max", type=int, default=60, metavar="SECONDS",
                   help="when a pull has no /shorts/ URL or width/height, videos up to this length count as Shorts (default 60)")
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("suggest", help="store YouTube search autocomplete for keywords (search-demand signal)")
    p.add_argument("seeds", nargs="*", help="keywords to look up")
    p.add_argument("--from-data", type=int, metavar="N", help="also use the top N topics/gaps from your data")
    p.add_argument("--expand", action="store_true", help="also try 'keyword a' .. 'keyword z' (27x the requests, many more long-tail phrases)")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1)")
    p.set_defaults(fn=cmd_suggest)

    p = sub.add_parser("status", help="what's in the database")
    p.set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
