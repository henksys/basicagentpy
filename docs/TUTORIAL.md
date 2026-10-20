# basicagentpy tutorial

A five minute setup guide, then the features you will actually use.

## Requirements

- **[Python 3.14 or newer](https://www.python.org/downloads/)** (`python3.14 --version`). Nothing else: no
  `pip install`, no dependencies, no build step.

The examples use `python3.14`; use `python` on systems where that is 3.14+.

## 1. Set it up

The guided setup walks you through everything with a handful of questions
(provider, model, API key, shell access, config and data directories), writes
the config file, tests the connection and then offers to start the web
interface in your browser:

```sh
python3.14 bin/basicagentpy-setup
```

You can also install the commands on your `PATH` (optional):

```sh
python3.14 -m pip install -e .
basicagentpy-setup
```

Every question shows a `[default]`; press Enter to accept it. The defaults
come from your existing config file, or from `config.example.json` on a fresh
install. For a local Ollama the whole setup is Enter-only. If you prefer to
configure by hand, section 3 lists the provider recipes.

## 2. Run it

From the project directory:

```sh
python3.14 bin/basicagentpy
```

Or, after `pip install -e .`:

```sh
basicagentpy
```

You can also run one-shot without the REPL:

```sh
python3.14 bin/basicagentpy "list the files in this directory and summarize the project"
```

Without configuration basicagentpy expects a local Ollama at
`http://localhost:11434/v1`; run the setup in section 1, or pick a provider
recipe in section 3.

## 3. Pick a provider

The setup in section 1 writes this for you; these are the recipes for a manual
config file. Create the config directory and copy the example:

```sh
mkdir -p ~/.config/basicagentpy
cp config.example.json ~/.config/basicagentpy/config.json
```

Then edit `provider` in `~/.config/basicagentpy/config.json`.

**Ollama Cloud** (no local install needed):

```json
"provider": {
  "baseUrl": "https://ollama.com/v1",
  "apiKeyEnv": "OLLAMA_API_KEY_CLOUD",
  "model": "gemma4:31b"
}
```

The key is read from the named environment variable at startup, so it stays
out of the file. Any variable works: `OLLAMA_API_KEY_CLOUD` here,
`DEEPSEEK_API_KEY` below, and so on.

**Local Ollama**:

```json
"provider": { "baseUrl": "http://localhost:11434/v1", "model": "qwen3:8b" }
```

Ollama ignores the API key; no key needed.

**llama.cpp server** (`llama-server --jinja -m model.gguf`):

```json
"provider": { "baseUrl": "http://localhost:8080/v1", "model": "local" }
```

**DeepSeek**:

```json
"provider": {
  "baseUrl": "https://api.deepseek.com/v1",
  "apiKeyEnv": "DEEPSEEK_API_KEY",
  "model": "deepseek-chat"
}
```

**OpenAI**:

```json
"provider": {
  "baseUrl": "https://api.openai.com/v1",
  "apiKeyEnv": "OPENAI_API_KEY",
  "model": "gpt-5.1"
}
```

Quick sanity check without touching config:

```sh
BASICAGENTPY_BASE_URL=https://ollama.com/v1 \
BASICAGENTPY_API_KEY="$OLLAMA_API_KEY_CLOUD" \
python3.14 bin/basicagentpy --model gemma4:31b "say hello in one short sentence"
```

### Give your agent a different name

The program is always `basicagentpy`, but the persona the model adopts is
yours. Set `agent.systemPrompt` (or `agent.systemPromptFile` pointing at a text
file) to replace the default "You are basicagentpy, ..." paragraph:

```json
"agent": { "systemPrompt": "You are Alfred, a concise command line assistant." }
```

Only the persona paragraph is replaced; the environment, skills and safety
sections are still appended. The bundled `basicagentpy` self-help skill also
follows the configured persona while keeping the program's paths and tooling.
The default paragraph is written out in `persona-example.md`; copy it if you
want to tweak the wording instead of replacing it entirely.

### Output cap vs. context budget

`provider.maxTokens` and `agent.contextChars` both sound like limits, but they
control different things:

- `provider.maxTokens` is an optional cap on **one model answer**. When set it
  is sent as `max_tokens` with the request; when it is unset the provider
  applies its own default output limit (for example, DeepSeek defaults to 4096
  with a maximum of 8192). The setup tool does not ask for it — set it in the
  config file or the web Settings if you want a cap. Tool-call arguments count
  as output, so a low cap can truncate a large `write_file` or `edit_file`, and
  a value above what the provider allows may be rejected with an HTTP 400.
- `agent.contextChars` (default `100000`) is the **conversation budget** before
  basicagentpy trims old history. It counts the serialized characters of the
  stored messages — not tokens — and is *not* the model's context window:
  basicagentpy never asks the provider for that, it just drops old context when
  the budget is full. Older tool results are replaced with
  `[tool result trimmed to save context]` first, then the oldest exchanges are
  dropped (the newest user message and answer are always kept).

The system prompt, skills index and saved facts are sent on top and are not
counted against `agent.contextChars`. The web interface shows this budget as a
gauge in the composer, e.g. `context 20% (25k/100k chars)`; it turns red at
90%, and at 100% old messages start being dropped. Changing `maxTokens` does
**not** move the gauge — change `agent.contextChars` for that, and size it to
your model's real context window (roughly 4 characters per token) minus some
headroom.

## 4. Use the REPL

```sh
basicagentpy
```

```
basicagentpy 0.1.0
model     gemma4:31b @ https://ollama.com/v1
cwd       /home/you/Projects/basicagentpy
skills    basicagentpy, example, omarchy, ...
basicagentpy> read pyproject.toml and tell me what scripts it defines
```

While the agent works you see a trace on stderr:

```
[tool] read_file {"path":"basicagentpy/provider.py"}
[tool] read_file ok (3ms)
```

Commands:

| Command | Effect |
| --- | --- |
| `/help` | List commands |
| `/new` | Start a new conversation session |
| `/reload` | Re-read the config file, skills and provider (keeps history) |
| `/skills` | List discovered skills |
| `/tools` | List registered tools |
| `/model [name]` | Show or change the model for this session |
| `/config` | Show the resolved config with the API key masked |
| `/quit` | Exit (Ctrl+D works too) |

Ctrl+C cancels the running turn, not the program.

## 5. Use the web interface

basicagentpy also ships a browser interface for the same agent (the setup in
section 1 can start it for you):

```sh
python3.14 bin/basicagentpy-web
```

It listens on `127.0.0.1:8787` by default and prints a URL that includes an
access token in the fragment; it also opens that URL in your default browser
automatically (pass `--no-open` to skip that, e.g. in scripts or over SSH).
The token is required for every API request, and the unlock screen accepts it
too. The interface has three tabs:

- **Chat** — sessions in a collapsible sidebar, streamed answers, every tool
  call with its arguments and result, and approve/deny buttons when a guarded
  action asks. Stop cancels the running turn and closing the tab aborts it.
  It uses the same sessions as the CLI, so you can switch between the two. A
  small context gauge in the composer shows how full the auto-trim budget
  (`agent.contextChars`) is — not the model's context window, and unrelated to
  `provider.maxTokens`.
- **Settings** — edits the same `~/.config/basicagentpy/config.json` (provider,
  agent limits, file roots, shell, web tool, skills, memory). Saving validates
  the values, writes the file and live-reloads the running agent. The API key
  is masked and never sent to the browser; leave the field empty to keep it.
- **Memory** — the facts from `facts.jsonl` (view, add, delete), session
  transcripts and the audit log with filters.

Options: `--port <number>` (default 8787), `--host <address>` (default
`127.0.0.1`), `--token <string>` (default: random, printed at startup) and
`--no-open` (do not launch the browser).
The server binds to loopback unless you pass a non-loopback `--host`. Every
`/api` request needs the bearer token, and Host/Origin checks reject
DNS-rebinding attempts from other local pages. There are no cookies (the
token lives in the URL fragment and `sessionStorage`), and everything the
agent renders is inserted as text, never HTML.

## 6. One-shot and scripting

```sh
# Answer on stdout, tool trace on stderr:
basicagentpy "create notes/todo.md with three bullet points about this project"

# Pipeline input:
echo "what files changed today?" | basicagentpy

# Machine-readable events for scripts:
basicagentpy --json "read README.md and return its first heading" | jq -c '.type'
```

`--json` emits one JSON object per line: `assistant_delta`, `tool_start`,
`tool_end`, `done`, `error`, `approval`. The final answer is the `done` event.
Exit code is 0 on success and 1 on failure. The web interface streams the same
events plus `approval_result` and `aborted`; there the `approval` event carries
an `id` that is answered with `POST /api/approvals/:id`.

One-shot mode never waits for approval. Actions that need approval are denied
unless you pass `--yes` (only do that in trusted, non-interactive contexts).

## 7. Skills

Skills are folders with a `SKILL.md` (frontmatter + markdown instructions).
basicagentpy injects only their name and description into the prompt and loads
the full text when the model calls `load_skill`.

The default skills paths are:

```
./skills                              project skills (relative to your working directory)
~/.config/basicagentpy/skills         your personal skills
~/.agents/skills, ~/.claude/skills    where many agents install skills
```

In addition, the project's own `skills/` directory is always scanned (resolved
from the install location, not from your working directory), and it is scanned
after the paths above, so a skill with the same name in your paths wins. It
ships two skills:

