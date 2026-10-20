# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Skills: reusable instruction documents the model can load on demand.
#
# A skill is a directory containing SKILL.md with YAML-ish frontmatter:
#
#   ---
#   name: my-skill
#   description: >
#     One or two sentences that tell the model WHEN to use this skill.
#   ---
#
#   # Markdown body with the actual instructions
#
# At startup only the name + description of every skill is injected into the
# system prompt (cheap for local models). The model calls `load_skill` to get
# the full body when a skill is relevant. The format is the same one used by
# Claude/agents-style skills, so existing skill directories can be reused.
#
# The frontmatter parser intentionally supports only a small subset of YAML:
#   key: value
#   key: >
#     folded text joined with spaces
#   key: |
#     literal text where newlines are kept

from __future__ import annotations

import os
import re
from typing import Any

from .tool import Tool
from .util import bounded, collapse_whitespace


def _strip_outer_quote(value: str) -> str:
    # Equivalent of rawValue.replace(/^["']|["']$/g, '').trim().
    if value and value[0] in ("'", '"'):
        value = value[1:]
    if value and value[-1] in ("'", '"'):
        value = value[:-1]
    return value.strip()


def parse_frontmatter(text: Any) -> tuple[dict[str, str], str]:
    """Parse the leading `--- ... ---` frontmatter block."""
    normalized = str(text).replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end = normalized.find("\n---", 4)
    if end == -1:
        return {}, normalized

    header = normalized[4:end]
    body = normalized[end + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    meta: dict[str, str] = {}
    lines = header.split("\n")

    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not match:
            index += 1
            continue
        key, raw_value = match.group(1), match.group(2)

        # Block scalars: > (folded) and | (literal), with optional +/-
        if re.fullmatch(r"[>|][+-]?", raw_value):
            folded = raw_value.startswith(">")
            collected: list[str] = []
            while index + 1 < len(lines) and (
                lines[index + 1].startswith("  ") or not lines[index + 1].strip()
            ):
                collected.append(lines[index + 1])
                index += 1
            indents = [
                len(entry) - len(entry.lstrip(" "))
                for entry in collected
                if entry.strip()
            ]
            indent = min(indents) if indents else 0
            stripped = [entry[min(indent, len(entry)) :] for entry in collected]
            joined = "\n".join(stripped).strip()
            if folded:
                joined = (
                    re.sub(r"\n\s*\n", "\x00", joined)
                    .replace("\n", " ")
                    .replace("\x00", "\n")
                    .strip()
                )
            meta[key] = joined
            index += 1
            continue

        meta[key] = _strip_outer_quote(raw_value)
        index += 1
    return meta, body


def parse_skill_file(
    text: Any, fallback_name: str = ""
) -> dict[str, str]:
    """Parse one SKILL.md file into { name, description, body }."""
    meta, body = parse_frontmatter(text)
    name = collapse_whitespace(meta.get("name") or fallback_name)
    description = collapse_whitespace(meta.get("description") or "")
    if not description:
        # Fall back to the first non-heading, non-empty line of the body.
        for line in body.split("\n"):
            if line.strip() and not line.strip().startswith("#"):
                description = collapse_whitespace(line)
                break
        else:
            description = ""
    return {"name": name, "description": description, "body": body}


def discover_skills(
    paths: list[str], *, max_description_chars: int = 300
) -> dict[str, dict[str, Any]]:
    """Scan each configured path for <dir>/SKILL.md (or a direct SKILL.md path)."""
    skills: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            stats = os.stat(path)
        except OSError:
            continue
        candidates: list[tuple[str, str]] = []
        if os.path.isfile(path) and os.path.basename(path) == "SKILL.md":
            candidates.append((path, os.path.dirname(path)))
        elif os.path.isdir(path):
            try:
                entries = list(os.scandir(path))
            except OSError:
                entries = []
            for entry in entries:
                candidate_dir = os.path.join(path, entry.name)
                is_directory = entry.is_dir(follow_symlinks=False)
                if not is_directory and entry.is_symlink():
                    try:
                        is_directory = os.path.isdir(candidate_dir)
                    except OSError:
                        is_directory = False
                if not is_directory:
                    continue
                file_path = os.path.join(candidate_dir, "SKILL.md")
                if os.path.exists(file_path):
                    candidates.append((file_path, candidate_dir))
        for file_path, directory in candidates:
            try:
                with open(file_path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
                parsed = parse_skill_file(text, os.path.basename(directory))
                if not parsed["name"] or parsed["name"] in skills:
                    continue
                skills[parsed["name"]] = {
                    "name": parsed["name"],
                    "description": bounded(
                        parsed["description"], max_description_chars
                    ),
                    "body": parsed["body"],
                    "path": file_path,
                    "dir": directory,
                }
            except OSError:
                # A broken skill file must not prevent startup.
                continue
    return skills


def skill_index_text(skills: dict[str, dict[str, Any]]) -> str:
    """The compact index that goes into the system prompt."""
    return "\n".join(
        f"- {skill['name']}: {skill['description'] or '(no description)'}"
        for skill in skills.values()
    )


def load_skill_tool(
    skills: dict[str, dict[str, Any]], *, max_result_chars: int = 24000
) -> Tool:
    """The load_skill tool definition. Returns the full SKILL.md body on demand."""

    def run(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        name = args.get("name")
        skill = skills.get(name)
        if not skill:
            available = ", ".join(skills.keys()) or "(none)"
            raise ValueError(f'Unknown skill "{name}". Available: {available}')
        return {
            "name": skill["name"],
            "path": skill["path"],
            "dir": skill["dir"],
            "instructions": bounded(skill["body"], max_result_chars),
        }

    return Tool(
        name="load_skill",
        description=(
            "Load the full instructions of a skill listed in the system prompt. "
            "Call this before doing work that a skill describes. Returns the skill text and its directory; "
            "files and scripts referenced by the skill are usually relative to that directory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact skill name as listed in the system prompt",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        run=run,
    )
