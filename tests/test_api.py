import json
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from spinwheel import analysis, db, discover, youtube_api as Y
from spinwheel.cli import main
from spinwheel.ingest import ingest_text

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
SECRET = "AIza-SECRET-KEY-123"


def video_item(i, views=1000, channel="UCchan", published="2026-10-11T08:00:00Z"):
    return {"kind": "youtube#video", "id": i, "snippet": {"publishedAt": published, "channelId": channel, "title": f"Title {i}"},
            "contentDetails": {"duration": "PT10M"}, "statistics": {"viewCount": str(views), "likeCount": "5", "commentCount": "2"}}


class FakeApi:
    """Answers the endpoints the way the documented API does, recording every URL called."""
    def __init__(self, found=("aaaaaaaaaaa", "bbbbbbbbbbb")):
        self.urls, self.found = [], list(found)

    def __call__(self, url):
        self.urls.append(url)
        ep = url.split("/youtube/v3/")[1].split("?")[0]
        q = dict(p.split("=", 1) for p in url.split("?")[1].split("&"))
        if ep == "search":
            return json.dumps({"kind": "youtube#searchListResponse", "etag": "e", "items": [
                {"kind": "youtube#searchResult", "id": {"kind": "youtube#video", "videoId": i},
                 "snippet": {"publishedAt": "2026-10-11T08:00:00Z", "channelId": "UCchan", "title": f"Title {i}"}} for i in self.found]})
        if ep == "videos":
            ids = q["id"].replace("%2C", ",").split(",")
            return json.dumps({"kind": "youtube#videoListResponse", "etag": "e", "items": [video_item(i) for i in ids]})
        if ep == "channels":
            return json.dumps({"kind": "youtube#channelListResponse", "etag": "e", "items": [
                {"kind": "youtube#channel", "id": c, "snippet": {"title": "Chan"}, "statistics": {"subscriberCount": "4200", "viewCount": "9", "videoCount": "3"}}
                for c in q["id"].replace("%2C", ",").split(",")]})
        if ep == "commentThreads":
            return json.dumps({"kind": "youtube#commentThreadListResponse", "etag": "e", "items": [
                {"kind": "youtube#commentThread", "id": "T1", "snippet": {"videoId": q["videoId"], "channelId": "UCchan",
                 "topLevelComment": {"id": "C1", "snippet": {"textOriginal": "How much did this cost in total?", "likeCount": 30, "authorChannelId": {"value": "UCfan"}}}},
                 "replies": {"comments": [{"id": "C1.r", "snippet": {"textOriginal": "About $400", "likeCount": 1, "authorChannelId": {"value": "UCchan"}}}]}}]})
        raise AssertionError(ep)


class Calls(unittest.TestCase):
    def test_key_is_in_the_request_but_never_in_what_is_stored(self):
        conn, api = db.connect(":memory:"), FakeApi()
        Y.search(conn, "ai documentary", SECRET, within="today", now=NOW, fetch=api)
        self.assertTrue(all(SECRET in u for u in api.urls))
        for (command,) in conn.execute("SELECT command FROM pulls"):
            self.assertNotIn(SECRET, command)
            self.assertIn("key redacted", command)
        for (body,) in conn.execute("SELECT body FROM raw_payloads"):
            self.assertNotIn(SECRET, body if isinstance(body, str) else "")

    def test_search_within_today_asks_for_the_last_24_hours_and_maps_the_topic(self):
        conn, api = db.connect(":memory:"), FakeApi()
        r = Y.search(conn, "ai documentary", SECRET, within="today", now=NOW, fetch=api)
        self.assertIn("publishedAfter=2026-10-10T12%3A00%3A00Z", api.urls[0])
        self.assertIn("order=viewCount", api.urls[0])
        self.assertEqual((r["found"], r["enriched"], r["channels"]), (2, 2, 1))
        rows = conn.execute("SELECT query, within, rank FROM searches ORDER BY rank").fetchall()
        self.assertEqual([tuple(x) for x in rows], [("ai documentary", "today", 0), ("ai documentary", "today", 1)])
        # search -> videos.list -> channels.list: real dates, stats and subscriber counts with no scraping
        v = analysis.build_dashboard_data(conn)["variants"]["all"]["detail"]["UCchan"]["videos"][0]
        self.assertEqual((v["published"], v["views"], v["subs"]), ("2026-10-11T08:00:00Z", 1000, 4200))

    def test_batches_of_fifty(self):
        conn, api = db.connect(":memory:"), FakeApi()
        ids = [f"v{i:010d}" for i in range(120)]
        self.assertEqual(Y.videos(conn, ids, SECRET, api), 120)
        self.assertEqual(len(api.urls), 3)                       # 50 + 50 + 20
        self.assertEqual(len(api.urls[0].split("id=")[1].split("&")[0].split("%2C")), 50)

    def test_channels_that_hide_subscribers_are_skipped_not_fatal(self):
        conn = db.connect(":memory:")
        hidden = lambda url: json.dumps({"kind": "youtube#channelListResponse", "items": [
            {"kind": "youtube#channel", "id": "UCx", "snippet": {"title": "X"}, "statistics": {"hiddenSubscriberCount": True, "viewCount": "5"}}]})
        self.assertEqual(Y.channels(conn, ["UCx"], SECRET, hidden), 1)
        self.assertIsNone(conn.execute("SELECT subscribers FROM channel_snapshots").fetchone()[0])

    def test_comment_threads_know_whether_the_channel_replied(self):
        conn = db.connect(":memory:")
        ingest_text(conn, json.dumps({"kind": "youtube#videoListResponse", "etag": "e", "items": [video_item("aaaaaaaaaaa", 50_000)]}))
        r = Y.thread_comments(conn, ["aaaaaaaaaaa"], SECRET, fetch=FakeApi(), sleep=lambda s: None)
        self.assertEqual(r["stored"], {"aaaaaaaaaaa": 2})
        a = analysis.build_dashboard_data(conn)["variants"]["all"]["detail"]["UCchan"]
        self.assertEqual((a["questions_answered"], a["unanswered"]), (1, []))        # the owner's reply counts as an answer

    def test_errors_are_explained(self):
        quota = json.dumps({"error": {"code": 403, "message": "q", "errors": [{"reason": "quotaExceeded"}]}})
        self.assertIn("quota", Y.describe_error(403, quota))
        bad = json.dumps({"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "errors": [{"reason": "badRequest"}]}})
        self.assertIn("rejected", Y.describe_error(400, bad))
        self.assertIn("disabled", Y.describe_error(403, json.dumps({"error": {"message": "x", "errors": [{"reason": "commentsDisabled"}]}})))

    def test_quota_exhaustion_stops_a_comment_run(self):
        conn = db.connect(":memory:")
        calls = []

        def fetch(url):
            calls.append(url)
            raise Y.ApiError("daily quota used up")
        r = Y.thread_comments(conn, ["a", "b", "c"], SECRET, fetch=fetch, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(r["failed"]), ["a"])


class Cli(unittest.TestCase):
    def test_a_missing_key_is_explained_and_nothing_is_called(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YOUTUBE_API_KEY", None)
            self.assertEqual(main(["--db", ":memory:", "api", "enrich"]), 1)

    def test_get_key_reads_the_named_variable(self):
        with mock.patch.dict(os.environ, {"MY_KEY": "abc"}):
            self.assertEqual(Y.get_key("MY_KEY"), "abc")
        with self.assertRaises(Y.ApiError):
            Y.get_key("SURELY_NOT_SET_12345")


if __name__ == "__main__":
    unittest.main()
