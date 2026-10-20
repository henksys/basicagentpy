# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Provider client for any OpenAI-compatible /v1/chat/completions endpoint.
#
# Tested against Ollama (local and cloud), llama.cpp server, DeepSeek and
# OpenAI. The client streams responses and rebuilds tool calls from the
# streamed deltas, tolerating the small differences between servers:
#
#   - missing or repeated tool call ids
#   - tool arguments delivered as an object instead of a JSON string
#   - names split across several deltas
#   - no trailing newline after the final SSE event
#
# `tool_choice` is omitted by default because Ollama does not support it.

from __future__ import annotations

import http.client
import json
import random
import socket
import ssl
import string
import threading
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import __version__
from .abort import AbortError, AbortToken
from .util import bounded, compact_json


class ProviderError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.name = "ProviderError"


class _Deadline:
    """Close the active connection when the total request deadline passes."""

    def __init__(self, timeout_ms: int, close: Any) -> None:
        self.timeout_ms = timeout_ms
        self.timed_out = False
        self._close = close
        self._timer = threading.Timer(max(timeout_ms, 1) / 1000.0, self._fire)
        self._timer.daemon = True
        self._lock = threading.Lock()

    def _fire(self) -> None:
        with self._lock:
            self.timed_out = True
        try:
            self._close()
        except Exception:
            pass

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.cancel()


