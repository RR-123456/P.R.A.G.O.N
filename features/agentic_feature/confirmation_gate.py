"""
agentic/confirmation_gate.py
─────────────────────────────
Jarvis v2.0 — Confirmation Gate (PRD Section 4.3)

PRD requirement:
  "Gate: Always asks for confirmation before sending
   messages, emails, or executing external actions."

All external-facing actions (WhatsApp send, email send,
API calls) must pass through this gate before execution.
"""

from __future__ import annotations
import threading
from typing import Callable


class ConfirmationGate:
    """
    Intercepts external actions and asks for user confirmation.

    Usage:
        gate = ConfirmationGate(speak_fn, listen_fn)
        approved = gate.ask("Shall I send this message to Amma?")
        if approved:
            whatsapp_send(...)
    """

    def __init__(self, speak_fn: Callable, listen_fn: Callable):
        self._speak = speak_fn
        self._listen = listen_fn

    def ask(self, question: str, timeout_seconds: int = 10) -> bool:
        """
        Speak a confirmation question and wait for yes/no.
        Returns True if approved, False if declined or timeout.
        """
        self._speak(question)

        response = self._listen()
        if not response:
            self._speak("No response received. Action cancelled.")
            return False

        response_lower = response.lower().strip()

        positive = ["yes", "yeah", "yep", "sure", "ok", "okay",
                    "send it", "go ahead", "do it", "confirm",
                    "aamaa", "sari", "ha", "haan"]
        negative = ["no", "nope", "cancel", "don't", "stop",
                    "abort", "never mind", "wait", "hold on",
                    "vendam", "illai", "nahi"]

        if any(w in response_lower for w in positive):
            return True
        if any(w in response_lower for w in negative):
            self._speak("Action cancelled, RR.")
            return False

        # Ambiguous — ask once more
        self._speak("I did not catch that clearly. Say yes to confirm or no to cancel.")
        clarification = self._listen()
        if clarification and any(w in clarification.lower() for w in positive):
            return True

        self._speak("Cancelling to be safe, RR.")
        return False

    def ask_for_whatsapp(self, contact: str, message: str) -> bool:
        question = f"Shall I send this message to {contact}? It says: {message[:50]}."
        return self.ask(question)

    def ask_for_email(self, to: str, subject: str) -> bool:
        question = f"Shall I send this email to {to} with subject {subject}?"
        return self.ask(question)

    def ask_for_action(self, action_description: str) -> bool:
        question = f"{action_description}. Shall I proceed?"
        return self.ask(question)