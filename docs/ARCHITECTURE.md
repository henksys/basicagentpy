# basicagentpy architecture

This document explains what every file does, how a conversation turn flows
through the system, and how to extend it. It is written to be useful both to a
developer and to an AI agent that is asked to modify this project.

`basicagentpy` is a Python 3.14 port of basicagent 1.3.6. It is an independent
project with its own config and data directories, but the module boundaries,
event stream, tool contract, file/web policies and JSONL formats intentionally
match the original.

## Big picture

```
                 +----------------------+
   prompt  --->  |  bin/basicagentpy    |  REPL / one-shot / --json
                 +----------+-----------+
                            |
                            v
                 +----------------------+     +----------------------+
                 | basicagentpy/agent   |---->| basicagentpy/provider|---> OpenAI-compatible
                 |   (the loop)         |<----| (chat completions)   |     /v1/chat/completions
                 +----------+-----------+     +----------------------+
                            |
               tool calls   v
                 +----------------------+     +----------------------+
                 | basicagentpy/tools   |---->| files / shell /      |
                 | registry + audit     |     | web / memory / skills|
                 +----------+-----------+     +----------------------+
                            |
                            v
                 +----------------------+
                 | basicagentpy/skills  |  SKILL.md discovery + load_skill
                 | basicagentpy/memory  |  JSONL facts + sessions
                 | basicagentpy/prompt  |  system prompt assembly
                 +----------------------+

   browser  --->  bin/basicagentpy-web ---> basicagentpy/web/server ---> basicagentpy/web/api
                  (HTTP server + UI)        (routing, auth)            (JSON endpoints)
                                                     |
                                                     v
                                              basicagentpy/web/session  one Agent + Memory
                                              (WebSession manager)      per conversation
```

There is no framework and no third-party dependency: Python's standard
`http.client`, `http.server`, `ssl`, `socket`, `subprocess`, `threading`,
plain JSONL persistence and `unittest` do all the work. `pyproject.toml` has
an empty `dependencies` list.

## Module map

| File | Responsibility |
| --- | --- |
| `bin/basicagentpy` | Source-tree CLI launcher. Adds the project root to `sys.path`, then calls `basicagentpy.cli.main`. |
| `bin/basicagentpy-web` | Source-tree web launcher. |
| `bin/basicagentpy-setup` | Source-tree setup launcher. |
| `basicagentpy/cli.py` | CLI entry point. Argument parsing, config loading, REPL, one-shot and `--json` modes, slash commands, Ctrl+C handling. All user-visible behavior lives here. |
| `basicagentpy/web_cli.py` | Web entry point. Flag parsing, token generation, startup banner, browser launch, signal handling. |
| `basicagentpy/setup_cli.py` | Onboarding entry point. stdin questions, provider connection test, writes the config, offers to start the web server. |
| `basicagentpy/setup.py` | Setup logic without I/O: provider presets, base-config loading (existing file or `config.example.json`), answer merging, the provider test, summary lines. |
| `basicagentpy/web/server.py` | HTTP server: static files from `web/`, host/origin checks, bearer-token auth for `/api`, routing to the API. |
| `basicagentpy/web/api.py` | JSON endpoints: status, chat streaming, approvals, sessions, facts, audit, config read/write. |
| `basicagentpy/web/session.py` | `WebSession` (one Agent + Memory + registry per conversation) and `SessionManager`; the approval bridge to the browser; disk helpers for browsing sessions. |
| `basicagentpy/web/open_browser.py` | Launches the token URL in the default browser (`xdg-open`/`open`/`start`); detached, failures are ignored. |
| `web/` | The frontend: hand-written HTML/CSS/JS, no build step. `web/assets/css/tokens.css` and the Marcellus SC font are copied from henksys.com so the UI matches the site's look; sync them when the site changes. |
| `basicagentpy/config.py` | Built-in defaults, config file loading and merging (defaults <- file <- env <- flags), validation and path normalization. |
| `basicagentpy/config_file.py` | Read/validate/write the raw config file; atomic writes, mode 0600. |
| `basicagentpy/provider.py` | The only place that talks HTTP to a model. One OpenAI-compatible `/v1/chat/completions` call with SSE streaming; rebuilds tool calls from deltas. |
| `basicagentpy/agent.py` | The agent loop: send messages + tool schemas, stream the answer, execute tool calls, feed results back, repeat. Applies iteration limits, the failure circuit breaker and context trimming. Also defines the event stream. |
| `basicagentpy/tool.py` | The `Tool` dataclass shared by the registry, skills and tools. |
| `basicagentpy/tools/__init__.py` | Tool registry: schema validation, dispatch, per-call audit log. |
| `basicagentpy/tools/files.py` | File tools plus the root-based path policy (`check_access`, `soft_realpath`). |
| `basicagentpy/tools/shell.py` | `run_command` with approval, timeout, denylist and output caps. |
| `basicagentpy/tools/web.py` | `fetch_url`: guarded HTTP fetching, SSRF checks, pinned connections, bounded reads, HTML-to-text extraction. Omitted when `tools.web.enabled` is false. |
| `basicagentpy/tools/memory_tools.py` | `remember`, `search_memory`, `forget`. |
| `basicagentpy/skills.py` | SKILL.md discovery, frontmatter parsing (plain, `>` and `|`), prompt index generation, `load_skill` tool. |
| `basicagentpy/memory.py` | JSONL persistence: `facts.jsonl`, `sessions/<id>.jsonl`, `last-session.txt`, `audit.jsonl`. |
| `basicagentpy/prompt.py` | System prompt assembly from persona + environment + skills + facts + tool guidance + safety rules. |
| `basicagentpy/approval.py` | Approval policy for guarded operations (interactive / deny / `--yes`). |
| `basicagentpy/ui.py` | Terminal rendering for human and JSON modes. |
| `basicagentpy/util.py` | Small helpers: UTF-8 `bounded`, path expansion, compact JSON, JS-compatible lengths, previews, masking, timestamps. |
| `basicagentpy/abort.py` | `AbortToken`, the Python equivalent of `AbortSignal`, with close/kill callbacks. |
| `skills/basicagentpy/SKILL.md` | The bundled self-help skill. |
| `skills/example/SKILL.md` | Working skill template. |
| `config.example.json` | Reference configuration. |
| `tests/` | Offline tests with a fake SSE provider; `tests/live/` holds an opt-in live test. |

