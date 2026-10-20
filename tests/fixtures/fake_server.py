# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Fixtures for tests: a tiny OpenAI-compatible SSE server.
#
# Each entry of `script` is the response for one request, either:
#   - a list of SSE event objects (or raw strings), or
#   - { "status": ..., "body": ... } to return an HTTP error.
# A function script(body, index) may be used for dynamic responses.
#
# Events are deliberately written in two halves to exercise the client's
# chunk buffering.

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def text_chunk(text: str, finish: str | None = None) -> dict[str, Any]:
    return {
        "choices": [
            {"index": 0, "delta": {"content": text}, "finish_reason": finish}
        ]
    }


def tool_chunk(
    *, index: int = 0, id: str | None = None, name: str | None = None, args: str | None = None
) -> dict[str, Any]:
    function: dict[str, Any] = {}
    if name:
        function["name"] = name
    if args:
        function["arguments"] = args
    call: dict[str, Any] = {"index": index, "function": function}
    if id:
        call["id"] = id
    return {
        "choices": [
            {"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}
        ]
    }


def finish_chunk(reason: str) -> dict[str, Any]:
    return {
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]
    }


class FakeProvider:
    def __init__(
        self,
        script: Any,
        server: ThreadingHTTPServer,
        thread: threading.Thread,
    ) -> None:
        self.script = script
        self.server = server
        self.thread = thread
        self.requests: list[Any] = []
        host, port = server.server_address[:2]
        self.base_url = f"http://{host}:{port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_fake_provider(script: Any) -> FakeProvider:
    holder: dict[str, FakeProvider] = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = None
            provider = holder["provider"]
            provider.requests.append(body)
            index = len(provider.requests) - 1
            if callable(script):
                entry = script(body, index)
            else:
                entry = script[min(index, len(script) - 1)]

            if isinstance(entry, dict):
                status = entry.get("status", 500)
                payload = entry.get("body", {"error": "fake failure"})
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            events = entry or []
            for event in events:
                text = (
                    event
                    if isinstance(event, str)
                    else f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                )
                middle = len(text) // 2
                self.wfile.write(text[:middle].encode("utf-8"))
                self.wfile.flush()
                self.wfile.write(text[middle:].encode("utf-8"))
                self.wfile.flush()
            if events and not isinstance(events[-1], str):
                self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = FakeProvider(script, server, thread)
    holder["provider"] = provider
    return provider
