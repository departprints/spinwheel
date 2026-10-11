"""Keyword research from stored data: which phrases work on a channel, and which work elsewhere
but haven't been tried here.

Performance is always relative to the video's *own* channel (its `score`, views ÷ channel median),
so a 50K-view video on a small channel and a 2M-view video on a big one are comparable.
"""

from __future__ import annotations

import re
from collections import defaultdict
from statistics import median

MIN_VIDEOS = 2           # a phrase must appear in this many videos to be ranked
GAP_MIN_SCORE = 1.3      # elsewhere it must beat the channel's typical video by this much
TOP_SHOWN = 12

STOP = set("""a about after all also am an and any are as at be been being but by can could did do does
doing for from get got had has have how i if in into is it its just like make me more most my no not of
off on one or our out over so some than that the their them then there these they this to up us use
using very was we were what when where which who why will with you your yours vs per new
don't doesn't didn't can't won't isn't aren't it's i'm i've i'd you're you've that's there's let's""".split())

# Generic YouTube packaging words. They describe the *format* of a video, not its subject, so a phrase
# can't start or end on one ("tips that changed" is not a topic; "sourdough" in "7 sourdough tips" is).
FORMAT = set("""tip tips trick tricks hack hacks mistake mistakes way ways thing things reason reasons secret secrets
guide guides tutorial tutorials review reviews best top worst beginner beginners complete ultimate explained
explain reaction vlog tour setup challenge honest honestly actually everything nothing changed change changes
tried trying try stop start learn learned learning week weeks day days month months year years worth cost costs
truth real easy simple quick fast free cheap full part episode update story time first ever never always
really anyone everyone nobody someone people thing lot need want know think going work works working look
looking video videos step steps made make making doing done using used talk talks talking""".split())

_WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return [w for w in (x.strip("'") for x in _WORD.findall(text.lower())) if w]


def _content(w: str) -> bool:
    return len(w) >= 3 and w not in STOP and not w.isdigit()


def _edge_ok(w: str) -> bool:
    """A word a phrase may start or end on: a real word that isn't filler or a format word."""
    return _content(w) and w not in FORMAT


def phrases_for(title: str, tags: list[str] | None = None) -> frozenset[str]:
    """1-3 word phrases from a title, plus whole tags. Phrases never start or end on a filler word."""
    ws = words(title)
    out = {w for w in ws if _edge_ok(w)}
    for n in (2, 3):
        for i in range(len(ws) - n + 1):
            g = ws[i:i + n]
            if _edge_ok(g[0]) and _edge_ok(g[-1]):
                out.add(" ".join(g))
    for t in tags or []:
        tw = words(t)
        if 1 <= len(tw) <= 4 and _edge_ok(tw[0]) and _edge_ok(tw[-1]):
            out.add(" ".join(tw))
    return frozenset(out)


def _med(xs):
    return median(xs) if xs else None


def _merge(by_phrase: dict[str, list[dict]], min_n: int) -> list[tuple[str, list[str], list[dict]]]:
    """Phrases that appear in exactly the same videos say the same thing: keep the longest as the
    label and list the rest as aliases."""
    groups: dict[frozenset, list[str]] = defaultdict(list)
    for p, vs in by_phrase.items():
        if len(vs) >= min_n:
            groups[frozenset(v["id"] for v in vs)].append(p)
    out = []
    for _, ps in groups.items():
        ps.sort(key=lambda p: (-len(p.split()), -len(p), p))
        out.append((ps[0], ps[1:5], by_phrase[ps[0]]))
    return out


def channel_topics(mature: list[dict], base: float | None) -> list[dict]:
    """Phrases used by >= MIN_VIDEOS mature videos on one channel, best first."""
    by: dict[str, list[dict]] = defaultdict(list)
    for v in mature:
        for p in v["_phrases"]:
            by[p].append(v)
    rows = []
    for label, also, vs in _merge(by, MIN_VIDEOS):
        m = _med([v["views"] for v in vs])
        top = sorted(vs, key=lambda v: -v["views"])[:3]
        rows.append({"phrase": label, "also": also, "n": len(vs), "median_views": m,
                     "lift": m / base if base else None,
                     "last_used": max((v["published"] or "")[:10] for v in vs) or None,
                     "videos": [v["id"] for v in top]})
    rows.sort(key=lambda r: (-(r["lift"] or 0), -r["n"], r["phrase"]))
    return rows


def find_gaps(own_videos: list[dict], other_channels: dict[str, list[dict]],
              titles: dict[str, str]) -> list[dict]:
    """Phrases that beat the typical video on other channels but appear nowhere in `own_videos`.

    other_channels: channel_id -> its videos. Only videos with a score (mature, channel has 3+ mature) count.
    """
    covered: set[str] = set()
    for v in own_videos:
        covered |= v["_phrases"]

    by: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for cid, vids in other_channels.items():
        for v in vids:
            if v.get("score") is None:
                continue
            for p in v["_phrases"] - covered:
                by[p].append((cid, v))

    rows = []
    for label, also, entries in _merge({p: [v for _, v in e] for p, e in by.items()}, MIN_VIDEOS):
        pairs = by[label]
        scores = [v["score"] for _, v in pairs]
        m = _med(scores)
        if m < GAP_MIN_SCORE:
            continue
        best = sorted(pairs, key=lambda cv: -cv[1]["score"])[:3]
        rows.append({"phrase": label, "also": also, "n": len(pairs),
                     "channels": sorted({titles.get(c, c) for c, _ in pairs}),
                     "median_score": m, "videos": [[c, v["id"]] for c, v in best]})
    rows.sort(key=lambda r: (-r["median_score"], -r["n"], r["phrase"]))
    return rows[:TOP_SHOWN]


def phrase_of_title(v: dict, topics: list[dict]) -> str | None:
    """The best-performing ranked phrase that appears in this video's title, or None."""
    member = {}
    for r in topics:
        for p in [r["phrase"], *r["also"]]:
            member.setdefault(p, r)
    hits = [(member[p], p) for p in v["_phrases"] if p in member]
    if not hits:
        return None
    row, p = max(hits, key=lambda rp: (rp[0]["lift"] or 0, len(rp[1].split()), len(rp[1])))
    return p
