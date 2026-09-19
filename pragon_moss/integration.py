"""
pragon_moss/integration.py
════════════════════════════════════════════════════════════════════════════
Wires MOSS into PRAGON **without changing a single line of PRAGON**.

Everything here is a wrapper. Each patched attribute keeps a reference to the
original callable and always calls it. No PRAGON behaviour is removed,
short-circuited or rewritten — Moss only:

    1. reads  → injects retrieved session context into a prompt before the
                original function sees it,
    2. writes → records the turn/tool result after the original returns,
    3. checks → runs a retrieval-grounded guardrail alongside (never instead
                of) PRAGON's own confirm_gate.

What gets wrapped
-----------------
    JarvisLive._friday_processor    RAG-grounded chat  → context + memory
    JarvisLive._ghost_agent_turn    tool-calling agent → context + memory
    JarvisLive._ghost_execute_tool  every GHOST action → guardrail + audit
    JarvisLive._execute_tool        every JARVIS voice action → audit
    pragon_main._rag_query          knowledge-base lookups → Moss recall appended

Guardrail modes (PRAGON_MOSS_GUARDRAIL)
---------------------------------------
    off      never consult the guardrail
    warn     (default) consult it, log WARN/BLOCK to the GHOST log, still run
    enforce  a BLOCK verdict stops the tool and returns the reason as the
             tool result instead of executing it

`warn` is the default on purpose: an unconfigured Moss database has no safety
items in it, and silently blocking a user's actions on day one would be worse
than useless. Populate safety/policy context first, then flip to enforce.

Idempotent: calling install() twice is a no-op.
"""

from __future__ import annotations

import functools

import json
import os
import time
from typing import Any, Optional

from .bridge import get_bridge, _dbg, _env_flag

_INSTALLED = False
_MARK = "_moss_wrapped"


def _mode() -> str:
    m = (os.environ.get("PRAGON_MOSS_GUARDRAIL", "warn") or "warn").strip().lower()
    return m if m in ("off", "warn", "enforce") else "warn"


def _truncate(text: Any, limit: int = 2000) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + " …[truncated]"


def _safe_log(jarvis, line: str, ghost: bool = False) -> None:
    """Best-effort UI logging. The UI is optional from Moss's point of view."""
    try:
        if ghost and hasattr(jarvis.ui, "write_ghost_log"):
            jarvis.ui.write_ghost_log(line)
        elif hasattr(jarvis.ui, "write_log"):
            jarvis.ui.write_log(line)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# FRIDAY — retrieval-grounded chat
# ══════════════════════════════════════════════════════════════════════════

def _wrap_friday(original):
    @functools.wraps(original)
    def _friday_with_moss(self, text: str, host: str, model: str) -> str:
        moss = get_bridge()
        if not moss.enabled:
            return original(self, text, host, model)

        channel = "friday"
        t0 = time.perf_counter()

        # READ — fold session context in front of whatever RAG context the
        # frontend already folded into `text`. Both survive; neither replaces
        # the other.
        try:
            augmented = moss.augment(text, channel=channel, header="MOSS SESSION CONTEXT")
        except Exception:
            augmented = text

        reply = original(self, augmented, host, model)

        # WRITE — the turn becomes retrievable context for every later turn,
        # in FRIDAY, in GHOST (via cross-channel reads) and in the metrics.
        try:
            moss.remember(text, {"type": "observation", "agent": "user",
                                 "channel": channel, "model": model}, channel=channel)
            moss.remember(_truncate(reply), {"type": "knowledge", "agent": "friday",
                                             "channel": channel, "model": model},
                          channel=channel)
        except Exception:
            pass

        _dbg(f"friday turn +moss in {(time.perf_counter()-t0)*1000:.1f} ms")
        return reply

    setattr(_friday_with_moss, _MARK, True)
    _friday_with_moss.__wrapped_original__ = original
    return _friday_with_moss


# ══════════════════════════════════════════════════════════════════════════
# GHOST — tool-calling agent turn
# ══════════════════════════════════════════════════════════════════════════

