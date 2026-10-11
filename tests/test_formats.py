import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from spinwheel import analysis, db
from spinwheel.adapters import parse
from spinwheel.analysis import classify_format
from spinwheel.ingest import ingest_text

LONG_AGO = "2025-06-01T00:00:00Z"


def snap(**kw):
    base = dict(page_url=None, width=None, height=None, duration_s=None)
    base.update(kw)
    return base


class Classify(unittest.TestCase):
    def test_shorts_url_wins(self):
        self.assertEqual(classify_format([snap(page_url="https://www.youtube.com/shorts/abc", duration_s=900)]), ("short", "url"))

    def test_vertical_up_to_three_minutes_is_a_short(self):
        self.assertEqual(classify_format([snap(width=1080, height=1920, duration_s=150)]), ("short", "shape"))

    def test_vertical_but_long_is_not_a_short(self):
        self.assertEqual(classify_format([snap(width=1080, height=1920, duration_s=900)]), ("long", "shape"))

    def test_landscape_is_long_even_if_brief(self):
        self.assertEqual(classify_format([snap(width=1920, height=1080, duration_s=30)]), ("long", "shape"))

    def test_without_url_or_shape_falls_back_to_length(self):
        self.assertEqual(classify_format([snap(duration_s=45)]), ("short", "duration"))
        self.assertEqual(classify_format([snap(duration_s=61)]), ("long", "duration"))
        self.assertEqual(classify_format([snap(duration_s=120)], short_max=180), ("short", "duration"))

    def test_nothing_known_is_long(self):
        self.assertEqual(classify_format([snap()]), ("long", "none"))

    def test_one_shorts_snapshot_is_enough(self):
        self.assertEqual(classify_format([snap(duration_s=30), snap(page_url="https://youtube.com/shorts/x")])[0], "short")


class Adapters(unittest.TestCase):
    def test_ytdlp_keeps_shape_and_prefers_a_shorts_url(self):
        v = parse(json.dumps({"id": "abc", "title": "t", "view_count": 1, "width": 1080, "height": 1920,
                              "webpage_url": "https://www.youtube.com/watch?v=abc",
                              "original_url": "https://www.youtube.com/shorts/abc"})).videos[0]
        self.assertEqual((v.width, v.height), (1080, 1920))
        self.assertIn("/shorts/", v.page_url)

    def test_api_has_no_shape(self):
        d = {"kind": "youtube#videoListResponse", "etag": "e", "items": [{"kind": "youtube#video", "id": "v1",
             "snippet": {"title": "t"}, "statistics": {"viewCount": "1"}, "contentDetails": {"duration": "PT30S"}}]}
        v = parse(json.dumps(d)).videos[0]
        self.assertEqual((v.width, v.height, v.page_url, v.duration_s), (None, None, None, 30))


def channel(conn, cid, longs, shorts, subs=1000, when=LONG_AGO):
    entries = []
    for i, v in enumerate(longs):
        entries.append({"id": f"{cid}L{i:02d}".ljust(11, "x"), "title": f"Long form {i}", "timestamp": 1690000000 + i * 86400,
                        "duration": 600, "view_count": v, "channel_id": cid, "width": 1920, "height": 1080})
    for i, v in enumerate(shorts):
        entries.append({"id": f"{cid}S{i:02d}".ljust(11, "x"), "title": f"Short form {i}", "timestamp": 1691000000 + i * 86400,
                        "duration": 30, "view_count": v, "channel_id": cid, "width": 1080, "height": 1920})
    ingest_text(conn, json.dumps({"_type": "playlist", "id": cid, "channel_id": cid, "channel": cid,
                                  "channel_follower_count": subs, "entries": entries}), pulled_at=when)


