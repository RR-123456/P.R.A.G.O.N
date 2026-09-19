"""
Macro Recorder - record & replay mouse/keyboard actions.

This is the core engine. It has no UI of its own — server.py wraps it
in a small HTTP API that index.html talks to.

Quick script usage still works:

    import macro_recorder as mr
    events = mr.record_until_key() # press ESC to stop
    mr.save(events, "login.json")
    mr.load_and_play("login.json")
"""

import json
import sys
import time
import threading

# ─── Windows DPI awareness ──────────────────────────────────────────────────
# Must happen BEFORE pynput/pyautogui touch the display. Without this,
# Windows silently rescales coordinates on any monitor with display scaling
# != 100%, so a click recorded at (800, 450) can land somewhere else
# entirely on playback. Marking the process per-monitor-DPI-aware makes the
# coordinates pynput records and the coordinates pyautogui clicks agree.
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2) # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from pynput import mouse, keyboard
import pyautogui

# pyautogui adds a 0.1s pause after every single call (move/click/press) by
# default, which compounds across a macro and makes replayed timing drift
# away from what was recorded. We drive all timing ourselves, so turn its
# built-in delays off. FAILSAFE stays on: slam the mouse into a screen
# corner during playback to abort instantly.
pyautogui.PAUSE = 0
pyautogui.MINIMUM_DURATION = 0
pyautogui.MINIMUM_SLEEP = 0
pyautogui.FAILSAFE = True


def _precise_sleep(duration, abort_flag=None):
    """
    time.sleep() on Windows only guarantees ~15ms resolution, which is
    enough to visibly smear out fast click sequences. Sleep in short
    slices (checking abort_flag between them, so Stop actually stops
    promptly even mid-way through a multi-second gap), then spin on a
    monotonic clock for the last couple milliseconds to land the final
    wake-up much closer to exact.
    """
    if duration <= 0:
        return
    target = time.perf_counter() + duration
    while True:
        remaining = target - time.perf_counter()
        if remaining <= 0:
            return
        if abort_flag is not None and abort_flag.is_set():
            return
        if remaining > 0.02:
            time.sleep(0.02)
        elif remaining > 0.003:
            time.sleep(remaining - 0.002)
        else:
            while time.perf_counter() < target:
                if abort_flag is not None and abort_flag.is_set():
                    return


# ─── Recorder ───────────────────────────────────────────────────────────────

