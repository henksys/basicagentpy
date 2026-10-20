# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import subprocess
import unittest

from basicagentpy.web.open_browser import browser_command, open_browser

URL_UNDER_TEST = "http://127.0.0.1:8787/#token=abc"


class OpenBrowserTests(unittest.TestCase):
    def test_browser_command_picks_the_right_launcher_per_platform(self):
        self.assertEqual(browser_command("linux", URL_UNDER_TEST), ("xdg-open", [URL_UNDER_TEST]))
        self.assertEqual(browser_command("darwin", URL_UNDER_TEST), ("open", [URL_UNDER_TEST]))
        self.assertEqual(
            browser_command("win32", URL_UNDER_TEST),
            ("cmd", ["/c", "start", "", URL_UNDER_TEST]),
        )

    def test_open_browser_spawns_a_detached_launcher(self):
        calls = []

        class Child:
            def wait(self):
                return 0

        def spawn_impl(args, **kwargs):
            calls.append({"args": args, **kwargs})
            return Child()

        result = open_browser(URL_UNDER_TEST, platform="linux", spawn_impl=spawn_impl)
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"], ["xdg-open", URL_UNDER_TEST])
        self.assertTrue(calls[0]["start_new_session"])
        self.assertEqual(calls[0]["stdin"], subprocess.DEVNULL)
        self.assertEqual(calls[0]["stdout"], subprocess.DEVNULL)
        self.assertEqual(calls[0]["stderr"], subprocess.DEVNULL)

    def test_open_browser_returns_false_when_spawning_fails(self):
        def spawn_impl(args, **kwargs):
            raise OSError("spawn ENOENT")

        self.assertFalse(open_browser(URL_UNDER_TEST, platform="linux", spawn_impl=spawn_impl))


if __name__ == "__main__":
    unittest.main()
