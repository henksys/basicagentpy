# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Terminal output.
#
# Two modes:
#   human  streaming text on stdout, tool activity and notices on stderr
#          (so `basicagentpy "..." > answer.txt` keeps stdout clean)
#   json   every event as one JSON object per line on stdout, suitable for
#          scripts; human errors still go to stderr
#
# Colors are used only when stderr is a TTY and NO_COLOR is not set.

from __future__ import annotations

import os
import sys
from typing import Any, TextIO

from .util import compact_json, preview_json


def prompt_label(
    label: str, *, stream: TextIO, env: dict[str, str] | None = None
) -> str:
    """Color the REPL prompt label like tool activity when the stream is a TTY."""
    env = os.environ if env is None else env
    if getattr(stream, "isatty", lambda: False)() and not env.get("NO_COLOR"):
        return f"\x1b[36m{label}\x1b[0m"
    return label


class UI:
    def __init__(
        self,
        *,
        json: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.json = json
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.use_color = bool(getattr(self.stderr, "isatty", lambda: False)()) and not os.environ.get(
            "NO_COLOR"
        )

    def paint(self, code: str, text: str) -> str:
        if not self.use_color:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def event(self, event: dict[str, Any]) -> None:
        """Machine readable event (JSON mode only)."""
        if self.json:
            self.stdout.write(compact_json(event) + "\n")
            self.stdout.flush()

    def banner(self, text: str) -> None:
        if not self.json:
            self.stderr.write(text + "\n")
            self.stderr.flush()

    def info(self, text: str) -> None:
        if not self.json:
            self.stderr.write(text + "\n")
            self.stderr.flush()

    def warn(self, text: str) -> None:
        prefix = "" if self.json else self.paint("33", "warning: ")
        self.stderr.write(prefix + text + "\n")
        self.stderr.flush()

    def error(self, text: str) -> None:
        prefix = "" if self.json else self.paint("31", "error: ")
        self.stderr.write(prefix + text + "\n")
        self.stderr.flush()

    def delta(self, text: str) -> None:
        if not self.json:
            self.stdout.write(text)
            self.stdout.flush()

    def turn_end(self) -> None:
        if not self.json:
            self.stdout.write("\n")
            self.stdout.flush()

    def tool_start(self, name: str, args: dict[str, Any]) -> None:
        if not self.json:
            self.stderr.write(
                self.paint("36", f"[tool] {name}") + " " + preview_json(args) + "\n"
            )
            self.stderr.flush()

    def tool_end(
        self, name: str, ok: bool, duration_ms: int, result: Any
    ) -> None:
        if self.json:
            return
        status = self.paint("32", "ok") if ok else self.paint("31", "failed")
        self.stderr.write(f"[tool] {name} {status} ({duration_ms}ms)\n")
        if not ok and isinstance(result, dict) and result.get("error"):
            self.stderr.write(f"       {str(result['error']).splitlines()[0]}\n")
        self.stderr.flush()


def create_ui(
    *,
    json: bool = False,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> UI:
    return UI(json=json, stdout=stdout, stderr=stderr)
