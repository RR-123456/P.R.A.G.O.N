"""
pragoncore/confirm_gate.py — a confirmation the model cannot forge.

THE PROBLEM WITH THE OLD GATE
    features/feature/computer_settings.py guarded shutdown/restart like this:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` is a tool PARAMETER, which means the model itself writes it.
    Nothing stops it from sending confirmed=yes on the very first call, and
    nothing checks that a human was ever involved — it's a convention, not a
    gate. It also only covered two actions, so switching off the Wi-Fi PRAGON
    is talking over went through with no gate at all.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `gate.request(prompt, action)` with a callable that
         does the real work.
      2. This module tells the UI to show a CONFIRM / CANCEL banner and
         returns IMMEDIATELY with a sentence for the model to say out loud.
      3. Only if — and only if — the user presses CONFIRM does the UI call
         `gate.resolve(request_id, approved=True)`, which is what actually
         runs the stored callable.

    Nothing blocks the voice session while the banner is up, so this costs no
    latency at all — it's cheaper than the old gate, which burned a full
    reject-then-re-call round trip on every shutdown attempt.

WHAT BELONGS HERE
    Only genuinely irreversible / disruptive actions (shutdown, restart,
    toggling Wi-Fi while it's the only way PRAGON hears you). Anything that
    can be reversed should just run immediately and register with
    pragoncore/undo.py instead — undo is faster than a question.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

_DEFAULT_TIMEOUT = 120  # seconds a pending confirmation stays valid


@dataclass
class _Pending:
    action: Callable[[], None]
    prompt: str
    created: float


class ConfirmGate:
    """
    Usage:
        gate = ConfirmGate(broadcast_fn=self.ui.broadcast)
        ack = gate.request("This will shut down the computer.", shutdown_computer)
        return ack   # spoken back immediately by the caller

    The UI's websocket layer calls gate.resolve(request_id, approved) when the
    user answers the on-screen banner; PragonUI wires this in via
    on_confirm_response (see pragon_ui.py).
    """

    def __init__(self, broadcast_fn: Callable[[dict], None], timeout: float = _DEFAULT_TIMEOUT):
        self._broadcast = broadcast_fn
        self._timeout = timeout
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def request(self, prompt: str, action: Callable[[], None]) -> str:
        """Issue a confirmation banner. Returns a sentence for the model to speak now."""
        req_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._prune_locked()
            self._pending[req_id] = _Pending(action=action, prompt=prompt, created=time.monotonic())
        try:
            self._broadcast({"type": "confirm_request", "id": req_id, "prompt": prompt})
        except Exception as e:
            print(f"[ConfirmGate] broadcast failed: {e}")
        return (
            f"{prompt} I've put a confirm button on screen — "
            f"I'll only go ahead once you press it."
        )

    def resolve(self, req_id: str, approved: bool) -> None:
        """Called by the UI (never the model) once the user answers."""
        with self._lock:
            pending = self._pending.pop(req_id, None)
        if not pending:
            return
        if approved:
            try:
                pending.action()
            except Exception as e:
                print(f"[ConfirmGate] action '{pending.prompt}' failed: {e}")

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [k for k, v in self._pending.items() if now - v.created > self._timeout]
        for k in expired:
            self._pending.pop(k, None)
