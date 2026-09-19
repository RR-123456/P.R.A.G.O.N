"""
customforge_launcher.py
------------------------
Starts P.R.A.G.O.N C.U.S.T.O.M FORGE (customforge/main.py) as a background
subprocess, once, on demand. FORGE is a standalone Flask app (its own UI,
drawing pad, and Gemini-backed generation pipeline) that ordinarily runs
via `python main.py` in the customforge/ folder. This module makes it
start automatically the first time it's needed from inside P.R.A.G.O.N's
own UI (the CustomDraw overlay / iframe) or its voice trigger, so nothing
has to be started by hand.

Safe to call ensure_forge_running() any number of times -- it's a no-op if
FORGE is already up.
"""
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

FORGE_DIR = Path(__file__).resolve().parent / "customforge"
FORGE_ENTRY = FORGE_DIR / "main.py"
FORGE_HOST = "127.0.0.1"
FORGE_PORT = 5000
FORGE_URL = f"http://{FORGE_HOST}:{FORGE_PORT}"

_lock = threading.Lock()
_proc = None


def _is_up(timeout=0.6) -> bool:
    try:
        urllib.request.urlopen(FORGE_URL, timeout=timeout)
        return True
    except Exception:
        return False


def ensure_forge_running(wait_seconds: float = 8.0) -> bool:
    """Starts FORGE if it isn't already reachable at FORGE_URL. Blocks up
    to wait_seconds for it to come up on first launch. Returns True if
    FORGE is reachable by the time this returns, False otherwise."""
    global _proc
    if _is_up():
        return True

    with _lock:
        if _is_up():
            return True
        if _proc is None or _proc.poll() is not None:
            if not FORGE_ENTRY.exists():
                return False
            env = dict(os.environ)
            env["FORGE_NO_AUTOOPEN"] = "1"  # the iframe/overlay is the UI; don't also pop a tab
            _proc = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=str(FORGE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _is_up():
            return True
        time.sleep(0.3)
    return _is_up()
