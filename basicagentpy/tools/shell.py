# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# The run_command tool: run a local executable with literal arguments.
#
# Design choices, all deliberate:
#   - Disabled by default (tools.shell.enabled = false).
#   - No shell interpretation: `command` is an executable name and `args` is
#     an array of strings. Shell metacharacters like ; | > * have no special
#     meaning, so "rm -rf /" style accidents cannot hide inside a pipeline.
#   - The working directory must itself pass the file-root policy.
#   - Every call needs approval unless the config sets approval: "never".
#   - A simple denylist backstop refuses obviously destructive commands. It is
#     an accident brake, not a sandbox.
#   - Hard timeout, kill on timeout or cancellation, output caps.
#
# Warning for users: approving a run_command is full trust. A command can
# write outside the configured file roots. The roots only constrain the
# file tools.

from __future__ import annotations

import codecs
import os
import re
import signal as signal_module
import subprocess
import threading
from typing import Any

from ..abort import AbortError, AbortToken
from ..tool import Tool
from ..util import bounded
from .files import check_access


def shell_tools(*, config: dict[str, Any], cwd: str) -> list[Tool]:
    settings = config["tools"]["shell"]

    def run_command_tool(
        args: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any]:
        if not settings["enabled"]:
            raise ValueError(
                "The run_command tool is disabled by configuration (tools.shell.enabled). "
                "Ask the user to enable it."
            )
        command = args.get("command")
        command_args = args.get("args", [])
        requested_cwd = args.get("cwd")
        if (
            not isinstance(command, str)
            or not command.strip()
            or "\x00" in command
        ):
            raise ValueError("Invalid command")
        if not re.fullmatch(r"[A-Za-z0-9_./+-]+", command):
            raise ValueError(
                "Command must be a plain executable name or path (no shell syntax)"
            )
        if not isinstance(command_args, list) or any(
            not isinstance(arg, str) or "\x00" in arg for arg in command_args
        ):
            raise ValueError(
                "args must be an array of strings without NUL bytes"
            )

        workdir = cwd
        if requested_cwd is not None:
            check = check_access(config=config, cwd=cwd, input=requested_cwd)
            try:
                stats = os.stat(check["real"])
            except OSError:
                stats = None
            if stats is None or not os.path.isdir(check["real"]):
                raise ValueError(f"Working directory not found: {check['real']}")
            workdir = check["real"]

        # Collapse whitespace so extra spaces cannot slip a denied word past
        # the comparison. Still an accident brake, not a sandbox.
        line = re.sub(r"\s+", " ", " ".join([command, *command_args])).strip()
        lowered = line.lower()
        for denied in settings["denylist"]:
            needle = re.sub(r"\s+", " ", str(denied)).strip().lower()
            if needle in lowered:
                raise ValueError(
                    f'Command matches the configured denylist entry "{denied}" and was refused'
                )

        if settings["approval"] == "always":
            approve = ctx.get("approve")
            approved = (
                approve(f"Run command: {line}\n  in {workdir}")
                if approve
                else False
            )
            if not approved:
                raise ValueError("Command denied by user")

        return _run_command(
            command=command,
            args=command_args,
            cwd=workdir,
            timeout_ms=settings["timeoutMs"],
            max_output_bytes=settings["maxOutputBytes"],
            signal=ctx.get("signal"),
        )

    description = (
        "Run a local executable with literal arguments (no shell). Every call is shown and needs approval. "
        if settings["enabled"]
        else "EXECUTABLE DISABLED: the run_command tool is turned off in the config. "
        "Tell the user to enable tools.shell.enabled. "
    ) + (
        "Use file tools for file operations. Returns exitCode, stdout and stderr."
    )

    return [
        Tool(
            name="run_command",
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": 'Executable name or path, e.g. "git" or "/usr/bin/ls"',
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Literal argument strings (optional)",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (optional, defaults to the agent working directory)",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            run=run_command_tool,
        )
    ]


def _run_command(
    *,
    command: str,
    args: list[str],
    cwd: str,
    timeout_ms: int,
    max_output_bytes: int,
    signal: AbortToken | None,
) -> dict[str, Any]:
    """Spawn one process and collect its output with caps and a timeout."""
    if signal is not None:
        signal.throw_if_aborted()
    try:
        process = subprocess.Popen(
            [command, *args],
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise ValueError(f"Executable not found: {command}") from error
    except OSError as error:
        raise ValueError(f"Could not run {command}: {error}") from error

    state = {"stdout": "", "stderr": "", "truncated": False}
    stream_truncated = {"stdout": False, "stderr": False}
    lock = threading.Lock()

    def reader(stream: Any, key: str) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                with lock:
                    if stream_truncated[key]:
                        continue
                    candidate = state[key] + text
                    if len(candidate.encode("utf-8")) > max_output_bytes:
                        state[key] = bounded(candidate, max_output_bytes)
                        stream_truncated[key] = True
                        state["truncated"] = True
                    else:
                        state[key] = candidate
            tail = decoder.decode(b"", final=True)
            if tail:
                with lock:
                    if not stream_truncated[key]:
                        candidate = state[key] + tail
                        if len(candidate.encode("utf-8")) > max_output_bytes:
                            state[key] = bounded(candidate, max_output_bytes)
                            stream_truncated[key] = True
                            state["truncated"] = True
                        else:
                            state[key] = candidate
        finally:
            try:
                stream.close()
            except Exception:
                pass

    stdout_thread = threading.Thread(target=reader, args=(process.stdout, "stdout"), daemon=True)
    stderr_thread = threading.Thread(target=reader, args=(process.stderr, "stderr"), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def kill() -> None:
        try:
            process.kill()
        except Exception:
            pass

    remove_abort = signal.add_callback(kill) if signal is not None else (lambda: None)
    timed_out = False
    try:
        try:
            process.wait(timeout=max(timeout_ms, 1) / 1000.0)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if signal is not None and signal.is_aborted():
            raise AbortError("Command interrupted")
    finally:
        remove_abort()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    returncode = process.returncode
    close_signal = None
    exit_code = returncode
    if returncode is not None and returncode < 0:
        exit_code = None
        try:
            close_signal = signal_module.Signals(-returncode).name
        except ValueError:
            close_signal = str(-returncode)

    with lock:
        stdout_text = state["stdout"]
        stderr_text = state["stderr"]
        truncated = state["truncated"]

    return {
        "command": command,
        "args": args,
        "exitCode": exit_code,
        "signal": close_signal,
        "timedOut": timed_out,
        "truncated": truncated,
        "stdout": stdout_text,
        "stderr": stderr_text,
    }
