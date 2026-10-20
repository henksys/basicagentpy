# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# basicagentpy onboarding.
#
# Usage:
#   basicagentpy-setup
#
# Asks for the provider, model and directories, optionally tests the
# connection, writes the config file and offers to start the web interface
# (which opens the browser). Every question shows a [default] taken from an
# existing config file or from config.example.json; Enter accepts it.
#
# The questionnaire is deliberately a plain stdin/stdout script with no
# dependency, and it touches neither the agent loop nor the web server: the
# "start now" answer simply spawns the web entry point.

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from . import __version__
from .config import DEFAULTS, default_config_path, default_data_dir
from .config_file import validate_raw_config, write_config_file
from .setup import (
    PROVIDER_PRESETS,
    apply_answers,
    get_by_path,
    load_base_config,
    matching_preset,
    summary_lines,
    test_provider,
)
from .util import resolve_user_path

VERSION = __version__
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_BIN = PROJECT_ROOT / "bin" / "basicagentpy-web"

HELP = f"""basicagentpy setup {VERSION} - guided configuration for basicagentpy

Usage:
  basicagentpy-setup [options]

Options:
  --config <file>  Config file to create or edit
                   (default: ~/.config/basicagentpy/config.json)
  --no-test        Skip the provider connection test
  --no-start       Do not offer to start the web interface
  -h, --help       Show this help
  -v, --version    Show the version

Every question shows a [default]; press Enter to accept it. The defaults come
from config.example.json, or from your existing config file when it exists.
"""


class InputEndedError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise ValueError(message)


def parse_flags(argv: list[str]) -> argparse.Namespace:
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--config")
    parser.add_argument("--no-test", action="store_true")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("-v", "--version", action="store_true")
    return parser.parse_args(argv)


class Prompter:
    def __init__(self) -> None:
        self.closed = False

    def ask(self, text: str) -> str:
        sys.stdout.write(text)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if line == "":
            raise InputEndedError()
        return line.strip()

    def secret(self, text: str) -> str:
        sys.stdout.write(text)
        sys.stdout.flush()
        if not sys.stdin.isatty():
            line = sys.stdin.readline()
            if line == "":
                raise InputEndedError()
            return line.strip()
        return self._secret_tty()

    def _secret_tty(self) -> str:
        if termios is None or tty is None:  # pragma: no cover - Windows
            return getpass.getpass("").strip()
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        chars: list[str] = []
        try:
            tty.setraw(fd)
            while True:
                char = os.read(fd, 1)
                if not char:
                    raise InputEndedError()
                if char == b"\x03":
                    raise KeyboardInterrupt()
                if char == b"\x04":
                    raise InputEndedError()
                if char in (b"\r", b"\n"):
                    break
                if char in (b"\x7f", b"\b"):
                    if chars:
                        chars.pop()
                        sys.stdout.write("\b \b")
                        sys.stdout.flush()
                    continue
                text = char.decode("utf-8", errors="replace")
                chars.append(text)
                sys.stdout.write("*")
                sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\n")
            sys.stdout.flush()
        return "".join(chars).strip()

    def close(self) -> None:
        self.closed = True


def with_default(value: Any) -> str:
    return "" if value is None or value == "" else f" [{value}]"


def ask_value(p: Prompter, label: str, fallback: Any) -> Any:
    answer = p.ask(f"{label}{with_default(fallback)}: ")
    return fallback if answer == "" else answer


