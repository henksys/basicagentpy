# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Read/validate/write the raw config file (used by the web settings tab and
# by basicagentpy-setup).
#
# The config file keeps the user's own JSON (relative paths, "~" and all);
# validation happens on a merged copy (DEFAULTS + file) with the same
# normalize_config the CLI uses, so a saved file behaves exactly like one
# written by hand. Writes are atomic (temp file + rename) and mode 0600
# because the file may contain an API key.

from __future__ import annotations

import copy
import json
import os
from typing import Any

from .config import DEFAULTS, deep_merge, normalize_config


def read_config_file(path: str) -> tuple[dict[str, Any], bool]:
    """Parse a config file. Returns (raw, found). A missing file is fine."""
    if not os.path.exists(path):
        return {}, False
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Config file {path} is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"Config file {path} must contain a JSON object")
    return raw, True


def validate_raw_config(
    raw: dict[str, Any], *, cwd: str, config_path: str
) -> dict[str, Any]:
    """Validate a raw config by normalizing DEFAULTS + raw."""
    merged = deep_merge(copy.deepcopy(DEFAULTS), raw)
    return normalize_config(merged, cwd=cwd, config_path=config_path, config_found=True)


def write_config_file(path: str, raw: dict[str, Any]) -> None:
    """Atomically write the raw config object as pretty JSON, mode 0600."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    text = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
