#!/usr/bin/env python3
"""Fill a database with SYNTHETIC yt-dlp-shaped pulls so the dashboard can be tried before real data exists.

Three made-up channels, weekly pulls over six weeks, a few planted patterns (numbers in titles and
8-15 min videos do better) and one title change. Nothing here is real YouTube data.

    python scripts/make_demo_data.py demo/demo.db
    python -m spinwheel --db demo/demo.db dashboard -o demo/dashboard.html
"""

import json
import math
import random
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spinwheel import comments as comments_mod  # noqa: E402
from spinwheel import db                      # noqa: E402
from spinwheel.ingest import ingest_text      # noqa: E402

END = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
PULLS = [END - timedelta(days=7 * k) for k in range(5, -1, -1)]

TOPICS = ["budget travel", "sourdough", "home server", "mechanical keyboards", "cold plunge",
          "ADHD planner", "thrift flips", "pasta", "van life", "note taking", "espresso", "minimalism"]
HOOKS = [
    "{n} {t} mistakes I made so you don't have to", "How I {t} on a tiny budget",
    "Why nobody talks about {t}", "I tried {t} for 30 days", "{t}: the honest beginner guide",
    "What {t} actually costs", "Stop doing {t} like this", "{n} {t} tips that changed everything",
    "Is {t} worth it?", "My {t} setup tour",
]


EXCLUSIVE = ["fermentation", "knife sharpening", "balcony garden"]   # only the small channel covers these


def make_channel(rng, cid, name, n_videos, subs0, scale, extra_topics=()):
    vids = []
    for i in range(n_videos):
        pub = END - timedelta(days=rng.randint(3, 330), hours=rng.randint(0, 23))
        topic, hook = rng.choice(TOPICS + list(extra_topics)), rng.choice(HOOKS)
        title = hook.format(n=rng.choice([3, 5, 7, 9, 10]), t=topic)
        dur = rng.choice([20, 40, 55, 360, 600, 720, 840, 1200, 2400])
        q = rng.lognormvariate(0, 0.55)
        if dur <= 60:
            q *= 2.5          # planted: Shorts get a different view scale than long-form
        if any(ch.isdigit() for ch in title):
            q *= 1.6
        if 480 <= dur < 900:
            q *= 1.5
        if pub.weekday() in (1, 3):
            q *= 1.25
        if "30 days" in title or "setup tour" in title:
            q *= 0.7
        if topic in EXCLUSIVE:
            q *= 2.2          # planted: these topics do well, but only on the small channel
        vids.append(dict(id=f"{cid[-3:]}{i:07d}x"[:11].ljust(11, "x"), pub=pub, title=title, dur=dur,
                         q=q, topic=topic))
    return dict(cid=cid, name=name, vids=vids, subs0=subs0, scale=scale)


def views_at(v, ch, when):
    age = max((when - v["pub"]).total_seconds() / 86400, 0)
    return int(ch["scale"] * v["q"] * (age / (age + 18)) ** 0.8 * (1 + 0.15 * math.log1p(age)))


def entry(v, ch, when):
    vw = views_at(v, ch, when)
    title = v["title"]
    # one planted A/B title test: the title changes after the 3rd pull
    if v is ch["vids"][0] and when >= PULLS[3]:
        title = "I changed my mind about " + v["topic"]
    e = {"id": v["id"], "title": title, "description": f"Demo video about {v['topic']}",
         "timestamp": int(v["pub"].timestamp()), "duration": v["dur"], "view_count": vw,
         "like_count": int(vw * 0.04), "comment_count": int(vw * 0.004),
         "channel_id": ch["cid"], "tags": [v["topic"]],
         "thumbnail": f"https://i.ytimg.com/vi/{v['id']}/maxresdefault.jpg"}
    # Each demo channel exposes a different amount of format evidence, like different pull styles would:
    #   Your Channel: width/height (full yt-dlp)   Competitor A: /shorts/ URLs (Shorts tab)   Small: duration only
    short = v["dur"] <= 60
    if ch["cid"].startswith("UCdemoMine"):
        e["width"], e["height"] = (1080, 1920) if short else (1920, 1080)
    elif ch["cid"].startswith("UCdemoRival"):
        e["webpage_url"] = f"https://www.youtube.com/{'shorts/' if short else 'watch?v='}{v['id']}"
    return e


REQUESTS = ["Can you make a video on {other} next?", "Please do a part 2 of this one", "Would love to see you try {other}",
            "What about {other}? Would watch that", "You should do a full video on {other}", "Next video: {other} please!"]
QUESTIONS = ["Does this work for {topic} if you're a total beginner?", "How much did the {topic} setup cost in total?",
             "Is {topic} still worth it in 2026 or is it overhyped?", "What would you change about {topic} if you started over?"]