- `basicagentpy` — the agent's self-help: its own configuration, memory,
  guardrails and how to extend it. Ask things like "where is your config
  stored?" or "how do I add a tool?".
- `example` — a template showing the `SKILL.md` format.

If for example the Omarchy skill lives in `~/.agents/skills` on your machine, it is
discovered automatically (symlinked directories are followed). To add your
own:

```sh
mkdir -p ~/.config/basicagentpy/skills/deploy
cat > ~/.config/basicagentpy/skills/deploy/SKILL.md <<'EOF'
---
name: deploy
description: Use when the user asks to deploy this project or roll back a release.
---

# Deploying

1. Run the tests first with run_command.
2. ...
EOF
```

Then `/reload` in the REPL (or just start a new run). See
`skills/example/SKILL.md` for a fuller template.

## 8. Guardrails

```json
"tools": {
  "files": {
    "roots": [
      { "path": ".",        "access": "rw" },
      { "path": "~/Projects", "access": "rw" },
      { "path": "~/.config",  "access": "approval" }
    ],
    "protectedPaths": ["~/.ssh"]
  },
  "shell": { "enabled": false, "approval": "always" },
  "web": {
    "enabled": true,
    "approval": "never",
    "denyDomains": [],
    "allowDomains": [],
    "allowPrivate": false
  }
}
```

