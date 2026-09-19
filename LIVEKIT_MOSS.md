# LiveKit × MOSS — voice path

Your credentials are now in `api/api_keys.json` (already gitignored). The
original `livekit_agent.py`, `livekit_client.py` and
`start_livekit_assistant.bat` are untouched.

## Start it

```bash
python start_livekit_moss.py        # preflight, then run the agent in dev mode
python start_livekit_moss.py --check   # preflight only
```

Then open the PRAGON UI and click the **LiveKit** pill in the input bar. The UI
mints a room token via `GET /livekit/token` and the browser joins the same room
this worker is serving.

# MOSS — Innovations & Efficiency Gains: What Can Be Done Next

*Roadmap ideas for MOSS, the realtime context engine, beyond its current implementation*

MOSS today is single-file, stdlib-first, sqlite-backed, with a deterministic hashing-vectorizer fallback and measured (not promised) latency traces. Everything below is a direction to push that further — organized by whether it's a **capability innovation** (does something new) or an **efficiency gain** (does the same thing faster/cheaper).

---

## Role of MOSS in the System

MOSS is the **context and retrieval layer** that sits underneath PRAGON's conversational AI — not the assistant itself, and not a chatbot. Its job is narrow and specific: give any calling agent fast, reliable access to the right context at the right moment. Concretely, that role breaks down into four responsibilities:

- **Realtime context retrieval** — answering "what's relevant right now" in milliseconds, measured directly via `LatencyTrace` rather than assumed.
- **Session memory across turns and restarts** — every user message and reply is written to a durable store, then re-injected as context on the next call, so the assistant that sits on top of MOSS doesn't have to solve continuity itself.
- **Shared context and multi-agent state** — because storage/embedding details are isolated behind `ContextStore.write` / `ContextRetriever.retrieve`, multiple agents or processors (voice, chat, tool-execution) can read and write the same context without knowing anything about sqlite or embeddings underneath.
- **Retrieval-grounded guardrails** — before a tool call executes, MOSS checks it against retrieved policy/safety context and fails safe (BLOCK, never a silent ALLOW) if retrieval itself fails, so safety doesn't depend on the caller remembering to check.

The deliberate design choice is that MOSS **wraps** existing behavior rather than replacing it — it was integrated into PRAGON with zero modifications to the original code, layering memory and safety around callables instead of rewriting them. In short: MOSS is the plumbing that makes a conversational agent feel continuous, fast, and safe, while staying invisible to both the end user and, mostly, to the code calling it.

---

## Capability Innovations

**1. Tiered Memory (Hot / Warm / Cold)**
Right now all session context lives in one sqlite store. A tiered model — hot context in-process memory, warm context in sqlite, cold context compacted/summarized into long-term storage — would let MOSS keep recent turns instant while still retaining months of history without bloating the working set.

**2. Automatic Session Summarization**
As a session grows, periodically compress older turns into a short summary block instead of retaining every raw exchange. This bounds retrieval size and keeps latency flat even in very long-running conversations, at the cost of some fidelity on old detail — worth exposing as a tunable.

**3. Cross-Device Context Sync**
MOSS session state currently lives per-instance. A lightweight sync protocol (even a simple signed-diff push over WebSocket) would let a worker's phone, desktop, and wearable share one continuous session instead of three isolated ones.

**4. Federated / On-Device Embedding Updates**
Where the real embedding backend (sentence-transformers or Gemini) is available, allow it to fine-tune or adapt embeddings on-device from a worker's own corrected retrievals, without shipping raw data anywhere — pairs naturally with the local-first design.

**5. Confidence-Scored Retrieval**
Extend `LatencyTrace` with a retrieval-confidence score alongside timing, so callers (and the guardrail logic) can distinguish "fast and confident" from "fast but weak match" — currently guardrails only fail safe on retrieval *failure*, not on low-confidence success.

**6. Pluggable Vectorizer Strategy**
Formalize the hashing-vectorizer fallback and the real embedding backend as swappable strategies behind one interface, so a third option (e.g., a small local ONNX model) can be dropped in without touching callers — useful for hardware with a GPU/NPU but no network.

**7. Event-Driven Guardrail Policy Updates**
Currently guardrail policy context is populated via `moss_remember`, HTTP API, or `moss.write`. A push-based policy update channel (company pushes new safety data centrally) would let `enforce` mode stay current without a worker's local database going stale.

---

## Efficiency Gains

**8. Write Batching / Debounced Persistence**
If MOSS is writing every turn to sqlite synchronously, batching writes (e.g., every N turns or M milliseconds) trades a small durability window for a meaningful reduction in disk I/O under high-frequency voice interaction.

**9. In-Memory LRU Cache Ahead of Sqlite**
A small in-process cache of the most recently retrieved context blocks would let repeat or near-repeat queries in a tight conversational loop skip the sqlite read path entirely — a cheap win given how much of a conversation reuses very recent context.

**10. Quantized Local Embeddings**
Where the real embedding backend is used, storing quantized (int8) vectors instead of full-precision floats cuts storage and comparison cost with minimal recall loss — meaningful on constrained field devices.

**11. Lazy Index Construction**
Build/refresh the semantic search index incrementally on write rather than recomputing on each retrieval call, especially important as session history or company knowledge base size grows.

**12. Compiled Hashing-Vectorizer Path**
The zero-dependency fallback is pure Python by design for portability; a compiled (Cython/Rust extension) fast path — optional, falling back to pure Python when unavailable — would keep the "zero required dependencies" guarantee while giving low-end hardware a speed option.

**13. Measured Regression Benchmarking in CI**
`moss.py benchmark --n 500` already reports real p50/p95/p99 numbers. Wiring that into CI as a regression gate (fail the build if p95 latency regresses beyond a threshold) turns "measured, not promised" into an enforced guarantee over time, not just a one-off number.

---

## Why this framing matters for judges

MOSS's stated value proposition is instant, dependency-light retrieval. Every item above either **extends what MOSS can retrieve/remember** without adding infrastructure, or **makes the existing retrieval path cheaper** without changing its interface — both directions stay consistent with the "no vector database, runs anywhere" positioning rather than working against it.

Measured locally: retrieval came back in **0.5 ms** on the hashing backend with
a small corpus. Your real numbers depend on backend and corpus size — measure
with `python moss.py benchmark --n 500`, and note that `sentence-transformers`
(what `auto` picks when installed) is slower per call but semantically better.

## A note on those credentials

You said you're rotating them, which is the right call — they came through a
chat message. When you do, update `api/api_keys.json` and nothing else; both
the agent and the UI token endpoint read that one file. You can also override
with `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` env vars, which
take precedence.
