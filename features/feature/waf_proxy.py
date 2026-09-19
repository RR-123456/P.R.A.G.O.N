#waf_proxy.py
"""
Web Application Firewall (WAF) reverse proxy for Pragon.

Sits in front of a local HTTP service (by default Pragon's own PhoneView
dashboard, see pragon_phoneview/server.py) and inspects every request
before it's allowed through:

  - signature matching for common attack classes: SQL injection, XSS,
    path traversal, command injection, template/JNDI injection
  - per-IP rate limiting
  - oversized-request rejection
  - structured logging of blocked requests (source IP, rule matched, path)

It's a reverse proxy, not a packet filter: browsers/clients talk to the
WAF's port, the WAF forwards clean requests to the real upstream service
and streams the response back. Built on the standard library only
(http.server + urllib) so it doesn't add any new dependency to the
project.

This is meant to protect Pragon's own local web surface (PhoneView, any
webhook receiver you add later) from malformed/malicious HTTP requests
hitting it over the LAN — it does not proxy general internet browsing.
"""

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote

DEFAULT_LISTEN_PORT = 9090   # NOTE: Pragon's own UI HTTP server uses 8080 — do not reuse that port here
MAX_BODY_BYTES = 2 * 1024 * 1024   # 2 MB
RATE_LIMIT_WINDOW = 10.0           # seconds
RATE_LIMIT_MAX_REQ = 100           # requests per IP per window

# (rule_name, compiled_regex) — checked against path, query string, and body
_SIGNATURES = [
    ("sql_injection", re.compile(
        r"(\bunion\b.{0,40}\bselect\b|\bselect\b.{0,40}\bfrom\b|\bor\b\s+1\s*=\s*1|"
        r"\bdrop\s+table\b|\bxp_cmdshell\b|--\s*$|;\s*--|'\s*or\s*'1'\s*=\s*'1)",
        re.IGNORECASE)),
    ("xss", re.compile(
        r"(<script\b|onerror\s*=|onload\s*=|javascript:|<img\b[^>]+src\s*=\s*['\"]?javascript:)",
        re.IGNORECASE)),
    ("path_traversal", re.compile(r"(\.\./|\.\.\\|%2e%2e%2f|%2e%2e/)", re.IGNORECASE)),
    ("command_injection", re.compile(
        r"(;\s*(rm|cat|wget|curl|nc|bash|sh)\s|\|\s*(rm|cat|wget|curl|nc|bash|sh)\s|`.*`|\$\(.*\))",
        re.IGNORECASE)),
    ("template_jndi_injection", re.compile(r"(\$\{jndi:|\{\{.*\}\}|<%.*%>)", re.IGNORECASE)),
]

_lock = threading.Lock()
_server = None
_thread = None
_upstream_host = "127.0.0.1"
_upstream_port = None
_listen_port = DEFAULT_LISTEN_PORT

_request_history: dict = defaultdict(lambda: deque())
_blocked_ips: set = set()
_log: deque = deque(maxlen=500)
_stats = {"allowed": 0, "blocked": 0}


