# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Configuration loading and validation.
#
# Precedence, lowest to highest:
#   1. built-in DEFAULTS
#   2. the config file (--config, BASICAGENTPY_CONFIG, or ~/.config/basicagentpy/config.json)
#   3. environment overrides (BASICAGENTPY_API_KEY, BASICAGENTPY_BASE_URL,
#      BASICAGENTPY_MODEL, BASICAGENTPY_DATA_DIR)
#   4. command line flags (--api-key, --base-url, --model, --data-dir)
#
# Every path in the config may start with "~". Relative paths are resolved
# against the directory basicagentpy was started in (the "cwd").
#
# The returned object is a fully normalized copy: all file/skill/memory paths
# are absolute, and `paths.configPath` records which config file was used
# (it is protected from unapproved writes even if no file exists yet).

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .util import is_integer, resolve_user_path

ACCESS_LEVELS = ["rw", "ro", "approval", "deny"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The project's own skills directory (bundled `basicagentpy` self-help skill
# and the `example` template). It is resolved from this module, not from the
# working directory, and is appended to every skills.paths list in load_config
# so the bundled skills are available no matter where basicagentpy runs.
BUNDLED_SKILLS_DIR = str(PROJECT_ROOT / "skills")

DEFAULTS: dict[str, Any] = {
    "provider": {
        # Any OpenAI-compatible /v1 server: Ollama, llama.cpp, DeepSeek, OpenAI, ...
        "baseUrl": "http://localhost:11434/v1",
        "apiKey": "",
        "apiKeyEnv": "BASICAGENTPY_API_KEY",
        "model": "qwen3:8b",
        "temperature": 0.2,
        # Optional cap on one reply. None omits max_tokens from the request, so
        # the provider applies its own default output limit.
        "maxTokens": None,
        "timeoutMs": 120000,
        # Ollama does not support tool_choice, so it stays None (omitted) by
        # default. Set to "auto", "required" or "none" for providers that do.
        "toolChoice": None,
        # Some servers return token usage in the stream; harmless when off.
        "includeUsage": False,
    },
    "agent": {
        "maxIterations": 25,
        "contextChars": 100000,
        "maxResultChars": 24000,
        # Optional replacement for the built-in persona paragraph.
        "systemPrompt": "",
        "systemPromptFile": "",
    },
    "tools": {
        "files": {
            # Each root grants access to a directory tree:
            #   rw       read and write without approval
            #   ro       read only
            #   approval writes require a y/N confirmation
            #   deny     refuse everything
            # Paths outside every root are always refused.
            "roots": [{"path": ".", "access": "rw"}],
            # Writes inside these paths always require approval.
            "protectedPaths": [],
            "maxReadBytes": 24000,
            "maxWriteBytes": 1048576,
            "maxListEntries": 200,
        },
        "shell": {
            # Disabled by default. When enabled, every command still needs
            # approval (approval: "always") unless changed to "never".
            "enabled": False,
            "approval": "always",
            "timeoutMs": 30000,
            "maxOutputBytes": 24000,
            # A deliberately simple, obvious backstop. It is NOT a sandbox.
            "denylist": ["rm -rf /", "mkfs", "dd if=", "shutdown", "reboot", "wipefs"],
        },
        "web": {
            # The fetch_url tool is read-only and enabled by default. Set
            # enabled: false (or use --no-web) to remove the tool entirely.
            "enabled": True,
            # "always" asks for y/N approval before every fetch; "never" fetches
            # directly. Private/reserved addresses are blocked either way.
            "approval": "never",
            "timeoutMs": 15000,
            "maxRawBytes": 512000,
            "maxBytes": 24000,
            "maxRedirects": 5,
            # Danger: true lets fetch_url reach localhost and private networks.
            # Only for trusted local testing.
            "allowPrivate": False,
            "denyDomains": [],
            "allowDomains": [],
            "userAgent": "",
        },
    },
    "skills": {
        # Each path is scanned for <name>/SKILL.md. Missing paths are ignored.
        "paths": [
            "./skills",
            "~/.config/basicagentpy/skills",
            "~/.agents/skills",
            "~/.claude/skills",
        ],
    },
    "memory": {
        "enabled": True,
        # Overridden in load_config with the XDG-aware default_data_dir(env)
        # unless the config file, BASICAGENTPY_DATA_DIR or --data-dir sets one.
        "dir": "~/.local/share/basicagentpy",
    },
}


def default_config_path(env: dict[str, str] | None = None) -> str:
    """Expand "~" and XDG_CONFIG_HOME for the default config location."""
    env = os.environ if env is None else env
    from .util import home_dir

    base = env.get("XDG_CONFIG_HOME") or os.path.join(home_dir(), ".config")
    return os.path.join(base, "basicagentpy", "config.json")


def default_data_dir(env: dict[str, str] | None = None) -> str:
    """Default data directory (facts, sessions, audit log)."""
    env = os.environ if env is None else env
    from .util import home_dir

    base = env.get("XDG_DATA_HOME") or os.path.join(home_dir(), ".local", "share")
    return os.path.join(base, "basicagentpy")


def deep_merge(base: Any, overlay: Any) -> Any:
    """Merge overlay into base recursively. Arrays and scalars replace."""
    if overlay is None:
        return None
    if isinstance(base, list) or isinstance(overlay, list):
        return copy.deepcopy(overlay)
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = copy.deepcopy(base)
        for key, value in overlay.items():
            if key in out:
                out[key] = deep_merge(out[key], value)
            else:
                out[key] = copy.deepcopy(value)
        return out
    return copy.deepcopy(overlay)


def _fail(message: str) -> None:
    raise ValueError(message)


def normalize_config(
    config: dict[str, Any], *, cwd: str, config_path: str, config_found: bool
) -> dict[str, Any]:
    """Validate and normalize a raw config object.

    Mutates and returns the merged config. Throws with a readable message on
    any invalid field so problems surface at startup, not mid-conversation.
    """
    provider = config.get("provider")
    if not isinstance(provider, dict):
        _fail("provider must be an object")
    if not isinstance(provider.get("baseUrl"), str) or not provider["baseUrl"].strip():
        _fail("provider.baseUrl must be a non-empty string")
    if not isinstance(provider.get("model"), str) or not provider["model"].strip():
        _fail("provider.model must be a non-empty string")
    temperature = provider.get("temperature")
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not (-float("inf") < float(temperature) < float("inf"))
        or float(temperature) < 0
        or float(temperature) > 2
    ):
        _fail("provider.temperature must be a number between 0 and 2")
    max_tokens = provider.get("maxTokens")
    if max_tokens is not None and (not is_integer(max_tokens) or int(max_tokens) <= 0):
        _fail("provider.maxTokens must be null or a positive integer")
    if max_tokens is not None:
        provider["maxTokens"] = int(max_tokens)
    if not is_integer(provider.get("timeoutMs")) or int(provider["timeoutMs"]) <= 0:
        _fail("provider.timeoutMs must be a positive integer")
    provider["timeoutMs"] = int(provider["timeoutMs"])
    tool_choice = provider.get("toolChoice")
    if tool_choice is not None and tool_choice not in ("auto", "required", "none"):
        _fail('provider.toolChoice must be null, "auto", "required" or "none"')
    api_key_env = provider.get("apiKeyEnv")
    if api_key_env is not None and not isinstance(api_key_env, str):
        _fail("provider.apiKeyEnv must be a string")

    agent = config.get("agent")
    if not isinstance(agent, dict):
        _fail("agent must be an object")
    if not is_integer(agent.get("maxIterations")) or int(agent["maxIterations"]) < 1:
        _fail("agent.maxIterations must be a positive integer")
    if not is_integer(agent.get("contextChars")) or int(agent["contextChars"]) < 1000:
        _fail("agent.contextChars must be an integer of at least 1000")
    if not is_integer(agent.get("maxResultChars")) or int(agent["maxResultChars"]) < 1000:
        _fail("agent.maxResultChars must be an integer of at least 1000")
    agent["maxIterations"] = int(agent["maxIterations"])
    agent["contextChars"] = int(agent["contextChars"])
    agent["maxResultChars"] = int(agent["maxResultChars"])

    tools = config.get("tools")
    if not isinstance(tools, dict):
        _fail("tools must be an object")
    files = tools.get("files")
    if not isinstance(files, dict):
        _fail("tools.files must be an object")
    roots = files.get("roots")
    if not isinstance(roots, list) or len(roots) == 0:
        _fail("tools.files.roots must be a non-empty array")
    normalized_roots: list[dict[str, str]] = []
    for index, root in enumerate(roots):
        raw = {"path": root} if isinstance(root, str) else root
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str) or not raw["path"].strip():
            _fail(f"tools.files.roots[{index}].path must be a non-empty string")
        access = "rw" if raw.get("access") is None else raw.get("access")
        if access not in ACCESS_LEVELS:
            _fail(f"tools.files.roots[{index}].access must be one of: {', '.join(ACCESS_LEVELS)}")
        normalized_roots.append(
            {"path": resolve_user_path(raw["path"], cwd), "access": access}
        )
    files["roots"] = normalized_roots

    protected = files.get("protectedPaths") or []
    if not isinstance(protected, list):
        _fail("tools.files.protectedPaths must be an array of strings")
    normalized_protected: list[str] = []
    for path in protected:
        if not isinstance(path, str) or not path.strip():
            _fail("tools.files.protectedPaths entries must be non-empty strings")
        normalized_protected.append(resolve_user_path(path, cwd))
    files["protectedPaths"] = normalized_protected

    for key in ("maxReadBytes", "maxWriteBytes", "maxListEntries"):
        if not is_integer(files.get(key)) or int(files[key]) <= 0:
            _fail(f"tools.files.{key} must be a positive integer")
        files[key] = int(files[key])

    shell = tools.get("shell")
    if not isinstance(shell, dict):
        _fail("tools.shell must be an object")
    if not isinstance(shell.get("enabled"), bool):
        _fail("tools.shell.enabled must be a boolean")
    if shell.get("approval") not in ("always", "never"):
        _fail('tools.shell.approval must be "always" or "never"')
    for key in ("timeoutMs", "maxOutputBytes"):
        if not is_integer(shell.get(key)) or int(shell[key]) <= 0:
            _fail(f"tools.shell.{key} must be a positive integer")
        shell[key] = int(shell[key])
    denylist = shell.get("denylist")
    if not isinstance(denylist, list) or any(not isinstance(entry, str) for entry in denylist):
        _fail("tools.shell.denylist must be an array of strings")

    web = tools.get("web")
    if not isinstance(web, dict):
        _fail("tools.web must be an object")
    if not isinstance(web.get("enabled"), bool):
        _fail("tools.web.enabled must be a boolean")
    if web.get("approval") not in ("always", "never"):
        _fail('tools.web.approval must be "always" or "never"')
    for key in ("timeoutMs", "maxRawBytes", "maxBytes", "maxRedirects"):
        if not is_integer(web.get(key)) or int(web[key]) <= 0:
            _fail(f"tools.web.{key} must be a positive integer")
        web[key] = int(web[key])
    if not isinstance(web.get("allowPrivate"), bool):
        _fail("tools.web.allowPrivate must be a boolean")
    for key in ("denyDomains", "allowDomains"):
        if not isinstance(web.get(key), list) or any(
            not isinstance(entry, str) for entry in web[key]
        ):
            _fail(f"tools.web.{key} must be an array of strings")
    if not isinstance(web.get("userAgent"), str):
        _fail("tools.web.userAgent must be a string")

    skills = config.get("skills")
    if not isinstance(skills, dict):
        _fail("skills must be an object")
    skill_paths = skills.get("paths")
    if not isinstance(skill_paths, list) or any(not isinstance(path, str) for path in skill_paths):
        _fail("skills.paths must be an array of strings")
    skills["paths"] = [resolve_user_path(path, cwd) for path in skill_paths]

    memory = config.get("memory")
    if not isinstance(memory, dict):
        _fail("memory must be an object")
    if not isinstance(memory.get("enabled"), bool):
        _fail("memory.enabled must be a boolean")
    if not isinstance(memory.get("dir"), str) or not memory["dir"].strip():
        _fail("memory.dir must be a non-empty string")
    memory["dir"] = resolve_user_path(memory["dir"], cwd)

    system_prompt_file = agent.get("systemPromptFile")
    if system_prompt_file:
        agent["systemPromptFile"] = resolve_user_path(system_prompt_file, cwd)
    if agent.get("systemPrompt") != "" and not isinstance(agent.get("systemPrompt"), str):
        _fail("agent.systemPrompt must be a string")

    provider["baseUrl"] = provider["baseUrl"].rstrip("/")
    config["paths"] = {"cwd": cwd, "configPath": config_path, "configFound": config_found}
    return config


