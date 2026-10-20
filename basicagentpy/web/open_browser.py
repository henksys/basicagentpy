# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Open a URL in the user's default browser.
#
# Python has no built-in browser launcher, so this shells out to the platform
# command: xdg-open on Linux, open on macOS and start on Windows. The child
# is detached so it cannot keep the server alive, and every failure (no
# display, command missing) is swallowed: the URL is already on screen, so
# the server must keep running.

from __future__ import annotations

import subprocess
import sys
import threading
from typing import Any, Callable


def browser_command(platform: str, url: str) -> tuple[str, list[str]]:
    if platform == "darwin":
        return "open", [url]
    if platform == "win32":
        return "cmd", ["/c", "start", "", url]
    return "xdg-open", [url]


def open_browser(
    url: str,
    *,
    platform: str | None = None,
    spawn_impl: Callable[..., Any] | None = None,
) -> bool:
    command, args = browser_command(platform or sys.platform, url)
    spawn = spawn_impl or subprocess.Popen
    try:
        child = spawn(
            [command, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        return False

    def reap() -> None:
        try:
            child.wait()
        except Exception:
            pass

    if hasattr(child, "wait"):
        threading.Thread(target=reap, daemon=True).start()
    return True
