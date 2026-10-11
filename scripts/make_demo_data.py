#!/usr/bin/env python3
"""Fill a database with SYNTHETIC yt-dlp-shaped pulls so the dashboard can be tried before real data exists.

Two made-up channels, weekly pulls over six weeks, a few planted patterns (numbers in titles and
8-15 min videos do better) and one title change. Nothing here is real YouTube data.

    python scripts/make_demo_data.py demo/demo.db
    python -m spinwheel --db demo/demo.db dashboard -o demo/dashboard.html
"""

import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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


def make_channel(rng, cid, name, n_videos, subs0, scale):
    vids = []
    for i in range(n_videos):
        pub = END - timedelta(days=rng.randint(3, 330), hours=rng.randint(0, 23))
        topic, hook = rng.choice(TOPICS), rng.choice(HOOKS)
        title = hook.format(n=rng.choice([3, 5, 7, 9, 10]), t=topic)
        dur = rng.choice([45, 180, 360, 600, 720, 840, 1200, 2400])
        q = rng.lognormvariate(0, 0.55)
        if any(ch.isdigit() for ch in title):
            q *= 1.6
        if 480 <= dur < 900:
            q *= 1.5
        if pub.weekday() in (1, 3):
            q *= 1.25
        if "30 days" in title or "setup tour" in title:
            q *= 0.7
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
    return {"id": v["id"], "title": title, "description": f"Demo video about {v['topic']}",
            "timestamp": int(v["pub"].timestamp()), "duration": v["dur"], "view_count": vw,
            "like_count": int(vw * 0.04), "comment_count": int(vw * 0.004),
            "channel_id": ch["cid"], "tags": [v["topic"]],
            "thumbnail": f"https://i.ytimg.com/vi/{v['id']}/maxresdefault.jpg"}


def main(path):
    rng = random.Random(7)
    chans = [make_channel(rng, "UCdemoMine0001", "Demo: Your Channel", 42, 8200, 30000),
             make_channel(rng, "UCdemoRival002", "Demo: Competitor A", 30, 61000, 160000)]
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
    print(f"wrote {len(PULLS)} synthetic pulls x {len(chans)} channels to {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo/demo.db")
