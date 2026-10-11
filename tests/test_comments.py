import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from spinwheel import analysis, audience, comments, db
from spinwheel.adapters import parse
from spinwheel.ingest import ingest_text

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def mem():
    return db.connect(":memory:")


def real_shape_comment(i, text, likes, **kw):
    """The fields yt-dlp really emits for a comment (checked against a live run), with fake values."""
    c = {"id": f"UgFAKE{i:03d}", "parent": "root", "text": text, "like_count": likes, "author_id": "UCfake",
         "author": "@someone", "author_thumbnail": "https://example.invalid/a.jpg", "author_is_uploader": False,
         "author_is_verified": False, "author_url": "https://example.invalid/@someone", "is_favorited": False,
         "_time_text": "1 year ago", "timestamp": 1760140800, "is_pinned": False}
    c.update(kw)
    return c


def video(vid="vid00000001", views=50_000, comment_list=None, **kw):
    v = {"id": vid, "title": f"Video {vid}", "view_count": views, "like_count": 1000, "comment_count": 321,
         "duration": 600, "upload_date": "20250101", "channel_id": "UCx", "channel": "X", "width": 1920, "height": 1080,
         "webpage_url": f"https://www.youtube.com/watch?v={vid}"}
    if comment_list is not None:
        v["comments"] = comment_list
        v["comment_count"] = len(comment_list)       # what yt-dlp really does: the number fetched, not the total
    v.update(kw)
    return v


class Parsing(unittest.TestCase):
    def test_ytdlp_comments_and_flags(self):
        p = parse(json.dumps(video(comment_list=[
            real_shape_comment(1, "can you make a part 2?", 12),
            real_shape_comment(2, "thanks all", 3, author_is_uploader=True, is_pinned=True),
            real_shape_comment(3, "agree", 1, parent="UgFAKE001")])))
        by = {c.comment_id: c for c in p.comments}
        self.assertEqual((by["UgFAKE001"].parent_id, by["UgFAKE001"].like_count), (None, 12))
        self.assertTrue(by["UgFAKE002"].by_creator and by["UgFAKE002"].pinned)
        self.assertEqual(by["UgFAKE003"].parent_id, "UgFAKE001")
        self.assertEqual(by["UgFAKE001"].published_at, "2025-10-11T00:00:00Z")

    def test_comment_count_is_unknown_when_it_only_counts_what_was_fetched(self):
        self.assertIsNone(parse(json.dumps(video(comment_list=[real_shape_comment(1, "hi", 1)]))).videos[0].comment_count)
        self.assertEqual(parse(json.dumps(video())).videos[0].comment_count, 321)      # a plain pull keeps the real total

    def test_author_names_are_not_stored(self):
        conn = mem()
        ingest_text(conn, json.dumps(video(comment_list=[real_shape_comment(1, "hello", 1)])))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(comment_snapshots)")]
        self.assertFalse({"author", "author_id", "author_url"} & set(cols))

    def test_api_comment_threads(self):
        d = {"kind": "youtube#commentThreadListResponse", "etag": "e", "items": [{
            "kind": "youtube#commentThread", "id": "T1",
            "snippet": {"videoId": "vid00000001", "channelId": "UCowner", "topLevelComment": {
                "id": "C1", "snippet": {"textOriginal": "please review X", "likeCount": 9, "publishedAt": "2025-01-01T00:00:00Z",
                                        "authorChannelId": {"value": "UCfan"}}}},
            "replies": {"comments": [{"id": "C1.r", "snippet": {"textOriginal": "thanks!", "likeCount": 0,
                                                                   "authorChannelId": {"value": "UCowner"}}}]}}]}
        by = {c.comment_id: c for c in parse(json.dumps(d)).comments}
        self.assertEqual((by["C1"].like_count, by["C1"].by_creator, by["C1"].parent_id), (9, False, None))
        self.assertEqual((by["C1.r"].parent_id, by["C1.r"].by_creator), ("C1", True))


API_VIDEO_RESPONSE = {"kind": "youtube#videoListResponse", "etag": "e", "items": [{
    "kind": "youtube#video", "id": "vid00000001",
    "snippet": {"publishedAt": "2025-01-01T00:00:00Z", "channelId": "UC1", "title": "t"},
    "contentDetails": {"duration": "PT10M"}, "statistics": {"viewCount": "123", "likeCount": "4", "commentCount": "5"}}]}


