# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import os
import shutil
import tempfile
import unittest

from basicagentpy.config import BUNDLED_SKILLS_DIR
from basicagentpy.skills import (
    discover_skills,
    load_skill_tool,
    parse_frontmatter,
    parse_skill_file,
    skill_index_text,
)


class SkillTests(unittest.TestCase):
    def test_parses_plain_folded_and_literal_frontmatter(self):
        plain = parse_frontmatter("---\nname: demo\ndescription: One line.\n---\nBody here.\n")
        self.assertEqual(plain[0]["name"], "demo")
        self.assertEqual(plain[0]["description"], "One line.")
        self.assertEqual(plain[1], "Body here.\n")

        folded = parse_frontmatter(
            "---\nname: f\ndescription: >\n  line one\n  line two\n\n  new paragraph\n---\nrest"
        )
        self.assertEqual(folded[0]["description"], "line one line two\nnew paragraph")

        literal = parse_frontmatter(
            "---\nname: l\ndescription: |\n  first\n  second\n---\nrest"
        )
        self.assertEqual(literal[0]["description"], "first\nsecond")

    def test_files_without_frontmatter_pass_through_untouched(self):
        meta, body = parse_frontmatter("# Just a document\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "# Just a document\n")

    def test_parse_skill_file_falls_back_to_the_first_body_line_for_a_description(self):
        skill = parse_skill_file("# My skill\nThis is what it does.\n", "fallback")
        self.assertEqual(skill["name"], "fallback")
        self.assertEqual(skill["description"], "This is what it does.")

    def test_discovers_skills_keeps_the_first_duplicate_and_skips_broken_files(self):
        root1 = tempfile.mkdtemp(prefix="basicagentpy-skills-a-")
        root2 = tempfile.mkdtemp(prefix="basicagentpy-skills-b-")
        self.addCleanup(shutil.rmtree, root1, ignore_errors=True)
        self.addCleanup(shutil.rmtree, root2, ignore_errors=True)
        os.makedirs(os.path.join(root1, "alpha"))
        os.makedirs(os.path.join(root1, "broken"))
        os.makedirs(os.path.join(root2, "beta"))
        os.makedirs(os.path.join(root2, "alpha"))
        with open(os.path.join(root1, "alpha", "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: alpha\ndescription: first alpha\n---\nbody alpha")
        with open(os.path.join(root2, "beta", "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: beta\ndescription: >\n  folded\n  beta\n---\nbody beta")
        with open(os.path.join(root2, "alpha", "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: alpha\ndescription: second alpha\n---\nbody")
        with open(os.path.join(root1, "broken", "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("not even close")

        skills = discover_skills([root1, root2, os.path.join(root1, "does-not-exist")])
        self.assertEqual(sorted(skills.keys()), ["alpha", "beta", "broken"])
        self.assertEqual(skills["alpha"]["description"], "first alpha")
        self.assertEqual(skills["beta"]["description"], "folded beta")
        self.assertRegex(skill_index_text(skills), r"- beta: folded beta")

    def test_load_skill_returns_the_body_and_directory_unknown_names_fail(self):
        directory = tempfile.mkdtemp(prefix="basicagentpy-skill-tool-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        os.makedirs(os.path.join(directory, "demo"))
        with open(os.path.join(directory, "demo", "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: demo\ndescription: d\n---\nDo the demo thing.")
        skills = discover_skills([directory])
        tool = load_skill_tool(skills)

        result = tool.run({"name": "demo"}, {})
        self.assertEqual(result["name"], "demo")
        self.assertEqual(result["dir"], os.path.join(directory, "demo"))
        self.assertEqual(result["instructions"], "Do the demo thing.")
        with self.assertRaisesRegex(ValueError, "Unknown skill"):
            tool.run({"name": "nope"}, {})

    def test_discovery_follows_symlinked_skill_directories(self):
        root = tempfile.mkdtemp(prefix="basicagentpy-skills-link-")
        elsewhere = tempfile.mkdtemp(prefix="basicagentpy-skills-real-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        os.makedirs(os.path.join(elsewhere, "linked-skill"))
        with open(os.path.join(elsewhere, "linked-skill", "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: linked\ndescription: via symlink\n---\nbody")
        os.symlink(os.path.join(elsewhere, "linked-skill"), os.path.join(root, "linked"))

        skills = discover_skills([root])
        self.assertEqual(skills["linked"]["description"], "via symlink")
        self.assertEqual(skills["linked"]["path"], os.path.join(root, "linked", "SKILL.md"))

    def test_the_bundled_skills_directory_contains_the_basicagentpy_self_help_skill(self):
        skills = discover_skills([BUNDLED_SKILLS_DIR])
        self.assertIn("basicagentpy", skills)
        self.assertRegex(skills["basicagentpy"]["description"], r"configuration")
        self.assertIn("example", skills)
        self.assertIn("## Where things live", skills["basicagentpy"]["body"])


if __name__ == "__main__":
    unittest.main()
