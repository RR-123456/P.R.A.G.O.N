# MOSS × P.R.A.G.O.N

Moss is now part of PRAGON. **Nothing in PRAGON was removed, edited or moved** —
`diff -rq` against the original tree reports zero modified files. The integration
is entirely new files plus runtime wrapping.

---

## What was added

| Path | Purpose |
|---|---|
| `moss.py` | Your file, byte-for-byte unchanged, at the project root |
| `pragon_moss/bridge.py` | Blocking, never-raising facade over the async engine |
| `pragon_moss/integration.py` | Wraps PRAGON's processors (wrap, never replace) |
| `pragon_moss/service.py` | Optional HTTP surface on `:8090` |
| `pragon_moss/selftest.py` | Offline proof the wiring works |
| `run_pragon_moss.py` | Launcher: PRAGON + Moss, zero edits to `pragon_main.py` |
| `plugins/moss_remember.py` | Voice tool: write session context |
| `plugins/moss_recall.py` | Voice tool: retrieve session context |
| `plugins/moss_status.py` | Voice tool: real latency/cache/backend health |

No new dependencies. Moss is stdlib + sqlite3; the plugins ride PRAGON's own
loader; the optional service uses the fastapi/uvicorn already in
`requirements.txt`.

---

## Run it

```bash
python run_pragon_moss.py      # PRAGON with Moss attached
python pragon_main.py          # PRAGON exactly as before, untouched
```

The launcher imports `pragon_main` (which only *defines* things), wraps four
methods and one function, then calls PRAGON's own `main()`. `JarvisLive.__init__`
registers its processors with the UI *after* the wrap, so the UI ends up holding
the Moss-augmented callables with the originals still inside them.

Verify before you trust it:

```bash
python -m pragon_moss.selftest          # no Ollama, no GPU, no network needed
python -m pragon_moss.selftest --live   # also imports the real pragon_main
python moss.py benchmark --n 500        # real p50/p95/p99 on your hardware
```

---

## What Moss actually does in each path

**`_friday_processor`** — FRIDAY rebuilds a two-message list every turn, so she
has no memory between turns; whatever RAG context the frontend folded into `text`
is all she gets. Moss prefixes a `MOSS SESSION CONTEXT` block in front of that —
the RAG context survives intact, it is not replaced — then stores both the user
message and the reply. Second turn onward, FRIDAY can answer "what did we
establish earlier?"

**`_ghost_agent_turn`** — same continuity problem, same fix, plus a line in the
GHOST log showing how many items came back and the measured retrieval time.

**`_ghost_execute_tool`** — every tool call is checked against retrieved
safety/policy context *before* running, and the result is written back as
`state` context so the next turn knows what actually happened. This runs
alongside PRAGON's existing `confirm_gate`, never instead of it.

**`_execute_tool`** (JARVIS voice) — audit only. Voice actions become retrievable
context; the async path is otherwise untouched.

**`_rag_query`** — the knowledge-base answer comes back first, unchanged, with
Moss session hits appended underneath. Disable with `PRAGON_MOSS_RAG=0`.

Sessions are per-channel (`pragon:friday`, `pragon:ghost`, `pragon:voice`) and
persist across restarts in the sqlite file.

---

## Guardrail modes

`PRAGON_MOSS_GUARDRAIL=off | warn | enforce` — default `warn`.

`warn` is the default deliberately. A fresh Moss database contains no safety
items, so `enforce` on day one would block things for no reason. Populate policy
context first (via `moss_remember`, the HTTP API, or `moss.write`), confirm the
guardrail warns where you expect, then switch:

```bash
PRAGON_MOSS_GUARDRAIL=enforce python run_pragon_moss.py
```

In `enforce`, a `BLOCK` returns the reason as the tool result instead of
executing. Moss's own fail-safe still applies: if retrieval itself fails, the
verdict is `BLOCK`, never a silent allow.

---

## Environment switches

| Variable | Default | Meaning |
|---|---|---|
| `PRAGON_MOSS` | `1` | `0` hard-disables everything |
| `PRAGON_MOSS_DB` | `<root>/pragon_moss.db` | sqlite store |
| `PRAGON_MOSS_EMBED` | `auto` | `auto`/`local`/`gemini`/`hashing` |
| `PRAGON_MOSS_TOPK` | `5` | hits per retrieval |
| `PRAGON_MOSS_TIMEOUT` | `2.5` | per-call budget, seconds |
| `PRAGON_MOSS_HALFLIFE` | `86400` | recency half-life, seconds |
| `PRAGON_MOSS_SESSION` | `pragon` | session-id prefix |
| `PRAGON_MOSS_GUARDRAIL` | `warn` | `off`/`warn`/`enforce` |
| `PRAGON_MOSS_RAG` | `1` | append Moss hits to `_rag_query` |
| `PRAGON_MOSS_DEBUG` | `0` | bridge diagnostics on stdout |

`auto` prefers PRAGON's real `consciousness.embeddings.EmbeddingBackend`
(sentence-transformers, or Gemini via your existing key rotation) and falls back
to Moss's hashing vectorizer when that isn't installed — so it works on a clean
machine and gets better on a configured one, with no code change.

---

## Failure behaviour

The bridge owns one background event loop on one daemon thread and hands
coroutines to it with a hard timeout. Every method degrades to a safe default —
empty list, unchanged prompt, `None` — if Moss is disabled, missing, slow or
broken. The only method that doesn't quietly degrade is `guardrail()`, which
fails to `BLOCK` by design.

Put plainly: if Moss dies mid-session, PRAGON keeps working exactly as it does
today. If you set `PRAGON_MOSS=0`, no Moss code runs on any hot path at all.

---

## Optional HTTP service

```bash
python -m pragon_moss.service --port 8090
```

Serves Moss's own `/context/write`, `/context/search`, `/context/evaluate`,
`/context/guardrail`, `/context/session/{id}` and `/metrics`, plus
`/pragon/status` and `/pragon/session/{channel}` for the dashboard. It's a
separate port next to ragsystem/phoneview/customforge — it proxies nothing.

---

## Backing out

```python
from pragon_moss import uninstall
uninstall()          # every original callable restored
```

Or just run `python pragon_main.py` and delete the added files. There is nothing
to un-edit.

---

## One optional inline hook

If you'd rather not use the launcher, adding these three lines at the top of
`main()` in `pragon_main.py` gets the same result — but they're additions you'd
be making, not changes I made:

```python
try:
    from pragon_moss import install; install()
except Exception as e:
    print(f"[moss] skipped: {e}")
```
