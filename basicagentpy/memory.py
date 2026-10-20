# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Conversation and fact persistence.
#
# Two plain-text stores live under the data directory (default
# ~/.local/share/basicagentpy, created with mode 0700):
#
#   facts.jsonl            durable key/value facts saved by the remember tool
#   sessions/<id>.jsonl    one JSON object per user/assistant message
#   last-session.txt       id of the most recent session (for --continue)
#
# Everything is JSON Lines: human readable, easy to grep, easy to delete.
# Only user and assistant text is stored; tool calls/results are intentionally
# not replayed on resume.
#
# Fact operations rewrite facts.jsonl atomically (write temp + rename) so a
# crash cannot leave a half-written file.

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable

from .util import bounded, compact_json, iso_now


MAX_EVENT_BYTES = 24000


def sanitize_session_id(value: Any) -> str:
    session_id = re.sub(r"[^A-Za-z0-9._-]", "-", str(value or "").strip())
    if not session_id or session_id in (".", ".."):
        raise ValueError(
            "Session id may only contain letters, numbers, dot, underscore and dash"
        )
    return session_id[:100]


def new_session_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def _sort_key(pair: tuple[str, Any]) -> tuple[str, str]:
    # Approximate JavaScript's default localeCompare: case-insensitive first,
    # then a stable fallback for keys that differ only by case.
    return (pair[0].casefold(), pair[0])


