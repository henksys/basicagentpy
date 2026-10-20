---
name: example
description: >
  Template skill showing the SKILL.md format. Use when the user asks how to
  write a skill for basicagentpy or mentions this example skill.
---

# Example skill

This file demonstrates how skills work. It is loaded on demand: only the name
and description above are always visible to the model. When a skill looks
relevant, the model calls `load_skill` with the exact name and receives the
whole body you are reading now.

## How to write your own

1. Create a directory under one of the configured `skills.paths`, for example
   `~/.config/basicagentpy/skills/my-skill/`.
2. Put a `SKILL.md` inside it with frontmatter and instructions.
3. Run `/reload` in the REPL (or restart basicagentpy).

## Frontmatter fields

- `name`: the identifier passed to `load_skill` (required).
- `description`: when the model should use the skill. This text is always in
  the system prompt, so keep it short and specific.

The parser supports plain values, `|` literal blocks and `>` folded blocks.
Everything after the closing `---` is the instruction body.

## Notes for a skill author

- Skills are instructions, not executable code. The model can only do what its
  tools allow; mention which tool to use for each step.
- Scripts referenced by a skill can live next to SKILL.md. The `load_skill`
  result includes the skill's `dir` so the model can read or run them when the
  configured file roots and shell policy allow it.
