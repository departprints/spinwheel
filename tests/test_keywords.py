import json
import math
import unittest
from datetime import datetime, timedelta, timezone

from spinwheel import analysis, db, keywords as K, topics as T
from spinwheel.ingest import ingest_text

REF = datetime(2026, 10, 11, tzinfo=timezone.utc)
parse_ts = analysis.parse_ts


class Formulas(unittest.TestCase):
    def test_lognorm_is_a_capped_log_scale(self):
        self.assertEqual(K.lognorm(0, 100), 0)
        self.assertEqual(K.lognorm(None, 100), 0)
        self.assertAlmostEqual(K.lognorm(99, 99), 100)
        self.assertEqual(K.lognorm(10**9, 99), 100)                            # capped
        self.assertAlmostEqual(K.lognorm(9, 99), 50)                           # log10(10)/log10(100)
        self.assertGreater(K.lognorm(1000, 10**7), K.lognorm(100, 10**7))

    def test_difficulty_is_65_percent_subscribers_35_percent_volume(self):
        self.assertEqual(K.difficulty(K.SUBS_CAP, K.VOLUME_CAP), 100)
        self.assertEqual(K.difficulty(K.SUBS_CAP, 0), 65)                      # only the subscriber part
        self.assertEqual(K.difficulty(None, K.VOLUME_CAP), 35)                 # only the volume part
        self.assertEqual(K.difficulty(None, 0), 0)

    def test_opportunity_is_60_percent_views_40_percent_views_per_subscriber(self):
        self.assertEqual(K.opportunity(K.VIEWS_CAP, 1.0), 100)
        self.assertEqual(K.opportunity(K.VIEWS_CAP, 0), 60)
        self.assertEqual(K.opportunity(0, 1.0), 40)
        self.assertEqual(K.opportunity(0, 5.0), 40)                            # views per sub is capped at 1.0 = FIRE line
        self.assertEqual(K.opportunity(0, 0.5), 20)

    def test_gap_score_is_average_views_over_channels(self):
        self.assertEqual(K.gap_score(90_000, 3), 30_000)
        self.assertIsNone(K.gap_score(None, 3))
        self.assertIsNone(K.gap_score(100, 0))


def vid(i, title, views, subs, channel="UC1", published=None, tier=None):
    return {"id": f"v{i}", "title": title, "views": views, "subs": subs, "channel_id": channel, "published": published, "tier": tier}


