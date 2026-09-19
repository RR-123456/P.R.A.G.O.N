"""
actions/macro_action.py
────────────────────────
A.C.T.I.O.N — Automated Click / Type Input Operation Node
JARVIS-facing wrapper around actions/macro_recorder.py.

Lets the assistant record a sequence of real mouse clicks and keystrokes,
save it under a name, and replay it later on command ("record a macro
called login", "play the login macro", "list my macros").

Recording/playback drive the REAL mouse & keyboard on this machine, so:
  * playback is gated behind ConfirmationGate.ask_for_action() in main.py
    before this module is ever called with mode="play"
  * ESC always panic-stops an active recording (handled inside
    macro_recorder.MacroRecorder)
  * a playback in progress can be cancelled with mode="stop_play"

This module never blocks the calling (voice) turn: start/stop/play/
stop_play all return immediately, with the actual recording/playback
running in background threads, exactly like the standalone
macro-bridge Flask server this was adapted from.
"""

import json
import re
import threading
from pathlib import Path

from features.feature import macro_recorder as mr


def _macro_dir() -> Path:
    # Lives under the user's home dir (not the install dir) so it survives
    # app updates/reinstalls and works even if PRAGON is frozen/read-only.
    d = Path.home() / ".jarvis" / "macros"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    name = Path((name or "").strip()).name # collapse to a bare filename
    if not name:
        raise ValueError("A macro name is required.")
    if not name.endswith(".json"):
        name += ".json"
    return name


# ─── Module-level state (mirrors macro-bridge/server.py) ────────────────────

_recorder = mr.MacroRecorder()
_record_lock = threading.Lock()
_play_lock = threading.Lock()
_playback_thread = None
_playback_abort = threading.Event()
_last_play_error = None


def _log(player, text: str) -> None:
    if player and hasattr(player, "write_log"):
        try:
            player.write_log(f"[A.C.T.I.O.N] {text}")
        except Exception:
            pass


# ─── Mode handlers ───────────────────────────────────────────────────────────

def _handle_start(player) -> str:
    with _record_lock:
        if _recorder.is_recording():
            return "Already recording a macro — say stop when you're done, or press ESC."
        _recorder.start()
    _log(player, "Recording started (press ESC to panic-stop).")
    return "Recording started. I'll capture every click and keystroke until you say stop."


def _handle_stop(args: dict, player) -> str:
    events = _recorder.stop()
    if not events:
        return "No macro was being recorded, or nothing was captured."

    name = args.get("name", "").strip()
    if not name:
        return (
            f"Recording stopped — {len(events)} events captured, but I need a name "
            f"to save it. Say 'save that macro as <name>'."
        )

    try:
        fname = _safe_name(name)
        mr.save(events, str(_macro_dir() / fname))
    except ValueError as e:
        return str(e)

    _log(player, f"Saved '{name}' ({len(events)} events).")
    return f"Macro '{name}' saved with {len(events)} events."


def _handle_list() -> str:
    files = sorted(f.stem for f in _macro_dir().glob("*.json"))
    if not files:
        return "No macros saved yet."
    return "Saved macros: " + ", ".join(files) + "."


def _handle_delete(args: dict, player) -> str:
    name = args.get("name", "").strip()
    if not name:
        return "Which macro should I delete?"
    try:
        fname = _safe_name(name)
    except ValueError as e:
        return str(e)
    path = _macro_dir() / fname
    if not path.exists():
        return f"I couldn't find a macro called '{name}'."
    path.unlink()
    _log(player, f"Deleted '{name}'.")
    return f"Macro '{name}' deleted."


def _handle_play(args: dict, player) -> str:
    global _playback_thread, _last_play_error

    name = args.get("name", "").strip()
    if not name:
        return "Which macro should I play?"
    try:
        speed = float(args.get("speed", 1.0) or 1.0)
    except (TypeError, ValueError):
        return "Speed must be a number."
    if speed <= 0:
        return "Speed must be greater than 0."

    try:
        fname = _safe_name(name)
        events = mr.load(str(_macro_dir() / fname))
    except FileNotFoundError:
        return f"I couldn't find a macro called '{name}'."
    except (ValueError, json.JSONDecodeError, KeyError):
        return f"The macro '{name}' file is corrupted or unreadable."

    with _play_lock:
        if _playback_thread is not None and _playback_thread.is_alive():
            return "A macro is already playing — say 'stop the macro' first."
        if _recorder.is_recording():
            return "Stop the current recording before playing a macro."

        _playback_abort.clear()
        _last_play_error = None

        def _run():
            global _last_play_error
            try:
                mr.play(events, speed=speed, abort_flag=_playback_abort)
            except Exception as e:
                _last_play_error = str(e)

        _playback_thread = threading.Thread(target=_run, daemon=True)
        _playback_thread.start()

    _log(player, f"Playing '{name}' at {speed}x ({len(events)} events).")
    return (
        f"Playing macro '{name}' now at {speed}x speed — {len(events)} steps. "
        f"Don't touch the mouse or keyboard until it finishes."
    )


