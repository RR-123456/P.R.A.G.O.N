# P.R.A.G.O.N + MOSS — Round 2 changes

Addresses the three review gaps (shared-database anti-pattern, incomplete
API security, no data-access boundary) and adds three new capabilities
(action rationale ledger, dry-run/preview for risky actions, signed
skill-pack sharing).

## 1. Shared-database anti-pattern → unified Data Access Layer

**New:** `consciousness/data_access.py` — a process-wide registry (`dal`)
that caches one SQLite connection and one ChromaDB client per resolved
path. Any two components asking for the same file now get the *same*
handle instead of opening independent ones.

**Routed through it:**
- `consciousness/sqlite_memory.py`
- `consciousness/unified_memory.py` (`_Timeline` + both Chroma clients)
- `consciousness/rag_engine.py`

**Left alone, on purpose:**
- `moss.py`'s thread-local connection pool — legitimate per-thread pooling
  for its own exclusive DB, not the cross-service collision under review.
- `ragsystem/`'s Chroma store — a genuinely separate microservice with its
  own isolated directory; that's correct isolation, not a violation.

This is also the seam a real HTTP boundary slots into later (splitting
Orchestrator/MOSS/RAG into actual separate processes) without touching
every caller again — see the docstring in `data_access.py`.

## 2. API security

**New:** `api/security.py` — per-install JWT secret (`~/.pragon/jwt_secret`),
`issue_token()` / `verify_token()`, a FastAPI dependency (`require_auth_dep`),
a Flask decorator/helper (`require_auth_flask` / inline `verify_token` use),
and `ensure_tls_cert()` (self-signed cert generator, or bring-your-own via
`PRAGON_TLS_CERT` / `PRAGON_TLS_KEY`).

**Applied:**
- `ragsystem/app/main.py` — every route but `/health` requires a bearer
  token; `QueryRequest` now has length/range bounds (`question`, `top_k`).
  Module docstring documents the TLS launch command.
- `moss.py`'s FastAPI surface (`/context/*`, `/metrics`) — same auth
  requirement, plus request-field bounds; `serve` mode now requests a TLS
  cert via `ensure_tls_cert()` before calling `uvicorn.run`.
- `customforge/server.py` and `customforge/agent_builder/server.py`
  (Flask) — a `before_request` hook requires a bearer token on every
  POST/PUT/DELETE (code generation/execution, config writes, workflow
  writes/deletes); GET routes serving the local UI stay open since these
  are 127.0.0.1-bound asset servers.

**Already solid, no changes needed:** `pragon_phoneview/server.py` already
has its own pairing-based auth and TLS support built in.

## 3. Action rationale ledger

**New:** `features/agentic_feature/action_ledger.py` — every tool
invocation gets one row (tool, parameters, plain-English rationale, risk
tier, outcome, timestamp), stored through the same `dal` from #1. Answers
"what did you do today?" with a real audit trail instead of terminal
scrollback.

**Wired in:** `features/agentic_feature/agent/executor.py`'s `_call_tool`
was the single choke point every agentic action already passed through;
added `_guarded_call_tool` around it so every dispatch (including
recovery/fix attempts) is logged without touching the 15+ individual
action modules.

## 4. Dry-run / preview before risky actions

**New:** `features/agentic_feature/dry_run.py` — classifies a (tool,
parameters) pair into low/medium/high risk and, for medium/high, builds a
one-line preview of the concrete effect ("This will permanently delete:
Downloads/report.docx") *before* execution. Low-risk actions keep PRAGON's
existing "act now, `pragoncore/undo.py` can reverse it" behavior unchanged.

**Wired in:** same `_guarded_call_tool` in `executor.py`. If `execute()` is
given a `confirm` callback (e.g. `ConfirmationGate.ask`), the user sees the
preview and can decline before anything runs (raises `ActionCancelled`,
handled as a clean per-step cancellation, not an error). Without a
`confirm` callback, the preview is spoken as a heads-up and still logged —
unattended runs stay auditable even when nothing can ask permission.

## 5. Opt-in signed skill-pack sharing

**New:** `customforge/agent_builder/skill_pack.py` — exports a saved
workflow (nodes/edges only, no user data) as a `.pragonskill` zip signed
with a per-install Ed25519 key (`~/.pragon/skill_signing/`). Import
re-verifies the signature against the bundled public key before anything
is written to disk — a trust-on-first-use model (like SSH host keys), no
central server, no account system.

**New routes** in `customforge/agent_builder/server.py`:
- `POST /api/skill-pack/export/<name>` → builds the pack, returns a
  download link
- `POST /api/skill-pack/verify` → inspect a pack's manifest + signer
  fingerprint with zero side effects
- `POST /api/skill-pack/import` → re-verifies and writes the workflow
  under a user-confirmed name — separate from `/verify` on purpose, so
  inspecting a pack never has a side effect

## Requirements

Added `PyJWT` to `requirements.txt` (`cryptography` was already present
and covers both the TLS cert generation and the skill-pack signing).

## What's not done

- The Flask `before_request` hooks protect *state-changing* routes; a
  full per-route allowlist review (e.g. should `/api/templates` GET stay
  open) is worth a pass before a real multi-user deployment.
- `dry_run.py`'s risk classifier is heuristic (tool name + action string).
  It's deliberately conservative (fails to "low risk" on anything it
  doesn't recognise) rather than trying to be exhaustive.
- No automated test suite was added for these modules — the smoke tests
  run during development (DAL connection caching, JWT round-trip, dry-run
  classification, ledger writes, skill-pack sign/verify/tamper-detection)
  all passed, but there's no `pytest` coverage checked in yet.
