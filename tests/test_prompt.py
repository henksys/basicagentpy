# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import unittest

from basicagentpy.prompt import build_system_prompt


EMPTY_CONFIG = {"agent": {"systemPrompt": "", "systemPromptFile": ""}}


class PromptTests(unittest.TestCase):
    def test_default_persona_names_basicagentpy_and_includes_the_skills_index(self):
        skills = {"demo": {"name": "demo", "description": "does demo things"}}
        text = build_system_prompt(
            config=EMPTY_CONFIG, skills=skills, memory=None, cwd="/tmp/work"
        )
        self.assertRegex(text, r"You are basicagentpy")
        self.assertRegex(text, r"- demo: does demo things")
        self.assertRegex(text, r"Working directory: /tmp/work")

    def test_a_custom_system_prompt_replaces_only_the_persona(self):
        config = {
            "agent": {
                "systemPrompt": "You are Alfred, a butler.",
                "systemPromptFile": "",
            }
        }
        text = build_system_prompt(
            config=config, skills={}, memory=None, cwd="/tmp"
        )
        self.assertTrue(text.startswith("You are Alfred, a butler."))
        self.assertNotIn("You are basicagentpy", text)
        self.assertIn("## Using tools", text)
        self.assertIn("## Safety rules", text)

    def test_saved_facts_are_included_and_marked_untrusted(self):
        memory = type(
            "Memory",
            (),
            {"facts_context": lambda self, max_bytes=4000: "- editor: nvim"},
        )()
        text = build_system_prompt(
            config=EMPTY_CONFIG, skills={}, memory=memory, cwd="/tmp"
        )
        self.assertIn("## Saved facts", text)
        self.assertIn("- editor: nvim", text)
        self.assertIn("untrusted reference data", text)


if __name__ == "__main__":
    unittest.main()
