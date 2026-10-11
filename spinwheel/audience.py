"""What the audience is asking for, from stored comments.

Only top-level comments from the latest fetch of each video are used, and the channel's own comments
are ignored. A comment is a *request* ("can you make a video on ...", "part 2 please") or a *question*
(has a question mark and at least 4 words). Ranking is by likes, and `heat` says how far a comment
stands out from the typical comment under the same video (likes ÷ that video's median likes), so a
comment on a huge video doesn't outrank a better one on a small video just by volume.

The words are the commenters', so everything here is untrusted text: it is only ever displayed with
textContent and quoted as data in briefs.
"""

from __future__ import annotations

import re
from statistics import median

MAX_TEXT = 280
MIN_QUESTION_WORDS = 4

_REQUEST = re.compile(r"""(
    \b(?:please|pls|plz)\b[^.!?\n]{0,60}\b(?:make|do|show|cover|review|explain|try|test|talk|teach|film|upload|compare)\b
  | \b(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:make|do|show|cover|review|explain|try|test|talk|teach|film|compare|put)\b
  | \b(?:make|do)\s+(?:a|an|another|more|one)\s+(?:video|part|episode|tutorial|series|vid)\b
  | \bpart\s*(?:2|two|ii)\b
  | \bnext\s+(?:video|one|time)\b
  | \b(?:i['’]?d|i\s+would|we['’]?d)\s+(?:love|like|want)\s+to\s+(?:see|watch|know|learn|hear)\b
  | \bwhat\s+about\b
  | \b(?:you|u)\s+should\s+(?:make|do|try|cover|review)\b
)""", re.I | re.X)


def classify(text: str) -> str | None:
    if _REQUEST.search(text):
        return "request"
    if "?" in text and len(text.split()) >= MIN_QUESTION_WORDS:
        return "question"
    return None


def _clean(text: str, limit: int = MAX_TEXT) -> str:
    t = " ".join(text.split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()[:60]


def analyze_video(rows: list) -> dict:
    """rows: the latest fetch's comment rows for one video (sqlite rows or dicts)."""
    top = [r for r in rows if r["parent_id"] is None and not r["by_creator"]]
    likes = [r["like_count"] or 0 for r in top]
    typical = max(median(likes), 1) if likes else 1
    asks, seen = [], set()
    for r in sorted(top, key=lambda r: -(r["like_count"] or 0)):
        kind = classify(r["text"])
        k = _key(r["text"])
        if not kind or k in seen:
            continue
        seen.add(k)
        asks.append({"kind": kind, "text": _clean(r["text"]), "likes": r["like_count"] or 0,
                     "heat": (r["like_count"] or 0) / typical, "video_id": r["video_id"]})
    best = sorted(top, key=lambda r: -(r["like_count"] or 0))[:5]
    return {
        "summary": {"at": max((r["captured_at"] for r in rows), default=None)[:10] if rows else None,
                    "n": len(top), "asks": len(asks),
                    "top": [{"text": _clean(r["text"], 400), "likes": r["like_count"] or 0, "kind": classify(r["text"])}
                            for r in best]},
        "asks": asks,
    }


def merge_asks(per_video: list[list[dict]], limit: int = 12) -> list[dict]:
    """Best asks across videos by likes, dropping near-identical wording."""
    flat = sorted((a for asks in per_video for a in asks), key=lambda a: -a["likes"])
    out, seen = [], set()
    for a in flat:
        k = _key(a["text"])
        if k not in seen:
            seen.add(k)
            out.append(a)
        if len(out) == limit:
            break
    return out