class Provider:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config["provider"]

    @property
    def model(self) -> str:
        return self.config["model"]

    @model.setter
    def model(self, value: str) -> None:
        self.config["model"] = value

    # One chat completion round. Returns:
    #   { content, toolCalls, finishReason, usage }
    # `toolCalls` is a list of { id, type: "function", function: { name, arguments } }.
    # `on_delta` receives streamed text chunks as they arrive.
    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        signal: AbortToken | None = None,
        on_delta: Any = None,
    ) -> dict[str, Any]:
        on_delta = on_delta or (lambda text: None)
        body: dict[str, Any] = {
            "model": self.config["model"],
            "messages": messages,
            "stream": True,
            "temperature": self.config["temperature"],
        }
        if self.config.get("maxTokens") is not None:
            body["max_tokens"] = self.config["maxTokens"]
        if tools:
            body["tools"] = tools
            if self.config.get("toolChoice"):
                body["tool_choice"] = self.config["toolChoice"]
        if self.config.get("includeUsage"):
            body["stream_options"] = {"include_usage": True}

        url = f"{self.config['baseUrl']}/chat/completions"
        headers = {
            "content-type": "application/json",
            "accept-encoding": "identity",
            "user-agent": f"basicagentpy/{__version__}",
        }
        if self.config.get("apiKey"):
            headers["authorization"] = f"Bearer {self.config['apiKey']}"

        if signal is not None:
            signal.throw_if_aborted()

        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        connection_box: dict[str, Any] = {"conn": None, "resp": None}

        def close_current() -> None:
            # Close the raw socket, not the buffered response: the reader
            # thread may be blocked inside BufferedReader.readline() while
            # holding the buffer lock, and closing the buffer from another
            # thread would wait for that lock.
            raw_socket = connection_box.get("sock")
            if raw_socket is None:
                connection = connection_box.get("conn")
                raw_socket = getattr(connection, "sock", None) if connection is not None else None
            if raw_socket is not None:
                try:
                    raw_socket.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    raw_socket.close()
                except Exception:
                    pass
            connection = connection_box.get("conn")
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

        deadline = _Deadline(int(self.config["timeoutMs"]), close_current)
        remove_abort = signal.add_callback(close_current) if signal is not None else (lambda: None)

        try:
            deadline.start()
            conn, response = self._open(url, headers, payload, deadline, signal, connection_box)
            connection_box["conn"] = conn
            connection_box["resp"] = response
            return self._read_stream(response, on_delta, signal, deadline)
        except ProviderError:
            raise
        except AbortError:
            raise
        except Exception as error:
            if signal is not None and signal.is_aborted():
                raise AbortError("The operation was aborted") from error
            if deadline.timed_out:
                raise ProviderError(
                    f"Provider request timed out after {self.config['timeoutMs']} ms"
                ) from error
            message = getattr(error, "strerror", None) or str(error)
            raise ProviderError(f"Could not reach provider at {url}: {message}") from error
        finally:
            deadline.stop()
            remove_abort()
            close_current()
            response = connection_box.get("resp")
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _open(
        self,
        url: str,
        headers: dict[str, str],
        payload: bytes,
        deadline: _Deadline,
        signal: AbortToken | None,
        connection_box: dict[str, Any],
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        current = url
        method = "POST"
        body: bytes | None = payload
        visited = 0

        while True:
            if signal is not None:
                signal.throw_if_aborted()
            if deadline.timed_out:
                raise ProviderError(
                    f"Provider request timed out after {self.config['timeoutMs']} ms"
                )

            parsed = urlsplit(current)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ProviderError(f"Invalid provider base URL: {self.config['baseUrl']}")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            timeout = max(float(self.config["timeoutMs"]) / 1000.0, 0.001)

            if parsed.scheme == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    parsed.hostname, port, timeout=timeout, context=ssl.create_default_context()
                )
            else:
                conn = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
            connection_box["conn"] = conn
            try:
                conn.request(method, path, body=body, headers=headers)
                # getresponse() may clear HTTPConnection.sock for a
                # Connection: close response, so retain the raw socket here.
                connection_box["sock"] = getattr(conn, "sock", None)
                response = conn.getresponse()
            except Exception:
                connection_box["sock"] = None
                try:
                    conn.close()
                except Exception:
                    pass
                raise

            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("location")
                try:
                    response.read(2000)
                except Exception:
                    pass
                response.close()
                conn.close()
                connection_box["conn"] = None
                connection_box["sock"] = None
                if not location:
                    raise ProviderError(
                        f"Provider HTTP {response.status}: redirect without a Location header"
                    )
                visited += 1
                if visited > 10:
                    raise ProviderError("Too many provider redirects")
                next_url = urljoin(current, location)
                if urlsplit(next_url).scheme not in ("http", "https"):
                    raise ProviderError(
                        f"Provider redirect to unsupported protocol: {urlsplit(next_url).scheme}"
                    )
                current = next_url
                if response.status in (301, 302, 303) and method != "GET":
                    method = "GET"
                    body = None
                continue

            if response.status < 200 or response.status >= 300:
                text = self._read_error_body(response)
                response.close()
                conn.close()
                connection_box["conn"] = None
                connection_box["sock"] = None
                raise ProviderError(f"Provider HTTP {response.status}: {text}")
            connection_box["resp"] = response
            return conn, response

    @staticmethod
    def _read_error_body(response: http.client.HTTPResponse) -> str:
        chunks: list[bytes] = []
        size = 0
        try:
            while size < 2000:
                chunk = response.read(min(512, 2000 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        except Exception:
            pass
        return bounded(b"".join(chunks).decode("utf-8", errors="replace"), 2000)

    def _read_stream(
        self,
        response: http.client.HTTPResponse,
        on_delta: Any,
        signal: AbortToken | None,
        deadline: _Deadline,
    ) -> dict[str, Any]:
        content = ""
        finish_reason: str | None = None
        usage: Any = None
        saw_event = False
        calls: dict[int, dict[str, str]] = {}

        def handle_event(data: str) -> None:
            nonlocal content, finish_reason, usage, saw_event
            if data == "[DONE]":
                return
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError as error:
                raise ProviderError(
                    f"Malformed stream chunk from provider: {bounded(data, 200)}"
                ) from error
            saw_event = True
            if isinstance(parsed, dict) and parsed.get("usage"):
                usage = parsed["usage"]
            choice = (parsed.get("choices") or [None])[0] if isinstance(parsed, dict) else None
            if not choice:
                return
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or choice.get("message") or {}

            text = delta.get("content")
            if isinstance(text, list):
                text = "".join(
                    part if isinstance(part, str) else (part or {}).get("text", "")
                    for part in text
                )
            if isinstance(text, str) and text:
                content += text
                on_delta(text)

            for call in delta.get("tool_calls") or []:
                index = call.get("index")
                if index is None:
                    index = len(calls)
                index = int(index)
                acc = calls.get(index) or {"id": "", "name": "", "arguments": ""}
                if call.get("id"):
                    acc["id"] = call["id"]
                name = (call.get("function") or {}).get("name")
                if name and not acc["name"].endswith(name):
                    acc["name"] += name
                args = (call.get("function") or {}).get("arguments")
                if args is not None and not isinstance(args, str):
                    try:
                        args = compact_json(args)
                    except (TypeError, ValueError):
                        args = str(args)
                if isinstance(args, str):
                    acc["arguments"] += args
                calls[index] = acc

        def process_line(line: str) -> None:
            if line.endswith("\r"):
                line = line[:-1]
            if not line or line.startswith(":") or not line.startswith("data:"):
                return
            handle_event(line[5:].strip())

        try:
            while True:
                if signal is not None:
                    signal.throw_if_aborted()
                if deadline.timed_out:
                    raise ProviderError(
                        f"Provider request timed out after {self.config['timeoutMs']} ms"
                    )
                raw_line = response.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace")
                process_line(line)
        except ProviderError:
            raise
        except AbortError:
            raise
        except Exception as error:
            if signal is not None and signal.is_aborted():
                raise AbortError("The operation was aborted") from error
            if deadline.timed_out:
                raise ProviderError(
                    f"Provider request timed out after {self.config['timeoutMs']} ms"
                ) from error
            raise ProviderError(f"Provider stream interrupted: {error}") from error

        if signal is not None:
            signal.throw_if_aborted()
        if deadline.timed_out:
            raise ProviderError(
                f"Provider request timed out after {self.config['timeoutMs']} ms"
            )
        if not saw_event:
            raise ProviderError("Provider returned an empty stream")

        tool_calls = []
        for index in sorted(calls):
            call = calls[index]
            call_id = call["id"] or self._random_call_id(index)
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
            )

        if finish_reason is None and not content and not tool_calls:
            raise ProviderError("Provider stream ended without a finish reason or content")
        return {
            "content": content,
            "toolCalls": tool_calls,
            "finishReason": finish_reason,
            "usage": usage,
        }

    @staticmethod
    def _random_call_id(index: int) -> str:
        suffix = "".join(
            random.choice(string.ascii_lowercase + string.digits) for _ in range(8)
        )
        return f"call_{index}_{suffix}"
