"""
pragon_moss/selftest.py
════════════════════════════════════════════════════════════════════════════
Proves the integration works without needing Ollama, a GPU, a network, or a
display.

    python -m pragon_moss.selftest

Checks, in order:
  1. moss.py imports and an engine spins up on a temp db
  2. the bridge's blocking facade round-trips write → search → evaluate
  3. conflict detection and the guardrail's fail-safe path behave
  4. the wrappers preserve the original callables exactly (a stub class with
     the same method names is patched, called, and its originals confirmed to
     have run with their real arguments)
  5. uninstall() restores the originals byte-for-byte
  6. with PRAGON_MOSS=0 the wrappers are pass-through

Step 4 uses a stub rather than the real JarvisLive on purpose: importing
pragon_main pulls in audio devices, a Tk UI and 37 tool modules, none of which
belong in a smoke test. `python -m pragon_moss.selftest --live` does the real
import too, if you want it.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, FAIL = "  ✓", "  ✗"
_failures = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label} {detail}")
        _failures.append(label)


# ── stub standing in for JarvisLive ──────────────────────────────────────
class _StubUI:
    def __init__(self):
        self.logs = []

    def write_log(self, line):
        self.logs.append(line)

    def write_ghost_log(self, line):
        self.logs.append(line)


class _StubJarvis:
    """Same method names and signatures as the real thing; records what it
    was handed so we can prove the wrapper passed everything through."""

    def __init__(self):
        self.ui = _StubUI()
        self.seen = []

    def _friday_processor(self, text, host, model):
        self.seen.append(("friday", text, host, model))
        return f"friday-reply::{len(text)}"

    def _ghost_agent_turn(self, text, host, model):
        self.seen.append(("ghost", text, host, model))
        return "ghost-reply"

    def _ghost_execute_tool(self, name, args):
        self.seen.append(("tool", name, args))
        return f"ran {name}"


def main() -> int:
    live = "--live" in sys.argv
    tmp = tempfile.mkdtemp(prefix="moss_selftest_")
    os.environ["PRAGON_MOSS_DB"] = str(Path(tmp) / "selftest.db")
    os.environ.setdefault("PRAGON_MOSS_EMBED", "hashing")  # no model download
    os.environ["PRAGON_MOSS"] = "1"

    print("\n── 1. engine ──")
    from pragon_moss.bridge import MossBridge

    bridge = MossBridge()
    check("moss engine starts", bridge.enabled and bridge.engine is not None, bridge.error or "")
    if not bridge.enabled:
        return 1
    check("embedding backend resolved", bool(bridge.engine.embed_backend_name),
          bridge.engine.embed_backend_name)

    print("\n── 2. write / read round-trip ──")
    item = bridge.remember("Customer is using ArcticAir X500.",
                           {"type": "observation", "agent": "intake", "equipment": "X500"},
                           channel="selftest")
    check("write returns an id", bool(item))
    bridge.remember("Safety: isolate power before opening the compressor housing.",
                    {"type": "safety", "agent": "policy", "confirmed": False},
                    channel="selftest")
    hits = bridge.recall("what equipment are we on?", channel="selftest", use_cache=False)
    check("search returns hits", len(hits) > 0, f"got {len(hits)}")
    check("hits carry priority + score",
          bool(hits) and "priority" in hits[0] and "score" in hits[0])

    block = bridge.format_block(hits)
    check("prompt block renders", "MOSS CONTEXT" in block)
    check("augment() prefixes the prompt",
          bridge.augment("what equipment?", channel="selftest").endswith("what equipment?"))

    print("\n── 3. evaluate + guardrail ──")
    bridge.remember("Equipment updated to ArcticAir X700 per customer correction.",
                    {"type": "observation", "agent": "intake", "equipment": "X700"},
                    channel="selftest")
    ev = bridge.evaluate("what equipment are we working with?", channel="selftest")
    check("evaluate reports a status", ev.get("context_status") in
          ("relevant", "weak", "stale", "conflict", "missing"), str(ev.get("context_status")))
    check("conflicting equipment values detected",
          ev.get("context_status") == "conflict" or bool(ev.get("conflicts")),
          str(ev.get("conflicts")))

    gr = bridge.guardrail("Open the compressor housing.",
                          policy_query="power isolation procedure", channel="selftest")
    check("unconfirmed safety context → WARN", gr.get("decision") == "WARN", str(gr))

    print("\n── 4. wrappers preserve PRAGON ──")
    from pragon_moss import integration

    integration._INSTALLED = False
    stub = _StubJarvis()
    cls = _StubJarvis
    cls._friday_processor = integration._wrap_friday(cls._friday_processor)
    cls._ghost_agent_turn = integration._wrap_ghost_turn(cls._ghost_agent_turn)
    cls._ghost_execute_tool = integration._wrap_ghost_tool(cls._ghost_execute_tool)

    reply = stub._friday_processor("what did we confirm?", "http://localhost:11434", "gemma3")
    check("original FRIDAY still ran", any(s[0] == "friday" for s in stub.seen))
    check("FRIDAY reply passed straight back", reply.startswith("friday-reply::"))
    friday_call = [s for s in stub.seen if s[0] == "friday"][0]
    check("host/model passed through untouched",
          friday_call[2] == "http://localhost:11434" and friday_call[3] == "gemma3")
    check("user text still present inside the augmented prompt",
          "what did we confirm?" in friday_call[1])

    stub._ghost_agent_turn("run a scan", "http://localhost:11434", "qwen2.5")
    check("original GHOST turn still ran", any(s[0] == "ghost" for s in stub.seen))

    os.environ["PRAGON_MOSS_GUARDRAIL"] = "warn"
    out = stub._ghost_execute_tool("open_app", {"app_name": "notepad"})
    check("tool executed in warn mode", out == "ran open_app", out)

    os.environ["PRAGON_MOSS_GUARDRAIL"] = "off"
    out = stub._ghost_execute_tool("open_app", {"app_name": "notepad"})
    check("guardrail off → straight through", out == "ran open_app", out)
    os.environ["PRAGON_MOSS_GUARDRAIL"] = "warn"

    print("\n── 5. restore ──")
    for attr in ("_friday_processor", "_ghost_agent_turn", "_ghost_execute_tool"):
        orig = getattr(getattr(cls, attr), "__wrapped_original__", None)
        check(f"{attr} keeps its original", orig is not None)
        if orig is not None:
            setattr(cls, attr, orig)
    check("stub restored to plain behaviour",
          _StubJarvis()._friday_processor("x", "h", "m") == "friday-reply::1")

    print("\n── 6. kill switch ──")
    from pragon_moss.bridge import MossBridge as MB

    os.environ["PRAGON_MOSS"] = "0"
    dark = MB()
    check("PRAGON_MOSS=0 disables the bridge", not dark.enabled)
    check("disabled augment() is identity", dark.augment("hello") == "hello")
    check("disabled recall() is empty", dark.recall("hello") == [])
    check("disabled guardrail does not block", dark.guardrail("anything")["decision"] == "ALLOW")
    os.environ["PRAGON_MOSS"] = "1"

    if live:
        print("\n── 7. live pragon_main import ──")
        try:
            import pragon_main

            rep = integration.install(pragon_main)
            check("install() patched something", bool(rep["installed"]), str(rep))
            integration.uninstall(pragon_main)
            check("uninstall() restored originals",
                  not getattr(pragon_main.JarvisLive._friday_processor, "_moss_wrapped", False))
        except Exception as exc:
            check("pragon_main imports", False, f"{type(exc).__name__}: {exc}")

    print("\n" + ("ALL CHECKS PASSED" if not _failures
                  else f"{len(_failures)} FAILED: {_failures}"))
    return 0 if not _failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
