import json
import unittest
from datetime import datetime, timezone

from spinwheel import analysis, db, discover
from spinwheel.ingest import ingest_text

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def mem():
    return db.connect(":memory:")


def flat(query, ids, views=1000):
    """What `yt-dlp -J --flat-playlist ytsearchN:query` prints (checked against a live run)."""
    return json.dumps({"_type": "playlist", "id": query, "entries": [
        {"_type": "url", "id": i, "title": f"Title {i}", "view_count": views + n, "duration": 600, "channel": f"Chan {i}",
         "channel_id": f"UC{i}", "channel_follower_count": None, "url": f"https://www.youtube.com/watch?v={i}"} for n, i in enumerate(ids)]})


def full(ids, subs=5000):
    """What the enrichment --print emits: one JSON object per line."""
    return "\n".join(json.dumps({"id": i, "title": f"Title {i}", "timestamp": 1760000000, "duration": 600, "view_count": 2000,
                                 "channel_id": f"UC{i}", "channel": f"Chan {i}", "channel_follower_count": subs}) for i in ids)


class FakeYtDlp:
    def __init__(self, flat_out=None, enrich_ids=(), flat_code=0, enrich_code=1):
        self.calls, self.flat_out, self.enrich_ids, self.flat_code, self.enrich_code = [], flat_out, list(enrich_ids), flat_code, enrich_code

    def __call__(self, cmd):
        self.calls.append(cmd)
        if "--flat-playlist" in cmd:
            return self.flat_code, self.flat_out or "", "ERROR: boom" if self.flat_code else ""
        return self.enrich_code, full(self.enrich_ids), "ERROR: Sign in to confirm you're not a bot"


