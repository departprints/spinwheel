# Spinwheel

A local, append-only history of your YouTube pulls, plus an HTML dashboard that turns it into content decisions.

- **Every pull is kept.** Each run adds a timestamped snapshot of every video and channel. Nothing is updated or deleted, and SQLite triggers enforce it. The raw CLI output is stored verbatim too, so a parser fix never loses data.
- **The dashboard is one self-contained HTML file.** No server, no network, no dependencies beyond Python 3.10+ (standard library only).

## Use it

```bash
# 1. Pull and store. Re-run whenever you want a new data point (weekly is plenty).
python -m spinwheel pull -- yt-dlp -J "https://www.youtube.com/@yourchannel/videos"
python -m spinwheel pull -- yt-dlp -J "https://www.youtube.com/@competitor/videos"   # competitors work the same way

# or store output you already have
yt-dlp -J "https://www.youtube.com/@yourchannel/videos" > pull.json
python -m spinwheel ingest pull.json            # `-` reads stdin; --pulled-at overrides the timestamp

# 2. Build the dashboard and open it
python -m spinwheel dashboard -o dashboard.html

python -m spinwheel status                       # what's stored
```

The database is `spinwheel.db` in the current directory (`--db PATH` to change it, placed before the subcommand).

**Accepted input** (auto-detected): `yt-dlp -J` / `--dump-json` output for videos, channels and playlists, and YouTube Data API v3 responses (`videos.list`, `channels.list`, `search.list`, `playlistItems.list`). Anything else is rejected and nothing is stored.

Tips for useful data:
- Pull a channel's `/videos` page (not just single videos) so channel subscribers and the whole catalogue are captured.
- Full (non-flat) yt-dlp output has likes, comments, duration, publish time; `--flat-playlist` is faster but gives less.
- Trends (momentum, subscriber growth, title changes) need at least two pulls, spaced days apart.

## What the dashboard tells you

Per channel (yours or a competitor's), from videos older than 14 days so new uploads aren't compared with year-old ones:

| Section | Decision it supports |
|---|---|
| What the data says | Plain-language findings: biggest outlier to follow up, best length, best publish day, title patterns, topic words, what's accelerating |
| Views by publish date | Which videos broke out, and by how much |
| Length, publish day, title patterns | What to change about format and packaging |
| Moving right now | Which video to promote or follow up while it's moving |
| Title changes | Your own A/B history, which only exists because old pulls are kept |
| Channels | You vs competitors: size, growth, typical views, views per subscriber, cadence |
| Breakouts across channels | Videos that got far more views than their channel's subscriber count would suggest |

**Outlier flags** (shown on every video, with the channel's subscriber count as the reference):

- **Video outlier**: the video has 2× or more its own channel's median views. It stands out on its own channel.
- **Channel outlier**: the channel's typical video earns 2× or more the views per subscriber of the *other tracked channels*. The whole channel is beating its size, so the format or niche may be the story, not one video. It needs 3+ tracked channels; with fewer, the dashboard shows "n/a" instead of guessing.

A video can carry both flags. A video outlier on a *small* channel is usually the strongest case to study. Views ÷ subs uses the channel's current subscriber count, so it flatters old videos on channels that have grown since.

Findings need at least 3 videos on each side of a comparison and 8 mature videos overall, and they are correlations, not proof. Every chart has a **Table** button with the same numbers.

## Try it with fake data

```bash
python scripts/make_demo_data.py demo/demo.db           # synthetic, clearly labelled "Demo:"
python -m spinwheel --db demo/demo.db dashboard -o demo/dashboard.html
```

## Layout

```
spinwheel/db.py          schema + append-only triggers
spinwheel/adapters.py    yt-dlp / Data API JSON -> normalized records
spinwheel/ingest.py      one transaction per pull
spinwheel/analysis.py    all the metrics and findings (tested)
spinwheel/dashboard.html the page; analysis JSON is embedded at build time
tests/                   python -m unittest discover -s tests
```

## Known limits

- Thumbnail changes aren't detected: YouTube keeps the same thumbnail URL when the image changes. Title changes are.
- Publish-day buckets use UTC dates. yt-dlp gives a date only for some outputs; the Data API gives a full time.
- If a video drops out of later pulls (deleted, or beyond `--playlist-end`) its last snapshot is still used.
