"""
livekit_agent_moss.py
════════════════════════════════════════════════════════════════════════════
PRAGON's LiveKit voice worker, with MOSS actually wired in.

The stock livekit_agent.py is left completely untouched — run whichever you
want:

    python livekit_agent.py dev          # original, Moss placeholder string
    python livekit_agent_moss.py dev     # this one, real Moss retrieval

What's different from the original
----------------------------------
1. Moss is real. The original's before_llm_cb assigns the literal string
   "Placeholder: MOSS retrieved context." — nothing was ever retrieved. Here
   every user turn is searched against the live Moss session and the agent's
   reply is written back, so voice shares one context store with FRIDAY,
   GHOST and the JARVIS tool path.

2. It runs on both livekit-agents generations. 1.x removed
   VoicePipelineAgent/before_llm_cb in favour of AgentSession +
   Agent.on_user_turn_completed. This file detects which is installed and
   builds the matching pipeline, so it doesn't break the day you upgrade.

3. Credentials come from api/api_keys.json as well as the environment, so it
   matches what pragon_ui.py's /livekit/token endpoint already reads. No need
   to set the same three values in two places.

4. connect() happens before the session starts, and the greeting only fires
   once a participant is actually in the room — the original greets into an
   empty room after a fixed 1s sleep, which usually means nobody hears it.

Run
---
    python livekit_agent_moss.py dev       # dev mode, hot reload
    python livekit_agent_moss.py start     # production worker

Environment (falls back to api/api_keys.json for all three):
    LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
Moss tuning:
    PRAGON_MOSS=0                disable Moss on the voice path entirely
    PRAGON_MOSS_LK_TIMEOUT=0.35  retrieval budget in seconds
    PRAGON_MOSS_LK_TOPK=4        context items injected per turn
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["PYTHONIOENCODING"] = "utf-8"
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Ensure LiveKit plugins are registered on the main thread
try:
    from livekit.plugins import google, silero  # noqa: F401
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

API_CONFIG_PATH = BASE_DIR / "api" / "api_keys.json"

SYSTEM_PROMPT = (
    "You are PRAGON, a highly capable AI assistant, speaking over a realtime "
    "voice link. Keep responses concise and natural — this is speech, not "
    "prose. When a MOSS SESSION CONTEXT block appears above the user's "
    "question, treat it as established fact from earlier in this session and "
    "answer from it; never read the block itself aloud."
)


# ── credentials ─────────────────────────────────────────────────────────────

def _load_keys() -> dict:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _get_api_key() -> str:
    """Gemini key: env first, then api/api_keys.json (same file pragon_main
    and the UI's /livekit/token endpoint read)."""
    key = os.environ.get("GEMINI_API_KEY") or _load_keys().get("gemini_api_key", "")
    if not key or "PUT-YOUR" in key.upper() or "YOUR-" in key.upper():
        raise RuntimeError(
            "No usable gemini_api_key. Set GEMINI_API_KEY or fill in "
            f"{API_CONFIG_PATH} before starting the voice agent."
        )
    return key


def _export_livekit_env() -> dict:
    """livekit-agents reads LIVEKIT_* from the environment only. Mirror
    api/api_keys.json into it so one config file is enough."""
    cfg = _load_keys()
    mapping = {
        "LIVEKIT_URL": "livekit_url",
        "LIVEKIT_API_KEY": "livekit_api_key",
        "LIVEKIT_API_SECRET": "livekit_api_secret",
    }
    resolved = {}
    for env_name, cfg_name in mapping.items():
        val = os.environ.get(env_name) or cfg.get(cfg_name, "")
        if val:
            os.environ[env_name] = val
        resolved[env_name] = val
    missing = [k for k, v in resolved.items() if not v]
    if missing:
        raise RuntimeError(
            f"LiveKit is not configured — missing {', '.join(missing)}. Add "
            f"livekit_url / livekit_api_key / livekit_api_secret to "
            f"{API_CONFIG_PATH}, or set the LIVEKIT_* environment variables."
        )
    return resolved


# ── Moss ────────────────────────────────────────────────────────────────────

def _moss():
    """Import the hooks lazily so a broken/absent Moss can't stop the voice
    agent from starting."""
    try:
        from pragon_moss import livekit_hooks

        return livekit_hooks
    except Exception as exc:
        print(f"[moss] voice hooks unavailable ({exc}); running without context.")
        return None


# ── livekit-agents API detection ────────────────────────────────────────────

def _agents_api() -> str:
    """'v1' for AgentSession-era livekit-agents, 'v0' for VoicePipelineAgent."""
    try:
        from livekit.agents import AgentSession  # noqa: F401

        return "v1"
    except Exception:
        try:
            from livekit.agents.pipeline import VoicePipelineAgent  # noqa: F401

            return "v0"
        except Exception as exc:
            raise RuntimeError(
                "livekit-agents is not installed (or is a version this file "
                "doesn't recognise). Install it with:\n"
                "    pip install 'livekit-agents' livekit-plugins-google "
                "livekit-plugins-silero\n"
                f"Underlying import error: {exc}"
            ) from exc


def prewarm(proc):
    try:
        from livekit.plugins import silero
        proc.userdata["vad"] = silero.VAD.load()
    except Exception:
        pass


# ── entrypoints ─────────────────────────────────────────────────────────────

async def _entrypoint_v1(ctx):
    from livekit.agents import Agent, AgentSession
    from livekit.plugins import google, silero

    moss = _moss()
    key = _get_api_key()

    class PragonAgent(Agent):
        def __init__(self):
            super().__init__(instructions=SYSTEM_PROMPT)

        async def on_user_turn_completed(self, turn_ctx, new_message):
            # Runs after STT finalises the user's turn, before the LLM sees
            # it — the exact seam the original file left as a placeholder.
            if moss is not None:
                moss.inject_into_turn(new_message)

    # Prewarmed VAD or lazy-load
    vad = None
    if hasattr(ctx, "proc") and ctx.proc.userdata.get("vad"):
        vad = ctx.proc.userdata["vad"]
    else:
        try:
            vad = silero.VAD.load()
        except Exception:
            pass

    # RealtimeModel uses Gemini Multimodal Live API directly with gemini_api_key
    model = google.realtime.RealtimeModel(
        api_key=key,
        instructions=SYSTEM_PROMPT,
        voice="Puck",
    )

    session = AgentSession(
        llm=model,
        vad=vad,
    )

    if moss is not None:
        @session.on("conversation_item_added")
        def _on_item(ev):
            try:
                item = getattr(ev, "item", None)
                if getattr(item, "role", "") == "assistant":
                    text = moss._read_message_text(item)
                    if text:
                        moss.remember_agent_turn(text)
                    _log_moss_state()
            except Exception:
                pass

    await ctx.connect()
    await session.start(agent=PragonAgent(), room=ctx.room)
    print("PRAGON voice agent connected (livekit-agents 1.x Realtime API).")

    # Wait for a real participant before greeting
    for _ in range(100):
        if getattr(ctx.room, "remote_participants", None) or getattr(
            ctx.room, "participants", None
        ):
            break
        await asyncio.sleep(0.1)

    try:
        await session.generate_reply(
            instructions="Greet the user briefly: you are online, LiveKit and Moss are active."
        )
    except Exception as e:
        print(f"[livekit] greeting note: {e}")


async def _entrypoint_v0(ctx):
    from livekit.agents import AutoSubscribe, llm
    from livekit.agents.pipeline import VoicePipelineAgent
    from livekit.plugins import google

    moss = _moss()
    key = _get_api_key()

    initial_ctx = llm.ChatContext().append(role="system", text=SYSTEM_PROMPT)

    agent = VoicePipelineAgent(
        vad=ctx.proc.userdata["vad"],
        stt=google.STT(api_key=key),
        llm=google.LLM(api_key=key, model="gemini-2.5-flash"),
        tts=google.TTS(api_key=key),
        chat_ctx=initial_ctx,
        before_llm_cb=(moss.make_before_llm_cb() if moss is not None else None),
    )

    if moss is not None:
        @agent.on("agent_speech_committed")
        def _on_speech(msg):
            try:
                text = moss._read_message_text(msg)
                if text:
                    moss.remember_agent_turn(text)
                _log_moss_state()
            except Exception:
                pass

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    agent.start(ctx.room)
    print("PRAGON voice agent connected (livekit-agents 0.x API).")

    # Wait for a real listener instead of sleeping and hoping.
    for _ in range(100):
        if getattr(ctx.room, "remote_participants", None) or getattr(
            ctx.room, "participants", None
        ):
            break
        await asyncio.sleep(0.1)

    await agent.say(
        "I am online. LiveKit and Moss are active. How can I help you, sir?",
        allow_interruptions=True,
    )


def _log_moss_state() -> None:
    if os.environ.get("PRAGON_MOSS_DEBUG"):
        try:
            from pragon_moss import get_bridge

            st = get_bridge().status()
            print(f"[moss] writes={st['writes']} reads={st['reads']} "
                  f"failures={st['failures']}")
        except Exception:
            pass


async def entrypoint(ctx):
    if _agents_api() == "v1":
        await _entrypoint_v1(ctx)
    else:
        await _entrypoint_v0(ctx)


def main() -> None:
    cfg = _export_livekit_env()
    print(f"[livekit] url={cfg['LIVEKIT_URL']} key={cfg['LIVEKIT_API_KEY'][:6]}…")

    try:
        from pragon_moss import get_bridge

        moss = get_bridge()
        print(f"[moss] {'enabled' if moss.enabled else 'disabled'} · "
              f"db={moss.db_path} · channel=livekit")
    except Exception as exc:
        print(f"[moss] unavailable: {exc}")

    from livekit.agents import WorkerOptions, cli

    opts = WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm)
    cli.run_app(opts)


if __name__ == "__main__":
    main()
