# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# File tools with a root-based access policy.
#
# Every path an agent touches passes through check_access():
#
#   1. "~" is expanded; relative paths resolve against the working directory.
#   2. The path is resolved with realpath (including the nearest existing
#      parent for files that do not exist yet, and following dangling
#      symlinks manually) so symlinks cannot escape.
#   3. The path must fall inside one of tools.files.roots. The most specific
#      matching root (the longest resolved root path) decides the access
#      level: rw, ro, approval or deny. This keeps a broad root from silently
#      shadowing a narrower restrictive one, regardless of config order.
#   4. Writes additionally require approval when the path is protected:
#      entries in tools.files.protectedPaths, the active basicagentpy config
#      file itself, or the memory data directory (so the agent can edit its
#      own config, facts and audit log only with consent).
#
# Paths outside every root are refused with an explanatory error.

from __future__ import annotations

import os
from typing import Any

from ..tool import Tool
from ..util import bounded, expand_home, is_integer, js_len


def is_inside(parent: str, child: str) -> bool:
    """Is `child` equal to or inside `parent`? Both absolute, normalized paths."""
    rel = os.path.relpath(child, parent)
    return rel == "." or not (rel.startswith("..") or os.path.isabs(rel))


def soft_realpath(path: str) -> str:
    """realpath that also works for paths that do not exist yet.

    Python's realpath(strict=False) resolves the nearest existing ancestor and
    follows dangling symlinks, which is exactly what the path policy needs.
    """
    return os.path.realpath(path, strict=False)


def check_access(
    *,
    config: dict[str, Any],
    cwd: str,
    input: Any,
    write: bool = False,
) -> dict[str, Any]:
    """Resolve and authorize one path.

    Returns { absolute, real, access, requiresApproval }. Throws when the path
    is outside all roots or denied/read-only as requested.
    """
    if not isinstance(input, str) or not input.strip():
        raise ValueError('A non-empty "path" string is required')
    expanded = expand_home(input.strip())
    absolute = (
        os.path.normpath(expanded)
        if os.path.isabs(expanded)
        else os.path.abspath(os.path.join(cwd, expanded))
    )
    real = soft_realpath(absolute)

    matched: dict[str, str] | None = None
    matched_real = ""
    for root in config["tools"]["files"]["roots"]:
        root_real = soft_realpath(root["path"])
        if is_inside(root_real, real) and (
            matched is None or len(root_real) > len(matched_real)
        ):
            matched = root
            matched_real = root_real
    if matched is None:
        raise ValueError(f"Path is outside the configured file roots: {input}")
    access = matched["access"]
    if access == "deny":
        raise ValueError(f"Access to this path is denied by policy: {input}")
    if write and access == "ro":
        raise ValueError(f"This path is read-only by policy: {input}")

    requires_approval = bool(write and access == "approval")

    if write:
        protected_paths = set(config["tools"]["files"].get("protectedPaths") or [])
        config_path = (config.get("paths") or {}).get("configPath")
        if config_path:
            protected_paths.add(config_path)
        # The memory directory holds facts, sessions and the audit log; the
        # agent must not be able to rewrite its own record without consent.
        memory = config.get("memory") or {}
        if memory.get("enabled") and memory.get("dir"):
            protected_paths.add(memory["dir"])
        for protected_path in protected_paths:
            protected_real = soft_realpath(protected_path)
            if is_inside(protected_real, real):
                requires_approval = True
                break

    return {
        "absolute": absolute,
        "real": real,
        "access": access,
        "requiresApproval": requires_approval,
    }


def _guard_write(ctx: dict[str, Any], check: dict[str, Any]) -> None:
    if not check["requiresApproval"]:
        return
    approve = ctx.get("approve")
    approved = approve(f"Write to {check['absolute']} (protected by config)") if approve else False
    if not approved:
        raise ValueError("Write denied by user")


def _guard_dir(check: dict[str, Any]) -> None:
    try:
        stats = os.stat(check["real"])
    except OSError:
        stats = None
    if stats is None or not os.path.isdir(check["real"]):
        raise ValueError(f"Not a directory: {check['real']}")


def _line_range(
    lines: list[str], start_line: Any, end_line: Any
) -> tuple[int, int]:
    start = max(1, int(start_line)) if is_integer(start_line) else 1
    end = min(len(lines), int(end_line)) if is_integer(end_line) else len(lines)
    return start, end


