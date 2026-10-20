# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# JSON API for the web interface.
#
# Routes (all under /api/, all requiring the bearer token):
#
#   GET    /api/status              server, model and tool summary
#   POST   /api/chat                run one turn; NDJSON event stream back
#   POST   /api/chat/abort          abort the running turn
#   POST   /api/approvals/:id       approve or deny a paused tool call
#   GET    /api/sessions            list stored conversations
#   POST   /api/sessions            start (or resume) a conversation
#   GET    /api/sessions/:id        one transcript with timestamps
#   DELETE /api/sessions/:id        remove one stored conversation
#   GET    /api/facts               list saved facts
#   PUT    /api/facts               add/update a fact {key, value}
#   DELETE /api/facts/:key          remove a fact by exact key
#   GET    /api/audit               audit log entries (limit/tool/ok filter)
#   GET    /api/config              raw + effective config (key redacted)
#   PUT    /api/config              validate, save and live-reload config
#
# The chat stream uses the same event objects as the CLI's --json mode,
# plus "approval", "approval_result" and "aborted".

from __future__ import annotations

import copy
import json
import os
from typing import Any
from urllib.parse import parse_qs, unquote

from ..abort import AbortError
from ..agent import context_size
from ..config import load_config
from ..config_file import read_config_file, validate_raw_config, write_config_file
from ..skills import discover_skills
from ..tools import create_registry
from ..util import mask
from .session import (
    WebSessionError,
    last_session_pointer,
    list_sessions,
    read_session_events,
)


MAX_MESSAGE_CHARS = 100000
AUDIT_LIMIT_DEFAULT = 200
AUDIT_LIMIT_MAX = 1000


