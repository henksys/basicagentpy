# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import json
import os
import tempfile
import unittest

from basicagentpy.config import (
    BUNDLED_SKILLS_DIR,
    deep_merge,
    default_config_path,
    default_data_dir,
    load_config,
)


class ConfigTests(unittest.TestCase):
    def temp_dir(self):
        directory = tempfile.mkdtemp(prefix="basicagentpy-config-")
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory

    def test_defaults_load_when_no_config_file_exists(self):
        directory = self.temp_dir()
        config = load_config(
            env={
                "XDG_CONFIG_HOME": os.path.join(directory, ".config"),
                "XDG_DATA_HOME": os.path.join(directory, "data"),
            },
            cwd=directory,
        )
        self.assertEqual(config["provider"]["baseUrl"], "http://localhost:11434/v1")
        self.assertIsNone(config["provider"]["maxTokens"])
        self.assertEqual(len(config["tools"]["files"]["roots"]), 1)
        self.assertEqual(config["tools"]["files"]["roots"][0]["path"], directory)
        self.assertFalse(config["paths"]["configFound"])
        self.assertEqual(
            config["paths"]["configPath"],
            os.path.join(directory, ".config", "basicagentpy", "config.json"),
        )

    def test_config_file_values_merge_over_defaults(self):
        directory = self.temp_dir()
        config_path = os.path.join(directory, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "provider": {
                        "model": "gemma4:31b",
                        "baseUrl": "https://ollama.com/v1",
                    },
                    "tools": {
                        "files": {"roots": [{"path": directory, "access": "rw"}]}
                    },
                },
                handle,
            )
        config = load_config(flags={"config": config_path}, env={}, cwd=directory)
        self.assertEqual(config["provider"]["model"], "gemma4:31b")
        self.assertEqual(config["provider"]["baseUrl"], "https://ollama.com/v1")
        self.assertEqual(config["provider"]["temperature"], 0.2)
        self.assertTrue(config["paths"]["configFound"])

    def test_precedence_flags_beat_env_beat_file(self):
        directory = self.temp_dir()
        config_path = os.path.join(directory, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"provider": {"model": "from-file"}}, handle)
        config = load_config(
            flags={"config": config_path, "model": "from-flag"},
            env={"BASICAGENTPY_MODEL": "from-env"},
            cwd=directory,
        )
        self.assertEqual(config["provider"]["model"], "from-flag")
        config2 = load_config(
            flags={"config": config_path},
            env={"BASICAGENTPY_MODEL": "from-env"},
            cwd=directory,
        )
        self.assertEqual(config2["provider"]["model"], "from-env")

    def test_api_key_comes_from_the_named_environment_variable(self):
        directory = self.temp_dir()
        config_path = os.path.join(directory, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"provider": {"apiKeyEnv": "OLLAMA_API_KEY_CLOUD"}}, handle)
        config = load_config(
            flags={"config": config_path},
            env={"OLLAMA_API_KEY_CLOUD": "secret-value"},
            cwd=directory,
        )
        self.assertEqual(config["provider"]["apiKey"], "secret-value")

    def test_explicit_missing_config_file_is_an_error(self):
        directory = self.temp_dir()
        with self.assertRaisesRegex(ValueError, "not found"):
            load_config(
                flags={"config": os.path.join(directory, "nope.json")},
                env={},
                cwd=directory,
            )

    def test_invalid_values_are_rejected_with_clear_errors(self):
        directory = self.temp_dir()
        config_path = os.path.join(directory, "config.json")

        def write(body):
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(body if isinstance(body, str) else json.dumps(body))

        write({"tools": {"files": {"roots": [{"path": directory, "access": "banana"}]}}})
        with self.assertRaisesRegex(ValueError, "access"):
            load_config(flags={"config": config_path}, env={}, cwd=directory)
        write({"agent": {"maxIterations": 0}})
        with self.assertRaisesRegex(ValueError, "maxIterations"):
            load_config(flags={"config": config_path}, env={}, cwd=directory)
        write({"provider": {"maxTokens": 0}})
        with self.assertRaisesRegex(ValueError, "maxTokens"):
            load_config(flags={"config": config_path}, env={}, cwd=directory)
        write("not json")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            load_config(flags={"config": config_path}, env={}, cwd=directory)

    def test_deep_merge_merges_objects_and_replaces_arrays(self):
        self.assertEqual(
            deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}}),
            {"a": {"b": 1, "c": 3}},
        )
        self.assertEqual(deep_merge({"a": [1, 2]}, {"a": [3]}), {"a": [3]})

    def test_default_config_path_honors_xdg_config_home(self):
        self.assertEqual(
            default_config_path({"XDG_CONFIG_HOME": "/x"}),
            os.path.join("/x", "basicagentpy", "config.json"),
        )

    def test_default_data_dir_honors_xdg_data_home(self):
        self.assertEqual(
            default_data_dir({"XDG_DATA_HOME": "/x"}),
            os.path.join("/x", "basicagentpy"),
        )

    def test_the_data_directory_follows_flag_env_file_xdg_default(self):
        directory = self.temp_dir()
        config_path = os.path.join(directory, "config.json")
        env = {
            "XDG_CONFIG_HOME": os.path.join(directory, ".config"),
            "XDG_DATA_HOME": os.path.join(directory, "xdg-data"),
        }

        config = load_config(env=env, cwd=directory)
        self.assertEqual(
            config["memory"]["dir"], os.path.join(directory, "xdg-data", "basicagentpy")
        )

        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"memory": {"dir": "agent-data"}}, handle)
        config = load_config(flags={"config": config_path}, env=env, cwd=directory)
        self.assertEqual(
            config["memory"]["dir"], os.path.join(directory, "agent-data")
        )

        config = load_config(
            flags={"config": config_path},
            env={**env, "BASICAGENTPY_DATA_DIR": os.path.join(directory, "env-data")},
            cwd=directory,
        )
        self.assertEqual(
            config["memory"]["dir"], os.path.join(directory, "env-data")
        )

        config = load_config(
            flags={
                "config": config_path,
                "dataDir": os.path.join(directory, "flag-data"),
            },
            env={**env, "BASICAGENTPY_DATA_DIR": os.path.join(directory, "env-data")},
            cwd=directory,
        )
        self.assertEqual(
            config["memory"]["dir"], os.path.join(directory, "flag-data")
        )

    def test_invalid_memory_dir_values_are_rejected(self):
        directory = self.temp_dir()
        config_path = os.path.join(directory, "config.json")
        for body in ({"memory": {"dir": ""}}, {"memory": {"dir": 5}}, {"memory": {"dir": None}}):
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(body, handle)
            with self.assertRaisesRegex(ValueError, r"memory\.dir"):
                load_config(flags={"config": config_path}, env={}, cwd=directory)
        with self.assertRaisesRegex(ValueError, r"memory\.dir"):
            load_config(
                flags={"config": config_path, "dataDir": ""}, env={}, cwd=directory
            )

    def test_the_bundled_skills_directory_is_always_included_after_user_paths(self):
        directory = self.temp_dir()
        config_path = os.path.join(directory, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"skills": {"paths": ["./custom-skills"]}}, handle)
        config = load_config(flags={"config": config_path}, env={}, cwd=directory)
        self.assertEqual(
            config["skills"]["paths"][0], os.path.join(directory, "custom-skills")
        )
        self.assertEqual(config["skills"]["paths"][-1], BUNDLED_SKILLS_DIR)
        self.assertEqual(
            config["skills"]["paths"].count(BUNDLED_SKILLS_DIR), 1
        )

    def test_the_bundled_skills_directory_is_present_with_default_paths(self):
        directory = self.temp_dir()
        config = load_config(
            env={
                "XDG_CONFIG_HOME": os.path.join(directory, ".config"),
                "XDG_DATA_HOME": os.path.join(directory, "data"),
            },
            cwd=directory,
        )
        self.assertIn(BUNDLED_SKILLS_DIR, config["skills"]["paths"])

    def test_tools_web_is_validated(self):
        directory = self.temp_dir()
        config_path = os.path.join(directory, "config.json")
        cases = [
            ({"tools": {"web": {"approval": "sometimes"}}}, r"tools\.web\.approval"),
            ({"tools": {"web": {"maxBytes": 0}}}, r"tools\.web\.maxBytes"),
            ({"tools": {"web": {"maxRedirects": -1}}}, r"tools\.web\.maxRedirects"),
            ({"tools": {"web": {"allowPrivate": "yes"}}}, r"tools\.web\.allowPrivate"),
            ({"tools": {"web": {"denyDomains": "example.com"}}}, r"tools\.web\.denyDomains"),
            ({"tools": {"web": {"userAgent": 5}}}, r"tools\.web\.userAgent"),
        ]
        for body, pattern in cases:
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(body, handle)
            with self.assertRaisesRegex(ValueError, pattern):
                load_config(flags={"config": config_path}, env={}, cwd=directory)

    def test_the_no_web_flag_disables_the_web_tool_for_one_run(self):
        directory = self.temp_dir()
        config_path = os.path.join(directory, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"tools": {"web": {"enabled": True}}}, handle)
        config = load_config(
            flags={"config": config_path, "noWeb": True}, env={}, cwd=directory
        )
        self.assertFalse(config["tools"]["web"]["enabled"])


if __name__ == "__main__":
    unittest.main()
