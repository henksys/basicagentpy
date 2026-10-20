# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Approval helper for policy-restricted operations (guarded writes, shell
# commands). The CLI decides the mode:
#
#   REPL        interactive question, default answer is "no"
#   one-shot    deny by default; --yes approves automatically
#   --json      same as one-shot, plus an approval event for the script
#
# The returned function has the signature (description: str) -> bool.

from __future__ import annotations

from typing import Any, Callable


def create_approval(
    *,
    interactive: bool = False,
    yes: bool = False,
    question: Callable[[str], str] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_notice: Callable[[str], None] | None = None,
) -> Callable[[str], bool]:
    on_event = on_event or (lambda event: None)
    on_notice = on_notice or (lambda text: None)

    def approve(description: str) -> bool:
        if yes:
            on_event(
                {
                    "type": "approval",
                    "description": description,
                    "approved": True,
                    "automatic": True,
                }
            )
            return True
        if not interactive:
            on_event(
                {
                    "type": "approval",
                    "description": description,
                    "approved": False,
                    "automatic": True,
                }
            )
            on_notice(
                f"Approval required for: {description}\n"
                "This run cannot ask (one-shot mode). Re-run with --yes to allow approvals, or use the interactive REPL."
            )
            return False
        if question is None:
            return False
        answer = str(
            question(f"Approve this action?\n  {description}\n[y/N] ")
        ).strip().lower()
        approved = answer in ("y", "yes")
        on_event(
            {
                "type": "approval",
                "description": description,
                "approved": approved,
                "automatic": False,
            }
        )
        return approved

    return approve
