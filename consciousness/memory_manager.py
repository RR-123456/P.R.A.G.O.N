import json
from datetime import datetime
from threading import Lock
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
MEMORY_PATH = BASE_DIR / "consciousness" / "long_term.json"
_lock = Lock()
MAX_VALUE_LENGTH = 380

# ── Storage vs. prompt budget are two different numbers ─────────────────────
#
# There used to be one: MEMORY_MAX_CHARS = 2200, applied to the WHOLE store.
# It was a storage limit that existed only because the entire memory used to
# get pasted into the system prompt on every connect — so growing the memory
# grew every single request. When it filled, _trim_to_limit() deleted the
# oldest entries and printed one line to a console nobody reads. Silently
# forgetting facts after a few weeks of normal use was the direct result of
# treating "how much can I store" and "how much rides in the prompt" as the
# same number.
#
# They're separate now:
#   MEMORY_MAX_CHARS  — a runaway guard, not a feature limit. Normal use never
#                       approaches it; a bug writing in a loop does.
#   PROMPT_CORE_CHARS — what actually rides in the system prompt every
#                       session (see format_memory_for_prompt below).
# Anything that doesn't fit in the prompt core stays on disk and is reachable
# on demand via search_memory() / the recall_memory tool — no network, no
# second model call, comfortably under a millisecond for a store this size.
MEMORY_MAX_CHARS = 200_000
PROMPT_CORE_CHARS = 2000

# ── Session continuity (ported from Mark-LIII) ───────────────────────────────
# A short free-text summary of "what we were doing" persisted across restarts,
# so the next launch can pick the thread back up instead of starting cold.
# Deliberately separate from the structured long_term.json store above -- this
# is transient handoff state, not a durable fact about the person.
SESSION_SUMMARY_PATH = BASE_DIR / "consciousness" / "last_session.json"

# Optional hook fired whenever _trim_to_limit() actually deletes something, so
# the UI can surface "SYS: memory trimmed, N entries dropped" instead of that
# only ever landing in a console nobody reads. None means "don't notify".
_trim_notifier = None

def _empty_memory() -> dict:
    return {
        "identity": {},
        "preferences": {},
        "projects": {},
        "relationships": {},
        "wishes": {},
        "notes": {},
    }

def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                return data
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] Load error: {e}")
            return _empty_memory()

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


def set_trim_notifier(fn) -> None:
    """Register a callback fn(cat: str, key: str) -> None, invoked once per
    entry dropped by _trim_to_limit(). Pass None to unregister."""
    global _trim_notifier
    _trim_notifier = fn


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    entries = _all_entries(memory)
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        print(f"[Memory] Trimmed {cat}/{key}")
        if _trim_notifier:
            try:
                _trim_notifier(cat, key)
            except Exception:
                pass
    return memory

def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory = load_memory()
    if _recursive_update(memory, memory_update):
        save_memory(memory)
        print(f"[Memory] Saved: {list(memory_update.keys())}")
    return memory

def format_memory_for_prompt(memory: dict | None) -> str:
    if not memory:
        return ""

    lines = []

    identity = memory.get("identity", {})
    id_fields = ["name", "age", "birthday", "city", "job", "language", "school", "nationality"]
    for field in id_fields:
        entry = identity.get(field)
        if entry:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in id_fields:
            continue
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{key.replace('_', ' ').title()}: {val}")

    prefs = memory.get("preferences", {})
    if prefs:
        lines.append("")
        lines.append("Preferences:")
        for key, entry in list(prefs.items())[:15]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f" - {key.replace('_', ' ').title()}: {val}")

    projects = memory.get("projects", {})
    if projects:
        lines.append("")
        lines.append("Active Projects / Goals:")
        for key, entry in list(projects.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f" - {key.replace('_', ' ').title()}: {val}")

    rels = memory.get("relationships", {})
    if rels:
        lines.append("")
        lines.append("People in their life:")
        for key, entry in list(rels.items())[:10]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f" - {key.replace('_', ' ').title()}: {val}")

    wishes = memory.get("wishes", {})
    if wishes:
        lines.append("")
        lines.append("Wishes / Plans / Wants:")
        for key, entry in list(wishes.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f" - {key.replace('_', ' ').title()}: {val}")

    notes = memory.get("notes", {})
    if notes:
        lines.append("")
        lines.append("Other notes:")
        for key, entry in list(notes.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f" - {key}: {val}")

    if not lines:
        return ""

    header = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]\n"
    result = header + "\n".join(lines)
    if len(result) > PROMPT_CORE_CHARS:
        result = result[:PROMPT_CORE_CHARS - 3] + "…"

    return result + "\n"


