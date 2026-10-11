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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any

from . import ideas as ideas_mod
from . import topics as topics_mod

MATURE_DAYS = 14
MIN_GROUP = 3            # smallest group we'll draw a comparison from
MIN_MATURE_FOR_PATTERNS = 8
OUTLIER_X = 2.0          # video outlier: >= this many x its channel's median; channel outlier: same, vs other channels
MIN_CHANNELS_TO_COMPARE = 3   # a channel needs at least 2 others to be compared with
BREAKOUTS_SHOWN = 15
UNKNOWN_CHANNEL = "(unknown channel)"

FORMATS = ("all", "long", "short")
SHORT_MAX_SECONDS = 60          # fallback when a pull has neither a /shorts/ URL nor width/height
SHORTS_PORTRAIT_MAX = 180       # vertical videos up to 3 minutes are Shorts (YouTube's limit since Oct 2024)
FORMAT_LABELS = {"all": "All videos", "long": "Long-form", "short": "Shorts"}
SHORT_BUCKETS = [
    (0, 15, "Under 15 s"), (15, 30, "15–30 s"), (30, 45, "30–45 s"), (45, 60, "45–60 s"), (60, float("inf"), "1–3 min"),
]
DURATION_BUCKETS = [
    (0, 60, "Under 1 min"), (60, 240, "1–4 min"), (240, 480, "4–8 min"),
    (480, 900, "8–15 min"), (900, 1800, "15–30 min"), (1800, float("inf"), "30+ min"),
]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

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


def classify_format(snaps, short_max: int = SHORT_MAX_SECONDS) -> tuple[str, str]:
    """('short' | 'long', how we knew). Best evidence first:
      url       a /shorts/ URL
      shape     width and height known: vertical and <= 3 min is a Short, anything else is long-form
      duration  neither of the above: <= short_max seconds is a Short (an approximation)
      none      nothing to go on: treated as long-form"""
    if any(s["page_url"] and "/shorts/" in s["page_url"] for s in snaps):
        return "short", "url"
    dur = next((s["duration_s"] for s in reversed(snaps) if s["duration_s"] is not None), None)
    wh = next(((s["width"], s["height"]) for s in reversed(snaps) if s["width"] and s["height"]), None)
    if wh:
        w, h = wh
        return ("short" if h > w and (dur is None or dur <= SHORTS_PORTRAIT_MAX) else "long"), "shape"
    if dur is not None:
        return ("short" if dur <= short_max else "long"), "duration"
    return "long", "none"


