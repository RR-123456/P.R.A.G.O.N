"""
moss.py
════════════════════════════════════════════════════════════════════════════
MOSS — PRAGON's realtime context engine.

One retrieval layer, four capabilities:

    1. Realtime context retrieval   (measured, not promised)
    2. Shared context + session memory + multi-agent state
    3. Local semantic search (works with zero required dependencies)
    4. Continuous context evaluation + retrieval-grounded guardrails

                             ┌──────────────────┐
                             │    AI Agents      │
                             └────────┬──────────┘
                                      ▼
                         ┌──────────────────────┐
                         │   Moss Context API    │
                         └──────────┬────────────┘
                 ┌────────────────────┼────────────────────┐
                 ▼                    ▼                     ▼
           Session Memory      Semantic Search         Agent State
                 └────────────────────┼────────────────────┘
                                      ▼
                                   MOSS (sqlite)
                                      │
                 ┌────────────────────┼────────────────────┐
                 ▼                    ▼                     ▼
           Shared Context      Local Retrieval          Guardrails

Design notes
------------
* Single file, stdlib-only by default. sqlite3 is the store; a deterministic
  hashing-vectorizer is the zero-dependency local embedding fallback.
* If PRAGON's real embedding backend (consciousness/embeddings.py —
  sentence-transformers or Gemini) is importable, Moss uses it automatically
  for better semantic quality. Either way "local semantic search" keeps
  working with no network and no heavy install.
* Every retrieval call returns a LatencyTrace with the actual measured
  per-stage timings. Nothing here claims sub-10ms — it reports what it
  measures, every time.
* Guardrails fail safe: if context retrieval itself fails, the decision is
  BLOCK, never a silent ALLOW.
* Moss-specific storage/embedding details are isolated in this module.
  Callers use ContextStore.write / ContextRetriever.retrieve or the engine's
  own write/search/evaluate/guardrail methods and never see sqlite rows.

Usage
-----
    engine = MossContextEngine(db_path="pragon_moss.db")
    item_id = await engine.write("session_123", "Customer confirmed fan is running.",
                                  {"type": "observation", "agent": "diagnostic_agent"})
    hits = await engine.search("session_123", "what has been confirmed?")
    verdict = await engine.evaluate("session_123", "what equipment are we working with?")
    decision = await engine.guardrail("session_123", action="Proceed with the procedure.",
                                       required_policy_query="power isolation procedure")

CLI
---
    python moss.py demo                # runs the 6-step demo scenario end to end
    python moss.py benchmark --n 500   # measures real p50/p95/p99 latency
    python moss.py serve               # optional FastAPI surface (needs fastapi+uvicorn)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional


# ══════════════════════════════════════════════════════════════════════════
# 1. Context priority (#9)
# ══════════════════════════════════════════════════════════════════════════

class Priority:
    CRITICAL = "CRITICAL"
    IMPORTANT = "IMPORTANT"
    RELEVANT = "RELEVANT"
    BACKGROUND = "BACKGROUND"


_PRIORITY_RULES = {
    "safety": Priority.CRITICAL,
    "policy": Priority.CRITICAL,
    "guardrail": Priority.CRITICAL,
    "state": Priority.IMPORTANT,
    "observation": Priority.IMPORTANT,
    "decision": Priority.IMPORTANT,
    "task": Priority.IMPORTANT,
    "knowledge": Priority.RELEVANT,
    "fact": Priority.RELEVANT,
}


def classify_priority(metadata: dict) -> str:
    return _PRIORITY_RULES.get((metadata or {}).get("type", ""), Priority.BACKGROUND)


# ══════════════════════════════════════════════════════════════════════════
# 2. Embeddings — pluggable, degrades gracefully (#6, local semantic search)
# ══════════════════════════════════════════════════════════════════════════

class _HashingVectorizer:
    """Zero-dependency local embedding fallback: a deterministic bag-of-words
    hashing vectorizer scored with cosine similarity. Not a neural embedding
    model — but it needs no install, no network and no GPU, which is the
    point of Moss's 'local semantic search' path: FAQ/notes/device-info
    scale corpora work fine with this."""

    DIM = 512
    _WORD_RE = re.compile(r"[a-z0-9]+")

    def embed_one(self, text: str) -> list:
        vec = [0.0] * self.DIM
        for word in self._WORD_RE.findall((text or "").lower()):
            idx = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16) % self.DIM
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_many(self, texts):
        return [self.embed_one(t) for t in texts]


def _load_embedder(backend: str = "auto", api_key_fn: Optional[Callable[[], str]] = None):
    """Prefer PRAGON's real EmbeddingBackend (sentence-transformers locally,
    or Gemini) when it's importable and available; otherwise fall back to
    the hashing vectorizer. Either way Moss always has a working local
    retrieval path — no hard dependency, no unsupported capability assumed."""
    if backend != "hashing":
        try:
            from consciousness.embeddings import EmbeddingBackend  # PRAGON's own module
            real_backend = "gemini" if backend == "gemini" else "local"
            eb = EmbeddingBackend(backend=real_backend, api_key_fn=api_key_fn)
            if eb.available:
                return eb, real_backend
        except Exception:
            pass
    return _HashingVectorizer(), "hashing"


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# ══════════════════════════════════════════════════════════════════════════
# 3. Latency tracing (#14) + metrics registry (backs GET /metrics, #17)
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class LatencyTrace:
    query_build_ms: float = 0.0
    moss_latency_ms: float = 0.0
    normalization_ms: float = 0.0
    evaluation_ms: float = 0.0
    total_context_latency_ms: float = 0.0

    def as_dict(self) -> dict:
        return {k: round(v, 3) for k, v in self.__dict__.items()}


@contextmanager
def _timer(box: dict):
    start = time.perf_counter()
    try:
        yield
    finally:
        box["ms"] = (time.perf_counter() - start) * 1000.0


class MetricsRegistry:
    """Rolling window of real, measured end-to-end retrieval latencies."""

    def __init__(self, maxlen: int = 5000):
        self._lat: list = []
        self._maxlen = maxlen
        self._lock = threading.Lock()

    def record(self, ms: float) -> None:
        with self._lock:
            self._lat.append(ms)
            if len(self._lat) > self._maxlen:
                self._lat.pop(0)

    def snapshot(self) -> dict:
        with self._lock:
            data = sorted(self._lat)
        if not data:
            return {"count": 0}

        def pct(p):
            idx = min(len(data) - 1, int(round(p / 100 * (len(data) - 1))))
            return round(data[idx], 3)

        return {
            "count": len(data),
            "p50_ms": pct(50), "p95_ms": pct(95), "p99_ms": pct(99),
            "min_ms": round(data[0], 3), "max_ms": round(data[-1], 3),
        }


# ══════════════════════════════════════════════════════════════════════════
# 4. Context cache (#15) — explicit invalidation, toggleable for benchmarks
# ══════════════════════════════════════════════════════════════════════════

class ContextCache:
    def __init__(self, capacity: int = 256):
        self.capacity = capacity
        self._store: "OrderedDict[str, list]" = OrderedDict()
        self.enabled = True
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(session_id: str, query: str, top_k: int) -> str:
        return hashlib.sha256(f"{session_id}|{query}|{top_k}".encode("utf-8")).hexdigest()

    def get(self, k: str):
        if not self.enabled:
            return None
        v = self._store.get(k)
        if v is None:
            self.misses += 1
            return None
        self.hits += 1
        self._store.move_to_end(k)
        return v

    def put(self, k: str, v: list) -> None:
        if not self.enabled:
            return
        self._store[k] = v
        self._store.move_to_end(k)
        if len(self._store) > self.capacity:
            self._store.popitem(last=False)

    def invalidate(self) -> None:
        # Every write/update/delete clears the whole cache. Cache keys are
        # opaque hashes of (session, query, top_k), so partial invalidation
        # by session would need a second index just to save a few entries —
        # not worth it. This keeps invalidation explicit and impossible to
        # get subtly wrong, as the spec asks for.
        self._store.clear()


# ══════════════════════════════════════════════════════════════════════════
# 5. Moss engine — write / search / update / delete / evaluate / guardrail
# ══════════════════════════════════════════════════════════════════════════

class MossContextEngine:
    """The central retrieval/context layer. SQLite-backed, pluggable
    embeddings, single process. Every public method is async so it drops
    straight into an agent's event loop without blocking it (sqlite calls
    run in a thread via asyncio.to_thread)."""

    def __init__(self, db_path: str = "moss_context.db", embed_backend: str = "auto",
                 api_key_fn: Optional[Callable[[], str]] = None, cache_capacity: int = 256):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()
        self.embedder, self.embed_backend_name = _load_embedder(embed_backend, api_key_fn)
        self.cache = ContextCache(capacity=cache_capacity)
        self.metrics = MetricsRegistry()

    # ── storage plumbing ──
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS moss_context (
                id         TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                content    TEXT NOT NULL,
                metadata   TEXT NOT NULL,
                embedding  TEXT NOT NULL,
                priority   TEXT NOT NULL,
                ts         REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_moss_session ON moss_context(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_moss_ts ON moss_context(ts)")
        conn.commit()
        conn.close()

    # ── WRITE PATH (#11, #2 shared context, #3 session memory, #5 semantic history) ──
    async def write(self, session_id: str, content: str, metadata: Optional[dict] = None) -> str:
        try:
            return await asyncio.to_thread(self._write_sync, session_id, content, metadata or {})
        except Exception as exc:
            raise MossError(f"Moss write failed: {exc}") from exc

    def _write_sync(self, session_id: str, content: str, metadata: dict) -> str:
        item_id = uuid.uuid4().hex
        vec = self.embedder.embed_one(content)
        priority = classify_priority(metadata)
        conn = self._conn()
        conn.execute(
            "INSERT INTO moss_context (id, session_id, content, metadata, embedding, priority, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (item_id, session_id, content, json.dumps(metadata), json.dumps(vec), priority, time.time()),
        )
        conn.commit()
        self.cache.invalidate()
        return item_id

    async def update(self, item_id: str, content: Optional[str] = None,
                      metadata: Optional[dict] = None) -> bool:
        return await asyncio.to_thread(self._update_sync, item_id, content, metadata)

    def _update_sync(self, item_id: str, content: Optional[str], metadata: Optional[dict]) -> bool:
        conn = self._conn()
        row = conn.execute("SELECT * FROM moss_context WHERE id=?", (item_id,)).fetchone()
        if not row:
            return False
        new_content = content if content is not None else row["content"]
        new_meta = {**json.loads(row["metadata"]), **(metadata or {})}
        vec = self.embedder.embed_one(new_content) if content is not None else json.loads(row["embedding"])
        conn.execute(
            "UPDATE moss_context SET content=?, metadata=?, embedding=?, priority=?, ts=? WHERE id=?",
            (new_content, json.dumps(new_meta), json.dumps(vec), classify_priority(new_meta), time.time(), item_id),
        )
        conn.commit()
        self.cache.invalidate()
        return True

    async def delete(self, item_id: str) -> bool:
        return await asyncio.to_thread(self._delete_sync, item_id)

    def _delete_sync(self, item_id: str) -> bool:
        conn = self._conn()
        cur = conn.execute("DELETE FROM moss_context WHERE id=?", (item_id,))
        conn.commit()
        self.cache.invalidate()
        return cur.rowcount > 0

    # ── READ / SEARCH PATH (#12 read path, #1 realtime, #10 temporal, #14 tracing) ──
    async def search(self, session_id: str, query: str, top_k: int = 5,
                      use_cache: bool = True, recency_half_life_s: float = 3600.0) -> dict:
        trace = LatencyTrace()
        t0 = time.perf_counter()

        box = {}
        with _timer(box):
            cache_key = self.cache.key(session_id, query, top_k)
        trace.query_build_ms = box["ms"]

        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                trace.total_context_latency_ms = (time.perf_counter() - t0) * 1000.0
                self.metrics.record(trace.total_context_latency_ms)
                return {"results": cached, "trace": trace.as_dict(), "cache_hit": True}

        box = {}
        with _timer(box):
            rows = await asyncio.to_thread(self._search_sync, session_id, query, top_k, recency_half_life_s)
        trace.moss_latency_ms = box["ms"]

        box = {}
        with _timer(box):
            results = list(rows)
        trace.normalization_ms = box["ms"]

        if use_cache:
            self.cache.put(cache_key, results)

        trace.total_context_latency_ms = (time.perf_counter() - t0) * 1000.0
        self.metrics.record(trace.total_context_latency_ms)
        return {"results": results, "trace": trace.as_dict(), "cache_hit": False}

    def _search_sync(self, session_id: str, query: str, top_k: int, half_life_s: float) -> list:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM moss_context WHERE session_id=?", (session_id,)).fetchall()
        if not rows:
            return []
        qvec = self.embedder.embed_one(query)
        now = time.time()
        priority_boost = {"CRITICAL": 0.15, "IMPORTANT": 0.08, "RELEVANT": 0.0, "BACKGROUND": -0.08}
        scored = []
        for row in rows:
            sim = _cosine(qvec, json.loads(row["embedding"]))
            age_s = max(0.0, now - row["ts"])
            recency = 0.5 ** (age_s / half_life_s) if half_life_s > 0 else 1.0
            score = 0.7 * sim + 0.2 * recency + priority_boost.get(row["priority"], 0.0)
            scored.append((score, sim, recency, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "id": row["id"], "session_id": row["session_id"], "content": row["content"],
                "metadata": json.loads(row["metadata"]), "priority": row["priority"],
                "timestamp": row["ts"], "score": round(score, 4),
                "similarity": round(sim, 4), "recency": round(recency, 4),
            }
            for score, sim, recency, row in scored[:top_k]
        ]

    async def session(self, session_id: str, limit: int = 200) -> list:
        return await asyncio.to_thread(self._session_sync, session_id, limit)

    def _session_sync(self, session_id: str, limit: int) -> list:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM moss_context WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [
            {"id": r["id"], "content": r["content"], "metadata": json.loads(r["metadata"]),
             "priority": r["priority"], "timestamp": r["ts"]}
            for r in rows
        ]

    # ── CONTEXT EVALUATION (#7) ──
    async def evaluate(self, session_id: str, query: str, top_k: int = 5) -> dict:
        t0 = time.perf_counter()
        search_res = await self.search(session_id, query, top_k=top_k, use_cache=False)
        results = search_res["results"]

        if not results:
            return {
                "context_status": "missing",
                "reason": "No relevant context found for this query.",
                "confidence": 0.0, "conflicts": [], "results": [],
                "trace_ms": round((time.perf_counter() - t0) * 1000, 3),
            }

        # Conflict detection: the same metadata field holding different
        # values across the retrieved items (e.g. two different equipment
        # models mentioned in the same session).
        by_key: dict = {}
        for r in results:
            for k, v in (r["metadata"] or {}).items():
                if k in ("type", "agent", "confirmed"):
                    continue
                by_key.setdefault(k, {}).setdefault(str(v), []).append(r)

        conflicts = []
        for key, values in by_key.items():
            if len(values) > 1:
                newest = {val: max(i["timestamp"] for i in items) for val, items in values.items()}
                conflicts.append({
                    "field": key, "values": list(values.keys()),
                    "latest_value": max(newest, key=newest.get),
                })

        top = results[0]
        if conflicts:
            status = "conflict"
        elif top["recency"] < 0.2:
            status = "stale"
        elif top["similarity"] > 0.35:
            status = "relevant"
        else:
            status = "weak"

        confidence = round(max(0.0, min(1.0, 0.6 * top["similarity"] + 0.4 * top["recency"])), 3)
        reason = (
            f"Conflicting values for: {', '.join(c['field'] for c in conflicts)}"
            if conflicts else
            f"Best match similarity={top['similarity']}, recency={top['recency']}"
        )
        return {
            "context_status": status, "reason": reason, "confidence": confidence,
            "conflicts": conflicts, "results": results,
            "trace_ms": round((time.perf_counter() - t0) * 1000, 3),
        }

    # ── REALTIME GUARDRAILS (#8) — retrieval-grounded, fails safe (#20) ──
    async def guardrail(self, session_id: str, action: str,
                         required_policy_query: Optional[str] = None, top_k: int = 5) -> dict:
        query = required_policy_query or action
        try:
            search_res = await self.search(session_id, query, top_k=top_k, use_cache=False)
        except Exception as exc:
            # Moss unavailable → do NOT silently approve. Fail safe.
            return {"decision": "BLOCK",
                    "reason": f"Guardrail context retrieval failed ({exc}); failing safe.",
                    "results": []}

        policy_items = [
            r for r in search_res["results"]
            if r["priority"] == Priority.CRITICAL
            or (r["metadata"] or {}).get("type") in ("policy", "safety", "guardrail")
        ]

        if not policy_items:
            return {"decision": "ALLOW",
                    "reason": "No relevant policy/safety context found for this action.",
                    "results": search_res["results"]}

        unconfirmed = [p for p in policy_items if not (p["metadata"] or {}).get("confirmed", False)]
        if unconfirmed:
            reasons = "; ".join(p["content"] for p in unconfirmed[:3])
            return {"decision": "WARN",
                    "reason": f"Relevant safety context found but not confirmed: {reasons}",
                    "results": policy_items}

        return {"decision": "ALLOW",
                "reason": "Relevant safety/policy context found and confirmed.",
                "results": policy_items}

    def metrics_snapshot(self) -> dict:
        return {
            "latency": self.metrics.snapshot(),
            "cache": {"hits": self.cache.hits, "misses": self.cache.misses,
                      "enabled": self.cache.enabled, "size": len(self.cache._store)},
            "embed_backend": self.embed_backend_name,
        }


class MossError(RuntimeError):
    """Raised on write-path failures (network/auth/rate-limit for a Gemini
    embed backend, disk errors for sqlite, etc). Kept separate from generic
    exceptions so callers — especially guardrail() — can fail safe on it."""


# ══════════════════════════════════════════════════════════════════════════
# 6. Thin convenience wrappers matching the spec's write/read path shapes
# ══════════════════════════════════════════════════════════════════════════

class ContextStore:
    def __init__(self, engine: MossContextEngine):
        self._engine = engine

    async def write(self, session_id: str, content: str, metadata: dict):
        return await self._engine.write(session_id, content, metadata)


class ContextRetriever:
    def __init__(self, engine: MossContextEngine):
        self._engine = engine

    async def retrieve(self, session_id: str, query: str, top_k: int = 5):
        res = await self._engine.search(session_id, query, top_k=top_k)
        return res["results"]


# ══════════════════════════════════════════════════════════════════════════
# 7. Optional HTTP surface (#13) — only imports fastapi if actually called
# ══════════════════════════════════════════════════════════════════════════

def create_api(engine: MossContextEngine):
    """POST /context/write, /context/search, /context/evaluate,
    /context/guardrail · GET /context/session/{id} · GET /metrics.
    Needs `pip install fastapi uvicorn` — the rest of this module has no
    web-framework dependency at all."""
    from fastapi import Depends, FastAPI
    from pydantic import BaseModel, Field

    try:
        from api.security import require_auth_dep
    except ImportError:
        require_auth_dep = None
        print("[MOSS] WARNING: api.security not importable -- HTTP surface "
              "running without auth.")

    app = FastAPI(title="Moss Context Engine")
    # Every route here can write into or read out of a user's context store,
    # so all of them require a bearer token (see api/security.py) -- unlike
    # ragsystem's /health, there's no unauthenticated liveness route to
    # carve out here since even /metrics leaks session activity counts.
    _AUTH = [Depends(require_auth_dep)] if require_auth_dep else []

    class WriteReq(BaseModel):
        session_id: str = Field(..., min_length=1, max_length=200)
        content: str = Field(..., min_length=1, max_length=20000)
        metadata: dict = {}

    class SearchReq(BaseModel):
        session_id: str = Field(..., min_length=1, max_length=200)
        query: str = Field(..., min_length=1, max_length=2000)
        top_k: int = Field(default=5, ge=1, le=50)

    class GuardrailReq(BaseModel):
        session_id: str = Field(..., min_length=1, max_length=200)
        action: str = Field(..., min_length=1, max_length=500)
        policy_query: Optional[str] = Field(default=None, max_length=2000)

    @app.post("/context/write", dependencies=_AUTH)
    async def _write(req: WriteReq):
        return {"id": await engine.write(req.session_id, req.content, req.metadata)}

    @app.post("/context/search", dependencies=_AUTH)
    async def _search(req: SearchReq):
        return await engine.search(req.session_id, req.query, req.top_k)

    @app.post("/context/evaluate", dependencies=_AUTH)
    async def _evaluate(req: SearchReq):
        return await engine.evaluate(req.session_id, req.query, req.top_k)

    @app.post("/context/guardrail", dependencies=_AUTH)
    async def _guardrail(req: GuardrailReq):
        return await engine.guardrail(req.session_id, req.action, req.policy_query)

    @app.get("/context/session/{session_id}", dependencies=_AUTH)
    async def _session(session_id: str, limit: int = 200):
        return await engine.session(session_id, limit)

    @app.get("/metrics", dependencies=_AUTH)
    async def _metrics():
        return engine.metrics_snapshot()

    return app


# ══════════════════════════════════════════════════════════════════════════
# 8. Real (labeled) recall/MRR evaluation (#16) — no fabricated numbers
# ══════════════════════════════════════════════════════════════════════════

async def evaluate_recall(engine: MossContextEngine, session_id: str,
                           labeled_queries: list, top_k: int = 5) -> dict:
    """labeled_queries: list of (query, {expected_item_ids}). There's no
    universal ground truth to fake here — plug in real labels for your own
    corpus and this reports real Recall@1/3/5, MRR, and empty-result rate."""
    r_at = {1: 0, 3: 0, 5: 0}
    reciprocal_ranks = []
    empty = 0
    for query, expected_ids in labeled_queries:
        res = await engine.search(session_id, query, top_k=max(top_k, 5), use_cache=False)
        ids = [r["id"] for r in res["results"]]
        if not ids:
            empty += 1
            reciprocal_ranks.append(0.0)
            continue
        rank = next((i + 1 for i, rid in enumerate(ids) if rid in expected_ids), None)
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        for k in (1, 3, 5):
            if any(rid in expected_ids for rid in ids[:k]):
                r_at[k] += 1
    n = len(labeled_queries) or 1
    return {
        "recall@1": round(r_at[1] / n, 3), "recall@3": round(r_at[3] / n, 3),
        "recall@5": round(r_at[5] / n, 3), "mrr": round(sum(reciprocal_ranks) / n, 3),
        "empty_result_rate": round(empty / n, 3),
    }


# ══════════════════════════════════════════════════════════════════════════
# 9. Demo scenario (#18) — one script, all four capabilities
# ══════════════════════════════════════════════════════════════════════════

def _demo_dataset() -> list:
    return [
        ("Customer is using ArcticAir X500.", {"type": "observation", "agent": "intake_agent", "equipment": "X500"}),
        ("Issue reported: error code E17.", {"type": "observation", "agent": "intake_agent", "issue": "E17"}),
        ("For E17, check refrigerant pressure and condenser fan.", {"type": "knowledge", "agent": "kb"}),
        ("Condenser fan confirmed operational by technician.", {"type": "state", "agent": "diagnostic_agent", "confirmed": True}),
        ("Safety: isolate power before opening the compressor housing.", {"type": "safety", "agent": "policy", "confirmed": False}),
        ("Equipment updated to ArcticAir X700 per customer correction.", {"type": "observation", "agent": "intake_agent", "equipment": "X700"}),
    ]


async def run_demo(engine: MossContextEngine, session_id: str = "demo-session") -> None:
    print("\n=== MOSS DEMO SCENARIO ===\n")

    # STEP 1 — Agent A writes session context
    for content, meta in _demo_dataset():
        await engine.write(session_id, content, meta)
    print("[Step 1] Agent A wrote session context.")

    # STEP 2 — Agent B retrieves shared context
    r1 = await engine.search(session_id, "What equipment and issue are we working with?")
    print("[Step 2] Agent B retrieved shared context:", r1["trace"],
          "->", [x["content"] for x in r1["results"][:2]])

    # STEP 3 — Semantic knowledge retrieval
    r2 = await engine.search(session_id, "What should I check for this issue?")
    print("[Step 3] Semantic retrieval:", r2["trace"], "->", [x["content"] for x in r2["results"][:2]])

    # STEP 4 — Multi-agent state
    r3 = await engine.search(session_id, "Has the condenser fan been confirmed operational?")
    print("[Step 4] Agent C reads Agent B's state:", [x["content"] for x in r3["results"][:1]])

    # STEP 5 — Guardrail
    gr = await engine.guardrail(session_id, action="Proceed with the procedure.",
                                 required_policy_query="power isolation procedure")
    print("[Step 5] Guardrail decision:", gr["decision"], "-", gr["reason"])

    # STEP 6 — Metrics
    print("[Step 6] Metrics:", engine.metrics_snapshot())
    print()


async def run_benchmark(engine: MossContextEngine, session_id: str = "bench-session", n: int = 200) -> None:
    """Real, measured p50/p95/p99 latency over n queries against a small
    written corpus, plus the non-empty-result rate. No hardcoded numbers —
    everything printed here comes from timers around the actual calls."""
    for content, meta in _demo_dataset():
        await engine.write(session_id, content, meta)

    queries = [
        "What equipment and issue are we working with?",
        "What should I check for this issue?",
        "Has the fan been confirmed operational?",
        "What safety step is required before opening the housing?",
        "What is the current equipment model?",
    ]

    lat = []
    non_empty = 0
    for i in range(n):
        q = queries[i % len(queries)]
        t0 = time.perf_counter()
        res = await engine.search(session_id, q, top_k=5, use_cache=False)
        lat.append((time.perf_counter() - t0) * 1000.0)
        if res["results"]:
            non_empty += 1

    lat.sort()

    def pct(p):
        idx = min(len(lat) - 1, int(round(p / 100 * (len(lat) - 1))))
        return round(lat[idx], 3)

    print("\nMoss Context Retrieval Benchmark\n")
    print(f"Queries: {n}\n")
    print("Latency")
    print("-------")
    print(f"p50: {pct(50)} ms")
    print(f"p95: {pct(95)} ms")
    print(f"p99: {pct(99)} ms\n")
    print("Retrieval")
    print("---------")
    print(f"Non-empty-result rate: {round(non_empty / n, 3)}")
    print(f"Embed backend: {engine.embed_backend_name}\n")
    print("(For real Recall@k/MRR against your own corpus, use evaluate_recall()")
    print(" with a labeled query set — there's no universal ground truth to fake.)\n")


# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Moss context engine — demo / benchmark / serve")
    parser.add_argument("mode", choices=["demo", "benchmark", "serve"], nargs="?", default="demo")
    parser.add_argument("--db", default="moss_context.db")
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    engine = MossContextEngine(db_path=args.db)

    if args.mode == "demo":
        asyncio.run(run_demo(engine))
    elif args.mode == "benchmark":
        asyncio.run(run_benchmark(engine, n=args.n))
    else:
        import uvicorn
        ssl_kwargs = {}
        try:
            from api.security import ensure_tls_cert
            certfile, keyfile = ensure_tls_cert()
            ssl_kwargs = {"ssl_certfile": certfile, "ssl_keyfile": keyfile}
        except ImportError:
            print("[MOSS] WARNING: api.security not importable -- serving "
                  "the HTTP surface over plain HTTP, not TLS.")
        uvicorn.run(create_api(engine), host=args.host, port=args.port, **ssl_kwargs)
