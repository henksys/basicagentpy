# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Tool registry: validation, dispatch and the audit log.
#
# Every tool is a Tool object:
#   Tool(name, description, parameters (JSON schema), run(args, ctx))
#
# The registry exposes them to the model in OpenAI function-calling format
# and validates arguments before running anything. Unknown arguments and
# wrong types are rejected with an error the model can read and correct,
# which keeps weak local models from silently doing the wrong thing.
#
# Every executed call is appended to audit.jsonl in the data directory:
#   { ts, tool, args (bounded), ok, error, durationMs, approvals }
# Approval requests made during the call are recorded with their answer, and
# an audit write that fails is reported once instead of silently dropped.

from __future__ import annotations

import math
import os
import time
from typing import Any, Callable

from ..skills import load_skill_tool
from ..tool import Tool
from ..util import bounded, compact_json, iso_now, preview_json
from .files import file_tools
from .memory_tools import memory_tools
from .shell import shell_tools
from .web import web_tools


def validate_args(schema: dict[str, Any], args: Any) -> None:
    """Minimal JSON-schema argument validation (the subset our tools use)."""
    if not isinstance(args, dict):
        raise ValueError("Arguments must be a JSON object")
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    for key in required:
        if key not in args:
            raise ValueError(f'Missing required argument "{key}"')
    for key, value in args.items():
        spec = properties.get(key)
        if spec is None:
            if schema.get("additionalProperties") is False:
                raise ValueError(f'Unknown argument "{key}"')
            continue
        kind = spec.get("type")
        if kind == "string":
            if not isinstance(value, str):
                raise ValueError(f'Argument "{key}" must be a string')
        elif kind == "number":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f'Argument "{key}" must be a number')
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f'Argument "{key}" must be a boolean')
        elif kind == "array":
            if not isinstance(value, list):
                raise ValueError(f'Argument "{key}" must be an array')
            if (spec.get("items") or {}).get("type") == "string" and any(
                not isinstance(item, str) for item in value
            ):
                raise ValueError(f'Argument "{key}" must be an array of strings')
        elif kind == "object":
            if not isinstance(value, dict):
                raise ValueError(f'Argument "{key}" must be an object')


def _memory_dir(memory: Any) -> str | None:
    if memory is None:
        return None
    if isinstance(memory, dict):
        return memory.get("dir")
    return getattr(memory, "dir", None)


class Registry:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        memory: Any | None,
        skills: dict[str, dict[str, Any]] | None,
        cwd: str,
        on_audit_error: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.skills = skills
        self.cwd = cwd
        self.on_audit_error = on_audit_error or (lambda text: None)
        self.tools: dict[str, Tool] = {}
        for tool in file_tools(config=config, cwd=cwd):
            self.register(tool)
        for tool in shell_tools(config=config, cwd=cwd):
            self.register(tool)
        for tool in web_tools(config=config):
            self.register(tool)
        if memory is not None and config["memory"]["enabled"]:
            for tool in memory_tools(memory=memory):
                self.register(tool)
        if skills:
            self.register(
                load_skill_tool(
                    skills, max_result_chars=config["agent"]["maxResultChars"]
                )
            )
        memory_dir = _memory_dir(memory)
        self.audit_path = (
            os.path.join(memory_dir, "audit.jsonl")
            if memory is not None and config["memory"]["enabled"] and memory_dir
            else None
        )
        self.audit_ready = False
        self.audit_failed = False

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self.tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self.tools.pop(name, None)

    def schemas(self) -> list[dict[str, Any]]:
        """Tool schemas in OpenAI function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self.tools.values()
        ]

    def names(self) -> list[str]:
        return list(self.tools.keys())

    def execute(
        self, name: str, args: dict[str, Any], ctx: dict[str, Any] | None = None
    ) -> Any:
        """Run one tool. ctx carries {signal, approve} from the caller."""
        tool = self.tools.get(name)
        if tool is None:
            available = ", ".join(self.tools.keys())
            raise ValueError(f'Unknown tool "{name}". Available tools: {available}')
        validate_args(tool.parameters, args)
        started = time.monotonic()
        approvals: list[dict[str, Any]] = []
        ctx = ctx or {}
        base_approve = ctx.get("approve") if callable(ctx.get("approve")) else None
        if base_approve is None:
            base_approve = lambda description: False

        def approve(description: str) -> bool:
            approved = bool(base_approve(description))
            approvals.append(
                {
                    "description": bounded(description, 500),
                    "approved": approved,
                }
            )
            return approved

        wrapped_ctx = {**ctx, "approve": approve}
        try:
            result = tool.run(args, wrapped_ctx)
            self._audit(
                {
                    "ts": iso_now(),
                    "tool": name,
                    "args": preview_json(args, 2000),
                    "ok": True,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "approvals": approvals,
                }
            )
            return result
        except Exception as error:
            self._audit(
                {
                    "ts": iso_now(),
                    "tool": name,
                    "args": preview_json(args, 2000),
                    "ok": False,
                    "error": bounded(str(error), 500),
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "approvals": approvals,
                }
            )
            raise

    def _audit(self, entry: dict[str, Any]) -> None:
        if not self.audit_path:
            return
        try:
            if not self.audit_ready:
                os.makedirs(_memory_dir(self.memory), mode=0o700, exist_ok=True)
                self.audit_ready = True
            fd = os.open(
                self.audit_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(compact_json(entry) + "\n")
        except OSError as error:
            # Auditing must never break a tool call, but a broken audit log is
            # worth reporting: without it there is no record of what the agent did.
            if not self.audit_failed:
                self.audit_failed = True
                self.on_audit_error(
                    f"Could not write the audit log ({error}); tool calls will not be recorded."
                )


def create_registry(
    *,
    config: dict[str, Any],
    memory: Any | None,
    skills: dict[str, dict[str, Any]] | None,
    cwd: str,
    on_audit_error: Callable[[str], None] | None = None,
) -> Registry:
    return Registry(
        config=config,
        memory=memory,
        skills=skills,
        cwd=cwd,
        on_audit_error=on_audit_error,
    )
