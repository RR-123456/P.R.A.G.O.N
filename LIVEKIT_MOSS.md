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

## Two things that were broken, and what was done

**1. Moss was never actually called.** The stock `livekit_agent.py` has this:

```python
# moss_context = moss.search(last_msg)
moss_context = "Placeholder: MOSS retrieved context."
```

Every voice turn was prefixed with that literal string. `livekit_agent_moss.py`
replaces the placeholder with a real retrieval against the same Moss store
FRIDAY, GHOST and the JARVIS tool path use, on a `livekit` channel — so
something said by voice is retrievable in FRIDAY, and vice versa.

**2. The stock agent can't run on current livekit-agents.** Verified here
against the version `pip` installs today, **1.8.2**:

```
import livekit_agent  ->  ModuleNotFoundError: No module named 'livekit.agents.pipeline'
```

1.x removed `VoicePipelineAgent` and `before_llm_cb` in favour of
`AgentSession` + `Agent.on_user_turn_completed`. `livekit_agent_moss.py`
detects which generation is installed and builds the matching pipeline, so it
works on 1.x today and still works if you pin an older 0.x.

Two smaller fixes in the new file: `ctx.connect()` runs before the session
starts, and the greeting waits for a participant to actually be in the room
rather than sleeping one second and talking to nobody.

## Verified working

```
✓ api/api_keys.json parses
✓ livekit_url / api_key / api_secret set
✓ token mints · signature verifies · grant has roomJoin · issuer matches
✓ DNS resolves pragon-mobile-rkzn6o4i.livekit.cloud
✓ TLS connect :443 — TLSv1.3
✓ moss bridge enabled · voice hook injects context · question preserved
```

Real round-trip through a genuine livekit-agents 1.8.2 `ChatMessage`:

```
BEFORE: was the fan confirmed working?

AFTER:
[MOSS SESSION CONTEXT — 1 item(s) · retrieved in 0.539 ms]
  - (IMPORTANT/tech) The X700 unit's condenser fan was confirmed operational.
[END MOSS SESSION CONTEXT]

was the fan confirmed working?
```

## Two things you must do on your machine

1. **Set a real `gemini_api_key`** in `api/api_keys.json`. It's a placeholder
   right now. The UI's token endpoint doesn't need it, but the voice agent
   does — STT, LLM and TTS all go through Gemini.
2. **Install the packages**: `pip install livekit-agents livekit-plugins-google
   livekit-plugins-silero`. They're in `requirements.txt` but weren't installed.

## Latency

Moss sits between the user finishing a sentence and the LLM starting to answer,
so the voice path gets its own tighter budget, separate from the global one:

| Variable | Default | Meaning |
|---|---|---|
| `PRAGON_MOSS_LK_TIMEOUT` | `0.35` | retrieval budget, seconds |
| `PRAGON_MOSS_LK_TOPK` | `4` | context items injected per turn |
| `PRAGON_MOSS` | `1` | `0` disables Moss on voice too |

A miss, a timeout or a dead engine returns the prompt unchanged. Voice never
blocks on Moss.

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
