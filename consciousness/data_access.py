"""
consciousness/data_access.py
════════════════════════════════════════════════════════════════════════════
Unified Data Access Layer (DAL) for PRAGON.

WHY THIS EXISTS
    Before this module, five different call sites (SQLiteMemory, the
    UnifiedMemory _Timeline, rag_engine.py, moss.py, and the standalone
    ragsystem service) each opened their own sqlite3.connect(...) /
    chromadb.PersistentClient(...) independently -- even when two of them
    pointed at the very same file on disk. That's the "shared database
    anti-pattern" flagged in review: multiple components reaching around
    any service boundary straight into storage, with no single place
    enforcing pragmas, connection reuse, or (later) auth.

WHAT THIS MODULE DOES
    A single process-wide registry mediates every connection:

        from consciousness.data_access import dal
        conn = dal.get_sqlite("pragon_brain/pragon_brain.db")
        client = dal.get_chroma("consciousness/unified/chroma_db")

    Every caller asking for the same path gets back the *same* connection /
    client object -- opened once, configured once (WAL + busy_timeout for
    SQLite, anonymized_telemetry=False for Chroma), and cached. No component
    can silently open a second, uncoordinated handle to a file another
    component already owns.

    This is the in-process fix for the immediate concurrency issue. It is
    also the seam an HTTP boundary slots into later without touching every
    caller again: see data_access_service.py, which wraps this exact
    registry behind a small authenticated FastAPI service so components can
    be split into real, isolated processes (Orchestrator / MOSS / RAG /
    Backup Manager each talking over HTTP instead of opening files) without
    changing how callers ask for data.

USAGE NOTES
    - Paths passed in may be relative (resolved against the project root)
      or absolute; either way they're normalised before being used as the
      cache key, so "pragon_brain/pragon_brain.db" and an absolute path to
      the same file always hit the same cached connection.
    - This registry does not change anyone's schema or query logic -- it
      only owns *opening* the connection/client. Existing call sites keep
      their own table definitions and queries.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional


def _project_root() -> Path:
    # consciousness/ is one level below the project root.
    return Path(__file__).resolve().parent.parent


class DataAccessLayer:
    """Process-wide registry of SQLite connections and Chroma clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sqlite_conns: dict[str, sqlite3.Connection] = {}
        self._chroma_clients: dict[str, object] = {}

    # ── path normalisation ──────────────────────────────────────────────

    def _resolve(self, path: str | Path) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = _project_root() / p
        return p.resolve()

    # ── SQLite ───────────────────────────────────────────────────────────

    def get_sqlite(self, path: str | Path) -> sqlite3.Connection:
        """
        Return the shared, cached connection for this SQLite file, opening
        and configuring it (WAL + busy_timeout, Row factory) on first use.
        Thread-safe: callers may share the connection across threads, same
        as every pre-existing call site already assumed
        (check_same_thread=False).
        """
        key = str(self._resolve(path))
        with self._lock:
            conn = self._sqlite_conns.get(key)
            if conn is None:
                resolved = Path(key)
                resolved.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(key, check_same_thread=False, timeout=10)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=10000")
                self._sqlite_conns[key] = conn
                print(f"[DataAccessLayer] SQLite connection ready: {key}")
            return conn

    # ── ChromaDB ─────────────────────────────────────────────────────────

    def get_chroma(self, persist_dir: str | Path):
        """
        Return the shared, cached PersistentClient for this directory,
        creating it on first use. Requires chromadb to be installed;
        raises the underlying ImportError if it isn't (same behaviour
        callers already handled before this module existed).
        """
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        key = str(self._resolve(persist_dir))
        with self._lock:
            client = self._chroma_clients.get(key)
            if client is None:
                Path(key).mkdir(parents=True, exist_ok=True)
                client = chromadb.PersistentClient(
                    path=key, settings=ChromaSettings(anonymized_telemetry=False)
                )
                self._chroma_clients[key] = client
                print(f"[DataAccessLayer] Chroma client ready: {key}")
            return client

    # ── introspection (for a future admin/traceability view) ────────────

    def open_handles(self) -> dict[str, list[str]]:
        with self._lock:
            return {
                "sqlite": list(self._sqlite_conns.keys()),
                "chroma": list(self._chroma_clients.keys()),
            }

    def close_all(self) -> None:
        with self._lock:
            for conn in self._sqlite_conns.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self._sqlite_conns.clear()
            self._chroma_clients.clear()


# One registry for the whole process -- every component imports this same
# instance instead of constructing its own.
dal = DataAccessLayer()
