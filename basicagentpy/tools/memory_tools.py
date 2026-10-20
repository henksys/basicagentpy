# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Memory tools: remember, search_memory and forget.
#
# These are thin wrappers over memory.py so the model can persist explicit
# user facts and search past sessions. Only registered when memory is enabled
# in the config.

from __future__ import annotations

from typing import Any

from ..tool import Tool


def memory_tools(*, memory: Any) -> list[Tool]:
    def remember(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        return memory.remember(args.get("key"), args.get("value"))

    def search_memory(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        matches = memory.search(args.get("query"))
        return {"matches": matches, "found": len(matches) > 0}

    def forget(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        removed = memory.forget(args.get("query"))
        return {"removed": removed, "forgotten": removed > 0}

    return [
        Tool(
            name="remember",
            description=(
                "Save an explicit durable user preference as a key/value pair. "
                "Only store facts the user actually stated; never guesses. Overwrites an existing key."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
            run=remember,
        ),
        Tool(
            name="search_memory",
            description=(
                "Search saved facts and past conversation sessions for a literal substring. "
                "Use for questions about earlier conversations or saved preferences."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            run=search_memory,
        ),
        Tool(
            name="forget",
            description=(
                "Remove every saved fact and session line containing a literal substring. "
                "Search first to find the exact stored text. Returns how many records were removed."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            run=forget,
        ),
    ]