class RequestError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _segment(path: str, prefix: str) -> str | None:
    try:
        return unquote(path[len(prefix) :], errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None


def create_api(*, state: dict[str, Any], manager: Any, version: str) -> "WebApi":
    return WebApi(state=state, manager=manager, version=version)


class WebApi:
    def __init__(self, *, state: dict[str, Any], manager: Any, version: str) -> None:
        self.state = state
        self.manager = manager
        self.version = version

    def memory_dir(self) -> str | None:
        return (
            self.state["config"]["memory"]["dir"]
            if self.state["config"]["memory"]["enabled"]
            else None
        )

    # Handlers ---------------------------------------------------------------

    def status(self, handler: Any) -> None:
        config = self.state["config"]
        directory = self.memory_dir()
        skills = discover_skills(config["skills"]["paths"])
        registry = create_registry(
            config=config,
            memory=self.manager.fact_store,
            skills=skills,
            cwd=self.state["cwd"],
        )
        handler.send_json(
            200,
            {
                "version": self.version,
                "model": config["provider"]["model"],
                "baseUrl": config["provider"]["baseUrl"],
                "cwd": self.state["cwd"],
                "contextChars": config["agent"]["contextChars"],
                "memoryEnabled": config["memory"]["enabled"],
                "dataDir": directory,
                "configPath": config["paths"]["configPath"],
                "configFound": config["paths"]["configFound"],
                "lastSessionId": last_session_pointer(directory) if directory else None,
                "sessions": len(self.manager.sessions),
                "tools": registry.names(),
                "skills": [
                    {"name": skill["name"], "description": skill["description"]}
                    for skill in skills.values()
                ],
            },
        )

    def chat(self, handler: Any) -> None:
        body = handler.read_json()
        message = body.get("message") if isinstance(body.get("message"), str) else ""
        if not message.strip():
            raise RequestError("message must be a non-empty string")
        if len(message) > MAX_MESSAGE_CHARS:
            raise RequestError(f"message exceeds {MAX_MESSAGE_CHARS} characters")
        session_id = body.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RequestError("sessionId is required")
        try:
            session = self.manager.get(session_id)
        except ValueError as error:
            raise RequestError("Invalid session id") from error
        if session.busy:
            handler.send_json(
                409, {"error": "A turn is already running in this session"}
            )
            return

        write_event, finish = handler.start_ndjson()
        finished = False
        disconnected = False

        def write(event: dict[str, Any]) -> None:
            nonlocal disconnected
            if finished or disconnected:
                return
            try:
                write_event(event)
            except (BrokenPipeError, ConnectionResetError, OSError):
                disconnected = True
                session.abort()

        try:
            session.run_turn(message, write)
        except AbortError:
            write({"type": "aborted"})
        except Exception as error:  # noqa: BLE001 - reported as an event
            write({"type": "error", "message": str(error) or type(error).__name__})
        finally:
            finished = True
            try:
                finish()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def chat_abort(self, handler: Any) -> None:
        body = handler.read_json()
        session_id = body.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RequestError("sessionId is required")
        try:
            self.manager.get(session_id).abort()
        except ValueError as error:
            raise RequestError("Invalid session id") from error
        handler.send_json(200, {"ok": True})

    def approval(self, handler: Any, path: str) -> None:
        approval_id = _segment(path, "/api/approvals/")
        if not approval_id:
            handler.send_json(404, {"error": "Unknown approval"})
            return
        body = handler.read_json()
        resolved = self.manager.resolve_approval(approval_id, bool(body.get("approved")))
        if not resolved:
            handler.send_json(404, {"error": "Unknown or expired approval"})
            return
        handler.send_json(200, {"ok": True})

    def sessions_list(self, handler: Any) -> None:
        directory = self.memory_dir()
        if not directory:
            handler.send_json(200, {"memoryEnabled": False, "sessions": []})
            return
        sessions = list_sessions(directory)
        for session in sessions:
            live = self.manager.sessions.get(session["id"])
            session["active"] = live is not None
            session["busy"] = bool(live.busy) if live is not None else False
        handler.send_json(200, {"memoryEnabled": True, "sessions": sessions})

    def session_start(self, handler: Any) -> None:
        body = handler.read_json()
        if "id" in body and not isinstance(body["id"], str):
            raise RequestError("id must be a string")
        try:
            session = (
                self.manager.get(body["id"])
                if body.get("id")
                else self.manager.create()
            )
        except ValueError as error:
            raise RequestError("Invalid session id") from error
        directory = self.memory_dir()
        handler.send_json(
            200,
            {
                "id": session.session_id,
                "messages": (
                    read_session_events(directory, session.session_id)
                    if directory
                    else []
                ),
                "contextSize": session.context_size(),
            },
        )

    def session_get(self, handler: Any, path: str) -> None:
        session_id = _segment(path, "/api/sessions/")
        if not session_id:
            handler.send_json(404, {"error": "Unknown session"})
            return
        directory = self.memory_dir()
        try:
            messages = (
                read_session_events(directory, session_id) if directory else []
            )
        except ValueError as error:
            raise RequestError("Invalid session id") from error
        live = self.manager.sessions.get(session_id)
        size = (
            live.context_size()
            if live is not None
            else context_size(
                [{"role": item["role"], "content": item["content"]} for item in messages]
            )
        )
        handler.send_json(200, {"id": session_id, "messages": messages, "contextSize": size})

    def session_delete(self, handler: Any, path: str) -> None:
        directory = self.memory_dir()
        if not directory:
            raise RequestError("memory is disabled")
        session_id = _segment(path, "/api/sessions/")
        if not session_id:
            handler.send_json(404, {"error": "Unknown session"})
            return
        try:
            result = self.manager.delete_session(session_id)
        except WebSessionError as error:
            handler.send_json(error.status, {"error": str(error)})
            return
        except ValueError as error:
            raise RequestError("Invalid session id") from error
        handler.send_json(200, result)

    def facts_list(self, handler: Any) -> None:
        store = self.manager.fact_store
        if store is None:
            raise RequestError("memory is disabled")
        store.reload_facts()
        facts = [
            {"key": key, "value": entry["value"], "at": entry["at"]}
            for key, entry in sorted(
                store.facts.items(), key=lambda item: (item[0].casefold(), item[0])
            )
        ]
        handler.send_json(200, {"facts": facts})

    def fact_put(self, handler: Any) -> None:
        store = self.manager.fact_store
        if store is None:
            raise RequestError("memory is disabled")
        body = handler.read_json()
        if not isinstance(body.get("key"), str) or not isinstance(
            body.get("value"), str
        ):
            raise RequestError("key and value must be strings")
        try:
            fact = store.remember(body["key"], body["value"])
        except ValueError as error:
            raise RequestError(str(error)) from error
        handler.send_json(200, {"fact": fact})

    def fact_delete(self, handler: Any, path: str) -> None:
        store = self.manager.fact_store
        if store is None:
            raise RequestError("memory is disabled")
        key = _segment(path, "/api/facts/")
        if not key:
            raise RequestError("key is required")
        handler.send_json(200, {"removed": store.delete_fact(key)})

    def audit(self, handler: Any, url: Any) -> None:
        directory = self.memory_dir()
        if not directory:
            raise RequestError("memory is disabled")
        path = os.path.join(directory, "audit.jsonl")
        if not os.path.exists(path):
            handler.send_json(
                200,
                {"entries": [], "total": 0, "limit": AUDIT_LIMIT_DEFAULT},
            )
            return

        params = parse_qs(url.query, keep_blank_values=True)
        raw_limit = params.get("limit", [str(AUDIT_LIMIT_DEFAULT)])[0]
        try:
            limit_value = int(raw_limit)
        except (TypeError, ValueError):
            limit_value = AUDIT_LIMIT_DEFAULT
        limit = (
            min(limit_value, AUDIT_LIMIT_MAX)
            if limit_value > 0
            else AUDIT_LIMIT_DEFAULT
        )
        tool = params.get("tool", [""])[0] or ""
        ok = params.get("ok", [""])[0] or ""

        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().split("\n")
        except OSError as error:
            handler.send_json(
                500, {"error": f"Could not read the audit log: {error}"}
            )
            return
        matching: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if tool and entry.get("tool") != tool:
                continue
            if ok == "true" and entry.get("ok") is not True:
                continue
            if ok == "false" and entry.get("ok") is not False:
                continue
            matching.append(entry)
        entries = matching[-limit:][::-1]
        handler.send_json(
            200, {"entries": entries, "total": len(matching), "limit": limit}
        )

    # Config -----------------------------------------------------------------

    def read_raw_config_safe(self) -> dict[str, Any]:
        path = self.state["config"]["paths"]["configPath"]
        try:
            raw, found = read_config_file(path)
            return {"raw": raw, "found": found, "path": path, "parseError": None}
        except Exception as error:  # noqa: BLE001 - reported to the browser
            return {
                "raw": {},
                "found": False,
                "path": path,
                "parseError": str(error) or type(error).__name__,
            }

    def config_payload(self) -> dict[str, Any]:
        config = self.state["config"]
        file_info = self.read_raw_config_safe()
        raw = copy.deepcopy(file_info["raw"])
        api_key_set = False
        if isinstance(raw.get("provider"), dict):
            api_key_set = bool(raw["provider"].get("apiKey"))
            raw["provider"]["apiKey"] = ""
        effective = copy.deepcopy(config)
        effective["provider"]["apiKey"] = mask(effective["provider"].get("apiKey"))
        return {
            "path": file_info["path"],
            "found": file_info["found"],
            "parseError": file_info["parseError"],
            "apiKeySet": api_key_set,
            "raw": raw,
            "effective": effective,
        }

    def config_get(self, handler: Any) -> None:
        handler.send_json(200, self.config_payload())

    def config_put(self, handler: Any) -> None:
        if self.manager.any_busy():
            raise RequestError(
                "Cannot reload the configuration while a turn is running", status=409
            )
        body = handler.read_json()
        incoming = body.get("config")
        if not isinstance(incoming, dict):
            raise RequestError("config must be a JSON object")

        # API key handling: null clears, ""/absent keeps the stored value. The
        # masked value never round-trips from the browser.
        provider = incoming.get("provider")
        if isinstance(provider, dict):
            if "apiKey" in provider:
                key = provider["apiKey"]
                if key is None:
                    provider.pop("apiKey", None)
                elif key == "":
                    existing = self.read_raw_config_safe()["raw"]
                    existing_provider = existing.get("provider") if isinstance(existing, dict) else None
                    existing_key = (
                        existing_provider.get("apiKey")
                        if isinstance(existing_provider, dict)
                        else None
                    )
                    if isinstance(existing_key, str) and existing_key:
                        provider["apiKey"] = existing_key
                    else:
                        provider.pop("apiKey", None)
                elif not isinstance(key, str):
                    raise RequestError("provider.apiKey must be a string or null")
            else:
                existing = self.read_raw_config_safe()["raw"]
                existing_provider = existing.get("provider") if isinstance(existing, dict) else None
                existing_key = (
                    existing_provider.get("apiKey")
                    if isinstance(existing_provider, dict)
                    else None
                )
                if isinstance(existing_key, str) and existing_key:
                    provider["apiKey"] = existing_key

        try:
            validate_raw_config(
                incoming,
                cwd=self.state["cwd"],
                config_path=self.state["config"]["paths"]["configPath"],
            )
        except Exception as error:  # noqa: BLE001 - reported to the browser
            raise RequestError(str(error) or type(error).__name__) from error
        try:
            write_config_file(self.state["config"]["paths"]["configPath"], incoming)
            self.state["config"] = load_config(
                flags=self.state["flags"], cwd=self.state["cwd"]
            )
        except Exception as error:  # noqa: BLE001 - reported to the browser
            handler.send_json(
                500, {"error": f"Could not save the config: {error}"}
            )
            return
        self.manager.reload(self.state["config"])
        handler.send_json(200, self.config_payload())

    # Router -----------------------------------------------------------------

    def handle(self, handler: Any, url: Any) -> bool:
        path = url.path
        method = handler.command
        try:
            if path == "/api/status" and method == "GET":
                self.status(handler)
                return True
            if path == "/api/chat" and method == "POST":
                self.chat(handler)
                return True
            if path == "/api/chat/abort" and method == "POST":
                self.chat_abort(handler)
                return True
            if path.startswith("/api/approvals/") and method == "POST":
                self.approval(handler, path)
                return True
            if path == "/api/sessions" and method == "GET":
                self.sessions_list(handler)
                return True
            if path == "/api/sessions" and method == "POST":
                self.session_start(handler)
                return True
            if path.startswith("/api/sessions/") and method == "GET":
                self.session_get(handler, path)
                return True
            if path.startswith("/api/sessions/") and method == "DELETE":
                self.session_delete(handler, path)
                return True
            if path == "/api/facts" and method == "GET":
                self.facts_list(handler)
                return True
            if path == "/api/facts" and method == "PUT":
                self.fact_put(handler)
                return True
            if path.startswith("/api/facts/") and method == "DELETE":
                self.fact_delete(handler, path)
                return True
            if path == "/api/audit" and method == "GET":
                self.audit(handler, url)
                return True
            if path == "/api/config" and method == "GET":
                self.config_get(handler)
                return True
            if path == "/api/config" and method == "PUT":
                self.config_put(handler)
                return True
            return False
        except RequestError as error:
            handler.send_json(error.status, {"error": str(error)})
            return True
        except Exception as error:  # noqa: BLE001 - reported to the browser
            status = getattr(error, "status", 500)
            try:
                handler.send_json(status, {"error": str(error) or type(error).__name__})
            except Exception:
                pass
            return True