class MacroRecorder:
    """
    Encapsulates recording state in an instance instead of module
    globals, so start()/stop() can be called safely from a web
    request handler (a different thread each time) without races.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._recording = False
        self._events = []
        self._start_time = None
        self._mouse_listener = None
        self._keyboard_listener = None

    def is_recording(self):
        return self._recording

    def start(self):
        with self._lock:
            if self._recording:
                return
            self._events = []
            self._start_time = None
            self._recording = True

        self._mouse_listener = mouse.Listener(on_click=self._on_click)
        self._keyboard_listener = keyboard.Listener(on_press=self._on_press)
        self._mouse_listener.start()
        self._keyboard_listener.start()

    def stop(self):
        """
        Stop recording (if active) and return the captured events.
        Draining self._events here (rather than just reading it) means a
        second stop() call — e.g. the web UI catching up after an
        ESC-triggered stop — returns [] instead of silently replaying the
        same events again, and start() can never wipe out an unclaimed
        recording underneath a caller who hasn't fetched it yet.
        """
        with self._lock:
            was_recording = self._recording
            self._recording = False
            events, self._events = self._events, []

        if was_recording:
            if self._mouse_listener:
                self._mouse_listener.stop()
            if self._keyboard_listener:
                self._keyboard_listener.stop()

        return events

    def _elapsed(self):
        # perf_counter is monotonic and sub-millisecond resolution, unlike
        # time.time() (which can jump if the system clock adjusts and is
        # lower resolution on some platforms).
        if self._start_time is None:
            self._start_time = time.perf_counter()
        return round(time.perf_counter() - self._start_time, 4)

    def _on_click(self, x, y, button, pressed):
        if not self._recording or not pressed:
            return
        self._events.append({
            "type": "click",
            "x": int(round(x)),
            "y": int(round(y)),
            "button": str(button).replace("Button.", ""),
            "time": self._elapsed(),
        })

    def _on_press(self, key):
        if not self._recording:
            return False # tells pynput to stop this listener
        if key == keyboard.Key.esc:
            # ESC is a physical panic-stop, in addition to the API stop.
            # Stop BOTH listeners here — otherwise the mouse-click listener
            # is left running (and leaking) forever after an ESC-stop,
            # since stop() below will see recording already False and skip
            # its own listener-stop calls.
            self._recording = False
            if self._mouse_listener:
                self._mouse_listener.stop()
            return False
        try:
            key_str = key.char
        except AttributeError:
            key_str = str(key).replace("Key.", "")
        self._events.append({
            "type": "key",
            "key": key_str,
            "time": self._elapsed(),
        })


# ─── Playback ───────────────────────────────────────────────────────────────

# Keys pyautogui recognises under a different name than pynput reports.
_SPECIAL_KEYS = {
    "enter": "enter", "return": "enter", "space": "space", "tab": "tab",
    "backspace": "backspace", "delete": "delete", "esc": "esc",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "shift": "shift", "shift_r": "shift",
    "ctrl": "ctrl", "ctrl_r": "ctrl", "ctrl_l": "ctrl",
    "alt": "alt", "alt_r": "alt", "alt_l": "alt",
    "cmd": "command", "cmd_r": "command",
}


def play(events, speed=1.0, abort_flag=None):
    """
    Replay recorded events, moving the real mouse and sending real
    keystrokes on this machine.

    Args:
        events: list of events from record_until_key()/load()
        speed: playback speed multiplier (2.0 = twice as fast)
        abort_flag: optional threading.Event; if set mid-playback,
                     playback stops after the current event.
    """
    if not events:
        return
    if speed <= 0:
        raise ValueError("speed must be > 0")

    prev_time = 0.0
    for event in events:
        if abort_flag is not None and abort_flag.is_set():
            break

        delay = (event["time"] - prev_time) / speed
        _precise_sleep(delay, abort_flag)
        prev_time = event["time"]

        if abort_flag is not None and abort_flag.is_set():
            break

        if event["type"] == "click":
            x, y = int(event["x"]), int(event["y"])
            # Move first, then click at that exact spot with zero tween —
            # click(x, y, duration=...) would otherwise glide the cursor
            # there, which is both slower and, on some setups, less exact
            # than an instant jump.
            pyautogui.moveTo(x, y, duration=0)
            pyautogui.click(button=event["button"])
        elif event["type"] == "key":
            key = event["key"]
            mapped = _SPECIAL_KEYS.get(key.lower(), key)
            pyautogui.press(mapped)


# ─── Save / load ────────────────────────────────────────────────────────────

def save(events, filepath):
    data = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event_count": len(events),
        "events": events,
    }
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def load(filepath):
    with open(filepath, "r") as f:
        data = json.load(f)
    return data["events"]


# ─── Backward-compatible convenience wrappers ──────────────────────────────
# (matches the original module's mr.record_until_key() / mr.record_and_save()
# / mr.load_and_play() API, for anyone using this as a plain script.)

_default_recorder = MacroRecorder()


def record_until_key():
    """Blocking helper: start recording, return once ESC is pressed."""
    _default_recorder.start()
    try:
        while _default_recorder.is_recording():
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    return _default_recorder.stop()


def record_and_save(filepath):
    events = record_until_key()
    save(events, filepath)
    return events


def load_and_play(filepath, speed=1.0):
    events = load(filepath)
    play(events, speed)
    return events


if __name__ == "__main__":
    print("=" * 50)
    print(" A.C.T.I.O.N (core module)")
    print("=" * 50)
    print()
    print("For the point-and-click version, run: python3 server.py")
    print("and open http://127.0.0.1:5005 in your browser.")
    print()
    print("Script usage:")
    print(" import macro_recorder as mr")
    print(" events = mr.record_until_key() # press ESC to stop")
    print(" mr.save(events, 'macro.json')")
    print(" mr.load_and_play('macro.json')")
