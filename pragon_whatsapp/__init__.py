"""
PRAGON WhatsUp — WhatsApp bot feature for P.R.A.G.O.N.

Spawns pragon_whatsapp/bot.js (whatsapp-web.js) as a subprocess, reads its
newline-delimited JSON stdout on a background thread, and exposes simple
Python callbacks (on_qr / on_ready / on_message / on_disconnected / on_error)
that pragon_main.py wires into the UI's websocket broadcast and into FRIDAY.

Usage (mirrors the existing PhoneView wiring pattern in pragon_main.py):

    from pragon_whatsapp import WhatsUpBridge

    bridge = WhatsUpBridge(
        on_qr=lambda data_uri: ui._broadcast({"type": "whatsapp_pairing", "ok": True, "qr": data_uri}),
        on_ready=lambda: ui._broadcast({"type": "whatsapp_status", "connected": True}),
        on_disconnected=lambda reason: ui._broadcast({"type": "whatsapp_status", "connected": False, "reason": reason}),
        on_message=lambda frm, text: handle_incoming(frm, text),
    )
    bridge.start()   # call from the "whatsapp_connect" ws handler
    ...
    bridge.send(frm, reply_text)
    ...
    bridge.stop()
"""

import json
import os
import shutil
import subprocess
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_node() -> str | None:
    """Locate node.exe/node reliably regardless of how P.R.A.G.O.N was
    launched.

    shutil.which("node") only searches the PATH the *current process*
    inherited at startup. That's fine from a terminal (cmd/PowerShell
    re-reads PATH fresh), but a desktop shortcut / double-clicked .pyw
    is spawned by explorer.exe, which caches its environment at
    login — so if Node was installed or PATH updated afterwards,
    Explorer-launched processes (including the shortcut) never see it,
    and shutil.which silently returns None even though Node works fine
    in a terminal. This is why 'it only works if I run it from cmd'.

    We work around that by (1) trying the normal PATH lookup, then
    (2) re-reading PATH fresh from the Windows registry instead of the
    stale inherited copy, then (3) checking common install locations
    directly.
    """
    found = shutil.which("node")
    if found:
        return found

    if sys.platform == "win32":
        # Re-read PATH straight from the registry (both machine- and
        # user-level) since the inherited os.environ["PATH"] may be stale.
        try:
            import winreg
            fresh_paths = []
            for hive, subkey in (
                (winreg.HKEY_CURRENT_USER, r"Environment"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ):
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        value, _ = winreg.QueryValueEx(key, "Path")
                        fresh_paths.extend(value.split(os.pathsep))
                except OSError:
                    continue
            found = shutil.which("node", path=os.pathsep.join(fresh_paths))
            if found:
                return found
        except Exception:
            pass

        # Fall back to the well-known install locations directly.
        candidates = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "nodejs", "node.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         "nodejs", "node.exe"),
            os.path.join(os.environ.get("APPDATA", ""), "npm", "node.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                         "node", "node.exe"),
            # nvm-windows: active version lives in a symlinked nvm\ dir
            os.path.join(os.environ.get("NVM_SYMLINK", ""), "node.exe"),
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c

    return None


class WhatsUpBridge:
    def __init__(self, on_qr=None, on_ready=None, on_message=None,
                 on_disconnected=None, on_error=None):
        self.on_qr = on_qr
        self.on_ready = on_ready
        self.on_message = on_message
        self.on_disconnected = on_disconnected
        self.on_error = on_error

        self._proc = None
        self._reader_thread = None
        self._lock = threading.Lock()
        self.connected = False

    # ── lifecycle ─────────────────────────────────────────────────────
    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self):
        """Start the Node sidecar if it isn't already running. Returns
        (ok: bool, error: str|None)."""
        with self._lock:
            if self.is_running():
                return True, None

            node_path = _find_node()
            if not node_path:
                msg = ("Node.js not found. Install it from "
                       "https://nodejs.org, then restart P.R.A.G.O.N. "
                       "(If it works from a terminal but not from the "
                       "desktop shortcut, log out and back in once so "
                       "Explorer picks up the updated PATH.)")
                if self.on_error:
                    self.on_error(msg)
                return False, msg

            if not os.path.isdir(os.path.join(_HERE, "node_modules")):
                msg = ("WhatsUp dependencies not installed. Run "
                       "'npm install' inside the pragon_whatsapp folder once, "
                       "then try connecting again.")
                if self.on_error:
                    self.on_error(msg)
                return False, msg

            try:
                self._proc = subprocess.Popen(
                    [node_path, "bot.js"],
                    cwd=_HERE,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except Exception as e:
                msg = f"Failed to start WhatsUp bridge: {e}"
                if self.on_error:
                    self.on_error(msg)
                return False, msg

            self._reader_thread = threading.Thread(
                target=self._read_loop, daemon=True
            )
            self._reader_thread.start()
            return True, None

    def stop(self):
        with self._lock:
            if self._proc:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                self._proc = None
            self.connected = False

    # ── outgoing ──────────────────────────────────────────────────────
    def send(self, to: str, text: str):
        """Send a WhatsApp message. `to` is the chat id as received via
        on_message (e.g. '91xxxxxxxxxx@c.us')."""
        if not self.is_running():
            return False
        try:
            line = json.dumps({"type": "send", "to": to, "text": text})
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
            return True
        except Exception as e:
            if self.on_error:
                self.on_error(f"Send failed: {e}")
            return False

    # ── internals ─────────────────────────────────────────────────────
    def _read_loop(self):
        proc = self._proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._dispatch(event)
        except Exception as e:
            if self.on_error:
                self.on_error(f"WhatsUp bridge reader crashed: {e}")
        finally:
            self.connected = False
            # Drain stderr for diagnostics if the process died unexpectedly
            if proc and proc.poll() is not None and proc.returncode != 0:
                try:
                    err_output = proc.stderr.read()
                    if err_output and self.on_error:
                        self.on_error(f"WhatsUp process exited: {err_output[:500]}")
                except Exception:
                    pass

    def _dispatch(self, event: dict):
        etype = event.get("type")
        print(f"[WhatsUp bridge] event: {event}", flush=True)
        if etype == "qr" and self.on_qr:
            self.on_qr(event.get("dataUri"))
        elif etype == "ready":
            self.connected = True
            if self.on_ready:
                self.on_ready()
        elif etype == "message" and self.on_message:
            self.on_message(event.get("from"), event.get("text"))
        elif etype == "disconnected":
            self.connected = False
            if self.on_disconnected:
                self.on_disconnected(event.get("reason"))
        elif etype == "error" and self.on_error:
            self.on_error(event.get("message"))
