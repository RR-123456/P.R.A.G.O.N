"""
plugins/moss_remember.py — write something into MOSS session context.

Discovered automatically by pragoncore/plugin_loader.py at startup. No edits
to pragon_main.py, no entry in GHOST_TOOLS, nothing else to change.
"""

PLUGIN = {
    "name": "moss_remember",
    "description": (
        "Store a fact, observation, decision or safety rule in MOSS, PRAGON's "
        "realtime context engine, so later turns and other agents (FRIDAY, GHOST) "
        "can retrieve it. Use for things like 'remember for this session that the "
        "server is on maintenance until Friday' or 'note as a safety rule that the "
        "power must be isolated first'. Use save_memory instead for permanent "
        "personal facts about the user; moss_remember is for working session "
        "context that retrieval should rank by relevance and recency."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "content": {"type": "STRING", "description": "The text to remember, in full."},
            "kind": {
                "type": "STRING",
                "description": (
                    "One of: safety, policy, guardrail (treated as CRITICAL), "
                    "state, observation, decision, task (IMPORTANT), "
                    "knowledge, fact (RELEVANT). Defaults to observation."
                ),
            },
            "channel": {
                "type": "STRING",
                "description": "Session channel: main, friday, ghost or voice. Defaults to voice.",
            },
            "confirmed": {
                "type": "STRING",
                "description": (
                    "'true' if this safety/policy step has already been carried out. "
                    "Unconfirmed safety items make the Moss guardrail warn before "
                    "related actions run."
                ),
            },
        },
        "required": ["content"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    content = (parameters.get("content") or "").strip()
    if not content:
        return "Sir, there was nothing to store — moss_remember needs some content."

    kind = (parameters.get("kind") or "observation").strip().lower()
    channel = (parameters.get("channel") or "voice").strip().lower()
    confirmed = str(parameters.get("confirmed", "")).strip().lower() in ("true", "yes", "1")

    try:
        from pragon_moss import get_bridge

        moss = get_bridge()
        if not moss.enabled:
            return f"Moss is not running right now, Sir ({moss.error or 'disabled'}), so I couldn't store that."

        item_id = moss.remember(
            content,
            {"type": kind, "agent": "jarvis", "confirmed": confirmed},
            channel=channel,
        )
        if not item_id:
            return "Moss accepted nothing back, Sir — the write didn't land."
    except Exception as e:
        return f"Sir, moss_remember failed: {e}"

    msg = f"Stored in Moss as {kind} context on the {channel} channel."
    if player:
        try:
            player.write_log(f"PRAGON: {msg} [{item_id[:8]}]")
        except Exception:
            pass
    return msg
