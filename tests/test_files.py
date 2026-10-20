# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import json
import os
import shutil
import tempfile
import unittest

from basicagentpy.config import load_config
from basicagentpy.tools.files import check_access, file_tools


def make_sandbox():
    base = tempfile.mkdtemp(prefix="basicagentpy-files-")
    os.makedirs(os.path.join(base, "outside-dir"))
    with open(os.path.join(base, "outside-dir", "secret.txt"), "w", encoding="utf-8") as handle:
        handle.write("secret")
    for name in ("rootA", "rootB", "rootC"):
        os.makedirs(os.path.join(base, name))
    with open(os.path.join(base, "rootA", "protected.txt"), "w", encoding="utf-8") as handle:
        handle.write("protected")
    os.symlink(
        os.path.join(base, "outside-dir"), os.path.join(base, "rootA", "escape")
    )

    config_path = os.path.join(base, "config.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "tools": {
                    "files": {
                        "roots": [
                            {"path": "rootA", "access": "rw"},
                            {"path": "rootB", "access": "ro"},
                            {"path": "rootC", "access": "approval"},
                        ],
                        "protectedPaths": ["rootA/protected.txt"],
                    }
                },
                "memory": {"enabled": False},
            },
            handle,
        )
    config = load_config(flags={"config": config_path}, env={}, cwd=base)
    tools = {tool.name: tool for tool in file_tools(config=config, cwd=base)}
    return base, config, tools, config_path


def make_context(allow=False):
    approvals = []

    def approve(description):
        approvals.append(description)
        return allow

    return {"approvals": approvals, "ctx": {"signal": None, "approve": approve}}


