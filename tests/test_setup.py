# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from basicagentpy.config import DEFAULTS
from basicagentpy.setup import (
    apply_answers,
    load_base_config,
    matching_preset,
    summary_lines,
    test_provider,
)
from tests.fixtures.fake_server import finish_chunk, start_fake_provider, text_chunk


BIN = str(Path(__file__).resolve().parents[1] / "bin" / "basicagentpy-setup")


def run_setup(args, *, cwd, input_text=""):
    return subprocess.run(
        [sys.executable, BIN, *args],
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=60,
    )


def workspace(case):
    directory = tempfile.mkdtemp(prefix="basicagentpy-setup-")
    case.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    return directory, os.path.join(directory, "config.json")


def read_config(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class SetupUnitTests(unittest.TestCase):
    def test_matching_preset_finds_presets_and_falls_back_to_custom(self):
        self.assertEqual(matching_preset("http://localhost:11434/v1"), 0)
        self.assertEqual(matching_preset("http://localhost:11434/v1/"), 0)
        self.assertEqual(matching_preset("https://api.deepseek.com/v1"), 2)
        self.assertEqual(matching_preset("http://my-server:1234/v1"), 5)

    def test_apply_answers_preserves_the_base_and_removes_cleared_keys(self):
        base = {
            "provider": {
                "baseUrl": "http://old/v1",
                "model": "old",
                "apiKey": "secret",
                "apiKeyEnv": "OLD_KEY",
                "maxTokens": 8192,
            },
            "tools": {
                "files": {"roots": [{"path": "/x", "access": "ro"}]},
                "shell": {"enabled": False},
            },
            "memory": {"enabled": True, "dir": "~/old"},
            "custom": {"keep": True},
        }
        raw = apply_answers(
            base,
            {
                "baseUrl": "https://new.example/v1/",
                "model": "new-model",
                "apiKey": None,
                "apiKeyEnv": "NEW_KEY",
                "shellEnabled": True,
                "memoryEnabled": False,
                "dataDir": "~/new",
            },
        )
        self.assertEqual(raw["provider"]["baseUrl"], "https://new.example/v1")
        self.assertEqual(raw["provider"]["model"], "new-model")
        self.assertEqual(raw["provider"]["maxTokens"], 8192)
        self.assertNotIn("apiKey", raw["provider"])
        self.assertEqual(raw["provider"]["apiKeyEnv"], "NEW_KEY")
        self.assertEqual(
            raw["tools"]["files"]["roots"], [{"path": "/x", "access": "ro"}]
        )
        self.assertTrue(raw["tools"]["shell"]["enabled"])
        self.assertFalse(raw["memory"]["enabled"])
        self.assertEqual(raw["memory"]["dir"], "~/new")
        self.assertEqual(raw["custom"], {"keep": True})
        self.assertEqual(base["provider"]["model"], "old")

    def test_apply_answers_keeps_keys_that_are_left_undefined(self):
        base = {
            "provider": {
                "baseUrl": "http://old/v1",
                "model": "old",
                "apiKey": "secret",
            }
        }
        raw = apply_answers(
            base,
            {
                "baseUrl": "http://new/v1",
                "model": "new",
                "shellEnabled": False,
                "memoryEnabled": True,
                "dataDir": "~/d",
            },
        )
        self.assertEqual(raw["provider"]["apiKey"], "secret")
        self.assertNotIn("maxTokens", raw["provider"])

    def test_load_base_config_prefers_an_existing_config_and_falls_back_to_the_example(self):
        directory, config_path = workspace(self)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"provider": {"model": "mine"}}, handle)
        found = load_base_config(config_path)
        self.assertTrue(found["found"])
        self.assertEqual(found["raw"]["provider"]["model"], "mine")

        example_path = os.path.join(directory, "example.json")
        with open(example_path, "w", encoding="utf-8") as handle:
            json.dump({"provider": {"model": "from-example"}}, handle)
        missing = load_base_config(
            os.path.join(directory, "nope.json"), example_path=example_path
        )
        self.assertFalse(missing["found"])
        self.assertEqual(missing["raw"]["provider"]["model"], "from-example")

        broken = load_base_config(
            os.path.join(directory, "nope.json"),
            example_path=os.path.join(directory, "missing.json"),
        )
        self.assertEqual(broken["raw"], {})

    def test_test_provider_reports_unreachable_providers_instead_of_throwing(self):
        class BoomProvider:
            def chat(self, **kwargs):
                raise RuntimeError("boom")

        result = test_provider(
            {"provider": {"baseUrl": "http://127.0.0.1:9/v1", "model": "none"}},
            provider_factory=lambda config: BoomProvider(),
        )
        self.assertFalse(result["ok"])
        self.assertRegex(result["message"], "boom")

    def test_summary_lines_describe_the_provider_key_source_and_memory(self):
        lines = summary_lines(
            {
                "provider": {
                    "baseUrl": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "apiKeyEnv": "DEEPSEEK_API_KEY",
                    "maxTokens": 4096,
                },
                "tools": {"shell": {"enabled": False}},
                "memory": {"enabled": True, "dir": "~/data"},
            },
            config_path="/tmp/config.json",
        )
        as_object = dict(lines)
        self.assertEqual(as_object["config file"], "/tmp/config.json")
        self.assertEqual(as_object["provider"], "https://api.deepseek.com/v1")
        self.assertEqual(as_object["api key"], "environment variable DEEPSEEK_API_KEY")
        self.assertEqual(as_object["shell tool"], "disabled")
        self.assertEqual(as_object["memory"], "enabled, ~/data")
        self.assertEqual(as_object["max tokens"], "4096")

        no_cap = dict(
            summary_lines(
                {"provider": {"baseUrl": "http://x/v1", "model": "m"}},
                config_path="/tmp/config.json",
            )
        )
        self.assertEqual(no_cap["max tokens"], "provider default")


