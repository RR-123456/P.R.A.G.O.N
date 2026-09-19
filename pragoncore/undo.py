"""
pragoncore/undo.py — one shared undo stack for actions that change state.

WHY THIS EXISTS
    A voice assistant misunderstands sometimes. When it does, "sorry" isn't a
    remedy — the file is already in another folder. Before this module the
    only recovery was to fix it by hand.

    The alternative — asking "are you sure?" before every file op — is worse:
    every confirmation is a round trip, and an assistant that double-checks
    before renaming a file is one people stop using. So: act immediately,
    remember how to reverse it, and let the user say "undo that". Real
    confirmation (pragoncore/confirm_gate.py) is reserved for the handful of
    actions that genuinely can't be reversed at all (shutdown, restart).

HOW AN ACTION OPTS IN
    Actions don't need to know anything about this file's internals — they
    just capture the "before" state and hand back a zero-argument callable:

        from pragoncore.undo import push_undo
        shutil.move(str(src), str(dst))
        push_undo(f"moved {src.name} -> {dst}", lambda: shutil.move(str(dst), str(src)))

    Only the action itself knows that the reverse of "move A to B" is "move B
    to A" — that part can't be centralised. What's central here is the stack,
    the ordering, and the thread safety.

COST
    Pushing is a list append behind a lock — microseconds. Nothing in here
    runs until the user actually says "undo".
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

_DEFAULT_MAX_DEPTH = 25


@dataclass
class _Entry:
    label: str
    revert: Callable[[], None]


class UndoStack:
    def __init__(self, max_depth: int = _DEFAULT_MAX_DEPTH):
        self._stack: list[_Entry] = []
        self._lock = threading.Lock()
        self._max_depth = max_depth

    def push(self, label: str, revert: Callable[[], None]) -> None:
        with self._lock:
            self._stack.append(_Entry(label, revert))
            if len(self._stack) > self._max_depth:
                self._stack.pop(0)

    def undo(self) -> str:
        with self._lock:
            if not self._stack:
                return "There's nothing to undo."
            entry = self._stack.pop()
        try:
            entry.revert()
            return f"Undone: {entry.label}."
        except Exception as e:
            return f"Could not undo '{entry.label}': {e}"

    def peek_label(self) -> Optional[str]:
        with self._lock:
            return self._stack[-1].label if self._stack else None

    def can_undo(self) -> bool:
        with self._lock:
            return bool(self._stack)

    def history(self) -> list[str]:
        """Most recent first — for a future UI panel / list mode on the undo tool."""
        with self._lock:
            return [e.label for e in reversed(self._stack)]

    def clear(self) -> None:
        with self._lock:
            self._stack.clear()


# One shared stack for the whole process — every action pushes to the same place.
_default_stack = UndoStack()


def push_undo(label: str, revert: Callable[[], None]) -> None:
    _default_stack.push(label, revert)


def undo_last() -> str:
    return _default_stack.undo()


def peek_last_label() -> Optional[str]:
    return _default_stack.peek_label()


# -- ported from Mark-LIII: convenience wrappers around the same shared stack --

def can_undo() -> bool:
    return _default_stack.can_undo()


def peek() -> Optional[str]:
    """Alias for peek_last_label(), matching Mark-LIII's naming."""
    return _default_stack.peek_label()


def history() -> list[str]:
    return _default_stack.history()
