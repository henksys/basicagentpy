# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import unittest

from basicagentpy.ui import prompt_label


CYAN = "\x1b[36m"
RESET = "\x1b[0m"


class FakeStream:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


class PromptLabelTests(unittest.TestCase):
    def test_prompt_label_paints_the_label_cyan_on_a_tty(self):
        self.assertEqual(
            prompt_label("basicagentpy", stream=FakeStream(True), env={}),
            f"{CYAN}basicagentpy{RESET}",
        )

    def test_prompt_label_stays_plain_when_the_prompt_stream_is_not_a_tty(self):
        self.assertEqual(
            prompt_label("basicagentpy", stream=FakeStream(False), env={}),
            "basicagentpy",
        )

    def test_prompt_label_respects_no_color(self):
        self.assertEqual(
            prompt_label(
                "basicagentpy",
                stream=FakeStream(True),
                env={"NO_COLOR": "1"},
            ),
            "basicagentpy",
        )


if __name__ == "__main__":
    unittest.main()
