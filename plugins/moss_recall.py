"""
plugins/moss_recall.py — retrieve session context out of MOSS, with the real
measured retrieval latency attached.
"""

PLUGIN = {
    "name": "moss_recall",
    "description": (
        "Search MOSS, PRAGON's realtime context engine, for what was said, "
        "observed, decided or done earlier in this session — across FRIDAY, GHOST "
        "and voice. Use for 'what did we establish about X', 'what has been "
        "confirmed so far', 'what did GHOST just do'. This is session working "
        "memory, not the document knowledge base: use rag/knowledge tools for "
        "ingested files, and moss_recall for the live session."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {"type": "STRING", "description": "What to look for, in natural language."},
            "channel": {
                "type": "STRING",
                "description": "Session channel to search: main, friday, ghost or voice. Defaults to voice.",
            },
            "top_k": {"type": "STRING", "description": "How many items to return. Defaults to 5."},
        },
        "required": ["query"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    query = (parameters.get("query") or "").strip()
    if not query:
        return "Sir, moss_recall needs something to search for."

    channel = (parameters.get("channel") or "voice").strip().lower()
    try:
        top_k = max(1, min(20, int(str(parameters.get("top_k", "5")).strip() or 5)))
    except Exception:
        top_k = 5

    try:
        from pragon_moss import get_bridge

        moss = get_bridge()
        if not moss.enabled:
            return f"Moss isn't running, Sir ({moss.error or 'disabled'}), so I have no session context to search."

        res = moss.recall_traced(query, top_k=top_k, channel=channel)
        hits = res.get("results", []) or []
    except Exception as e:
        return f"Sir, moss_recall failed: {e}"

    if not hits:
        return f"Nothing in Moss matches that on the {channel} channel yet, Sir."

    ms = (res.get("trace") or {}).get("total_context_latency_ms", "?")
    lines = [f"{len(hits)} item(s) from Moss ({channel}, {ms} ms):"]
    for h in hits:
        lines.append(f"- ({h.get('priority')}, score {h.get('score')}) {h.get('content')}")
    out = "\n".join(lines)

    if player:
        try:
            player.write_log(f"PRAGON: Moss recall '{query}' -> {len(hits)} hit(s) in {ms} ms")
        except Exception:
            pass
    return out
