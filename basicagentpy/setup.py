# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Onboarding helpers for `basicagentpy-setup`.
#
# The interactive questions live in setup_cli.py; this module holds the pure
# parts so they are testable without a terminal:
#
#   PROVIDER_PRESETS   the provider menu
#   load_base_config   defaults: the existing config, else config.example.json
#   apply_answers      merge the answers into the base config
#   test_provider      one minimal chat completion against the provider
#   summary_lines      the "about to write" block
#
# The result is validated with the same normalize_config the CLI and the web
# settings use, so the written file behaves exactly like one written by hand.

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .config import DEFAULTS, deep_merge, normalize_config
from .config_file import read_config_file
from .provider import Provider
from .util import bounded, collapse_whitespace, mask

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = str(PROJECT_ROOT / "config.example.json")

PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "label": "Local Ollama",
        "baseUrl": "http://localhost:11434/v1",
        "model": "qwen3:8b",
        "apiKeyEnv": "",
        "needsKey": False,
    },
    {
        "label": "Ollama Cloud",
        "baseUrl": "https://ollama.com/v1",
        "model": "gemma4:31b",
        "apiKeyEnv": "OLLAMA_API_KEY_CLOUD",
        "needsKey": True,
    },
    {
        "label": "DeepSeek",
        "baseUrl": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "apiKeyEnv": "DEEPSEEK_API_KEY",
        "needsKey": True,
    },
    {
        "label": "OpenAI",
        "baseUrl": "https://api.openai.com/v1",
        "model": "gpt-5.1",
        "apiKeyEnv": "OPENAI_API_KEY",
        "needsKey": True,
    },
    {
        "label": "llama.cpp server",
        "baseUrl": "http://localhost:8080/v1",
        "model": "local",
        "apiKeyEnv": "",
        "needsKey": False,
    },
    {
        "label": "Custom (enter your own URL and model)",
        "custom": True,
        "needsKey": True,
    },
]

CUSTOM_INDEX = len(PROVIDER_PRESETS) - 1


def matching_preset(base_url: Any) -> int:
    """The preset whose baseUrl matches, or the Custom entry."""
    clean = str(base_url or "").rstrip("/")
    for index, preset in enumerate(PROVIDER_PRESETS):
        if preset.get("baseUrl") and preset["baseUrl"] == clean:
            return index
    return CUSTOM_INDEX


def get_by_path(value: Any, path: str) -> Any:
    current = value
    for key in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(key)
        else:
            try:
                current = getattr(current, key)
            except AttributeError:
                return None
    return current


def load_base_config(
    config_path: str, *, example_path: str = EXAMPLE_PATH
) -> dict[str, Any]:
    """Defaults for the questions: existing config, else config.example.json."""
    if os.path.exists(config_path):
        raw, _found = read_config_file(config_path)
        return {"raw": raw, "source": config_path, "found": True}
    try:
        with open(example_path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            return {"raw": raw, "source": example_path, "found": False}
    except (OSError, json.JSONDecodeError):
        pass
    return {"raw": {}, "source": "built-in defaults", "found": False}


def apply_answers(base: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """Merge the answers into the base config; every other key is preserved."""
    raw = copy.deepcopy(base or {})
    raw["provider"] = dict(raw.get("provider") or {})
    raw["provider"]["baseUrl"] = str(answers["baseUrl"]).rstrip("/")
    raw["provider"]["model"] = answers["model"]
    for key in ("apiKey", "apiKeyEnv"):
        if key not in answers:
            continue
        value = answers[key]
        if value is None or value == "":
            raw["provider"].pop(key, None)
        else:
            raw["provider"][key] = value
    raw["tools"] = dict(raw.get("tools") or {})
    raw["tools"]["shell"] = {
        **dict(raw["tools"].get("shell") or {}),
        "enabled": bool(answers.get("shellEnabled")),
    }
    raw["memory"] = {
        **dict(raw.get("memory") or {}),
        "enabled": bool(answers.get("memoryEnabled")),
        "dir": answers.get("dataDir"),
    }
    return raw


def test_provider(
    raw: dict[str, Any],
    *,
    cwd: str | None = None,
    config_path: str = "",
    timeout_ms: int = 10000,
    provider_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """One minimal chat completion to check URL, model and key."""
    cwd = cwd or os.getcwd()
    merged = deep_merge(copy.deepcopy(DEFAULTS), raw)
    config = normalize_config(
        merged, cwd=cwd, config_path=config_path, config_found=True
    )
    config["provider"]["timeoutMs"] = min(config["provider"]["timeoutMs"], timeout_ms)
    config["provider"]["maxTokens"] = 16
    config["provider"]["temperature"] = 0
    if os.environ.get("BASICAGENTPY_API_KEY"):
        config["provider"]["apiKey"] = os.environ["BASICAGENTPY_API_KEY"]
    elif not config["provider"].get("apiKey") and config["provider"].get("apiKeyEnv"):
        config["provider"]["apiKey"] = (
            os.environ.get(config["provider"]["apiKeyEnv"], "") or ""
        )

    provider = provider_factory(config) if provider_factory else Provider(config)
    started = time.monotonic()
    try:
        result = provider.chat(
            messages=[{"role": "user", "content": "Reply with the single word: ready"}]
        )
        reply = (
            bounded(collapse_whitespace(result.get("content") or ""), 120)
            or "(empty reply)"
        )
        return {
            "ok": True,
            "message": reply,
            "durationMs": int((time.monotonic() - started) * 1000),
        }
    except Exception as error:  # noqa: BLE001 - reported to the setup user
        return {
            "ok": False,
            "message": str(error) or type(error).__name__,
            "durationMs": int((time.monotonic() - started) * 1000),
        }


def summary_lines(
    raw: dict[str, Any], *, config_path: str
) -> list[tuple[str, str]]:
    """[label, value] pairs for the confirmation block."""
    provider = raw.get("provider") or {}
    key = (
        f"stored in the file ({mask(provider.get('apiKey'))})"
        if provider.get("apiKey")
        else (
            f"environment variable {provider.get('apiKeyEnv')}"
            if provider.get("apiKeyEnv")
            else "(none)"
        )
    )
    max_tokens = provider.get("maxTokens")
    memory = raw.get("memory") or {}
    shell = (raw.get("tools") or {}).get("shell") or {}
    return [
        ("config file", config_path),
        ("provider", str(provider.get("baseUrl", ""))),
        ("model", str(provider.get("model", ""))),
        ("api key", key),
        ("max tokens", "provider default" if max_tokens is None else str(max_tokens)),
        (
            "shell tool",
            "enabled (every command asks approval)"
            if shell.get("enabled")
            else "disabled",
        ),
        (
            "memory",
            "disabled"
            if memory.get("enabled") is False
            else f"enabled, {memory.get('dir')}",
        ),
    ]
