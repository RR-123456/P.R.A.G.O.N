# PRAGON — C.U.S.T.O.M FORGE

A drawing-pad app builder. Sketch a layout (icons, boxes, background regions,
positions), pick a type (**Website / Frontend / Game**), type a brief, hit
**FORGE**, and get back a full working single-file app — previewed live and
saved to `forgeoutput/`.

## What changed from the original UI

The UI (`templates/index.html`) used to call `https://api.anthropic.com/v1/messages`
straight from the browser. That can't actually work — there's no safe place
to put an API key in client-side JS, and browsers can't call that endpoint
directly. This package adds the missing piece: **`server.py`**, a small Flask
bridge that:

1. Receives your prompt, the chosen type, and the full sketch structure
   (every drawn element's name, type, and position, plus the "Icons /
   Boxes / BG Template" lists from the sidebar) as JSON, along with a
   PNG snapshot of the canvas.
2. Builds one detailed generation prompt from all of that and sends it —
   text + image — to Gemini (`gemini-3.5-flash`, multimodal), falling
   back to Groq and then a local Ollama model if Gemini/Groq are both
   unavailable (see "Optional: Groq fallback" and "Third tier: local
   fallback via Ollama" below).
3. Automatically continues the generation if the model gets cut off by
   the token limit, stitching the pieces into one file.
4. Runs a safety check on the returned HTML (blocks `eval`, filesystem/
   process access, etc. — this is going straight into a browser tab).
5. Saves the result to `./forgeoutput/<slug>.html` and returns the code
   to the page for the live preview / code view / download / copy buttons.

### Live streaming

Hitting **FORGE** calls `/api/forge/stream` instead of the older one-shot
`/api/forge` endpoint (still present, unused by the UI). It's a
Server-Sent-Events response: as the model writes the file, each piece of
text is pushed to the browser and appended straight into the Code view in
real time — you watch the HTML get typed out instead of staring at a
spinner until the whole thing lands at once. The Code tab opens
automatically when you hit FORGE for this reason; it flips back to Preview
once the file is complete.

A status bar above the code shows what's happening: which provider is
currently writing (`GEMINI — WRITING…`), when a fallback kicks in
(`SWITCHING TO GROQ…` — the partial output from the failed provider is
discarded first, never spliced with the next model's output), and the
post-generation validate/fix and image-repair passes (`VALIDATING…`,
`CHECKING IMAGES…`). Those two passes rewrite the file server-side rather
than streaming token-by-token, so the code view snaps to the corrected
version in one step when they run.

## Agent Builder (bundled)

`agent_builder/` is a second, self-contained app bundled inside this
package: a visual canvas for wiring up tool/agent nodes (file control,
file processing, dev/code helper agents, a generic Gemini "agent" step,
and a meta "agent builder" tool) into runnable workflows, with
save/load/run all backed by its own small Flask server.

Running `python main.py` starts **both**: FORGE on `http://127.0.0.1:5000`
and Agent Builder alongside it on `http://127.0.0.1:5057`, as two
independent Flask processes (Agent Builder doesn't share any routes,
state, or code with FORGE's `server.py` — it's launched as a subprocess).
Open Agent Builder either directly at `:5057`, or from FORGE's topbar via
the **🤖 Agent Builder** button, which opens it in a new tab.

If FORGE's `config/api_keys.json` already has a `gemini_api_key` set,
`main.py` copies it into `agent_builder/config/api_keys.json`
automatically on first run (only if Agent Builder doesn't already have
its own key configured) so you don't have to enter it twice. Agent
Builder's per-tool optional extras (image/PDF/DOCX/CSV/PPTX processing,
etc.) are a separate install — see `agent_builder/requirements-optional.txt`
and `agent_builder/README.md` for details on what each one unlocks.

### Export Agent

The **Build Agent** node generates a complete standalone `.py` agent file
(its own ReAct loop, tool imports, memory). It always saves that file to
`agent_builder/exports/` (in addition to its usual Desktop/output_path
save), and running the node surfaces a green **⬇ Export Agent (.py)**
button in the run log — click it to download the file straight to your
browser. This exists because the Desktop/output_path save happens on
whatever machine is running the server, which the browser using the
canvas may have no filesystem access to at all (a remote server, a
container); the export button works regardless of where the server runs.
`GET /api/export-agent` lists everything exported so far, and
`GET /api/export-agent/<filename>` downloads one directly.

Want Agent Builder on its own, without FORGE? `python agent_builder/server.py`
runs it standalone exactly as before, on the same port, with its own
auto-open browser tab.

Running `python server.py` directly (instead of `main.py`) starts FORGE
only, with no Agent Builder process — useful for a headless FORGE-only
deployment.

## Setup

```bash
pip install -r requirements.txt
```

Edit `config/api_keys.json` and paste in your Gemini API key:

```json
{ "gemini_api_key": "YOUR_KEY_HERE" }
```

(Get a key at https://aistudio.google.com/apikey — this is a Google
Gemini key, not an Anthropic one; the model doing the actual generation
here is Gemini.)

### Optional: Groq fallback

Gemini's free tier caps out at a small number of requests per day. If you
add a Groq key too, the server automatically falls back to Groq whenever
Gemini returns a quota/rate-limit error — any other Gemini error (bad key,
safety block) still surfaces normally instead of silently switching
providers.

```json
{
  "gemini_api_key": "YOUR_GEMINI_KEY_HERE",
  "groq_api_key": "YOUR_GROQ_KEY_HERE"
}
```

Get a free Groq key at https://console.groq.com/keys. Nothing else to
configure — `/api/health` reports `groq_fallback_configured` so you can
confirm it's picked up. Groq's model lineup changes often; if the
fallback starts failing with a "model decommissioned" error, check
https://console.groq.com/docs/deprecations and update the model names
near the top of `run_completion_groq` in `server.py`.

### Third tier: local fallback via Ollama (free, no rate limits)

If Gemini *and* Groq are both unavailable (quota out, no key, network
hiccup — anything), the server falls back one more time to a local model
running through [Ollama](https://ollama.com), so FORGE keeps working with
no API key and no rate limit at all — just the speed of your CPU.

1. Install Ollama and pull a coder model:

   ```bash
   ollama pull qwen2.5-coder:3b
   ```

2. Make sure `ollama serve` is running (it usually starts automatically
   after install, listening on `http://localhost:11434`).

That's it — no config file changes needed, the server points at
`qwen2.5-coder:3b` on `localhost:11434` by default. `/api/health` reports
`ollama_fallback_model` and `ollama_fallback_reachable` so you can confirm
it's picked up.

> **Fixed:** earlier versions of `server.py` didn't set Ollama's `num_ctx`,
> so it silently defaulted to 2048 tokens — on a heavy brief (style
> template + several animations + a feature checklist) the actual prompt
> was getting truncated before the local model ever saw most of it, which
> is what produced near-empty output instead of anything resembling the
> brief. `run_completion_ollama()` now sizes `num_ctx` to the prompt
> itself. Even with that fixed, this tier is still the weakest of the
> three models — if you're leaning on it often, see the quota/fallback
> note below.

**Picking a model size** — this is a CPU-vs-quality trade-off, no GPU
assumed:

| Model | Speed on CPU | Quality | When to use |
|---|---|---|---|
| `qwen2.5-coder:1.5b` | fastest | weakest — may struggle to get a full single-file app right in one shot | slow/older machine, want quick iteration |
| `qwen2.5-coder:3b` (default) | moderate | good balance | most CPU-only machines |
| `qwen2.5-coder:7b` | slow — can be several minutes per response | noticeably better code | you have a fast CPU / many cores and don't mind the wait |
| `hermes3` (Nous Research) | comparable to the 3B/7B qwen sizes depending on which `hermes3` tag you pull | general-purpose, not coder-specialized — solid but not tuned for HTML/CSS/JS the way the qwen-coder line is | you already run Hermes elsewhere and want the same model here too |

To switch, pull the model you want and point the server at it in
`config/api_keys.json` (no key needed, just the model tag):

```json
{
  "gemini_api_key": "YOUR_GEMINI_KEY_HERE",
  "groq_api_key": "YOUR_GROQ_KEY_HERE",
  "ollama_model": "qwen2.5-coder:7b"
}
```

Or use the in-app **Settings → Ollama → Model** field — it has quick-pick
buttons for all three qwen-coder sizes plus Hermes 3, or you can type any
other Ollama model tag by hand.

You can also point at a non-default Ollama host (e.g. Ollama running on
another machine on your network) with `"ollama_host": "http://192.168.1.x:11434"`.

Note: the coder models are text-only (no vision), so at this tier the
sketch screenshot is dropped — the text brief and the structure notes
describing the sketch's layout are still sent, so it's not flying blind,
just without the image itself. Hermes 3 is text-only here too — same
image-dropping behavior applies.

### About "Hermes Agent"

[Hermes Agent](https://hermes-agent.nousresearch.com) (Nous Research's
`ollama launch hermes` CLI) is a separate terminal coding agent, not an
API — it's built to be driven interactively (editing files, running
shell commands, browsing) rather than called for one-shot completions
the way server.py calls Gemini/Groq/OpenRouter/Ollama. There's no HTTP
endpoint on the Hermes Agent side for this bridge to call into, so it
can't be wired in as a fifth provider tier the way Groq or OpenRouter
were. What Hermes Agent actually runs *on* — a model like `hermes3`
served through Ollama — is exactly what the local fallback tier above
already supports directly, which is the effect this section gives you
without needing the Hermes Agent CLI itself.

## Run

```bash
python main.py
```

This starts the bridge server and opens **http://127.0.0.1:5000** in your
browser automatically. (Prefer to start it without auto-opening a browser,
e.g. on a headless machine? Run `python server.py` instead — same server,
no auto-open.)

## Folder layout

```
pragon_custom_forge/
├── main.py                 ← run this (opens the browser for you)
├── server.py                ← the bridge server itself
├── requirements.txt
├── config/
│   └── api_keys.json       ← put your Gemini key here
├── templates/
│   └── index.html           ← the FORGE drawing-pad UI
└── forgeoutput/              ← every generated app is saved here as .html
```

## Using it

1. Pick **Website / Frontend / Game** at the top.
2. Sketch on the pad — pen, shapes, text, or drop named "Icon" / "Box" /
   "BG Template" items from the sidebar onto the canvas. Every shape you
   draw gets prompted for a name + number, which becomes part of the
   structure sent to the model (name, type, and approximate position).
3. Type a plain-English brief in the bottom bar (what the site/app/game
   should actually do).
4. Hit **FORGE**. The right panel shows a live preview; toggle **Code**
   to see the raw HTML, **Copy** / **Download** it, or **Open ↗** in a
   new tab. The same file is already sitting in `forgeoutput/` on disk.

## Notes

- Everything generated is a single self-contained `.html` file — no
  build step, no external dependencies beyond optional Google Fonts.
- The safety filter is intentionally strict about filesystem/process
  access and `eval` — this is designed to be opened directly in a
  browser, not run as a desktop app.
- If FORGE fails immediately with an API-key error, double check
  `config/api_keys.json`.
