# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import unittest

from basicagentpy.provider import Provider, ProviderError
from tests.fixtures.fake_server import (
    finish_chunk,
    start_fake_provider,
    text_chunk,
    tool_chunk,
)


def provider_for(server, overrides=None):
    config = {
        "provider": {
            "baseUrl": server.base_url,
            "apiKey": "test-key",
            "model": "fake-model",
            "temperature": 0.2,
            "maxTokens": 100,
            "timeoutMs": 5000,
            "toolChoice": None,
            "includeUsage": False,
        }
    }
    config["provider"].update(overrides or {})
    return Provider(config)


class ProviderTests(unittest.TestCase):
    def test_streams_text_and_rebuilds_a_tool_call_split_across_chunks(self):
        server = start_fake_provider(
            [
                [
                    text_chunk("Hello "),
                    text_chunk("world"),
                    tool_chunk(index=0, id="call_1", name="read"),
                    tool_chunk(index=0, name="_file"),
                    tool_chunk(index=0, name="_file"),
                    tool_chunk(index=0, args='{"path":'),
                    tool_chunk(index=0, args='"a.txt"}'),
                    finish_chunk("tool_calls"),
                ]
            ]
        )
        self.addCleanup(server.close)

        deltas = []
        result = provider_for(server).chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {}},
                }
            ],
            on_delta=deltas.append,
        )

        self.assertEqual(result["content"], "Hello world")
        self.assertEqual(deltas, ["Hello ", "world"])
        self.assertEqual(len(result["toolCalls"]), 1)
        self.assertEqual(result["toolCalls"][0]["id"], "call_1")
        self.assertEqual(result["toolCalls"][0]["function"]["name"], "read_file")
        self.assertEqual(
            result["toolCalls"][0]["function"]["arguments"], '{"path":"a.txt"}'
        )
        self.assertEqual(result["finishReason"], "tool_calls")
        self.assertEqual(server.requests[0]["model"], "fake-model")
        self.assertTrue(server.requests[0].get("tools"))

    def test_max_tokens_is_only_sent_when_provider_max_tokens_is_set(self):
        server = start_fake_provider(
            [
                [text_chunk("ok"), finish_chunk("stop")],
                [text_chunk("ok"), finish_chunk("stop")],
            ]
        )
        self.addCleanup(server.close)

        provider_for(server, {"maxTokens": 8192}).chat(
            messages=[{"role": "user", "content": "hi"}], tools=[]
        )
        self.assertEqual(server.requests[0]["max_tokens"], 8192)

        provider_for(server, {"maxTokens": None}).chat(
            messages=[{"role": "user", "content": "hi"}], tools=[]
        )
        self.assertNotIn("max_tokens", server.requests[1])

    def test_tool_arguments_arriving_as_an_object_are_stringified(self):
        server = start_fake_provider(
            [
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "x",
                                            "function": {
                                                "name": "list_dir",
                                                "arguments": {"path": "."},
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    },
                    finish_chunk("tool_calls"),
                ]
            ]
        )
        self.addCleanup(server.close)
        result = provider_for(server).chat(
            messages=[{"role": "user", "content": "hi"}], tools=[]
        )
        self.assertEqual(
            result["toolCalls"][0]["function"]["arguments"], '{"path":"."}'
        )

    def test_missing_tool_call_id_is_generated(self):
        server = start_fake_provider(
            [[tool_chunk(index=0, name="list_dir", args="{}"), finish_chunk("tool_calls")]]
        )
        self.addCleanup(server.close)
        result = provider_for(server).chat(
            messages=[{"role": "user", "content": "hi"}], tools=[]
        )
        self.assertRegex(result["toolCalls"][0]["id"], r"^call_")

    def test_http_errors_surface_as_provider_error_with_status(self):
        server = start_fake_provider(
            [{"status": 401, "body": {"error": {"message": "bad key"}}}]
        )
        self.addCleanup(server.close)
        with self.assertRaises(ProviderError) as raised:
            provider_for(server).chat(
                messages=[{"role": "user", "content": "hi"}], tools=[]
            )
        self.assertIn("401", str(raised.exception))
        self.assertIn("bad key", str(raised.exception))

    def test_connection_failures_surface_as_provider_error(self):
        provider = Provider(
            {
                "provider": {
                    "baseUrl": "http://127.0.0.1:1",
                    "apiKey": "",
                    "model": "m",
                    "temperature": 0,
                    "maxTokens": 10,
                    "timeoutMs": 1000,
                    "toolChoice": None,
                    "includeUsage": False,
                }
            }
        )
        with self.assertRaises(ProviderError):
            provider.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    def test_an_event_without_a_trailing_newline_is_still_parsed(self):
        server = start_fake_provider(
            [
                [
                    'data: {"choices":[{"index":0,"delta":{"content":"tail"},"finish_reason":"stop"}]}'
                ]
            ]
        )
        self.addCleanup(server.close)
        result = provider_for(server).chat(
            messages=[{"role": "user", "content": "hi"}], tools=[]
        )
        self.assertEqual(result["content"], "tail")


if __name__ == "__main__":
    unittest.main()