def _handle_stop_play(player) -> str:
    if _playback_thread is None or not _playback_thread.is_alive():
        return "No macro is currently playing."
    _playback_abort.set()
    _log(player, "Playback stopped.")
    return "Macro playback stopped."


def _handle_status() -> str:
    playing = _playback_thread is not None and _playback_thread.is_alive()
    if _recorder.is_recording():
        return "Currently recording a macro."
    if playing:
        return "Currently playing a macro."
    if _last_play_error:
        return f"Idle. Last playback error: {_last_play_error}"
    return "Idle — not recording or playing."


# ─── Direct UI helpers (Settings > Action panel) ─────────────────────────────
# These bypass the mode-dispatch below entirely — the Record/ESC/rename/delete/
# play buttons in the web UI call them straight from main.py, with no LLM in
# the loop (a button click or an ESC press is already the user's confirmation).

_ACTION_NAME_RE = re.compile(r"^ACTION \((\d+)\)$", re.IGNORECASE)


def list_macro_names() -> list:
    """Sorted list of saved macro names, for populating the Settings UI."""
    return sorted(f.stem for f in _macro_dir().glob("*.json"))


def generate_next_action_name() -> str:
    """Next auto-generated name in the 'ACTION (n)' series."""
    highest = 0
    for name in list_macro_names():
        m = _ACTION_NAME_RE.match(name)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"ACTION ({highest + 1})"


def ui_start_recording(player=None) -> str:
    return _handle_start(player)


def ui_stop_and_save(player=None):
    """
    Stop the active recording and save it under an auto-generated
    'ACTION (n)' name (the UI then lets the user rename it inline).
    Returns (message, saved_name_or_None).
    """
    events = _recorder.stop()
    if not events:
        return "No macro was being recorded, or nothing was captured.", None

    name = generate_next_action_name()
    try:
        fname = _safe_name(name)
        mr.save(events, str(_macro_dir() / fname))
    except ValueError as e:
        return str(e), None

    _log(player, f"Saved '{name}' ({len(events)} events).")
    return f"Recording stopped — saved as '{name}' ({len(events)} events).", name


def ui_rename(old: str, new: str, player=None) -> str:
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new:
        return "Both the current and new name are required."
    try:
        old_fname, new_fname = _safe_name(old), _safe_name(new)
    except ValueError as e:
        return str(e)
    old_path, new_path = _macro_dir() / old_fname, _macro_dir() / new_fname
    if not old_path.exists():
        return f"I couldn't find a macro called '{old}'."
    if new_path.exists() and new_path != old_path:
        return f"A macro named '{new}' already exists."
    old_path.rename(new_path)
    _log(player, f"Renamed '{old}' to '{new}'.")
    return f"Renamed '{old}' to '{new}'."


def ui_delete(name: str, player=None) -> str:
    return _handle_delete({"name": name}, player)


def ui_play(name: str, player=None) -> str:
    """Play a macro triggered directly from the Settings UI (no confirmation
    gate — clicking Play is itself the user's explicit confirmation)."""
    return _handle_play({"name": name}, player)


# ─── Entry point (same shape as every other actions/*.py module) ────────────

def macro_action(parameters: dict, player=None, speak=None) -> str:
    """
    parameters:
        mode : "start" | "stop" | "play" | "stop_play" | "list" | "delete" | "status"
        name : macro name (required for stop/play/delete)
        speed : playback speed multiplier, default 1.0 (play only)
    """
    args = parameters or {}
    mode = (args.get("mode") or "").strip().lower()

    try:
        if mode == "start":
            return _handle_start(player)
        if mode == "stop":
            return _handle_stop(args, player)
        if mode == "play":
            return _handle_play(args, player)
        if mode == "stop_play":
            return _handle_stop_play(player)
        if mode == "list":
            return _handle_list()
        if mode == "delete":
            return _handle_delete(args, player)
        if mode == "status":
            return _handle_status()
        return (
            "Unknown macro mode. Use one of: start, stop, play, stop_play, "
            "list, delete, status."
        )
    except Exception as e:
        return f"Macro action failed: {e}"
