# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import unittest

from basicagentpy.approval import create_approval


class ApprovalTests(unittest.TestCase):
    def test_yes_approves_automatically_and_emits_an_event(self):
        events = []
        approve = create_approval(yes=True, on_event=events.append)
        self.assertTrue(approve("do it"))
        self.assertTrue(events[0]["approved"])
        self.assertTrue(events[0]["automatic"])

    def test_non_interactive_runs_deny_and_explain_why(self):
        events = []
        notices = []
        approve = create_approval(
            interactive=False, on_event=events.append, on_notice=notices.append
        )
        self.assertFalse(approve("dangerous thing"))
        self.assertFalse(events[0]["approved"])
        self.assertTrue(events[0]["automatic"])
        self.assertRegex(notices[0], r"--yes")

    def test_interactive_runs_ask_and_parse_yes_no_answers(self):
        asked = []

        def question(text):
            asked.append(text)
            return answers.pop(0)

        approve = create_approval(interactive=True, question=question)
        answers = ["y", "yes", "n", ""]
        self.assertTrue(approve("write file"))
        self.assertTrue(approve("write file"))
        self.assertFalse(approve("write file"))
        self.assertFalse(approve("write file"))
        self.assertEqual(len(asked), 4)
        self.assertIn("write file", asked[0])


if __name__ == "__main__":
    unittest.main()
