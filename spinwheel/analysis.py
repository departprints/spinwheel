"""Turn stored snapshots into the numbers the dashboard shows.

All logic lives here (not in the page) so it can be tested. The page only renders.
Patterns are computed on "mature" videos (>= MATURE_DAYS old) so a video that is three
days into its life is not compared with one that has had a year to collect views.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any

MATURE_DAYS = 14
MIN_GROUP = 3            # smallest group we'll draw a comparison from
MIN_MATURE_FOR_PATTERNS = 8
UNKNOWN_CHANNEL = "(unknown channel)"

DURATION_BUCKETS = [
    (0, 60, "Under 1 min"), (60, 240, "1–4 min"), (240, 480, "4–8 min"),
    (480, 900, "8–15 min"), (900, 1800, "15–30 min"), (1800, float("inf"), "30+ min"),
]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_STOP = set("""a about after all also am an and any are as at be been being but by can could did do does
doing for from get got had has have how i if in into is it its just like make me more most my no not of
off on one or our out over so some than that the their them then there these they this to up us use
using very was we were what when where which who why will with you your yours vs per new""".split())

_QUESTION_START = re.compile(r"^(how|why|what|when|which|can|should|is|are|do|does|will)\b", re.I)

TITLE_FEATURES: list[tuple[str, Any]] = [
    ("Has a number", lambda t: bool(re.search(r"\d", t))),
    ("Asks a question", lambda t: "?" in t or bool(_QUESTION_START.match(t.strip()))),
    ("45 characters or fewer", lambda t: len(t) <= 45),
    ("Has brackets or parentheses", lambda t: bool(re.search(r"[\[\(]", t))),
    ("Has an ALL-CAPS word", lambda t: bool(re.search(r"\b[A-Z]{3,}\b", t))),
    ("First person (I / my / we)", lambda t: bool(re.search(r"\b(i|i'm|i've|my|we|our)\b", t, re.I))),
]


def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds() / 86400


def _med(xs: list[float]) -> float | None:
    return median(xs) if xs else None


def tokenize(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", title.lower())
    return {w.strip("'") for w in words if len(w) >= 3 and w not in _STOP and not w.isdigit()}


def recent_rate(points: list[tuple[datetime, int]], window_days: float = 7) -> dict | None:
    """Views/day gained recently, from the snapshot history. None if there's no usable history."""
    if len(points) < 2:
        return None
    t_last, v_last = points[-1]
    older = [p for p in points[:-1] if _days(t_last, p[0]) >= window_days]
    base = older[-1] if older else points[0]
    span = _days(t_last, base[0])
    if span < 0.5:
        return None
    gain = v_last - base[1]
    return {"per_day": gain / span, "gain": gain, "span_days": span}


# ------------------------------------------------------------------- loading

def _load(conn: sqlite3.Connection):
    v_rows = conn.execute("SELECT * FROM video_snapshots ORDER BY captured_at, id").fetchall()
    c_rows = conn.execute("SELECT * FROM channel_snapshots ORDER BY captured_at, id").fetchall()
    pulls = conn.execute("SELECT pulled_at FROM pulls ORDER BY pulled_at").fetchall()
    return v_rows, c_rows, pulls


