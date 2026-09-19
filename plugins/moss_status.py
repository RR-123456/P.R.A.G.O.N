"""
plugins/moss_status.py — report MOSS health: measured latency percentiles,
cache behaviour, embedding backend and item counts.
"""

PLUGIN = {
    "name": "moss_status",
    "description": (
        "Report the health of MOSS, PRAGON's realtime context engine: whether "
        "it's running, which embedding backend it resolved to, how many items "
        "are in the current session, and the real measured p50/p95/p99 "
        "retrieval latency. Use for 'how is Moss doing', 'how fast is context "
        "retrieval', 'is the context engine up'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "channel": {
                "type": "STRING",
                "description": "Channel to count items for: main, friday, ghost or voice. Defaults to voice.",
            },
        },
        "required": [],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    channel = (parameters.get("channel") or "voice").strip().lower()
    try:
        from pragon_moss import get_bridge

        moss = get_bridge()
        if not moss.enabled:
            return f"Moss is offline, Sir: {moss.error or 'disabled via PRAGON_MOSS'}."

        st = moss.status()
        engine = st.get("engine", {}) or {}
        lat = engine.get("latency", {}) or {}
        cache = engine.get("cache", {}) or {}
        items = len(moss.session(channel, limit=1000))
    except Exception as e:
        return f"Sir, moss_status failed: {e}"

    if lat.get("count"):
        lat_line = (f"latency p50 {lat.get('p50_ms')} ms, p95 {lat.get('p95_ms')} ms, "
                    f"p99 {lat.get('p99_ms')} ms over {lat.get('count')} retrievals")
    else:
        lat_line = "no retrievals measured yet"

    msg = (
        f"Moss is running on {engine.get('embed_backend', '?')} embeddings. "
        f"{items} item(s) on the {channel} channel; {lat_line}. "
        f"Cache: {cache.get('hits', 0)} hits, {cache.get('misses', 0)} misses. "
        f"{st.get('writes', 0)} writes and {st.get('reads', 0)} reads this run, "
        f"{st.get('failures', 0)} failure(s)."
    )
    if player:
        try:
            player.write_log(f"PRAGON: {msg}")
        except Exception:
            pass
    return msg
