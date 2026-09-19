"""
pragon_moss/bridge.py
════════════════════════════════════════════════════════════════════════════
A synchronous, never-raising facade over moss.MossContextEngine.

Why this exists
---------------
moss.py is fully async. Most of PRAGON's hot paths that want context
(_friday_processor, _ghost_agent_turn, _ghost_execute_tool, _rag_query) are
plain synchronous functions running on worker threads, not on an event loop.
Calling asyncio.run() inside them would spin up and tear down a loop on every
single turn, and calling it from a thread that already has a running loop
would explode.

So the bridge owns ONE background event loop on ONE daemon thread for the
whole process, and exposes blocking methods that hand coroutines to that loop
via run_coroutine_threadsafe with a hard timeout.

Contract with the rest of PRAGON
--------------------------------
* Nothing in here ever raises into a caller. Every method degrades to a safe
  default (empty list / empty string / None) if Moss is disabled, missing,
  slow, or broken. PRAGON must behave exactly as it did before if Moss dies.
* The one deliberate exception to "safe default" is guardrail(), which fails
  SAFE rather than quiet: a retrieval failure there returns BLOCK, matching
  moss.py's own behaviour.
* Nothing in PRAGON is removed, replaced, or rewritten by this module. It is
  additive only.

Environment switches (all optional)
-----------------------------------
    PRAGON_MOSS=0                 hard-disable the whole integration
    PRAGON_MOSS_DB=<path>         sqlite file (default: <base>/pragon_moss.db)
    PRAGON_MOSS_EMBED=auto        auto | local | gemini | hashing
    PRAGON_MOSS_TOPK=5            default hits per retrieval
    PRAGON_MOSS_TIMEOUT=2.5       per-call budget in seconds
    PRAGON_MOSS_SESSION=pragon    session id prefix (memory persists per id)
    PRAGON_MOSS_HALFLIFE=86400    recency half-life in seconds
    PRAGON_MOSS_DEBUG=1           print bridge diagnostics to stdout
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Project root on sys.path so `import moss` and `consciousness.embeddings`
# both resolve no matter where PRAGON was launched from.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


DEBUG = _env_flag("PRAGON_MOSS_DEBUG", False)


def _dbg(msg: str) -> None:
    if DEBUG:
        print(f"[moss] {msg}", flush=True)


class MossBridge:
    """Blocking, fail-soft access to a single process-wide Moss engine."""

    _singleton_lock = threading.Lock()
    _singleton: "MossBridge | None" = None

    # ── construction ────────────────────────────────────────────────────
    def __init__(
        self,
        db_path: Optional[str] = None,
        embed_backend: Optional[str] = None,
        api_key_fn: Optional[Callable[[], str]] = None,
        enabled: Optional[bool] = None,
    ):
        self.enabled = _env_flag("PRAGON_MOSS", True) if enabled is None else enabled
        self.timeout = _env_float("PRAGON_MOSS_TIMEOUT", 2.5)
        self.top_k = _env_int("PRAGON_MOSS_TOPK", 5)
        self.half_life = _env_float("PRAGON_MOSS_HALFLIFE", 86400.0)
        self.session_prefix = os.environ.get("PRAGON_MOSS_SESSION", "pragon")
        self.db_path = db_path or os.environ.get(
            "PRAGON_MOSS_DB", str(_ROOT / "pragon_moss.db")
        )
        self.embed_backend = embed_backend or os.environ.get("PRAGON_MOSS_EMBED", "auto")

        self.engine = None
        self.error: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._writes = 0
        self._reads = 0
        self._failures = 0

        if not self.enabled:
            _dbg("disabled via PRAGON_MOSS")
            return

        try:
            from moss import MossContextEngine  # noqa: WPS433 - deliberate late import

            self._start_loop()
            self.engine = MossContextEngine(
                db_path=self.db_path,
                embed_backend=self.embed_backend,
                api_key_fn=api_key_fn,
            )
            _dbg(f"engine ready · db={self.db_path} · embed={self.engine.embed_backend_name}")
        except Exception as exc:  # moss missing, sqlite locked, anything
            self.enabled = False
            self.error = f"{type(exc).__name__}: {exc}"
            _dbg(f"init failed, staying dark: {self.error}")

    @classmethod
    def instance(cls, **kwargs) -> "MossBridge":
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls(**kwargs)
            return cls._singleton

    # ── event loop plumbing ─────────────────────────────────────────────
    def _start_loop(self) -> None:
        ready = threading.Event()

        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=_runner, name="moss-loop", daemon=True)
        self._thread.start()
        ready.wait(timeout=5.0)

    def _call(self, coro, default=None, label: str = ""):
        """Run a Moss coroutine on the bridge loop and block for the result."""
        if not self.enabled or self._loop is None:
            return default
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return fut.result(timeout=self.timeout)
        except Exception as exc:
            self._failures += 1
            _dbg(f"{label or 'call'} failed: {type(exc).__name__}: {exc}")
            return default

    # ── session naming ──────────────────────────────────────────────────
    def session_id(self, channel: str = "main") -> str:
        return f"{self.session_prefix}:{channel}"

    # ── write path ──────────────────────────────────────────────────────
    def remember(
        self,
        content: str,
        metadata: Optional[dict] = None,
        channel: str = "main",
    ) -> Optional[str]:
        """Store one context item. Returns its id, or None if Moss is dark."""
        if not self.enabled or not (content or "").strip():
            return None
        self._writes += 1
        return self._call(
            self.engine.write(self.session_id(channel), content.strip(), metadata or {}),
            default=None,
            label="write",
        )

    # ── read path ───────────────────────────────────────────────────────
    def recall(
        self,
        query: str,
        top_k: Optional[int] = None,
        channel: str = "main",
        use_cache: bool = True,
    ) -> list:
        if not self.enabled or not (query or "").strip():
            return []
        self._reads += 1
        res = self._call(
            self.engine.search(
                self.session_id(channel),
                query.strip(),
                top_k=top_k or self.top_k,
                use_cache=use_cache,
                recency_half_life_s=self.half_life,
            ),
            default=None,
            label="search",
        )
        if not res:
            return []
        self._last_trace = res.get("trace", {})
        return res.get("results", []) or []

    def recall_traced(self, query: str, top_k: Optional[int] = None,
                      channel: str = "main") -> dict:
        """Same as recall() but keeps the LatencyTrace, for the HUD/metrics."""
        if not self.enabled or not (query or "").strip():
            return {"results": [], "trace": {}, "cache_hit": False}
        self._reads += 1
        return self._call(
            self.engine.search(
                self.session_id(channel), query.strip(),
                top_k=top_k or self.top_k, recency_half_life_s=self.half_life,
            ),
            default={"results": [], "trace": {}, "cache_hit": False},
            label="search",
        )

    def session(self, channel: str = "main", limit: int = 200) -> list:
        return self._call(
            self.engine.session(self.session_id(channel), limit) if self.enabled else None,
            default=[],
            label="session",
        ) or []

    # ── evaluation + guardrails ─────────────────────────────────────────
    def evaluate(self, query: str, channel: str = "main", top_k: Optional[int] = None) -> dict:
        if not self.enabled:
            return {"context_status": "disabled", "confidence": 0.0,
                    "conflicts": [], "results": [], "reason": "Moss disabled."}
        return self._call(
            self.engine.evaluate(self.session_id(channel), query, top_k or self.top_k),
            default={"context_status": "unavailable", "confidence": 0.0,
                     "conflicts": [], "results": [], "reason": "Moss evaluate timed out."},
            label="evaluate",
        )

    def guardrail(self, action: str, policy_query: Optional[str] = None,
                  channel: str = "main") -> dict:
        """Retrieval-grounded check. Fails SAFE: if Moss can't answer, BLOCK.
        Callers decide what to do with a BLOCK (see integration.py's three
        guardrail modes: off / warn / enforce)."""
        if not self.enabled:
            # Explicitly disabled is not a failure -- it's opt-out.
            return {"decision": "ALLOW", "reason": "Moss guardrails disabled.", "results": []}
        return self._call(
            self.engine.guardrail(self.session_id(channel), action, policy_query),
            default={"decision": "BLOCK",
                     "reason": "Moss guardrail retrieval timed out; failing safe.",
                     "results": []},
            label="guardrail",
        )

    # ── prompt shaping ──────────────────────────────────────────────────
    @staticmethod
    def format_block(results: list, trace: Optional[dict] = None,
                     header: str = "MOSS CONTEXT") -> str:
        """Render retrieved items as a compact, priority-labelled block that
        reads cleanly inside a system/user prompt. Empty string when there's
        nothing worth injecting -- callers can concatenate unconditionally."""
        if not results:
            return ""
        lines = []
        ms = (trace or {}).get("total_context_latency_ms")
        stamp = f" · retrieved in {ms} ms" if ms is not None else ""
        lines.append(f"[{header} — {len(results)} item(s){stamp}]")
        for r in results:
            prio = r.get("priority", "BACKGROUND")
            meta = r.get("metadata") or {}
            who = meta.get("agent") or meta.get("type") or "context"
            lines.append(f"  - ({prio}/{who}) {r.get('content','').strip()}")
        lines.append(f"[END {header}]")
        return "\n".join(lines)

    def augment(self, text: str, top_k: Optional[int] = None,
                channel: str = "main", header: str = "MOSS CONTEXT") -> str:
        """Prefix `text` with retrieved session context. Returns `text`
        unchanged when Moss is dark or has nothing relevant -- so it is always
        safe to wrap a prompt in this."""
        if not self.enabled or not (text or "").strip():
            return text
        res = self.recall_traced(text, top_k=top_k, channel=channel)
        block = self.format_block(res.get("results", []), res.get("trace"), header)
        if not block:
            return text
        return f"{block}\n\n{text}"

    # ── introspection ───────────────────────────────────────────────────
    def status(self) -> dict:
        base = {
            "enabled": self.enabled,
            "db_path": self.db_path,
            "error": self.error,
            "writes": self._writes,
            "reads": self._reads,
            "failures": self._failures,
            "timeout_s": self.timeout,
            "top_k": self.top_k,
            "session_prefix": self.session_prefix,
        }
        if self.enabled and self.engine is not None:
            try:
                base["engine"] = self.engine.metrics_snapshot()
            except Exception:
                pass
        return base


def get_bridge(api_key_fn: Optional[Callable[[], str]] = None) -> MossBridge:
    """Process-wide accessor. First caller wins on configuration; later calls
    just get the same instance back."""
    return MossBridge.instance(api_key_fn=api_key_fn)
