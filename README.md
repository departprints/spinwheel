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

# 3. Optional: what people actually search for (YouTube autocomplete)
python -m spinwheel suggest --from-data 10       # uses your top topics and untested gaps as seeds
python -m spinwheel suggest "van life" --expand  # or your own keywords; --expand tries "van life a" .. "van life z"

# 4. Optional: read the comments on your biggest videos (needs yt-dlp)
python -m spinwheel comments --dry-run           # which videos (over 10,000 views) would be fetched
python -m spinwheel comments                     # fetch top comments for them, most-viewed first

python -m spinwheel status                       # what's stored
```

**One-click refresh.** `python -m spinwheel serve --open` serves the dashboard at `http://127.0.0.1:8765/` with a **Refresh data** button. It repeats your earlier channel pulls, re-runs your tracked topic searches, then updates subscriber counts and missing publish dates, and reloads the page. Each step is reported, and one failing step doesn't stop the others. It can take a few minutes. Comments, labels and autocomplete suggestions stay separate commands. The server listens on 127.0.0.1 only. The button also appears on a saved `dashboard.html` opened from disk: it talks to the helper on `127.0.0.1:8765`, so keep `python -m spinwheel serve` running in a terminal, click the button, and the helper rewrites `dashboard.html` (`-o FILE` to change the path) before the page reloads. If the helper isn't running, the button says so.

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
| Content ideas | Ranked ideas with their evidence and a "Copy brief" button each (see below) |
| Keyword research | Which topics work on this channel, which work on other channels but are untested here, and what people type into YouTube search |
| Audience asks | Content requests, unanswered questions and most-liked comments from your long-form videos over 10K views |
| Title changes | Your own A/B history, which only exists because old pulls are kept |
| Channels | You vs competitors: size, growth, typical views, views per subscriber, cadence |
| Breakouts across channels | Videos that got far more views than their channel's subscriber count would suggest |