def _wrap_ghost_turn(original):
    @functools.wraps(original)
    def _ghost_with_moss(self, text: str, host: str, model: str) -> str:
        moss = get_bridge()
        if not moss.enabled:
            return original(self, text, host, model)

        channel = "ghost"

        # READ — GHOST's own message list is rebuilt from scratch every turn
        # (see _ghost_agent_turn), so it is stateless across turns by design.
        # Moss gives it continuity without touching that loop: the prior
        # session is handed in as context on the user message itself.
        try:
            res = moss.recall_traced(text, channel=channel)
            block = moss.format_block(res.get("results", []), res.get("trace"),
                                      header="MOSS SESSION CONTEXT")
            augmented = f"{block}\n\n{text}" if block else text
            if block:
                _safe_log(self, f"Moss: {len(res.get('results', []))} context item(s) "
                                f"in {res.get('trace', {}).get('total_context_latency_ms', '?')} ms",
                          ghost=True)
        except Exception:
            augmented = text

        reply = original(self, augmented, host, model)

        try:
            moss.remember(text, {"type": "observation", "agent": "user",
                                 "channel": channel, "model": model}, channel=channel)
            moss.remember(_truncate(reply), {"type": "decision", "agent": "ghost",
                                             "channel": channel, "model": model},
                          channel=channel)
        except Exception:
            pass
        return reply

    setattr(_ghost_with_moss, _MARK, True)
    _ghost_with_moss.__wrapped_original__ = original
    return _ghost_with_moss


# ══════════════════════════════════════════════════════════════════════════
# GHOST tool execution — guardrail + audit trail
# ══════════════════════════════════════════════════════════════════════════

def _wrap_ghost_tool(original):
    @functools.wraps(original)
    def _ghost_tool_with_moss(self, name: str, args: dict) -> str:
        moss = get_bridge()
        mode = _mode()
        if not moss.enabled or mode == "off":
            return original(self, name, args)

        channel = "ghost"
        action = f"{name} {json.dumps(args, default=str)[:400]}"

        verdict = {"decision": "ALLOW", "reason": ""}
        try:
            verdict = moss.guardrail(action, policy_query=name, channel=channel)
        except Exception:
            pass

        decision = verdict.get("decision", "ALLOW")
        if decision != "ALLOW":
            _safe_log(self, f"Moss guardrail [{decision}] {name}: {verdict.get('reason','')}",
                      ghost=True)

        if decision == "BLOCK" and mode == "enforce":
            blocked = (f"Blocked by Moss guardrail before execution: "
                       f"{verdict.get('reason', 'no reason given')}")
            try:
                moss.remember(f"Blocked tool '{name}': {verdict.get('reason','')}",
                              {"type": "guardrail", "agent": "moss", "tool": name,
                               "decision": "BLOCK", "confirmed": False}, channel=channel)
            except Exception:
                pass
            return blocked

        result = original(self, name, args)

        # AUDIT — what the agent actually did becomes session state that the
        # next turn (and any other agent on this session) can retrieve.
        try:
            moss.remember(f"Tool '{name}' ran with {json.dumps(args, default=str)[:300]} "
                          f"-> {_truncate(result, 600)}",
                          {"type": "state", "agent": "ghost", "tool": name,
                           "decision": decision, "confirmed": True}, channel=channel)
        except Exception:
            pass
        return result

    setattr(_ghost_tool_with_moss, _MARK, True)
    _ghost_tool_with_moss.__wrapped_original__ = original
    return _ghost_tool_with_moss


# ══════════════════════════════════════════════════════════════════════════
# JARVIS voice tool execution — audit only (async)
# ══════════════════════════════════════════════════════════════════════════

def _wrap_jarvis_tool(original):
    @functools.wraps(original)
    async def _jarvis_tool_with_moss(self, fc):
        moss = get_bridge()
        if not moss.enabled:
            return await original(self, fc)

        result = await original(self, fc)
        try:
            fn_name = getattr(fc, "name", "") or ""
            fn_args = getattr(fc, "args", {}) or {}
            moss.remember(
                f"Voice tool '{fn_name}' called with {json.dumps(dict(fn_args), default=str)[:300]}",
                {"type": "state", "agent": "jarvis", "tool": fn_name, "confirmed": True},
                channel="voice",
            )
        except Exception:
            pass
        return result

    setattr(_jarvis_tool_with_moss, _MARK, True)
    _jarvis_tool_with_moss.__wrapped_original__ = original
    return _jarvis_tool_with_moss


# ══════════════════════════════════════════════════════════════════════════
# _rag_query — Moss recall appended to knowledge-base answers
# ══════════════════════════════════════════════════════════════════════════