def ask_yes_no(p: Prompter, label: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = p.ask(f"{label} {suffix}: ").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        sys.stdout.write("Please answer y or n.\n")
        sys.stdout.flush()


def provider_menu() -> str:
    lines = ["Provider:"]
    for index, preset in enumerate(PROVIDER_PRESETS):
        suffix = "" if preset.get("custom") else f"  {preset.get('baseUrl', '')}"
        lines.append(f"  {index + 1}) {preset['label']:<26}{suffix}")
    return "\n".join(lines)


def choose_preset(p: Prompter, default_index: int) -> int:
    sys.stdout.write(provider_menu() + "\n")
    sys.stdout.flush()
    while True:
        answer = p.ask(f"Choose [{default_index + 1}]: ")
        if not answer:
            return default_index
        try:
            value = int(answer)
        except ValueError:
            value = 0
        if 1 <= value <= len(PROVIDER_PRESETS):
            return value - 1
        sys.stdout.write(
            f"Please enter a number between 1 and {len(PROVIDER_PRESETS)}.\n"
        )
        sys.stdout.flush()


def ask_provider(p: Prompter, raw: dict[str, Any]) -> dict[str, Any]:
    provider = raw.get("provider") or {}
    current = {
        "baseUrl": provider.get("baseUrl", DEFAULTS["provider"]["baseUrl"]),
        "model": provider.get("model", DEFAULTS["provider"]["model"]),
        "apiKeyEnv": provider.get(
            "apiKeyEnv", DEFAULTS["provider"]["apiKeyEnv"]
        ),
        "apiKey": provider.get("apiKey"),
    }

    choice = choose_preset(p, matching_preset(current["baseUrl"]))
    preset = PROVIDER_PRESETS[choice]
    if not preset.get("custom"):
        current.update(
            {
                "baseUrl": preset.get("baseUrl"),
                "model": preset.get("model"),
                "apiKeyEnv": preset.get("apiKeyEnv") or current["apiKeyEnv"],
            }
        )

    answers = {
        "baseUrl": ask_value(p, "Provider base URL", current["baseUrl"]),
        "model": ask_value(p, "Model", current["model"]),
    }

    if preset.get("needsKey"):
        api_key_env = ask_value(p, "API key env var", current["apiKeyEnv"])
        if api_key_env == "-":
            api_key_env = ""
        answers["apiKeyEnv"] = api_key_env
        label = (
            "API key to store in the config file (Enter keeps the stored key, \"-\" removes it): "
            if current["apiKey"]
            else "API key to store in the config file (Enter uses the env var; safer to keep it out of files): "
        )
        typed = p.secret(label)
        if typed == "-":
            answers["apiKey"] = None
        elif typed != "":
            answers["apiKey"] = typed
    return answers


def run_provider_test(
    p: Prompter, raw: dict[str, Any], context: dict[str, Any]
) -> bool:
    sys.stdout.write("Testing the provider connection...\n")
    sys.stdout.flush()
    result = test_provider(raw, **context)
    if result["ok"]:
        sys.stdout.write(
            f'ok: {raw["provider"]["model"]} replied "{result["message"]}" in {result["durationMs"]} ms\n'
        )
        sys.stdout.flush()
        return True
    sys.stdout.write(
        f'failed: {result["message"]} (after {result["durationMs"]} ms)\n'
    )
    sys.stdout.flush()
    return False


def spawn_web(config_path: str, default_path: str) -> int:
    args: list[str]
    if WEB_BIN.exists():
        args = [sys.executable, str(WEB_BIN)]
    else:
        args = [sys.executable, "-m", "basicagentpy.web_cli"]
    if config_path != default_path:
        args.extend(["--config", config_path])
    try:
        return subprocess.call(args)
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        sys.stderr.write(f"Could not start the web interface: {error}\n")
        return 1


def print_start_hint(config_path: str, default_path: str) -> None:
    suffix = "" if config_path == default_path else f" --config {config_path}"
    sys.stdout.write(
        f"\nStart the interface any time with:\n  basicagentpy-web{suffix}\n"
    )
    sys.stdout.flush()


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

    cwd = os.getcwd()
    default_path = default_config_path()
    config_path = (
        resolve_user_path(values.config, cwd) if values.config else default_path
    )
    try:
        base = load_base_config(config_path)
    except Exception as error:  # noqa: BLE001 - configuration error
        sys.stderr.write(f"fatal: {error}\n")
        return 1
    p = Prompter()

    sys.stdout.write(f"basicagentpy setup {VERSION}\n\n")
    sys.stdout.write(
        "This writes the basicagentpy configuration file. Press Enter to accept the\n"
    )
    sys.stdout.write("[default] shown for each question.\n\n")
    if base["found"]:
        sys.stdout.write(
            f"Found an existing config at {base['source']}; its values are the defaults.\n\n"
        )
    else:
        sys.stdout.write(
            f"No config found; the defaults come from {base['source']}.\n\n"
        )
    sys.stdout.flush()

    try:
        answers = ask_provider(p, base["raw"])
        answers["shellEnabled"] = ask_yes_no(
            p,
            "Enable the shell tool (run_command; every command still needs approval)",
            bool(get_by_path(base["raw"], "tools.shell.enabled")),
        )
        target_path = resolve_user_path(
            ask_value(p, "Config file", config_path), cwd
        )
        answers["dataDir"] = ask_value(
            p,
            "Data directory (facts, sessions, audit)",
            get_by_path(base["raw"], "memory.dir") or default_data_dir(),
        )
        answers["memoryEnabled"] = ask_yes_no(
            p,
            "Enable memory",
            get_by_path(base["raw"], "memory.enabled")
            if get_by_path(base["raw"], "memory.enabled") is not None
            else True,
        )

        raw = apply_answers(base["raw"], answers)

        if not values.no_test and ask_yes_no(
            p, "Test the provider connection now", True
        ):
            while not run_provider_test(
                p, raw, {"cwd": cwd, "config_path": target_path}
            ):
                if not ask_yes_no(
                    p, "Edit the provider settings and try again", True
                ):
                    break
                answers.update(ask_provider(p, raw))
                raw = apply_answers(base["raw"], answers)

        sys.stdout.write("\nAbout to write:\n\n")
        for label, value in summary_lines(raw, config_path=target_path):
            sys.stdout.write(f"  {label:<12}  {value}\n")
        sys.stdout.write("\n")
        if os.path.exists(target_path):
            sys.stdout.write(
                "The config file already exists; its other settings are preserved.\n"
            )

        if not ask_yes_no(p, "Write this configuration", True):
            p.close()
            sys.stdout.write("Nothing written.\n")
            return 0

        try:
            validate_raw_config(raw, cwd=cwd, config_path=target_path)
            write_config_file(target_path, raw)
        except Exception as error:  # noqa: BLE001 - reported to the user
            sys.stderr.write(f"error: {error}\n")
            p.close()
            return 2
        sys.stdout.write(f"Wrote {target_path} (mode 0600).\n")
        sys.stdout.flush()

        if values.no_start:
            p.close()
            print_start_hint(target_path, default_path)
            return 0

        start = ask_yes_no(p, "Start the web interface now", True)
        p.close()
        if not start:
            print_start_hint(target_path, default_path)
            return 0
        sys.stdout.write(
            "\nStarting the web interface; Ctrl+C stops it. The browser opens automatically.\n\n"
        )
        sys.stdout.flush()
        return spawn_web(target_path, default_path)
    except InputEndedError:
        sys.stdout.write("\nInput ended; nothing written.\n")
        return 1
    except KeyboardInterrupt:
        sys.stdout.write("\nSetup cancelled; nothing written.\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