def _build_videos(v_rows) -> dict[str, dict]:
    by_vid: dict[str, list] = defaultdict(list)
    for r in v_rows:
        by_vid[r["video_id"]].append(r)

    videos: dict[str, dict] = {}
    for vid, snaps in by_vid.items():
        last = snaps[-1]
        counted = [s for s in snaps if s["view_count"] is not None]
        latest = counted[-1] if counted else last
        pub = next((s["published_at"] for s in reversed(snaps) if s["published_at"]), None)
        cid = next((s["channel_id"] for s in reversed(snaps) if s["channel_id"]), None) or UNKNOWN_CHANNEL
        title = next((s["title"] for s in reversed(snaps) if s["title"]), vid)
        views, likes, comments = latest["view_count"], latest["like_count"], latest["comment_count"]
        t_cap = parse_ts(latest["captured_at"])
        pub_dt = parse_ts(pub) if pub else None
        age = _days(t_cap, pub_dt) if pub_dt else None

        points = [(parse_ts(s["captured_at"]), s["view_count"]) for s in counted]
        rate = recent_rate(points)
        lifetime_vpd = views / max(age, 1) if views is not None and age is not None else None

        title_changes = []
        prev_title = None
        for s in snaps:
            if s["title"] and prev_title and s["title"] != prev_title:
                title_changes.append({"at": s["captured_at"], "from": prev_title, "to": s["title"],
                                      "views": s["view_count"]})
            if s["title"]:
                prev_title = s["title"]

        videos[vid] = {
            "id": vid, "channel_id": cid, "title": title,
            "published": pub, "age_days": age,
            "duration_s": next((s["duration_s"] for s in reversed(snaps) if s["duration_s"] is not None), None),
            "views": views, "likes": likes, "comments": comments,
            "engagement": ((likes or 0) + (comments or 0)) / views if views and (likes is not None or comments is not None) else None,
            "vpd": lifetime_vpd,
            "recent_per_day": rate["per_day"] if rate else None,
            "recent_gain": rate["gain"] if rate else None,
            "recent_span": rate["span_days"] if rate else None,
            "mature": bool(views is not None and age is not None and age >= MATURE_DAYS),
            "score": None,
            "history": [[s["captured_at"], s["view_count"]] for s in counted],
            "title_changes": title_changes,
            "captured_at": latest["captured_at"],
            "thumb": next((s["thumbnail_url"] for s in reversed(snaps) if s["thumbnail_url"]), None),
        }
    return videos


# --------------------------------------------------------------- per channel

def _group_stats(vids: list[dict], key) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = defaultdict(list)
    for v in vids:
        k = key(v)
        if k is not None:
            g[k].append(v)
    return g


def _lift(m: float | None, base: float | None) -> float | None:
    return m / base if m is not None and base else None


def _duration_label(s: int | None) -> str | None:
    if s is None:
        return None
    return next(label for lo, hi, label in DURATION_BUCKETS if lo <= s < hi)


def _fmt(n: float) -> str:
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            v = n / div
            return f"{v:.1f}".rstrip("0").rstrip(".") + suf if v < 100 else f"{v:.0f}{suf}"
    return f"{n:.0f}"


def _short(t: str, n: int = 60) -> str:
    return t if len(t) <= n else t[: n - 1] + "…"


