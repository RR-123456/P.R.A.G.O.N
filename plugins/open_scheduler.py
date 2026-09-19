"""
plugins/open_scheduler.py
════════════════════════════════════════════════════════════════════════════
Opens the standalone FIELD SCHEDULER module.

This is intentionally a tiny, self-contained launcher — it does NOT import,
call, or modify anything in features/feature/reminder.py or
features/agentic_feature/actions/reminder.py. The scheduler it opens
(scheduler_module/scheduler.html) is a fully independent app with its own
UI, its own storage (localStorage, inside that page only), and its own
ping/notification logic. Nothing here is shared with PRAGON's existing
reminder feature.

Trigger: user types or says "open scheduler" (or close variants — see
PLUGIN["description"], which Gemini uses to decide when to call this tool).
Discovered automatically by pragoncore/plugin_loader.py; no other file
needs to change.
"""
from __future__ import annotations

import sys
import webbrowser
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _scheduler_path() -> Path:
    return _base_dir() / "scheduler_module" / "scheduler.html"


PLUGIN = {
    "name": "open_scheduler",
    "description": (
        "Opens the standalone Field Scheduler app — a separate, independent "
        "reminder tool where the worker can set their own pings, e.g. tea "
        "time, put-on-gloves/PPE reminders, neck/shoulder stretch breaks to "
        "avoid cramping, tablet/medicine time, shift-ending, or returning "
        "tools to the tool holder. Use ONLY when the user says something "
        "like 'open scheduler', 'launch the scheduler', 'open the field "
        "scheduler', or 'show my reminders app'. Do NOT use this for "
        "setting a single one-off reminder in conversation — that is the "
        "existing reminder tool's job. This tool only opens the app window; "
        "the user sets and manages reminders inside that app themselves."
    ),
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    path = _scheduler_path()
    try:
        if not path.exists():
            return f"Sir, I can't find the scheduler module at {path}."
        webbrowser.open(path.resolve().as_uri())
        result = "Opening the Field Scheduler."
    except Exception as e:
        return f"Sir, I couldn't open the scheduler: {e}"

    if player:
        try:
            player.write_log(f"PRAGON [open_scheduler]: {result}")
        except Exception:
            pass
    return result
