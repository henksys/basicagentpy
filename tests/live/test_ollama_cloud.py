# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Live integration test against Ollama Cloud's OpenAI-compatible endpoint.
#
# Skipped automatically unless OLLAMA_API_KEY_CLOUD is set. It makes a small
# number of billed calls, so keep it focused:
#
#   python -m unittest tests.live.test_ollama_cloud -v
#
# Model and endpoint can be overridden with LIVE_MODEL / LIVE_BASE_URL.

import os
import shutil
import tempfile
import unittest

from basicagentpy.agent import Agent
from basicagentpy.config import load_config
from basicagentpy.provider import Provider
from basicagentpy.tools import create_registry

API_KEY = os.environ.get("OLLAMA_API_KEY_CLOUD", "")
LIVE_ENABLED = bool(API_KEY) and os.environ.get("BASICAGENTPY_LIVE") == "1"


@unittest.skipUnless(
    LIVE_ENABLED,
    "set BASICAGENTPY_LIVE=1 and OLLAMA_API_KEY_CLOUD to run the live test",
)
class LiveOllamaCloudTests(unittest.TestCase):
    def test_live_gemma4_uses_write_file_and_read_file_end_to_end(self):
        directory = tempfile.mkdtemp(prefix="basicagentpy-live-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)

        base_url = os.environ.get("LIVE_BASE_URL", "https://ollama.com/v1")
        model = os.environ.get("LIVE_MODEL", "gemma4:31b")
        config = load_config(
            env={
                "BASICAGENTPY_BASE_URL": base_url,
                "BASICAGENTPY_MODEL": model,
                "BASICAGENTPY_API_KEY": API_KEY,
            },
            cwd=directory,
        )
        config["tools"]["files"]["roots"] = [{"path": directory, "access": "rw"}]

        provider = Provider(config)
        registry = create_registry(
            config=config, memory=None, skills={}, cwd=directory
        )
        agent = Agent(
            provider=provider,
            registry=registry,
            config=config,
            emit=lambda event: (
                print(f"tool: {event['name']} {str(event['args'])[:120]}")
                if event["type"] == "tool_start"
                else (
                    print(f"error: {event['message']}")
                    if event["type"] == "error"
                    else None
                )
            ),
            approve=lambda description: False,
        )
        agent.set_system("You are basicagentpy. Use the provided tools. Be concise.")

        agent.run(
            "Use the write_file tool to create a file named hello.txt in the working directory. "
            "The content must be exactly: hello from basicagentpy. Then reply with one short sentence."
        )
        with open(os.path.join(directory, "hello.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello from basicagentpy")

        second = agent.run(
            "Use read_file to read hello.txt and tell me its exact content."
        )
        self.assertIn("hello from basicagentpy", second["text"])


if __name__ == "__main__":
    unittest.main()
