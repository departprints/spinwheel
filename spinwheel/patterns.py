"""Title structure and cross-channel patterns: what high-performing titles have in common.

Everything here looks across all tracked channels in the current format slice, not one channel.
"High-tier" means a video with any tier (HOT, OUTLIER or FIRE). CHOICE: a "small" channel has fewer than
SMALL_CHANNEL_SUBS subscribers (the source page doesn't define small).
"""

from __future__ import annotations

import re
from collections import Counter
from statistics import median

from . import topics as T

SMALL_CHANNEL_SUBS = 100_000
TOP_RAW = 15
DEEP_DIVE_SHOWN = 15

_COMPARISON = re.compile(r"\b(vs\.?|versus|compared (?:to|with)|difference between|better than)\b", re.I)
# "How to ..." is a how-to and "How I ..." is a story; only "how do / does / can / much / many ..." asks something.
_QUESTION_START = re.compile(r"^\s*(?:why|what|when|which|who|can|should|is|are|do|does|will|would|could)\b"
                             r"|^\s*how\s+(?:do|does|did|can|should|would|much|many|long|often|far)\b", re.I)
_NUMBERED = re.compile(r"^\s*\d+\b|\b(?:top|best)\s+\d+\b|\b\d+\s+(?:ways|things|tips|reasons|mistakes|steps|ideas|tricks|hacks|secrets|lessons|rules)\b", re.I)
_HOWTO = re.compile(r"\bhow to\b|\bstep[- ]by[- ]step\b|\btutorial\b|\bguide\b", re.I)
_PERSONAL = re.compile(r"\b(?:i|i'm|i've|i'd|my|we|our)\b", re.I)

# First match wins, so a title that is both a question and a comparison counts as a comparison.
STRUCTURES = ["Comparison", "Question", "Numbered list", "How-to", "Personal story", "Other"]


def is_comparison(t: str) -> bool:
    return bool(_COMPARISON.search(t))


def is_numbered(t: str) -> bool:
    return bool(_NUMBERED.search(t))


def is_question(t: str) -> bool:
    return "?" in t or bool(_QUESTION_START.match(t))


def structure_of(title: str) -> str:
    if is_comparison(title):
        return "Comparison"
    if is_question(title):
        return "Question"
    if is_numbered(title):
        return "Numbered list"
    if _HOWTO.search(title):
        return "How-to"
    if _PERSONAL.search(title):
        return "Personal story"
    return "Other"


def structure_clusters(vids: list[dict]) -> list[dict]:
    """How title structures are spread among high-tier videos compared with all videos. Over-representation
    above 1 means the structure turns up in high-tier videos more often than it does overall."""
    counted = [v for v in vids if v["views"] is not None]
    high = [v for v in counted if v["tier"]]
    out = []
    for label in STRUCTURES:
        allv = [v for v in counted if structure_of(v["title"]) == label]
        hv = [v for v in high if structure_of(v["title"]) == label]
        if not allv:
            continue
        share_all = len(allv) / len(counted)
        share_high = len(hv) / len(high) if high else None
        out.append({"label": label, "videos": len(allv), "high": len(hv), "share_all": share_all, "share_high": share_high,
                    "over": (share_high / share_all) if share_high is not None else None,
                    "examples": [[v["channel_id"], v["id"]] for v in sorted(hv, key=lambda v: -v["views"])[:2]]})
    out.sort(key=lambda r: (-r["high"], r["label"]))
    return out


def top_by_views(vids: list[dict], n: int = TOP_RAW) -> list[list[str]]:
    return [[v["channel_id"], v["id"]] for v in sorted((v for v in vids if v["views"] is not None), key=lambda v: -v["views"])[:n]]


def small_channel_deep_dive(vids: list[dict], small_subs: int = SMALL_CHANNEL_SUBS) -> dict:
    """FIRE and OUTLIER videos from channels under `small_subs` subscribers, and what they have in common."""
    hits = [v for v in vids if v["tier"] in ("fire", "outlier") and v["subs"] and v["subs"] < small_subs and v["views"] is not None]
    hits.sort(key=lambda v: -(v["views"] / v["subs"]))
    structures = Counter(structure_of(v["title"]) for v in hits)
    durations = [v["duration_s"] for v in hits if v["duration_s"] is not None]
    phrase_counts = Counter(p for v in hits for p in T.phrases_for(v["title"]))
    return {
        "small_below": small_subs, "videos": len(hits), "channels": len({v["channel_id"] for v in hits}),
        "top": [[v["channel_id"], v["id"]] for v in hits[:DEEP_DIVE_SHOWN]],
        "structures": [{"label": k, "n": n} for k, n in structures.most_common()],
        "median_length_s": median(durations) if durations else None,
        "shorts": sum(1 for v in hits if v["format"] == "short"),
        "phrases": [{"phrase": p, "n": n} for p, n in phrase_counts.most_common(40) if n >= 2][:6],
    }


def build(vids: list[dict]) -> dict:
    return {"structures": structure_clusters(vids), "top_views": top_by_views(vids),
            "deep_dive": small_channel_deep_dive(vids), "small_below": SMALL_CHANNEL_SUBS}