- `rw` free access, `ro` read only, `approval` writes ask first, `deny` blocked.
  Paths outside all roots are always refused. When roots overlap, the most
  specific (longest) matching root decides, so a broad `rw` root cannot
  silently shadow a narrower `deny` or `approval` root.
- `protectedPaths` force a prompt even inside an `rw` root. The active
  basicagentpy config file and the memory data directory (`memory.dir`, holding
  facts, sessions and the audit log) are protected automatically, so the agent
  can change its own record only with your consent.
- `shell` is off by default. When enabled, every command is shown and needs a
  `y` in the REPL. Approving a command is full trust: a command can write
  anywhere, so allow it only if you accept that.
- `fetch_url` (web) is read-only and on by default. It refuses private and
  reserved network addresses (before and after redirects, including
  non-canonical IPv6, NAT64, 6to4 and Teredo forms) and the connection is
  pinned to the validated addresses so DNS cannot be rebound. It never runs
  JavaScript or sends cookies. Restrict it with `denyDomains` /
  `allowDomains`, or turn it off entirely with `"enabled": false` or the
  `--no-web` flag for one run. Set `"approval": "always"` to confirm each
  fetch. `allowPrivate` is a testing escape hatch — leave it false.
- Every tool call is appended to `~/.local/share/basicagentpy/audit.jsonl`,
  including approval requests and their answers.

