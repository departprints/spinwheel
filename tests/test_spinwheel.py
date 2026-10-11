import json
import sqlite3
import unittest
from pathlib import Path

from spinwheel import analysis, dashboard, db
from spinwheel.adapters import parse, parse_iso_duration
from spinwheel.ingest import ingest_text

YTDLP_VIDEO = {"id": "abc12345678", "title": "Hello 5 things", "timestamp": 1700000000, "duration": 600,
               "view_count": 1000, "like_count": 50, "comment_count": 5, "channel_id": "UC1",
               "channel": "Chan", "channel_follower_count": 900, "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg"}

API_VIDEOS = {"kind": "youtube#videoListResponse", "etag": "e", "items": [{
    "kind": "youtube#video", "id": "vid00000001",
    "snippet": {"publishedAt": "2024-01-02T03:04:05Z", "channelId": "UC2", "title": "T", "tags": ["a"],
                "thumbnails": {"default": {"url": "https://i/d.jpg"}, "high": {"url": "https://i/h.jpg"}}},
    "contentDetails": {"duration": "PT1H2M3S"},
    "statistics": {"viewCount": "123", "likeCount": "4", "commentCount": "5"}}]}

API_CHANNELS = {"kind": "youtube#channelListResponse", "items": [{
    "kind": "youtube#channel", "id": "UC2", "snippet": {"title": "Chan2"},
    "statistics": {"subscriberCount": "777", "viewCount": "9999", "videoCount": "12"}}]}


def mem():
    return db.connect(":memory:")


class Adapters(unittest.TestCase):
    def test_ytdlp_single_video_gives_video_and_channel(self):
        p = parse(json.dumps(YTDLP_VIDEO))
        self.assertEqual(p.source, "yt-dlp")
        self.assertEqual(p.videos[0].view_count, 1000)
        self.assertEqual(p.videos[0].published_at, "2023-11-14T22:13:20Z")
        self.assertEqual(p.channels[0].subscribers, 900)

    def test_ytdlp_json_lines(self):
        text = "\n".join(json.dumps(dict(YTDLP_VIDEO, id=f"v{i}")) for i in range(3))
        self.assertEqual(len(parse(text).videos), 3)

    def test_ytdlp_playlist_entries_inherit_channel(self):
        flat = {"_type": "playlist", "channel_id": "UC9", "entries": [{"id": "a", "title": "x", "view_count": 5}]}
        self.assertEqual(parse(json.dumps(flat)).videos[0].channel_id, "UC9")

    def test_api_video_and_channel(self):
        v = parse(json.dumps(API_VIDEOS)).videos[0]
        self.assertEqual((v.view_count, v.duration_s, v.thumbnail_url), (123, 3723, "https://i/h.jpg"))
        c = parse(json.dumps(API_CHANNELS)).channels[0]
        self.assertEqual((c.subscribers, c.video_count), (777, 12))

    def test_api_hidden_subscribers_is_none_not_zero(self):
        d = json.loads(json.dumps(API_CHANNELS))
        d["items"][0]["statistics"] = {"hiddenSubscriberCount": True, "viewCount": "1"}
        self.assertIsNone(parse(json.dumps(d)).channels[0].subscribers)

    def test_duration(self):
        self.assertEqual(parse_iso_duration("PT45S"), 45)
        self.assertIsNone(parse_iso_duration("garbage"))

    def test_garbage_is_rejected_not_stored(self):
        conn = mem()
        with self.assertRaises(ValueError):
            ingest_text(conn, "not json at all")
        with self.assertRaises(ValueError):
            ingest_text(conn, json.dumps({"hello": "world"}))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pulls").fetchone()[0], 0)


class AppendOnly(unittest.TestCase):
    def test_second_pull_adds_rows_and_keeps_the_first(self):
        conn = mem()
        ingest_text(conn, json.dumps(YTDLP_VIDEO), pulled_at="2026-01-01T00:00:00Z")
        ingest_text(conn, json.dumps(dict(YTDLP_VIDEO, view_count=2000, title="Renamed")), pulled_at="2026-01-08T00:00:00Z")
        rows = conn.execute("SELECT view_count, title FROM video_snapshots ORDER BY captured_at").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1000, "Hello 5 things"), (2000, "Renamed")])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0], 2)

    def test_database_refuses_update_and_delete(self):
        conn = mem()
        ingest_text(conn, json.dumps(YTDLP_VIDEO))
        for table in ("video_snapshots", "channel_snapshots", "raw_payloads", "pulls", "videos", "channels"):
            with self.assertRaises(sqlite3.IntegrityError, msg=table):
                conn.execute(f"DELETE FROM {table}")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE video_snapshots SET view_count = 0")

    def test_raw_payload_is_kept_verbatim(self):
        conn = mem()
        text = json.dumps(YTDLP_VIDEO, indent=2)
        ingest_text(conn, text)
        self.assertEqual(conn.execute("SELECT body FROM raw_payloads").fetchone()[0], text)


class Analysis(unittest.TestCase):
    def test_recent_rate_uses_snapshot_history(self):
        t = analysis.parse_ts
        r = analysis.recent_rate([(t("2026-01-01"), 100), (t("2026-01-08"), 800)])
        self.assertAlmostEqual(r["per_day"], 100)
        self.assertIsNone(analysis.recent_rate([(t("2026-01-01"), 100)]))

    def test_title_change_detected_across_pulls(self):
        conn = mem()
        ingest_text(conn, json.dumps(YTDLP_VIDEO), pulled_at="2026-01-01T00:00:00Z")
        ingest_text(conn, json.dumps(dict(YTDLP_VIDEO, title="B")), pulled_at="2026-01-08T00:00:00Z")
        d = analysis.build_dashboard_data(conn)
        ch = d["detail"]["UC1"]["title_changes"]
        self.assertEqual((ch[0]["from"], ch[0]["to"]), ("Hello 5 things", "B"))

    def test_young_videos_are_not_scored(self):
        conn = mem()
        for i in range(5):
            ingest_text(conn, json.dumps(dict(YTDLP_VIDEO, id=f"old{i}", timestamp=1700000000 + i, view_count=100 * (i + 1))),
                        pulled_at="2024-06-01T00:00:00Z")
        ingest_text(conn, json.dumps(dict(YTDLP_VIDEO, id="new", timestamp=1717200000, view_count=10**6)),
                    pulled_at="2024-06-02T00:00:00Z")
        vids = {v["id"]: v for v in analysis.build_dashboard_data(conn)["detail"]["UC1"]["videos"]}
        self.assertIsNone(vids["new"]["score"])
        self.assertIsNotNone(vids["old2"]["score"])

    def test_untrusted_text_cannot_close_script_tag(self):
        conn = mem()
        ingest_text(conn, json.dumps(dict(YTDLP_VIDEO, title="</script><script>alert(1)</script>")))
        html = dashboard.render(conn)
        self.assertEqual(html.count("</script>"), 1)

    def test_empty_database_renders(self):
        self.assertIn("DATA = ", dashboard.render(mem()))


class DemoData(unittest.TestCase):
    def test_demo_pipeline_produces_findings(self):
        import importlib.util, tempfile
        spec = importlib.util.spec_from_file_location("demo", Path(__file__).parent.parent / "scripts" / "make_demo_data.py")
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as d:
            mod.main(f"{d}/x.db")
            data = analysis.build_dashboard_data(db.connect(f"{d}/x.db"))
        self.assertEqual(len(data["channels"]), 2)
        self.assertTrue(data["detail"]["UCdemoMine0001"]["findings"])


if __name__ == "__main__":
    unittest.main()