def _record(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    _log.append(line)
    print(f"[WAF] {msg}")


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hist = _request_history[ip]
    hist.append(now)
    while hist and now - hist[0] > RATE_LIMIT_WINDOW:
        hist.popleft()
    return len(hist) > RATE_LIMIT_MAX_REQ


def _match_signatures(text: str):
    for name, pattern in _SIGNATURES:
        if pattern.search(text):
            return name
    return None


class _WAFHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # we do our own structured logging

    def _client_ip(self) -> str:
        return self.client_address[0]

    def _block(self, reason: str, status: int = 403):
        with _lock:
            _stats["blocked"] += 1
        _record(f"BLOCKED {self._client_ip()} {self.command} {self.path} -> {reason}")
        body = json.dumps({"error": "blocked_by_waf", "reason": reason}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _inspect_and_forward(self):
        ip = self._client_ip()

        if ip in _blocked_ips:
            return self._block("source ip blocked", 403)

        if _rate_limited(ip):
            return self._block("rate limit exceeded", 429)

        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY_BYTES:
            return self._block("request body too large", 413)

        body = self.rfile.read(length) if length else b""

        decoded_path = unquote(self.path)
        inspect_text = decoded_path
        try:
            inspect_text += " " + body.decode("utf-8", errors="ignore")
        except Exception:
            pass

        hit = _match_signatures(inspect_text)
        if hit:
            return self._block(f"signature match: {hit}", 403)

        if _upstream_port is None:
            return self._block("no upstream configured", 502)

        # forward to upstream
        url = f"http://{_upstream_host}:{_upstream_port}{self.path}"
        req = urllib.request.Request(url, data=body or None, method=self.command)
        for h, v in self.headers.items():
            if h.lower() in ("host", "content-length", "connection"):
                continue
            req.add_header(h, v)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                with _lock:
                    _stats["allowed"] += 1
                self.send_response(resp.status)
                for h, v in resp.getheaders():
                    if h.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(h, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read() if e.fp else b"")
        except Exception as e:
            self._block(f"upstream unreachable: {e}", 502)

    def do_GET(self):
        self._inspect_and_forward()

    def do_POST(self):
        self._inspect_and_forward()

    def do_PUT(self):
        self._inspect_and_forward()

    def do_DELETE(self):
        self._inspect_and_forward()

    def do_PATCH(self):
        self._inspect_and_forward()


# ---------------------------------------------------------------------------
# Public controls
# ---------------------------------------------------------------------------

def waf_start(upstream_port: int, listen_port: int = DEFAULT_LISTEN_PORT, upstream_host: str = "127.0.0.1") -> str:
    global _server, _thread, _upstream_host, _upstream_port, _listen_port
    if _server is not None:
        return "WAF proxy is already running."

    listen_port = int(listen_port)
    if listen_port == 8080:
        return (
            "Port 8080 is reserved for Pragon's own UI server — pick a different "
            "listen_port for the WAF proxy (default is 9090)."
        )

    _upstream_host = upstream_host
    _upstream_port = int(upstream_port)
    _listen_port = listen_port

    try:
        _server = ThreadingHTTPServer(("0.0.0.0", _listen_port), _WAFHandler)
    except OSError as e:
        _server = None
        return f"Could not bind WAF proxy to port {_listen_port}: {e}"

    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()
    _record(f"WAF proxy started on :{_listen_port} -> forwarding to {_upstream_host}:{_upstream_port}")
    return f"WAF proxy listening on port {_listen_port}, protecting {_upstream_host}:{_upstream_port}."


def waf_stop() -> str:
    global _server, _thread
    if _server is None:
        return "WAF proxy is not running."
    _server.shutdown()
    _server.server_close()
    _server = None
    _thread = None
    _record("WAF proxy stopped.")
    return "WAF proxy stopped."


def waf_status() -> str:
    if _server is None:
        return "WAF: stopped."
    with _lock:
        allowed, blocked = _stats["allowed"], _stats["blocked"]
    return (
        f"WAF: running on port {_listen_port}, forwarding to {_upstream_host}:{_upstream_port}.\n"
        f"Requests allowed: {allowed} | blocked: {blocked}\n"
        f"Manually blocked IPs: {sorted(_blocked_ips) or 'none'}"
    )


def waf_recent_log(n: int = 20) -> str:
    with _lock:
        entries = list(_log)[-n:]
    return "\n".join(entries) if entries else "No WAF events logged yet."


def waf_block_ip(ip: str) -> str:
    _blocked_ips.add(ip)
    return f"{ip} added to WAF block-list."


def waf_unblock_ip(ip: str) -> str:
    _blocked_ips.discard(ip)
    return f"{ip} removed from WAF block-list."


def waf_add_signature(name: str, pattern: str) -> str:
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Invalid regex: {e}"
    _SIGNATURES.append((name, compiled))
    return f"Added signature '{name}'."


# ---------------------------------------------------------------------------
# Dispatcher (same calling convention as computer_settings / firewall_control)
# ---------------------------------------------------------------------------

def waf_proxy(
    parameters: dict = None,
    response=None,
    player=None,
) -> str:
    """
    parameters:
        action: "start" | "stop" | "status" | "log" | "block_ip" | "unblock_ip"
        value:  meaning depends on action:
                 start      -> "upstream_port[:listen_port]" e.g. "8000:9090"
                 log        -> number of lines
                 block_ip / unblock_ip -> IP address
    """
    params = parameters or {}
    action = str(params.get("action", "")).strip().lower()
    value = params.get("value")

    if player:
        player.write_log(f"[WAF] {action}")

    if action == "start":
        upstream_port = params.get("upstream_port")
        listen_port = params.get("listen_port", DEFAULT_LISTEN_PORT)
        if not upstream_port and value:
            parts = str(value).split(":")
            upstream_port = parts[0]
            if len(parts) > 1:
                listen_port = parts[1]
        if not upstream_port:
            return "No upstream_port specified (e.g. value='8000:9090' to protect PhoneView on 8000)."
        return waf_start(int(upstream_port), int(listen_port))
    if action == "stop":
        return waf_stop()
    if action == "status":
        return waf_status()
    if action == "log":
        n = int(value) if value else 20
        return waf_recent_log(n)
    if action == "block_ip":
        if not value:
            return "No IP specified."
        return waf_block_ip(str(value))
    if action == "unblock_ip":
        if not value:
            return "No IP specified."
        return waf_unblock_ip(str(value))

    return f"Unknown WAF action: '{action}'."