class Variants(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        # long-form ~1K views, Shorts ~100K and the majority: mixing them makes every long video look like a flop
        channel(self.conn, "UCa", [1000, 1100, 900], [100000, 120000, 90000, 110000, 100000, 95000, 900000])
        self.d = analysis.build_dashboard_data(self.conn)
        self.v = self.d["variants"]

    def test_counts_per_slice(self):
        n = {m: self.v[m]["channels"][0]["n_videos"] for m in analysis.FORMATS}
        self.assertEqual(n, {"all": 10, "long": 3, "short": 7})

    def test_each_slice_is_compared_with_its_own_median(self):
        self.assertAlmostEqual(self.v["long"]["channels"][0]["median_views"], 1000)
        self.assertEqual(self.v["short"]["channels"][0]["median_views"], 100000)
        long_scores = [x["score"] for x in self.v["long"]["detail"]["UCa"]["videos"]]
        self.assertTrue(all(0.8 < s < 1.2 for s in long_scores))
        # ...whereas in 'all' the Shorts set the median, so every long-form video sits far beneath it
        mixed = [x["score"] for x in self.v["all"]["detail"]["UCa"]["videos"] if x["format"] == "long"]
        self.assertTrue(all(s < 0.1 for s in mixed))

    def test_videos_only_appear_in_their_own_slice(self):
        self.assertEqual({x["format"] for x in self.v["long"]["detail"]["UCa"]["videos"]}, {"long"})
        self.assertEqual({x["format"] for x in self.v["short"]["detail"]["UCa"]["videos"]}, {"short"})

    def test_shorts_use_shorts_length_buckets(self):
        labels = [r["label"] for r in self.v["short"]["detail"]["UCa"]["durations"]]
        self.assertEqual(labels, ["30–45 s"])
        self.assertIn("8–15 min", [r["label"] for r in self.v["long"]["detail"]["UCa"]["durations"]])

    def test_a_channel_with_no_shorts_is_still_listed_with_zero(self):
        conn = db.connect(":memory:")
        channel(conn, "UCb", [1000] * 6, [])
        s = analysis.build_dashboard_data(conn)["variants"]["short"]
        self.assertEqual(s["channels"][0]["n_videos"], 0)
        self.assertEqual(s["detail"]["UCb"]["videos"], [])

    def test_history_is_sent_once_not_per_slice(self):
        out = json.loads(analysis.to_json(self.d))
        self.assertTrue(out["history"])
        for m in analysis.FORMATS:
            for c in out["variants"][m]["detail"].values():
                self.assertTrue(all("history" not in x for x in c["videos"]))

    def test_basis_is_reported(self):
        self.assertEqual(self.d["format_basis"], {"shape": 10})

    def test_brief_names_the_format_for_a_slice_only(self):
        ideas = lambda m: self.v[m]["detail"]["UCa"]["ideas"]
        self.assertTrue(ideas("short") and ideas("all"))
        self.assertTrue(all("Format: Shorts only" in i["brief"] for i in ideas("short")))
        self.assertTrue(all("Format:" not in i["brief"] for i in ideas("all")))

    def test_duration_threshold_is_adjustable(self):
        conn = db.connect(":memory:")
        ingest_text(conn, json.dumps({"id": "vid00000001", "title": "t", "view_count": 5, "duration": 120,
                                      "channel_id": "UCx", "timestamp": 1690000000}), pulled_at=LONG_AGO)
        n = lambda sm: analysis.build_dashboard_data(conn, short_max=sm)["variants"]["short"]["channels"][0]["n_videos"]
        self.assertEqual((n(60), n(180)), (0, 1))


class Migration(unittest.TestCase):
    def test_database_from_before_shape_columns_is_upgraded_in_place(self):
        old = re.sub(r",\s*width .*?\n\s*height .*?\n\s*page_url [^\n]*\n", "\n", db.SCHEMA, flags=re.S)
        self.assertNotIn("page_url", old)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            raw = sqlite3.connect(path)
            raw.executescript(old)
            raw.execute("INSERT INTO pulls(pulled_at, ingested_at, source, sha256) VALUES ('2025-01-01T00:00:00Z','x','yt-dlp','h')")
            raw.execute("INSERT INTO videos(video_id, first_seen_at) VALUES ('v','2025-01-01T00:00:00Z')")
            raw.execute("INSERT INTO video_snapshots(pull_id, video_id, captured_at, view_count) VALUES (1,'v','2025-01-01T00:00:00Z',42)")
            raw.commit(); raw.close()
            conn = db.connect(path)
            row = conn.execute("SELECT view_count, width, height, page_url FROM video_snapshots").fetchone()
            self.assertEqual(tuple(row), (42, None, None, None))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE video_snapshots SET width = 1")
            conn.close()
            db.connect(path).close()      # reconnecting is a no-op, not an error


if __name__ == "__main__":
    unittest.main()
