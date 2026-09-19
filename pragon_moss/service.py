"""
pragon_moss/service.py
════════════════════════════════════════════════════════════════════════════
Optional HTTP surface for Moss, sitting beside PRAGON's existing services
(ragsystem on :8000, phoneview, customforge). It does not touch, proxy or
replace any of them — it is one more independent port.

    python -m pragon_moss.service            # :8090
    python -m pragon_moss.service --port 9001

Endpoints
---------
    POST /context/write        {session_id, content, metadata}
    POST /context/search       {session_id, query, top_k}
    POST /context/evaluate     {session_id, query, top_k}
    POST /context/guardrail    {session_id, action, policy_query}
    GET  /context/session/{id}
    GET  /metrics                      moss engine latency/cache/backend
    GET  /pragon/status                bridge health as PRAGON sees it
    GET  /pragon/session/{channel}     friday | ghost | voice | main

The first six come straight from moss.create_api(); the PRAGON-specific two
are added on top so the dashboard can show Moss state per channel without
knowing Moss's session-id scheme.

Needs fastapi + uvicorn (already in PRAGON's requirements.txt).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .bridge import get_bridge  # noqa: E402


def build_app():
    from moss import create_api  # noqa: WPS433

    bridge = get_bridge()
    if not bridge.enabled or bridge.engine is None:
        raise RuntimeError(
            f"Moss engine unavailable: {bridge.error or 'PRAGON_MOSS is disabled'}"
        )

    app = create_api(bridge.engine)

    @app.get("/pragon/status")
    async def _status():
        return bridge.status()

    @app.get("/pragon/session/{channel}")
    async def _channel(channel: str, limit: int = 100):
        return {
            "channel": channel,
            "session_id": bridge.session_id(channel),
            "items": bridge.session(channel, limit),
        }

    return app


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Moss context service for PRAGON")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
