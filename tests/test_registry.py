# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import json
import os
import shutil
import tempfile
import unittest

from basicagentpy.config import load_config
from basicagentpy.tools import create_registry


class RegistryTests(unittest.TestCase):
    def setup_registry(self, *, break_audit=False):
        base = tempfile.mkdtemp(prefix="basicagentpy-registry-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        os.makedirs(os.path.join(base, "root"))
        memory_dir = os.path.join(base, "data")
        if break_audit:
            with open(memory_dir, "w", encoding="utf-8") as handle:
                handle.write("not a directory")
        config_path = os.path.join(base, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "tools": {
                        "files": {"roots": [{"path": "root", "access": "approval"}]},
                        "shell": {"enabled": False},
                    },
                    "memory": {"enabled": True, "dir": memory_dir},
                },
                handle,
            )
        config = load_config(flags={"config": config_path}, env={}, cwd=base)
        notices = []
        registry = create_registry(
            config=config,
            memory={"dir": memory_dir},
            skills={},
            cwd=base,
            on_audit_error=notices.append,
        )
        return base, registry, memory_dir, notices

    def read_audit(self, memory_dir):
        with open(os.path.join(memory_dir, "audit.jsonl"), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle.read().strip().split("\n")]

    def test_audit_entries_record_approval_outcomes(self):
        base, registry, memory_dir, notices = self.setup_registry()
        registry.execute(
            "write_file", {"path": "root/a.txt", "content": "x"}, {"approve": lambda d: True}
        )
        entries = self.read_audit(memory_dir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "write_file")
        self.assertTrue(entries[0]["ok"])
        self.assertEqual(len(entries[0]["approvals"]), 1)
        self.assertTrue(entries[0]["approvals"][0]["approved"])
        self.assertRegex(entries[0]["approvals"][0]["description"], r"Write to")

    def test_denied_approvals_are_recorded_and_the_tool_fails(self):
        base, registry, memory_dir, notices = self.setup_registry()
        with self.assertRaisesRegex(ValueError, "denied by user"):
            registry.execute(
                "write_file",
                {"path": "root/a.txt", "content": "x"},
                {"approve": lambda d: False},
            )
        entries = self.read_audit(memory_dir)
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["ok"])
        self.assertFalse(entries[0]["approvals"][0]["approved"])

    def test_a_broken_audit_log_is_reported_once_without_failing_tool_calls(self):
        base, registry, memory_dir, notices = self.setup_registry(break_audit=True)
        first = registry.execute(
            "write_file", {"path": "root/a.txt", "content": "x"}, {"approve": lambda d: True}
        )
        self.assertTrue(first["written"])
        second = registry.execute(
            "write_file", {"path": "root/b.txt", "content": "y"}, {"approve": lambda d: True}
        )
        self.assertTrue(second["written"])
        self.assertEqual(len(notices), 1)
        self.assertRegex(notices[0], r"audit log")


if __name__ == "__main__":
    unittest.main()
