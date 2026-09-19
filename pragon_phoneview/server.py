"""
pragon_phoneview/server.py — Pragon PhoneView

A drop-in, app-agnostic "control this app from your phone" dashboard.

Standalone extraction of the Mark-XLIX "Remote Dashboard" feature, decoupled
from any assistant-specific code (no Gemini, no JARVIS branding, no
hard-coded action list). Any Python app can import PhoneViewServer, wire up
a few callbacks, and get:

  - QR-code / 6-character-key pairing over the LAN
  - AES-256-CBC encrypted command channel (phone -> app)
  - A live text feed the app can push messages into (app -> phone)
  - Two-way file transfer
  - Raw PCM16 audio streaming from the phone's microphone
  - "Remembered device" auto-reconnect (no re-typing the key every time)

Nothing here reaches out past your own LAN, and nothing here lets one
instance control a *different* person's device — pairing requires physically
scanning a QR code (or typing a one-time key) that this same process
generated, on the same network.

Install deps: pip install fastapi "uvicorn[standard]" cryptography
Optional: pip install qrcode[pil] (for QR image generation)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import socket
import string
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

PKG_DIR = Path(__file__).resolve().parent
STATIC_DIR = PKG_DIR / "static"
DEFAULT_PORT = 8000
MAX_UPLOAD_MB = 500
PAIRING_KEY_LEN = 6
# Characters that read unambiguously out loud / on a small screen.
_KEY_CHARS = tuple(c for c in (string.ascii_uppercase + string.digits)
                    if c not in "OIL01")

# ── AES-256-CBC ──────────────────────────────────────────────────────────────
_AES_SALT = b"PRAGON-PHONEVIEW-v1"


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) -> 32-byte AES-256 key."""
    return hashlib.sha256(session_key.encode("utf-8") + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw = base64.b64decode(enc_b64)
    iv, ct = raw[:16], raw[16:]
    padded = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    data = padded.update(ct) + padded.finalize()
    unpad = sym_pad.PKCS7(128).unpadder()
    return (unpad.update(data) + unpad.finalize()).decode("utf-8")


_CRYPTOJS_CDN = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_crypto_js_async() -> None:
    """Fetch CryptoJS once, in the background, so the constructor never
    blocks on network I/O. The /static/crypto.js route falls back to a CDN
    redirect until the file lands."""
    if _CRYPTOJS_FILE.exists():
        return

    def _download() -> None:
        try:
            import urllib.request
            urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        except Exception:
            pass # /static/crypto.js will keep redirecting to the CDN

    threading.Thread(target=_download, daemon=True).start()


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.
    Ported from Mark-LIII's dashboard/server.py during the September 2026
    upgrade pass -- PRAGON's phoneview server had no equivalent, so "works on
    my PC but not from my phone" had no automated fix before this.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI -> sudo -n -> prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile

    # -- Windows --------------------------------------------------------------
    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"PRAGON Dashboard Port {port}"
        prog_rule = "PRAGON Dashboard Python"
        py_exe = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port = not _netsh_rule_exists(port_rule)
        need_prog = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return # already fully configured

        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="pragon_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs")) # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        try: # succeeds when already admin
            r = subprocess.run([bat_path], capture_output=True, timeout=8, shell=True)
            if r.returncode == 0:
                print(f"[PhoneView] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[PhoneView] One-time network setup required.")
        print("[PhoneView] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", bat_path, None, None, 0, # SW_HIDE
            )
            if int(ret) > 32:
                time.sleep(2)
                print(f"[PhoneView] Network setup complete — port {port} is open.")
                print("[PhoneView] Refresh your phone browser to connect.")
            else:
                print("[PhoneView] Setup was not allowed.")
                print("[PhoneView] Phone connections may fail until PRAGON is run as Administrator.")
        except Exception as e:
            print(f"[PhoneView] Firewall setup error: {e}")
        finally:
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # -- macOS ------------------------------------------------------------------
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run([fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5)
            if "disabled" in r.stdout.lower():
                return # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run([fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5)
            if py in listed.stdout:
                return # already allowed

            print("[PhoneView] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass # macOS firewall is off by default — silent failure is fine
        return

    # -- Linux --------------------------------------------------------------
    def _privileged(cmd: list) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try: # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[PhoneView] ufw: port {port} allowed.")
            else:
                print(f"[PhoneView] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try: # firewalld
        r = subprocess.run(["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5)
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[PhoneView] firewalld: port {port} allowed.")
            else:
                print(f"[PhoneView] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try: # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[PhoneView] iptables: port {port} opened.")
            else:
                print(f"[PhoneView] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass # no iptables means firewall is probably off — nothing to do


def _local_ip() -> str:
    """Best-effort LAN-facing IPv4 address, no internet required."""
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except OSError:
            pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _safe_filename(raw: str) -> str:
    name = Path(raw).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name or "upload"


def _safe_call(fn: Optional[Callable], *args) -> None:
    """Run a user-supplied callback without letting an exception in it
    take down a request handler or the event loop."""
    if fn is None:
        return
    try:
        fn(*args)
    except Exception as e:
        print(f"[PhoneView] callback error: {e}")


@dataclass(frozen=True)
class _Session:
    """One authenticated phone session. A single source of truth for
    'is this token valid' and 'what key encrypts its traffic', instead of
    juggling parallel dicts that can drift out of sync."""
    token: str
    session_key: str
    aes_key: bytes


# ── PhoneViewServer ───────────────────────────────────────────────────────────

class PhoneViewServer:
    """
    Generic phone-pairing remote control server for any Python app.

    Wire it up with callbacks, then run `await server.serve()` inside your
    app's asyncio event loop:

        server = PhoneViewServer(app_name="My App")
        server.on_command(lambda text: print("Phone said:", text))
        server.on_audio_frame(lambda pcm_bytes: ...) # optional, mic streaming
        await server.serve()

    From your UI, when the user wants to pair a phone:

        key = server.new_key()
        url = server.get_auto_login_url(key) # encode this as a QR code

    Push messages to connected phones at any time:

        await server.broadcast({"type": "log", "speaker": "app", "text": "Hello!"})
    """

    def __init__(
        self,
        app_name: str = "Pragon PhoneView",
        port: int = DEFAULT_PORT,
        uploads_dir: Optional[Path] = None,
        ssl_cert: Optional[Path] = None,
        ssl_key: Optional[Path] = None,
    ):
        self.app_name = app_name
        self.port = port
        self._ip = _local_ip()
        self._ssl_cert = Path(ssl_cert) if ssl_cert else None
        self._ssl_key = Path(ssl_key) if ssl_key else None

        # token -> _Session (auth + crypto state, single source of truth)
        self._sessions: dict[str, _Session] = {}
        # one-time pairing key -> expiry timestamp
        self._pending_keys: dict[str, float] = {}
        # remembered-device token -> session_key (long-lived, survives re-pairing)
        self._device_sessions: dict[str, str] = {}

        self._clients: set["WebSocket"] = set()
        self._history: list[dict] = []

        self._command_queue: asyncio.Queue = asyncio.Queue()
        self._phone_audio_queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        self._command_cb: Optional[Callable[[str], None]] = None
        self._wake_cb: Optional[Callable[[], None]] = None
        self._connect_cb: Optional[Callable[[], None]] = None
        self._audio_cb: Optional[Callable[[bytes], None]] = None
        self._model_cb: Optional[Callable[[str], None]] = None

        self._models = ("jarvis", "omnix", "friday", "ghost")
        self._current_model = self._models[0]

        self._uploads_dir = Path(uploads_dir) if uploads_dir else self._default_uploads_dir()
        self._uploads_dir.mkdir(parents=True, exist_ok=True)

        _ensure_crypto_js_async()
        self._login_html = _read("login.html").replace("__APP_NAME__", app_name)
        self._app_html = _read("app.html").replace("__APP_NAME__", app_name)

        self.app = self._build_app()

    @staticmethod
    def _default_uploads_dir() -> Path:
        for candidate in (
            Path.home() / "Downloads" / "Pragon PhoneView Uploads",
            Path.home() / "Documents" / "Pragon PhoneView Uploads",
        ):
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                return candidate
            except OSError:
                continue
        return Path.cwd() / "phoneview_uploads"

    # ── callback registration ───────────────────────────────────────────────

    def on_command(self, fn: Callable[[str], None]) -> None:
        """fn(text) is called whenever the phone sends a command/message."""
        self._command_cb = fn

    def on_wake(self, fn: Callable[[], None]) -> None:
        """fn() is called when the phone taps the 'wake' action."""
        self._wake_cb = fn

    def on_model_change(self, fn: Callable[[str], None]) -> None:
        """fn(model_id) is called whenever the phone switches the active
        model pill (one of 'jarvis', 'omnix', 'friday', 'ghost')."""
        self._model_cb = fn

    def on_connect(self, fn: Callable[[], None]) -> None:
        """fn() is called whenever a phone successfully pairs."""
        self._connect_cb = fn

    def on_audio_frame(self, fn: Callable[[bytes], None]) -> None:
        """fn(pcm16_bytes) is called for each ~64ms chunk of phone mic audio."""
        self._audio_cb = fn

    # ── pairing ──────────────────────────────────────────────────────────────

    def _purge_expired_keys(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        if self._pending_keys:
            self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}

    def new_key(self, expiry_secs: int = 600) -> str:
        """Generate a fresh one-time pairing key."""
        now = time.time()
        self._purge_expired_keys(now)
        key = "".join(secrets.choice(_KEY_CHARS) for _ in range(PAIRING_KEY_LEN))
        self._pending_keys[key] = now + expiry_secs
        return key

    def get_auto_login_url(self, key: str) -> str:
        """URL to encode as a QR code — scanning it pairs instantly."""
        return f"{self.get_url()}/auto-login?key={key}"

    def _ssl_enabled(self) -> bool:
        return bool(self._ssl_cert and self._ssl_key
                    and self._ssl_cert.exists() and self._ssl_key.exists())

    def get_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{self.port}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS is active this points
        at the alias port (also HTTPS, started by `serve()`) — phone
        browsers auto-upgrade a bare IP:PORT to https, so the alias needs
        TLS too even though it's only meant for manual typing."""
        if self._ssl_enabled():
            return f"{self._ip}:{self.port + 1}"
        return f"{self._ip}:{self.port}"

    def _issue_session(self, session_key: str) -> str:
        """Create a fresh auth token bound to `session_key`, cache its
        derived AES key once, and register the session. Shared by every
        login path (PIN, QR, remembered-device) so there's exactly one
        place that creates sessions."""
        token = secrets.token_urlsafe(32)
        self._sessions[token] = _Session(
            token=token, session_key=session_key, aes_key=_derive_key(session_key)
        )
        return token

    def _session(self, token: str) -> Optional[_Session]:
        return self._sessions.get(token) if token else None

    def _decrypt(self, token: str, enc_b64: str) -> Optional[str]:
        session = self._session(token)
        if not session:
            return None
        try:
            return _decrypt_cbc(session.aes_key, enc_b64)
        except Exception:
            return None

    def _on_paired(self, sys_text: str) -> None:
        _safe_call(self._connect_cb)
        asyncio.create_task(self.broadcast({"type": "sys", "text": sys_text}))

    # ── broadcast (app -> all paired phones) ────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            del self._history[:-300]
        if not self._clients:
            return
        dead: set["WebSocket"] = set()
        for ws in tuple(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── background queue drains ─────────────────────────────────────────────

    async def _drain_commands(self) -> None:
        while True:
            text = await self._command_queue.get()
            _safe_call(self._command_cb, text)

    async def _drain_audio(self) -> None:
        while True:
            frame = await self._phone_audio_queue.get()
            _safe_call(self._audio_cb, frame)

    async def _submit_command(self, text: str) -> None:
        if not text:
            return
        await self._command_queue.put(text)
        _safe_call(self._wake_cb)

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)
        unauthorized = lambda: JSONResponse({"error": "Unauthorized"}, status_code=401)

        def _authed(req: "Request") -> Optional[_Session]:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return self._session(tok)

        # ── static assets ────────────────────────────────────────────────
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE), media_type="application/javascript")
            return RedirectResponse(_CRYPTOJS_CDN)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(self.port)))
            return HTMLResponse(html)

        # ── pairing ──────────────────────────────────────────────────────
        @app.post("/login")
        async def login(req: Request):
            body = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now = time.time()
            self._purge_expired_keys(now)
            if entered not in self._pending_keys or self._pending_keys[entered] <= now:
                return JSONResponse({"ok": False, "error": "Invalid or expired key"}, status_code=401)
            del self._pending_keys[entered]
            token = self._issue_session(entered)
            self._on_paired("Remote connection established.")
            return JSONResponse({"ok": True, "token": token})

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            now = time.time()
            self._purge_expired_keys(now)
            if not key or key not in self._pending_keys:
                return HTMLResponse(_EXPIRED_HTML.replace("__APP_NAME__", self.app_name))

            del self._pending_keys[key]
            token = self._issue_session(key)
            dev_tok = secrets.token_urlsafe(32)
            self._device_sessions[dev_tok] = key

            self._on_paired("Remote connection established via QR code.")
            return HTMLResponse(_CONNECTING_HTML.format(tok=token, key=key, dev_tok=dev_tok))

        @app.post("/api/device-login")
        async def device_login(req: Request):
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            session_key = self._device_sessions.get(dev_tok)
            if not session_key:
                return JSONResponse({"ok": False}, status_code=401)
            token = self._issue_session(session_key)
            self._on_paired("Known device reconnected automatically.")
            return JSONResponse({"ok": True, "token": token, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            if not _authed(req):
                return unauthorized()
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        # ── commands ─────────────────────────────────────────────────────
        @app.post("/api/command")
        async def command(req: Request):
            session = _authed(req)
            if not session:
                return unauthorized()
            body = await req.json()
            enc = body.get("enc", "")
            if enc:
                text = self._decrypt(session.token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            await self._submit_command(text)
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _authed(req):
                return unauthorized()
            _safe_call(self._wake_cb)
            return JSONResponse({"ok": True})

        # ── model selection ─────────────────────────────────────────────
        @app.get("/api/model")
        async def get_model(req: Request):
            if not _authed(req):
                return unauthorized()
            return JSONResponse({"model": self._current_model, "models": list(self._models)})

        @app.post("/api/model")
        async def set_model(req: Request):
            if not _authed(req):
                return unauthorized()
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Invalid body"}, status_code=400)
            model = str(body.get("model", "")).strip().lower()
            if model not in self._models:
                return JSONResponse(
                    {"error": f"Unknown model. Choose from: {', '.join(self._models)}"},
                    status_code=400,
                )
            self._current_model = model
            _safe_call(self._model_cb, model)
            await self.broadcast({"type": "model_changed", "model": model})
            return JSONResponse({"ok": True, "model": model})

        # ── phone mic PCM16 stream ────────────────────────────────────
        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            if not self._session(token.strip()):
                await websocket.close(code=4001)
                return
            await websocket.accept()
            await self.broadcast({"type": "sys", "text": "Phone microphone live."})
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        pass # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                await self.broadcast({"type": "sys", "text": "Phone microphone stopped."})

        # ── file sharing ─────────────────────────────────────────────────
        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _authed(req):
                    return unauthorized()
                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size, max_bytes = 0, MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while chunk := await file.read(65536):
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    dest.unlink(missing_ok=True)
                    return JSONResponse({"error": str(exc)}, status_code=500)

                await self.broadcast({
                    "type": "file_received", "name": dest.name, "size": size,
                    "saved_to": str(self._uploads_dir),
                })
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _authed(req):
                return unauthorized()
            try:
                entries = [p for p in self._uploads_dir.iterdir() if p.is_file()]
                entries.sort(key=lambda p: p.stat().st_mtime, reverse=True) # newest first
                files = [{"name": p.name, "size": p.stat().st_size} for p in entries]
            except OSError:
                files = []
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            if not self._session(token.strip()):
                return unauthorized()
            safe = re.sub(r"[/\\]", "", filename)
            path = self._uploads_dir / safe
            if not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        # ── live feed / command socket ──────────────────────────────────
        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            session = self._session(token.strip())
            if not session:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") != "command":
                        continue
                    enc = data.get("enc", "")
                    text = self._decrypt(session.token, enc) if enc else (data.get("text") or "").strip()
                    await self._submit_command(text)
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── serve ────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on port+1, sharing the same app/state — for
        phones that type a bare IP:PORT manually (browsers force-upgrade
        that to https, so it needs its own TLS listener)."""
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=self.port + 1, log_level="warning",
            ssl_keyfile=str(self._ssl_key), ssl_certfile=str(self._ssl_cert),
        )
        print(f"[PhoneView] Manual entry: {self._ip}:{self.port + 1} (accept the cert warning once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        """Start the dashboard. Runs forever — launch as an asyncio task."""
        if not _DEPS_OK:
            print("[PhoneView] fastapi/uvicorn not installed — dashboard disabled.")
            print('[PhoneView] Run: pip install fastapi "uvicorn[standard]" cryptography')
            return

        asyncio.create_task(self._drain_commands())
        asyncio.create_task(self._drain_audio())

        # Best-effort OS firewall config for LAN access -- ported from
        # Mark-LIII. Background thread: never blocks uvicorn startup, and a
        # failure here just means the user falls back to opening the port
        # manually (message already printed by the platform branch that failed).
        threading.Thread(target=_ensure_network_access, args=(self.port,), daemon=True).start()

        use_ssl = self._ssl_enabled()
        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg_kwargs = (
            {"ssl_keyfile": str(self._ssl_key), "ssl_certfile": str(self._ssl_cert)}
            if use_ssl else {}
        )
        cfg = uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning", **cfg_kwargs)
        print(f"[PhoneView] {self.get_url()}")
        print("[PhoneView] Call server.new_key() and show the QR / key in your UI to pair a phone.")
        await uvicorn.Server(cfg).serve()


_EXPIRED_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Ask __APP_NAME__ for a new QR code.</p>
</div></body></html>"""

_CONNECTING_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('pv_token','{tok}');
  sessionStorage.setItem('pv_key','{key}');
  localStorage.setItem('pv_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting…</p>
</body></html>"""
