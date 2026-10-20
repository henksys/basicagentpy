# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# basicagentpy web server.
#
# Usage:
#   basicagentpy-web                 serve the UI on 127.0.0.1:8787
#   basicagentpy-web --port 9000     pick another port
#   basicagentpy-web --token secret  use a fixed token instead of a random one
#
# The server binds to loopback only unless --host is given explicitly. Every
# /api request needs the bearer token, which is printed (and embedded in the
# URL fragment) at startup.

from __future__ import annotations

import argparse
import base64
import os
import secrets
import signal
import sys
import threading
from typing import Any
from urllib.parse import quote

from . import __version__
from .config import load_config
from .web.open_browser import open_browser
from .web.server import start_web_server

VERSION = __version__

HELP = f"""basicagentpy web {VERSION} - browser interface for basicagentpy

Usage:
  basicagentpy-web [options]

Options:
  --port <number>    Port to listen on (default: 8787)
  --host <address>   Address to bind (default: 127.0.0.1; anything else is
                     exposed to the network and requires the token)
  --token <string>   Fixed access token (default: random, printed at startup)
  --config <file>    Config file (default: ~/.config/basicagentpy/config.json)
  --cwd <dir>        Working directory for the agent (default: current dir)
  --data-dir <dir>   Data directory for facts/sessions/audit
  --model <name>     Override the configured model
  --base-url <url>   Override the provider base URL
  --api-key <key>    Override the API key (prefer BASICAGENTPY_API_KEY)
  --no-web           Disable the fetch_url web tool
  --no-open          Do not open the interface in the default browser
  -h, --help         Show this help
  -v, --version      Show the version

Settings come from, in order: built-in defaults, the config file,
BASICAGENTPY_API_KEY/BASICAGENTPY_BASE_URL/BASICAGENTPY_MODEL/BASICAGENTPY_DATA_DIR,
then CLI flags. The Settings tab reads and writes the same config file.
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise ValueError(message)


def parse_flags(argv: list[str]) -> argparse.Namespace:
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--port")
    parser.add_argument("--host")
    parser.add_argument("--token")
    parser.add_argument("--config")
    parser.add_argument("--cwd")
    parser.add_argument("--data-dir")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("-v", "--version", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        values = parse_flags(argv)
    except ValueError as error:
        sys.stderr.write(f"error: {error}\n\n{HELP}")
        return 2
    if values.help:
        sys.stdout.write(HELP)
        return 0
    if values.version:
        sys.stdout.write(f"{VERSION}\n")
        return 0

    if values.port is None:
        port = 8787
    else:
        try:
            port = int(values.port)
        except (TypeError, ValueError):
            port = -1
    if port < 0 or port > 65535:
        sys.stderr.write("error: --port must be an integer between 0 and 65535\n")
        return 2
    host = values.host or "127.0.0.1"
    token = values.token or base64.urlsafe_b64encode(
        secrets.token_bytes(24)
    ).rstrip(b"=").decode("ascii")
    cwd = os.path.abspath(values.cwd) if values.cwd else os.getcwd()

    flags = {
        "config": values.config,
        "cwd": values.cwd,
        "dataDir": values.data_dir,
        "model": values.model,
        "baseUrl": values.base_url,
        "apiKey": values.api_key,
        "noWeb": values.no_web,
    }
    try:
        config = load_config(flags=flags, cwd=cwd)
    except Exception as error:  # noqa: BLE001 - configuration error
        sys.stderr.write(f"error: {error}\n")
        return 2

    try:
        started = start_web_server(
            config=config,
            flags=flags,
            cwd=cwd,
            version=VERSION,
            token=token,
            host=host,
            port=port,
        )
    except OSError as error:
        sys.stderr.write(
            f"error: could not listen on {host}:{port}: {error}\n"
        )
        return 2

    token_url = f"{started.url}#token={quote(token, safe='')}"
    memory_line = (
        f"data       {config['memory']['dir']}\n"
        if config["memory"]["enabled"]
        else "memory     disabled\n"
    )
    sys.stdout.write(
        f"basicagentpy web {VERSION}\n"
        f"model      {config['provider']['model']} @ {config['provider']['baseUrl']}\n"
        f"cwd        {cwd}\n"
        + memory_line
        + f"listening  {started.url}\n"
        + f"token      {token}\n\n"
        + f"Open {token_url}\n"
        + (
            ""
            if values.no_open
            else "Opening it in your default browser (use --no-open to skip).\n"
        )
        + "\nPress Ctrl+C to stop.\n"
    )
    sys.stdout.flush()
    if not values.no_open:
        open_browser(token_url)
    if host not in ("127.0.0.1", "::1", "localhost"):
        sys.stdout.write(
            "Warning: the server is reachable from the network; keep the token secret.\n"
        )
        sys.stdout.flush()

    stop = threading.Event()

    def shutdown(signum: int, frame: Any) -> None:  # noqa: ARG001
        stop.set()

    old_int = signal.signal(signal.SIGINT, shutdown)
    old_term = signal.signal(signal.SIGTERM, shutdown)
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    sys.stdout.write("\nStopping basicagentpy web...\n")
    sys.stdout.flush()
    started.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