## 9. Memory

basicagentpy saves long-term facts (via the `remember` tool) and user/assistant
text per conversation session under `$XDG_DATA_HOME/basicagentpy` (falling back
to `~/.local/share/basicagentpy`), created with mode 0700. The files are plain
JSON:

| File | Contents |
| --- | --- |
| `facts.jsonl` | long-term facts, injected into the system prompt of every run |
| `sessions/<id>.jsonl` | user/assistant text of one conversation |
| `last-session.txt` | session pointer used by `--continue` |
| `audit.jsonl` | one line per tool call: tool, args, ok/error, duration, approvals |

The directory can be set per agent, in the usual precedence order: `--data-dir`
flag, then `BASICAGENTPY_DATA_DIR`, then `memory.dir` in the config file, then
the XDG-aware default above:

```sh
basicagentpy --data-dir ~/agents/scout/data "what do you remember?"
BASICAGENTPY_DATA_DIR=~/agents/scout/data basicagentpy "..."   # same, via env
```

```sh
basicagentpy --continue "what did we work on last time?"   # resume last session
basicagentpy --session project-x                            # named session
basicagentpy --ephemeral "just answer, store nothing"       # in-memory only
```

Ask the agent to "remember that I prefer tabs" and it stores a fact; ask it to
"forget my tab preference" and it removes matching records. Resuming replays
only user/assistant text, not tool traffic. Everything is plain JSONL — grep
or delete files directly if you prefer.

Facts are also a lightweight way to rename the agent or teach it your name:
save `agent_name` / `user_name` (for example in the web interface's Memory
tab) and they appear in the system prompt of every run — see
`persona-example.md`.

## 10. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Could not reach provider ...` | Server not running or wrong `baseUrl`. Start Ollama or fix the URL. |
| `Provider HTTP 401` | Wrong or missing API key. Check `apiKeyEnv` and the variable. |
| Model answers but never uses tools | Some models are weak at tool calling. Try a stronger model; for OpenAI/DeepSeek set `"toolChoice": "required"` to force a call; Ollama ignores `tool_choice`. |
| `Tool arguments are not valid JSON` | Local model quirk. The error is returned to the model so it can retry; a stronger model usually fixes it. |
| llama.cpp never calls tools | Start it with `--jinja`. |
| `The run_command tool is disabled` | Set `tools.shell.enabled` to `true`. |
| Approval denied in a script | One-shot denies approvals. Use `--yes`, or run the REPL. |
| `Path is outside the configured file roots` | Add the directory to `tools.files.roots`, or run with `--cwd` inside an allowed root. |
| Agent says it has no web tool | `fetch_url` was removed via `tools.web.enabled: false` or `--no-web`. |
| `Blocked private or reserved address` / `Domain is in the deny list` | The web guardrails refused the URL. Adjust `allowDomains` / `denyDomains` deliberately, or use `allowPrivate` only for local testing. |
| Fetched page looks empty or garbled | The page likely renders with JavaScript (unsupported) or served an unusual encoding. Try the raw API/URL instead. |
| `Unsupported content type` when fetching | Only html, xhtml, xml, plain, markdown and JSON are accepted. |
| Timeouts on slow models | Raise `provider.timeoutMs`. |
| Answers or tool calls are truncated | The provider's default output cap is low (DeepSeek defaults to 4096). Set `provider.maxTokens` to the provider's maximum (DeepSeek 8192). |
| `Provider HTTP 400` mentioning `max_tokens` | `provider.maxTokens` is above what the model allows. Lower it, or remove the setting to use the provider default. |

## 11. Tests

```sh
python3.14 -m unittest discover -s tests -v          # offline suite, no network
BASICAGENTPY_LIVE=1 python3.14 -m unittest tests.live.test_ollama_cloud -v  # opt-in: also needs OLLAMA_API_KEY_CLOUD
```

The offline suite mirrors the original basicagent test suite: 124 tests plus
one opt-in Ollama Cloud integration test. It uses a local fake SSE provider
and never needs network access.