## One turn, step by step

1. The CLI collects a prompt (argument, stdin pipe, or the REPL line).
2. `build_system_prompt` is called fresh each turn, so new facts and skill
   changes are picked up without a restart.
3. `Agent.run` pushes the user message and enters the loop:
   1. `Provider.chat` streams the model answer, emitting `assistant_delta`
      events, and returns `{content, toolCalls, finishReason, usage}`.
   2. If there are no tool calls, the loop emits `done` and returns.
   3. Otherwise each tool call is parsed, validated and executed through the
      registry; each result is appended as a `role: "tool"` message.
   4. The loop repeats (up to `agent.maxIterations`).
4. The CLI streams text to stdout, tool activity to stderr, appends the turn
   to the memory session, and reprompts.

### Hard limits inside the loop

- **maxIterations** (`agent.maxIterations`, default 25) caps one turn.
- **Circuit breaker**: the same tool name + arguments failing three times
  aborts the turn with an `AgentError`.
- **Context trimming** (`agent.contextChars`): old tool results become a
  placeholder first, then whole old exchanges are dropped. Tool messages are
  removed together with the assistant message that requested them so the
  provider never sees an orphaned `tool` message. The metric is a character
  count of the stored messages only — the system prompt, skills and facts are
  sent on top and not counted, and characters are not tokens — and it neither
  reflects nor queries the provider's context window. `provider.maxTokens`
  is a separate, optional per-request output cap. The same serialized-size
  metric (`context_size`) is reported on every `iteration` event and drives
  the context gauge in the web interface.
- **Abort**: Ctrl+C in the REPL (or SIGINT in one-shot) aborts the current
  `AbortToken`; the provider stream and any running command are closed/killed.

### Event stream

Every event the agent emits is rendered by `basicagentpy/ui.py`. In `--json`
mode it is also written to stdout as one JSON object per line:

| Event | Fields |
| --- | --- |
| `iteration` | `iteration`, `contextSize` (serialized conversation chars) |
| `assistant_delta` | `text` |
| `tool_start` | `name`, `args` |
| `tool_end` | `name`, `ok`, `durationMs`, `result` |
| `warning` | `message` |
| `done` | `text` |
| `error` | `message` |
| `approval` | `description`, `approved`, `automatic` (CLI) |
| `approval` | `id`, `description`, `automatic` (web; answer with `POST /api/approvals/:id`) |
| `approval_result` | `id`, `approved` (web only) |
| `aborted` | (web only; the turn was cancelled) |

