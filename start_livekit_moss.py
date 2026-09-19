"""
start_livekit_moss.py
════════════════════════════════════════════════════════════════════════════
One command to bring up the LiveKit + Moss voice path.

    python start_livekit_moss.py            # preflight, then start the agent
    python start_livekit_moss.py --check    # preflight only
    python start_livekit_moss.py --prod     # 'start' instead of 'dev' mode

Credentials are read from api/api_keys.json (or LIVEKIT_* env vars if set),
so there is nothing to paste into this file — unlike
start_livekit_assistant.bat, which is left untouched.

The preflight runs first on purpose: a bad key, a missing package or an
unreachable server all produce one clear line here instead of a retry loop
inside the worker.
"""

from __future__ import annotations

import os
import subprocess
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

ROOT = Path(__file__).resolve().parent

# Check if current interpreter has livekit; if not, check spidy conda environment
def _resolve_python() -> str:
    try:
        import livekit  # noqa: F401
        return sys.executable
    except ImportError:
        candidate = Path(r"C:\Users\skart\anaconda3\envs\spidy\python.exe")
        if candidate.exists():
            return str(candidate)
        return sys.executable


def main() -> int:
    check_only = "--check" in sys.argv
    mode = "start" if "--prod" in sys.argv else "dev"
    py_exec = _resolve_python()

    print("Running preflight…\n")
    rc = subprocess.call([py_exec, "-m", "pragon_moss.livekit_check"], cwd=str(ROOT))
    if rc != 0:
        print("\nPreflight found problems — fix them above, then re-run.")
        print("(If only the gemini_api_key warning appeared, the token endpoint "
              "still works but the voice agent won't.)")
        return rc
    if check_only:
        return 0

    print(f"\nStarting PRAGON voice agent in '{mode}' mode…")
    print("Leave this running, then open the PRAGON UI and click the LiveKit "
          "pill in the input bar.\n")
    return subprocess.call(
        [py_exec, "livekit_agent_moss.py", mode], cwd=str(ROOT)
    )


if __name__ == "__main__":
    raise SystemExit(main())

