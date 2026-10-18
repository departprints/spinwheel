import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from spinwheel import dashboard, db, serve
from spinwheel.ingest import ingest_text

PULL = json.dumps({"_type": "playlist", "id": "UC1", "channel_id": "UC1", "channel": "Chan", "channel_follower_count": 10,
                   "entries": [{"id": "v1", "title": "T", "view_count": 5, "channel_id": "UC1", "channel": "Chan",
                                "duration": 100, "timestamp": 1700000000}]})


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.db")
        self.conn = db.connect(self.path)
        ingest_text(self.conn, PULL, command="yt-dlp -J https://www.youtube.com/@c/videos")
        ingest_text(self.conn, PULL, command="yt-dlp -J https://www.youtube.com/@c/videos")  # duplicate
        ingest_text(self.conn, PULL, command="yt-dlp -J --flat-playlist ytsearch5:cats")
        ingest_text(self.conn, PULL, command="curl https://example.com")

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def test_only_channel_pulls_are_repeated_once(self):
        cmds = serve.stored_pull_commands(self.conn)
        self.assertEqual(cmds, [["yt-dlp", "-J", "https://www.youtube.com/@c/videos"]])

    def test_refresh_reingests_and_reports_failures(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout=PULL, stderr="")

        before = self.conn.execute("SELECT COUNT(*) FROM pulls").fetchone()[0]
        with mock.patch("spinwheel.serve.discover.refresh_channels", return_value={"stored": [], "failed": {}}):
            steps = serve.refresh(self.path, run=run)
        self.assertEqual(calls[0][:2], ["yt-dlp", "-J"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pulls").fetchone()[0], before + 1)
        self.assertEqual([s["step"] for s in steps], ["Channel pulls", "Tracked topics", "Subscriber counts", "Publish dates"])

        failing = lambda cmd, **kw: mock.Mock(returncode=1, stdout="", stderr="boom")
        steps = serve.refresh(self.path, run=failing)
        self.assertFalse(steps[0]["ok"])
        self.assertIn("exited 1", steps[0]["detail"])

    def test_page_only_shows_button_when_live(self):
        self.assertIn("const LIVE = false;", dashboard.render(self.conn))
        self.assertIn("const LIVE = true;", dashboard.render(self.conn, live=True))

    def test_refresh_from_saved_file_rewrites_it_and_allows_null_origin(self):
        out = os.path.join(self.dir.name, "dashboard.html")
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(self.path, 60, "yt-dlp", [], out))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            pre = urllib.request.urlopen(urllib.request.Request(base + "/refresh", method="OPTIONS", headers={"Origin": "null"}))
            self.assertEqual(pre.headers["Access-Control-Allow-Origin"], "null")
            other = urllib.request.urlopen(urllib.request.Request(base + "/refresh", method="OPTIONS", headers={"Origin": "https://evil.example"}))
            self.assertIsNone(other.headers["Access-Control-Allow-Origin"])
            with mock.patch("spinwheel.serve.refresh", return_value=[{"step": "x", "ok": True, "detail": ""}]):
                r = urllib.request.urlopen(urllib.request.Request(
                    base + "/refresh", method="POST", headers={"Origin": "null", "X-Spinwheel": "1"}))
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], "null")
            self.assertIn("const LIVE = false;", open(out, encoding="utf-8").read())
        finally:
            server.shutdown()
            server.server_close()

    def test_http_guards(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(self.path, 60, "yt-dlp", []))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            self.assertIn("const LIVE = true;", urllib.request.urlopen(base + "/").read().decode())
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + "/refresh", method="POST"))  # no guard header
            self.assertEqual(e.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + "/", headers={"Host": "evil.example"}))
            self.assertEqual(e.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