def analyze_channel(vids: list[dict]) -> dict:
    mature = [v for v in vids if v["mature"]]
    base = _med([v["views"] for v in mature])
    if len(mature) >= MIN_GROUP:
        for v in mature:
            v["score"] = _lift(v["views"], base)

    # duration
    dg = _group_stats(mature, lambda v: _duration_label(v["duration_s"]))
    durations = [{"label": lbl, "n": len(dg[lbl]), "median_views": _med([v["views"] for v in dg[lbl]])}
                 for _, _, lbl in DURATION_BUCKETS if lbl in dg]

    # weekday (UTC date of publish)
    wg = _group_stats(mature, lambda v: WEEKDAYS[parse_ts(v["published"]).weekday()] if v["published"] else None)
    weekdays = [{"label": d, "n": len(wg.get(d, [])), "median_views": _med([v["views"] for v in wg.get(d, [])])}
                for d in WEEKDAYS]

    # title features
    features = []
    for name, fn in TITLE_FEATURES:
        w = [v for v in mature if fn(v["title"])]
        wo = [v for v in mature if not fn(v["title"])]
        mw, mo = _med([v["views"] for v in w]), _med([v["views"] for v in wo])
        ok = len(w) >= MIN_GROUP and len(wo) >= MIN_GROUP
        features.append({"label": name, "n_with": len(w), "n_without": len(wo),
                         "median_with": mw, "median_without": mo,
                         "lift": _lift(mw, mo) if ok else None})

    # keywords (words that always appear in exactly the same videos are merged into one entry)
    kw: dict[str, list[dict]] = defaultdict(list)
    for v in mature:
        for w in tokenize(v["title"]):
            kw[w].append(v)
    merged: dict[frozenset, list[str]] = defaultdict(list)
    for w, vs in kw.items():
        if len(vs) >= 2:
            merged[frozenset(v["id"] for v in vs)].append(w)
    kws = []
    for ids, words in merged.items():
        vs = kw[words[0]]
        m = _med([v["views"] for v in vs])
        kws.append({"word": " + ".join(sorted(words)), "n": len(vs), "median_views": m, "lift": _lift(m, base)})
    kws.sort(key=lambda k: (-(k["lift"] or 0), -k["n"], k["word"]))

    # cadence: uploads per month for the last 12 months with data
    months: dict[str, int] = defaultdict(int)
    for v in vids:
        if v["published"]:
            months[v["published"][:7]] += 1
    cadence = [{"month": m, "n": months[m]} for m in sorted(months)][-12:]

    movers = sorted((v for v in vids if v["recent_per_day"] is not None),
                    key=lambda v: -v["recent_per_day"])[:10]

    changes = sorted((dict(c, video_id=v["id"], video=v["title"]) for v in vids for c in v["title_changes"]),
                     key=lambda c: c["at"], reverse=True)[:25]

    findings = _findings(mature, base, durations, weekdays, features, kws, movers, vids)

    return {
        "videos": sorted(vids, key=lambda v: v["published"] or "", reverse=True),
        "median_views": base, "n_mature": len(mature),
        "median_engagement": _med([v["engagement"] for v in mature if v["engagement"] is not None]),
        "durations": durations, "weekdays": weekdays, "features": features,
        "keywords_top": kws[:10], "keywords_bottom": [k for k in kws[::-1] if (k["lift"] or 0) < 1][:5],
        "cadence": cadence, "movers": [v["id"] for v in movers], "title_changes": changes,
        "findings": findings,
    }


def _findings(mature, base, durations, weekdays, features, kws, movers, vids) -> list[dict]:
    out: list[dict] = []
    n = len(mature)
    if n < MIN_MATURE_FOR_PATTERNS:
        out.append({"kind": "caution", "text":
                    f"Only {n} video{'s' if n != 1 else ''} older than {MATURE_DAYS} days so far. "
                    f"Patterns below are directional; they firm up past {MIN_MATURE_FOR_PATTERNS}."})

    scored = [v for v in mature if v["score"] is not None]
    if scored:
        top = max(scored, key=lambda v: v["score"])
        if top["score"] >= 1.5:
            out.append({"kind": "outlier", "text":
                        f"“{_short(top['title'])}” is the biggest outlier: {top['score']:.1f}× the channel median "
                        f"({_fmt(top['views'])} views). Its topic or angle is the first candidate for a follow-up."})

    if n >= MIN_MATURE_FOR_PATTERNS:
        d = [x for x in durations if x["n"] >= MIN_GROUP]
        if len(d) >= 2:
            best = max(d, key=lambda x: x["median_views"])
            lift = _lift(best["median_views"], base)
            if lift and lift >= 1.25:
                out.append({"kind": "length", "text":
                            f"Length: {best['label']} videos have a median of {_fmt(best['median_views'])} views, "
                            f"{lift:.1f}× the channel median (n={best['n']} of {n})."})
        w = [x for x in weekdays if x["n"] >= MIN_GROUP]
        if len(w) >= 2:
            best = max(w, key=lambda x: x["median_views"])
            lift = _lift(best["median_views"], base)
            if lift and lift >= 1.25:
                out.append({"kind": "weekday", "text":
                            f"Publish day: {best['label']} uploads have a median of {_fmt(best['median_views'])} views, "
                            f"{lift:.1f}× the channel median (n={best['n']}; dates are UTC)."})
        strong = [f for f in features if f["lift"] and (f["lift"] >= 1.3 or f["lift"] <= 1 / 1.3)]
        strong.sort(key=lambda f: -abs(math.log(f["lift"])))
        for f in strong[:2]:
            verb = "outperform" if f["lift"] > 1 else "underperform"
            ratio = f["lift"] if f["lift"] > 1 else 1 / f["lift"]
            out.append({"kind": "title", "text":
                        f"Titles — “{f['label']}”: those videos {verb} the rest by {ratio:.1f}× on median views "
                        f"(n={f['n_with']} vs {f['n_without']})."})
        if kws and kws[0]["lift"] and kws[0]["lift"] >= 1.5:
            k = kws[0]
            out.append({"kind": "keyword", "text":
                        f"Topic: videos with “{k['word']}” in the title have a median of {_fmt(k['median_views'])} views, "
                        f"{k['lift']:.1f}× the channel median (n={k['n']})."})

    # momentum: recent pace well above the video's own lifetime average
    def accel(v):
        return v["recent_per_day"] / v["vpd"] if v["vpd"] and v["recent_per_day"] else 0
    hot = max(movers, key=accel, default=None)
    if hot and accel(hot) >= 1.2 and hot["recent_gain"] > 0:
        out.append({"kind": "momentum", "text":
                    f"Momentum: “{_short(hot['title'])}” gained {_fmt(hot['recent_gain'])} views over the last "
                    f"{hot['recent_span']:.0f} days ({_fmt(hot['recent_per_day'])}/day, {accel(hot):.1f}× its lifetime "
                    f"average). Worth a pinned comment, a community post, or a follow-up while it's moving."})
    return out


