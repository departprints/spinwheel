import json
import sqlite3
import unittest
import urllib.error

from spinwheel import analysis, db, suggest, topics
from spinwheel.ingest import ingest_text

LONG_AGO = "2025-06-01T00:00:00Z"


def mem():
    return db.connect(":memory:")


def channel(conn, cid, subs, specs, when=LONG_AGO):
    """specs: [(title, views, tags)] -- all published well before `when`, so all are 'mature'."""
    entries = [{"id": f"{cid}{i:02d}".ljust(11, "x"), "title": t, "timestamp": 1690000000 + i * 86400, "duration": 600,
                "view_count": v, "tags": tags, "channel_id": cid} for i, (t, v, tags) in enumerate(specs)]
    ingest_text(conn, json.dumps({"_type": "playlist", "id": cid, "channel_id": cid, "channel": cid,
                                  "channel_follower_count": subs, "entries": entries}), pulled_at=when)


class Phrases(unittest.TestCase):
    def test_format_words_cannot_start_or_end_a_phrase(self):
        p = topics.phrases_for("7 sourdough tips that changed everything")
        self.assertIn("sourdough", p)
        self.assertNotIn("tips", p)
        self.assertNotIn("tips that changed", p)

    def test_multiword_topics_and_filler_in_the_middle(self):
        p = topics.phrases_for("Why nobody talks about home server builds")
        self.assertIn("home server", p)
        self.assertNotIn("about home", p)        # starts on a filler word

    def test_tags_are_phrases_too(self):
        self.assertIn("mechanical keyboards", topics.phrases_for("My setup", ["Mechanical Keyboards"]))

    def test_identical_video_sets_merge_to_the_longest_label(self):
        a = {"id": "a", "views": 10, "published": "2025-01-01", "_phrases": topics.phrases_for("Zebra garden one")}
        b = {"id": "b", "views": 30, "published": "2025-02-01", "_phrases": topics.phrases_for("Zebra garden two")}
        rows = topics.channel_topics([a, b], 10)
        zg = next(r for r in rows if "zebra" in r["phrase"])
        self.assertEqual(zg["phrase"], "zebra garden")
        self.assertEqual(sorted(zg["also"]), ["garden", "zebra"])
        self.assertEqual(zg["n"], 2)


