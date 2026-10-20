# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# The web server: static files, security checks and the JSON API.
#
# Security model (this server can drive an agent with file and shell tools):
#   - binds to loopback by default; a non-loopback --host is an explicit
#     opt-out from the host/origin checks below
#   - the Host header must be the loopback address the server is bound to
#     (blocks DNS-rebinding attacks from other websites)
#   - cross-origin requests to /api are rejected
#   - every /api request needs "Authorization: Bearer <token>"; there are no
#     cookies, so there is no CSRF surface to abuse
#   - static files are served only from the bundled web/ directory
#
# Python's built-in http.server module is all that is used here; no
# dependencies.

from __future__ import annotations

import hmac
import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ..util import compact_json
from .api import create_api
from .session import SessionManager


WEB_ROOT = str(Path(__file__).resolve().parents[2] / "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

REQUEST_BODY_LIMIT = 2 * 1024 * 1024


def token_matches(header: str | None, token: str) -> bool:
    match = re.fullmatch(r"Bearer\s+(.+)", header or "")
    if not match:
        return False
    return hmac.compare_digest(match.group(1), token)


def loopback_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def is_loopback_host(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "basicagentpy"

    app_state: dict[str, Any]
    manager: SessionManager
    api: Any
    token_value: str
    enforce_host_checks: bool
    web_root: str

    # Suppress the default per-request logging.
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._safe_dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._safe_dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._safe_dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._safe_dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._safe_dispatch()

    # Response helpers -------------------------------------------------------

    def _safe_dispatch(self) -> None:
        self._response_started = False
        try:
            self._dispatch()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as error:  # noqa: BLE001 - send a last-resort response
            try:
                self.send_text(500, f"Internal error: {error}")
            except Exception:
                self.close_connection = True

    def send_text(self, status: int, text: str) -> None:
        if self._response_started:
            return
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self._response_started = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, body: Any) -> None:
        if self._response_started:
            return
        data = compact_json(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self._response_started = True
        self.wfile.write(data)

    def read_json(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = self._read_chunked()
        else:
            try:
                length = int(length_header or 0)
            except ValueError:
                length = 0
            if length > REQUEST_BODY_LIMIT:
                raise RequestError("Request body too large", status=413)
            data = self.rfile.read(length) if length > 0 else b""
        text = data.decode("utf-8", errors="replace")
        if not text.strip():
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise RequestError("Request body is not valid JSON", status=400) from error
        if not isinstance(parsed, dict):
            raise RequestError("Request body is not valid JSON", status=400)
        return parsed

    def _read_chunked(self) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            line = self.rfile.readline(65537)
            if not line:
                break
            try:
                chunk_size = int(line.split(b";")[0].strip(), 16)
            except ValueError as error:
                raise RequestError("Bad chunked request body", status=400) from error
            if chunk_size == 0:
                while True:
                    trailer = self.rfile.readline(65537)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            chunk = self.rfile.read(chunk_size)
            chunks.append(chunk)
            size += len(chunk)
            if size > REQUEST_BODY_LIMIT:
                raise RequestError("Request body too large", status=413)
            self.rfile.read(2)  # trailing CRLF
        return b"".join(chunks)

    def start_ndjson(self) -> tuple[Any, Any]:
        if self._response_started:
            raise RuntimeError("Response already started")
        self.send_response(200)
        self.send_header("content-type", "application/x-ndjson; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-accel-buffering", "no")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header("transfer-encoding", "chunked")
        self.send_header("connection", "close")
        self.end_headers()
        self._response_started = True
        self.close_connection = True

        def write_event(event: dict[str, Any]) -> None:
            data = (compact_json(event) + "\n").encode("utf-8")
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
            self.wfile.flush()

        def finish() -> None:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        return write_event, finish

    # Routing ----------------------------------------------------------------

    def _dispatch(self) -> None:
        try:
            url = urlsplit(self.path)
        except ValueError:
            self.send_text(400, "Bad request")
            return

        actual_port = self.server.server_address[1]
        host_header = self.headers.get("Host") or ""
        if self.enforce_host_checks and host_header not in loopback_hosts(actual_port):
            self.send_text(403, "Invalid Host header")
            return

        if url.path == "/api" or url.path.startswith("/api/"):
            if self.enforce_host_checks:
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{host_header}":
                    self.send_text(403, "Cross-origin request rejected")
                    return
            if not token_matches(self.headers.get("Authorization"), self.token_value):
                self.send_text(401, "Unauthorized")
                return
            if not self.api.handle(self, url):
                self.send_text(404, "Not found")
            return

        self._serve_static(url.path)

    def _serve_static(self, pathname: str) -> None:
        if self.command not in ("GET", "HEAD"):
            self.send_text(405, "Method not allowed")
            return
        try:
            relative = unquote(pathname, errors="strict")
        except (UnicodeDecodeError, ValueError):
            self.send_text(400, "Bad request")
            return
        if relative.endswith("/"):
            relative += "index.html"
        # Node's path.join concatenates even when the second part starts with
        # "/", so strip it before joining.
        relative = relative.lstrip("/")
        target = os.path.normpath(os.path.join(self.web_root, relative))
        root = os.path.normpath(self.web_root)
        if target != root and not target.startswith(root + os.sep):
            self.send_text(404, "Not found")
            return
        if not os.path.isfile(target):
            self.send_text(404, "Not found")
            return
        try:
            with open(target, "rb") as handle:
                data = handle.read()
        except OSError:
            self.send_text(404, "Not found")
            return
        extension = os.path.splitext(target)[1].lower()
        content_type = CONTENT_TYPES.get(extension, "application/octet-stream")
        cache = (
            "public, max-age=86400"
            if f"{os.sep}assets{os.sep}" in target
            else "no-cache"
        )
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", cache)
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        self.end_headers()
        self._response_started = True
        if self.command != "HEAD":
            self.wfile.write(data)


class WebServer:
    def __init__(
        self,
        *,
        server: ThreadingHTTPServer,
        manager: SessionManager,
        host: str,
        port: int,
        thread: threading.Thread,
    ) -> None:
        self.server = server
        self.manager = manager
        self.host = host
        self.port = port
        self.thread = thread
        self.url = (
            f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"
        )

    def close(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass
        self.thread.join(timeout=5)


def start_web_server(
    *,
    config: dict[str, Any],
    flags: dict[str, Any] | None = None,
    cwd: str,
    version: str,
    token: str,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> WebServer:
    if not token:
        raise ValueError("A token is required")
    flags = flags or {}
    state = {"config": config, "flags": flags, "cwd": cwd}
    manager = SessionManager(config=config, cwd=cwd)
    api = create_api(state=state, manager=manager, version=version)
    enforce_host_checks = is_loopback_host(host)

    class Handler(_Handler):
        pass

    Handler.app_state = state
    Handler.manager = manager
    Handler.api = api
    Handler.token_value = token
    Handler.enforce_host_checks = enforce_host_checks
    Handler.web_root = WEB_ROOT

    class _BoundServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    server = _BoundServer((host, port), Handler)
    server.daemon_threads = True
    server.block_on_close = False
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return WebServer(
        server=server, manager=manager, host=host, port=actual_port, thread=thread
    )
