"""Keyword research over tracked topics: the formulas from the YouTube Outlier Finder, with the parts
that page leaves undefined filled in explicitly (each choice is marked CHOICE and listed in the README).

A topic is a keyword you searched (`spinwheel search`); its videos are the ones that search surfaced.

  Gap Score      = average views / number of channels covering the topic          (as on the page)
  Difficulty     = 65% log-scaled average subscribers of the competing channels + 35% log-scaled video volume
  Opportunity    = 60% log-scaled average views + 40% views-per-subscriber
  Ranking        = highest opportunity first, then lowest difficulty

CHOICE  video volume       the number of tracked videos for the topic (or whose title has the phrase).
                           Real search volume is not available from free data.
CHOICE  log-scaled         min(100, 100 * log10(1 + x) / log10(1 + cap)), with caps below.
CHOICE  views-per-subscriber  the mean of views / subscribers over the videos, where views equal to the
                           channel's subscribers (the FIRE line) scores 100.
CHOICE  heat               TRENDING / ACTIVE / STEADY from recent uploads (see heat()).
A long-tail phrase must appear in at least 2 titles for the same topic, as the page says.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from statistics import mean, median

from . import topics as T

SUBS_CAP = 10_000_000
VIEWS_CAP = 1_000_000
VOLUME_CAP = 50
MIN_TITLES = 2
RECENT_DAYS = 30
LONGTAIL_SHOWN = 15
HEAT_MIN_DATED = 5
THIN = 3                 # fewer tracked videos than this and a topic's numbers are mostly noise


def lognorm(x: float | None, cap: float) -> float:
    if not x or x <= 0:
        return 0.0
    return min(100.0, 100.0 * math.log10(1 + x) / math.log10(1 + cap))


def difficulty(avg_subs: float | None, volume: int) -> int:
    return round(0.65 * lognorm(avg_subs, SUBS_CAP) + 0.35 * lognorm(volume, VOLUME_CAP))


def opportunity(avg_views: float | None, views_per_sub: float | None) -> int:
    return round(0.60 * lognorm(avg_views, VIEWS_CAP) + 0.40 * min(100.0, 100.0 * (views_per_sub or 0)))


def gap_score(avg_views: float | None, channels: int) -> float | None:
    return avg_views / channels if avg_views is not None and channels else None


def _views_per_sub(vids: list[dict]) -> float | None:
    xs = [v["views"] / v["subs"] for v in vids if v["subs"] and v["views"] is not None]
    return mean(xs) if xs else None


def heat(vids: list[dict], ref: datetime | None, parse_ts) -> str | None:
    """CHOICE. Needs at least HEAT_MIN_DATED videos with a publish date, else None (unknown).
      TRENDING  3+ videos in the last 30 days, at least 25% of the topic, and their median views/day
                at least 1.5x that of the older videos (which need 3+ too): new uploads are outpacing old ones
      ACTIVE    2+ videos in the last 30 days
      STEADY    otherwise"""
    if not ref:
        return None
    dated = []
    for v in vids:
        if v["published"] and v["views"] is not None:
            age = (ref - parse_ts(v["published"])).total_seconds() / 86400
            dated.append((age, v["views"] / max(age, 1)))
    if len(dated) < HEAT_MIN_DATED:
        return None
    recent = [r for a, r in dated if a <= RECENT_DAYS]
    old = [r for a, r in dated if a > RECENT_DAYS]
    if len(recent) >= 3 and len(old) >= 3 and len(recent) / len(dated) >= 0.25 and median(recent) >= 1.5 * median(old):
        return "TRENDING"
    return "ACTIVE" if len(recent) >= 2 else "STEADY"


def long_tail(vids: list[dict], topic: str) -> list[dict]:
    """Multi-word phrases found in at least MIN_TITLES titles of the topic's videos, scored and ranked."""
    by: dict[str, list[dict]] = defaultdict(list)
    for v in vids:
        for p in T.long_tail_phrases(v["title"]):
            if p != topic:
                by[p].append(v)
    rows = []
    for label, also, vs in T._merge(by, MIN_TITLES):
        chans = {v["channel_id"] for v in vs}
        subs_by_channel = {v["channel_id"]: v["subs"] for v in vs if v["subs"]}
        avg_subs = mean(subs_by_channel.values()) if subs_by_channel else None
        avg_views = mean(v["views"] for v in vs)
        rows.append({"phrase": label, "also": also, "videos": len(vs), "channels": len(chans),
                     "avg_views": avg_views, "views_per_sub": _views_per_sub(vs), "avg_subs": avg_subs,
                     "difficulty": difficulty(avg_subs, len(vs)),
                     "opportunity": opportunity(avg_views, _views_per_sub(vs))})
    rows.sort(key=lambda r: (-r["opportunity"], r["difficulty"], r["phrase"]))
    return rows[:LONGTAIL_SHOWN]


def tracked_topics(slice_videos: dict[str, dict], searches: dict[str, list[str]], suggestions: dict,
                   ref: datetime | None, parse_ts) -> list[dict]:
    """slice_videos: video id -> video dict (this format slice, all channels). searches: topic -> video ids."""
    out = []
    for topic, ids in searches.items():
        vids = [slice_videos[i] for i in dict.fromkeys(ids) if i in slice_videos and slice_videos[i]["views"] is not None]
        if not vids:
            continue
        chans = {v["channel_id"] for v in vids}
        avg_views = mean(v["views"] for v in vids)
        sug = suggestions.get(topic)
        out.append({
            "topic": topic, "videos": len(vids), "thin": len(vids) < THIN, "channels": len(chans), "total_views": sum(v["views"] for v in vids),
            "avg_views": avg_views, "gap_score": gap_score(avg_views, len(chans)),
            "heat": heat(vids, ref, parse_ts),
            "tiers": {t: sum(1 for v in vids if v["tier"] == t) for t in ("fire", "outlier", "hot")},
            "long_tail": long_tail(vids, topic),
            "suggestions": [i["text"] for i in sug["items"][:10]] if sug else [],
            "top": [[v["channel_id"], v["id"]] for v in sorted(vids, key=lambda v: -v["views"])[:5]],
        })
    out.sort(key=lambda t: (t["videos"] < THIN, -(t["gap_score"] or 0), t["topic"]))   # thin topics last: one video is noise
    return out
