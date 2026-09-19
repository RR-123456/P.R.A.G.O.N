"""
plugins/field_ops.py
════════════════════════════════════════════════════════════════════════════
FIELD-OPS  —  Context-Aware Field Agent (single drop-in module)

WHY THIS EXISTS
    Distills the "voice agent for frontline/field workers" innovation set
    (asset-aware retrieval, zero-touch work orders, safety guardian,
    parts check, expert-in-the-ear / "why?" engine, dispatcher assist,
    job memory) into ONE self-contained PRAGON plugin, instead of one file
    per idea. It is discovered automatically by pragoncore/plugin_loader.py
    — dropping this file into plugins/ is the only step required; no other
    file needs to change.

DESIGN
    - Local-first: all state lives in the existing PRAGON SQLite brain via
      consciousness.data_access.dal (the shared Data Access Layer already
      used to fix the shared-database anti-pattern), so this module never
      opens its own ad-hoc DB file or duplicates a connection.
    - One voice-callable tool ("field_ops") with an `action` sub-command,
      so Gemini calls a single, well-documented function instead of the
      model having to pick between a dozen near-duplicate tools.
    - Every answer is tagged with a confidence label — VERIFIED / SUPPORTED
      / UNCERTAIN / ESCALATE — per the "no confident guessing" requirement
      for field procedures (Challenge 3 in the field-agent research notes).
    - Safety-relevant actions never auto-clear a lockout/isolation step;
      they only track what the worker has verbally confirmed and remind
      them what is still outstanding. This module supports, and does not
      replace, the site's formal safety procedure and human authorization.

ACTIONS (all handled by the one `field_ops` tool)
    lookup_asset      — asset history, last service, known recurring issues
    log_job           — zero-touch work order: turn spoken summary into a
                         structured job record (also feeds job memory)
    why               — "why?" engine: explain a recommended action using
                         asset history + past jobs on the same asset
    check_parts       — local parts-availability lookup + reserve
    safety_check      — contextual safety reminder + tracks confirmed steps
    dispatch_suggest  — rank candidate technicians for a job by skill/
                         location/availability (from a local roster)
    recall_expert     — "expert-in-the-ear": past technicians/cases that
                         solved a similar symptom on this asset family
    escalate          — explicitly flag "insufficient evidence, get a human"

All data is seeded/updated conversationally (log_job, dispatch roster
entries, etc.) — nothing here requires a pre-built dataset to be useful,
though it improves as more jobs are logged.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

try:
    from consciousness.data_access import dal
except Exception:  # pragma: no cover - allows standalone import/testing
    dal = None

DB_PATH = "pragon_brain/pragon_brain.db"

PLUGIN = {
    "name": "field_ops",
    "description": (
        "Context-aware field-technician assistant: asset history lookup, "
        "turning a spoken job summary into a structured work order, a "
        "'why am I doing this' explanation grounded in asset/job history, "
        "local spare-parts availability + reservation, a safety-procedure "
        "reminder that tracks lockout/isolation confirmations, technician "
        "dispatch suggestions, recalling how similar faults were solved "
        "before, and escalating to a human when evidence is insufficient. "
        "Use for phrases like 'what's the history on pump 182', 'log this "
        "job', 'why are we replacing this part', 'do we have a spare "
        "sensor', 'what's the safety procedure', 'who should I send', "
        "'has this happened before', or 'I'm not sure, escalate this'. "
        "Do NOT use for general knowledge questions unrelated to a "
        "physical asset, job, or field task."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "One of: lookup_asset, log_job, why, check_parts, "
                    "safety_check, dispatch_suggest, recall_expert, escalate"
                ),
            },
            "asset_id": {"type": "STRING", "description": "Asset/equipment identifier, e.g. 'P-182' or 'pump 182'."},
            "symptom": {"type": "STRING", "description": "What the worker observed, e.g. 'high vibration'."},
            "action_taken": {"type": "STRING", "description": "What the worker did, for log_job."},
            "outcome": {"type": "STRING", "description": "Result of the job, for log_job (e.g. 'resolved')."},
            "part_name": {"type": "STRING", "description": "Part/spare name for check_parts."},
            "quantity": {"type": "STRING", "description": "Quantity needed/reserved for check_parts."},
            "reserve": {"type": "STRING", "description": "'yes' to reserve the part instead of just checking, for check_parts."},
            "procedure": {"type": "STRING", "description": "Safety procedure name/code, or the step being confirmed, for safety_check."},
            "confirm": {"type": "STRING", "description": "What the worker just confirmed, e.g. 'isolation confirmed', for safety_check."},
            "skill": {"type": "STRING", "description": "Required skill/certification for dispatch_suggest."},
            "location": {"type": "STRING", "description": "Job location/site for dispatch_suggest."},
            "reason": {"type": "STRING", "description": "Why this needs human escalation, for escalate."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "field_ops",
    "title": "Field-Ops",
    "fields": [
        {"key": "default_site", "label": "Default site/location", "type": "text"},
    ],
}

_VALID_ACTIONS = {
    "lookup_asset", "log_job", "why", "check_parts",
    "safety_check", "dispatch_suggest", "recall_expert", "escalate",
}


# ── storage ──────────────────────────────────────────────────────────────

def _conn():
    if dal is None:
        raise RuntimeError("data_access layer unavailable")
    return dal.get_sqlite(DB_PATH)


def _ensure_schema() -> None:
    conn = _conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS field_ops_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            symptom TEXT,
            action_taken TEXT,
            outcome TEXT,
            technician TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_field_ops_jobs_asset
            ON field_ops_jobs (asset_id);

        CREATE TABLE IF NOT EXISTS field_ops_parts (
            part_name TEXT PRIMARY KEY,
            quantity INTEGER NOT NULL DEFAULT 0,
            location TEXT
        );

        CREATE TABLE IF NOT EXISTS field_ops_safety_state (
            asset_id TEXT NOT NULL,
            procedure TEXT NOT NULL,
            step TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            PRIMARY KEY (asset_id, procedure, step)
        );

        CREATE TABLE IF NOT EXISTS field_ops_technicians (
            name TEXT PRIMARY KEY,
            skills TEXT,
            location TEXT,
            available INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_asset(asset_id: str) -> str:
    """'pump 182' -> 'PUMP-182' style normalisation so voice input matches
    stored IDs loosely instead of requiring an exact string match."""
    a = re.sub(r"\s+", "-", asset_id.strip().upper())
    a = re.sub(r"-{2,}", "-", a)
    return a


# ── actions ──────────────────────────────────────────────────────────────

def _lookup_asset(asset_id: str) -> str:
    if not asset_id:
        return "Sir, I need an asset ID to look up its history."
    aid = _norm_asset(asset_id)
    conn = _conn()
    rows = conn.execute(
        "SELECT symptom, action_taken, outcome, created_at FROM field_ops_jobs "
        "WHERE asset_id = ? ORDER BY created_at DESC LIMIT 5",
        (aid,),
    ).fetchall()
    if not rows:
        return (
            f"UNCERTAIN: I have no logged history for {aid}. This may be its "
            f"first recorded job — log this job when you're done so future "
            f"lookups have something to go on."
        )
    last = rows[0]
    lines = [f"SUPPORTED: {aid} has {len(rows)} logged job(s). Most recent — "
             f"symptom: {last['symptom'] or 'n/a'}, action: {last['action_taken'] or 'n/a'}, "
             f"outcome: {last['outcome'] or 'n/a'}."]
    if len(rows) > 1:
        symptoms = [r["symptom"] for r in rows if r["symptom"]]
        repeat = _most_common(symptoms)
        if repeat:
            lines.append(f"Recurring symptom across history: {repeat}.")
    return " ".join(lines)


def _log_job(asset_id: str, symptom: str, action_taken: str, outcome: str) -> str:
    if not asset_id:
        return "Sir, I need an asset ID to log this job against."
    aid = _norm_asset(asset_id)
    conn = _conn()
    conn.execute(
        "INSERT INTO field_ops_jobs (asset_id, symptom, action_taken, outcome, technician, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (aid, symptom or "", action_taken or "", outcome or "", "", _now()),
    )
    conn.commit()
    return (
        f"VERIFIED: Logged job for {aid} — symptom: {symptom or 'n/a'}, "
        f"action: {action_taken or 'n/a'}, outcome: {outcome or 'n/a'}."
    )


def _why(asset_id: str) -> str:
    if not asset_id:
        return "Sir, tell me which asset, and I'll explain the reasoning from its history."
    aid = _norm_asset(asset_id)
    conn = _conn()
    rows = conn.execute(
        "SELECT symptom, action_taken, outcome, created_at FROM field_ops_jobs "
        "WHERE asset_id = ? ORDER BY created_at DESC LIMIT 10",
        (aid,),
    ).fetchall()
    if not rows:
        return f"UNCERTAIN: No job history on {aid} to reason from — I can't ground a 'why' answer yet."
    symptoms = [r["symptom"] for r in rows if r["symptom"]]
    repeat = _most_common(symptoms)
    if repeat and symptoms.count(repeat) > 1:
        return (
            f"SUPPORTED: {aid} has recurring reports of '{repeat}' "
            f"({symptoms.count(repeat)} of {len(rows)} logged jobs). That pattern is "
            f"the basis for the current recommendation — worth flagging if the same "
            f"root cause hasn't actually been addressed yet."
        )
    return (
        f"SUPPORTED: {aid}'s most recent logged job was '{rows[0]['symptom'] or 'n/a'}', "
        f"resolved by '{rows[0]['action_taken'] or 'n/a'}' with outcome "
        f"'{rows[0]['outcome'] or 'n/a'}'. No strong recurring pattern beyond that."
    )


def _check_parts(part_name: str, quantity: str, reserve: str) -> str:
    if not part_name:
        return "Sir, which part do you need me to check?"
    conn = _conn()
    _ensure_seed_parts(conn)
    row = conn.execute(
        "SELECT quantity, location FROM field_ops_parts WHERE part_name = ?",
        (part_name.strip().lower(),),
    ).fetchone()
    if row is None:
        return f"UNCERTAIN: '{part_name}' isn't in the local parts index. Check the enterprise inventory system directly."
    qty_needed = _to_int(quantity, default=1)
    have = row["quantity"]
    loc = row["location"] or "unspecified location"
    if str(reserve or "").strip().lower() in ("yes", "true", "confirm", "reserve"):
        if have < qty_needed:
            return f"ESCALATE: Only {have} of '{part_name}' available at {loc}; cannot reserve {qty_needed}."
        conn.execute(
            "UPDATE field_ops_parts SET quantity = quantity - ? WHERE part_name = ?",
            (qty_needed, part_name.strip().lower()),
        )
        conn.commit()
        return f"VERIFIED: Reserved {qty_needed} of '{part_name}' at {loc}. {have - qty_needed} remaining."
    if have <= 0:
        return f"SUPPORTED: '{part_name}' is out of stock at {loc}."
    return f"SUPPORTED: {have} of '{part_name}' available at {loc}."


def _safety_check(asset_id: str, procedure: str, confirm: str) -> str:
    aid = _norm_asset(asset_id) if asset_id else "UNSPECIFIED-ASSET"
    proc = procedure or "general lockout/isolation"
    conn = _conn()
    if confirm:
        conn.execute(
            "INSERT OR REPLACE INTO field_ops_safety_state (asset_id, procedure, step, confirmed_at) "
            "VALUES (?, ?, ?, ?)",
            (aid, proc, confirm.strip().lower(), _now()),
        )
        conn.commit()
        return f"VERIFIED: Logged confirmation '{confirm}' for {proc} on {aid}. Continue to the next step."
    rows = conn.execute(
        "SELECT step FROM field_ops_safety_state WHERE asset_id = ? AND procedure = ?",
        (aid, proc),
    ).fetchall()
    confirmed = {r["step"] for r in rows}
    if "isolation confirmed" not in confirmed and "lockout confirmed" not in confirmed:
        return (
            f"Before proceeding on {aid} ({proc}): this system does not replace your site's "
            f"formal safety procedure. Confirm isolation and lockout with your authorized "
            f"process first, then tell me 'isolation confirmed' or 'lockout confirmed'."
        )
    return f"VERIFIED: Isolation/lockout already confirmed for {aid} ({proc}). Proceed per the written procedure."


def _dispatch_suggest(skill: str, location: str) -> str:
    conn = _conn()
    _ensure_seed_technicians(conn)
    rows = conn.execute(
        "SELECT name, skills, location, available FROM field_ops_technicians WHERE available = 1"
    ).fetchall()
    if not rows:
        return "UNCERTAIN: No technicians recorded as available in the local roster."
    ranked = []
    for r in rows:
        score = 0
        skills = (r["skills"] or "").lower()
        if skill and skill.strip().lower() in skills:
            score += 2
        if location and location.strip().lower() in (r["location"] or "").lower():
            score += 1
        ranked.append((score, r["name"], r["skills"], r["location"]))
    ranked.sort(key=lambda t: t[0], reverse=True)
    top = ranked[:3]
    desc = "; ".join(f"{n} (skills: {sk or 'n/a'}, at {loc or 'n/a'}, match score {s})" for s, n, sk, loc in top)
    return f"SUPPORTED: Suggested candidates in order — {desc}. A human dispatcher should confirm the assignment."


def _recall_expert(asset_id: str, symptom: str) -> str:
    conn = _conn()
    if asset_id:
        aid = _norm_asset(asset_id)
        rows = conn.execute(
            "SELECT technician, action_taken, outcome FROM field_ops_jobs "
            "WHERE asset_id = ? AND outcome LIKE '%resolved%' ORDER BY created_at DESC LIMIT 5",
            (aid,),
        ).fetchall()
    elif symptom:
        rows = conn.execute(
            "SELECT technician, action_taken, outcome, asset_id FROM field_ops_jobs "
            "WHERE symptom LIKE ? AND outcome LIKE '%resolved%' ORDER BY created_at DESC LIMIT 5",
            (f"%{symptom}%",),
        ).fetchall()
    else:
        return "Sir, give me an asset ID or a symptom to search past resolved cases for."
    if not rows:
        return "UNCERTAIN: No resolved past cases match that in the local job log."
    actions = [r["action_taken"] for r in rows if r["action_taken"]]
    common = _most_common(actions)
    return (
        f"SUPPORTED: {len(rows)} past resolved case(s) match. Most common fix: "
        f"'{common or actions[0]}'. Treat this as prior evidence, not a guarantee — "
        f"confirm root cause on-site before repeating it."
    )


def _escalate(asset_id: str, reason: str) -> str:
    aid = _norm_asset(asset_id) if asset_id else "unspecified asset"
    why = reason or "insufficient evidence to proceed automatically"
    return f"ESCALATE: Flagging {aid} for human review — {why}. A qualified expert should take it from here."


# ── small helpers ────────────────────────────────────────────────────────

def _most_common(items: list[str]) -> Optional[str]:
    items = [i for i in items if i]
    if not items:
        return None
    counts: dict[str, int] = {}
    for i in items:
        counts[i] = counts.get(i, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _to_int(value, default: int = 1) -> int:
    try:
        return max(1, int(str(value).strip()))
    except Exception:
        return default


def _ensure_seed_parts(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS c FROM field_ops_parts").fetchone()
    if row["c"] == 0:
        conn.executemany(
            "INSERT INTO field_ops_parts (part_name, quantity, location) VALUES (?, ?, ?)",
            [
                ("pressure sensor", 2, "Chennai warehouse"),
                ("bearing kit", 5, "Chennai warehouse"),
                ("gasket seal", 10, "Depot 2"),
            ],
        )
        conn.commit()


def _ensure_seed_technicians(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS c FROM field_ops_technicians").fetchone()
    if row["c"] == 0:
        conn.executemany(
            "INSERT INTO field_ops_technicians (name, skills, location, available) VALUES (?, ?, ?, 1)",
            [
                ("A. Kumar", "pumps, hydraulics", "Chennai"),
                ("S. Reddy", "electrical, HV cabinets", "Chennai"),
                ("R. Nair", "pumps, sensors", "Coimbatore"),
            ],
        )
        conn.commit()


# ── entry point (matches the plugin contract) ───────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action", "")).strip().lower()
    if action not in _VALID_ACTIONS:
        return (
            f"Sir, '{action or '(none)'}' isn't a field_ops action. Valid actions: "
            + ", ".join(sorted(_VALID_ACTIONS)) + "."
        )
    try:
        _ensure_schema()
        if action == "lookup_asset":
            result = _lookup_asset(parameters.get("asset_id", ""))
        elif action == "log_job":
            result = _log_job(
                parameters.get("asset_id", ""),
                parameters.get("symptom", ""),
                parameters.get("action_taken", ""),
                parameters.get("outcome", ""),
            )
        elif action == "why":
            result = _why(parameters.get("asset_id", ""))
        elif action == "check_parts":
            result = _check_parts(
                parameters.get("part_name", ""),
                parameters.get("quantity", ""),
                parameters.get("reserve", ""),
            )
        elif action == "safety_check":
            result = _safety_check(
                parameters.get("asset_id", ""),
                parameters.get("procedure", ""),
                parameters.get("confirm", ""),
            )
        elif action == "dispatch_suggest":
            result = _dispatch_suggest(parameters.get("skill", ""), parameters.get("location", ""))
        elif action == "recall_expert":
            result = _recall_expert(parameters.get("asset_id", ""), parameters.get("symptom", ""))
        else:  # escalate
            result = _escalate(parameters.get("asset_id", ""), parameters.get("reason", ""))
    except Exception as e:
        return f"Sir, field_ops '{action}' failed: {e}"

    if player:
        try:
            player.write_log(f"PRAGON [field_ops:{action}]: {result}")
        except Exception:
            pass
    return result