Scripts should treat `done` as the final assistant answer and `error` as a
failed run (the CLI also exits 1).

## Web interface

`basicagentpy-web` serves the UI in `web/` plus a JSON API on loopback
(default `127.0.0.1:8787`) and requires `Authorization: Bearer <token>` on
every `/api` request. Static files are deliberately unauthenticated (no
secrets); the shell can be opened by anything, the API cannot.

Security model — the API can drive an agent with file and shell tools, so it
is treated like local shell access:

- Host header must be the loopback address the server is bound to (blocks
  DNS-rebinding from web pages); cross-origin requests to `/api` are rejected.
- There are no cookies, so there is no CSRF surface; the token travels in the
  URL fragment (never sent to the server) and is kept in `sessionStorage`.
- `--host` with a non-loopback address skips the Host/Origin checks (the
  user's explicit choice) but never the token.
- All model and tool output is rendered with `textContent`, never
  `innerHTML`.

| Endpoint | Notes |
| --- | --- |
| `GET /api/status` | model, cwd, data dir, tools, skills, last session. |
| `POST /api/chat` | one turn; NDJSON stream with the events above. 409 while busy. |
| `POST /api/chat/abort` | cancels the running turn (also on client disconnect). |
| `POST /api/approvals/:id` | answers a paused approval; expired/unknown ids 404. |
| `GET/POST /api/sessions`, `GET /api/sessions/:id` | session list and transcripts from `sessions/*.jsonl`. |
| `DELETE /api/sessions/:id` | removes one stored conversation and its in-memory session; the `--continue` pointer moves to the newest remaining session (409 while a turn runs). |
| `GET/PUT /api/facts`, `DELETE /api/facts/:key` | fact store via `Memory` (exact-key delete). |
| `GET /api/audit` | parsed `audit.jsonl`, newest first, `limit`/`tool`/`ok` filters. |
| `GET/PUT /api/config` | raw file + effective (masked) config; PUT validates, writes atomically and live-reloads every session. |

Sessions are never replayed from tool traffic: a `WebSession` loads only the
persisted user/assistant text, exactly like `--session` in the CLI. Approvals
and the API event objects differ slightly from the CLI (`id` instead of
`approved`), noted in the event table. Facts are shared between sessions
through `facts.jsonl`; each turn calls `Memory.reload_facts()` so tabs see
other sessions' changes. Concurrent turns in different tabs can still race on
the last write (atomic rename, last writer wins) — acceptable for a
single-user local tool.

## Provider contract

`basicagentpy/provider.py` speaks one dialect well and tolerates the known
variations between servers:

- request: `{ model, messages, stream: true, temperature }` plus `tools` when
  tools exist and `max_tokens` only when `provider.maxTokens` is set (unset =
  the provider's own default output limit). `tool_choice` is only sent when
  configured, because Ollama rejects it.
- response: SSE `data:` lines, `[DONE]` terminator, `choices[0].delta`.
- tolerated quirks: missing/repeated tool call ids, names split across deltas,
  arguments delivered as objects, missing trailing newline, `content` given as
  an array of parts.

Tested against: Ollama (local and cloud), llama.cpp server, DeepSeek, OpenAI.
For llama.cpp, start the server with `--jinja` so tool calling works.

The HTTP/SSE layer uses `http.client`:

- an incremental line reader, because Python has no built-in SSE client;
- a total deadline timer that closes the raw socket;
- `AbortToken` callbacks that close the raw socket, so Ctrl+C and the web
  Abort button interrupt a blocked `readline()` promptly;
- redirect following with the same small set of fetch-like rules;
- `Accept-Encoding: identity` so a proxy cannot turn the stream into a gzip
  blob.

## Tool contract

A tool is a `Tool` dataclass:

```python
Tool(
    name="read_file",
    description="Shown to the model; say when to use the tool and what it returns.",
    parameters={
        "type": "object",
        "properties": {...},
        "required": [...],
        "additionalProperties": False,
    },
    run=lambda args, ctx: {...},
)
```

- `ctx["signal"]` — `AbortToken` for the current turn; long operations must
  honor it.
- `ctx["approve"](description)` — asks the user for approval; returns a bool.

The registry (`basicagentpy/tools/__init__.py`) validates arguments against
`parameters` before running anything and rejects unknown arguments. Throwing
inside `run` does not crash the turn: the error text is returned to the model
as the tool result so it can correct itself. Three identical failures trip the
circuit breaker.

### Adding a tool

1. Create `basicagentpy/tools/my_tool.py` exporting a factory that returns
   `Tool` objects.
2. Register it in `create_registry` in `basicagentpy/tools/__init__.py`.
3. Add a unit test under `tests/`.
4. If the tool can change user data, think about approval and root policy first.

### Built-in tools

| Tool | Notes |
| --- | --- |
| `read_file` | Line-numbered output, 24 KB cap, ranges for large files. |
| `write_file` | Creates parent directories. Use for new files/full rewrites. |
| `edit_file` | Exact fragment replacement; refuses ambiguous matches unless `replace_all`. |
| `append_file` | Only appends; never creates files. |
| `list_dir` | Sorted, capped listing; directories get a trailing `/`. |
| `rename_file` | Refuses existing destinations and cross-filesystem moves. |
| `run_command` | Disabled by default; argv only, no shell; approval + timeout + denylist. |
| `fetch_url` | Enabled by default; http(s) only; SSRF check on the URL and every redirect; domain allow/deny; bounded stream + text caps; no JS/cookies. |
| `remember` / `search_memory` / `forget` | Registered only when memory is enabled. |
| `load_skill` | Registered only when skills are found. |

## File access policy

Every file path is resolved and authorized by `check_access` in
`basicagentpy/tools/files.py`:

1. `~` expands; relative paths resolve against the working directory.
2. `soft_realpath` resolves the path (or its nearest existing parent, following
   dangling symlinks explicitly) so symlinks cannot escape a root.
3. The path must be inside one of `tools.files.roots`; the **most specific
   matching root** (the longest resolved root path) decides the access level:
   `rw` (free), `ro` (read only), `approval` (writes ask), `deny` (refuse).
   This keeps a broad root from silently shadowing a narrower restrictive one,
   regardless of config order.
4. Writes inside `tools.files.protectedPaths` — and always inside the active
   config file and the memory data directory (`memory.dir`, which holds facts,
   sessions and `audit.jsonl`) — require approval even in an `rw` root.

Paths outside every root are refused with an error the model can read.

**Shell caveat:** approving a `run_command` is full trust; a command can write
anywhere, including outside all roots. The roots constrain only the file tools.
Keep `tools.shell.enabled` false unless you need it.

## Web access policy

`fetch_url` (`basicagentpy/tools/web.py`) is enabled by default and read-only.
Every request — including each redirect hop — passes the same checks:

1. **URL**: `http:`/`https:` only, no embedded credentials; `localhost` names
   refused.
2. **Domains**: `denyDomains` always wins; when `allowDomains` is non-empty it
   becomes an allowlist. Entries match the domain and its subdomains;
   `*.example.com` also matches the apex.
3. **Addresses**: the hostname is resolved with `socket.getaddrinfo` and
   refused when any result is private or reserved: loopback, RFC 1918, CGNAT,
   link-local (including the `169.254.169.254` cloud metadata address),
   TEST-NETs, multicast, ULA, IPv6 link-local, NAT64, 6to4, Teredo and
   IPv4-compatible ranges. Classification uses `ipaddress` plus explicit CIDR
   lists, so expanded and mapped IPv6 forms are handled.
   `tools.web.allowPrivate` disables this and is meant only for trusted local
   testing.
4. **Pinning**: the HTTP/HTTPS connection is opened to the exact address that
   passed the check, with the original hostname used for the `Host` header and
   TLS SNI/certificate verification. A second DNS answer cannot redirect it
   (no rebinding TOCTOU).
5. **Bounds**: redirects are followed manually, at most `maxRedirects` hops;
   the body is streamed with a `maxRawBytes` cap (the rest is abandoned); the
   sanitized text is capped at `maxBytes` and flagged `truncated`.
6. **Content**: only text/html, xhtml+xml, xml, plain, markdown and JSON are
   accepted. Charset comes from the Content-Type header or a `<meta charset>`
   sniff and is decoded with Python's codecs.

HTML is converted to text by an ordered regex pipeline: comments/doctype/CDATA,
then `head|script|style|noscript|template|svg|iframe`, then a `<main>` or
`<article>` region when present, then block structure becomes newlines
(`<li>` becomes `- `, headings become `#`), then tags are stripped and entities
are decoded in a single pass (no double decoding of `&amp;lt;`).

Deliberate limitations: regex handling is best-effort and can leak markup on
pathological HTML; there is no JavaScript rendering, no cookie/auth support and
no robots.txt handling. Setting `tools.web.enabled: false` (or `--no-web`)
removes the tool from the schema entirely; `approval: "always"` asks for a y/N
before each fetch.

## Skills

A skill is a directory with a `SKILL.md`:

```markdown
---
name: my-skill
description: >
  When the model should use this skill.
---

Instructions for the model, in markdown.
```

- `discover_skills` scans every path in `skills.paths` for `<name>/SKILL.md`
  (or a direct path to a `SKILL.md`). Missing paths are ignored; broken files
  never prevent startup; earlier paths win on duplicate names.
- `load_config` always appends `BUNDLED_SKILLS_DIR` — the project's own
  `skills/` directory, resolved from `basicagentpy/config.py` — after the
  configured paths. This keeps the bundled `basicagentpy` self-help skill and
  the `example` template available no matter which directory basicagentpy runs
  in, including when a user config replaces `skills.paths` entirely.
- Symlinked skill directories are followed, which is how many agents
  (including Omarchy's `/usr/share/omarchy/default/agents/skills`) install
  skills.
- Only `name` + `description` go into the system prompt. The full body is
  loaded on demand with `load_skill`, which also returns the skill's `dir` so
  referenced scripts can be read with `read_file`.
- The frontmatter parser supports plain values, folded `>` and literal `|`
  blocks — the same shape used by Claude/Omarchy-style skills.

## Memory format

Under `memory.dir` (default: `$XDG_DATA_HOME/basicagentpy`, falling back to
`~/.local/share/basicagentpy`; settable per agent via the config file,
`BASICAGENTPY_DATA_DIR` or `--data-dir`; mode `0700`):

| File | Content |
| --- | --- |
| `facts.jsonl` | `{"key","value","at"}` per line, rewritten atomically. |
| `sessions/<id>.jsonl` | `{"ts","role","content"}` per user/assistant message. |
| `last-session.txt` | session id used by `--continue`. |
| `audit.jsonl` | every tool call: `{ts, tool, args, ok, error, durationMs, approvals}`. |

Resuming replays only user/assistant text; tool traffic is not replayed.
`forget` rewrites facts and session files to remove matching lines.

## CLI modes

- **REPL** (`basicagentpy`): interactive; commands `/help /new /reload /skills
  /tools /model [name] /config /quit`. Ctrl+C cancels the running turn;
  Ctrl+D exits. `/reload` re-reads the config file, skills, provider and
  prompt while keeping the conversation.
- **One-shot** (`basicagentpy "..."` or a pipe): runs one turn and exits.
  Answer on stdout, tool trace on stderr, exit code 0/1.
- **JSON** (`--json`): one-shot with one JSON event per line on stdout. This
  is the mode to embed in other programs.

The REPL runs the turn in a worker thread while a small input thread feeds the
main event loop. This keeps the original behaviour: output and approval prompts
stream while the user can still type, busy input gets the "Still working"
message, and Ctrl+C aborts the active turn. The main event loop handles
signals by setting an abort flag on the current `AbortToken`.

## Configuration reference

See `config.example.json` and the table in `docs/TUTORIAL.md`. Precedence:
defaults, config file, environment (`BASICAGENTPY_API_KEY`,
`BASICAGENTPY_BASE_URL`, `BASICAGENTPY_MODEL`, `BASICAGENTPY_DATA_DIR`,
`BASICAGENTPY_CONFIG`), CLI flags.

## Testing

- `python3.14 -m unittest discover -s tests -v` — 128 offline tests: config
  merging, SSE parsing, the agent loop, the path policy, skills, memory, the
  web server (auth, streaming, approvals, config save), the CLI end to end
  against a fake in-process provider, and the setup questionnaire.
- `BASICAGENTPY_LIVE=1 python3.14 -m unittest tests.live.test_ollama_cloud -v`
  — opt-in integration test against Ollama Cloud (`OLLAMA_API_KEY_CLOUD`,
  default model `gemma4:31b`). It creates a file with `write_file` and reads it back with
  `read_file`.

## Design notes

- The core is synchronous; the web server and REPL use threads. This keeps the
  provider/tool code easy to read and preserves the original's blocking tool
  semantics. Long-running tools must honor `ctx["signal"]`.
- The provider and web-fetch clients deliberately avoid third-party HTTP
  libraries. `http.client` plus explicit timeouts, raw-socket cancellation and
  a pinned connect path cover the project's needs.
