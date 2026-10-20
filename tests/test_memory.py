# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import json
import os
import shutil
import tempfile
import unittest

from basicagentpy.memory import Memory


class MemoryTests(unittest.TestCase):
    def make_memory(self):
        directory = tempfile.mkdtemp(prefix="basicagentpy-memory-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory, Memory(dir=directory)

    def test_delete_fact_removes_one_exact_key_only(self):
        directory, memory = self.make_memory()
        memory.remember("editor", "nvim")
        memory.remember("editor.theme", "dark")
        memory.remember("OS", "Linux")

        self.assertTrue(memory.delete_fact("editor"))
        self.assertFalse(memory.delete_fact("editor"))
        self.assertIn("editor.theme", memory.facts)
        self.assertEqual(len(memory.facts), 2)

        with open(os.path.join(directory, "facts.jsonl"), encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle.read().strip().split("\n")]
        self.assertEqual(sorted(entry["key"] for entry in lines), ["OS", "editor.theme"])

    def test_reload_facts_picks_up_facts_written_by_another_instance(self):
        directory, memory = self.make_memory()
        memory.remember("a", "1")

        other = Memory(dir=directory)
        other.remember("b", "2")
        self.assertNotIn("b", memory.facts)

        self.assertEqual(memory.reload_facts(), 2)
        self.assertEqual(memory.facts["a"]["value"], "1")
        self.assertEqual(memory.facts["b"]["value"], "2")

        with open(os.path.join(directory, "facts.jsonl"), "w", encoding="utf-8") as handle:
            handle.write('not json\n{"key":"c","value":"3","at":"now"}\n')
        self.assertEqual(memory.reload_facts(), 1)
        self.assertEqual(memory.facts["c"]["value"], "3")

    def test_delete_session_removes_the_file_and_keeps_the_continue_pointer_valid(self):
        directory, memory = self.make_memory()
        a = memory.start_session("20260101000000-aaaa")
        b = memory.start_session("20260102000000-bbbb")
        c = memory.start_session("20260103000000-cccc")
        memory.append_event("user", "hello from c")

        def pointer():
            with open(os.path.join(directory, "last-session.txt"), encoding="utf-8") as handle:
                return handle.read().strip()

        self.assertTrue(memory.delete_session(c))
        self.assertFalse(os.path.exists(os.path.join(directory, "sessions", f"{c}.jsonl")))
        self.assertEqual(pointer(), b)

        self.assertTrue(memory.delete_session(b))
        self.assertEqual(pointer(), a)

        self.assertTrue(memory.delete_session(a))
        self.assertFalse(os.path.exists(os.path.join(directory, "last-session.txt")))

        self.assertFalse(memory.delete_session("20260104000000-dddd"))


if __name__ == "__main__":
    unittest.main()
