"""
pragon_moss/livekit_hooks.py
════════════════════════════════════════════════════════════════════════════
Real Moss retrieval for the LiveKit voice path.

The stock livekit_agent.py ships a placeholder in its before_llm_cb:

    # moss_context = moss.search(last_msg)
    moss_context = "Placeholder: MOSS retrieved context."

…so nothing was ever retrieved there. This module supplies the real thing,
in a form that works with both generations of the livekit-agents API:

    livekit-agents 0.x   VoicePipelineAgent(before_llm_cb=make_before_llm_cb())
    livekit-agents 1.x   Agent.on_user_turn_completed -> inject_into_turn()

Both funnel into the same bridge the rest of PRAGON uses, on a dedicated
"livekit" channel, so context written by a voice turn is retrievable by
FRIDAY and GHOST and vice versa.

Latency matters more here than anywhere else in PRAGON — this sits between
the user finishing a sentence and the LLM starting to answer. Two guards:

  * the retrieval budget is separately tunable and defaults tighter than the
    global one (PRAGON_MOSS_LK_TIMEOUT, default 0.35s),
  * a miss, a timeout or a dead engine returns the prompt untouched. Voice
    never blocks on Moss.

This file does not import livekit. It is duck-typed against whatever chat
context object it is handed, so it stays importable (and testable) on a
machine with no livekit installed at all.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .bridge import get_bridge, _dbg, _env_float

CHANNEL = "livekit"


def _budget() -> float:
    return _env_float("PRAGON_MOSS_LK_TIMEOUT", 0.35)


def _top_k() -> int:
    try:
        return int(os.environ.get("PRAGON_MOSS_LK_TOPK", "4"))
    except Exception:
        return 4


# ══════════════════════════════════════════════════════════════════════════
# Core: text in → text out
# ══════════════════════════════════════════════════════════════════════════

def augment_text(user_text: str) -> str:
    """Return the user's utterance with Moss context prefixed, or unchanged.

    This is the whole integration in one function; everything below is glue
    for the two different livekit-agents APIs."""
    text = (user_text or "").strip()
    if not text:
        return user_text

    moss = get_bridge()
    if not moss.enabled:
        return user_text

    # Tighten the per-call budget for the voice path only, then restore it.
    original_timeout = moss.timeout
    moss.timeout = min(original_timeout, _budget())
    try:
        res = moss.recall_traced(text, top_k=_top_k(), channel=CHANNEL)
        hits = res.get("results", []) or []
        if not hits:
            return user_text
        block = moss.format_block(hits, res.get("trace"), header="MOSS SESSION CONTEXT")
        ms = (res.get("trace") or {}).get("total_context_latency_ms", "?")
        _dbg(f"livekit: {len(hits)} context item(s) in {ms} ms")
        return f"{block}\n\n{text}"
    except Exception as exc:
        _dbg(f"livekit augment failed, passing through: {exc}")
        return user_text
    finally:
        moss.timeout = original_timeout


def remember_user_turn(text: str) -> None:
    get_bridge().remember(
        text, {"type": "observation", "agent": "user", "channel": CHANNEL}, channel=CHANNEL
    )


def remember_agent_turn(text: str) -> None:
    get_bridge().remember(
        text, {"type": "knowledge", "agent": "pragon-voice", "channel": CHANNEL},
        channel=CHANNEL,
    )


# ══════════════════════════════════════════════════════════════════════════
# livekit-agents 0.x — VoicePipelineAgent(before_llm_cb=...)
# ══════════════════════════════════════════════════════════════════════════

def make_before_llm_cb(chain: Optional[Any] = None):
    """Build a before_llm_cb that injects Moss context into the last user
    message. Pass `chain` to run your own callback afterwards."""

    async def _before_llm_cb(assistant, chat_ctx):
        try:
            msgs = getattr(chat_ctx, "messages", None) or []
            if msgs:
                last = msgs[-1]
                original = _read_message_text(last)
                if original:
                    _write_message_text(last, augment_text(original))
                    remember_user_turn(original)
        except Exception as exc:
            _dbg(f"before_llm_cb: {exc}")  # voice continues regardless
        if chain is not None:
            return await chain(assistant, chat_ctx)

    return _before_llm_cb


# ══════════════════════════════════════════════════════════════════════════
# livekit-agents 1.x — Agent.on_user_turn_completed(turn_ctx, new_message)
# ══════════════════════════════════════════════════════════════════════════

def inject_into_turn(new_message) -> None:
    """Mutate the incoming ChatMessage in place with Moss context."""
    try:
        original = _read_message_text(new_message)
        if not original:
            return
        _write_message_text(new_message, augment_text(original))
        remember_user_turn(original)
    except Exception as exc:
        _dbg(f"on_user_turn_completed: {exc}")


# ══════════════════════════════════════════════════════════════════════════
# Message shape adapters
# ══════════════════════════════════════════════════════════════════════════
# ChatMessage.content has been a str, a list of parts, and `.text_content` has
# come and gone across livekit-agents releases. These two helpers read and
# write whichever shape is actually in front of us rather than betting on one.

def _read_message_text(msg) -> str:
    for attr in ("text_content", "text"):
        val = getattr(msg, attr, None)
        if isinstance(val, str) and val.strip():
            return val
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = [c for c in content if isinstance(c, str)]
        if parts:
            return "\n".join(parts)
    return ""


def _write_message_text(msg, new_text: str) -> None:
    content = getattr(msg, "content", None)
    if isinstance(content, (list, tuple)):
        # Replace the first string part, leave images/audio parts alone.
        updated = list(content)
        for i, part in enumerate(updated):
            if isinstance(part, str):
                updated[i] = new_text
                break
        else:
            updated.append(new_text)
        try:
            msg.content = updated
            return
        except Exception:
            pass
    for attr in ("content", "text"):
        if hasattr(msg, attr):
            try:
                setattr(msg, attr, new_text)
                return
            except Exception:
                continue