NOISE = ["Great video!", "First!", "Subscribed", "Underrated channel", "lol same", "this helped a lot, thanks", "Algorithm brought me here"]


def fake_comments(rng, v, views):
    """SYNTHETIC top-level comments in yt-dlp's shape (parent 'root'), most liked first."""
    n, out = rng.randint(14, 30), []
    for i in range(n):
        r = rng.random()
        other = rng.choice(TOPICS)
        text = (rng.choice(REQUESTS).format(other=other) if r < 0.18 else
                rng.choice(QUESTIONS).format(topic=v["topic"]) if r < 0.38 else rng.choice(NOISE))
        likes = int(views * 0.0006 * rng.lognormvariate(0, 1.1)) + (rng.randint(0, 3) if r >= 0.38 else rng.randint(0, 20))
        out.append({"id": f"Ug{v['id']}{i:03d}", "parent": "root", "text": text, "like_count": likes,
                    "author": "@demo-viewer", "author_is_uploader": False, "is_pinned": False,
                    "timestamp": int((END - timedelta(days=rng.randint(1, 60))).timestamp())})
    # replies, as fetched with a few per thread: the channel answers about half of the questions
    for c in list(out):
        if c["text"] in [q.format(topic=v["topic"]) for q in QUESTIONS] and rng.random() < 0.5:
            out.append({"id": f"{c['id']}.r1", "parent": c["id"], "text": "Good question! I cover that in the description.",
                        "like_count": rng.randint(0, 20), "author": "@demo-channel", "author_is_uploader": True,
                        "is_pinned": False, "timestamp": int(END.timestamp())})
        elif rng.random() < 0.3:
            out.append({"id": f"{c['id']}.r2", "parent": c["id"], "text": "same here", "like_count": rng.randint(0, 5),
                        "author": "@demo-viewer", "author_is_uploader": False, "is_pinned": False, "timestamp": int(END.timestamp())})
    out.append({"id": f"Ug{v['id']}own", "parent": "root", "text": "Thanks for watching! Can you tell me what to cover next?",
                "like_count": 40, "author": "@demo-channel", "author_is_uploader": True, "is_pinned": True,
                "timestamp": int(END.timestamp())})       # the channel's own comment: must be ignored
    return sorted(out, key=lambda c: (c["parent"] != "root", -c["like_count"]))


def main(path):
    rng = random.Random(7)
    chans = [make_channel(rng, "UCdemoMine0001", "Demo: Your Channel", 42, 46000, 1400),
             make_channel(rng, "UCdemoRival002", "Demo: Competitor A", 30, 390000, 9000),
             # small audience, big views: should be flagged as a channel outlier
             make_channel(rng, "UCdemoSmall003", "Demo: Small Breakout", 30, 6200, 1500, EXCLUSIVE)]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    for k, when in enumerate(PULLS):
        for ch in chans:
            subs = int(ch["subs0"] * (1 + 0.012 * k))
            payload = {"_type": "playlist", "id": ch["cid"], "channel_id": ch["cid"], "channel": ch["name"],
                       "channel_follower_count": subs,
                       "entries": [entry(v, ch, when) for v in ch["vids"] if v["pub"] <= when]}
            ingest_text(conn, json.dumps(payload), pulled_at=when,
                        command=f"yt-dlp -J https://www.youtube.com/channel/{ch['cid']}/videos (synthetic)")
    # Comments, fetched the way `spinwheel comments` does: one yt-dlp-style video JSON per video over 10K views.
    rc, fetched = random.Random(11), 0
    for ch in chans:
        for v in ch["vids"]:
            e = entry(v, ch, END)
            if v["pub"] <= END and v["dur"] > 60 and e["view_count"] > 10_000 and rc.random() < 0.6:   # long-form only
                e["comments"] = fake_comments(rc, v, e["view_count"])
                ingest_text(conn, json.dumps(e), pulled_at=END + timedelta(hours=1),
                            command=shlex.join(comments_mod.command_for(v["id"], 100)) + " (synthetic)")
                fetched += 1
    # Topic discovery, as `spinwheel search` would store it: each topic's videos from every channel, in rank order.
    for topic in TOPICS + EXCLUSIVE:
        hits = sorted(((v, ch) for ch in chans for v in ch["vids"] if v["topic"] == topic and v["pub"] <= END),
                      key=lambda vc: -views_at(vc[0], vc[1], END))
        if hits:
            ingest_text(conn, json.dumps({"_type": "playlist", "id": topic, "entries": [entry(v, ch, END) for v, ch in hits]}),
                        pulled_at=END + timedelta(hours=2), command=f"yt-dlp -J ytsearch20:{topic} (synthetic)", search_query=topic)
    print(f"wrote {len(PULLS)} synthetic pulls x {len(chans)} channels + comments for {fetched} videos to {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo/demo.db")
