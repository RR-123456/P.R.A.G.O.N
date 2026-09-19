#firewall_control.py
"""
Firewall control action for Pragon.

Lets Pragon check status, enable/disable, and manage simple allow/block
rules on the host OS firewall:
  - Windows -> netsh advfirewall
  - macOS   -> socketfilterfw (Application Firewall)
  - Linux   -> ufw (falls back to iptables if ufw isn't installed)

All OS-mutating calls require elevated privileges (Admin / sudo). Pragon
should surface that to the user rather than silently failing.
"""

import platform
import subprocess

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

if _OS == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}


def _run(cmd: list) -> tuple:
    """Run a command, return (success, output)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            **_WIN_HIDE,
        )
        ok = result.returncode == 0
        out = (result.stdout or result.stderr or "").strip()
        return ok, out
    except FileNotFoundError:
        return False, f"Required tool not found for command: {' '.join(cmd)}"
    except subprocess.TimeoutExpired:
        return False, "Command timed out."
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def firewall_status() -> str:
    if _OS == "Windows":
        ok, out = _run(["netsh", "advfirewall", "show", "allprofiles", "state"])
        return out if ok else f"Could not read firewall status: {out}"

    if _OS == "Darwin":
        ok, out = _run([
            "/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"
        ])
        return out if ok else f"Could not read firewall status: {out}"

    # Linux
    ok, out = _run(["ufw", "status", "verbose"])
    if ok:
        return out
    ok, out = _run(["iptables", "-L", "-n"])
    return out if ok else f"Could not read firewall status: {out}"


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------

def enable_firewall() -> str:
    if _OS == "Windows":
        ok, out = _run(["netsh", "advfirewall", "set", "allprofiles", "state", "on"])
    elif _OS == "Darwin":
        ok, out = _run([
            "/usr/libexec/ApplicationFirewall/socketfilterfw", "--setglobalstate", "on"
        ])
    else:
        ok, out = _run(["ufw", "--force", "enable"])

    return "Firewall enabled." if ok else f"Failed to enable firewall (needs admin/sudo): {out}"


def disable_firewall() -> str:
    if _OS == "Windows":
        ok, out = _run(["netsh", "advfirewall", "set", "allprofiles", "state", "off"])
    elif _OS == "Darwin":
        ok, out = _run([
            "/usr/libexec/ApplicationFirewall/socketfilterfw", "--setglobalstate", "off"
        ])
    else:
        ok, out = _run(["ufw", "disable"])

    return "Firewall disabled." if ok else f"Failed to disable firewall (needs admin/sudo): {out}"


# ---------------------------------------------------------------------------
# Port rules
# ---------------------------------------------------------------------------

def block_port(port: int, protocol: str = "TCP") -> str:
    protocol = protocol.upper()
    if _OS == "Windows":
        rule = f"Pragon Block Port {port} {protocol}"
        ok, out = _run([
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={rule}", "dir=in", "action=block",
            f"protocol={protocol}", f"localport={port}",
        ])
    elif _OS == "Darwin":
        ok, out = _run(["pfctl", "-t", "pragon_block", "-T", "add", str(port)])
    else:
        ok, out = _run(["ufw", "deny", f"{port}/{protocol.lower()}"])

    return f"Blocked port {port}/{protocol}." if ok else f"Failed to block port {port}: {out}"


def allow_port(port: int, protocol: str = "TCP") -> str:
    protocol = protocol.upper()
    if _OS == "Windows":
        rule = f"Pragon Allow Port {port} {protocol}"
        ok, out = _run([
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={rule}", "dir=in", "action=allow",
            f"protocol={protocol}", f"localport={port}",
        ])
    elif _OS == "Darwin":
        ok, out = _run(["pfctl", "-t", "pragon_block", "-T", "delete", str(port)])
    else:
        ok, out = _run(["ufw", "allow", f"{port}/{protocol.lower()}"])

    return f"Allowed port {port}/{protocol}." if ok else f"Failed to allow port {port}: {out}"


# ---------------------------------------------------------------------------
# IP rules
# ---------------------------------------------------------------------------

def block_ip(ip: str) -> str:
    if _OS == "Windows":
        rule = f"Pragon Block IP {ip}"
        ok, out = _run([
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={rule}", "dir=in", "action=block", f"remoteip={ip}",
        ])
    elif _OS == "Darwin":
        ok, out = _run(["pfctl", "-t", "pragon_block_ip", "-T", "add", ip])
    else:
        ok, out = _run(["ufw", "deny", "from", ip])

    return f"Blocked IP {ip}." if ok else f"Failed to block IP {ip}: {out}"


def allow_ip(ip: str) -> str:
    if _OS == "Windows":
        rule = f"Pragon Block IP {ip}"
        ok, out = _run([
            "netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule}",
        ])
    elif _OS == "Darwin":
        ok, out = _run(["pfctl", "-t", "pragon_block_ip", "-T", "delete", ip])
    else:
        ok, out = _run(["ufw", "delete", "deny", "from", ip])

    return f"Allowed IP {ip}." if ok else f"Failed to allow IP {ip}: {out}"


# ---------------------------------------------------------------------------
# Dispatcher (matches the ACTION_MAP style used by computer_settings.py)
# ---------------------------------------------------------------------------

def firewall_control(
    parameters: dict = None,
    response=None,
    player=None,
) -> str:
    """
    Entry point for Pragon's action router (matches the computer_settings
    dispatch convention: called as
    firewall_control(parameters=args, response=None, player=self.ui)).

    parameters:
        action:   "status" | "enable" | "disable" | "block_port" |
                  "allow_port" | "block_ip" | "allow_ip"
        value:    port number or IP address, depending on the action
        protocol: "TCP" | "UDP" (only used for port actions, default TCP)
    """
    params = parameters or {}
    action = str(params.get("action", "")).strip().lower()
    value = params.get("value")
    protocol = str(params.get("protocol", "TCP") or "TCP")

    if not action:
        return "No firewall action specified."

    print(f"[Firewall] Action: {action} Value: {value} OS: {_OS}")
    if player:
        player.write_log(f"[Firewall] {action}")

    if action == "status":
        return firewall_status()
    if action == "enable":
        return enable_firewall()
    if action == "disable":
        return disable_firewall()
    if action == "block_port":
        if not value:
            return "No port specified."
        return block_port(int(value), protocol)
    if action == "allow_port":
        if not value:
            return "No port specified."
        return allow_port(int(value), protocol)
    if action == "block_ip":
        if not value:
            return "No IP specified."
        return block_ip(str(value))
    if action == "allow_ip":
        if not value:
            return "No IP specified."
        return allow_ip(str(value))

    return f"Unknown firewall action: '{action}'."