def load_config(
    *,
    flags: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Load, merge and validate the configuration.

    Never throws when the default config file is simply missing; an explicitly
    requested file must exist.
    """
    flags = flags or {}
    env = os.environ if env is None else env
    cwd = cwd or os.getcwd()

    explicit_path = flags.get("config") or env.get("BASICAGENTPY_CONFIG") or ""
    candidate = (
        resolve_user_path(explicit_path, cwd)
        if explicit_path
        else default_config_path(env)
    )

    file_config: dict[str, Any] = {}
    config_found = False
    if os.path.exists(candidate):
        try:
            with open(candidate, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Config file {candidate} is not valid JSON: {error}") from error
        if not isinstance(raw, dict):
            raise ValueError(f"Config file {candidate} must contain a JSON object")
        file_config = raw
        config_found = True
    elif explicit_path:
        raise ValueError(f"Config file not found: {candidate}")

    config = deep_merge(copy.deepcopy(DEFAULTS), file_config)

    # Environment overrides (before flags, after the file).
    if env.get("BASICAGENTPY_API_KEY"):
        config["provider"]["apiKey"] = env["BASICAGENTPY_API_KEY"]
    if env.get("BASICAGENTPY_BASE_URL"):
        config["provider"]["baseUrl"] = env["BASICAGENTPY_BASE_URL"]
    if env.get("BASICAGENTPY_MODEL"):
        config["provider"]["model"] = env["BASICAGENTPY_MODEL"]

    # Command line flag overrides.
    if flags.get("apiKey") is not None:
        config["provider"]["apiKey"] = flags["apiKey"]
    if flags.get("baseUrl") is not None:
        config["provider"]["baseUrl"] = flags["baseUrl"]
    if flags.get("model") is not None:
        config["provider"]["model"] = flags["model"]
    if flags.get("noWeb"):
        config["tools"]["web"]["enabled"] = False

    # Data directory: flag > BASICAGENTPY_DATA_DIR > config file memory.dir >
    # XDG-aware default. The default only applies when the file did not set one;
    # an invalid value from the file or a flag still fails validation.
    memory = config.get("memory")
    if isinstance(memory, dict):
        file_memory = file_config.get("memory")
        file_has_data_dir = isinstance(file_memory, dict) and "dir" in file_memory
        if flags.get("dataDir") is not None:
            memory["dir"] = flags["dataDir"]
        elif env.get("BASICAGENTPY_DATA_DIR"):
            memory["dir"] = env["BASICAGENTPY_DATA_DIR"]
        elif not file_has_data_dir:
            memory["dir"] = default_data_dir(env)

    # The API key may come from a named environment variable (keeps secrets
    # out of the config file). A directly configured key wins over the env name.
    if not config["provider"].get("apiKey") and config["provider"].get("apiKeyEnv"):
        config["provider"]["apiKey"] = env.get(config["provider"]["apiKeyEnv"], "") or ""

    normalized = normalize_config(
        config, cwd=cwd, config_path=candidate, config_found=config_found
    )

    # Always scan the bundled skills too, after the user's paths so a skill of
    # the same name in the user's paths takes precedence.
    if BUNDLED_SKILLS_DIR not in normalized["skills"]["paths"]:
        normalized["skills"]["paths"].append(BUNDLED_SKILLS_DIR)
    return normalized
