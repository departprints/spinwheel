import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from spinwheel import analysis, db, discover
from spinwheel.cli import main
from spinwheel.ingest import ingest_text


def flat(ids, channel="UCaaaaaaaaaaa", views=500):
    return json.dumps({"_type": "playlist", "id": "q", "entries": [
        {"_type": "url", "id": i, "title": f"Title {i}", "view_count": views, "duration": 600, "channel": "Chan", "channel_id": channel}
        for i in ids]})


def video(conn, ids, within, when, **kw):
    ingest_text(conn, flat(ids, **kw), pulled_at=when, search_query="topic", within=within)


def v(conn, vid):
    d = analysis.build_dashboard_data(conn)["variants"]["all"]["detail"]
    return next(x for a in d.values() for x in a["videos"] if x["id"] == vid)


class Windows(unittest.TestCase):
    def test_the_search_url_carries_youtubes_upload_date_filter(self):
        cmd = discover.search_command("ai voice documentary", 15, within="today")
        self.assertIn("sp=EgQIAhAB", cmd[-1])
        self.assertIn("search_query=ai+voice+documentary", cmd[-1])
        self.assertEqual(cmd[cmd.index("--playlist-end") + 1], "15")
        self.assertTrue(discover.search_command("x", 5)[-1].startswith("ytsearch5:"))      # no filter: the plain search
        with self.assertRaises(ValueError):
            discover.search_command("x", 5, within="decade")

    def test_a_video_seen_under_today_is_at_most_a_day_old_without_any_date(self):
        conn = db.connect(":memory:")
        video(conn, ["aaaaaaaaaaa"], "today", "2026-10-11T12:00:00Z")
        x = v(conn, "aaaaaaaaaaa")
        self.assertIsNone(x["published"])
        self.assertAlmostEqual(x["age_max_days"], 1.0)
        self.assertEqual(x["window"], "today")

    def test_a_real_publish_date_overrides_the_window(self):
        conn = db.connect(":memory:")
        video(conn, ["aaaaaaaaaaa"], "week", "2026-10-11T12:00:00Z")
        ingest_text(conn, json.dumps({"id": "aaaaaaaaaaa", "title": "t", "view_count": 9, "channel_id": "UCaaaaaaaaaaa",
                                      "timestamp": 1760000000}), pulled_at="2026-10-11T12:00:00Z")          # 2025-10-09
        x = v(conn, "aaaaaaaaaaa")
        self.assertIsNone(x["window"])
        self.assertGreater(x["age_max_days"], 300)

    def test_sightings_combine_latest_for_how_old_and_narrowest_filter_wins(self):
        conn = db.connect(":memory:")
        video(conn, ["aaaaaaaaaaa"], "week", "2026-10-09T00:00:00Z")
        video(conn, ["aaaaaaaaaaa"], "today", "2026-10-10T00:00:00Z")
        video(conn, ["aaaaaaaaaaa"], "today", "2026-10-11T00:00:00Z")           # still "today" a day later: published after 10-10
        w = analysis._windows(conn)["aaaaaaaaaaa"]
        self.assertEqual((w["within"], w["after"], w["before"]), ("today", "2026-10-10T00:00:00Z", "2026-10-09T00:00:00Z"))

    def test_no_filter_means_no_window(self):
        conn = db.connect(":memory:")
        ingest_text(conn, flat(["aaaaaaaaaaa"]), search_query="topic")
        self.assertEqual(analysis._windows(conn), {})
        self.assertIsNone(v(conn, "aaaaaaaaaaa")["age_max_days"])

    def test_searches_table_gains_the_within_column_on_old_databases(self):
        old = re.sub(r",\s*within\s+TEXT[^\n]*", "", db.SCHEMA)
        with tempfile.TemporaryDirectory() as d:
            raw = sqlite3.connect(Path(d) / "o.db")
            raw.executescript(old)
            self.assertNotIn("within", [r[1] for r in raw.execute("PRAGMA table_info(searches)")])
            raw.close()
            conn = db.connect(Path(d) / "o.db")
            self.assertIn("within", [r[1] for r in conn.execute("PRAGMA table_info(searches)")])
            conn.close()


class Labels(unittest.TestCase):
    def test_latest_label_wins_and_history_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "l.db")
            conn = db.connect(path)
            ingest_text(conn, flat(["aaaaaaaaaaa"]))
            conn.close()
            self.assertEqual(main(["--db", path, "label", "unsure", "UCaaaaaaaaaaa", "--note", "one thumbnail only"]), 0)
            self.assertEqual(main(["--db", path, "label", "faceless", "UCaaaaaaaaaaa", "--source", "thumbnail-review", "--note", "no people in 3 thumbnails"]), 0)
            conn = db.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM channel_labels").fetchone()[0], 2)
            ch = analysis.build_dashboard_data(conn)["variants"]["all"]["channels"][0]
            self.assertEqual((ch["label"]["label"], ch["label"]["source"]), ("faceless", "thumbnail-review"))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM channel_labels")

    def test_unlabelled_channels_have_no_label(self):
        conn = db.connect(":memory:")
        ingest_text(conn, flat(["aaaaaaaaaaa"]))
        self.assertIsNone(analysis.build_dashboard_data(conn)["variants"]["all"]["channels"][0]["label"])


if __name__ == "__main__":
    unittest.main()


class UnknownChannelSortsLast(unittest.TestCase):
    def test_collaboration_videos_without_a_channel_never_lead_the_list(self):
        conn = db.connect(":memory:")
        ingest_text(conn, json.dumps({"_type": "playlist", "id": "q", "entries": [
            {"id": f"collab{i:06d}", "title": "t", "view_count": 5, "channel": "A and B"} for i in range(5)] +
            [{"id": "solo0000001", "title": "t", "view_count": 5, "channel_id": "UCsolo", "channel": "Solo"}]}))
        chans = analysis.build_dashboard_data(conn)["variants"]["all"]["channels"]
        self.assertEqual(chans[0]["id"], "UCsolo")             # even though the unknown bucket has more videos
        self.assertEqual(chans[-1]["id"], analysis.UNKNOWN_CHANNEL)
