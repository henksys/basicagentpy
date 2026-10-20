# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# basicagentpy CLI.
#
# Usage:
#   basicagentpy                     interactive REPL
#   basicagentpy "do something"      one-shot: run one prompt and exit
#   basicagentpy --json "..."        one-shot with JSON-lines events on stdout
#   echo "prompt" | basicagentpy     read the prompt from stdin
#
# The REPL supports: /help /new /reload /skills /tools /model /config /quit
#
# One-shot mode denies approval-required actions unless --yes is given, so
# scripts never hang on a prompt. The REPL asks interactively; --yes there
# auto-approves everything as well.

from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import signal
import sys
import threading
from typing import Any

from . import __version__
from .abort import AbortError, AbortToken
from .agent import Agent
from .approval import create_approval
from .config import load_config
from .memory import Memory
from .prompt import build_system_prompt
from .provider import Provider
from .skills import discover_skills
from .tools import create_registry
from .ui import create_ui
from .util import mask

VERSION = __version__

HELP = f"""basicagentpy {VERSION} - a small CLI agent with file tools, skills and OpenAI-compatible models

Usage:
  basicagentpy [options] [prompt]
  echo "prompt" | basicagentpy [options]

Options:
  --config <file>    Config file (default: ~/.config/basicagentpy/config.json)
  --cwd <dir>        Working directory for this run (default: current directory)
  --data-dir <dir>   Data directory for facts/sessions/audit
                     (default: $XDG_DATA_HOME/basicagentpy or ~/.local/share/basicagentpy)
  --model <name>     Override the configured model
  --base-url <url>   Override the provider base URL (e.g. http://localhost:11434/v1)
  --api-key <key>    Override the API key (prefer BASICAGENTPY_API_KEY or apiKeyEnv)
  --continue         Resume the most recent conversation session
  --session <id>     Use (or create) a named conversation session
  --ephemeral        Disable memory for this run
  --no-web           Disable the fetch_url web tool for this run
  --json             One-shot JSON-lines event output (for scripts)
  --yes              Auto-approve guarded actions without asking (dangerous,
                     in both REPL and one-shot mode)
  -h, --help         Show this help
  -v, --version      Show the version

REPL commands:
  /help /new /reload /skills /tools /model [name] /config /quit

Settings come from, in order: built-in defaults, the config file,
BASICAGENTPY_API_KEY/BASICAGENTPY_BASE_URL/BASICAGENTPY_MODEL/BASICAGENTPY_DATA_DIR,
then CLI flags.
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise ValueError(message)


def parse_flags(argv: list[str]) -> argparse.Namespace:
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--continue", dest="continue_", action="store_true")
    parser.add_argument("--session")
    parser.add_argument("--config")
    parser.add_argument("--cwd")
    parser.add_argument("--data-dir")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("-v", "--version", action="store_true")
    parser.add_argument("prompt", nargs="*")
    return parser.parse_args(argv)


def read_stdin() -> str:
    return sys.stdin.read().strip()


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    copy_config = copy.deepcopy(config)
    copy_config["provider"]["apiKey"] = mask(copy_config["provider"].get("apiKey"))
    return copy_config


def render_error(ui: Any, error: BaseException) -> None:
    if isinstance(error, AbortError) or getattr(error, "name", "") == "AbortError":
        ui.error("Interrupted")
        return
    message = str(error) or type(error).__name__
    ui.event({"type": "error", "message": message})
    ui.error(message)


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

    ui = create_ui(json=values.json)
    memory = (
        Memory(dir=config["memory"]["dir"])
        if config["memory"]["enabled"] and not values.ephemeral
        else None
    )

    history: list[dict[str, Any]] = []
    if memory is not None:
        if values.continue_:
            resumed = memory.continue_session()
            if resumed is None:
                ui.error(
                    "No previous session to continue; run without --continue to start one."
                )
                return 1
            history = resumed["messages"]
        elif values.session:
            try:
                history = memory.read_session(memory.start_session(values.session))
            except (ValueError, OSError) as error:
                ui.error(str(error))
                return 2
        else:
            memory.start_session()
    elif values.continue_ or values.session:
        ui.warn(
            "--continue/--session ignored because memory is disabled or --ephemeral was used."
        )

    prompt_text = " ".join(values.prompt).strip()
    if not prompt_text and not sys.stdin.isatty():
        prompt_text = read_stdin()
    repl_mode = not prompt_text

    # Mutable runtime state so /reload and /model can swap pieces in place.
    state: dict[str, Any] = {
        "config": config,
        "skills": {},
        "provider": None,
        "registry": None,
        "agent": None,
        "busy": False,
        "controller": None,
        "closed": False,
        "pending_answers": [],
    }

    def on_event(event: dict[str, Any]) -> None:
        ui.event(event)
        kind = event.get("type")
        if kind == "assistant_delta":
            ui.delta(event.get("text", ""))
        elif kind == "tool_start":
            ui.tool_start(event.get("name", ""), event.get("args") or {})
        elif kind == "tool_end":
            ui.tool_end(
                event.get("name", ""),
                bool(event.get("ok")),
                int(event.get("durationMs") or 0),
                event.get("result"),
            )
        elif kind == "warning":
            ui.warn(event.get("message", ""))

    def build() -> None:
        current_config = state["config"]
        state["skills"] = discover_skills(current_config["skills"]["paths"])
        state["provider"] = Provider(current_config)
        state["registry"] = create_registry(
            config=current_config,
            memory=memory,
            skills=state["skills"],
            cwd=cwd,
            on_audit_error=ui.warn,
        )
        state["agent"] = Agent(
            provider=state["provider"],
            registry=state["registry"],
            config=current_config,
            emit=on_event,
            approve=approval,
        )
        state["agent"].messages = history

    def refresh_system_prompt() -> None:
        state["agent"].set_system(
            build_system_prompt(
                config=state["config"],
                skills=state["skills"],
                memory=memory,
                cwd=cwd,
            )
        )

    def run_turn_text(text: str, token: AbortToken) -> None:
        refresh_system_prompt()
        if memory:
            memory.append_event("user", text)
        result = state["agent"].run(text, signal=token)
        ui.turn_end()
        if memory:
            memory.append_event("assistant", result["text"])

    # Approval -------------------------------------------------------------
    # For the REPL, the question is routed through the event loop so the
    # input thread can keep reading while the turn runs.

    event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

    def repl_question(text: str) -> str:
        answer_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        state["pending_answers"].append(answer_queue)
        event_queue.put(("approval", (text, answer_queue)))
        answer = answer_queue.get()
        try:
            state["pending_answers"].remove(answer_queue)
        except ValueError:
            pass
        return answer

    approval = create_approval(
        interactive=repl_mode and not values.json,
        yes=bool(values.yes),
        question=repl_question if repl_mode else None,
        on_event=ui.event,
        on_notice=ui.warn,
    )

    build()

    # One-shot -------------------------------------------------------------
    if not repl_mode:
        controller = AbortToken()
        old_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda signum, frame: controller.abort())
        try:
            run_turn_text(prompt_text, controller)
        except BaseException as error:  # noqa: BLE001 - rendered for the user
            render_error(ui, error)
            return 1
        finally:
            signal.signal(signal.SIGINT, old_handler)
            if memory:
                memory.close()
        return 0

    # Interactive REPL -----------------------------------------------------
    refresh_system_prompt()
    ui.banner(f"basicagentpy {VERSION}")
    ui.banner(
        f"model     {state['provider'].model} @ {state['config']['provider']['baseUrl']}"
    )
    ui.banner(f"cwd       {cwd}")
    ui.banner(
        "skills    "
        + (", ".join(state["skills"].keys()) if state["skills"] else "(none)")
    )
    if memory and memory.session_id:
        ui.banner(f"session   {memory.session_id}")
    if history:
        ui.banner(
            f"resumed   {len(history)} messages from the previous session"
        )
    ui.banner(
        "Type /help for commands. Ctrl+C cancels a running turn; Ctrl+D exits."
    )

    def prompt_target() -> Any:
        return ui.stderr if values.json else ui.stdout

    def show_prompt() -> None:
        if state["closed"]:
            return
        target = prompt_target()
        target.write("basicagentpy> ")
        target.flush()

    def ask_target() -> Any:
        return ui.stderr if values.json else ui.stdout

    def start_turn(text: str) -> None:
        state["busy"] = True
        token = AbortToken()
        state["controller"] = token

        def worker() -> None:
            try:
                run_turn_text(text, token)
                event_queue.put(("turn_done", None))
            except BaseException as error:  # noqa: BLE001 - rendered in main loop
                event_queue.put(("turn_error", error))

        threading.Thread(target=worker, daemon=True).start()

    def handle_command(line: str) -> None:
        parts = line[1:].split()
        name = parts[0] if parts else ""
        argument = " ".join(parts[1:]).strip()
        if name == "help":
            ui.banner(
                "Commands: /new /reload /skills /tools /model [name] /config /quit"
            )
        elif name in ("quit", "exit"):
            state["closed"] = True
            if state["controller"] is not None:
                state["controller"].abort()
            for answer in list(state["pending_answers"]):
                try:
                    answer.put_nowait("n")
                except queue.Full:
                    pass
        elif name == "new":
            state["agent"].reset()
            if memory:
                memory.start_session()
            ui.info("Started a new conversation.")
        elif name == "reload":
            messages = state["agent"].messages
            try:
                state["config"] = load_config(flags=flags, cwd=cwd)
            except Exception as error:  # noqa: BLE001 - reported to the user
                ui.error(f"Reload failed: {error}")
                return
            build()
            state["agent"].messages = messages
            refresh_system_prompt()
            ui.info(
                f"Reloaded: model {state['provider'].model}, "
                f"{len(state['skills'])} skills, {len(state['registry'].names())} tools."
            )
        elif name == "skills":
            if not state["skills"]:
                ui.info("No skills found. Add <dir>/SKILL.md and /reload.")
            else:
                for skill in state["skills"].values():
                    ui.info(f"- {skill['name']}: {skill['description']}")
        elif name == "tools":
            ui.info(", ".join(state["registry"].names()))
        elif name == "model":
            if argument:
                state["provider"].model = argument
                ui.info(
                    f"Model set to {state['provider'].model} for this session (config unchanged)."
                )
            else:
                ui.info(f"model: {state['provider'].model}")
        elif name == "config":
            ui.info(
                json.dumps(redact_config(state["config"]), indent=2, ensure_ascii=False)
            )
        else:
            ui.warn(f"Unknown command /{name}. Try /help.")

    # Input thread: reads complete lines; the event loop handles the prompt.
    def input_loop() -> None:
        while True:
            line = sys.stdin.readline()
            if line == "":
                event_queue.put(("eof", None))
                return
            event_queue.put(("line", line.rstrip("\n")))

    threading.Thread(target=input_loop, daemon=True).start()

    sigint_seen = {"count": 0}

    def on_sigint(signum: int, frame: Any) -> None:  # noqa: ARG001
        sigint_seen["count"] += 1

    old_sigint = signal.signal(signal.SIGINT, on_sigint)
    show_prompt()
    try:
        while not state["closed"]:
            if sigint_seen["count"]:
                sigint_seen["count"] = 0
                if state["busy"] and state["controller"] is not None:
                    ui.warn("Cancelling current turn...")
                    for answer in list(state["pending_answers"]):
                        try:
                            answer.put_nowait("n")
                        except queue.Full:
                            pass
                    state["controller"].abort()
                else:
                    ui.info("(press Ctrl+D or type /quit to exit)")
                    show_prompt()
                continue

            try:
                kind, payload = event_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if kind == "line":
                line = str(payload).strip()
                if not line:
                    show_prompt()
                    continue
                if line.startswith("/"):
                    handle_command(line)
                    if not state["closed"]:
                        show_prompt()
                    continue
                if state["busy"]:
                    ui.warn("Still working; press Ctrl+C to cancel first.")
                    show_prompt()
                    continue
                start_turn(line)
            elif kind == "approval":
                text, answer_queue = payload
                target = ask_target()
                target.write(text)
                target.flush()
                # The next input line answers the approval.
                answer = ""
                while not state["closed"]:
                    if sigint_seen["count"]:
                        sigint_seen["count"] = 0
                        if state["controller"] is not None:
                            state["controller"].abort()
                        try:
                            answer_queue.put_nowait("n")
                        except queue.Full:
                            pass
                        answer = "n"
                        break
                    try:
                        line_kind, line_payload = event_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if line_kind == "eof":
                        state["closed"] = True
                        try:
                            answer_queue.put_nowait("n")
                        except queue.Full:
                            pass
                        break
                    if line_kind == "line":
                        answer = str(line_payload).strip()
                        break
                if not state["closed"]:
                    try:
                        answer_queue.put_nowait(answer)
                    except queue.Full:
                        pass
            elif kind == "turn_done":
                state["busy"] = False
                state["controller"] = None
                show_prompt()
            elif kind == "turn_error":
                state["busy"] = False
                state["controller"] = None
                error = payload
                render_error(ui, error)
                if not (
                    isinstance(error, AbortError)
                    or getattr(error, "name", "") == "AbortError"
                ):
                    ui.warn(
                        "The turn ended with an error; the conversation is still usable."
                    )
                show_prompt()
            elif kind == "eof":
                state["closed"] = True
                if state["controller"] is not None:
                    state["controller"].abort()
                break
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        for answer in list(state["pending_answers"]):
            try:
                answer.put_nowait("n")
            except queue.Full:
                pass
        if memory:
            memory.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
