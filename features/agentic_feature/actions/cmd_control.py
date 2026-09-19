# features/agentic_feature/actions/cmd_control.py
#
# NEW — there was no existing tool matching planner.py's "cmd_control"
# (open a file/app, or run a short system command, from a natural
# language description). This is a minimal implementation: it asks
# Gemini to turn the task into one safe shell command for the current
# OS, then runs it. Kept intentionally small — for anything heavier
# than "open X" / "run Y", prefer code_helper (action=run/build) or
# file_controller instead.

import json
import re
import subprocess
import sys
import platform
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "api" / "api_keys.json"
DESKTOP         = Path.home() / "Desktop"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _os_name() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    return "linux"


def _command_for_task(task: str, os_name: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=_get_api_key())
    model = genai.GenerativeModel("gemini-2.5-flash-lite")

    prompt = f"""Turn this task into exactly ONE safe shell command to run on {os_name}.
Task: {task}

Rules:
- Assume relative filenames (like "notes.txt") live on the Desktop at: {DESKTOP}
- Output ONLY the raw command, nothing else. No markdown, no backticks, no explanation.
- Prefer opening files with the OS default handler ("start" on windows, "open" on macos, "xdg-open" on linux)
  unless a specific app is named in the task.
- Never include destructive commands (no rm -rf, del /f /s /q, formatting, or similar).

Command:"""

    response = model.generate_content(prompt)
    cmd = response.text.strip()
    cmd = re.sub(r"^```[a-zA-Z]*\n?", "", cmd)
    cmd = re.sub(r"\n?```$", "", cmd)
    return cmd.strip()


_DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b", r"\bdel\s+/f\b", r"\bformat\b", r"\bmkfs\b",
    r"\bshutdown\b", r"\breboot\b", r":\(\)\{.*\};:",
]


def _is_dangerous(cmd: str) -> bool:
    low = cmd.lower()
    return any(re.search(p, low) for p in _DANGEROUS_PATTERNS)


def cmd_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p       = parameters or {}
    task    = (p.get("task") or "").strip()
    visible = bool(p.get("visible", True))

    if not task:
        return "Please describe what to open or run, sir."

    os_name = _os_name()

    try:
        cmd = _command_for_task(task, os_name)
    except Exception as e:
        return f"Could not work out a command for that: {e}"

    if not cmd:
        return "Could not work out a command for that, sir."

    if _is_dangerous(cmd):
        return f"Refusing to run a potentially destructive command: {cmd}"

    if player:
        player.write_log(f"[CmdControl] Running: {cmd}")
    print(f"[CmdControl] ▶️ {cmd}")

    try:
        if os_name == "windows":
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=20
            )
        else:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=20
            )

        output = (result.stdout or "").strip()
        error  = (result.stderr or "").strip()

        if result.returncode == 0:
            msg = f"Done, sir. Ran: {cmd}"
            return f"{msg}\n\n{output}" if output else msg

        raise RuntimeError(error or output or f"Exit code {result.returncode}")

    except subprocess.TimeoutExpired:
        # Long-running/GUI-launching commands (e.g. opening notepad) are
        # expected to still be "running" past a short timeout — treat as success.
        return f"Opened/started, sir: {cmd}"
    except Exception as e:
        raise RuntimeError(f"cmd_control failed running '{cmd}': {e}")
