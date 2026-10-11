"""Turn the JSON a YouTube CLI prints into normalized records.

Supported shapes (auto-detected, mixable in one file / stream; any of them may be wrapped in
youtube-pp-cli's {"meta": ..., "results": ...} envelope):
  * yt-dlp:   `yt-dlp -J URL`, `--dump-json URL` (one JSON object per line), channel or
              playlist dumps with an `entries` list, flat or full.
  * YouTube Data API v3: any `videos.list`, `channels.list`, `search.list` or
              `playlistItems.list` response (`{"items": [...]}`), or a bare item.
Anything else is ignored and counted, never guessed at.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator


@dataclass
class VideoRec:
    video_id: str
    channel_id: str | None = None
    published_at: str | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    duration_s: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None
    page_url: str | None = None


@dataclass
class CommentRec:
    video_id: str
    comment_id: str
    text: str
    parent_id: str | None = None
    like_count: int | None = None
    published_at: str | None = None
    by_creator: bool = False
    pinned: bool = False


@dataclass
class ChannelRec:
    channel_id: str
    title: str | None = None
    subscribers: int | None = None
    view_count: int | None = None
    video_count: int | None = None


@dataclass
class Parsed:
    source: str
    videos: list[VideoRec]
    channels: list[ChannelRec]
    skipped: int
    comments: list[CommentRec] = field(default_factory=list)


def _int(x: Any) -> int | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _iso_from_epoch(ts: Any) -> str | None:
    n = _int(ts)
    if n is None:
        return None
    return datetime.fromtimestamp(n, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_iso_duration(s: str | None) -> int | None:
    m = _DUR.match(s or "")
    if not m:
        return None
    d, h, mi, sec = (int(g) if g else 0 for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


# --------------------------------------------------------------------- yt-dlp

def _ytdlp_published(o: dict) -> str | None:
    iso = _iso_from_epoch(o.get("timestamp") or o.get("release_timestamp"))
    if iso:
        return iso
    d = o.get("upload_date")
    if isinstance(d, str) and re.fullmatch(r"\d{8}", d):
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return None


def _page_url(o: dict) -> str | None:
    """A /shorts/ URL if any of the URL fields has one (that is how a Short is identified), else the page URL."""
    urls = [u for u in (o.get("webpage_url"), o.get("original_url"), o.get("url")) if isinstance(u, str)]
    return next((u for u in urls if "/shorts/" in u), urls[0] if urls else None)


def _ytdlp(o: dict, parent: dict | None, out: "_Acc") -> None:
    entries = o.get("entries")
    if isinstance(entries, list):
        ctx = {
            "channel_id": o.get("channel_id") or o.get("uploader_id") or (parent or {}).get("channel_id"),
        }
        # A channel page dump carries the channel's own stats at the top level.
        cid = o.get("channel_id") or (o.get("id") if str(o.get("id", "")).startswith("UC") else None)
        if cid and o.get("channel_follower_count") is not None:
            out.channel(ChannelRec(cid, o.get("channel") or o.get("title"),
                                   subscribers=_int(o.get("channel_follower_count"))))
        for e in entries:
            if isinstance(e, dict):
                _ytdlp(e, ctx, out)
            else:
                out.skipped += 1
        return

    vid = o.get("id")
    if not isinstance(vid, str) or not (o.get("title") or o.get("view_count") is not None):
        out.skipped += 1
        return
    cid = o.get("channel_id") or (parent or {}).get("channel_id")
    comments = o.get("comments")
    if isinstance(comments, list):
        for c in comments:
            if isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("text"), str):
                out.comment(CommentRec(
                    video_id=vid, comment_id=c["id"], text=c["text"],
                    parent_id=None if c.get("parent") in (None, "root") else str(c["parent"]),
                    like_count=_int(c.get("like_count")), published_at=_iso_from_epoch(c.get("timestamp")),
                    by_creator=bool(c.get("author_is_uploader")), pinned=bool(c.get("is_pinned"))))
    out.video(VideoRec(
        video_id=vid,
        channel_id=cid,
        published_at=_ytdlp_published(o),
        title=o.get("title"),
        description=o.get("description"),
        tags=[t for t in (o.get("tags") or []) if isinstance(t, str)],
        duration_s=_int(o.get("duration")),
        view_count=_int(o.get("view_count")),
        like_count=_int(o.get("like_count")),
        # With --write-comments yt-dlp overwrites comment_count with the number it fetched, not the
        # video's real total, so a count that came with comments is unknown, not 12.
        comment_count=None if isinstance(comments, list) else _int(o.get("comment_count")),
        thumbnail_url=o.get("thumbnail"),
        width=_int(o.get("width")), height=_int(o.get("height")),
        page_url=_page_url(o),
    ))
    if cid and o.get("channel_follower_count") is not None:
        out.channel(ChannelRec(cid, o.get("channel") or o.get("uploader"),
                               subscribers=_int(o.get("channel_follower_count"))))


# ------------------------------------------------------------------ Data API v3

def _best_thumb(thumbs: Any) -> str | None:
    if not isinstance(thumbs, dict):
        return None
    for k in ("maxres", "standard", "high", "medium", "default"):
        if isinstance(thumbs.get(k), dict) and thumbs[k].get("url"):
            return thumbs[k]["url"]
    return None


def _api_comment(c: dict, video_id: str, parent: str | None, owner: str | None) -> CommentRec | None:
    sn = c.get("snippet") or {}
    text = sn.get("textOriginal") or sn.get("textDisplay")
    if not (isinstance(c.get("id"), str) and isinstance(text, str)):
        return None
    author = (sn.get("authorChannelId") or {}).get("value")
    return CommentRec(video_id=video_id, comment_id=c["id"], text=text, parent_id=parent,
                      like_count=_int(sn.get("likeCount")), published_at=sn.get("publishedAt"),
                      by_creator=bool(owner and author == owner))


def _api_item(it: dict, out: "_Acc") -> None:
    snip = it.get("snippet") or {}
    stats = it.get("statistics") or {}
    kind = it.get("kind", "")
    raw_id = it.get("id")

    if kind == "youtube#commentThread":            # commentThreads.list
        vid, owner = snip.get("videoId"), snip.get("channelId")
        top = snip.get("topLevelComment") or {}
        if not vid or not top:
            out.skipped += 1
            return
        rec = _api_comment(top, vid, None, owner)
        if rec:
            out.comment(rec)
        for r in ((it.get("replies") or {}).get("comments") or []):
            rr = _api_comment(r, vid, top.get("id"), owner)
            if rr:
                out.comment(rr)
        return

    is_channel = kind == "youtube#channel" or (
        not kind and "subscriberCount" in stats and isinstance(raw_id, str))
    if is_channel:
        subs = None if stats.get("hiddenSubscriberCount") else _int(stats.get("subscriberCount"))
        out.channel(ChannelRec(raw_id, snip.get("title"), subs,
                               _int(stats.get("viewCount")), _int(stats.get("videoCount"))))
        return

    vid = None
    if isinstance(raw_id, dict):                          # search.list
        vid = raw_id.get("videoId")
    elif kind == "youtube#playlistItem":                  # playlistItems.list
        vid = (snip.get("resourceId") or {}).get("videoId")
    elif isinstance(raw_id, str):                         # videos.list
        vid = raw_id
    if not vid:
        out.skipped += 1
        return

    cid = snip.get("videoOwnerChannelId") or snip.get("channelId")
    out.video(VideoRec(
        video_id=vid,
        channel_id=cid,
        published_at=snip.get("publishedAt"),
        title=snip.get("title"),
        description=snip.get("description"),
        tags=[t for t in (snip.get("tags") or []) if isinstance(t, str)],
        duration_s=parse_iso_duration((it.get("contentDetails") or {}).get("duration")),
        view_count=_int(stats.get("viewCount")),
        like_count=_int(stats.get("likeCount")),
        comment_count=_int(stats.get("commentCount")),
        thumbnail_url=_best_thumb(snip.get("thumbnails")),
    ))


# ------------------------------------------------------------------------ glue

class _Acc:
    def __init__(self) -> None:
        self._videos: dict[str, VideoRec] = {}
        self._channels: dict[str, ChannelRec] = {}
        self._comments: dict[str, CommentRec] = {}
        self.skipped = 0
        self.sources: set[str] = set()

    def video(self, v: VideoRec) -> None:
        self._videos[v.video_id] = v          # last one in a single pull wins

    def comment(self, c: CommentRec) -> None:
        self._comments[c.comment_id] = c

    def channel(self, c: ChannelRec) -> None:
        prev = self._channels.get(c.channel_id)
        if prev:  # merge: keep what we already knew when the newer record lacks it
            for f in ("title", "subscribers", "view_count", "video_count"):
                if getattr(c, f) is None:
                    setattr(c, f, getattr(prev, f))
        self._channels[c.channel_id] = c


def _json_objects(text: str) -> Iterator[Any]:
    text = text.strip()
    if not text:
        return
    try:
        yield json.loads(text)
        return
    except json.JSONDecodeError:
        pass
    for n, line in enumerate(text.splitlines(), 1):    # yt-dlp --dump-json: one object per line
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"input is neither one JSON document nor JSON lines (line {n}: {e.msg})") from None


def _is_envelope(o: dict) -> bool:
    """youtube-pp-cli --agent wraps its data: {"meta": {"source": "live"|"local", ...}, "results": <data>}."""
    return isinstance(o.get("meta"), dict) and "results" in o and "items" not in o and "id" not in o


def _dispatch(o: Any, acc: "_Acc", depth: int = 0) -> None:
    if isinstance(o, list):
        for x in o:
            _dispatch(x, acc, depth)
    elif not isinstance(o, dict):
        acc.skipped += 1
    elif _is_envelope(o) and depth < 3:
        _dispatch(o["results"], acc, depth + 1)
    elif isinstance(o.get("items"), list) and (str(o.get("kind", "")).startswith("youtube#") or "etag" in o):
        acc.sources.add("youtube-api")
        for it in o["items"]:
            if isinstance(it, dict):
                _api_item(it, acc)
            else:
                acc.skipped += 1
    elif str(o.get("kind", "")).startswith("youtube#") or (
            ("snippet" in o or "statistics" in o) and "id" in o and "entries" not in o):
        # a typed item, or a bare Data API item (as in an envelope's `results` list) with no `kind`
        acc.sources.add("youtube-api")
        _api_item(o, acc)
    else:
        acc.sources.add("yt-dlp")
        _ytdlp(o, None, acc)


def parse(text: str) -> Parsed:
    acc = _Acc()
    for obj in _json_objects(text):
        _dispatch(obj, acc)
    source = "mixed" if len(acc.sources) > 1 else (next(iter(acc.sources), "unknown"))
    return Parsed(source, list(acc._videos.values()), list(acc._channels.values()), acc.skipped,
                  list(acc._comments.values()))