class Heat(unittest.TestCase):
    def days(self, n):
        return (REF - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_trending_needs_new_uploads_that_outpace_old_ones(self):
        new = [vid(i, "t", 30_000, 1, published=self.days(5 + i)) for i in range(3)]       # ~6K views/day
        old = [vid(10 + i, "t", 30_000, 1, published=self.days(200 + i)) for i in range(3)]  # ~150 views/day
        self.assertEqual(K.heat(new + old, REF, parse_ts), "TRENDING")

    def test_active_when_recent_but_not_faster(self):
        new = [vid(i, "t", 100, 1, published=self.days(5 + i)) for i in range(2)]
        old = [vid(10 + i, "t", 100_000, 1, published=self.days(100 + i)) for i in range(4)]
        self.assertEqual(K.heat(new + old, REF, parse_ts), "ACTIVE")

    def test_steady_when_nothing_is_recent(self):
        self.assertEqual(K.heat([vid(i, "t", 100, 1, published=self.days(90 + i)) for i in range(6)], REF, parse_ts), "STEADY")

    def test_unknown_without_enough_dated_videos(self):
        self.assertIsNone(K.heat([vid(i, "t", 100, 1, published=self.days(5)) for i in range(4)], REF, parse_ts))
        self.assertIsNone(K.heat([vid(i, "t", 100, 1) for i in range(9)], REF, parse_ts))         # undated
        self.assertIsNone(K.heat([], None, parse_ts))


class LongTail(unittest.TestCase):
    def test_phrase_must_appear_in_two_titles_and_may_end_on_a_format_word(self):
        vs = [vid(1, "Best cast iron seasoning tips", 1000, 10_000), vid(2, "cast iron seasoning tips for beginners", 3000, 10_000),
              vid(3, "My unrelated rant", 500, 10_000)]
        phrases = {r["phrase"]: r for r in K.long_tail(vs, "cast iron")}
        self.assertTrue(any("seasoning tips" in p for p in phrases))           # "tips" is allowed at the end here
        self.assertFalse(any("rant" in p for p in phrases))                    # appears in one title only
        self.assertNotIn("cast iron", phrases)                                 # the topic itself is not a long-tail variant

    def test_phrases_never_start_or_end_on_filler_and_need_a_subject_word(self):
        p = T.long_tail_phrases("tips that I made")
        self.assertEqual(p, frozenset())                                       # nothing but format and filler words
        self.assertIn("budget travel tips", T.long_tail_phrases("how I do budget travel tips"))
        self.assertNotIn("of budget travel", T.long_tail_phrases("a list of budget travel"))

    def test_scores_use_the_formulas_with_the_competing_channels(self):
        vs = [vid(1, "iron skillet tips", 40_000, 100_000, channel="UCa"), vid(2, "iron skillet tips", 60_000, 300_000, channel="UCb")]
        r = next(x for x in K.long_tail(vs, "other") if x["phrase"] == "iron skillet tips")
        avg_subs, avg_views = 200_000, 50_000
        vps = (0.4 + 0.2) / 2
        self.assertEqual(r["difficulty"], K.difficulty(avg_subs, 2))
        self.assertEqual(r["opportunity"], K.opportunity(avg_views, vps))
        self.assertEqual((r["videos"], r["channels"]), (2, 2))

    def test_ranked_by_opportunity_then_lower_difficulty(self):
        big = [vid(i, "alpha beta gamma", 900_000, 9_000_000, channel=f"UC{i}") for i in range(2)]      # strong views, huge channels
        small = [vid(10 + i, "delta epsilon zeta", 900_000, 9_000_000, channel="UCs") for i in range(2)]
        rows = K.long_tail(big + small, "x")
        self.assertEqual([r["opportunity"] for r in rows], sorted((r["opportunity"] for r in rows), reverse=True))


def flat_search(conn, topic, entries):
    ingest_text(conn, json.dumps({"_type": "playlist", "id": topic, "entries": entries}), pulled_at="2026-10-01T00:00:00Z", search_query=topic)


def entry(i, title, views, channel, subs, ts=1759000000, **kw):
    e = {"id": f"vid{i:08d}", "title": title, "view_count": views, "duration": 600, "channel_id": channel, "channel": channel,
         "channel_follower_count": subs, "timestamp": ts, "width": 1920, "height": 1080}
    e.update(kw)
    return e


class TrackedTopics(unittest.TestCase):
    def build(self):
        conn = db.connect(":memory:")
        flat_search(conn, "Cast Iron", [entry(1, "cast iron tips", 10_000, "UCa", 1000), entry(2, "cast iron guide", 30_000, "UCa", 1000),
                                        entry(3, "cast iron care", 20_000, "UCb", 100_000)])
        flat_search(conn, "tiny", [entry(4, "tiny thing", 5, "UCc", 10)])
        return analysis.build_dashboard_data(conn)

    def test_topic_stats_and_gap_score(self):
        t = {x["topic"]: x for x in self.build()["variants"]["all"]["topics"]}["cast iron"]
        self.assertEqual((t["videos"], t["channels"], t["avg_views"]), (3, 2, 20_000))
        self.assertEqual(t["gap_score"], 10_000)                               # 20K average / 2 channels
        # UCa (1,000 subs): 10K and 30K views are both >= 100% of subscribers = FIRE; UCb (100K subs): 20K is 20% = HOT
        self.assertEqual(t["tiers"], {"fire": 2, "outlier": 0, "hot": 1})

    def test_thin_topics_sort_last_even_with_a_high_gap_score(self):
        conn = db.connect(":memory:")
        flat_search(conn, "solid", [entry(i, f"solid {i}", 1000, "UCa", 10**6) for i in range(3)])
        flat_search(conn, "one hit", [entry(9, "one hit wonder", 10**9, "UCb", 10)])
        topics = analysis.build_dashboard_data(conn)["variants"]["all"]["topics"]
        self.assertEqual([t["topic"] for t in topics], ["solid", "one hit"])
        self.assertTrue(topics[1]["thin"] and not topics[0]["thin"])

    def test_topics_follow_the_format_slice(self):
        conn = db.connect(":memory:")
        flat_search(conn, "mix", [entry(1, "a long one", 1000, "UCa", 5000), entry(2, "a short one", 9000, "UCa", 5000, duration=30, width=1080, height=1920)])
        v = analysis.build_dashboard_data(conn)["variants"]
        n = lambda m: v[m]["topics"][0]["videos"] if v[m]["topics"] else 0
        self.assertEqual((n("all"), n("long"), n("short")), (2, 1, 1))

    def test_videos_never_fetched_do_not_break_a_topic(self):
        conn = db.connect(":memory:")
        flat_search(conn, "t", [entry(1, "alpha beta one", 100, "UCa", 5000)])
        conn.execute("INSERT INTO videos(video_id, first_seen_at) VALUES ('ghost','2026-10-01T00:00:00Z')")
        conn.execute("INSERT INTO searches(pull_id, query, video_id, rank, captured_at) VALUES (1,'t','ghost',9,'2026-10-01T00:00:00Z')")
        self.assertEqual(analysis.build_dashboard_data(conn)["variants"]["all"]["topics"][0]["videos"], 1)

    def test_search_mapping_is_append_only_and_accumulates_across_runs(self):
        conn = db.connect(":memory:")
        flat_search(conn, "t", [entry(1, "alpha beta one", 100, "UCa", 5000)])
        flat_search(conn, "t", [entry(1, "alpha beta one", 150, "UCa", 5000), entry(2, "alpha beta two", 200, "UCa", 5000)])
        self.assertEqual(analysis.build_dashboard_data(conn)["variants"]["all"]["topics"][0]["videos"], 2)
        with self.assertRaises(Exception):
            conn.execute("DELETE FROM searches")


if __name__ == "__main__":
    unittest.main()
