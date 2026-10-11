import json
import unittest

from spinwheel import analysis, db
from spinwheel.analysis import tier_for
from spinwheel.ingest import ingest_text


class TierRules(unittest.TestCase):
    """FIRE >= 100% of subs; OUTLIER >= 30% of subs or >= 5x channel average; HOT >= 10% of subs or >= 2x average."""

    def test_subscriber_thresholds_are_inclusive(self):
        t = lambda views: tier_for(views, 1000, None)[0]
        self.assertEqual([t(1000), t(999), t(300), t(299), t(100), t(99)], ["fire", "outlier", "outlier", "hot", "hot", None])

    def test_average_alternatives(self):
        self.assertEqual(tier_for(500, 1_000_000, 100.0)[0], "outlier")      # 5x average, tiny share of subs
        self.assertEqual(tier_for(499, 1_000_000, 100.0)[0], "hot")          # just under 5x, but 2x+
        self.assertEqual(tier_for(200, 1_000_000, 100.0)[0], "hot")
        self.assertIsNone(tier_for(199, 1_000_000, 100.0)[0])

    def test_the_higher_tier_wins_whichever_rule_gets_there(self):
        self.assertEqual(tier_for(5000, 1000, 100.0)[0], "fire")             # also 50x the average

    def test_unknown_subscribers_fall_back_to_the_average_only(self):
        self.assertEqual(tier_for(600, None, 100.0)[0], "outlier")
        self.assertIsNone(tier_for(600, None, None)[0])
        self.assertIsNone(tier_for(600, 0, None)[0])                         # zero subs is not a divisor

    def test_no_views_no_tier(self):
        self.assertEqual(tier_for(None, 1000, 10.0), (None, None))

    def test_the_reason_names_the_rule(self):
        self.assertIn("100% of subscribers", tier_for(1000, 1000, None)[1])
        self.assertIn("5× the channel average", tier_for(500, 10**6, 100.0)[1])


def channel(conn, cid, subs, views, fmt_h=1080, fmt_w=1920, dur=600, when="2025-06-01T00:00:00Z", start=0):
    entries = [{"id": f"{cid}{start + i:03d}".ljust(11, "x"), "title": f"Video {start + i}", "timestamp": 1690000000 + i * 86400,
                "duration": dur, "view_count": v, "channel_id": cid, "width": fmt_w, "height": fmt_h} for i, v in enumerate(views)]
    ingest_text(conn, json.dumps({"_type": "playlist", "id": cid, "channel_id": cid, "channel": cid,
                                  "channel_follower_count": subs, "entries": entries}), pulled_at=when)


class TierData(unittest.TestCase):
    def test_tiers_and_counts_per_channel(self):
        conn = db.connect(":memory:")
        channel(conn, "UCa", 1000, [50, 120, 400, 1500, 60])
        d = analysis.build_dashboard_data(conn)["variants"]["all"]
        tiers = {v["views"]: v["tier"] for v in d["detail"]["UCa"]["videos"]}
        self.assertEqual(tiers, {50: None, 60: None, 120: "hot", 400: "outlier", 1500: "fire"})
        self.assertEqual(d["channels"][0]["tier_counts"], {"fire": 1, "outlier": 1, "hot": 1})

    def test_young_videos_are_tiered_too(self):
        conn = db.connect(":memory:")
        ingest_text(conn, json.dumps({"id": "vid00000001", "title": "t", "view_count": 5000, "timestamp": 1760000000,
                                      "channel_id": "UCa", "channel_follower_count": 1000}), pulled_at="2025-10-10T00:00:00Z")
        v = analysis.build_dashboard_data(conn)["variants"]["all"]["detail"]["UCa"]["videos"][0]
        self.assertFalse(v["mature"])
        self.assertEqual(v["tier"], "fire")

    def test_channel_average_needs_three_videos(self):
        conn = db.connect(":memory:")
        channel(conn, "UCa", 10**7, [100, 600])                                 # 2 videos: no average, no subs-based tier
        self.assertEqual([v["tier"] for v in analysis.build_dashboard_data(conn)["variants"]["all"]["detail"]["UCa"]["videos"]], [None, None])
        conn = db.connect(":memory:")
        channel(conn, "UCb", 10**7, [100, 100, 100, 100, 700])                  # 5x+ the (mean-of-five) average? mean=220, 700/220 = 3.2x
        self.assertEqual({v["views"]: v["tier"] for v in analysis.build_dashboard_data(conn)["variants"]["all"]["detail"]["UCb"]["videos"]}[700], "hot")

    def test_the_average_is_per_format_slice(self):
        conn = db.connect(":memory:")
        channel(conn, "UCa", 10**7, [100] * 7, start=0)                                       # 7 long-form videos, ~100 views
        channel(conn, "UCa", 10**7, [100_000] * 3, fmt_h=1920, fmt_w=1080, dur=30, start=50)  # 3 Shorts, ~100K views
        v = analysis.build_dashboard_data(conn)["variants"]
        tiered = lambda m: sorted((x["format"], x["tier"]) for x in v[m]["detail"]["UCa"]["videos"] if x["tier"])
        # mixed: mean = 30,070, so each Short is 3.3x the average -> HOT. Alone, Shorts are all typical for Shorts.
        self.assertEqual(tiered("all"), [("short", "hot")] * 3)
        self.assertEqual(tiered("short"), [])
        self.assertEqual(tiered("long"), [])


if __name__ == "__main__":
    unittest.main()
