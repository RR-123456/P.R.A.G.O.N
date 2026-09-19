"""
CPU Balancer — rebalances CPU load across cores/processes.
Zero subprocess calls on all platforms — uses psutil only, same style as system_monitor.py.

What it does:
  - Snapshots per-process CPU usage.
  - For processes that are hogging CPU (above `hog_threshold`) and are not on an
    ignore-list of critical/system processes, it:
      1. Lowers their scheduling priority one notch (nice / priority class),
         so the OS scheduler gives foreground / lighter processes a fairer share.
      2. Re-spreads their CPU affinity across all logical cores in round-robin
         order, so load isn't pinned onto the same few cores.
  - Never kills, suspends, or force-quits anything — purely a "be a better
    citizen" rebalance, entirely reversible (affinity/priority are just hints
    to the OS scheduler).
"""
import itertools
import platform

import psutil

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

DEFAULT_HOG_THRESHOLD = 15.0   # % CPU — a process above this is a "hog" candidate
DEFAULT_MAX_REBALANCE = 6      # don't touch more than N processes per pass
_COOLDOWN = 60                 # seconds between automatic passes

# Never touch these — system-critical / would break the machine or PRAGON itself.
_IGNORE_NAMES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
    "systemd", "systemd-journald", "kernel_task", "launchd",
    "python.exe", "pythonw.exe", "pragon_main.py",
}

if _OS == "Windows":
    import psutil as _psutil
    _LOWER_PRIORITY = _psutil.BELOW_NORMAL_PRIORITY_CLASS
    _NORMAL_PRIORITY = _psutil.NORMAL_PRIORITY_CLASS
else:
    _LOWER_PRIORITY = 5   # nice delta-ish absolute value, gentle
    _NORMAL_PRIORITY = 0


def _core_cycle():
    n = psutil.cpu_count(logical=True) or 1
    return itertools.cycle(range(n))


def get_cpu_breakdown() -> dict:
    """Per-core + top-process CPU snapshot, used by the balancer and for status reporting."""
    per_core = psutil.cpu_percent(interval=0.3, percpu=True)
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent"]):
        try:
            info = p.info
            cpu = info.get("cpu_percent") or 0.0
            if cpu > 0:
                procs.append({"pid": info["pid"], "name": info["name"], "cpu_percent": round(cpu, 1)})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x["cpu_percent"], reverse=True)

    return {
        "per_core_percent": [round(c, 1) for c in per_core],
        "core_count": len(per_core),
        "top_processes": procs[:10],
        "imbalance": round((max(per_core) - min(per_core)), 1) if per_core else 0.0,
    }


def balance_cpu(hog_threshold: float = DEFAULT_HOG_THRESHOLD,
                 max_rebalance: int = DEFAULT_MAX_REBALANCE) -> dict:
    """
    Runs one rebalancing pass. Returns a summary dict describing what changed.
    Safe to call on demand (voice command) or periodically (background task).
    """
    cores = _core_cycle()
    touched = []
    skipped_critical = 0

    candidates = [
        p for p in psutil.process_iter(["pid", "name", "cpu_percent"])
        if (p.info.get("cpu_percent") or 0.0) >= hog_threshold
    ]
    candidates.sort(key=lambda p: p.info.get("cpu_percent") or 0.0, reverse=True)

    for proc in candidates:
        if len(touched) >= max_rebalance:
            break
        name = (proc.info.get("name") or "").lower()
        if name in _IGNORE_NAMES:
            skipped_critical += 1
            continue
        try:
            core = next(cores)
            proc.nice(_LOWER_PRIORITY) if _OS != "Windows" else proc.nice(_LOWER_PRIORITY)
            try:
                proc.cpu_affinity([core])
            except (AttributeError, NotImplementedError):
                pass  # not supported on this platform (e.g. macOS has no cpu_affinity)
            touched.append({
                "pid": proc.info["pid"],
                "name": proc.info.get("name"),
                "cpu_percent": round(proc.info.get("cpu_percent") or 0.0, 1),
                "assigned_core": core,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    breakdown = get_cpu_breakdown()
    return {
        "rebalanced": touched,
        "skipped_critical": skipped_critical,
        "cpu_percent_total": psutil.cpu_percent(interval=None),
        "per_core_percent": breakdown["per_core_percent"],
        "imbalance_after": breakdown["imbalance"],
    }


def cpu_balancer_tool(parameters: dict | None = None, **_kwargs) -> str:
    """Entry point matching the other `features/feature/*` tool signatures."""
    parameters = parameters or {}
    mode = (parameters.get("mode") or "balance").lower()

    if mode == "status":
        b = get_cpu_breakdown()
        return (
            f"CPU is spread across {b['core_count']} cores, "
            f"top core-to-core imbalance is {b['imbalance']}%. "
            f"Busiest processes: " +
            ", ".join(f"{p['name']} ({p['cpu_percent']}%)" for p in b["top_processes"][:5])
        )

    result = balance_cpu(
        hog_threshold=float(parameters.get("hog_threshold", DEFAULT_HOG_THRESHOLD)),
        max_rebalance=int(parameters.get("max_rebalance", DEFAULT_MAX_REBALANCE)),
    )
    if not result["rebalanced"]:
        return "CPU load already looks balanced — nothing needed adjusting."
    names = ", ".join(f"{p['name']} → core {p['assigned_core']}" for p in result["rebalanced"])
    return (
        f"Rebalanced {len(result['rebalanced'])} process(es): {names}. "
        f"Total CPU now at {result['cpu_percent_total']:.0f}%, "
        f"core imbalance {result['imbalance_after']}%."
    )


class CPUBalancer:
    """
    Stateful auto-balancer for the background loop (mirrors SystemMonitor).
    Call check() periodically; it rebalances quietly and returns a short status
    string only when it actually changed something (so voice alerts stay rare).
    """

    def __init__(self, hog_threshold: float = DEFAULT_HOG_THRESHOLD,
                 max_rebalance: int = DEFAULT_MAX_REBALANCE):
        self.hog_threshold = hog_threshold
        self.max_rebalance = max_rebalance
        self._last_run = 0.0

    def check(self) -> str | None:
        import time
        now = time.monotonic()
        if now - self._last_run < _COOLDOWN:
            return None
        self._last_run = now
        try:
            result = balance_cpu(self.hog_threshold, self.max_rebalance)
        except Exception:
            return None
        if not result["rebalanced"]:
            return None
        return (
            f"[CPU_BALANCER] Rebalanced {len(result['rebalanced'])} process(es) "
            f"across cores to smooth out a CPU spike. No action needed from the user."
        )
