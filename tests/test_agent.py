# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import copy
import json
import unittest

from basicagentpy.agent import (
    Agent,
    AgentError,
    context_size,
    parse_tool_arguments,
    trim_history,
)


class FakeProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.model = "fake"

    def chat(self, *, messages, on_delta=None, tools=None, signal=None):
        self.calls.append(copy.deepcopy(messages))
        reply = (
            self.replies.pop(0)
            if self.replies
            else {"content": "(no more replies)", "toolCalls": [], "finishReason": "stop"}
        )
        if reply.get("content") and on_delta:
            on_delta(reply["content"])
        return reply


def tool_call(name, args, id="call_1"):
    return {
        "id": id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def make_config(overrides=None):
    config = {
        "agent": {
            "maxIterations": 25,
            "contextChars": 100000,
            "maxResultChars": 24000,
        }
    }
    config.update(overrides or {})
    return config


def make_registry(execute=None):
    calls = []

    class FakeRegistry:
        def schemas(self):
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "fake_tool",
                        "description": "",
                        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                }
            ]

        def names(self):
            return ["fake_tool"]

        def execute(self, name, args, ctx):
            calls.append({"name": name, "args": args, "hasApprove": callable(ctx.get("approve"))})
            if execute is not None:
                return execute(name, args, ctx)
            return {"ok": True}

    registry = FakeRegistry()
    registry.calls = calls
    return registry


class AgentTests(unittest.TestCase):
    def test_runs_a_tool_call_and_returns_the_final_answer(self):
        provider = FakeProvider(
            [
                {"content": "", "toolCalls": [tool_call("fake_tool", {"x": 1})], "finishReason": "tool_calls"},
                {"content": "All done.", "toolCalls": [], "finishReason": "stop"},
            ]
        )
        registry = make_registry()
        events = []
        agent = Agent(
            provider=provider,
            registry=registry,
            config=make_config(),
            emit=events.append,
            approve=lambda description: True,
        )
        agent.set_system("system prompt")

        result = agent.run("do the thing")

        self.assertEqual(result["text"], "All done.")
        self.assertEqual(len(registry.calls), 1)
        self.assertEqual(registry.calls[0]["args"], {"x": 1})
        second_call = provider.calls[1]
        tool_message = next(message for message in second_call if message["role"] == "tool")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertTrue(json.loads(tool_message["content"])["ok"])
        self.assertTrue(any(event["type"] == "tool_end" and event["ok"] is True for event in events))
        self.assertTrue(any(event["type"] == "done" and event["text"] == "All done." for event in events))

    def test_tool_errors_are_returned_to_the_model_not_thrown(self):
        provider = FakeProvider(
            [
                {"content": "", "toolCalls": [tool_call("fake_tool", {})], "finishReason": "tool_calls"},
                {"content": "Recovered.", "toolCalls": [], "finishReason": "stop"},
            ]
        )

        def execute(name, args, ctx):
            raise RuntimeError("boom")

        registry = make_registry(execute)
        agent = Agent(provider=provider, registry=registry, config=make_config())
        agent.set_system("system")

        result = agent.run("go")
        self.assertEqual(result["text"], "Recovered.")
        tool_message = next(message for message in provider.calls[1] if message["role"] == "tool")
        self.assertIn("boom", tool_message["content"])

    def test_malformed_tool_arguments_produce_an_error_result_instead_of_crashing(self):
        provider = FakeProvider(
            [
                {
                    "content": "",
                    "toolCalls": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "fake_tool", "arguments": "not-json{"},
                        }
                    ],
                    "finishReason": "tool_calls",
                },
                {"content": "Fixed.", "toolCalls": [], "finishReason": "stop"},
            ]
        )
        registry = make_registry()
        agent = Agent(provider=provider, registry=registry, config=make_config())
        agent.set_system("system")
        result = agent.run("go")
        self.assertEqual(result["text"], "Fixed.")
        self.assertEqual(len(registry.calls), 0)
        tool_message = next(message for message in provider.calls[1] if message["role"] == "tool")
        self.assertIn("not valid JSON", tool_message["content"])

    def test_the_same_failing_call_three_times_trips_the_circuit_breaker(self):
        provider = FakeProvider(
            [
                {"content": "", "toolCalls": [tool_call("fake_tool", {"same": True})], "finishReason": "tool_calls"}
                for _ in range(10)
            ]
        )

        def execute(name, args, ctx):
            raise RuntimeError("always fails")

        registry = make_registry(execute)
        agent = Agent(provider=provider, registry=registry, config=make_config())
        agent.set_system("system")

        with self.assertRaises(AgentError) as raised:
            agent.run("go")
        self.assertIn("3 times", str(raised.exception))
        self.assertEqual(len(registry.calls), 3)

    def test_max_iterations_stops_a_runaway_loop(self):
        provider = FakeProvider(
            [
                {"content": "", "toolCalls": [tool_call("fake_tool", {"n": index})], "finishReason": "tool_calls"}
                for index in range(10)
            ]
        )
        registry = make_registry()
        agent = Agent(
            provider=provider,
            registry=registry,
            config=make_config(
                {"agent": {"maxIterations": 2, "contextChars": 100000, "maxResultChars": 24000}}
            ),
        )
        agent.set_system("system")
        with self.assertRaisesRegex(AgentError, "maximum of 2"):
            agent.run("go")

    def test_parse_tool_arguments_accepts_objects_empty_input_and_rejects_garbage(self):
        self.assertEqual(parse_tool_arguments('{"a":1}'), {"a": 1})
        self.assertEqual(parse_tool_arguments(""), {})
        self.assertEqual(parse_tool_arguments(None), {})
        self.assertEqual(parse_tool_arguments({"a": 1}), {"a": 1})
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            parse_tool_arguments("[")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_tool_arguments("[1]")

    def test_trim_history_drops_old_tool_results_before_old_messages(self):
        messages = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("fake_tool", {})]},
            {"role": "tool", "tool_call_id": "call_1", "content": "x" * 2000},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second question"},
        ]
        trimmed = trim_history(messages, 500)
        tool_message = next(message for message in trimmed if message["role"] == "tool")
        self.assertEqual(tool_message["content"], "[tool result trimmed to save context]")
        self.assertEqual(len(trimmed), len(messages))
        self.assertEqual(trimmed[-1]["content"], "second question")

    def test_context_size_is_the_trim_metric_and_every_iteration_reports_it(self):
        provider = FakeProvider(
            [
                {"content": "", "toolCalls": [tool_call("fake_tool", {"x": 1})], "finishReason": "tool_calls"},
                {"content": "Done.", "toolCalls": [], "finishReason": "stop"},
            ]
        )
        registry = make_registry()
        events = []
        agent = Agent(
            provider=provider,
            registry=registry,
            config=make_config(),
            emit=events.append,
            approve=lambda description: True,
        )
        agent.set_system("system")
        agent.run("hello")

        message = {"role": "user", "content": "a"}
        self.assertEqual(context_size([message]), len(json.dumps(message, separators=(",", ":"))))

        iterations = [event for event in events if event["type"] == "iteration"]
        self.assertEqual(len(iterations), 2)
        self.assertIsInstance(iterations[0]["contextSize"], int)
        self.assertGreater(iterations[0]["contextSize"], 0)
        self.assertGreater(iterations[1]["contextSize"], iterations[0]["contextSize"])


if __name__ == "__main__":
    unittest.main()