class FileToolTests(unittest.TestCase):
    def sandbox(self):
        base, config, tools, config_path = make_sandbox()
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        return base, config, tools, config_path

    def test_write_file_and_read_file_round_trip_with_line_numbers(self):
        base, config, tools, config_path = self.sandbox()
        ctx = make_context()["ctx"]
        tools["write_file"].run({"path": "rootA/note.txt", "content": "hello\nworld\n"}, ctx)
        read = tools["read_file"].run({"path": "rootA/note.txt"}, ctx)
        self.assertEqual(read["totalLines"], 3)
        self.assertRegex(read["text"], r"    1\| hello")
        self.assertRegex(read["text"], r"    2\| world")

    def test_paths_outside_every_root_are_refused(self):
        base, config, tools, config_path = self.sandbox()
        ctx = make_context()["ctx"]
        with self.assertRaisesRegex(ValueError, "outside the configured file roots"):
            tools["write_file"].run({"path": "../outside.txt", "content": "x"}, ctx)
        with self.assertRaisesRegex(ValueError, "outside the configured file roots"):
            tools["read_file"].run({"path": "outside-dir/secret.txt"}, ctx)

    def test_symlinks_cannot_escape_a_root(self):
        base, config, tools, config_path = self.sandbox()
        ctx = make_context()["ctx"]
        with self.assertRaisesRegex(ValueError, "outside the configured file roots"):
            tools["read_file"].run({"path": "rootA/escape/secret.txt"}, ctx)
        with self.assertRaisesRegex(ValueError, "outside the configured file roots"):
            tools["write_file"].run({"path": "rootA/escape/new.txt", "content": "x"}, ctx)

    def test_dangling_symlinks_to_outside_targets_cannot_be_written_through(self):
        base, config, tools, config_path = self.sandbox()
        ctx = make_context()["ctx"]
        os.symlink(
            os.path.join(base, "outside-dir", "newfile.txt"),
            os.path.join(base, "rootA", "dangling"),
        )
        with self.assertRaisesRegex(ValueError, "outside the configured file roots"):
            tools["write_file"].run({"path": "rootA/dangling", "content": "escaped"}, ctx)
        self.assertFalse(os.path.exists(os.path.join(base, "outside-dir", "newfile.txt")))

    def test_dangling_symlinks_that_stay_inside_a_root_still_work(self):
        base, config, tools, config_path = self.sandbox()
        ctx = make_context()["ctx"]
        os.symlink(
            os.path.join(base, "rootA", "real.txt"),
            os.path.join(base, "rootA", "inside-dangling"),
        )
        tools["write_file"].run({"path": "rootA/inside-dangling", "content": "ok"}, ctx)
        with open(os.path.join(base, "rootA", "real.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "ok")

    def test_read_only_roots_allow_reads_but_refuse_writes(self):
        base, config, tools, config_path = self.sandbox()
        with open(os.path.join(base, "rootB", "readme.md"), "w", encoding="utf-8") as handle:
            handle.write("keep me")
        ctx = make_context()["ctx"]
        read = tools["read_file"].run({"path": "rootB/readme.md"}, ctx)
        self.assertIn("keep me", read["text"])
        with self.assertRaisesRegex(ValueError, "read-only"):
            tools["write_file"].run({"path": "rootB/new.txt", "content": "x"}, ctx)

    def test_approval_roots_ask_before_writing_and_honor_the_answer(self):
        base, config, tools, config_path = self.sandbox()
        denied = make_context(False)
        with self.assertRaisesRegex(ValueError, "denied by user"):
            tools["write_file"].run({"path": "rootC/denied.txt", "content": "x"}, denied["ctx"])
        self.assertEqual(len(denied["approvals"]), 1)
        self.assertFalse(os.path.exists(os.path.join(base, "rootC", "denied.txt")))

        allowed = make_context(True)
        tools["write_file"].run({"path": "rootC/allowed.txt", "content": "x"}, allowed["ctx"])
        self.assertTrue(os.path.exists(os.path.join(base, "rootC", "allowed.txt")))

    def test_protected_paths_inside_an_rw_root_also_require_approval(self):
        base, config, tools, config_path = self.sandbox()
        denied = make_context(False)
        with self.assertRaisesRegex(ValueError, "denied by user"):
            tools["write_file"].run({"path": "rootA/protected.txt", "content": "changed"}, denied["ctx"])
        with open(os.path.join(base, "rootA", "protected.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "protected")

    def test_the_active_config_file_is_protected_even_inside_an_rw_root(self):
        base, config, tools, config_path = self.sandbox()
        config["paths"]["configPath"] = os.path.join(base, "rootA", "basicagentpy.json")
        denied = make_context(False)
        with self.assertRaisesRegex(ValueError, "denied by user"):
            tools["write_file"].run({"path": "rootA/basicagentpy.json", "content": "{}"}, denied["ctx"])
        self.assertEqual(len(denied["approvals"]), 1)

    def test_edit_file_replaces_exact_text_and_protects_against_ambiguity(self):
        base, config, tools, config_path = self.sandbox()
        ctx = make_context()["ctx"]
        with open(os.path.join(base, "rootA", "edit.txt"), "w", encoding="utf-8") as handle:
            handle.write("one two one\n")

        tools["edit_file"].run({"path": "rootA/edit.txt", "old_string": "two", "new_string": "TWO"}, ctx)
        with open(os.path.join(base, "rootA", "edit.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "one TWO one\n")

        with self.assertRaisesRegex(ValueError, "matches 2 times"):
            tools["edit_file"].run({"path": "rootA/edit.txt", "old_string": "one", "new_string": "x"}, ctx)
        tools["edit_file"].run(
            {"path": "rootA/edit.txt", "old_string": "one", "new_string": "1", "replace_all": True},
            ctx,
        )
        with open(os.path.join(base, "rootA", "edit.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "1 TWO 1\n")

        with self.assertRaisesRegex(ValueError, "not found"):
            tools["edit_file"].run({"path": "rootA/edit.txt", "old_string": "missing", "new_string": "x"}, ctx)

    def test_append_file_never_creates_a_file_and_preserves_existing_bytes(self):
        base, config, tools, config_path = self.sandbox()
        ctx = make_context()["ctx"]
        with self.assertRaisesRegex(ValueError, "does not create files"):
            tools["append_file"].run({"path": "rootA/new.txt", "text": "x"}, ctx)
        with open(os.path.join(base, "rootA", "log.txt"), "w", encoding="utf-8") as handle:
            handle.write("first")
        tools["append_file"].run({"path": "rootA/log.txt", "text": " second"}, ctx)
        with open(os.path.join(base, "rootA", "log.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "first second")

    def test_rename_file_refuses_to_overwrite_and_moves_the_file(self):
        base, config, tools, config_path = self.sandbox()
        ctx = make_context()["ctx"]
        with open(os.path.join(base, "rootA", "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("data")
        with self.assertRaisesRegex(ValueError, "already exists"):
            tools["rename_file"].run({"from": "rootA/a.txt", "to": "rootA/protected.txt"}, ctx)
        tools["rename_file"].run({"from": "rootA/a.txt", "to": "rootA/b.txt"}, ctx)
        self.assertFalse(os.path.exists(os.path.join(base, "rootA", "a.txt")))
        with open(os.path.join(base, "rootA", "b.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "data")

    def test_list_dir_marks_directories_and_sorts_entries(self):
        base, config, tools, config_path = self.sandbox()
        ctx = make_context()["ctx"]
        os.makedirs(os.path.join(base, "rootA", "subdir"))
        with open(os.path.join(base, "rootA", "z.txt"), "w", encoding="utf-8") as handle:
            handle.write("")
        listing = tools["list_dir"].run({"path": "rootA"}, ctx)
        self.assertIn("subdir/", listing["entries"])
        self.assertIn("z.txt", listing["entries"])
        self.assertEqual(listing["entries"], sorted(listing["entries"]))

    def test_check_access_reports_approval_requirements_to_callers(self):
        base, config, tools, config_path = self.sandbox()
        normal = check_access(config=config, cwd=base, input="rootA/x.txt", write=True)
        self.assertFalse(normal["requiresApproval"])
        guarded = check_access(config=config, cwd=base, input="rootC/y.txt", write=True)
        self.assertTrue(guarded["requiresApproval"])

    def test_the_most_specific_matching_root_decides_the_access_level(self):
        base = tempfile.mkdtemp(prefix="basicagentpy-specific-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        os.makedirs(os.path.join(base, "wide", "narrow"))
        with open(os.path.join(base, "wide", "open.txt"), "w", encoding="utf-8") as handle:
            handle.write("open")
        config_path = os.path.join(base, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "tools": {
                        "files": {
                            "roots": [
                                {"path": "wide", "access": "rw"},
                                {"path": "wide/narrow", "access": "deny"},
                            ]
                        }
                    },
                    "memory": {"enabled": False},
                },
                handle,
            )
        config = load_config(flags={"config": config_path}, env={}, cwd=base)
        tools = {tool.name: tool for tool in file_tools(config=config, cwd=base)}
        ctx = make_context()["ctx"]

        read = tools["read_file"].run({"path": "wide/open.txt"}, ctx)
        self.assertIn("open", read["text"])
        with self.assertRaisesRegex(ValueError, "denied by policy"):
            tools["read_file"].run({"path": "wide/narrow/x.txt"}, ctx)
        with self.assertRaisesRegex(ValueError, "denied by policy"):
            tools["write_file"].run({"path": "wide/narrow/x.txt", "content": "x"}, ctx)

    def test_the_memory_data_directory_requires_approval_for_writes(self):
        base = tempfile.mkdtemp(prefix="basicagentpy-memdir-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        memory_dir = os.path.join(base, "rootA", "data")
        os.makedirs(memory_dir)
        with open(os.path.join(memory_dir, "audit.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("")
        config_path = os.path.join(base, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "tools": {"files": {"roots": [{"path": "rootA", "access": "rw"}]}},
                    "memory": {"enabled": True, "dir": memory_dir},
                },
                handle,
            )
        config = load_config(flags={"config": config_path}, env={}, cwd=base)
        tools = {tool.name: tool for tool in file_tools(config=config, cwd=base)}

        denied = make_context(False)
        with self.assertRaisesRegex(ValueError, "denied by user"):
            tools["write_file"].run({"path": "rootA/data/audit.jsonl", "content": "tampered"}, denied["ctx"])
        self.assertEqual(len(denied["approvals"]), 1)
        with open(os.path.join(memory_dir, "audit.jsonl"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "")

        allowed = make_context(True)
        tools["write_file"].run({"path": "rootA/data/facts.jsonl", "content": "ok"}, allowed["ctx"])
        with open(os.path.join(memory_dir, "facts.jsonl"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "ok")

    def test_edit_file_enforces_the_write_size_limit(self):
        base, config, tools, config_path = self.sandbox()
        config["tools"]["files"]["maxWriteBytes"] = 20
        ctx = make_context()["ctx"]
        with open(os.path.join(base, "rootA", "grow.txt"), "w", encoding="utf-8") as handle:
            handle.write("short")
        with self.assertRaisesRegex(ValueError, "write limit"):
            tools["edit_file"].run(
                {"path": "rootA/grow.txt", "old_string": "short", "new_string": "x" * 50},
                ctx,
            )
        with open(os.path.join(base, "rootA", "grow.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "short")


if __name__ == "__main__":
    unittest.main()