class SetupEndToEndTests(unittest.TestCase):
    def test_enter_only_run_writes_the_example_defaults_without_a_max_tokens_cap(self):
        directory, config_path = workspace(self)
        input_text = "\n".join(["", "", "", "", "", "", "", "n", ""]) + "\n"
        result = run_setup(
            ["--config", config_path, "--no-start"], cwd=directory, input_text=input_text
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"Wrote .*config\.json")
        raw = read_config(config_path)
        self.assertEqual(raw["provider"]["baseUrl"], "http://localhost:11434/v1")
        self.assertEqual(raw["provider"]["model"], "qwen3:8b")
        self.assertEqual(raw["provider"]["apiKeyEnv"], "BASICAGENTPY_API_KEY")
        self.assertNotIn("maxTokens", raw["provider"])
        self.assertFalse(raw["tools"]["shell"]["enabled"])
        self.assertTrue(raw["memory"]["enabled"])
        self.assertEqual(raw["memory"]["dir"], "~/.local/share/basicagentpy")
        self.assertTrue(raw["tools"]["web"]["enabled"])
        self.assertEqual(stat.S_IMODE(os.stat(config_path).st_mode), 0o600)

    def test_preset_answers_inline_key_shell_and_custom_paths_are_written(self):
        directory, config_path = workspace(self)
        data_dir = os.path.join(directory, "agent-data")
        input_text = (
            "\n".join(["2", "", "", "", "sk-test-123", "y", "", data_dir, "", "n", ""])
            + "\n"
        )
        result = run_setup(
            ["--config", config_path, "--no-start"], cwd=directory, input_text=input_text
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ollama Cloud", result.stdout)
        raw = read_config(config_path)
        self.assertEqual(raw["provider"]["baseUrl"], "https://ollama.com/v1")
        self.assertEqual(raw["provider"]["model"], "gemma4:31b")
        self.assertEqual(raw["provider"]["apiKeyEnv"], "OLLAMA_API_KEY_CLOUD")
        self.assertEqual(raw["provider"]["apiKey"], "sk-test-123")
        self.assertNotIn("maxTokens", raw["provider"])
        self.assertTrue(raw["tools"]["shell"]["enabled"])
        self.assertEqual(raw["memory"]["dir"], data_dir)

    def test_an_existing_config_keeps_its_values_and_unrelated_settings(self):
        directory, config_path = workspace(self)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "provider": {
                        "baseUrl": "http://localhost:9999/v1",
                        "model": "my-model",
                        "apiKey": "sekret",
                        "apiKeyEnv": "MY_KEY",
                        "maxTokens": 8192,
                    },
                    "tools": {
                        "files": {"roots": [{"path": "/somewhere", "access": "ro"}]},
                        "shell": {"enabled": True},
                    },
                    "custom": {"keep": True},
                },
                handle,
            )
        input_text = (
            "\n".join(["", "", "", "", "", "", "", os.path.join(directory, "data"), "", ""])
            + "\n"
        )
        result = run_setup(
            ["--config", config_path, "--no-start", "--no-test"],
            cwd=directory,
            input_text=input_text,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Found an existing config", result.stdout)
        raw = read_config(config_path)
        self.assertEqual(raw["provider"]["baseUrl"], "http://localhost:9999/v1")
        self.assertEqual(raw["provider"]["model"], "my-model")
        self.assertEqual(raw["provider"]["apiKey"], "sekret")
        self.assertEqual(raw["provider"]["apiKeyEnv"], "MY_KEY")
        self.assertEqual(raw["provider"]["maxTokens"], 8192)
        self.assertEqual(
            raw["tools"]["files"]["roots"], [{"path": "/somewhere", "access": "ro"}]
        )
        self.assertTrue(raw["tools"]["shell"]["enabled"])
        self.assertEqual(raw["custom"], {"keep": True})

    def test_the_provider_test_runs_against_the_configured_provider(self):
        provider = start_fake_provider([[text_chunk("ready"), finish_chunk("stop")]])
        self.addCleanup(provider.close)
        directory, config_path = workspace(self)
        input_text = (
            "\n".join(
                [
                    "6",
                    provider.base_url,
                    "fake-model",
                    "",
                    "",
                    "",
                    "",
                    os.path.join(directory, "data"),
                    "",
                    "",
                    "",
                ]
            )
            + "\n"
        )
        result = run_setup(
            ["--config", config_path, "--no-start"],
            cwd=directory,
            input_text=input_text,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Testing the provider connection", result.stdout)
        self.assertIn('ok: fake-model replied "ready"', result.stdout)
        raw = read_config(config_path)
        self.assertEqual(raw["provider"]["baseUrl"], provider.base_url)
        self.assertEqual(raw["provider"]["model"], "fake-model")

    def test_a_failed_provider_test_can_be_skipped_and_the_config_is_still_written(self):
        directory, config_path = workspace(self)
        input_text = (
            "\n".join(
                [
                    "6",
                    "http://127.0.0.1:9/v1",
                    "nope-model",
                    "",
                    "",
                    "",
                    "",
                    os.path.join(directory, "data"),
                    "",
                    "",
                    "n",
                    "",
                ]
            )
            + "\n"
        )
        result = run_setup(
            ["--config", config_path, "--no-start"],
            cwd=directory,
            input_text=input_text,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("failed:", result.stdout)
        self.assertIn("Edit the provider settings and try again", result.stdout)
        self.assertEqual(
            read_config(config_path)["provider"]["baseUrl"],
            "http://127.0.0.1:9/v1",
        )

    def test_declining_the_write_leaves_no_file_and_starts_nothing(self):
        directory, config_path = workspace(self)
        input_text = "\n".join(["", "", "", "", "", "", "", "n", "n"]) + "\n"
        result = run_setup(
            ["--config", config_path, "--no-start"],
            cwd=directory,
            input_text=input_text,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing written", result.stdout)
        self.assertFalse(os.path.exists(config_path))

    def test_help_and_version_exit_without_prompting(self):
        directory, config_path = workspace(self)
        help_result = run_setup(["--help"], cwd=directory, input_text="")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("basicagentpy setup", help_result.stdout)
        self.assertIn("--config <file>", help_result.stdout)

        version_result = run_setup(["--version"], cwd=directory, input_text="")
        self.assertEqual(version_result.returncode, 0)
        self.assertRegex(version_result.stdout.strip(), r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