class Search(unittest.TestCase):
    IDS = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]

    def test_flat_results_are_mapped_to_the_topic_in_rank_order(self):
        conn = mem()
        r = discover.search(conn, ["Cast Iron"], fast=True, runner=FakeYtDlp(flat(" cast iron", self.IDS)), sleep=lambda s: None)
        self.assertEqual(r["done"], {"Cast Iron": {"found": 3, "enriched": 0}})
        rows = conn.execute("SELECT query, video_id, rank FROM searches ORDER BY rank").fetchall()
        self.assertEqual([tuple(x) for x in rows], [("cast iron", i, n) for n, i in enumerate(self.IDS)])
        self.assertEqual(discover.tracked_topics(conn), ["cast iron"])

    def test_partial_enrichment_is_kept_and_reported(self):
        conn = mem()
        fake = FakeYtDlp(flat("q", self.IDS), enrich_ids=self.IDS[:1])          # 1 of 3 passes the bot check
        r = discover.search(conn, ["q"], runner=fake, sleep=lambda s: None)
        self.assertEqual(r["done"]["q"], {"found": 3, "enriched": 1})
        dated = conn.execute("SELECT DISTINCT video_id FROM video_snapshots WHERE published_at IS NOT NULL").fetchall()
        self.assertEqual([x[0] for x in dated], self.IDS[:1])
        self.assertEqual(conn.execute("SELECT COUNT(DISTINCT video_id) FROM searches").fetchone()[0], 3)   # all still mapped

    def test_enrichment_fully_blocked_still_keeps_the_search_results(self):
        conn = mem()
        r = discover.search(conn, ["q"], runner=FakeYtDlp(flat("q", self.IDS)), sleep=lambda s: None)
        self.assertEqual(r["done"]["q"], {"found": 3, "enriched": 0})
        self.assertEqual(r["failed"], {})

    def test_a_failed_search_stores_nothing_and_the_next_topic_still_runs(self):
        conn = mem()
        calls = []

        def runner(cmd):
            calls.append(cmd)
            if "ytsearch20:bad" in cmd:
                return 1, "", "ERROR: HTTP 429"
            return 0, flat("good", self.IDS), ""
        r = discover.search(conn, ["bad", "good"], fast=True, runner=runner, sleep=lambda s: None)
        self.assertIn("429", r["failed"]["bad"])
        self.assertEqual(list(r["done"]), ["good"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM searches WHERE query='bad'").fetchone()[0], 0)

    def test_fast_mode_makes_one_request_per_topic_and_delays_between_topics(self):
        conn, naps = mem(), []
        fake = FakeYtDlp(flat("q", self.IDS))
        discover.search(conn, ["one", "two", "one"], fast=True, delay=2, runner=fake, sleep=naps.append)
        self.assertEqual(len(fake.calls), 2)          # duplicates collapse
        self.assertEqual(naps, [2])

    def test_missing_ytdlp_stops_with_the_reason(self):
        def runner(cmd):
            raise FileNotFoundError("yt-dlp")
        r = discover.search(mem(), ["q"], runner=runner, sleep=lambda s: None)
        self.assertEqual((r["done"], list(r["failed"])), ({}, ["q"]))

    def test_commands(self):
        self.assertEqual(discover.search_command("a b", 5, extra=["--cookies-from-browser", "firefox"]),
                         ["yt-dlp", "-J", "--flat-playlist", "--cookies-from-browser", "firefox", "ytsearch5:a b"])
        cmd = discover.enrich_command(["abc"])
        self.assertIn("--print", cmd)
        self.assertTrue(cmd[-1].endswith("watch?v=abc"))
        self.assertEqual(discover.channel_command("UCx")[-3:], ["--playlist-items", "0", "https://www.youtube.com/channel/UCx/videos"][-3:])

    def test_channel_name_is_captured_even_without_a_subscriber_count(self):
        conn = mem()
        discover.search(conn, ["q"], fast=True, runner=FakeYtDlp(flat("q", self.IDS)), sleep=lambda s: None)
        row = conn.execute("SELECT title, subscribers FROM channel_snapshots WHERE channel_id='UCaaaaaaaaaaa'").fetchone()
        self.assertEqual((row[0], row[1]), ("Chan aaaaaaaaaaa", None))


class Channels(unittest.TestCase):
    def build(self):
        conn = mem()
        ingest_text(conn, flat("q", ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]), pulled_at="2026-10-01T00:00:00Z")
        # b has a recent count, c an old one
        ingest_text(conn, json.dumps({"_type": "playlist", "id": "UCbbbbbbbbbbb", "channel_id": "UCbbbbbbbbbbb", "channel": "B",
                                      "channel_follower_count": 10, "entries": []}), pulled_at="2026-10-09T00:00:00Z")
        ingest_text(conn, json.dumps({"_type": "playlist", "id": "UCccccccccccc", "channel_id": "UCccccccccccc", "channel": "C",
                                      "channel_follower_count": 10, "entries": []}), pulled_at="2026-08-01T00:00:00Z")
        return conn

    def test_only_channels_missing_or_stale_are_refreshed_never_fetched_first(self):
        self.assertEqual(discover.stale_channels(self.build(), refresh_days=7, now=NOW), ["UCaaaaaaaaaaa", "UCccccccccccc"])

    def test_limit_and_non_channel_ids_are_ignored(self):
        conn = self.build()
        ingest_text(conn, json.dumps({"id": "vid00000001", "title": "t", "view_count": 5, "channel_id": "not-a-channel"}))
        self.assertEqual(discover.stale_channels(conn, limit=1, now=NOW), ["UCaaaaaaaaaaa"])

    def test_refresh_stores_counts_and_reports_hidden_ones(self):
        conn = self.build()

        def runner(cmd):
            cid = cmd[-1].split("/")[-2]
            if cid == "UCccccccccccc":       # subscribers hidden: the page has no count
                return 0, json.dumps({"_type": "playlist", "id": cid, "channel_id": cid, "entries": []}), ""
            return 0, json.dumps({"_type": "playlist", "id": cid, "channel_id": cid, "channel": "A", "channel_follower_count": 321, "entries": []}), ""
        r = discover.refresh_channels(conn, ["UCaaaaaaaaaaa", "UCccccccccccc"], runner=runner, sleep=lambda s: None)
        self.assertEqual(r["stored"], ["UCaaaaaaaaaaa"])
        self.assertIn("hidden", r["failed"]["UCccccccccccc"])
        self.assertEqual(conn.execute("SELECT MAX(subscribers) FROM channel_snapshots WHERE channel_id='UCaaaaaaaaaaa'").fetchone()[0], 321)

    def test_a_search_discovered_channel_shows_up_in_the_dashboard_with_its_subscribers(self):
        conn = self.build()
        d = analysis.build_dashboard_data(conn)["variants"]["all"]["channels"]
        self.assertEqual({c["id"]: c["subscribers"] for c in d}["UCbbbbbbbbbbb"], 10)


class Enrich(unittest.TestCase):
    def test_undated_videos_are_found_most_viewed_first_and_retry_fills_them_in(self):
        conn = mem()
        ingest_text(conn, flat("q", ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]))
        ingest_text(conn, full(["bbbbbbbbbbb"]))                                   # b already has a date
        self.assertEqual(discover.undated_videos(conn), ["ccccccccccc", "aaaaaaaaaaa"])
        r = discover.enrich(conn, ["ccccccccccc", "aaaaaaaaaaa"], runner=lambda c: (1, full(["ccccccccccc"]), "partial"), sleep=lambda s: None)
        self.assertEqual((r["enriched"], r["requested"]), (1, 2))
        self.assertEqual(discover.undated_videos(conn), ["aaaaaaaaaaa"])

    def test_the_dashboard_does_not_call_an_undated_video_too_new(self):
        conn = mem()
        ingest_text(conn, flat("q", ["aaaaaaaaaaa"]))
        v = analysis.build_dashboard_data(conn)["variants"]["all"]["detail"]["UCaaaaaaaaaaa"]["videos"][0]
        self.assertIsNone(v["published"])
        self.assertFalse(v["mature"])


if __name__ == "__main__":
    unittest.main()
