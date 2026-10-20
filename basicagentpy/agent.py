# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# The agent loop.
#
# One turn works like this:
#
#   1. send the conversation + tool schemas to the provider
#   2. stream the assistant text; if the model asked for tools, run them
#   3. append each tool result as a `role: "tool"` message
#   4. repeat until the model answers without tool calls
#
# Safety rails in the loop itself:
#   - maxIterations caps how many model/tool rounds one turn may use
#   - the same tool + arguments failing three times stops the turn
#   - the context is trimmed when it grows past agent.contextChars
#   - Ctrl+C aborts the whole turn via AbortToken
#
# The loop only knows about an abstract event stream, which the CLI renders
# either as human text or as JSON lines:
#
#   { type: "iteration", iteration }
#   { type: "assistant_delta", text }
#   { type: "tool_start", name, args }
#   { type: "tool_end", name, ok, durationMs, result }
#   { type: "warning", message }
#   { type: "done", text }
#   { type: "error", message }

from __future__ import annotations

import json
import time
from typing import Any, Callable

from .abort import AbortError, AbortToken
from .util import bounded, compact_json, js_json_len


class AgentError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.name = "AgentError"


TRIMMED_TOOL_RESULT = "[tool result trimmed to save context]"


def context_size(messages: list[dict[str, Any]]) -> int:
    """Sum of the serialized message sizes.

    This is the metric trim_history uses to decide when to drop context, and
    the one the web interface shows as its context gauge, so both always agree.
    """
    return sum(js_json_len(message) for message in messages)


def trim_history(
    messages: list[dict[str, Any]], max_chars: int
) -> list[dict[str, Any]]:
    """Drop old context when the message history gets too large.

    Old tool results are replaced by a placeholder first; after that the oldest
    exchanges are removed, and tool messages always go together with their
    assistant message so the provider never sees an orphaned tool result.
    """
    if max_chars <= 0:
        return messages
    out = list(messages)
    while len(out) > 2 and context_size(out) > max_chars:
        tool_index = next(
            (
                index
                for index, message in enumerate(out)
                if message.get("role") == "tool"
                and message.get("content") != TRIMMED_TOOL_RESULT
            ),
            -1,
        )
        if tool_index >= 0:
            out[tool_index] = {**out[tool_index], "content": TRIMMED_TOOL_RESULT}
            continue
        # Remove the oldest exchange (index 1) plus any tool messages directly
        # following it, keeping the final user message and the newest answer.
        end = 1
        while end + 1 < len(out) and out[end + 1].get("role") == "tool":
            end += 1
        del out[1 : end + 1]
    return out


def _reject_constant(value: str) -> Any:
    raise ValueError(f"Invalid JSON value: {value}")


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Parse tool arguments from the provider's function call payload.

    Tool arguments normally arrive as a JSON string. Some servers send an
    object; some send nothing for parameterless tools. Anything else is an
    error the model gets to see so it can correct itself.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ValueError("Tool arguments must be a JSON object")
    try:
        parsed = json.loads(raw, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(
            f"Tool arguments are not valid JSON ({error}): {bounded(raw, 200)}"
        ) from error
    if not isinstance(parsed, dict):
        raise ValueError("Tool arguments must be a JSON object")
    return parsed


class Agent:
    def __init__(
        self,
        *,
        provider: Any,
        registry: Any,
        config: dict[str, Any],
        emit: Callable[[dict[str, Any]], None] | None = None,
        approve: Callable[[str], bool] | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.config = config
        self.emit = emit or (lambda event: None)
        self.approve = approve or (lambda description: False)
        self.messages: list[dict[str, Any]] = []
        self.system = ""

    def set_system(self, text: str) -> None:
        self.system = text

    def reset(self) -> None:
        """Forget the conversation but keep the system prompt and tools."""
        self.messages = []

    def run(
        self, user_input: str, *, signal: AbortToken | None = None
    ) -> dict[str, Any]:
        """Run one user turn. Returns {text, messages}."""
        self.messages.append({"role": "user", "content": user_input})
        limits = self.config["agent"]
        last_failure = {"key": "", "count": 0}

        for iteration in range(1, int(limits["maxIterations"]) + 1):
            self._check(signal)
            self.emit(
                {
                    "type": "iteration",
                    "iteration": iteration,
                    "contextSize": context_size(self.messages),
                }
            )

            result = self.provider.chat(
                messages=self._outgoing(),
                tools=self.registry.schemas(),
                signal=signal,
                on_delta=lambda text: self.emit(
                    {"type": "assistant_delta", "text": text}
                ),
            )
            self._check(signal)

            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": result.get("content") or "",
            }
            if result.get("toolCalls"):
                assistant["tool_calls"] = result["toolCalls"]
            self.messages.append(assistant)

            if not result.get("toolCalls"):
                if result.get("finishReason") == "length":
                    self.emit(
                        {
                            "type": "warning",
                            "message": "The model reply was truncated by its token limit.",
                        }
                    )
                text = result.get("content") or ""
                self.emit({"type": "done", "text": text})
                return {"text": text, "messages": self.messages}

            for call in result["toolCalls"]:
                self._check(signal)
                function = call.get("function") or {}
                name = function.get("name") or ""
                args: dict[str, Any] = {}
                failure: Exception | None = None
                try:
                    args = parse_tool_arguments(function.get("arguments"))
                except Exception as error:  # noqa: BLE001 - reported to the model
                    failure = error

                self.emit({"type": "tool_start", "name": name, "args": args})
                started = time.monotonic()
                if failure is not None:
                    output: Any = {"error": str(failure)}
                else:
                    try:
                        output = self.registry.execute(
                            name, args, {"signal": signal, "approve": self.approve}
                        )
                    except AbortError:
                        raise
                    except Exception as error:  # noqa: BLE001 - returned to the model
                        if signal is not None and signal.is_aborted():
                            raise
                        output = {"error": str(error) or type(error).__name__}
                ok = not output or not (isinstance(output, dict) and "error" in output)
                duration_ms = int((time.monotonic() - started) * 1000)
                self.emit(
                    {
                        "type": "tool_end",
                        "name": name,
                        "ok": ok,
                        "durationMs": duration_ms,
                        "result": output,
                    }
                )

                # Circuit breaker: the same failing call three times is a loop.
                key = f"{name}:{compact_json(args)}"
                if not ok:
                    if key == last_failure["key"]:
                        last_failure["count"] += 1
                    else:
                        last_failure = {"key": key, "count": 1}
                    if last_failure["count"] >= 3:
                        raise AgentError(
                            f'Tool "{name}" failed {last_failure["count"]} times with identical arguments; '
                            f"stopping to avoid a loop. Last error: {output.get('error')}"
                        )
                else:
                    last_failure = {"key": "", "count": 0}

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": bounded(
                            self._json_result(output), limits["maxResultChars"]
                        ),
                    }
                )

            self.messages = trim_history(self.messages, limits["contextChars"])

        raise AgentError(
            f"Reached the maximum of {limits['maxIterations']} tool iterations without a final answer"
        )

    def _outgoing(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.system}, *self.messages]

    @staticmethod
    def _check(signal: AbortToken | None) -> None:
        if signal is not None:
            signal.throw_if_aborted()

    @staticmethod
    def _json_result(output: Any) -> str:
        try:
            return compact_json(output if output is not None else None)
        except (TypeError, ValueError):
            return compact_json(str(output))
