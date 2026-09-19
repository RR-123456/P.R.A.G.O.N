"""
pragoncore/audio_devices.py — pick which microphone and speakers PRAGON uses.

WHY
    pragon_main.py opened both audio streams with no `device=` argument, so
    they always took whatever the OS calls "default". On a machine with a
    built-in mic, a webcam mic, and a headset, that's a coin toss — and on
    Windows the default can move on its own the moment a headset is plugged
    in. "PRAGON can't hear me" usually means "PRAGON is listening to the
    monitor's microphone".

WHY NAMES, NOT INDICES
    sounddevice identifies devices by integer index, and indices shift
    whenever a device appears or disappears. We store the device *name* and
    resolve it to an index at stream-open time.

WHY THIS IS CACHED
    `sd.query_devices()` talks to the host audio API and can take a few
    hundred milliseconds. Callers should invoke `warm_cache_async()` once at
    startup so the first real lookup is instant.
"""
from __future__ import annotations

import threading
from typing import Optional

DEFAULT_LABEL = "System default"
DEFAULT_VALUE = ""

_cache: Optional[dict] = None
_cache_lock = threading.Lock()

# Aliases for "the default device" and internal OS routing endpoints, ported
# from Mark-LIII. Matched case-insensitively as substrings against the device
# name. These clutter the picker without ever being a real, separate device —
# "System default" (DEFAULT_LABEL) already covers what they're for.
_PSEUDO_DEVICES = (
    "sound mapper",   # Windows MME
    "primary sound",  # Windows DirectSound ("Primary Sound Capture Driver")
    "sysdefault",     # ALSA
    "default",        # ALSA / PulseAudio alias
    "dmix", "dsnoop", # ALSA software mixing plugins
    "surround",       # ALSA channel-layout permutations of one card
    "samplerate", "speexrate", "upmix", "vdownmix", "null",
)


def _is_pseudo(name: str) -> bool:
    low = name.lower()
    return any(tok in low for tok in _PSEUDO_DEVICES)


def _query() -> dict:
    import sounddevice as sd
    devices = sd.query_devices()
    inputs, outputs = [DEFAULT_LABEL], [DEFAULT_LABEL]
    for d in devices:
        name = (d.get("name") or "").strip()
        if not name or _is_pseudo(name):
            continue
        if d.get("max_input_channels", 0) > 0 and name not in inputs:
            inputs.append(name)
        if d.get("max_output_channels", 0) > 0 and name not in outputs:
            outputs.append(name)
    return {"inputs": inputs, "outputs": outputs}


def list_devices(force_refresh: bool = False) -> dict:
    """Cached {'inputs': [names...], 'outputs': [names...]}."""
    global _cache
    with _cache_lock:
        if _cache is None or force_refresh:
            try:
                _cache = _query()
            except Exception as e:
                print(f"[AudioDevices] query failed: {e}")
                _cache = {"inputs": [DEFAULT_LABEL], "outputs": [DEFAULT_LABEL]}
        return _cache


def warm_cache_async() -> None:
    """Kick off the (slow) device query on a background thread — call once at startup."""
    threading.Thread(target=list_devices, daemon=True).start()


# Alias — ported from Mark-LIII, which calls the same startup warm-up prefetch().
# Kept as a thin wrapper (rather than renaming warm_cache_async) so any existing
# PRAGON caller keeps working unchanged.
def prefetch() -> None:
    warm_cache_async()


# The rates PRAGON's streams actually open at (see SEND_SAMPLE_RATE in
# pragon_main.py) already match Mark-LIII's defaults, so there is nothing to
# reconcile today. This exists as a forward-compat hook, ported for naming
# parity: if a variable-rate mode is ever added, wiring it through here
# invalidates the device cache so a stale list built under the old rate isn't
# served to the picker.
_RATES = {"input": 16000, "output": 24000}


def configure(input_rate: int, output_rate: int) -> None:
    """Record the sample rates the audio streams use and drop the cached
    device list, since which devices are usable can depend on the rate."""
    global _cache
    _RATES["input"] = int(input_rate)
    _RATES["output"] = int(output_rate)
    with _cache_lock:
        _cache = None


def resolve_input(name: Optional[str]) -> Optional[int]:
    """Device name -> sounddevice index for input, or None for 'let the OS decide'."""
    return _resolve(name, kind="input")


def resolve_output(name: Optional[str]) -> Optional[int]:
    return _resolve(name, kind="output")


def _resolve(name: Optional[str], kind: str) -> Optional[int]:
    if not name or name == DEFAULT_LABEL:
        return None
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        channel_key = "max_input_channels" if kind == "input" else "max_output_channels"
        for i, d in enumerate(devices):
            if (d.get("name") or "").strip() == name and d.get(channel_key, 0) > 0:
                return i
    except Exception as e:
        print(f"[AudioDevices] resolve('{name}') failed: {e}")
    return None
