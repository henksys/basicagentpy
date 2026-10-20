# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixtures.fake_server import (
    finish_chunk,
    start_fake_provider,
    text_chunk,
    tool_chunk,
)


BIN = str(Path(__file__).resolve().parents[1] / "bin" / "basicagentpy")


def make_workspace(server, *, files=None, **extra_config):
    directory = tempfile.mkdtemp(prefix="basicagentpy-cli-")
    config_path = os.path.join(directory, "config.json")
    raw = {
        "provider": {
            "baseUrl": server.base_url,
            "model": "fake-model",
            "apiKey": "test",
        },
        "tools": {
            "files": {
                "roots": [{"path": directory, "access": "rw"}],
                **(files or {}),
            }
        },
        "skills": {"paths": []},
        "memory": {"enabled": False},
    }
    raw.update(extra_config)
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle)
    return directory, config_path


def run_cli(args, *, cwd):
    return subprocess.run(
        [sys.executable, BIN, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def parse_jsonl(text):
    return [json.loads(line) for line in text.split("\n") if line]


class CliTests(unittest.TestCase):
    def provider(self, script):
        server = start_fake_provider(script)
        self.addCleanup(server.close)
        return server

    def workspace(self, server, **kwargs):
        directory, config_path = make_workspace(server, **kwargs)
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory, config_path

    def test_one_shot_json_runs_a_tool_call_and_reports_done(self):
        server = self.provider(
            [
                [
                    tool_chunk(
                        index=0,
                        id="c1",
                        name="write_file",
                        args='{"path":"hello.txt","content":"hi from agent"}',
                    ),
                    finish_chunk("tool_calls"),
                ],
                [text_chunk("Created hello.txt."), finish_chunk("stop")],
            ]
        )
        directory, config_path = self.workspace(server)

        result = run_cli(
            ["--config", config_path, "--json", "create hello.txt"], cwd=directory
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        events = parse_jsonl(result.stdout)
        tool_end = next(event for event in events if event["type"] == "tool_end")
        self.assertTrue(tool_end["ok"], events)
        self.assertEqual(
            next(event for event in events if event["type"] == "done")["text"],
            "Created hello.txt.",
        )
        with open(os.path.join(directory, "hello.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hi from agent")

        names = [tool["function"]["name"] for tool in server.requests[0]["tools"]]
        self.assertIn("write_file", names)
        self.assertIn("fetch_url", names)
        self.assertEqual(server.requests[1]["messages"][-1]["role"], "tool")

    def test_one_shot_denies_approval_required_actions_without_yes(self):
        server = self.provider(
            [
                [
                    tool_chunk(
                        index=0,
                        id="c1",
                        name="write_file",
                        args='{"path":"protected.txt","content":"nope"}',
                    ),
                    finish_chunk("tool_calls"),
                ],
                [text_chunk("I could not write it."), finish_chunk("stop")],
            ]
        )
        directory, config_path = self.workspace(
            server, files={"protectedPaths": ["protected.txt"]}
        )

        result = run_cli(
            ["--config", config_path, "--json", "write protected.txt"], cwd=directory
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        events = parse_jsonl(result.stdout)
        approval = next(event for event in events if event["type"] == "approval")
        self.assertFalse(approval["approved"])
        self.assertFalse(
            next(event for event in events if event["type"] == "tool_end")["ok"]
        )
        self.assertFalse(os.path.exists(os.path.join(directory, "protected.txt")))
        self.assertIn("--yes", result.stderr)

    def test_yes_approves_guarded_actions(self):
        server = self.provider(
            [
                [
                    tool_chunk(
                        index=0,
                        id="c1",
                        name="write_file",
                        args='{"path":"protected.txt","content":"yes"}',
                    ),
                    finish_chunk("tool_calls"),
                ],
                [text_chunk("Written."), finish_chunk("stop")],
            ]
        )
        directory, config_path = self.workspace(
            server, files={"protectedPaths": ["protected.txt"]}
        )
        result = run_cli(
            ["--config", config_path, "--json", "--yes", "write protected.txt"],
            cwd=directory,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(directory, "protected.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "yes")

    def test_a_repeated_failing_tool_call_stops_the_run_with_exit_code_1(self):
        server = self.provider(
            [
                [
                    tool_chunk(
                        index=0,
                        id="c1",
                        name="write_file",
                        args='{"path":"../escape.txt","content":"x"}',
                    ),
                    finish_chunk("tool_calls"),
                ]
            ]
        )
        directory, config_path = self.workspace(server)
        result = run_cli(
            ["--config", config_path, "--json", "escape"], cwd=directory
        )
        self.assertEqual(result.returncode, 1)
        events = parse_jsonl(result.stdout)
        self.assertEqual(
            len([event for event in events if event["type"] == "tool_end"]), 3
        )
        self.assertRegex(
            next(event for event in events if event["type"] == "error")["message"],
            "3 times",
        )
        self.assertEqual(len(server.requests), 3)

    def test_human_one_shot_keeps_tool_traces_off_stdout(self):
        server = self.provider(
            [
                [
                    tool_chunk(
                        index=0,
                        id="c1",
                        name="write_file",
                        args='{"path":"quiet.txt","content":"shh"}',
                    ),
                    finish_chunk("tool_calls"),
                ],
                [text_chunk("Done quietly."), finish_chunk("stop")],
            ]
        )
        directory, config_path = self.workspace(server)
        result = run_cli(["--config", config_path, "write quietly"], cwd=directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Done quietly.")
        self.assertIn("[tool] write_file", result.stderr)

    def test_base_url_and_model_flags_override_the_config_file(self):
        server = self.provider([[text_chunk("Overridden."), finish_chunk("stop")]])
        directory, config_path = self.workspace(server)
        result = run_cli(
            [
                "--config",
                config_path,
                "--json",
                "--base-url",
                server.base_url,
                "--model",
                "from-flag",
                "hello",
            ],
            cwd=directory,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        events = parse_jsonl(result.stdout)
        self.assertEqual(
            next(event for event in events if event["type"] == "done")["text"],
            "Overridden.",
        )
        self.assertEqual(server.requests[0]["model"], "from-flag")

    def test_no_web_removes_fetch_url_from_the_tool_schema(self):
        server = self.provider([[text_chunk("No web here."), finish_chunk("stop")]])
        directory, config_path = self.workspace(server)
        result = run_cli(
            ["--config", config_path, "--json", "--no-web", "hello"], cwd=directory
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [tool["function"]["name"] for tool in server.requests[0]["tools"]]
        self.assertNotIn("fetch_url", names)
        self.assertIn("read_file", names)

    def test_data_dir_puts_sessions_and_the_audit_log_there(self):
        server = self.provider(
            [
                [
                    tool_chunk(
                        index=0,
                        id="c1",
                        name="write_file",
                        args='{"path":"hello.txt","content":"hi"}',
                    ),
                    finish_chunk("tool_calls"),
                ],
                [text_chunk("Done."), finish_chunk("stop")],
            ]
        )
        directory, config_path = self.workspace(server, memory={"enabled": True})
        data_dir = os.path.join(directory, "agent-data")
        result = run_cli(
            [
                "--config",
                config_path,
                "--data-dir",
                data_dir,
                "--json",
                "hello",
            ],
            cwd=directory,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(os.path.join(data_dir, "last-session.txt")))
        self.assertTrue(os.path.exists(os.path.join(data_dir, "audit.jsonl")))
        sessions = [
            name
            for name in os.listdir(os.path.join(data_dir, "sessions"))
            if name.endswith(".jsonl")
        ]
        self.assertEqual(len(sessions), 1)


if __name__ == "__main__":
    unittest.main()
