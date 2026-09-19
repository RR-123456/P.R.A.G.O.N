"""
utils/logger.py
────────────────
Jarvis v2.0 — Structured Logger

Logs to console + pragon_log.log file.
Categories: SPEAK, LISTEN, ROUTER, BRAIN, AGENTIC, WS, ERROR
"""

from __future__ import annotations
import os
from datetime import datetime


CATEGORIES = {
    "SPEAK" : "",
    "LISTEN" : "",
    "ROUTER" : "",
    "BRAIN" : "",
    "AGENTIC": "",
    "WS" : "",
    "MEMORY" : "",
    "ERROR" : "",
    "SYSTEM" : "",
}


class JarvisLogger:
    def __init__(self, log_file: str = "pragon_log.log"):
        self.log_file = log_file

    def log(self, category: str, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        icon = CATEGORIES.get(category.upper(), "•")
        line = f"[{timestamp}] {icon} [{category}] {message}"
        print(line)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def error(self, source: str, error: Exception):
        self.log("ERROR", f"{source}: {str(error)}")