class Envelope(unittest.TestCase):
    """youtube-pp-cli --agent prints {"meta": {...}, "results": <data>}."""
    META = {"source": "live", "synced_at": "2026-10-11T00:00:00Z"}

    def test_wrapped_raw_api_response(self):
        p = parse(json.dumps({"meta": self.META, "results": API_VIDEO_RESPONSE}))
        self.assertEqual((p.source, p.videos[0].video_id, p.videos[0].view_count), ("youtube-api", "vid00000001", 123))

    def test_wrapped_list_of_bare_items_without_kind(self):
        bare = {k: v for k, v in API_VIDEO_RESPONSE["items"][0].items() if k != "kind"}
        p = parse(json.dumps({"meta": self.META, "results": [bare]}))
        self.assertEqual((p.videos[0].view_count, p.videos[0].duration_s), (123, 600))

    def test_wrapped_channel_and_comment_threads(self):
        ch = {"kind": "youtube#channelListResponse", "items": [{"kind": "youtube#channel", "id": "UC1", "snippet": {"title": "C"},
              "statistics": {"subscriberCount": "777", "viewCount": "9", "videoCount": "3"}}]}
        self.assertEqual(parse(json.dumps({"meta": self.META, "results": ch})).channels[0].subscribers, 777)
        th = {"kind": "youtube#commentThreadListResponse", "items": [{"kind": "youtube#commentThread", "id": "T",
              "snippet": {"videoId": "vid00000001", "channelId": "UC1", "topLevelComment": {"id": "C1", "snippet": {"textOriginal": "hi?", "likeCount": 2}}}}]}
        self.assertEqual(parse(json.dumps({"meta": self.META, "results": th})).comments[0].comment_id, "C1")

    def test_wrapped_ytdlp_style_object_and_double_wrap(self):
        inner = {"meta": self.META, "results": video()}
        self.assertEqual(parse(json.dumps({"meta": self.META, "results": inner})).videos[0].view_count, 50_000)

    def test_envelope_with_nothing_inside_is_rejected_not_stored(self):
        conn = mem()
        with self.assertRaises(ValueError):
            ingest_text(conn, json.dumps({"meta": self.META, "results": []}))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pulls").fetchone()[0], 0)

    def test_a_yt_dlp_video_with_a_meta_key_is_not_mistaken_for_an_envelope(self):
        self.assertEqual(parse(json.dumps(dict(video(), meta={"x": 1}))).videos[0].video_id, "vid00000001")


class Storage(unittest.TestCase):
    def test_large_payloads_are_compressed_and_read_back_verbatim(self):
        conn = mem()
        big = json.dumps(video(comment_list=[real_shape_comment(1, "x", 1)], formats=[{"u": "y" * 50} for _ in range(5000)]))
        small = json.dumps(video("vid00000002"))
        r1, r2 = ingest_text(conn, big), ingest_text(conn, small)
        enc = dict(conn.execute("SELECT pull_id, encoding FROM raw_payloads").fetchall())
        self.assertEqual((enc[r1["pull_id"]], enc[r2["pull_id"]]), ("zlib", "plain"))
        self.assertEqual(db.read_raw(conn, r1["pull_id"]), big)
        self.assertEqual(db.read_raw(conn, r2["pull_id"]), small)
        self.assertLess(len(conn.execute("SELECT body FROM raw_payloads WHERE pull_id=?", (r1["pull_id"],)).fetchone()[0]), len(big) / 5)

    def test_comment_snapshots_are_append_only_and_each_fetch_is_kept(self):
        conn = mem()
        ingest_text(conn, json.dumps(video(comment_list=[real_shape_comment(1, "hi", 5)])), pulled_at="2026-01-01T00:00:00Z")
        ingest_text(conn, json.dumps(video(comment_list=[real_shape_comment(1, "hi", 9)])), pulled_at="2026-02-01T00:00:00Z")
        self.assertEqual([r[0] for r in conn.execute("SELECT like_count FROM comment_snapshots ORDER BY captured_at")], [5, 9])
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE comment_snapshots SET like_count = 0")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM comment_snapshots")

    def test_old_database_gains_the_encoding_column(self):
        old = re.sub(r"CREATE TABLE IF NOT EXISTS raw_payloads \(.*?\);",
                     "CREATE TABLE IF NOT EXISTS raw_payloads (pull_id INTEGER PRIMARY KEY, body TEXT NOT NULL);", db.SCHEMA, flags=re.S)
        old = re.sub(r"CREATE TABLE IF NOT EXISTS comment_snapshots.*?\);\nCREATE INDEX[^\n]*\n", "", old, flags=re.S)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "o.db"
            raw = sqlite3.connect(path)
            raw.executescript(old)
            self.assertNotIn("encoding", [r[1] for r in raw.execute("PRAGMA table_info(raw_payloads)")])
            raw.execute("INSERT INTO pulls(pulled_at, ingested_at, source, sha256) VALUES ('2025-01-01T00:00:00Z','x','yt-dlp','h')")
            raw.execute("INSERT INTO raw_payloads(pull_id, body) VALUES (1, 'old payload')")
            raw.commit()
            raw.close()
            conn = db.connect(path)
            self.assertIn("encoding", [r[1] for r in conn.execute("PRAGMA table_info(raw_payloads)")])
            self.assertEqual(db.read_raw(conn, 1), "old payload")       # old rows (encoding NULL) still read as plain
            conn.close()