def _score(query_words: list[str], cat: str, key: str, value: str) -> int:
    """Word-overlap relevance score (ported from Mark-LIII's memory search).
    A whole-phrase substring hit still wins outright; otherwise entries that
    match more of the query's individual words rank higher than an old plain
    substring-only match would, without changing search_memory's contract."""
    haystack = f"{cat} {key} {value}".lower()
    score = 0
    for w in query_words:
        if w and w in haystack:
            score += 1
    return score


def search_memory(query: str, limit: int = 5) -> list[dict]:
    """
    On-demand recall over the FULL store (not just what fit in the prompt
    core). Local match across category/key/value — no network, no second
    model call. Backs the `recall_memory` tool.

    Ranking: whole-phrase substring matches first (most specific), then
    word-overlap score, then recency -- so "my dog's name" still finds an
    entry that only contains "dog" even if the exact phrase never appears.
    """
    query = (query or "").strip().lower()
    if not query:
        return []
    query_words = [w for w in query.split() if w]
    memory = load_memory()
    hits = []
    for cat, key, entry in _all_entries(memory):
        val = str(entry.get("value", ""))
        haystack = f"{cat} {key} {val}".lower()
        exact = query in haystack
        score = _score(query_words, cat, key, val)
        if exact or score > 0:
            hits.append({
                "category": cat,
                "key": key,
                "value": val,
                "updated": entry.get("updated", ""),
                "_exact": exact,
                "_score": score,
            })
    hits.sort(key=lambda h: (h["_exact"], h["_score"], h.get("updated", "0000-00-00")), reverse=True)
    for h in hits:
        h.pop("_exact", None)
        h.pop("_score", None)
    return hits[:limit]


def all_entries_for_ui() -> list[dict]:
    """Flat list of every stored memory entry, for a future Settings > Memory
    browser panel. Ported from Mark-LIII; not wired into the UI yet."""
    memory = load_memory()
    return [
        {"category": cat, "key": key, "value": str(entry.get("value", "")), "updated": entry.get("updated", "")}
        for cat, key, entry in _all_entries(memory)
    ]

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    update_memory({category: {key: {"value": value}}})
    return f"Remembered: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    memory = load_memory()
    cat = memory.get(category, {})
    if key in cat:
        del cat[key]
        memory[category] = cat
        save_memory(memory)
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


forget_memory = forget


def save_session_summary(summary: str, language: str = "") -> None:
    """Persist a short free-text summary of the session just ended, so the
    next launch can greet the user with continuity ('last time we were
    working on...') instead of starting cold. Ported from Mark-LIII."""
    summary = (summary or "").strip()
    if not summary:
        return
    payload = {
        "summary": summary,
        "language": language or "",
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    try:
        SESSION_SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSION_SUMMARY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[Memory] Could not save session summary: {e}")


def pop_last_session() -> dict | None:
    """Read-and-clear the last saved session summary. 'Pop' semantics --
    once consumed at startup, it won't be replayed again next launch."""
    if not SESSION_SUMMARY_PATH.exists():
        return None
    try:
        data = json.loads(SESSION_SUMMARY_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = None
    try:
        SESSION_SUMMARY_PATH.unlink()
    except Exception:
        pass
    return data if isinstance(data, dict) else None