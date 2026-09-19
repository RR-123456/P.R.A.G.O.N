# main.py
# PRAGON — C.U.S.T.O.M FORGE — entry point
#
# This is what you run. It starts:
#   1. The FORGE bridge server (server.py) on http://127.0.0.1:5000
#   2. Agent Builder (agent_builder/server.py) alongside it on
#      http://127.0.0.1:5057 — a separate, self-contained Flask app for
#      building/running tool workflows on a visual canvas.
# ...then opens the FORGE UI in your browser. Agent Builder is reachable
# directly at :5057, or via the "Agent Builder" button in the FORGE topbar.
#
#   python main.py
#
# (You can also run `python server.py` directly if you don't want the
# auto-open-browser / Agent Builder behavior, e.g. on a headless machine —
# or `python agent_builder/server.py` to run Agent Builder completely on
# its own.)

import os
import sys
import json
import atexit
import threading
import subprocess
import webbrowser
from pathlib import Path

from server import app, OUTPUT_DIR, CONFIG_PATH, get_api_key

BASE_DIR             = Path(__file__).resolve().parent
AGENT_BUILDER_DIR    = BASE_DIR / "agent_builder"
AGENT_BUILDER_SCRIPT = AGENT_BUILDER_DIR / "server.py"
AGENT_BUILDER_CONFIG = AGENT_BUILDER_DIR / "config" / "api_keys.json"

HOST = "127.0.0.1"
PORT = 5000
URL = f"http://{HOST}:{PORT}"

_agent_builder_proc = None


def open_browser():
    webbrowser.open(URL)


def _sync_agent_builder_key():
    """Agent Builder keeps its own config/api_keys.json (it's a fully
    self-contained app), but it wants the same "gemini_api_key" FORGE
    uses. If FORGE already has a key configured and Agent Builder doesn't
    have its own yet, copy it over so the key only has to be entered once.
    Never overwrites a key Agent Builder already has, and never raises —
    Agent Builder's own tools already handle a missing key gracefully."""
    if AGENT_BUILDER_CONFIG.exists() or not CONFIG_PATH.exists():
        return
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            key = json.load(f).get("gemini_api_key", "")
        if not key or "YOUR_" in key.upper():
            return
        AGENT_BUILDER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with open(AGENT_BUILDER_CONFIG, "w", encoding="utf-8") as f:
            json.dump({"gemini_api_key": key}, f, indent=2)
        print(f"  [i] Copied gemini_api_key into {AGENT_BUILDER_CONFIG} for Agent Builder.")
    except Exception:
        pass  # best-effort convenience only — never block startup over this


def start_agent_builder():
    """Runs Agent Builder as its own process on port 5057, alongside
    FORGE. It's a fully separate Flask app (own routes, own static UI,
    own workflow engine) — nothing about FORGE's server is touched by
    this. If it fails to start for any reason (port in use, missing
    dependency), FORGE keeps running normally; only Agent Builder is
    unavailable."""
    global _agent_builder_proc
    if not AGENT_BUILDER_SCRIPT.exists():
        print("  [!] agent_builder/server.py not found — skipping Agent Builder.")
        return None
    env = dict(os.environ)
    env["AGENT_BUILDER_NO_AUTOOPEN"] = "1"  # FORGE's topbar button opens it instead
    try:
        proc = subprocess.Popen(
            [sys.executable, str(AGENT_BUILDER_SCRIPT)],
            cwd=str(AGENT_BUILDER_DIR),
            env=env,
        )
        _agent_builder_proc = proc
        atexit.register(_stop_agent_builder)
        print("  Agent Builder : http://127.0.0.1:5057  (also linked from the FORGE topbar)")
        return proc
    except Exception as e:
        print(f"  [!] Could not start Agent Builder: {e}")
        return None


def _stop_agent_builder():
    if _agent_builder_proc and _agent_builder_proc.poll() is None:
        _agent_builder_proc.terminate()


def main():
    print("=" * 60)
    print("  PRAGON — C.U.S.T.O.M FORGE")
    print(f"  Output folder : {OUTPUT_DIR}")
    print(f"  Config        : {CONFIG_PATH}")
    print(f"  URL           : {URL}")
    print("=" * 60)

    try:
        get_api_key()
    except Exception as e:
        print(f"  [!] {e}")
        print(f"  Paste your Gemini API key into {CONFIG_PATH} before forging.")

    _sync_agent_builder_key()
    start_agent_builder()

    # Give the servers a moment to bind before the browser tries to load them.
    # Skipped when launched embedded (e.g. from P.R.A.G.O.N's UI, which loads
    # FORGE in its own CustomDraw iframe/overlay) to avoid a duplicate tab.
    if not os.environ.get("FORGE_NO_AUTOOPEN"):
        threading.Timer(1.0, open_browser).start()

    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