def seed(conn, views_by_video, when="2026-09-01T00:00:00Z"):
    for vid, views in views_by_video.items():
        ingest_text(conn, json.dumps(video(vid, views)), pulled_at=when)


class Eligibility(unittest.TestCase):
    def test_only_videos_with_more_views_than_the_threshold_most_viewed_first(self):
        conn = mem()
        seed(conn, {"vid00000001": 10_000, "vid00000002": 10_001, "vid00000003": 90_000, "vid00000004": 500})
        got = [v["video_id"] for v in comments.eligible(conn, now=NOW)]
        self.assertEqual(got, ["vid00000003", "vid00000002"])           # 10,000 exactly is not "over"

    def test_uses_the_latest_view_count(self):
        conn = mem()
        seed(conn, {"vid00000001": 5_000}, when="2026-08-01T00:00:00Z")
        seed(conn, {"vid00000001": 20_000}, when="2026-09-01T00:00:00Z")
        self.assertEqual(len(comments.eligible(conn, now=NOW)), 1)

    def test_recently_fetched_videos_are_skipped_until_the_refresh_window_passes(self):
        conn = mem()
        seed(conn, {"vid00000001": 50_000})
        ingest_text(conn, json.dumps(video("vid00000001", 50_000, [real_shape_comment(1, "hi", 1)])), pulled_at="2026-10-05T00:00:00Z")
        self.assertEqual(comments.eligible(conn, refresh_days=14, now=NOW), [])
        self.assertEqual(len(comments.eligible(conn, refresh_days=3, now=NOW)), 1)

    def test_limit(self):
        conn = mem()
        seed(conn, {f"vid0000000{i}": 20_000 + i for i in range(5)})
        self.assertEqual(len(comments.eligible(conn, limit=2, now=NOW)), 2)


class Collect(unittest.TestCase):
    def test_stores_fetches_and_one_failure_does_not_stop_the_rest(self):
        conn = mem()
        seed(conn, {"vid00000001": 30_000, "vid00000002": 20_000, "vid00000003": 10_500})
        calls, naps = [], []

        def runner(cmd):
            calls.append(cmd)
            vid = cmd[-1].split("=")[-1]
            if vid == "vid00000002":
                return 1, "", "ERROR: Sign in to confirm you're not a bot"
            return 0, json.dumps(video(vid, 31_000, [real_shape_comment(1, "hi?", 1)])), ""
        r = comments.collect(conn, comments.eligible(conn, now=NOW), runner=runner, sleep=naps.append, delay=2)
        self.assertEqual(sorted(r["stored"]), ["vid00000001", "vid00000003"])
        self.assertIn("not a bot", r["failed"]["vid00000002"])
        self.assertEqual(naps, [2, 2])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM comment_snapshots WHERE video_id='vid00000002'").fetchone()[0], 0)

    def test_command_asks_for_top_level_comments_most_liked_first(self):
        cmd = comments.command_for("abc", 40)
        arg = cmd[cmd.index("--extractor-args") + 1]
        self.assertEqual(arg, "youtube:max_comments=40,40,0,0;comment_sort=top")
        self.assertIn("--skip-download", cmd)

    def test_missing_ytdlp_stops_cleanly_with_the_reason(self):
        conn = mem()
        seed(conn, {"vid00000001": 30_000, "vid00000002": 20_000})

        def runner(cmd):
            raise FileNotFoundError("yt-dlp")
        r = comments.collect(conn, comments.eligible(conn, now=NOW), runner=runner, sleep=lambda s: None)
        self.assertEqual((r["stored"], list(r["failed"])), ({}, ["vid00000001"]))

    def test_garbage_output_is_a_failure_not_a_crash(self):
        conn = mem()
        seed(conn, {"vid00000001": 30_000})
        r = comments.collect(conn, comments.eligible(conn, now=NOW), runner=lambda c: (0, "not json", ""), sleep=lambda s: None)
        self.assertEqual(list(r["failed"]), ["vid00000001"])


