"""
features/agentic_feature/background_monitor.py — Background Monitor

Watches a small set of user-configured topics and surfaces genuinely
new headlines once per check interval (default: once a day), instead of
re-announcing the same news every time. Reuses the existing DuckDuckGo
news search already built for web_search.py, and FRIDAY (via
pragon_ui.ask_friday) to decide what's actually worth mentioning --
no new search backend, no new LLM connection.

Runs as a simple background thread started from pragon_main.py; state
(topics + last-seen headlines, so nothing repeats) persists to a small
JSON file next to this module.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

from features.feature.web_search import _ddg_news
from pragon_ui import ask_friday

_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "background_monitor_state.json")

_DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60  # once a day


def _load_state() -> dict:
    if os.path.isfile(_STATE_PATH):
        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"topics": [], "seen_titles": [], "last_check": None}


def _save_state(state: dict) -> None:
    try:
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[BackgroundMonitor] couldn't save state: {e}")


class BackgroundMonitor:
    """Started once from pragon_main.py; runs its own daemon thread."""

    def __init__(self, on_alert=None, interval_seconds: int = _DEFAULT_INTERVAL_SECONDS):
        self.on_alert = on_alert  # callback(message: str)
        self.interval_seconds = interval_seconds
        self._state = _load_state()
        self._stop = threading.Event()
        self._thread = None

    # ── public API (mirrors the WhatsUp bridge's shape for consistency) ──
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def set_topics(self, topics: list[str]):
        self._state["topics"] = [t.strip() for t in topics if t.strip()]
        _save_state(self._state)

    def get_topics(self) -> list[str]:
        return list(self._state.get("topics", []))

    def check_now(self):
        """Runs one check cycle immediately (used by the 'Check Now' button
        and by the loop itself). Safe to call from any thread."""
        topics = self._state.get("topics", [])
        if not topics:
            return

        seen = set(self._state.get("seen_titles", []))
        new_items = []

        for topic in topics:
            try:
                results = _ddg_news(topic, max_results=5)
            except Exception as e:
                print(f"[BackgroundMonitor] search failed for '{topic}': {e}")
                continue
            for r in results:
                title = (r.get("title") or "").strip()
                if title and title not in seen:
                    new_items.append(f"[{topic}] {title}")
                    seen.add(title)

        # Cap how much history we remember so this file doesn't grow forever
        self._state["seen_titles"] = list(seen)[-500:]
        self._state["last_check"] = datetime.now().isoformat()
        _save_state(self._state)

        if not new_items:
            return

        # Let FRIDAY decide what's actually worth a proactive mention,
        # rather than dumping every headline on the user.
        prompt = (
            "These are new headlines since the last check, across topics "
            "the user asked to be kept informed about:\n"
            + "\n".join(f"- {item}" for item in new_items[:20])
            + "\n\nIf anything here is genuinely worth proactively telling "
            "the user about, write one short, natural sentence doing so "
            "(like a personal assistant would). If nothing is significant "
            "enough to interrupt them for, reply with exactly: NOTHING"
        )
        summary = ask_friday(prompt).strip()

        if summary and summary.upper() != "NOTHING" and self.on_alert:
            self.on_alert(summary)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.check_now()
            except Exception as e:
                print(f"[BackgroundMonitor] check cycle failed: {e}")
            self._stop.wait(self.interval_seconds)