class Memory:
    def __init__(self, *, dir: str) -> None:
        self.dir = dir
        self.sessions_dir = os.path.join(dir, "sessions")
        os.makedirs(self.sessions_dir, mode=0o700, exist_ok=True)
        self.facts_path = os.path.join(dir, "facts.jsonl")
        self.last_session_path = os.path.join(dir, "last-session.txt")
        self.audit_path = os.path.join(dir, "audit.jsonl")
        self.facts: dict[str, dict[str, str]] = self._load_facts()
        self.session_id: str | None = None
        self.buffer: list[dict[str, Any]] = []

    def _load_facts(self) -> dict[str, dict[str, str]]:
        facts: dict[str, dict[str, str]] = {}
        if not os.path.exists(self.facts_path):
            return facts
        with open(self.facts_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                        value = entry.get("value")
                        facts[entry["key"]] = {
                            "value": "" if value is None else str(value),
                            "at": entry.get("at") or "",
                        }
                except json.JSONDecodeError:
                    # Ignore damaged lines instead of failing the whole session.
                    pass
        return facts

    def _save_facts(self) -> None:
        lines = [
            compact_json(
                {"key": key, "value": entry["value"], "at": entry["at"]}
            )
            for key, entry in sorted(self.facts.items(), key=_sort_key)
        ]
        tmp = f"{self.facts_path}.tmp"
        text = "\n".join(lines) + ("\n" if lines else "")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, self.facts_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # Session lifecycle ------------------------------------------------------

    def start_session(self, session_id: str | None = None) -> str:
        self.session_id = sanitize_session_id(
            new_session_id() if session_id is None else session_id
        )
        path = os.path.join(self.sessions_dir, f"{self.session_id}.jsonl")
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
        fd = os.open(
            self.last_session_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{self.session_id}\n")
        self.buffer = []
        return self.session_id

    def continue_session(self) -> dict[str, Any] | None:
        """Resume the most recent session, or return None when there is none."""
        if not os.path.exists(self.last_session_path):
            return None
        with open(self.last_session_path, encoding="utf-8", errors="replace") as handle:
            session_id = handle.read().strip()
        if not session_id:
            return None
        try:
            return {
                "id": self.start_session(session_id),
                "messages": self.read_session(session_id),
            }
        except (ValueError, OSError):
            return None

    def delete_session(self, session_id: str) -> bool:
        """Delete one conversation; return True when the file existed."""
        clean = sanitize_session_id(session_id)
        path = os.path.join(self.sessions_dir, f"{clean}.jsonl")
        if not os.path.exists(path):
            return False
        os.unlink(path)
        pointer = ""
        if os.path.exists(self.last_session_path):
            with open(self.last_session_path, encoding="utf-8", errors="replace") as handle:
                pointer = handle.read().strip()
        if pointer == clean:
            remaining = sorted(
                name
                for name in os.listdir(self.sessions_dir)
                if name.endswith(".jsonl")
            )
            newest = remaining[-1] if remaining else None
            if newest:
                fd = os.open(
                    self.last_session_path,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"{newest.removesuffix('.jsonl')}\n")
            else:
                os.unlink(self.last_session_path)
        return True

    def read_session(self, session_id: str) -> list[dict[str, str]]:
        clean = sanitize_session_id(session_id)
        path = os.path.join(self.sessions_dir, f"{clean}.jsonl")
        if not os.path.exists(path):
            return []
        messages: list[dict[str, str]] = []
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("role") in ("user", "assistant"):
                        content = entry.get("content")
                        messages.append(
                            {
                                "role": entry["role"],
                                "content": "" if content is None else str(content),
                            }
                        )
                except (json.JSONDecodeError, AttributeError):
                    pass
        return messages

    def append_event(self, role: str, content: str) -> None:
        if not self.session_id or not content:
            return
        entry = {
            "ts": iso_now(),
            "role": role,
            "content": bounded(content, MAX_EVENT_BYTES),
        }
        self.buffer.append(entry)
        path = os.path.join(self.sessions_dir, f"{self.session_id}.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(compact_json(entry) + "\n")

    # Facts ------------------------------------------------------------------

    def reload_facts(self) -> int:
        """Re-read facts.jsonl from disk; returns the fact count."""
        self.facts = self._load_facts()
        return len(self.facts)

    def delete_fact(self, key: Any) -> bool:
        """Remove one fact by its exact key. Returns True when it existed."""
        existed = self.facts.pop(str(key or ""), None) is not None
        if existed:
            self._save_facts()
        return existed

    def remember(self, key: Any, value: Any) -> dict[str, str]:
        clean_key = bounded(str(key or "").strip(), 200)
        clean_value = bounded(str(value or "").strip(), 3000)
        if not clean_key or not clean_value:
            raise ValueError("Both key and value are required")
        fact = {"key": clean_key, "value": clean_value, "at": iso_now()}
        self.facts[clean_key] = {"value": clean_value, "at": fact["at"]}
        self._save_facts()
        return fact

    def search(self, query: Any) -> list[dict[str, str]]:
        """Case-insensitive substring search over facts, sessions and audit logs."""
        needle = str(query or "").strip().lower()
        if not needle:
            return []
        results: list[dict[str, str]] = []

        def remember(at: str, source: str, text: str) -> None:
            if len(results) < 8 and needle in str(text).lower():
                results.append(
                    {"at": at, "source": source, "snippet": bounded(str(text), 300)}
                )

        for key, entry in self.facts.items():
            remember(entry.get("at", ""), f"fact:{key}", f"{key}: {entry['value']}")

        session_names: Iterable[str] = []
        if os.path.exists(self.sessions_dir):
            session_names = sorted(
                (
                    name
                    for name in os.listdir(self.sessions_dir)
                    if name.endswith(".jsonl")
                ),
                reverse=True,
            )[:50]
        for name in session_names:
            path = os.path.join(self.sessions_dir, name)
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip() or needle not in line.lower():
                        continue
                    try:
                        entry = json.loads(line)
                        content = entry.get("content")
                        remember(
                            entry.get("ts", ""),
                            f"session:{name.removesuffix('.jsonl')}",
                            f"{entry.get('role')}: {'' if content is None else content}",
                        )
                    except (json.JSONDecodeError, AttributeError):
                        pass
        return results

    def forget(self, query: Any) -> int:
        """Remove every fact and session line containing the literal substring."""
        needle = str(query or "").strip().lower()
        if not needle:
            raise ValueError("Specify what to forget")
        removed = 0

        for key, entry in list(self.facts.items()):
            if needle in key.lower() or needle in entry["value"].lower():
                del self.facts[key]
                removed += 1
        if removed:
            self._save_facts()

        if os.path.exists(self.sessions_dir):
            for name in os.listdir(self.sessions_dir):
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(self.sessions_dir, name)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    lines = handle.read().split("\n")
                kept: list[str] = []
                for line in lines:
                    if not line.strip():
                        kept.append(line)
                        continue
                    if needle in line.lower():
                        removed += 1
                    else:
                        kept.append(line)
                tmp = f"{path}.tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(kept))
                os.replace(tmp, path)
        return removed

    def facts_context(self, max_bytes: int = 4000) -> str:
        """Facts formatted for the system prompt. Returns '' when there are none."""
        if not self.facts:
            return ""
        lines = [
            f"- {key}: {entry['value']}"
            for key, entry in sorted(self.facts.items(), key=_sort_key)
        ]
        return bounded("\n".join(lines), max_bytes)

    def close(self) -> None:
        # Python flushes synchronous writes; kept for symmetry with the CLI.
        pass