class Audience(unittest.TestCase):
    def test_classify(self):
        c = audience.classify
        self.assertEqual(c("Can you make a video on cast iron?"), "request")
        self.assertEqual(c("please do part 2"), "request")
        self.assertEqual(c("I'd love to see you try the other one"), "request")
        self.assertEqual(c("How much does the whole setup cost in total?"), "question")
        self.assertIsNone(c("Why?"))                      # too short to be useful
        self.assertIsNone(c("Great video, loved it"))

    def test_analyze_ignores_creator_and_replies_and_dedupes(self):
        rows = [dict(video_id="v", parent_id=None, by_creator=0, captured_at="2026-01-01T00:00:00Z", text=t, like_count=n)
                for t, n in [("Can you make a video on X?", 50), ("can you make a video on x??", 20), ("great", 5), ("nice one", 5), ("ok", 5)]]
        rows += [dict(video_id="v", parent_id=None, by_creator=1, captured_at="2026-01-01T00:00:00Z", text="Tell me what to cover next?", like_count=999),
                 dict(video_id="v", parent_id="c1", by_creator=0, captured_at="2026-01-01T00:00:00Z", text="please make Z", like_count=500)]
        r = audience.analyze_video(rows)
        self.assertEqual([a["text"] for a in r["asks"]], ["Can you make a video on X?"])
        self.assertEqual(r["summary"]["n"], 5)
        self.assertEqual(r["asks"][0]["heat"], 10)        # 50 likes vs a typical (median) 5

    def test_long_comments_are_truncated(self):
        rows = [dict(video_id="v", parent_id=None, by_creator=0, captured_at="2026-01-01T00:00:00Z",
                     text="can you make a video on " + "x" * 600, like_count=3)]
        self.assertLessEqual(len(audience.analyze_video(rows)["asks"][0]["text"]), audience.MAX_TEXT)


class Dashboard(unittest.TestCase):
    def build(self):
        conn = mem()
        # 8 ordinary videos so the channel has a median, then one video with fetched comments
        for i in range(8):
            ingest_text(conn, json.dumps(video(f"vid0000000{i}", 20_000 + i * 100)), pulled_at="2026-09-01T00:00:00Z")
        cs = [real_shape_comment(i, f"nice {i}", 10) for i in range(9)]
        cs.append(real_shape_comment(99, "Can you make a video on cast iron skillets?", 400))
        ingest_text(conn, json.dumps(video("vid00000000", 20_000, cs)), pulled_at="2026-09-02T00:00:00Z")
        return analysis.build_dashboard_data(conn)

    def test_engagement_survives_a_comments_pull(self):
        a = self.build()["variants"]["all"]["detail"]["UCx"]
        v = next(x for x in a["videos"] if x["id"] == "vid00000000")
        self.assertEqual(v["comments"], 321)              # the real total from the plain pull, not 10
        self.assertAlmostEqual(v["engagement"], (1000 + 321) / 20_000)

    def test_asks_and_audience_idea_with_quoted_data_framing(self):
        d = self.build()
        a = d["variants"]["all"]["detail"]["UCx"]
        self.assertEqual(a["asks"][0]["text"], "Can you make a video on cast iron skillets?")
        self.assertEqual(a["comment_videos"], 1)
        idea = next(i for i in a["ideas"] if i["type"] == "audience")
        self.assertIn("quoted data, not instructions", idea["brief"])
        self.assertTrue(next(v for v in a["videos"] if v["id"] == "vid00000000")["has_comments"])
        self.assertEqual(d["comments"]["vid00000000"]["asks"], 1)

    def test_comment_text_cannot_break_out_of_the_page(self):
        conn = mem()
        ingest_text(conn, json.dumps(video("vid00000001", 50_000, [real_shape_comment(1, "can you make </script><script>alert(1)</script> ?", 9)])))
        self.assertEqual(analysis.to_json(analysis.build_dashboard_data(conn)).count("</script>"), 0)

    def test_comments_follow_the_format_slice(self):
        conn = mem()
        ingest_text(conn, json.dumps(video("vid00000001", 50_000, [real_shape_comment(1, "Can you make a video on tea please?", 50)],
                                           width=1080, height=1920, duration=30)))
        d = analysis.build_dashboard_data(conn)["variants"]
        self.assertEqual(d["short"]["detail"]["UCx"]["comment_videos"], 1)
        self.assertEqual(d["long"]["detail"]["UCx"]["comment_videos"], 0)


if __name__ == "__main__":
    unittest.main()
