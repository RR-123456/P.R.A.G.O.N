"""
unified_memory.py
═══════════════════════════════════════════════════════════════════════════════
PRAGON — Unified Memory Engine v2.0
A single-file memory system combining:
  • ChromaDB vector semantic memory
  • SQLite conversation & task timeline
  • JSON structured long-term memory
  • Daily briefs & todo tracking
  • Structured event logging

Drop this one file into your project. Install deps:
    pip install chromadb sentence-transformers

Usage:
    from unified_memory import UnifiedMemory
    mem = UnifiedMemory("./data")
    mem.remember("identity", "name", "Pragath")
    mem.chat("user", "Hello, what's the news?")
    results = mem.recall("What did I say about gold?") # semantic search
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from enum import IntEnum
from pathlib import Path
from threading import RLock
from typing import Optional, Any, Iterator, Callable

# ── Optional ChromaDB ────────────────────────────────────────────────────────

_CHROMA_AVAILABLE = False

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    _CHROMA_AVAILABLE = True
except ImportError:
    warnings.warn("[Memory] ChromaDB not installed. Semantic search disabled. "
                  "Run: pip install chromadb", stacklevel=2)

try:
    from .data_access import dal
except ImportError:
    from data_access import dal  # when imported without package context

try:
    from .embeddings import EmbeddingBackend
except ImportError:  # allow running this file standalone, outside the package
    from embeddings import EmbeddingBackend

# ── Constants ────────────────────────────────────────────────────────────────

CATEGORIES = frozenset({
    "identity", "preferences", "projects",
    "relationships", "wishes", "notes"
})

CATEGORY_LIMITS = {
    "identity": 20, "preferences": 15, "projects": 8,
    "relationships": 10, "wishes": 8, "notes": 8,
}

MEMORY_MAX_CHARS = 2500
VALUE_MAX_LEN = 500
BRIEF_RETENTION_DAYS = 90
LOG_MAX_BYTES = 5_000_000

# ── Data Models ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MemoryEntry:
    """Immutable structured memory entry."""
    value: str
    updated: str
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Message:
    role: str
    content: str
    brain: str = "GENERAL"
    sentiment: str = "neutral"
    timestamp: Optional[str] = None


@dataclass
class TaskLog:
    task: str
    detail: str
    timestamp: Optional[str] = None


@dataclass
class Reminder:
    message: str
    trigger_at: str
    done: bool = False
    timestamp: Optional[str] = None


@dataclass
class Deadline:
    title: str
    due_date: str
    notes: str = ""
    done: bool = False
    created_at: Optional[str] = None


@dataclass
class DailyBrief:
    date: str
    text: str
    generated_at: str


@dataclass
class TodoItem:
    text: str
    done: bool = False
    created_at: str = ""


class Level(IntEnum):
    DEBUG = 0
    INFO = 1
    NOTICE = 2
    WARNING = 3
    ERROR = 4
    CRITICAL = 5


# ── Sub-Engines ──────────────────────────────────────────────────────────────

class _StructuredMemory:
    """JSON-backed categorized memory vault."""

    def __init__(self, filepath: Path, max_chars: int = MEMORY_MAX_CHARS):
        self._path = Path(filepath)
        self._max_chars = max_chars
        self._lock = RLock()
        self._data: dict[str, dict[str, MemoryEntry]] = {c: {} for c in CATEGORIES}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = {
                cat: {
                    key: MemoryEntry(
                        value=val.get("value", ""),
                        updated=val.get("updated", datetime.now().strftime("%Y-%m-%d")),
                        entry_id=val.get("id", uuid.uuid4().hex[:8])
                    )
                    for key, val in (raw.get(cat, {}) or {}).items()
                    if isinstance(val, dict) and "value" in val
                }
                for cat in CATEGORIES
            }
        except Exception as exc:
            self._log("warning", f"Structured memory load failed: {exc}")
            self._data = {c: {} for c in CATEGORIES}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            cat: {
                key: {"value": e.value, "updated": e.updated, "id": e.entry_id}
                for key, e in items.items()
            }
            for cat, items in self._data.items()
        }
        self._path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _trim(self) -> None:
        payload = self._to_serializable()
        while len(json.dumps(payload, ensure_ascii=False)) > self._max_chars:
            oldest: Optional[tuple[str, str, str]] = None
            for cat, items in self._data.items():
                for key, entry in items.items():
                    if oldest is None or entry.updated < oldest[2]:
                        oldest = (cat, key, entry.updated)
            if oldest:
                del self._data[oldest[0]][oldest[1]]
                payload = self._to_serializable()
            else:
                break

    def _to_serializable(self) -> dict:
        return {
            cat: {
                key: {"value": e.value, "updated": e.updated, "id": e.entry_id}
                for key, e in items.items()
            }
            for cat, items in self._data.items()
        }

    def _log(self, level: str, msg: str) -> None:
        print(f"[StructuredMemory] [{level.upper()}] {msg}")

    def store(self, category: str, key: str, value: str) -> MemoryEntry:
        if category not in CATEGORIES:
            category = "notes"
        value = value.strip()[:VALUE_MAX_LEN]
        if not value:
            raise ValueError("Value cannot be empty")
        entry = MemoryEntry(value=value, updated=datetime.now().strftime("%Y-%m-%d"))
        with self._lock:
            self._data[category][key] = entry
            self._trim()
            self._save()
        return entry

    def retrieve(self, category: str, key: str) -> Optional[MemoryEntry]:
        with self._lock:
            return self._data.get(category, {}).get(key)

    def remove(self, category: str, key: str) -> bool:
        with self._lock:
            if key in self._data.get(category, {}):
                del self._data[category][key]
                self._save()
                return True
        return False

    def all_entries(self) -> Iterator[tuple[str, str, MemoryEntry]]:
        with self._lock:
            for cat, items in self._data.items():
                for key, entry in items.items():
                    yield cat, key, entry

    def keyword_search(self, query: str) -> list[tuple[str, str, MemoryEntry]]:
        query = query.lower()
        results = []
        for cat, key, entry in self.all_entries():
            if query in entry.value.lower() or query in key.lower():
                results.append((cat, key, entry))
        return results

    def to_prompt_context(self) -> str:
        lines: list[str] = []
        identity = self._data.get("identity", {})
        id_order = ["name", "nickname", "age", "birthday", "city", "job", "language", "school", "nationality"]
        for field in id_order:
            if field in identity:
                lines.append(f"{field.title()}: {identity[field].value}")
        for key, entry in identity.items():
            if key not in id_order:
                lines.append(f"{key.replace('_', ' ').title()}: {entry.value}")

        headers = {
            "preferences": "Preferences",
            "projects": "Active Projects / Goals",
            "relationships": "People in their life",
            "wishes": "Wishes / Plans / Wants",
            "notes": "Other notes",
        }
        for cat, header in headers.items():
            items = self._data.get(cat, {})
            if not items:
                continue
            lines.append("")
            lines.append(f"{header}:")
            for key, entry in list(items.items())[:CATEGORY_LIMITS.get(cat, 10)]:
                lines.append(f" - {key.replace('_', ' ').title()}: {entry.value}")

        if not lines:
            return ""
        header = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]\n"
        result = header + "\n".join(lines)
        return result[:2200] + "…\n" if len(result) > 2200 else result + "\n"

    def stats(self) -> dict[str, Any]:
        return {
            "total_entries": sum(len(v) for v in self._data.values()),
            "by_category": {k: len(v) for k, v in self._data.items()},
            "file_size": self._path.stat().st_size if self._path.exists() else 0,
        }


class _Timeline:
    """SQLite-backed conversation, task, reminder & deadline store."""

    def __init__(self, db_path: Path):
        self._db = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # Routed through the shared DataAccessLayer -- see
        # consciousness/data_access.py. WAL + busy_timeout are applied once,
        # by the DAL, the first time this path is opened by anyone.
        return dal.get_sqlite(self._db)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    task TEXT NOT NULL,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    brain TEXT DEFAULT 'GENERAL',
                    sentiment TEXT DEFAULT 'neutral'
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    message TEXT NOT NULL,
                    trigger_at TEXT NOT NULL,
                    done INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS deadlines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    done INTEGER DEFAULT 0,
                    notes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(timestamp);
                CREATE INDEX IF NOT EXISTS idx_tasks_time ON tasks(timestamp);
                CREATE INDEX IF NOT EXISTS idx_reminders_trigger ON reminders(trigger_at);
            """)
            conn.commit()

    def record_message(self, msg: Message) -> int:
        ts = msg.timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO conversations (timestamp, role, content, brain, sentiment) VALUES (?, ?, ?, ?, ?)",
                (ts, msg.role, msg.content, msg.brain, msg.sentiment)
            )
            conn.commit()
            return cur.lastrowid or 0

    def recent_messages(self, limit: int = 20, brain: Optional[str] = None) -> list[Message]:
        with self._connect() as conn:
            if brain:
                rows = conn.execute(
                    "SELECT timestamp, role, content, brain, sentiment FROM conversations WHERE brain = ? ORDER BY id DESC LIMIT ?",
                    (brain, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT timestamp, role, content, brain, sentiment FROM conversations ORDER BY id DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [Message(role=r["role"], content=r["content"], brain=r["brain"],
                           sentiment=r["sentiment"], timestamp=r["timestamp"]) for r in reversed(rows)]

    def log_task(self, log: TaskLog) -> int:
        ts = log.timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute("INSERT INTO tasks (timestamp, task, detail) VALUES (?, ?, ?)",
                               (ts, log.task, log.detail))
            conn.commit()
            return cur.lastrowid or 0

    def recent_tasks(self, limit: int = 30) -> list[TaskLog]:
        with self._connect() as conn:
            rows = conn.execute("SELECT timestamp, task, detail FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [TaskLog(task=r["task"], detail=r["detail"], timestamp=r["timestamp"]) for r in rows]

    def add_reminder(self, rem: Reminder) -> int:
        ts = rem.timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO reminders (timestamp, message, trigger_at, done) VALUES (?, ?, ?, ?)",
                (ts, rem.message, rem.trigger_at, int(rem.done))
            )
            conn.commit()
            return cur.lastrowid or 0

    def pending_reminders(self) -> list[tuple[int, Reminder]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, message, trigger_at, timestamp FROM reminders WHERE done = 0").fetchall()
            return [(r["id"], Reminder(message=r["message"], trigger_at=r["trigger_at"], timestamp=r["timestamp"]))
                    for r in rows]

    def complete_reminder(self, rid: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
            conn.commit()
            return cur.rowcount > 0

    def add_deadline(self, dl: Deadline) -> int:
        ts = dl.created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO deadlines (title, due_date, created_at, done, notes) VALUES (?, ?, ?, ?, ?)",
                (dl.title, dl.due_date, ts, int(dl.done), dl.notes)
            )
            conn.commit()
            return cur.lastrowid or 0

    def active_deadlines(self) -> list[tuple[int, Deadline]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, due_date, created_at, notes FROM deadlines WHERE done = 0 ORDER BY due_date"
            ).fetchall()
            return [(r["id"], Deadline(title=r["title"], due_date=r["due_date"],
                                       created_at=r["created_at"], notes=r["notes"] or "")) for r in rows]

    def complete_deadline(self, did: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE deadlines SET done = 1 WHERE id = ?", (did,))
            conn.commit()
            return cur.rowcount > 0

    def search_content(self, query: str, limit: int = 20) -> list[dict]:
        pattern = f"%{query}%"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT timestamp, role, content, brain FROM conversations WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (pattern, limit)
            ).fetchall()
            return [{"timestamp": r["timestamp"], "role": r["role"], "content": r["content"], "brain": r["brain"]}
                    for r in rows]

    def summary(self) -> dict[str, int]:
        with self._connect() as conn:
            return {
                "total_tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "total_messages": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                "pending_reminders": conn.execute("SELECT COUNT(*) FROM reminders WHERE done = 0").fetchone()[0],
                "active_deadlines": conn.execute("SELECT COUNT(*) FROM deadlines WHERE done = 0").fetchone()[0],
            }


class _BriefArchive:
    """JSON-backed daily brief and todo storage."""

    def __init__(self, filepath: Path):
        self._path = Path(filepath)
        self._lock = RLock()
        self._briefs: dict[str, DailyBrief] = {}
        self._todos: list[TodoItem] = []
        self._load()
        self._prune()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._briefs = {
                d: DailyBrief(date=b.get("date", d), text=b.get("brief_text", ""),
                              generated_at=b.get("generated_at", d))
                for d, b in raw.get("briefs", {}).items()
            }
            self._todos = [TodoItem(text=t.get("text", ""), done=t.get("done", False),
                                    created_at=t.get("created_at", "")) for t in raw.get("todos", [])]
        except Exception as exc:
            print(f"[Briefs] Load failed: {exc}")

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "briefs": {d: {"date": b.date, "brief_text": b.text, "generated_at": b.generated_at}
                       for d, b in self._briefs.items()},
            "todos": [{"text": t.text, "done": t.done, "created_at": t.created_at or datetime.now().strftime("%Y-%m-%d")}
                      for t in self._todos]
        }
        self._path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _prune(self) -> None:
        cutoff = datetime.now()
        to_remove = []
        for d in self._briefs:
            try:
                if (cutoff - datetime.strptime(d, "%Y-%m-%d")).days > BRIEF_RETENTION_DAYS:
                    to_remove.append(d)
            except ValueError:
                # Malformed date key from a hand-edited or corrupted file —
                # skip it rather than taking down memory-system startup.
                print(f"[Briefs] Skipping unparseable brief date: {d!r}")
        for d in to_remove:
            del self._briefs[d]
        if to_remove:
            self._save()

    def store_brief(self, date_str: str, text: str) -> DailyBrief:
        brief = DailyBrief(date=date_str, text=text,
                           generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with self._lock:
            self._briefs[date_str] = brief
            self._save()
        return brief

    def get_brief(self, date_str: str) -> Optional[DailyBrief]:
        with self._lock:
            return self._briefs.get(date_str)

    def latest_briefs(self, n: int = 5) -> list[DailyBrief]:
        with self._lock:
            return [self._briefs[d] for d in sorted(self._briefs.keys(), reverse=True)[:n]]

    def add_todo(self, text: str) -> TodoItem:
        item = TodoItem(text=text, created_at=datetime.now().strftime("%Y-%m-%d"))
        with self._lock:
            self._todos.append(item)
            self._save()
        return item

    def list_todos(self, include_done: bool = False) -> list[TodoItem]:
        with self._lock:
            return list(self._todos) if include_done else [t for t in self._todos if not t.done]

    def mark_todo_done(self, index: int) -> bool:
        with self._lock:
            if 0 <= index < len(self._todos):
                self._todos[index].done = True
                self._save()
                return True
        return False

    def remove_todo(self, index: int) -> bool:
        with self._lock:
            if 0 <= index < len(self._todos):
                self._todos.pop(index)
                self._save()
                return True
        return False


class _EventLogger:
    """Rotating file logger with severity levels."""

    ICONS = {Level.DEBUG: "", Level.INFO: "ℹ ", Level.NOTICE: "",
             Level.WARNING: " ", Level.ERROR: "", Level.CRITICAL: ""}

    def __init__(self, filepath: Path, max_size: int = LOG_MAX_BYTES, min_level: Level = Level.INFO):
        self._path = Path(filepath)
        self._max_size = max_size
        self._min_level = min_level
        self._lock = RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _rotate(self) -> None:
        if self._path.exists() and self._path.stat().st_size > self._max_size:
            backup = self._path.with_suffix(".log.old")
            if backup.exists():
                backup.unlink()
            self._path.rename(backup)

    def _write(self, line: str) -> None:
        with self._lock:
            self._rotate()
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def log(self, level: Level, source: str, message: str, extra: Optional[dict] = None) -> None:
        if level < self._min_level:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        icon = self.ICONS.get(level, "•")
        line = f"[{ts}] {icon} [{source}] {message}"
        if extra:
            line += " | " + " ".join(f"{k}={v}" for k, v in extra.items())
        self._write(line)
        print(line)

    def debug(self, src: str, msg: str, **kw): self.log(Level.DEBUG, src, msg, kw)
    def info(self, src: str, msg: str, **kw): self.log(Level.INFO, src, msg, kw)
    def notice(self, src: str, msg: str, **kw): self.log(Level.NOTICE, src, msg, kw)
    def warning(self, src: str, msg: str, **kw): self.log(Level.WARNING, src, msg, kw)
    def error(self, src: str, msg: str, **kw): self.log(Level.ERROR, src, msg, kw)
    def critical(self, src: str, msg: str, **kw): self.log(Level.CRITICAL, src, msg, kw)


class _VectorMemory:
    """ChromaDB-backed semantic memory for vector search."""

    def __init__(
        self,
        persist_dir: Path,
        collection_name: str = "memory",
        embedder: Optional[EmbeddingBackend] = None,
        client: Optional[Any] = None,
    ):
        self._embedder = embedder
        self._available = _CHROMA_AVAILABLE and embedder is not None and embedder.available
        self._client: Optional[Any] = None
        self._collection: Optional[Any] = None

        if not self._available:
            print(f"[VectorMemory:{collection_name}] ChromaDB or embeddings not available. Semantic search disabled.")
            return

        if client is not None:
            # Share one persistent client across collections (memory + conversations).
            self._client = client
        else:
            # Routed through the shared DataAccessLayer instead of opening a
            # private PersistentClient -- see consciousness/data_access.py.
            self._client = dal.get_chroma(persist_dir)
        self._collection = self._client.get_or_create_collection(name=collection_name)
        print(f"[VectorMemory] Collection ready: {collection_name}")

    def embed(self, text: str) -> list[float]:
        if self._embedder:
            return self._embedder.embed_one(text)
        return []

    def add(self, doc_id: str, text: str, metadata: Optional[dict] = None) -> bool:
        if not self._available or self._collection is None:
            return False
        try:
            self._collection.add(
                ids=[doc_id],
                documents=[text],
                embeddings=[self.embed(text)],
                metadatas=[metadata or {}]
            )
            return True
        except Exception as exc:
            print(f"[VectorMemory] Add failed: {exc}")
            return False

    def search(self, query: str, n_results: int = 5) -> list[dict]:
        if not self._available or self._collection is None:
            return []
        try:
            results = self._collection.query(
                query_embeddings=[self.embed(query)],
                n_results=n_results
            )
            output = []
            for i in range(len(results["ids"][0])):
                output.append({
                    "id": results["ids"][0][i],
                    "text": results["documents"][0][i],
                    "metadata": results["metadatas"][0][i],
                    "distance": results["distances"][0][i] if results.get("distances") else None
                })
            return output
        except Exception as exc:
            print(f"[VectorMemory] Search failed: {exc}")
            return []

    def delete(self, doc_id: str) -> bool:
        if not self._available or self._collection is None:
            return False
        try:
            self._collection.delete(ids=[doc_id])
            return True
        except Exception:
            return False

    def delete_where(self, where: dict) -> bool:
        """Delete every entry matching a metadata filter, e.g.
        {"category": "wishes", "key": "purchase_intent"}. Needed because
        entries are stored under an id that includes a random entry_id the
        caller doesn't know at delete-time — exact-id delete() can't reach them."""
        if not self._available or self._collection is None:
            return False
        try:
            self._collection.delete(where=where)
            return True
        except Exception as exc:
            print(f"[VectorMemory] delete_where failed: {exc}")
            return False

    def count(self) -> int:
        if not self._available or self._collection is None:
            return 0
        return self._collection.count()


# ── Unified Interface ────────────────────────────────────────────────────────

class UnifiedMemory:
    """
    One class to manage ALL memory:
      • JSON structured memory (identity, preferences, etc.)
      • SQLite timeline (conversations, tasks, reminders, deadlines)
      • ChromaDB vector memory (semantic search)
      • Daily briefs & todos
      • Event logging
    """

    def __init__(
        self,
        base_dir: str | Path,
        embedding_backend: str = "local",
        gemini_api_key_fn: Optional[Callable[[], str]] = None,
    ):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

        self._structured = _StructuredMemory(self._base / "long_term.json")
        self._timeline = _Timeline(self._base / "timeline.db")
        self._briefs = _BriefArchive(self._base / "briefs.json")
        self._logger = _EventLogger(self._base / "events.log")

        # One embedder + one persistent Chroma client shared by both collections
        # below, so "memory" (remembered facts) and "conversations" (chat turns)
        # live side by side on disk but are queried separately.
        self._embedder = EmbeddingBackend(backend=embedding_backend, api_key_fn=gemini_api_key_fn)
        chroma_dir = self._base / "chroma_db"
        shared_client = None
        if _CHROMA_AVAILABLE and self._embedder.available:
            # Routed through the shared DataAccessLayer -- see
            # consciousness/data_access.py.
            shared_client = dal.get_chroma(chroma_dir)
        self._vector = _VectorMemory(chroma_dir, "memory", embedder=self._embedder, client=shared_client)
        self._chat_vector = _VectorMemory(chroma_dir, "conversations", embedder=self._embedder, client=shared_client)

        self._logger.notice("MEMORY", "UnifiedMemory engine initialized", path=str(self._base))

    # ── Structured Memory (JSON) ──────────────────────────────────────

    def remember(self, category: str, key: str, value: str) -> MemoryEntry:
        """Store a fact in structured memory + vector index."""
        entry = self._structured.store(category, key, value)
        doc_text = f"[{category}] {key}: {value}"
        self._vector.add(f"{category}:{key}:{entry.entry_id}", doc_text, {
            "category": category,
            "key": key,
            "updated": entry.updated,
            "entry_id": entry.entry_id
        })
        self._logger.notice("MEMORY", f"Stored {category}/{key}")
        return entry

    def recall_exact(self, category: str, key: str) -> Optional[MemoryEntry]:
        """Exact lookup by category + key."""
        return self._structured.retrieve(category, key)

    def forget(self, category: str, key: str) -> bool:
        """Remove from structured + vector memory."""
        removed = self._structured.remove(category, key)
        if removed:
            # Entries are indexed under "{category}:{key}:{random_entry_id}",
            # and the entry_id is gone once _structured.remove() has run, so
            # delete by the (category, key) metadata every add() also writes
            # instead of trying to guess the id.
            self._vector.delete_where({"$and": [{"category": category}, {"key": key}]})
            self._logger.notice("MEMORY", f"Forgotten {category}/{key}")
        return removed

    def search_keywords(self, query: str) -> list[tuple[str, str, MemoryEntry]]:
        """Keyword search across all structured memory."""
        return self._structured.keyword_search(query)

    def recall(self, query: str, n_results: int = 5) -> list[dict]:
        """
         SEMANTIC SEARCH — finds memories by MEANING, not just keywords.
        Requires: pip install chromadb sentence-transformers
        Example: recall("What did I say about gold?")
        """
        results = self._vector.search(query, n_results)
        self._logger.info("MEMORY", f"Semantic recall: '{query}' {len(results)} results")
        return results

    def context(self) -> str:
        """Format structured memory as LLM prompt context."""
        return self._structured.to_prompt_context()

    def memory_stats(self) -> dict[str, Any]:
        return self._structured.stats()

    # ── Timeline (SQLite) ─────────────────────────────────────────────

    def chat(self, role: str, content: str, brain: str = "GENERAL", sentiment: str = "neutral") -> int:
        """Log a conversation message — stored in SQLite timeline AND embedded
        into the conversation vector store, so it becomes semantically
        recallable later via recall_chat()."""
        msg_id = self._timeline.record_message(Message(role=role, content=content, brain=brain, sentiment=sentiment))
        if content and content.strip():
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._chat_vector.add(f"chat:{msg_id}", content, {
                "role": role, "brain": brain, "sentiment": sentiment, "timestamp": ts,
            })
        self._logger.notice("TIMELINE", f"Message logged [{brain}]", role=role)
        return msg_id

    def recall_chat(self, query: str, n_results: int = 5) -> list[dict]:
        """SEMANTIC SEARCH over past conversation turns (by meaning, not just
        substring like search_chat). Example: recall_chat("what did we decide about the trip")"""
        results = self._chat_vector.search(query, n_results)
        self._logger.info("MEMORY", f"Semantic chat recall: '{query}' {len(results)} results")
        return results

    def recent_chat(self, limit: int = 20, brain: Optional[str] = None) -> list[Message]:
        return self._timeline.recent_messages(limit, brain)

    def log_task(self, task: str, detail: str = "") -> int:
        tid = self._timeline.log_task(TaskLog(task=task, detail=detail))
        self._logger.notice("TIMELINE", f"Task logged: {task}")
        return tid

    def recent_tasks(self, limit: int = 30) -> list[TaskLog]:
        return self._timeline.recent_tasks(limit)

    def set_reminder(self, message: str, trigger_at: str) -> int:
        rid = self._timeline.add_reminder(Reminder(message=message, trigger_at=trigger_at))
        self._logger.notice("TIMELINE", f"Reminder set: {message} @ {trigger_at}")
        return rid

    def get_reminders(self) -> list[tuple[int, Reminder]]:
        return self._timeline.pending_reminders()

    def done_reminder(self, rid: int) -> bool:
        return self._timeline.complete_reminder(rid)

    def set_deadline(self, title: str, due_date: str, notes: str = "") -> int:
        did = self._timeline.add_deadline(Deadline(title=title, due_date=due_date, notes=notes))
        self._logger.notice("TIMELINE", f"Deadline set: {title} by {due_date}")
        return did

    def get_deadlines(self) -> list[tuple[int, Deadline]]:
        return self._timeline.active_deadlines()

    def done_deadline(self, did: int) -> bool:
        return self._timeline.complete_deadline(did)

    def search_chat(self, query: str, limit: int = 20) -> list[dict]:
        return self._timeline.search_content(query, limit)

    def timeline_summary(self) -> dict[str, int]:
        return self._timeline.summary()

    # ── Briefs ────────────────────────────────────────────────────────

    def save_brief(self, date_str: str, text: str) -> DailyBrief:
        brief = self._briefs.store_brief(date_str, text)
        self._logger.notice("BRIEFS", f"Brief stored for {date_str}")
        return brief

    def get_brief(self, date_str: str) -> Optional[DailyBrief]:
        return self._briefs.get_brief(date_str)

    def latest_briefs(self, n: int = 5) -> list[DailyBrief]:
        return self._briefs.latest_briefs(n)

    def add_todo(self, text: str) -> TodoItem:
        return self._briefs.add_todo(text)

    def todos(self, include_done: bool = False) -> list[TodoItem]:
        return self._briefs.list_todos(include_done)

    def complete_todo(self, index: int) -> bool:
        return self._briefs.mark_todo_done(index)

    def remove_todo(self, index: int) -> bool:
        return self._briefs.remove_todo(index)

    # ── Logging ───────────────────────────────────────────────────────

    def log(self, level: Level, source: str, message: str, extra: Optional[dict] = None) -> None:
        self._logger.log(level, source, message, extra)

    # ── System ────────────────────────────────────────────────────────

    def full_summary(self) -> dict[str, Any]:
        return {
            "structured": self._structured.stats(),
            "timeline": self._timeline.summary(),
            "vector_count": self._vector.count(),
            "chat_vector_count": self._chat_vector.count(),
            "briefs": len(self._briefs._briefs),
            "todos": len(self._briefs._todos),
        }


# ── Convenience aliases ──────────────────────────────────────────────────────

if __name__ == "__main__":
    mem = UnifiedMemory("./demo_memory")

    mem.remember("identity", "name", "Pragath")
    mem.remember("identity", "nickname", "RR")
    mem.remember("identity", "city", "Chennai")
    mem.remember("wishes", "purchase_intent", "Buy 5 sovereigns of gold jewelry")

    mem.chat("user", "Good morning, what's the news?")
    mem.chat("assistant", "Good morning RR! Here is today's briefing...")
    mem.log_task("web_search", "top world news today")
    mem.set_reminder("Listen to Freddy", "2026-07-18 09:00")
    mem.save_brief(str(date.today()), "Good evening RR. Your calendar is clear.")
    mem.add_todo("Finish the patent design")

    print("\n--- Semantic Recall ---")
    results = mem.recall("What did I want to buy?")
    for r in results:
        print(f" {r['text']} (distance: {r['distance']:.3f})")

    print("\n--- Keyword Search ---")
    for cat, key, entry in mem.search_keywords("gold"):
        print(f" [{cat}] {key}: {entry.value}")

    print("\n--- LLM Context ---")
    print(mem.context())

    print("\n--- System Summary ---")
    print(json.dumps(mem.full_summary(), indent=2))
