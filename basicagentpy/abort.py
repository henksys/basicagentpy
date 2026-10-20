# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
# Cancellation primitive used instead of JavaScript's AbortSignal.
#
# A turn owns one AbortToken.  Long-running operations register callbacks that
# close sockets or kill child processes, so Ctrl+C in the CLI and
# POST /api/chat/abort in the web interface stop a turn promptly.

from __future__ import annotations

import threading
from collections.abc import Callable


class AbortError(Exception):
    """Raised when the current turn is cancelled."""

    name = "AbortError"


class AbortToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    def abort(self) -> None:
        """Mark the token aborted and run every registered cancel callback."""
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # Cancellation must never fail because one closer raised.
                pass

    def throw_if_aborted(self) -> None:
        if self._event.is_set():
            raise AbortError("The operation was aborted")

    def is_aborted(self) -> bool:
        return self._event.is_set()

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a cancel callback; returns a function that removes it."""
        with self._lock:
            if self._event.is_set():
                run_now = True
            else:
                run_now = False
                self._callbacks.append(callback)
        if run_now:
            try:
                callback()
            except Exception:
                pass
        removed = False

        def remove() -> None:
            nonlocal removed
            with self._lock:
                if removed:
                    return
                removed = True
                try:
                    self._callbacks.remove(callback)
                except ValueError:
                    pass

        return remove