def _wrap_rag_query(original):
    @functools.wraps(original)
    def _rag_query_with_moss(question: str, top_k: int = 5) -> str:
        answer = original(question, top_k)
        moss = get_bridge()
        if not moss.enabled or not _env_flag("PRAGON_MOSS_RAG", True):
            return answer
        try:
            hits = moss.recall(question, top_k=min(top_k, 3), channel="friday")
            block = moss.format_block(hits, header="MOSS SESSION CONTEXT")
            return f"{answer}\n\n{block}" if block else answer
        except Exception:
            return answer

    setattr(_rag_query_with_moss, _MARK, True)
    _rag_query_with_moss.__wrapped_original__ = original
    return _rag_query_with_moss


# ══════════════════════════════════════════════════════════════════════════
# install / uninstall
# ══════════════════════════════════════════════════════════════════════════

_TARGETS = [
    ("_friday_processor", _wrap_friday),
    ("_ghost_agent_turn", _wrap_ghost_turn),
    ("_ghost_execute_tool", _wrap_ghost_tool),
    ("_execute_tool", _wrap_jarvis_tool),
]


def install(pragon_main=None, verbose: bool = True) -> dict:
    """Patch PRAGON in place. Import pragon_main yourself and pass it in, or
    leave it to be imported here. Returns a report dict; never raises."""
    global _INSTALLED

    report = {"installed": [], "skipped": [], "errors": [], "already": _INSTALLED}
    if _INSTALLED:
        return report

    try:
        if pragon_main is None:
            import pragon_main  # noqa: WPS433
    except Exception as exc:
        report["errors"].append(f"import pragon_main failed: {exc}")
        return report

    moss = get_bridge(api_key_fn=getattr(pragon_main, "_get_api_key", None))
    if not moss.enabled:
        report["skipped"].append(f"moss bridge dark ({moss.error or 'disabled'})")
        if verbose:
            print(f"[moss] not installed — {moss.error or 'PRAGON_MOSS is off'}")
        return report

    JarvisLive = getattr(pragon_main, "JarvisLive", None)
    if JarvisLive is None:
        report["errors"].append("JarvisLive class not found on pragon_main")
        return report

    for attr, wrapper in _TARGETS:
        try:
            current = getattr(JarvisLive, attr, None)
            if current is None:
                report["skipped"].append(f"{attr} (absent)")
                continue
            if getattr(current, _MARK, False):
                report["skipped"].append(f"{attr} (already wrapped)")
                continue
            setattr(JarvisLive, attr, wrapper(current))
            report["installed"].append(f"JarvisLive.{attr}")
        except Exception as exc:
            report["errors"].append(f"{attr}: {exc}")

    try:
        rq = getattr(pragon_main, "_rag_query", None)
        if rq is not None and not getattr(rq, _MARK, False):
            pragon_main._rag_query = _wrap_rag_query(rq)
            report["installed"].append("pragon_main._rag_query")
    except Exception as exc:
        report["errors"].append(f"_rag_query: {exc}")

    # Make the bridge reachable from anywhere that already has pragon_main.
    try:
        pragon_main.moss_bridge = moss
    except Exception:
        pass

    _INSTALLED = True
    if verbose:
        backend = getattr(moss.engine, "embed_backend_name", "?")
        print(f"[moss] installed → {', '.join(report['installed'])}")
        print(f"[moss] db={moss.db_path} · embeddings={backend} · "
              f"guardrail={_mode()} · top_k={moss.top_k}")
        if report["errors"]:
            print(f"[moss] non-fatal issues: {report['errors']}")
    return report


def uninstall(pragon_main=None) -> None:
    """Restore every original callable. PRAGON is byte-for-byte itself again."""
    global _INSTALLED
    try:
        if pragon_main is None:
            import pragon_main  # noqa: WPS433
        JarvisLive = getattr(pragon_main, "JarvisLive", None)
        for attr, _ in _TARGETS:
            current = getattr(JarvisLive, attr, None)
            orig = getattr(current, "__wrapped_original__", None)
            if orig is not None:
                setattr(JarvisLive, attr, orig)
        rq = getattr(pragon_main, "_rag_query", None)
        orig = getattr(rq, "__wrapped_original__", None)
        if orig is not None:
            pragon_main._rag_query = orig
    except Exception:
        pass
    _INSTALLED = False
