"""
agentic/deadline_manager.py
────────────────────────────
Jarvis v2.0 — Smart Deadline Manager (PRD Section 5.3)

Multi-day proactive reminders:
  - Day 3 of a 10-day project first check-in
  - Frequency increases near deadline
  - Goal: eliminate last-night rushes
"""

from __future__ import annotations
from datetime import datetime, timedelta
from typing import Callable


class DeadlineManager:
    def __init__(self, db=None, speak_fn: Callable = None):
        self.db = db
        self._speak = speak_fn
        self._alerted: set = set() # track IDs already alerted today

    def check_upcoming(self) -> list[str]:
        """
        Check all active deadlines and return alert messages.
        Called periodically (every 30 min) by agentic scheduler.
        """
        if not self.db:
            return []

        alerts = []
        today = datetime.now().date()
        today_str = str(today)

        try:
            deadlines = self.db.get_active_deadlines()
        except Exception:
            return []

        for dl in deadlines:
            dl_id = dl["id"]
            title = dl["title"]

            try:
                due_date = datetime.strptime(dl["due_date"], "%Y-%m-%d").date()
                created_at = datetime.strptime(dl["created_at"][:10], "%Y-%m-%d").date()
            except Exception:
                continue

            days_remaining = (due_date - today).days
            total_days = (due_date - created_at).days or 1
            days_elapsed = (today - created_at).days

            alert_key = f"{dl_id}_{today_str}"

            # Alert logic from PRD:
            # - 10-day project: first check at day 3, increasing frequency near deadline
            # - 1-day before: always alert
            # - Overdue: always alert

            should_alert = False
            urgency = "normal"

            if days_remaining < 0:
                should_alert = True
                urgency = "overdue"
            elif days_remaining == 0:
                should_alert = True
                urgency = "due_today"
            elif days_remaining == 1:
                should_alert = True
                urgency = "due_tomorrow"
            elif days_remaining <= 3:
                should_alert = True
                urgency = "urgent"
            elif total_days >= 7 and days_elapsed >= 3:
                # For long projects, check in from day 3 onwards
                # Frequency: every 3 days initially, then every day in last 30%
                threshold = max(3, int(total_days * 0.3))
                if days_remaining <= threshold:
                    should_alert = True
                    urgency = "approaching"

            if should_alert and alert_key not in self._alerted:
                self._alerted.add(alert_key)
                message = self._build_alert(title, days_remaining, urgency)
                alerts.append(message)

        return alerts

    def _build_alert(self, title: str, days_remaining: int, urgency: str) -> str:
        if urgency == "overdue":
            return f"Alert: {title} is overdue. You may want to address this as soon as possible."
        elif urgency == "due_today":
            return f"Heads up: {title} is due today."
        elif urgency == "due_tomorrow":
            return f"Reminder: {title} is due tomorrow. Make sure you are on track."
        elif urgency == "urgent":
            return f"Deadline approaching: {title} is due in {days_remaining} days."
        else:
            return f"Progress check: {title} is due in {days_remaining} days. How is it going?"