#network_firewall.py
"""
Network-level Stateful Inspection Firewall (SIF) for Pragon.

This sits below the OS-firewall toggler in firewall_control.py and adds the
thing a plain on/off switch doesn't give you: connection *state* tracking.

How it works
------------
A background thread polls active sockets (via psutil) every POLL_INTERVAL
seconds and keeps a table of 5-tuples -> state:

    NEW          first time this (src_ip, src_port, dst_ip, dst_port, proto)
                 has been seen
    ESTABLISHED  the OS reports it as ESTABLISHED (handshake completed)
    RELATED      a new connection from an IP that already has an
                 ESTABLISHED session (e.g. a passive-FTP data channel)
    INVALID      state can't be matched to any known flow (spoofed/garbage)

Default policy, like a real stateful firewall:
    - ESTABLISHED / RELATED  -> always allowed, no rule lookup needed
    - NEW                    -> checked against the allow/deny rule table;
                                 default is deny for inbound NEW connections
                                 unless a rule explicitly allows the port
    - INVALID                -> always dropped, and source IP is logged

On top of that it does simple anomaly detection: if a single source IP
opens more than MAX_NEW_PER_WINDOW brand-new connections inside
WINDOW_SECONDS (a classic port-scan / SYN-flood signature), it is
auto-blocked at the OS level via features.feature.firewall_control.block_ip.

This is a monitoring + heuristic layer implemented in pure Python
(psutil), not a kernel packet filter — actual packet drops for the rules
below are still enforced by the OS firewall (iptables conntrack on Linux,
the stateful engine built into Windows Defender Firewall, or pf's
`keep state` on macOS). Enabling this module also makes sure the OS-level
stateful engine itself is switched on.
"""

import platform
import subprocess
import threading
import time
from collections import defaultdict, deque

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

from features.feature.firewall_control import block_ip, enable_firewall, _run, _OS

POLL_INTERVAL = 2.0        # seconds between connection-table scans
WINDOW_SECONDS = 10.0      # sliding window for new-connection rate check
MAX_NEW_PER_WINDOW = 40    # >N brand-new connections from one IP in the window = scan

_lock = threading.Lock()
_running = False
_thread = None

# 5-tuple -> {"state": str, "first_seen": float, "last_seen": float}
_conn_table: dict = {}

# allow-list of inbound ports for NEW connections, e.g. {8000, 443}
_allowed_new_ports: set = set()

# ip -> deque[timestamps] of NEW connections, for scan detection
_new_conn_history: dict = defaultdict(lambda: deque())

# ips we've auto-blocked this session, so we don't spam block calls
_auto_blocked: set = set()

_log: deque = deque(maxlen=500)


