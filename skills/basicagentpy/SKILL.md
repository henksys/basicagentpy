---
name: basicagentpy
description: >
  basicagentpy's own identity, configuration and extension guide. Use when the
  user asks how this agent works, where its config or memory lives, how to
  change settings, add tools or skills, or modify its source.
---

# basicagentpy self-help

You run inside **basicagentpy**, a small Python CLI agent that runs an
OpenAI-compatible model in a tool-calling loop. It also ships a local web
interface (`basicagentpy-web`) for chatting, editing settings and browsing
memory. Python 3.14+ only, no third-party dependencies. Version and project
metadata are in `pyproject.toml`; license is GPL-3.0-or-later.
"basicagentpy" is the name of the *program* (binary, config paths, tools, this
skill). The user may give *you* a different name or persona via
`agent.systemPrompt` / `agent.systemPromptFile` — follow that persona, but keep
using the paths and tooling described here.

## Where things live

| Thing | Location |
| --- | --- |
| Config | `~/.config/basicagentpy/config.json` (or `--config`, `BASICAGENTPY_CONFIG`) |
| Data | `$XDG_DATA_HOME/basicagentpy` (or `~/.local/share/basicagentpy`); set via `memory.dir`, `BASICAGENTPY_DATA_DIR` or `--data-dir`, mode 0700 |
| Facts | `facts.jsonl` — injected into your system prompt on every run |
| Sessions | `sessions/<id>.jsonl`; `last-session.txt` drives `--continue` |
| Audit | `audit.jsonl` — one line per tool call |
| Source | `bin/basicagentpy` (CLI), `bin/basicagentpy-web` (web server), `basicagentpy/` (loop, provider, tools), `basicagentpy/web/` (server, API, sessions), `web/` (browser UI), `skills/`, `docs/`, `tests/` |

Env overrides: `BASICAGENTPY_API_KEY`, `BASICAGENTPY_BASE_URL`,
`BASICAGENTPY_MODEL`, `BASICAGENTPY_DATA_DIR`, `BASICAGENTPY_CONFIG`.

## Configuration you can change

