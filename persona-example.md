# Default persona

This is the default persona of basicagentpy — the paragraph the model receives
at the start of its system prompt. It is used when both `agent.systemPrompt`
and `agent.systemPromptFile` are empty. The source of truth is
`DEFAULT_PERSONA` in `basicagentpy/prompt.py`.

> You are basicagentpy, a small command line assistant with file and
> (optionally) command tools. Be precise, direct and concise. Work step by
> step: inspect before you change, then verify the result. Never claim
> something succeeded unless a tool result shows it succeeded. When you need
> information that a tool can provide, call the tool instead of guessing.

## Giving the agent another name

There are two ways to rename the agent or to teach it your own name:

1. **Replace the persona** — set `agent.systemPrompt` in the config file, or
   copy this file and point `agent.systemPromptFile` at the copy. Only the
   persona paragraph is replaced; the environment, skills, saved facts, tool
   guidance and safety sections are still appended by the program.
2. **Save it as a fact** — lighter weight, and the built-in persona stays in
   place. Facts are stored as key/value pairs and are injected into the system
   prompt of every run under "Saved facts", so they are easy to add and change:

   | Key | Value | Effect |
   | --- | --- | --- |
   | `agent_name` | `Sky` | the agent calls itself Sky |
   | `user_name` | `Jim` | the agent addresses you by name |

   Add them in the web interface (Memory tab, facts) or ask in a conversation:
   "remember that your name is Sky" or "remember that my name is Jim". The
   agent stores the fact with the `remember` tool; saving an existing key
   overwrites it, and the Memory tab's delete button (or `forget`) removes it
   again.

The persona replacement is deterministic and part of the configuration; a fact
is a preference the agent applies when relevant, and it can be updated
mid-conversation.
