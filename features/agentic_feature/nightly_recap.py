"""
agentic/nightly_recap.py
─────────────────────────
Jarvis v2.0 — Nightly Recap (FIXED)

Triggered by "goodnight":
  1. Recap today's activity
  2. Ask for tomorrow's goals (stores them)
  3. Ask for things to do (no deadline)
  4. Save everything to pragon_storage.json
"""

from __future__ import annotations
import json
import os
import threading
from datetime import datetime, date, timedelta

BRIEF_FILE = "pragon_storage.json"


def load_brief_data() -> dict:
    if os.path.exists(BRIEF_FILE):
        try:
            with open(BRIEF_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"briefs": {}, "todos": [], "deadlines_cache": []}


def save_brief_data(data: dict):
    with open(BRIEF_FILE, "w") as f:
        json.dump(data, f, indent=2)


class NightlyRecap:
    def __init__(self, config: dict, speak_fn, listen_fn):
        self.config = config
        self.user_name = config.get("user_name", "RR")
        self._speak = speak_fn
        self._listen = listen_fn

    def trigger(self, sqlite_mem=None):
        threading.Thread(
            target=self._run_recap,
            args=(sqlite_mem,),
            daemon=True
        ).start()

    def _run_recap(self, sqlite_mem=None):
        user = self.user_name
        today = date.today().isoformat()
        data = load_brief_data()

        self._speak(
            f"Goodnight, {user}. "
            "Let me give you a brief recap before you rest."
        )

        # ── Recap today ──────────────────────────────────────────────
        if sqlite_mem:
            try:
                tasks = sqlite_mem.get_recent_tasks(limit=10)
                today_tasks = [t for t in tasks if t["timestamp"].startswith(today)]
                if today_tasks:
                    self._speak(
                        f"Today you logged {len(today_tasks)} actions, including "
                        f"{today_tasks[0]['task']} and others."
                    )
                else:
                    self._speak("The logs were quiet today.")
            except Exception:
                self._speak("Logs were quiet today.")
        else:
            self._speak("Logs were quiet today.")

        # ── Ask for tomorrow's goals ──────────────────────────────────
        self._speak(
            f"Now, what are your main goals for tomorrow, {user}? "
            "You can list them one by one. Say done when you are finished."
        )

        tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
        goals = []
        attempts = 0

        while attempts < 5:
            goal = self._listen()
            if not goal:
                break
            if "done" in goal.lower() or "nothing" in goal.lower() or "that's it" in goal.lower():
                break
            goals.append(goal.strip())
            attempts += 1
            if attempts < 5:
                self._speak(f"Got it. Anything else?")

        if goals:
            self._speak(
                f"Perfect. I have noted {len(goals)} goal{'s' if len(goals) > 1 else ''} for tomorrow."
            )
        else:
            self._speak("No goals set for tomorrow. That is fine.")

        # ── Ask for things to do (no deadline) ───────────────────────
        self._speak(
            "Any general things you want to do — no deadline, just things on your mind? "
            "Say done when finished."
        )

        todos = []
        attempts = 0
        while attempts < 5:
            todo = self._listen()
            if not todo:
                break
            if "done" in todo.lower() or "no" in todo.lower() or "nothing" in todo.lower():
                break
            todos.append(todo.strip())
            attempts += 1
            if attempts < 5:
                self._speak("Noted. Anything else?")

        # ── Build brief for tomorrow ──────────────────────────────────
        brief_entry = {
            "date" : tomorrow_str,
            "created_at" : datetime.now().isoformat(),
            "goals" : goals,
            "todos_added" : todos,
            "brief_text" : "", # filled by morning brief
        }

        # Save to pragon_storage.json
        if tomorrow_str not in data["briefs"]:
            data["briefs"][tomorrow_str] = brief_entry
        else:
            # Merge
            data["briefs"][tomorrow_str]["goals"] += goals
            data["briefs"][tomorrow_str]["todos_added"] += todos

        # Also merge into global todos
        for t in todos:
            if t not in data.get("todos", []):
                data.setdefault("todos", []).append(t)

        save_brief_data(data)

        # ── Also save goals to SQLite if available ────────────────────
        if sqlite_mem and goals:
            try:
                sqlite_mem.log(
                    "Nightly Goals",
                    f"[{tomorrow_str}] " + " | ".join(goals)
                )
            except Exception:
                pass

        self._speak(
            f"Everything is saved, {user}. "
            f"I will have your morning brief ready tomorrow. "
            f"Rest well. Goodnight."
        )