- `provider`: `baseUrl` (e.g. `https://ollama.com/v1`), `model`, `apiKey` or
  `apiKeyEnv` (name of an environment variable), `temperature`, `maxTokens`
  (optional cap on one reply; unset means the provider's own default is used),
  `timeoutMs`, `toolChoice` (omit it for Ollama).
- `agent`: `maxIterations`, `contextChars` (character budget for the stored
  conversation before old history is trimmed — not the model's context window),
  `maxResultChars`, `systemPrompt` / `systemPromptFile` (use these to
  rename/re-persona yourself; the default persona is in `persona-example.md`).
- `tools.files`: `roots` (`{path, access}` with `rw` / `ro` / `approval` /
  `deny`), `protectedPaths`, size caps.
- `tools.shell`: `enabled` (default false), `approval` (`always` / `never`),
  `timeoutMs`, `maxOutputBytes`, `denylist`.
- `tools.web`: `fetch_url` settings — `enabled` (default true), `approval`
  (`never` / `always`), `timeoutMs`, `maxBytes`, `maxRedirects`,
  `allowPrivate` (default false), `denyDomains`, `allowDomains`, `userAgent`.
- `skills.paths`: directories scanned for `<name>/SKILL.md`.
- `memory`: `enabled`, `dir`.

Precedence: defaults < config file < environment < CLI flags.

## Guardrails (enforced in code, not just by policy)

- File paths must fall inside a configured root; symlink escapes are blocked.
  When roots overlap, the most specific matching root decides the access level.
- Writes inside `protectedPaths` — and always your own config file and the
  memory data directory (`audit.jsonl`, facts, sessions) — require approval
  (when the path is inside an allowed root).
- `run_command` is off by default. When enabled, every call is shown and needs
  approval; approving a command is full trust, because a command can write
  anywhere.
- `fetch_url` is read-only and enabled by default: it refuses private or
  reserved network addresses (including after redirects and non-canonical IPv6
  forms) and pins the connection to the validated addresses. It always treats
  fetched content as untrusted data. It does not run JavaScript and sends no
  cookies. Disable it entirely with `tools.web.enabled: false` or `--no-web`.
- Every tool call is appended to `audit.jsonl`, including approval requests
  and their answers.
- The web interface (`basicagentpy-web`) binds to loopback by default. Every
  `/api` request needs the bearer token printed at startup, and Host/Origin
  checks block DNS rebinding from other local pages. It writes only the same
  config file and data directory the CLI uses.
- One-shot mode denies approval-required actions unless `--yes` is passed;
  `--yes` auto-approves in the REPL as well.

## Changing yourself

- **Config**: edit the config file with `edit_file` (approval is asked), then
  `/reload` in the REPL applies it; one-shot runs read it on next start. The
  web interface's Settings tab edits the same file, validating the values and
  live-reloading every session on save. Never claim a config change took
  effect without a reload or restart.
- **Name/persona**: set `agent.systemPrompt` or `agent.systemPromptFile`;
  the program, paths, tools and skills keep the name basicagentpy.
- **Source**: read `docs/ARCHITECTURE.md` first. Add tools in
  `basicagentpy/tools/<name>.py` and register them in
  `basicagentpy/tools/__init__.py`; add tests under `tests/`; keep `README.md`
  and `docs/` in sync; run `python -m unittest discover -s tests -v`.
- **Version**: `basicagentpy/__init__.py` `__version__` is the single source
  (the CLI reads it). Releases are tagged `vX.Y.Z`.

## Memory and sessions

Facts saved with `remember` appear in every future prompt; `search_memory` finds
past records and `forget` removes them. `--continue` resumes the last session's
user/assistant text only (no tool traffic); `--session <id>` pins a named
conversation; `--ephemeral` disables memory entirely. The web interface's
Memory tab lists facts (add and delete), session transcripts and the audit
log; each web turn re-reads `facts.jsonl` so tabs stay in sync.

## CLI

REPL: `basicagentpy`. One-shot: `basicagentpy "prompt"`. Web interface:
`basicagentpy-web` (or `python -m basicagentpy.web_cli`) serves
`http://127.0.0.1:8787` by default and opens it in the default browser
(`--no-open` skips that); `--port`, `--host` and `--token` adjust it. Guided
configuration: `basicagentpy-setup` (provider, model, key, shell access,
config/data dirs; tests the provider and can start the web interface).
Scripting: `basicagentpy --json "prompt"` emits JSONL events
(`assistant_delta`, `tool_start`, `tool_end`, `done`, `error`, `approval`). The
web interface streams the same events plus `approval_result` and `aborted`;
there the `approval` event carries an `id` answered with
`POST /api/approvals/:id`. Handy flags: `--no-web` (disable web fetch for one
run), `--no-color` (disable ANSI colors), `--continue`, `--session <id>`,
`--ephemeral`. REPL commands: `/help /new /reload /skills /tools /model
[name] /config /quit`.

From a source checkout, prefix the command with `python3.14 bin/` (for example
`python3.14 bin/basicagentpy`). Installing with
`python3.14 -m pip install -e .` is optional and only puts the three commands
on your `PATH`; the project runs directly from the source tree without it.

## Skills

A skill is `<dir>/SKILL.md` with frontmatter (`name`, `description`) and
instructions. Only the name and description are always visible; the body loads
through `load_skill`. This bundled skill lives in the project's
`skills/basicagentpy/`; personal skills go in `~/.config/basicagentpy/skills/`.

## Tests

`python -m unittest discover -s tests -v` is offline (fake provider; includes
the web server). The opt-in Ollama Cloud integration test needs
`BASICAGENTPY_LIVE=1` and `OLLAMA_API_KEY_CLOUD` (model `gemma4:31b`):
`BASICAGENTPY_LIVE=1 python -m unittest tests.live.test_ollama_cloud -v`.