# ------------------------------------------------------------------- top level

def build_dashboard_data(conn: sqlite3.Connection) -> dict:
    v_rows, c_rows, pulls = _load(conn)
    videos = _build_videos(v_rows)

    by_channel: dict[str, list[dict]] = defaultdict(list)
    for v in videos.values():
        by_channel[v["channel_id"]].append(v)

    ch_snaps: dict[str, list] = defaultdict(list)
    for r in c_rows:
        ch_snaps[r["channel_id"]].append(r)

    ref = parse_ts(max([r["captured_at"] for r in v_rows] + [r["captured_at"] for r in c_rows])) \
        if (v_rows or c_rows) else None

    channels = []
    detail = {}
    for cid in sorted(set(by_channel) | set(ch_snaps)):
        snaps = ch_snaps.get(cid, [])
        title = next((s["title"] for s in reversed(snaps) if s["title"]), cid)
        subs_pts = [s for s in snaps if s["subscribers"] is not None]
        a = analyze_channel(by_channel.get(cid, []))
        recent_uploads = 0
        if ref:
            recent_uploads = sum(1 for v in a["videos"]
                                 if v["published"] and _days(ref, parse_ts(v["published"])) <= 90)
        subs = subs_pts[-1]["subscribers"] if subs_pts else None
        delta = None
        if len(subs_pts) >= 2:
            delta = {"change": subs - subs_pts[0]["subscribers"],
                     "days": _days(parse_ts(subs_pts[-1]["captured_at"]), parse_ts(subs_pts[0]["captured_at"]))}
        channels.append({
            "id": cid, "title": title, "subscribers": subs, "subs_delta": delta,
            "n_videos": len(a["videos"]), "median_views": a["median_views"],
            "median_engagement": a["median_engagement"], "uploads_90d": recent_uploads,
            "last_pulled": max([s["captured_at"] for s in snaps] +
                               [v["captured_at"] for v in a["videos"]] or [""]),
        })
        a["growth"] = [[s["captured_at"], s["subscribers"], s["view_count"], s["video_count"]] for s in snaps]
        detail[cid] = a

    channels.sort(key=lambda c: (-c["n_videos"], c["title"]))
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_through": ref.strftime("%Y-%m-%dT%H:%M:%SZ") if ref else None,
        "pulls": len(pulls), "first_pull": pulls[0][0] if pulls else None,
        "video_snapshots": len(v_rows), "channel_snapshots": len(c_rows),
        "mature_days": MATURE_DAYS, "min_group": MIN_GROUP,
        "channels": channels, "detail": detail,
    }


def to_json(data: dict) -> str:
    # "</" must not appear inside a <script> block
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
