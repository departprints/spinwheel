"""Command line: ingest a pull, run a CLI and ingest its output, build the dashboard."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys

from . import analysis, autostart, comments, dashboard, db, discover, serve, suggest, youtube_api
from .ingest import ingest_text, utc_iso


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


def cmd_serve(args) -> int:
    db.connect(args.db).close()  # create/migrate the database before the first request
    serve.serve(args.db, port=args.port, short_max=args.short_max, yt_dlp=args.yt_dlp, extra=_extra(args),
                open_browser=args.open, output=args.output or None)
    return 0


def cmd_autostart(args) -> int:
    if args.action == "install":
        path = autostart.install(args.db, args.output, yt_dlp=args.yt_dlp, port=args.port, extra_args=_extra(args))
        print(f"installed {path}\nthe refresh helper now starts at login and is running now (http://127.0.0.1:{args.port}/)\n"
              f"it uses the database and dashboard in {args.db} / {args.output}; log: {autostart.LOG}")
    elif args.action == "uninstall":
        print("removed the login item" if autostart.uninstall() else "was not installed")
    else:
        print("running" if autostart.running() else "not running")
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


def cmd_comments(args) -> int:
    conn = db.connect(args.db)
    videos = comments.eligible(conn, min_views=args.min_views, refresh_days=args.refresh_days, limit=args.max_videos,
                               include_shorts=args.include_shorts)
    kind = "video(s)" if args.include_shorts else "long-form video(s)"
    if not videos:
        print(f"no {kind} over {args.min_views:,} views are waiting for a comment fetch "
              f"(fetched within {args.refresh_days:g} days are skipped). Pull channels first, or lower --min-views.")
        return 0
    print(f"{len(videos)} {kind} over {args.min_views:,} views, up to {args.max_comments} top-level comments each"
          + (f" plus up to {args.replies} replies (to see whether the channel answered)" if args.replies else ", no replies"))
    if args.dry_run:
        for v in videos:
            print(f"  {v['video_id']}  {v['views']:>12,} views  {v['title'] or ''}"[:120])
        print("dry run: nothing fetched")
        return 0
    r = comments.collect(conn, videos, max_comments=args.max_comments, delay=args.delay, replies=args.replies,
                         yt_dlp=args.yt_dlp, extra=_extra(args), on_progress=print)
    total = sum(r["stored"].values())
    print(f"stored {total} comment(s) from {len(r['stored'])} video(s); {len(r['failed'])} failed")
    for vid, err in r["failed"].items():
        print(f"  {vid}: {err}", file=sys.stderr)
    return 1 if r["failed"] else 0


def _extra(args) -> list[str]:
    return shlex.split(args.yt_dlp_args or "")


def cmd_search(args) -> int:
    conn = db.connect(args.db)
    queries = list(args.queries)
    if args.all:
        queries += discover.tracked_topics(conn)
    if not queries:
        print("usage: spinwheel search \"keyword\" [\"keyword\" ...]  or  --all (re-run every tracked topic)", file=sys.stderr)
        return 2
    print(f"searching YouTube for {len(dict.fromkeys(queries))} topic(s), {args.max_results} results each"
          + (f", uploaded in the last {args.within}" if args.within else "")
          + (", flat (no publish dates or subscribers)" if args.fast else ""))
    r = discover.search(conn, queries, max_results=args.max_results, fast=args.fast, within=args.within, delay=args.delay,
                        yt_dlp=args.yt_dlp, extra=_extra(args), on_progress=print)
    if not args.no_channels and r["done"]:
        todo = discover.stale_channels(conn, refresh_days=args.refresh_days)
        if todo:
            print(f"refreshing subscriber counts for {len(todo)} channel(s)")
            cr = discover.refresh_channels(conn, todo, yt_dlp=args.yt_dlp, extra=_extra(args))
            print(f"  {len(cr['stored'])} updated, {len(cr['failed'])} without a count")
    for q, err in r["failed"].items():
        print(f"  {q}: FAILED, nothing stored ({err})", file=sys.stderr)
    return 1 if r["failed"] else 0


def cmd_channels(args) -> int:
    conn = db.connect(args.db)
    todo = discover.stale_channels(conn, refresh_days=args.refresh_days, limit=args.max_channels)
    if not todo:
        print(f"every channel with videos has a subscriber count from the last {args.refresh_days:g} days")
        return 0
    print(f"{len(todo)} channel(s) need a subscriber count")
    if args.dry_run:
        print("  " + ", ".join(todo[:12]) + (" ..." if len(todo) > 12 else "") + "\ndry run: nothing fetched")
        return 0
    r = discover.refresh_channels(conn, todo, delay=args.delay, yt_dlp=args.yt_dlp, extra=_extra(args), on_progress=print)
    print(f"updated {len(r['stored'])} channel(s); {len(r['failed'])} failed")
    for cid, err in r["failed"].items():
        print(f"  {cid}: {err}", file=sys.stderr)
    return 1 if r["failed"] else 0


def cmd_enrich(args) -> int:
    conn = db.connect(args.db)
    ids = discover.undated_videos(conn, args.max_videos)
    if not ids:
        print("every video on record has a publish date")
        return 0
    print(f"{len(ids)} video(s) have no publish date (most viewed first)")
    if args.dry_run:
        print("dry run: nothing fetched")
        return 0
    r = discover.enrich(conn, ids, delay=args.delay, yt_dlp=args.yt_dlp, extra=_extra(args))
    print(f"enriched {r['enriched']} of {r['requested']}" + (f"; {r['error']}" if r["error"] else
          "" if r["enriched"] == r["requested"] else ". The rest were refused (YouTube may be asking for a login); retry later or pass --yt-dlp-args"))
    return 0 if r["enriched"] else 1


def cmd_label(args) -> int:
    conn = db.connect(args.db)
    when = utc_iso()
    with conn:
        for cid in args.channels:
            conn.execute("INSERT INTO channel_labels(channel_id, label, source, note, captured_at) VALUES (?,?,?,?,?)",
                         (cid, args.label, args.source, args.note, when))
    print(f"labelled {len(args.channels)} channel(s) as {args.label} ({args.source})")
    return 0


def cmd_api(args) -> int:
    """The YouTube Data API route: no bot checks, but a daily quota. See youtube_api.py."""
    conn = db.connect(args.db)
    try:
        key = youtube_api.get_key(args.key_env)
        what = args.what
        if what == "search":
            if not args.targets:
                print('usage: spinwheel api search "keyword" ["keyword" ...] [--within today]', file=sys.stderr)
                return 2
            print(f"{len(args.targets)} search(es) cost about {100 * len(args.targets) + 2 * len(args.targets)} quota units")
            for q in args.targets:
                r = youtube_api.search(conn, q, key, within=args.within, max_results=args.max_results, order=args.order)
                print(f"  {q}: {r['found']} videos, {r['enriched']} with dates and stats, {r['channels']} channel counts")
        elif what == "enrich":
            ids = discover.undated_videos(conn, args.max_videos)
            print(f"{len(ids)} undated video(s); about {-(-len(ids) // 50)} quota unit(s)")
            print(f"  enriched {youtube_api.videos(conn, ids, key)}" if ids else "  nothing to do")
        elif what == "channels":
            ids = discover.stale_channels(conn, refresh_days=args.refresh_days, limit=args.max_channels)
            print(f"{len(ids)} channel(s) need a subscriber count; about {-(-len(ids) // 50)} quota unit(s)")
            print(f"  updated {youtube_api.channels(conn, ids, key)}" if ids else "  nothing to do")
        elif what == "comments":
            vids = comments.eligible(conn, min_views=args.min_views, refresh_days=args.refresh_days, limit=args.max_videos)
            print(f"{len(vids)} long-form video(s) over {args.min_views:,} views; about {len(vids)} quota unit(s)")
            r = youtube_api.thread_comments(conn, [v["video_id"] for v in vids], key, max_comments=args.max_comments)
            print(f"  stored {sum(r['stored'].values())} comment(s) from {len(r['stored'])} video(s); {len(r['failed'])} failed")
            for vid, err in r["failed"].items():
                print(f"  {vid}: {err}", file=sys.stderr)
            return 1 if r["failed"] else 0
    except youtube_api.ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_status(args) -> int:
    conn = db.connect(args.db)
    q = lambda sql: conn.execute(sql).fetchone()[0]
    n_sug = q('SELECT COUNT(DISTINCT seed) FROM search_suggestions')
    print(f"{args.db}: {q('SELECT COUNT(*) FROM pulls')} pulls, "
          f"{q('SELECT COUNT(DISTINCT video_id) FROM video_snapshots')} videos "
          f"({q('SELECT COUNT(*) FROM video_snapshots')} snapshots), "
          f"{q('SELECT COUNT(DISTINCT channel_id) FROM channel_snapshots')} channels "
          f"({q('SELECT COUNT(*) FROM channel_snapshots')} snapshots), {n_sug} searched keyword(s), "
          f"{q('SELECT COUNT(DISTINCT video_id) FROM comment_snapshots')} video(s) with comments, "
          f"{q('SELECT COUNT(DISTINCT query) FROM searches')} tracked topic(s)")
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

    p = sub.add_parser("comments", help="fetch top comments (via yt-dlp) for videos over a view threshold")
    p.add_argument("--min-views", type=int, default=comments.DEFAULT_MIN_VIEWS, help="only videos with MORE views than this (default 10000)")
    p.add_argument("--max-comments", type=int, default=100, help="top-level comments per video, most liked first (default 100)")
    p.add_argument("--replies", type=int, default=comments.DEFAULT_REPLIES, help="replies to fetch per video in total, at most 3 per thread, "
                   "used to tell whether the channel answered a question (default 150; 0 = none, answers then unknown)")
    p.add_argument("--include-shorts", action="store_true", help="also fetch comments for Shorts (default: long-form only)")
    p.add_argument("--max-videos", type=int, default=25, help="videos per run, most viewed first (default 25)")
    p.add_argument("--refresh-days", type=float, default=14, help="skip videos fetched within this many days (default 14)")
    p.add_argument("--delay", type=float, default=3.0, help="seconds between videos (default 3)")
    p.add_argument("--yt-dlp", default="yt-dlp", metavar="PATH", help="the yt-dlp executable")
    p.add_argument("--yt-dlp-args", metavar="ARGS", help='extra yt-dlp arguments, e.g. "--cookies-from-browser firefox"')
    p.add_argument("--dry-run", action="store_true", help="list the videos that would be fetched, fetch nothing")
    p.set_defaults(fn=cmd_comments)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--yt-dlp", default="yt-dlp", metavar="PATH", help="the yt-dlp executable")
    common.add_argument("--yt-dlp-args", metavar="ARGS", help='extra yt-dlp arguments, e.g. "--cookies-from-browser firefox" when YouTube asks to sign in')

    p = sub.add_parser("search", parents=[common], help="find videos by keyword and track the keyword as a topic")
    p.add_argument("queries", nargs="*", help="keywords to search for")
    p.add_argument("--all", action="store_true", help="also re-run every topic searched before")
    p.add_argument("--max-results", type=int, default=20, help="results per keyword (default 20)")
    p.add_argument("--fast", action="store_true", help="skip the enrichment pass (no publish dates or subscriber counts)")
    p.add_argument("--within", choices=sorted(discover.WITHIN_SP), help="only videos uploaded today / this week / month / year (YouTube's upload-date filter)")
    p.add_argument("--delay", type=float, default=3.0, help="seconds between keywords (default 3)")
    p.add_argument("--no-channels", action="store_true", help="don't refresh subscriber counts afterwards")
    p.add_argument("--refresh-days", type=float, default=7, help="subscriber counts newer than this are not refreshed (default 7)")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("channels", parents=[common], help="refresh subscriber counts for channels that have videos on record")
    p.add_argument("--refresh-days", type=float, default=7, help="skip channels counted within this many days (default 7)")
    p.add_argument("--max-channels", type=int, default=50)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_channels)

    p = sub.add_parser("enrich", parents=[common], help="retry publish dates and subscriber counts for videos that have none")
    p.add_argument("--max-videos", type=int, default=50)
    p.add_argument("--delay", type=float, default=3.0)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_enrich)

    p = sub.add_parser("serve", parents=[common], help="serve the dashboard locally with a Refresh button that re-pulls your data")
    p.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    p.add_argument("--short-max", type=int, default=60, metavar="SECONDS", help="as for `dashboard` (default 60)")
    p.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    p.add_argument("-o", "--output", default="dashboard.html", metavar="FILE",
                   help="after each refresh, also rewrite this file so a saved dashboard.html shows the new data (default dashboard.html; '' = don't)")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("autostart", parents=[common], help="macOS: start the refresh helper at login, so the dashboard's button always works")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--port", type=int, default=8765, help="must match the dashboard button (default 8765)")
    p.add_argument("-o", "--output", default="dashboard.html", metavar="FILE", help="dashboard file to rewrite after each refresh")
    p.set_defaults(fn=cmd_autostart)

    p = sub.add_parser("label", help="record a judgment about channels, e.g. faceless (kept as history; the latest wins)")
    p.add_argument("label", choices=["faceless", "face", "unsure"])
    p.add_argument("channels", nargs="+", help="channel ids (UC...)")
    p.add_argument("--note", help="why, e.g. what you saw")
    p.add_argument("--source", default="manual", choices=["manual", "thumbnail-review"])
    p.set_defaults(fn=cmd_label)

    p = sub.add_parser("api", help="use the official YouTube Data API instead of scraping (needs a key in $YOUTUBE_API_KEY)")
    p.add_argument("what", choices=["search", "enrich", "channels", "comments"])
    p.add_argument("targets", nargs="*", help="for search: the keywords")
    p.add_argument("--key-env", default=youtube_api.DEFAULT_KEY_ENV, help="environment variable holding the key")
    p.add_argument("--within", choices=sorted(discover.WITHIN_SP), help="search only videos uploaded in this window")
    p.add_argument("--order", default="viewCount", choices=["viewCount", "relevance", "date", "rating"], help="search order (default viewCount)")
    p.add_argument("--max-results", type=int, default=50)
    p.add_argument("--max-videos", type=int, default=50)
    p.add_argument("--max-channels", type=int, default=200)
    p.add_argument("--max-comments", type=int, default=100)
    p.add_argument("--min-views", type=int, default=comments.DEFAULT_MIN_VIEWS)
    p.add_argument("--refresh-days", type=float, default=7)
    p.set_defaults(fn=cmd_api)

    p = sub.add_parser("status", help="what's in the database")
    p.set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
