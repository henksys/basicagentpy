# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Web sessions: one Agent + Memory + tool registry per conversation.
#
# The CLI builds and owns a single Agent for its whole process. The web
# server instead keeps one WebSession per conversation, all composed from
# the same modules the CLI uses:
#
#   WebSession       provider + registry + agent + memory for one session id
#   SessionManager   lookup/creation of WebSessions and the shared fact store
#
# Approval is bridged to the browser: instead of a y/N prompt, the session
# emits { type: "approval", id, description } through its event stream and
# waits for resolve_approval(id, approved) from an HTTP request. Without an
# answer it denies after APPROVAL_TIMEOUT_MS, and it denies immediately when
# the turn is aborted.

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any, Callable

from ..agent import Agent, context_size
from ..abort import AbortToken
from ..memory import Memory, new_session_id, sanitize_session_id
from ..prompt import build_system_prompt
from ..provider import Provider
from ..skills import discover_skills
from ..tools import create_registry
from ..util import collapse_whitespace, iso_from_timestamp


APPROVAL_TIMEOUT_MS = 5 * 60 * 1000


class WebSessionError(Exception):
    def __init__(self, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.status = status


class WebSession:
    def __init__(
        self, *, config: dict[str, Any], cwd: str, session_id: str | None
    ) -> None:
        self.config = config
        self.cwd = cwd
        self.listeners: set[Callable[[dict[str, Any]], None]] = set()
        self.pending_approvals: dict[str, Future[bool]] = {}
        self.busy = False
        self.controller: AbortToken | None = None
        self._create_memory(session_id)
        self._build()
        self.agent.messages = (
            self.memory.read_session(self.session_id) if self.memory else []
        )

    def _create_memory(self, session_id: str | None) -> None:
        self.memory = (
            Memory(dir=self.config["memory"]["dir"])
            if self.config["memory"]["enabled"]
            else None
        )
        requested = (
            sanitize_session_id(session_id) if session_id else new_session_id()
        )
        self.session_id = (
            self.memory.start_session(requested) if self.memory else requested
        )

    def _build(self) -> None:
        self.skills = discover_skills(self.config["skills"]["paths"])
        self.provider = Provider(self.config)
        self.registry = create_registry(
            config=self.config,
            memory=self.memory,
            skills=self.skills,
            cwd=self.cwd,
        )
        self.agent = Agent(
            provider=self.provider,
            registry=self.registry,
            config=self.config,
            emit=self.emit,
            approve=self._approve,
        )
        self._refresh_system_prompt()

    def _refresh_system_prompt(self) -> None:
        self.agent.set_system(
            build_system_prompt(
                config=self.config,
                skills=self.skills,
                memory=self.memory,
                cwd=self.cwd,
            )
        )

    def emit(self, event: dict[str, Any]) -> None:
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                # A dead stream must never break the turn.
                pass

    def run_turn(
        self, user_input: str, emit: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any]:
        """Run one user turn, forwarding every agent event to `emit`."""
        if self.busy:
            raise WebSessionError(
                "A turn is already running in this session", status=409
            )
        self.busy = True
        self.controller = AbortToken()

        def forward(event: dict[str, Any]) -> None:
            try:
                emit(event)
            except Exception:
                # Ignore write failures on a closed stream.
                pass

        self.listeners.add(forward)
        try:
            if self.memory:
                self.memory.reload_facts()
            self._refresh_system_prompt()
            if self.memory:
                self.memory.append_event("user", user_input)
            result = self.agent.run(user_input, signal=self.controller)
            if self.memory:
                self.memory.append_event("assistant", result["text"])
            return result
        finally:
            self.listeners.discard(forward)
            self.busy = False
            self.controller = None
            self._deny_pending()

    def abort(self) -> None:
        if self.controller is not None:
            self.controller.abort()
        self._deny_pending()

    # Approval bridge --------------------------------------------------------

    def _approve(self, description: str) -> bool:
        approval_id = str(uuid.uuid4())
        future: Future[bool] = Future()
        self.pending_approvals[approval_id] = future
        self.emit(
            {
                "type": "approval",
                "id": approval_id,
                "description": description,
                "automatic": False,
            }
        )
        try:
            return bool(future.result(timeout=APPROVAL_TIMEOUT_MS / 1000.0))
        except FutureTimeoutError:
            self.pending_approvals.pop(approval_id, None)
            return False
        finally:
            self.pending_approvals.pop(approval_id, None)

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        future = self.pending_approvals.get(approval_id)
        if future is None:
            return False
        # Emit before releasing the waiting thread so the approval_result
        # event is ordered before the tool result (as in the Node version).
        self.emit(
            {
                "type": "approval_result",
                "id": approval_id,
                "approved": bool(approved),
            }
        )
        if not future.done():
            future.set_result(bool(approved))
        self.pending_approvals.pop(approval_id, None)
        return True

    def _deny_pending(self) -> None:
        for future in list(self.pending_approvals.values()):
            if not future.done():
                future.set_result(False)
        self.pending_approvals.clear()

    # Reload ----------------------------------------------------------------

    def reload(self, config: dict[str, Any]) -> None:
        self.config = config
        messages = self.agent.messages
        session_id = self.session_id
        self._create_memory(session_id)
        self._build()
        self.agent.messages = messages

    def info(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "busy": self.busy,
            "messages": len(self.agent.messages),
        }

    def context_size(self) -> int:
        """Serialized size of the live conversation for the context gauge."""
        return context_size(self.agent.messages)


class SessionManager:
    def __init__(self, *, config: dict[str, Any], cwd: str) -> None:
        self.config = config
        self.cwd = cwd
        self.sessions: dict[str, WebSession] = {}
        self.fact_store = (
            Memory(dir=config["memory"]["dir"])
            if config["memory"]["enabled"]
            else None
        )

    def get(self, session_id: str) -> WebSession:
        clean = sanitize_session_id(session_id)
        if clean not in self.sessions:
            self.sessions[clean] = WebSession(
                config=self.config, cwd=self.cwd, session_id=clean
            )
        return self.sessions[clean]

    def create(self, session_id: str | None = None) -> WebSession:
        return self.get(session_id or new_session_id())

    def delete_session(self, session_id: str) -> dict[str, Any]:
        clean = sanitize_session_id(session_id)
        session = self.sessions.get(clean)
        if session is not None and session.busy:
            raise WebSessionError("A turn is running in this session", status=409)
        self.sessions.pop(clean, None)
        removed = (
            self.fact_store.delete_session(clean) if self.fact_store else False
        )
        directory = self.config["memory"]["dir"] if self.config["memory"]["enabled"] else None
        return {
            "removed": removed,
            "lastSessionId": last_session_pointer(directory) if directory else None,
        }

    def any_busy(self) -> bool:
        return any(session.busy for session in self.sessions.values())

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        for session in self.sessions.values():
            if session.resolve_approval(approval_id, approved):
                return True
        return False

    def reload(self, config: dict[str, Any]) -> None:
        self.config = config
        self.fact_store = (
            Memory(dir=config["memory"]["dir"])
            if config["memory"]["enabled"]
            else None
        )
        for session in self.sessions.values():
            session.reload(config)


# Disk helpers for the browsing endpoints -----------------------------------


def last_session_pointer(directory: str) -> str | None:
    path = os.path.join(directory, "last-session.txt")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as handle:
        session_id = handle.read().strip()
    return session_id or None


def list_sessions(directory: str) -> list[dict[str, Any]]:
    sessions_dir = os.path.join(directory, "sessions")
    if not os.path.exists(sessions_dir):
        return []
    out: list[dict[str, Any]] = []
    for name in os.listdir(sessions_dir):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(sessions_dir, name)
        stats = os.stat(path)
        count = 0
        title = ""
        updated_at = iso_from_timestamp(stats.st_mtime)
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("role") not in ("user", "assistant"):
                        continue
                    count += 1
                    if not title and entry.get("role") == "user":
                        title = collapse_whitespace(
                            "" if entry.get("content") is None else entry["content"]
                        )[:90]
                    if entry.get("ts"):
                        updated_at = entry["ts"]
                except (ValueError, AttributeError):
                    pass
        out.append(
            {
                "id": name.removesuffix(".jsonl"),
                "messages": count,
                "title": title,
                "updatedAt": updated_at,
            }
        )
    return sorted(out, key=lambda item: str(item["updatedAt"]), reverse=True)


def read_session_events(directory: str, session_id: str) -> list[dict[str, str]]:
    path = os.path.join(directory, "sessions", f"{sanitize_session_id(session_id)}.jsonl")
    if not os.path.exists(path):
        return []
    out: list[dict[str, str]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("role") in ("user", "assistant"):
                    out.append(
                        {
                            "ts": entry.get("ts") or "",
                            "role": entry["role"],
                            "content": ""
                            if entry.get("content") is None
                            else str(entry["content"]),
                        }
                    )
            except (ValueError, AttributeError):
                pass
    return out
