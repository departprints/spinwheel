import plistlib
import unittest

from spinwheel import autostart


class AutostartTests(unittest.TestCase):
    def test_plist_pins_everything_launchd_would_not_provide(self):
        p = autostart.build_plist(python="/usr/bin/python3", package_root="/repo", workdir="/data", db_path="/data/spinwheel.db",
                                  output="/data/dashboard.html", yt_dlp="/opt/homebrew/bin/yt-dlp", port=8765,
                                  extra_args=["--cookies-from-browser", "firefox"], log="/tmp/x.log")
        self.assertEqual(p["ProgramArguments"][:6], ["/usr/bin/python3", "-m", "spinwheel", "--db", "/data/spinwheel.db", "serve"])
        self.assertIn("--yt-dlp", p["ProgramArguments"])
        self.assertEqual(p["ProgramArguments"][-2:], ["--yt-dlp-args", "--cookies-from-browser firefox"])
        self.assertEqual(p["EnvironmentVariables"]["PYTHONPATH"], "/repo")
        self.assertTrue(p["RunAtLoad"] and p["KeepAlive"])
        plistlib.loads(plistlib.dumps(p))  # serializes

    def test_other_platforms_get_a_clear_error(self):
        from unittest import mock
        with mock.patch("spinwheel.autostart.sys.platform", "linux"):
            with self.assertRaises(ValueError):
                autostart.install("a.db", "d.html")


if __name__ == "__main__":
    unittest.main()
