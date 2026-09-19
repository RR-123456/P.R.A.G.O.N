"""
agentic/morning_brief.py
─────────────────────────
Jarvis v2.0 — Morning Brief (FIXED)

Issue fixed: crew.morning_orchestration was calling Ollama
which gave a one-sentence response. Now builds the brief
directly and only uses crew for extra enrichment if Groq available.

Also reads from pragon_storage.json (nightly recap goals).
"""

from __future__ import annotations
import os
import json
from datetime import datetime, date, timedelta

BRIEF_FILE = "pragon_storage.json"


def load_brief_storage() -> dict:
    if os.path.exists(BRIEF_FILE):
        try:
            with open(BRIEF_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"briefs": {}, "todos": []}


def save_brief_storage(data: dict):
    try:
        with open(BRIEF_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[ Morning Brief ] Save error: {e}")


class MorningBrief:
    def __init__(self, config: dict):
        self.config = config
        self.user_name = config.get("user_name", "RR")

    def generate(self, sqlite_mem=None, calendar=None, github=None, crew=None) -> str:
        now = datetime.now()
        hour = now.hour
        day = now.strftime("%A, %d %B %Y")
        today_str = date.today().isoformat()

        # Time-aware greeting
        if 5 <= hour < 12: greeting = "Good morning"
        elif 12 <= hour < 17: greeting = "Good afternoon"
        else: greeting = "Good evening"

        # ── Pull data from all sources ────────────────────────────────
        calendar_events = []
        github_alerts = []
        deadlines = []
        reminders_count = 0
        nightly_goals = []

        if calendar and getattr(calendar, 'is_connected', False):
            try:
                calendar_events = calendar.get_today_events()
            except Exception as e:
                print(f"[ Morning Brief ] Calendar error: {e}")

        if github and getattr(github, 'is_connected', False):
            try:
                github_alerts = github.get_urgent_alerts()
            except Exception as e:
                print(f"[ Morning Brief ] GitHub error: {e}")

        if sqlite_mem:
            try:
                deadlines = sqlite_mem.get_active_deadlines()
                reminders_count = len(sqlite_mem.get_pending_reminders())
            except Exception as e:
                print(f"[ Morning Brief ] DB error: {e}")

        # Pull goals set during last nightly recap
        try:
            brief_data = load_brief_storage()
            day_brief = brief_data.get("briefs", {}).get(today_str, {})
            nightly_goals = day_brief.get("goals", [])
        except Exception:
            pass

        # ── Build brief directly (reliable) ──────────────────────────
        lines = [f"{greeting}, {self.user_name}. Today is {day}."]

        # Goals from nightly recap
        if nightly_goals:
            if len(nightly_goals) == 1:
                lines.append(f"Your goal for today is: {nightly_goals[0]}.")
            else:
                goals_str = ", ".join(nightly_goals[:-1]) + f", and {nightly_goals[-1]}"
                lines.append(f"Your goals for today are: {goals_str}.")

        # Calendar
        if calendar_events:
            count = len(calendar_events)
            first = calendar_events[0]
            lines.append(
                f"You have {count} event{'s' if count > 1 else ''} scheduled today. "
                f"First up: {first['title']} at {first['time']}."
            )
        else:
            lines.append("Your calendar is clear today.")

        # Deadlines
        if deadlines:
            nearest = deadlines[0]
            today_d = date.today()
            try:
                due_d = datetime.strptime(nearest['due_date'], "%Y-%m-%d").date()
                days_left = (due_d - today_d).days
                if days_left < 0:
                    urgency = f"{abs(days_left)} days overdue"
                elif days_left == 0:
                    urgency = "due today"
                elif days_left == 1:
                    urgency = "due tomorrow"
                else:
                    urgency = f"due in {days_left} days"
                lines.append(
                    f"Your most urgent deadline is {nearest['title']}, {urgency}. "
                    f"You have {len(deadlines)} active deadline{'s' if len(deadlines) > 1 else ''} in total."
                )
            except Exception:
                lines.append(f"You have {len(deadlines)} active deadline{'s' if len(deadlines) > 1 else ''}.")

        # GitHub
        if github_alerts:
            lines.append(github_alerts[0])

        # Reminders
        if reminders_count:
            lines.append(
                f"You have {reminders_count} pending reminder{'s' if reminders_count > 1 else ''}."
            )

        # Motivational close
        motivators = [
            "Make today count, RR.",
            "Let us get to work.",
            "Your goals are waiting. I am right here.",
            "One step at a time — today is yours.",
            "Stay focused. I have got your back.",
        ]
        lines.append(motivators[now.day % len(motivators)])

        brief_text = " ".join(lines)

        # ── Save brief to storage so brief.html can display it ────────
        try:
            brief_data = load_brief_storage()
            if today_str not in brief_data["briefs"]:
                brief_data["briefs"][today_str] = {}
            brief_data["briefs"][today_str]["brief_text"] = brief_text
            brief_data["briefs"][today_str]["date"] = today_str
            save_brief_storage(brief_data)
        except Exception as e:
            print(f"[ Morning Brief ] Storage error: {e}")

        return brief_text