def _read_text(path: str) -> str:
    with open(path, "rb") as handle:
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def file_tools(*, config: dict[str, Any], cwd: str) -> list[Tool]:
    limits = config["tools"]["files"]

    def read_file(
        args: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        check = check_access(config=config, cwd=cwd, input=args.get("path"))
        try:
            stats = os.stat(check["real"])
        except OSError:
            stats = None
        if stats is None:
            raise ValueError(f"File not found: {check['real']}")
        if not os.path.isfile(check["real"]):
            raise ValueError(f"Not a regular file: {check['real']}")
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        if (
            stats.st_size > limits["maxReadBytes"]
            and start_line is None
            and end_line is None
        ):
            raise ValueError(
                f"File is {stats.st_size} bytes (limit {limits['maxReadBytes']}); "
                "read it in ranges with start_line/end_line"
            )
        with open(check["real"], "rb") as handle:
            content = handle.read()
        if b"\x00" in content:
            raise ValueError("Binary files are not supported")
        lines = content.decode("utf-8", errors="replace").split("\n")
        start, end = _line_range(lines, start_line, end_line)
        selected = lines[start - 1 : end]
        numbered = "\n".join(
            f"{start + index:5d}| {line}" for index, line in enumerate(selected)
        )
        text = bounded(numbered, limits["maxReadBytes"])
        return {
            "path": check["real"],
            "totalLines": len(lines),
            "from": start,
            "to": end,
            "truncated": js_len(text) < js_len(numbered),
            "text": text,
        }

    def write_file(
        args: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError('"content" must be a string')
        size = len(content.encode("utf-8"))
        if size > limits["maxWriteBytes"]:
            raise ValueError(
                f"Content exceeds the {limits['maxWriteBytes']} byte write limit"
            )
        check = check_access(
            config=config, cwd=cwd, input=args.get("path"), write=True
        )
        _guard_write(ctx, check)
        os.makedirs(os.path.dirname(check["real"]) or ".", exist_ok=True)
        _write_text(check["real"], content)
        return {"written": True, "path": check["real"], "bytes": size}

    def edit_file(
        args: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        replace_all = bool(args.get("replace_all", False))
        if not old_string:
            raise ValueError(
                "old_string must not be empty; use write_file for new files"
            )
        if old_string == new_string:
            raise ValueError("old_string and new_string are identical")
        check = check_access(
            config=config, cwd=cwd, input=args.get("path"), write=True
        )
        _guard_write(ctx, check)
        content = _read_text(check["real"])
        count = content.count(old_string)
        if count == 0:
            raise ValueError(
                "old_string was not found in the file; read it again and copy the exact text"
            )
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_string matches {count} times; add more context or set replace_all: true"
            )
        updated = (
            content.replace(old_string, new_string)
            if replace_all
            else content.replace(old_string, new_string, 1)
        )
        if len(updated.encode("utf-8")) > limits["maxWriteBytes"]:
            raise ValueError(
                f"Content exceeds the {limits['maxWriteBytes']} byte write limit"
            )
        _write_text(check["real"], updated)
        return {
            "edited": True,
            "path": check["real"],
            "replacements": count if replace_all else 1,
        }

    def append_file(
        args: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        text = args.get("text")
        if not isinstance(text, str):
            raise ValueError('"text" must be a string')
        check = check_access(
            config=config, cwd=cwd, input=args.get("path"), write=True
        )
        try:
            stats = os.stat(check["real"])
        except OSError:
            stats = None
        if stats is None or not os.path.isfile(check["real"]):
            raise ValueError(
                f"File not found: {check['real']}. append_file does not create files."
            )
        _guard_write(ctx, check)
        with open(check["real"], "a", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return {
            "appended": True,
            "path": check["real"],
            "bytes": len(text.encode("utf-8")),
        }

    def list_dir(
        args: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        check = check_access(
            config=config, cwd=cwd, input=args.get("path", ".")
        )
        _guard_dir(check)
        try:
            entries = list(os.scandir(check["real"]))
        except OSError as error:
            raise ValueError(f"Could not list {check['real']}: {error}") from error
        names = sorted(
            (
                entry.name + "/"
                if entry.is_dir(follow_symlinks=False)
                else entry.name
                for entry in entries
            ),
            key=lambda name: (name.casefold(), name),
        )
        return {
            "path": check["real"],
            "count": len(names),
            "truncated": len(names) > limits["maxListEntries"],
            "entries": names[: limits["maxListEntries"]],
        }

    def rename_file(
        args: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        source = check_access(
            config=config, cwd=cwd, input=args.get("from"), write=True
        )
        try:
            stats = os.stat(source["real"])
        except OSError:
            stats = None
        if stats is None or not os.path.isfile(source["real"]):
            raise ValueError(f"Source file not found: {source['real']}")
        destination = check_access(
            config=config, cwd=cwd, input=args.get("to"), write=True
        )
        try:
            os.stat(destination["real"])
            exists = True
        except OSError:
            exists = False
        if exists:
            raise ValueError(f"Destination already exists: {destination['real']}")
        _guard_write(ctx, source)
        _guard_write(ctx, destination)
        os.rename(source["real"], destination["real"])
        return {"renamed": True, "from": source["real"], "to": destination["real"]}

    return [
        Tool(
            name="read_file",
            description=(
                "Read a UTF-8 text file. Output is line numbered (\"   12| text\"). "
                f"Files larger than {limits['maxReadBytes']} bytes need start_line/end_line. "
                "When quoting text for edit_file, do NOT include the line numbers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {
                        "type": "number",
                        "description": "First line to read (1-based, optional)",
                    },
                    "end_line": {
                        "type": "number",
                        "description": "Last line to read (inclusive, optional)",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            run=read_file,
        ),
        Tool(
            name="write_file",
            description=(
                "Write a complete UTF-8 file, creating parent directories as needed. "
                "Use for new files or explicitly requested full rewrites; for small changes prefer edit_file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            run=write_file,
        ),
        Tool(
            name="edit_file",
            description=(
                "Replace an exact text fragment in an existing file, preserving every other byte. "
                "old_string must match exactly (including whitespace and newlines). "
                "If old_string appears more than once, provide more surrounding context or set replace_all."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence (default false)",
                    },
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
            run=edit_file,
        ),
        Tool(
            name="append_file",
            description=(
                "Append text to the end of an EXISTING file without changing existing bytes. "
                "No newline is added automatically; include one in text when needed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["path", "text"],
                "additionalProperties": False,
            },
            run=append_file,
        ),
        Tool(
            name="list_dir",
            description=(
                "List the entries of a directory (directories get a trailing \"/\"), newest first is not guaranteed. "
                f"Limited to {limits['maxListEntries']} entries."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": 'Directory (default ".")',
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            run=list_dir,
        ),
        Tool(
            name="rename_file",
            description=(
                "Rename or move a file within the allowed roots. Refuses to overwrite an existing destination "
                "or move between filesystems."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                },
                "required": ["from", "to"],
                "additionalProperties": False,
            },
            run=rename_file,
        ),
    ]
