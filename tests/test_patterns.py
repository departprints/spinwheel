import unittest

from spinwheel import analysis, patterns as P
from spinwheel.patterns import structure_of


def v(i, title, views, subs=1000, channel="UC1", tier=None, duration=600, fmt="long"):
    return {"id": f"v{i}", "title": title, "views": views, "subs": subs, "channel_id": channel, "tier": tier,
            "duration_s": duration, "format": fmt}


class Structures(unittest.TestCase):
    def test_each_structure(self):
        cases = {
            "iPhone vs Android: which is better?": "Comparison",          # comparison wins over question
            "Is cast iron worth it?": "Question",
            "Why nobody talks about this": "Question",
            "How do I season a pan": "Question",
            "7 sourdough tips that changed everything": "Numbered list",
            "Top 10 espresso machines": "Numbered list",
            "How to season cast iron": "How-to",                           # not a question
            "The honest beginner guide to pasta": "How-to",
            "How I tried van life for 30 days": "Personal story",          # not a question
            "I changed my mind about minimalism": "Personal story",
            "Quarterly review notes": "Other",
        }
        for title, want in cases.items():
            self.assertEqual(structure_of(title), want, title)

    def test_the_question_feature_in_the_title_analysis_uses_the_same_rule(self):
        q = dict(analysis.TITLE_FEATURES)["Asks a question"]
        self.assertFalse(q("How to season cast iron"))
        self.assertTrue(q("How much does it cost"))
        self.assertTrue(q("Worth it?"))

    def test_new_title_patterns_are_in_the_pattern_list(self):
        names = [n for n, _ in analysis.TITLE_FEATURES]
        self.assertTrue(any("Numbered list" in n for n in names) and any("Comparison" in n for n in names))


class Clusters(unittest.TestCase):
    def test_over_representation_is_share_among_high_tier_over_share_overall(self):
        vids = [v(i, f"{i} sourdough tips", 100, tier="hot" if i < 3 else None) for i in range(4)]       # numbered: 4 videos, 3 high-tier
        vids += [v(10 + i, "Quarterly notes", 100, tier="hot" if i == 0 else None) for i in range(6)]    # other: 6 videos, 1 high-tier
        rows = {r["label"]: r for r in P.structure_clusters(vids)}
        n = rows["Numbered list"]
        self.assertEqual((n["videos"], n["high"]), (4, 3))
        self.assertAlmostEqual(n["share_high"], 3 / 4)
        self.assertAlmostEqual(n["share_all"], 4 / 10)
        self.assertAlmostEqual(n["over"], (3 / 4) / (4 / 10))
        self.assertEqual(list(rows)[0], "Numbered list")                    # most high-tier videos first

    def test_no_high_tier_videos_means_no_ratio_not_a_crash(self):
        r = P.structure_clusters([v(1, "Quarterly notes", 5)])[0]
        self.assertIsNone(r["over"])

    def test_examples_are_the_most_viewed_high_tier_videos(self):
        vids = [v(1, "1 tips", 10, tier="hot"), v(2, "2 tips", 90, tier="hot"), v(3, "3 tips", 50, tier="hot"), v(4, "4 tips", 999)]
        self.assertEqual([e[1] for e in P.structure_clusters(vids)[0]["examples"]], ["v2", "v3"])


class TopAndSmall(unittest.TestCase):
    def test_top_by_raw_views_ignores_unknown_and_limits(self):
        vids = [v(i, "t", i * 10) for i in range(1, 30)] + [v(99, "t", None)]
        top = P.top_by_views(vids)
        self.assertEqual(len(top), P.TOP_RAW)
        self.assertEqual(top[0][1], "v29")
        self.assertNotIn("v99", [t[1] for t in top])

    def test_small_channel_deep_dive_only_counts_fire_and_outlier_from_small_channels(self):
        vids = [
            v(1, "Is balcony garden worth it?", 9000, subs=2000, channel="UCs", tier="fire"),        # 4.5x subs
            v(2, "Is balcony garden hard?", 700, subs=2000, channel="UCs", tier="outlier", duration=30, fmt="short"),   # 0.35x
            v(3, "balcony garden setup", 100, subs=2000, channel="UCs", tier="hot"),                 # hot: excluded
            v(4, "Huge channel hit", 10**7, subs=5_000_000, channel="UCbig", tier="fire"),           # not small: excluded
            v(5, "Tiny channel nothing", 5, subs=100, channel="UCt", tier=None),
        ]
        d = P.small_channel_deep_dive(vids)
        self.assertEqual((d["videos"], d["channels"], d["shorts"]), (2, 1, 1))
        self.assertEqual([t[1] for t in d["top"]], ["v1", "v2"])                                     # most views per subscriber first
        self.assertEqual(d["structures"], [{"label": "Question", "n": 2}])
        self.assertIn("balcony garden", [p["phrase"] for p in d["phrases"]])
        self.assertEqual(d["median_length_s"], 315)

    def test_empty(self):
        self.assertEqual(P.small_channel_deep_dive([])["videos"], 0)


class AverageLift(unittest.TestCase):
    def test_average_lift_is_reported_next_to_the_median_lift(self):
        import json
        from spinwheel import db
        from spinwheel.ingest import ingest_text
        # with the pattern: 100, 100, 100, 10000 (one hit); without: 100 x4
        titles = [(f"{i} tips", x) for i, x in enumerate([100, 100, 100, 10_000])] + [("Quarterly notes", 100)] * 4
        entries = [{"id": f"vid{i:08d}", "title": t, "view_count": x, "duration": 600, "timestamp": 1690000000 + i * 86400,
                    "channel_id": "UCa"} for i, (t, x) in enumerate(titles)]
        conn = db.connect(":memory:")
        ingest_text(conn, json.dumps({"_type": "playlist", "id": "UCa", "channel_id": "UCa", "channel_follower_count": 10**6,
                                      "entries": entries}), pulled_at="2025-06-01T00:00:00Z")
        a = analysis.build_dashboard_data(conn)["variants"]["all"]["detail"]["UCa"]
        f = next(x for x in a["features"] if x["label"].startswith("Numbered list"))
        self.assertAlmostEqual(f["lift"], 1.0)                      # medians are both 100
        self.assertAlmostEqual(f["avg_lift"], 2575 / 100)           # the one hit swings the average


if __name__ == "__main__":
    unittest.main()
