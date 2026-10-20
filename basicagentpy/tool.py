# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# The tool contract shared by local tools, skills and the registry.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[dict[str, Any], dict[str, Any]], Any]

    def execute(self, args: dict[str, Any], ctx: dict[str, Any]) -> Any:
        return self.run(args, ctx)
