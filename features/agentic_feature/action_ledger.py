"""
features/agentic_feature/action_ledger.py
════════════════════════════════════════════════════════════════════════════
Action Rationale Ledger.

WHY THIS EXISTS
    PRAGON's executor already retries, replans, and recovers from errors
    autonomously (see agent/executor.py) -- but none of that reasoning was
    ever written down. After a week of autonomous use, a user had no way to
    audit *why* the agent did what it did short of re-reading terminal
    scrollback. That's a trust gap: an agent that acts on your machine
    without a system-of-record for its own decisions is hard to hold
    accountable when something goes wrong.

    This module gives every tool invocation a one-line, plain-English
    rationale, a coarse risk tier, and a timestamp, persisted through the
    shared DataAccessLayer (consciousness/data_access.py) so it survives
    restarts and lives next to the rest of PRAGON's memory instead of in a
    separate, easy-to-lose log file.

HOW IT'S WIRED IN
    AgentExecutor._call_tool is the single choke point every agentic action
    already passes through (see agent/executor.py) -- every tool dispatch
    in that file already funnels through one function, so this is the one
    place a ledger entry can be written for genuinely every action, instead
    of instrumenting each of the 15+ individual action modules separately.

USAGE
    from features.agentic_feature.action_ledger import ledger

    ledger.record(
        tool="file_controller",
        parameters={"action": "delete", "path": "old_report.docx"},
        rationale="User asked to clean up the Downloads folder of files older than 30 days.",
        risk="medium",
        outcome="Done.",
    )

    ledger.recent(20)          # -> list[dict], most recent first
    ledger.summarize_session() # -> human-readable digest for a "what did you do today" query
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from consciousness.data_access import dal
except ImportError:
    # Allow running/importing this file before the package root is on
    # sys.path (mirrors the fallback pattern used elsewhere in the repo).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from consciousness.data_access import dal

RISK_LEVELS = ("low", "medium", "high")


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


class ActionLedger:
    """
    Append-only log of every autonomous action: what tool ran, with what
    parameters, why (in the agent's own words), how risky it was judged to
    be, and what happened. Backed by the same DataAccessLayer as the rest
    of PRAGON's memory -- one more logical table, not a new file to manage.
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        self._db_path = str(db_path or (_get_base_dir() / "pragon_brain" / "action_ledger.db"))
        self._conn = dal.get_sqlite(self._db_path)
        self._create_table()

    def _create_table(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS action_ledger (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp  TEXT NOT NULL,
                tool       TEXT NOT NULL,
                parameters TEXT NOT NULL,
                rationale  TEXT NOT NULL,
                risk       TEXT NOT NULL DEFAULT 'low',
                outcome    TEXT,
                goal       TEXT
            )
            """
        )
        self._conn.commit()

    def record(
        self,
        tool: str,
        parameters: dict,
        rationale: str,
        risk: str = "low",
        outcome: Optional[str] = None,
        goal: Optional[str] = None,
    ) -> int:
        if risk not in RISK_LEVELS:
            risk = "low"
        # Truncate defensively -- parameters can contain arbitrary (large)
        # generated content (e.g. file_controller's "content" field).
        params_json = json.dumps(parameters, default=str)[:4000]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = self._conn.execute(
            "INSERT INTO action_ledger (timestamp, tool, parameters, rationale, risk, outcome, goal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (timestamp, tool, params_json, rationale[:500], risk, (outcome or "")[:1000], (goal or "")[:500]),
        )
        self._conn.commit()
        return cur.lastrowid

    def recent(self, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT timestamp, tool, parameters, rationale, risk, outcome, goal "
            "FROM action_ledger ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def high_risk_since(self, since_iso: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT timestamp, tool, parameters, rationale, risk, outcome, goal "
            "FROM action_ledger WHERE risk='high' AND timestamp >= ? ORDER BY id DESC",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    def summarize_session(self, limit: int = 50) -> str:
        """Human-readable digest -- e.g. for a 'what did you do today, PRAGON?' query."""
        entries = self.recent(limit)
        if not entries:
            return "No autonomous actions recorded yet."
        lines = [
            f"- [{e['timestamp']}] ({e['risk']}) {e['tool']}: {e['rationale']}"
            for e in reversed(entries)
        ]
        return "\n".join(lines)


# One shared ledger for the whole process, same pattern as pragoncore/undo.py.
ledger = ActionLedger()
