# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Small shared helpers used across the project. No dependencies.

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

try:  # POSIX: match Node's os.homedir(), which ignores $HOME.
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None  # type: ignore[assignment]


def home_dir() -> str:
    """Return the current user's home directory.

    Node's os.homedir() uses the password database on POSIX, not $HOME.  The
    config code relies on that behaviour, so use pwd when available.
    """
    if pwd is not None:
        try:
            return pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, OSError):
            pass
    return os.path.expanduser("~")


def expand_home(value: str) -> str:
    """Expand a leading "~" to the current user's home directory."""
    if not isinstance(value, str):
        return value
    if value == "~":
        return home_dir()
    if value.startswith("~/"):
        return os.path.join(home_dir(), value[2:])
    return value


def resolve_user_path(value: str, base: str | None = None) -> str:
    """Expand "~" and make a user supplied path absolute relative to base."""
    if base is None:
        base = os.getcwd()
    expanded = expand_home(str(value))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(base, expanded))


def bounded(text: Any, byte_limit: int) -> str:
    """Trim a string so it never exceeds `byte_limit` UTF-8 bytes.

    Characters are never split in half, so the result is always valid UTF-8.
    """
    value = str(text)
    data = value.encode("utf-8")
    if len(data) <= byte_limit:
        return value
    return data[:byte_limit].decode("utf-8", errors="ignore")


def js_len(text: str) -> int:
    """Length of a Python string in JavaScript UTF-16 code units."""
    return len(str(text).encode("utf-16-le")) // 2


def collapse_whitespace(text: Any) -> str:
    """Collapse all whitespace runs (including newlines) into single spaces."""
    return re.sub(r"\s+", " ", str(text)).strip()


def compact_json(value: Any) -> str:
    """JSON with JavaScript's JSON.stringify separators and no escaping."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def js_json_len(value: Any) -> int:
    """Equivalent of JSON.stringify(value).length in JavaScript."""
    return js_len(compact_json(value))


def preview_json(value: Any, max_bytes: int = 160) -> str:
    """Compact one-line JSON for log lines and terminal previews."""
    try:
        text = compact_json(value)
    except (TypeError, ValueError):
        text = str(value)
    return bounded(text, max_bytes)


def mask(secret: Any) -> str:
    """Mask a secret for display. Never prints the full value."""
    value = "" if secret is None else str(secret)
    if not value:
        return "(none)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}...{value[-2:]} ({len(value)} chars)"


def iso_now() -> str:
    """Current UTC time in the same format as Date.toISOString()."""
    return iso_timestamp(datetime.now(timezone.utc))


def iso_timestamp(value: datetime) -> str:
    """Format a datetime like JavaScript's Date.toISOString()."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_from_timestamp(timestamp: float) -> str:
    """Format a filesystem timestamp like Date.toISOString()."""
    return iso_timestamp(datetime.fromtimestamp(timestamp, tz=timezone.utc))


def is_integer(value: Any) -> bool:
    """Match JavaScript Number.isInteger for JSON numbers (booleans excluded)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    return False


def json_copy(value: Any) -> Any:
    """A deep copy for plain JSON data (equivalent of structuredClone)."""
    import copy

    return copy.deepcopy(value)
