"""
features/agentic_feature/dry_run.py
════════════════════════════════════════════════════════════════════════════
Dry-run / preview layer for risky agentic actions.

WHY THIS EXISTS
    pragoncore/undo.py already makes most actions *reversible after the
    fact* -- act first, remember how to revert, let the user say "undo".
    That's the right default for low-stakes actions (renaming a file,
    opening an app): asking "are you sure?" every time trains people to
    stop reading the prompt and just say yes.

    But a few action classes are either irreversible in practice (deleting
    a file outside a trash-recoverable path, running a shell command,
    shutting down/restarting the machine) or expensive to get wrong (a
    generated-code fallback running arbitrary Python). For exactly those,
    "undo after" isn't good enough -- the user should see what's *about* to
    happen before it happens.

    This module classifies a (tool, parameters) pair into a risk tier and,
    for medium/high risk, builds a one-line human-readable preview of the
    concrete effect ("This will permanently delete: Downloads/report.docx").
    It does not decide whether to proceed -- that's still
    confirmation_gate.py's job, now handed a real preview instead of just
    the raw action description.

HOW IT'S WIRED IN
    Called from AgentExecutor._call_tool right before dispatch (see
    agent/executor.py), the same single choke point action_ledger.py hooks
    into. Low-risk actions pass through untouched -- this only adds
    latency/friction for the handful of tools that are genuinely
    irreversible or destructive.

USAGE
    from features.agentic_feature.dry_run import assess

    verdict = assess("file_controller", {"action": "delete", "path": "report.docx"})
    if verdict.risk != "low":
        approved = confirmation_gate.ask(f"{verdict.preview} Shall I proceed?")
        if not approved:
            return "Cancelled before execution."
"""

from __future__ import annotations

from dataclasses import dataclass

# Tools whose *some* actions are irreversible enough to warrant a preview.
# A tool not listed here is treated as low-risk (fire-and-rely-on-undo,
# PRAGON's existing default).
_HIGH_RISK_TOOLS = {"cmd_control", "generated_code"}
_MEDIUM_RISK_TOOLS = {"file_controller", "computer_settings", "computer_control", "send_message"}

# Within file_controller / computer_control, these sub-actions are the ones
# that actually destroy or externally transmit something -- everything else
# in those tools (read, list, open) stays low-risk even though the tool
# name is in the medium-risk set above.
_DESTRUCTIVE_FILE_ACTIONS = {"delete", "overwrite", "move", "rename"}
_DESTRUCTIVE_SYSTEM_ACTIONS = {"shutdown", "restart", "sleep", "log_off", "factory_reset"}


@dataclass
class DryRunVerdict:
    risk: str            # "low" | "medium" | "high"
    preview: str         # human-readable description of the concrete effect
    reversible: bool     # whether pragoncore/undo.py can realistically undo this


def _preview_file_action(params: dict) -> str:
    action = (params.get("action") or "").lower()
    path = params.get("path") or params.get("source") or params.get("file") or "(unspecified path)"
    if action == "delete":
        return f"This will permanently delete: {path}"
    if action == "overwrite":
        return f"This will overwrite the existing content of: {path}"
    if action in ("move", "rename"):
        dest = params.get("destination") or params.get("dest") or params.get("new_path") or "(unspecified destination)"
        return f"This will move/rename '{path}' to '{dest}'"
    return f"This will run '{action}' on: {path}"


def _preview_system_action(params: dict) -> str:
    action = (params.get("action") or "").lower()
    return f"This will {action.replace('_', ' ')} the computer."


def _preview_cmd(params: dict) -> str:
    cmd = params.get("command") or params.get("cmd") or "(unspecified command)"
    return f"This will run the shell command: {cmd}"


def _preview_generated_code(params: dict) -> str:
    desc = str(params.get("description", ""))[:200]
    return f"This will generate and execute a one-off Python script for: {desc}"


def _preview_send_message(params: dict) -> str:
    to = params.get("to") or params.get("contact") or "(unspecified recipient)"
    body = str(params.get("message") or params.get("content") or "")[:80]
    return f'This will send a message to {to}: "{body}"'


def assess(tool: str, parameters: dict) -> DryRunVerdict:
    """
    Classify a tool call before it runs. Never raises -- an unrecognised
    tool or malformed parameters just falls back to low-risk, since the
    goal is to add friction only where we're confident it's warranted, not
    to block actions this classifier doesn't understand.
    """
    params = parameters or {}
    action = str(params.get("action", "")).lower()

    try:
        if tool == "cmd_control":
            return DryRunVerdict("high", _preview_cmd(params), reversible=False)

        if tool == "generated_code":
            return DryRunVerdict("high", _preview_generated_code(params), reversible=False)

        if tool == "file_controller" and action in _DESTRUCTIVE_FILE_ACTIONS:
            # A hard delete is riskier than a move/rename/overwrite, which
            # undo.py can usually reverse.
            risk = "high" if action == "delete" else "medium"
            return DryRunVerdict(risk, _preview_file_action(params), reversible=(action != "delete"))

        if tool == "computer_control" and action in _DESTRUCTIVE_SYSTEM_ACTIONS:
            return DryRunVerdict("high", _preview_system_action(params), reversible=False)

        if tool == "computer_settings" and action in _DESTRUCTIVE_SYSTEM_ACTIONS:
            return DryRunVerdict("high", _preview_system_action(params), reversible=False)

        if tool == "send_message":
            return DryRunVerdict("medium", _preview_send_message(params), reversible=False)

    except Exception:
        # A bug in preview rendering should never itself block or crash an
        # action -- fall through to the low-risk default below.
        pass

    return DryRunVerdict("low", "", reversible=True)


def needs_preview(verdict: DryRunVerdict) -> bool:
    return verdict.risk in ("medium", "high")