class Gaps(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        own = [(f"Alpha story {w}", 100, []) for w in ("one", "two", "three", "four", "five", "six")] + [("Alpha big win", 900, [])]
        other = [(f"Zebra garden {w}", 1000, []) for w in ("red", "blue", "green")] + \
                [(f"Beta thing {w}", 100, []) for w in ("red", "blue", "green")]
        channel(self.conn, "UCown", 1000, own)
        channel(self.conn, "UCoth", 5000, other)
        self.d = analysis.build_dashboard_data(self.conn)

    def test_topic_that_works_elsewhere_and_is_absent_here_is_a_gap(self):
        gaps = {g["phrase"]: g for g in self.d["variants"]["all"]["detail"]["UCown"]["gaps"]}
        self.assertIn("zebra garden", gaps)
        self.assertGreaterEqual(gaps["zebra garden"]["median_score"], 1.3)
        self.assertEqual(gaps["zebra garden"]["channels"], ["UCoth"])

    def test_nothing_is_a_gap_for_the_channel_that_already_covers_it(self):
        self.assertNotIn("zebra garden", [g["phrase"] for g in self.d["variants"]["all"]["detail"]["UCoth"]["gaps"]])

    def test_a_single_channel_has_no_gaps(self):
        conn = mem()
        channel(conn, "UCsolo", 100, [(f"Alpha {i}", 100, []) for i in range(6)])
        self.assertEqual(analysis.build_dashboard_data(conn)["variants"]["all"]["detail"]["UCsolo"]["gaps"], [])

    def test_ideas_cover_follow_up_and_gap_and_carry_a_brief(self):
        ideas = {(i["type"], i["topic"]): i for i in self.d["variants"]["all"]["detail"]["UCown"]["ideas"]}
        self.assertIn(("gap", "zebra garden"), ideas)
        fu = next(i for i in self.d["variants"]["all"]["detail"]["UCown"]["ideas"] if i["type"] == "follow_up")
        self.assertIn("Alpha big win", fu["headline"])
        self.assertIn("Why this idea", fu["brief"])
        self.assertEqual(fu["evidence"][0][0], "UCown")

    def test_internal_fields_do_not_reach_the_page(self):
        out = analysis.to_json(self.d)
        for private in ("_phrases", "topics_all", '"packaging"', '"tags"'):
            self.assertNotIn(private, out.replace('"packaging":{"length"', ""), private)


class Suggestions(unittest.TestCase):
    def test_parse_response(self):
        self.assertEqual(suggest.parse_response('["q",["a b"," c ",""],[],{}]'), ["a b", "c"])
        with self.assertRaises(ValueError):
            suggest.parse_response("<html>blocked</html>")

    def test_collect_stores_per_seed_and_a_failure_stores_nothing_for_that_seed(self):
        conn = mem()

        def fake(q):
            if q == "bad":
                raise urllib.error.URLError("rate limited")
            return [q, q + " ideas"]
        r = suggest.collect(conn, ["Good", "bad", "good"], fetch_fn=fake, sleep=lambda s: None, captured_at="2026-01-01T00:00:00Z")
        self.assertEqual(r["stored"], {"good": 2})
        self.assertIn("bad", r["failed"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_suggestions WHERE seed='bad'").fetchone()[0], 0)

    def test_expand_asks_for_every_letter_and_pauses_between_requests(self):
        conn, asked, naps = mem(), [], []
        suggest.collect(conn, ["x"], expand=True, delay=0.5, fetch_fn=lambda q: asked.append(q) or [], sleep=naps.append)
        self.assertEqual(len(asked), 27)
        self.assertEqual(naps, [0.5] * 26)

    def test_second_fetch_marks_new_phrases_and_keeps_the_first_fetch(self):
        conn = mem()
        suggest.collect(conn, ["van life"], fetch_fn=lambda q: ["van life", "van life tour"], captured_at="2026-01-01T00:00:00Z")
        suggest.collect(conn, ["van life"], fetch_fn=lambda q: ["van life", "van life build"], captured_at="2026-02-01T00:00:00Z")
        s = analysis._suggestions(conn)["van life"]
        self.assertEqual({i["text"]: i["new"] for i in s["items"]}, {"van life": False, "van life build": True})
        self.assertEqual(s["prev_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_suggestions").fetchone()[0], 4)   # nothing overwritten

    def test_suggestions_are_append_only(self):
        conn = mem()
        suggest.collect(conn, ["x"], fetch_fn=lambda q: ["x y"])
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM search_suggestions")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE search_suggestions SET suggestion='z'")

    def test_stored_suggestions_attach_to_matching_ideas(self):
        conn = mem()
        channel(conn, "UCown", 1000, [(f"Alpha story {w}", 100, []) for w in "abcdef"] + [("Alpha big win", 900, [])])
        suggest.collect(conn, ["alpha"], fetch_fn=lambda q: ["alpha beta", "alpha gamma"])
        ideas = analysis.build_dashboard_data(conn)["variants"]["all"]["detail"]["UCown"]["ideas"]
        searched = [i for i in ideas if i["searches"]]
        self.assertTrue(searched and searched[0]["searches"][0]["text"] == "alpha beta")

    def test_top_seeds_are_short_topics(self):
        conn = mem()
        channel(conn, "UCown", 1000, [(f"Alpha story {w}", 100 * (1 + i), []) for i, w in enumerate("abcdef")])
        seeds = analysis.top_seeds(conn, 5)
        self.assertTrue(seeds and all(len(s.split()) <= 3 for s in seeds))


if __name__ == "__main__":
    unittest.main()
