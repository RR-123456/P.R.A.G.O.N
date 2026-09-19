"""
memory/sqlite_memory.py
────────────────────────
Jarvis v2.0 — SQLite Conversation History

Stores general conversation history and task logs.
SQLCipher encryption can be added by installing pysqlcipher3
and replacing sqlite3 with sqlcipher.

Schema:
  tasks(id, timestamp, task, detail)
  conversations(id, timestamp, role, content, brain, sentiment)
  reminders(id, timestamp, message, trigger_at, done)
  deadlines(id, title, due_date, created_at, done, notes)
"""

from __future__ import annotations
import os
from datetime import datetime

from consciousness.data_access import dal


class SQLiteMemory:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # Routed through the shared DataAccessLayer instead of opening a
        # private connection -- see consciousness/data_access.py. Any other
        # component that asks for this same path gets the same connection.
        self._conn = dal.get_sqlite(db_path)
        self._create_tables()
        print(f"[ SQLite ] Database ready: {db_path}")

    def _create_tables(self):
        c = self._conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                task TEXT,
                detail TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                role TEXT,
                content TEXT,
                brain TEXT DEFAULT 'GENERAL',
                sentiment TEXT DEFAULT 'neutral'
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                message TEXT,
                trigger_at TEXT,
                done INTEGER DEFAULT 0
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS deadlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                due_date TEXT,
                created_at TEXT,
                done INTEGER DEFAULT 0,
                notes TEXT
            )
        """)

        self._conn.commit()

    # ── Tasks ─────────────────────────────────────────────────────────────

    def log(self, task: str, detail: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "INSERT INTO tasks (timestamp, task, detail) VALUES (?, ?, ?)",
            (timestamp, task, detail)
        )
        self._conn.commit()

    def get_recent_tasks(self, limit: int = 30) -> list[dict]:
        c = self._conn.cursor()
        rows = c.execute(
            "SELECT timestamp, task, detail FROM tasks ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [{"timestamp": r[0], "task": r[1], "detail": r[2]} for r in rows]

    # ── Conversations ─────────────────────────────────────────────────────

    def save_message(self, role: str, content: str, brain: str = "GENERAL", sentiment: str = "neutral"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "INSERT INTO conversations (timestamp, role, content, brain, sentiment) VALUES (?, ?, ?, ?, ?)",
            (timestamp, role, content, brain, sentiment)
        )
        self._conn.commit()

    def get_conversation_history(self, limit: int = 20) -> list[dict]:
        c = self._conn.cursor()
        rows = c.execute(
            "SELECT timestamp, role, content, brain FROM conversations ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [{"timestamp": r[0], "role": r[1], "content": r[2], "brain": r[3]} for r in reversed(rows)]

    # ── Reminders ─────────────────────────────────────────────────────────

    def add_reminder(self, message: str, trigger_at: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "INSERT INTO reminders (timestamp, message, trigger_at, done) VALUES (?, ?, ?, 0)",
            (timestamp, message, trigger_at)
        )
        self._conn.commit()

    def get_pending_reminders(self) -> list[dict]:
        c = self._conn.cursor()
        rows = c.execute(
            "SELECT id, message, trigger_at FROM reminders WHERE done=0"
        ).fetchall()
        return [{"id": r[0], "message": r[1], "trigger_at": r[2]} for r in rows]

    def mark_reminder_done(self, reminder_id: int):
        self._conn.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))
        self._conn.commit()

    # ── Deadlines ─────────────────────────────────────────────────────────

    def add_deadline(self, title: str, due_date: str, notes: str = ""):
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "INSERT INTO deadlines (title, due_date, created_at, done, notes) VALUES (?, ?, ?, 0, ?)",
            (title, due_date, created, notes)
        )
        self._conn.commit()

    def get_active_deadlines(self) -> list[dict]:
        c = self._conn.cursor()
        rows = c.execute(
            "SELECT id, title, due_date, created_at, notes FROM deadlines WHERE done=0 ORDER BY due_date"
        ).fetchall()
        return [{"id": r[0], "title": r[1], "due_date": r[2], "created_at": r[3], "notes": r[4]} for r in rows]

    def complete_deadline(self, deadline_id: int):
        self._conn.execute("UPDATE deadlines SET done=1 WHERE id=?", (deadline_id,))
        self._conn.commit()

    # ── Summary ───────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        c = self._conn.cursor()
        return {
            "total_tasks" : c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "total_conversations": c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "pending_reminders" : c.execute("SELECT COUNT(*) FROM reminders WHERE done=0").fetchone()[0],
            "active_deadlines" : c.execute("SELECT COUNT(*) FROM deadlines WHERE done=0").fetchone()[0],
        }

    def close(self):
        self._conn.close()