**Outlier flags** (shown on every video, with the channel's subscriber count as the reference):

- **Video outlier**: the video has 2× or more its own channel's median views. It stands out on its own channel.
- **Channel outlier**: the channel's typical video earns 2× or more the views per subscriber of the *other tracked channels*. The whole channel is beating its size, so the format or niche may be the story, not one video. It needs 3+ tracked channels; with fewer, the dashboard shows "n/a" instead of guessing.

A video can carry both flags. A video outlier on a *small* channel is usually the strongest case to study. Views ÷ subs uses the channel's current subscriber count, so it flatters old videos on channels that have grown since.

## Finding videos by topic

You don't have to name channels. `spinwheel search "cast iron" "sourdough starter"` runs a YouTube keyword search for each topic (20 results by default) and keeps a **video-to-topic mapping**: which video appeared for which keyword, at what rank, and when. Every channel that shows up is tracked from then on. `search --all` re-runs every topic you've searched before. No API key is used.

A search is two steps because YouTube sometimes refuses individual videos ("Sign in to confirm you're not a bot"):

1. a flat search, which reliably gives rank, title, views and channel for every result (stored with the topic);
2. an enrichment pass for publish date, likes and the channel's subscriber count. Whatever succeeds is kept and the shortfall is reported ("4 could not be fetched").

Subscriber counts are also refreshed per channel (`spinwheel channels`, run automatically after a search), which is fast and doesn't depend on the per-video step. `spinwheel enrich` retries videos that still have no publish date. Pass `--yt-dlp-args "--cookies-from-browser firefox"` to any of these when YouTube asks for a login. `--fast` skips enrichment.

Honest limits: in testing from a cloud server, per-video enrichment was blocked for most videos, while the flat search and channel refresh always worked. Without a publish date a video still counts for tiers, views and topics, but not for age-based analysis (it shows "no date" instead of a score). Run from your own machine, or pass cookies, for fuller results. The YouTube Data API is not needed; if you use it instead, its `search.list` output ingests, but the query mapping is only recorded by `search`.

## Patterns across tracked channels

Under **Content ideas**, three cards look across *all* tracked channels in the current format slice:

- **Title structures in high-tier videos.** Every title gets one structure, first match wins: *comparison* (vs / versus / compared to), *question* (a `?`, or why / what / is / can / how do...), *numbered list* ("7 tips", "top 10"), *how-to* ("how to", tutorial, guide, step-by-step), *personal story* (I / my / we), else *other*. The table compares each structure's share among HOT / OUTLIER / FIRE videos with its share of all videos; over 1.0× means it turns up in high-tier videos more than chance. "How to..." is a how-to and "How I..." a story; only "how do / can / much..." counts as a question.
- **Top titles by raw views**, with their tiers, so you can see how often the most-viewed video is simply the biggest channel's.
- **Small channels punching above their weight.** FIRE and OUTLIER videos from channels under 100,000 subscribers (the source page doesn't define "small"; the limit is `SMALL_CHANNEL_SUBS` in `spinwheel/patterns.py`), most views per subscriber first, with what they share: title structures, median length, how many are Shorts, and repeated phrases.

The **title pattern** chart gained *numbered list* and *comparison* patterns. Its bars use the median-based lift (one hit can swing an average); the source page defines lift on averages, so the average-based lift is in each bar's tooltip and in the chart's Table view.

## Keyword research

The **Keyword research** section starts with **Tracked topics**: every keyword you've searched with `spinwheel search`, with the videos that search surfaced. It follows the formulas of the [YouTube Outlier Finder](https://chadesheppard.com/videos/youtube-outlier-finder); the page leaves several terms undefined, so these are the choices made here:

| Column / score | Definition |
|---|---|
| Videos (volume) | Distinct videos tracked for the topic. **Not search volume**, which no free data source gives. |
| Gap score | average views ÷ number of channels covering the topic (as on the page). Topics with fewer than 3 videos sort last and are marked *thin*. |
| Heat | **My definition.** TRENDING: 3+ videos in the last 30 days, at least 25% of the topic, and their median views/day at least 1.5× that of the older videos (which need 3+ too). ACTIVE: 2+ in the last 30 days. STEADY: otherwise. Blank without 5+ videos that have a publish date. |
| Difficulty (0-100) | 65% log-scaled average subscribers of the channels in the phrase's videos + 35% log-scaled video volume (the number of videos) |
| Opportunity (0-100) | 60% log-scaled average views + 40% views-per-subscriber |

"Log-scaled" is undefined on the page; here it is `100 × log10(1 + x) ÷ log10(1 + cap)`, at most 100, with caps of 10M subscribers, 1M average views and 50 videos. Views-per-subscriber is the mean of views ÷ subscribers, with views equal to subscribers (the FIRE line) scoring 100. All of this is in `spinwheel/keywords.py`, marked CHOICE.

Click a topic to see its **long-tail keywords**: phrases of 2-4 words found in at least 2 titles for that topic, ranked by highest opportunity, then lowest difficulty, alongside the topic's autocomplete suggestions (if fetched) and top videos. Long-tail phrases may start or end on format words ("budget travel tips", "cast iron tutorial"), since those are what people search; the channel-level topic list deliberately doesn't allow that.

**Check a keyword** (the page's pre-script check): type a planned keyword and see every tracked video with it in the title, how many are FIRE / OUTLIER / HOT, and the best examples. No tiered video on an angle means it may be underserved, or that nobody wants it, so compare it with the autocomplete suggestions.

## Comments

`spinwheel comments` runs yt-dlp for **long-form** videos with **more than** `--min-views` views (default 10,000; `--include-shorts` adds Shorts) and stores their top-level comments, most liked first (default 100 each, 25 videos per run, 3 s apart). It also fetches a few replies (default 150 per video, at most 3 per thread, `--replies 0` to skip) to see whether the channel answered a question. Videos fetched within `--refresh-days` (default 14) are skipped, and each fetch is stored as new snapshots, so like counts can be compared over time. `--dry-run` lists what it would fetch without touching the network. A video that fails (rate limit, "confirm you're not a bot") is reported and stores nothing; the rest continue, and the exit code is 1.

The dashboard's **Audience asks** section has three lists, plus the video dialog's top comments and **Audience asked** ideas:

- **Content requests**: comments asking for a video ("can you make a video on...", "part 2 please", "what about...").
- **Unanswered questions**: a question mark and at least 4 words, with no reply from the channel *among the replies fetched*. Only the first few replies per thread and the first replies overall are fetched, so "unanswered" means none seen, not proof; if a fetch had no replies, answered or not is shown as unknown and nothing is claimed.
- **Most-liked comments**: whatever they say.

Your own comments are excluded. "vs typical" is a comment's likes against the median comment on the same video, so a big video doesn't win on volume alone. Comments follow the Shorts / long-form toggle (the Shorts view is empty unless you fetched Shorts).

Things worth knowing:

- yt-dlp reports `comment_count` as the number of comments it *fetched*, not the video's real total. Spinwheel treats that count as unknown, so engagement keeps using the real total from your normal pulls.
- Comments are other people's words. They are shown as plain text only, and in a copied brief they are labelled as quoted data, not instructions.
- Commenter names are not stored in the comment tables. The raw yt-dlp output is still kept verbatim (it includes handles), compressed once it exceeds 100 KB; `db.read_raw()` returns it unchanged. The database is local to you; keep that in mind before sharing it.
- Comments need yt-dlp. The YouTube Data API's `commentThreads.list` output is also understood by `ingest` (without a stored command, replies count as not requested).

## Pages

The dashboard is one file with eight pages, each with its own navigation tab (`#today`, `#videos`, ...):

| Page | For |
|---|---|
| **Today** | What is blowing up now: videos from the last 24 hours ranked by views ÷ subscribers, with a faceless filter |
| **Overview** | One channel: size, typical views, findings, and the charts of what works for it |
| **Channels** | Every channel side by side, breakouts, and small channels punching above their weight |
| **Videos** | The video table (tier and date filters, all-channels switch), the views-by-date chart, momentum |
| **Keywords** | Tracked topics with gap score, heat, long-tail phrases; check-a-keyword; autocomplete |
| **Ideas** | Ranked ideas with evidence, and title patterns across channels |
| **Comments** | Content requests, unanswered questions, most-liked comments |
| **Data** | What is in the database and how complete it is: coverage, topics tracked, labels, recent pulls, how to refresh |

The channel picker appears only on pages about one channel; the format toggle (All / Long-form / Shorts) is on every page except Data. Your channel and format choice is remembered in the browser.

## Blowing up today

`spinwheel search "topic" --within today` uses YouTube's own "uploaded today" filter (`--within week|month|year` also work). Everything such a search returns was published inside the window, so a video is known to be at most a day old **without asking for its publish date**, which is the request YouTube blocks most. That bound is stored with the search (`searches.within`) and drives the **Today** page and the 24-hour date filter. A video known only from the filter counts as "last 24 hours" if it was seen under it within a few hours (6) of your newest pull, because collecting subscriber counts takes time after the search; a video with an exact publish date gets no such grace.

The filter covers regular videos, not Shorts.

## Faceless channels

Whether a channel shows a face cannot be read from the data, so it is a recorded judgment:

```bash
python -m spinwheel label faceless UCxxxx UCyyyy --source thumbnail-review --note "no people in 3 thumbnails"
python -m spinwheel label face UCzzzz
```

Labels are history (the newest wins, older ones are kept). The **Today** page can show only verified faceless channels, only the not-yet-reviewed ones, or hide channels that show a face, and the **Data** page lists every label with its source and date. Unlabelled means not reviewed, not "has a face".

## YouTube Data API (optional)

Scraping needs no key but YouTube refuses many per-video requests from servers and busy IPs, and publish dates and comments are what go missing. The official API has no such check; it has a daily quota (10,000 units by default). With a key in `$YOUTUBE_API_KEY` (set it in your shell or environment settings; never paste it into a chat or commit it):

```bash
python -m spinwheel api search "ai documentary" "stoicism" --within today   # ~102 units per keyword; dates, stats and subscribers included
python -m spinwheel api enrich                                              # publish dates, 1 unit per 50 videos
python -m spinwheel api channels                                            # subscriber counts, 1 unit per 50 channels
python -m spinwheel api comments                                            # top threads with replies, 1 unit per video
```

The key is sent with each request but never stored (stored commands say "key redacted"). Note: this client was written against the documented response shapes and tested with them, but has not been run against the live API.

## Tiers, filters and the all-channels list

Every video gets a performance tier against its channel's subscribers (inclusive comparisons):

| Tier | Rule |
|---|---|
| FIRE | views ≥ 100% of subscribers |
| OUTLIER | views ≥ 30% of subscribers, or ≥ 5× the channel average |
| HOT | views ≥ 10% of subscribers, or ≥ 2× the channel average |

The channel average is the mean views of the channel's videos in the current format slice and only counts with 3+ videos; with unknown subscribers only the average rules apply. Tiers apply at any video age (a day-old video with 30% of subscribers' worth of views is an OUTLIER), unlike the "vs median" score, which waits 14 days. This follows the setup-guide version of the [YouTube Outlier Finder](https://chadesheppard.com/videos/youtube-outlier-finder); its article body states the thresholds with `>` and without the average rules, and the page doesn't reconcile the two.

The **Videos** table filters by tier and by publish date (24 hours, 7, 30 or 90 days, measured from your newest pull, not today; a date with no time of day counts the whole day), and a **This channel / All channels** switch lists every tracked channel's videos together. Morning check: All channels, last 24 hours, sort by Tier.

## Shorts vs long-form

The **Format** toggle (All / Long-form / Shorts) at the top scopes the whole page: tiles, findings, charts, the video table, ideas, topics, gaps, the channel comparison and breakouts. It is in the URL (`#channel=…&format=short`), so you can bookmark a view.

Each format is analysed *on its own*, not filtered after the fact. A Short and a ten-minute video live on different view scales, so "2× the channel median" only means something against the same format: in a channel with mostly Shorts, a mixed median would make every long-form video look like a flop. In the Shorts view, length is bucketed in seconds (under 15 s ... 1-3 min), and ideas and gaps compare Shorts with other Shorts.

**How a Short is detected**, best evidence first (each video records which rule decided it, and the page shows the counts):

1. A `/shorts/` URL: Short.
2. Width and height known: vertical and 3 minutes or less is a Short; anything else is long-form.
3. Neither: 60 seconds or less is a Short. This is an approximation; change it with `dashboard --short-max 180`.
4. Nothing known: long-form.

yt-dlp gives the URL and dimensions (full, non-flat output gives dimensions; the Shorts tab gives `/shorts/` URLs). The YouTube Data API gives neither, so API pulls rely on rule 3. If part of your data was classified by length alone, the page says how many videos.

Databases created before this feature are upgraded in place (three new columns; old rows read empty and fall back to rule 3, nothing is rewritten).

## Keyword research and content ideas

**Topics** are 1-3 word phrases pulled from titles and tags. Generic format words ("tips", "mistakes", "tried", "guide", "review", ...) can't start or end a phrase, so "7 sourdough tips that changed everything" gives *sourdough*, not *tips that changed*. A phrase needs 2+ videos older than 14 days. Lift is the phrase's median views against the channel median. The word list is `FORMAT` in `spinwheel/topics.py`; edit it if your niche's real topics get filtered out, or format words slip through.

**Gaps** are phrases that average 1.3× or more the *own-channel median* on the other tracked channels (in 2+ videos) and appear in none of this channel's titles or tags. Every video is scored against its own channel, so a big channel's hit and a small channel's hit are comparable. Needs at least one other tracked channel.

**Ideas** come in four kinds, all computed from your stored numbers:

| Kind | Rule |
|---|---|
| Follow up | A video with 2× or more its channel's median views |
| Untested topic | The top gaps above |
| Double down | A topic averaging 1.3× or more here that hasn't been covered in 60+ days |
| Riding the wave | A video whose recent views per day are 1.2× or more its lifetime average (needs 2+ pulls) |

Each idea lists the videos behind it, what people search for on that topic (if you've fetched suggestions), and a **Copy brief** button. The brief is plain text: the evidence, the packaging that has worked on the channel, your best-performing titles, and a request for 5 title options, a thumbnail concept and an opening. Paste it into a chat or doc to turn the idea into a draft. The dashboard deliberately doesn't write titles itself: it can't tell a topic from a format reliably enough to fill a template without producing nonsense.

**Search suggestions** are YouTube's own autocomplete, via the unofficial endpoint the search box uses. They show what people look for and in what order; they do **not** give search volume (no free source does). Each fetch is stored, so a later fetch can mark phrases as *new*. The endpoint can rate-limit or change; a failing keyword is reported and nothing is stored for it. Requests are 1 second apart by default, and `--expand` makes 27 requests per keyword, so use it sparingly.

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
spinwheel/topics.py      phrases, topic stats, cross-channel gaps
spinwheel/ideas.py       ranked ideas + copyable briefs
spinwheel/suggest.py     YouTube autocomplete fetch, stored append-only
spinwheel/youtube_api.py optional Data API client (search, videos, channels, comments)
spinwheel/comments.py    picks videos over the view threshold, fetches comments via yt-dlp
spinwheel/audience.py    classifies comments as requests / questions
spinwheel/dashboard.html the page; analysis JSON is embedded at build time
tests/                   python -m unittest discover -s tests
```

## Known limits

- Thumbnail changes aren't detected: YouTube keeps the same thumbnail URL when the image changes. Title changes are.
- Publish-day buckets use UTC dates. yt-dlp gives a date only for some outputs; the Data API gives a full time.
- Shorts detection by length alone is approximate: a 50-second landscape video is counted as a Short, and a 90-second Short as long-form, unless the pull carries a URL or dimensions.
- Topic detection works on words, not meaning: synonyms count as different topics, and a generic phrase can occasionally rank as one.
- Gaps only know what you've pulled. A topic you covered on a channel you haven't pulled still shows as untested.
- If a video drops out of later pulls (deleted, or beyond `--playlist-end`) its last snapshot is still used.