def _build_videos(v_rows, short_max: int = SHORT_MAX_SECONDS) -> dict[str, dict]:
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
        tags = []
        for s_ in reversed(snaps):
            try:
                tags = [t for t in json.loads(s_["tags"] or "[]") if isinstance(t, str)]
            except (json.JSONDecodeError, TypeError):
                tags = []
            if tags:
                break
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

        fmt, basis = classify_format(snaps, short_max)
        videos[vid] = {
            "id": vid, "channel_id": cid, "title": title, "format": fmt, "format_basis": basis,
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
            "_phrases": topics_mod.phrases_for(title, tags),
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


def _duration_label(s: int | None, buckets=DURATION_BUCKETS) -> str | None:
    if s is None:
        return None
    return next(label for lo, hi, label in buckets if lo <= s < hi)


def _fmt(n: float) -> str:
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            v = n / div
            return f"{v:.1f}".rstrip("0").rstrip(".") + suf if v < 100 else f"{v:.0f}{suf}"
    return f"{n:.0f}"


def _short(t: str, n: int = 60) -> str:
    return t if len(t) <= n else t[: n - 1] + "…"


def analyze_channel(vids: list[dict], subs: int | None = None, mode: str = "all") -> dict:
    mature = [v for v in vids if v["mature"]]
    base = _med([v["views"] for v in mature])
    if len(mature) >= MIN_GROUP:
        for v in mature:
            v["score"] = _lift(v["views"], base)

    # duration
    buckets = SHORT_BUCKETS if mode == "short" else DURATION_BUCKETS
    dg = _group_stats(mature, lambda v: _duration_label(v["duration_s"], buckets))
    durations = [{"label": lbl, "n": len(dg[lbl]), "median_views": _med([v["views"] for v in dg[lbl]])}
                 for _, _, lbl in buckets if lbl in dg]

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

    # topics: phrases from titles and tags
    topics_all = topics_mod.channel_topics(mature, base)

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

    findings = _findings(mature, base, durations, weekdays, features, topics_all, movers, vids, subs)

    return {
        "videos": sorted(vids, key=lambda v: v["published"] or "", reverse=True),
        "median_views": base, "n_mature": len(mature),
        "median_engagement": _med([v["engagement"] for v in mature if v["engagement"] is not None]),
        "durations": durations, "weekdays": weekdays, "features": features,
        "topics_top": topics_all[:topics_mod.TOP_SHOWN],
        "topics_weak": [k for k in topics_all[::-1] if (k["lift"] or 0) < 1 and k["n"] >= MIN_GROUP][:5],
        "topics_all": topics_all, "gaps": [], "ideas": [],
        "cadence": cadence, "movers": [v["id"] for v in movers], "title_changes": changes,
        "findings": findings,
    }


def _findings(mature, base, durations, weekdays, features, kws, movers, vids, subs=None) -> list[dict]:
    out: list[dict] = []
    n = len(mature)
    if n < MIN_MATURE_FOR_PATTERNS:
        out.append({"kind": "caution", "text":
                    f"Only {n} video{'s' if n != 1 else ''} older than {MATURE_DAYS} days so far. "
                    f"Patterns below are directional; they firm up past {MIN_MATURE_FOR_PATTERNS}."})

    scored = [v for v in mature if v["score"] is not None]
    if scored:
        top = max(scored, key=lambda v: v["score"])
        if top["score"] >= OUTLIER_X:
            size = f" on a channel with {_fmt(subs)} subscribers" if subs else ""
            out.append({"kind": "outlier", "text":
                        f"“{_short(top['title'])}” is the biggest outlier: {top['score']:.1f}× the channel median "
                        f"({_fmt(top['views'])} views{size}). Its topic or angle is the first candidate for a follow-up."})

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
                        f"Topic: videos with “{k['phrase']}” in the title or tags have a median of {_fmt(k['median_views'])} views, "
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

def build_dashboard_data(conn: sqlite3.Connection, short_max: int = SHORT_MAX_SECONDS) -> dict:
    """Everything the page shows, computed three times: for all videos, for long-form only and for Shorts
    only. Each slice is analysed on its own (its own medians, outliers, topics, gaps, ideas), because a
    Short's views say nothing about how a ten-minute video is doing."""
    v_rows, c_rows, pulls = _load(conn)
    videos = _build_videos(v_rows, short_max)
    history = {vid: v.pop("history") for vid, v in videos.items()}     # shared by all slices, sent once

    ch_snaps: dict[str, list] = defaultdict(list)
    for r in c_rows:
        ch_snaps[r["channel_id"]].append(r)
    channel_ids = sorted({v["channel_id"] for v in videos.values()} | set(ch_snaps))

    ref = parse_ts(max([r["captured_at"] for r in v_rows] + [r["captured_at"] for r in c_rows])) \
        if (v_rows or c_rows) else None
    suggestions = _suggestions(conn)

    variants = {}
    for mode in FORMATS:
        subset = [dict(v) for v in videos.values() if mode == "all" or v["format"] == mode]
        variants[mode] = _build_variant(mode, subset, channel_ids, ch_snaps, ref, suggestions)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_through": ref.strftime("%Y-%m-%dT%H:%M:%SZ") if ref else None,
        "pulls": len(pulls), "first_pull": pulls[0][0] if pulls else None,
        "video_snapshots": len(v_rows), "channel_snapshots": len(c_rows),
        "mature_days": MATURE_DAYS, "min_group": MIN_GROUP, "outlier_x": OUTLIER_X,
        "min_channels": MIN_CHANNELS_TO_COMPARE,
        "short_max": short_max, "format_basis": dict(Counter(v["format_basis"] for v in videos.values())),
        "formats": [{"id": m, "label": FORMAT_LABELS[m]} for m in FORMATS],
        "variants": variants, "history": history, "suggestions": suggestions,
    }


def _build_variant(mode: str, subset: list[dict], channel_ids: list[str], ch_snaps: dict, ref, suggestions: dict) -> dict:
    by_channel: dict[str, list[dict]] = defaultdict(list)
    for v in subset:
        by_channel[v["channel_id"]].append(v)

    channels, detail = [], {}
    for cid in channel_ids:
        snaps = ch_snaps.get(cid, [])
        title = next((s["title"] for s in reversed(snaps) if s["title"]), cid)
        subs_pts = [s for s in snaps if s["subscribers"] is not None]
        subs = subs_pts[-1]["subscribers"] if subs_pts else None
        a = analyze_channel(by_channel.get(cid, []), subs, mode)
        for v in a["videos"]:
            v["subs"] = subs
            v["views_per_sub"] = v["views"] / subs if subs and v["views"] is not None else None
            v["video_outlier"] = v["score"] is not None and v["score"] >= OUTLIER_X
            v["channel_outlier"] = None          # filled in once every channel is known
        recent_uploads = 0
        if ref:
            recent_uploads = sum(1 for v in a["videos"]
                                 if v["published"] and _days(ref, parse_ts(v["published"])) <= 90)
        delta = None
        if len(subs_pts) >= 2:
            delta = {"change": subs - subs_pts[0]["subscribers"],
                     "days": _days(parse_ts(subs_pts[-1]["captured_at"]), parse_ts(subs_pts[0]["captured_at"]))}
        channels.append({
            "id": cid, "title": title, "subscribers": subs, "subs_delta": delta,
            "n_videos": len(a["videos"]), "median_views": a["median_views"],
            "median_engagement": a["median_engagement"], "uploads_90d": recent_uploads,
            "views_per_sub": a["median_views"] / subs if subs and a["median_views"] else None,
            "size_ratio": None, "size_ref": None, "size_outlier": None,
            "format": FORMAT_LABELS[mode],
            "last_pulled": max([s["captured_at"] for s in snaps] +
                               [v["captured_at"] for v in a["videos"]] or [""]),
        })
        a["growth"] = [[s["captured_at"], s["subscribers"], s["view_count"], s["video_count"]] for s in snaps]
        detail[cid] = a

    _compare_channel_sizes(channels, detail)
    titles = {c["id"]: c["title"] for c in channels}
    for c in channels:
        a = detail[c["id"]]
        others = {oc: detail[oc]["videos"] for oc in detail if oc != c["id"]}
        a["gaps"] = topics_mod.find_gaps(a["videos"], others, titles) if others else []
        a["ideas"] = ideas_mod.build_ideas(channel=c, a=a, gaps=a["gaps"], suggestions=suggestions,
                                           ref=ref, outlier_x=OUTLIER_X)
        a["packaging"] = ideas_mod.packaging(a)
        a["style"] = ideas_mod.style_titles(a["videos"])
        del a["topics_all"]
    for v in subset:                       # internal helpers, not for the page
        v.pop("_phrases", None)
    channels.sort(key=lambda c: (-c["n_videos"], c["title"]))
    return {"channels": channels, "detail": detail, "breakouts": _breakouts(detail)}


