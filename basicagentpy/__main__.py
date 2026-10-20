# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Allow `python -m basicagentpy` to run the CLI.

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