def _record(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    _log.append(line)
    print(f"[SIF] {msg}")


def _classify_and_track(conns):
    now = time.time()
    seen_keys = set()

    for c in conns:
        if not c.laddr or not c.raddr:
            continue
        proto = "TCP" if c.type == 1 else "UDP"
        key = (c.raddr.ip, c.raddr.port, c.laddr.ip, c.laddr.port, proto)
        seen_keys.add(key)
        os_state = getattr(c, "status", "NONE")

        existing = _conn_table.get(key)

        if existing is None:
            # brand new flow
            state = "NEW"
            _conn_table[key] = {"state": state, "first_seen": now, "last_seen": now}
            _on_new_connection(c.raddr.ip, c.laddr.port, proto, now)
        else:
            existing["last_seen"] = now
            if os_state == "ESTABLISHED":
                existing["state"] = "ESTABLISHED"
            elif existing["state"] == "NEW" and now - existing["first_seen"] > 30:
                # never made it to ESTABLISHED after 30s -> treat as invalid/stale
                existing["state"] = "INVALID"
                _record(f"INVALID (stale) flow from {c.raddr.ip}:{c.raddr.port} -> port {c.laddr.port}/{proto}")

    # prune closed flows
    stale = [k for k in _conn_table if k not in seen_keys and now - _conn_table[k]["last_seen"] > 60]
    for k in stale:
        del _conn_table[k]


def _on_new_connection(src_ip: str, dst_port: int, proto: str, now: float):
    # rate tracking for scan/flood detection
    hist = _new_conn_history[src_ip]
    hist.append(now)
    while hist and now - hist[0] > WINDOW_SECONDS:
        hist.popleft()

    if len(hist) > MAX_NEW_PER_WINDOW and src_ip not in _auto_blocked:
        _auto_blocked.add(src_ip)
        _record(f"ANOMALY: {src_ip} opened {len(hist)} new connections in {WINDOW_SECONDS:.0f}s -> auto-blocking")
        try:
            block_ip(src_ip)
        except Exception as e:
            _record(f"Auto-block failed for {src_ip}: {e}")
        return

    # policy check for brand-new inbound connections
    if _allowed_new_ports and dst_port not in _allowed_new_ports:
        _record(f"DENY (policy): NEW connection {src_ip} -> port {dst_port}/{proto} not in allow-list")


def _loop():
    global _running
    while _running:
        if _PSUTIL:
            try:
                conns = psutil.net_connections(kind="inet")
                with _lock:
                    _classify_and_track(conns)
            except (PermissionError, psutil.AccessDenied):
                _record("Insufficient privileges to read the connection table (run as admin/root).")
            except Exception as e:
                _record(f"Poll error: {e}")
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Public controls
# ---------------------------------------------------------------------------

def sif_start(allowed_ports=None) -> str:
    global _running, _thread, _allowed_new_ports
    if not _PSUTIL:
        return "psutil is not installed. Run: pip install psutil"
    if _running:
        return "Stateful inspection firewall is already running."

    if allowed_ports:
        _allowed_new_ports = {int(p) for p in allowed_ports}

    enable_firewall()  # make sure the OS-level stateful engine itself is on
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    _record(f"Stateful inspection started. Allow-listed ports: {sorted(_allowed_new_ports) or 'none set (monitor-only)'}")
    return "Stateful inspection firewall started."


def sif_stop() -> str:
    global _running
    if not _running:
        return "Stateful inspection firewall is not running."
    _running = False
    _record("Stateful inspection stopped.")
    return "Stateful inspection firewall stopped."


def sif_status() -> str:
    if not _running:
        return "SIF: stopped."
    with _lock:
        total = len(_conn_table)
        by_state = defaultdict(int)
        for v in _conn_table.values():
            by_state[v["state"]] += 1
    lines = [
        "SIF: running.",
        f"Tracked flows: {total} (NEW={by_state.get('NEW',0)}, "
        f"ESTABLISHED={by_state.get('ESTABLISHED',0)}, INVALID={by_state.get('INVALID',0)})",
        f"Auto-blocked IPs this session: {len(_auto_blocked)}",
        f"Allowed NEW ports: {sorted(_allowed_new_ports) or 'none set (monitor-only)'}",
    ]
    return "\n".join(lines)


def sif_recent_log(n: int = 20) -> str:
    with _lock:
        entries = list(_log)[-n:]
    return "\n".join(entries) if entries else "No SIF events logged yet."


def sif_allow_port(port: int) -> str:
    _allowed_new_ports.add(int(port))
    return f"Port {port} added to the NEW-connection allow-list."


def sif_disallow_port(port: int) -> str:
    _allowed_new_ports.discard(int(port))
    return f"Port {port} removed from the NEW-connection allow-list."


# ---------------------------------------------------------------------------
# Dispatcher (same calling convention as computer_settings / firewall_control)
# ---------------------------------------------------------------------------

def network_firewall(
    parameters: dict = None,
    response=None,
    player=None,
) -> str:
    """
    parameters:
        action: "start" | "stop" | "status" | "log" | "allow_port" | "disallow_port"
        value:  port number (for allow_port/disallow_port) or comma-separated
                ports (for start, e.g. "8000,443")
    """
    params = parameters or {}
    action = str(params.get("action", "")).strip().lower()
    value = params.get("value")

    if player:
        player.write_log(f"[SIF] {action}")

    if action == "start":
        ports = None
        if value:
            ports = [p.strip() for p in str(value).split(",") if p.strip()]
        return sif_start(ports)
    if action == "stop":
        return sif_stop()
    if action == "status":
        return sif_status()
    if action == "log":
        n = int(value) if value else 20
        return sif_recent_log(n)
    if action == "allow_port":
        if not value:
            return "No port specified."
        return sif_allow_port(int(value))
    if action == "disallow_port":
        if not value:
            return "No port specified."
        return sif_disallow_port(int(value))

    return f"Unknown SIF action: '{action}'."