def _compare_channel_sizes(channels: list[dict], detail: dict) -> None:
    """Flag a channel whose typical video earns >= OUTLIER_X times the views-per-subscriber of the
    other tracked channels. Left as None (not False) when there aren't enough channels to say."""
    for c in channels:
        others = [o["views_per_sub"] for o in channels if o is not c and o["views_per_sub"] is not None]
        if c["views_per_sub"] is None or len(others) + 1 < MIN_CHANNELS_TO_COMPARE:
            continue
        c["size_ref"] = median(others)
        c["size_ratio"] = c["views_per_sub"] / c["size_ref"]
        c["size_outlier"] = c["size_ratio"] >= OUTLIER_X
        a = detail[c["id"]]
        for v in a["videos"]:
            v["channel_outlier"] = c["size_outlier"]
        if c["size_outlier"]:
            a["findings"].insert(0, {"kind": "channel", "text":
                f"Channel outlier: a typical video here gets {c['views_per_sub']:.1f}× its {_fmt(c['subscribers'])} "
                f"subscribers in views, {c['size_ratio']:.1f}× what the other tracked channels get. "
                f"The channel as a whole is beating its size, so study its format and topics, not only its top video."})


def _suggestions(conn: sqlite3.Connection) -> dict:
    """seed -> {at, prev_at, items: [{text, rank, new}]} from the two most recent fetches of each seed.
    `new` marks a phrase that wasn't suggested the time before. Order: the seed's own suggestions
    first (autocomplete order), then phrases found by expanding with a letter."""
    rows = conn.execute("SELECT captured_at, seed, query, suggestion, rank FROM search_suggestions "
                        "ORDER BY captured_at, id").fetchall()
    by_seed: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_seed[r["seed"]][r["captured_at"]].append(r)
    out = {}
    for seed, caps in by_seed.items():
        times = sorted(caps)
        latest = caps[times[-1]]
        prev = {r["suggestion"] for r in caps[times[-2]]} if len(times) > 1 else None
        seen, items = set(), []
        for r in sorted(latest, key=lambda r: (r["query"] != seed, r["rank"])):
            if r["suggestion"] not in seen:
                seen.add(r["suggestion"])
                items.append({"text": r["suggestion"], "rank": r["rank"],
                              "new": prev is not None and r["suggestion"] not in prev})
        out[seed] = {"at": times[-1], "prev_at": times[-2] if len(times) > 1 else None, "items": items}
    return out


def top_seeds(conn: sqlite3.Connection, n: int) -> list[str]:
    """Phrases worth asking autocomplete about: the best topics and untested gaps across channels."""
    v = build_dashboard_data(conn)["variants"]["all"]
    seeds: list[str] = []
    for c in v["channels"]:
        a = v["detail"][c["id"]]
        for phrase in [g["phrase"] for g in a["gaps"]] + [t["phrase"] for t in a["topics_top"]]:
            if len(phrase.split()) <= 3 and phrase not in seeds:
                seeds.append(phrase)
    return seeds[:n]


def _breakouts(detail: dict) -> list[list[str]]:
    """Mature videos across all channels ranked by views per subscriber: who got far more views than
    their audience size would suggest. [[channel_id, video_id], ...]"""
    if len(detail) < 2:
        return []
    rows = [(v["views_per_sub"], cid, v["id"]) for cid, a in detail.items() for v in a["videos"]
            if v["mature"] and v["views_per_sub"] is not None]
    rows.sort(reverse=True)
    return [[cid, vid] for _, cid, vid in rows[:BREAKOUTS_SHOWN]]


def to_json(data: dict) -> str:
    # "</" must not appear inside a <script> block
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
