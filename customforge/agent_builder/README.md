# Agent Builder

A standalone, visual workflow builder for the four automation tools:
`file_controller`, `file_processor`, `dev_agent`, and `code_helper`, plus a
generic Gemini "Agent" step. Build a graph on the canvas, hit **Run**, and it
actually executes against your real filesystem / Gemini account — this is not
a mockup.

## 1. Install

```bash
cd agent_builder
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Only install `requirements-optional.txt` entries you actually need — they
back specific `file_processor` actions (images, PDFs, audio/video, etc.) and
`code_helper`'s screen-debug action. Everything else works without them.

## 2. Add your Gemini API key

```bash
cp config/api_keys.json.example config/api_keys.json
```

Edit `config/api_keys.json` and paste in your key:

```json
{ "gemini_api_key": "your-real-key-here" }
```

Any node that doesn't call Gemini (e.g. a `file_controller` "list" or
"organize_desktop" tool node) works fine without a key — you'll only see an
error if a specific run step needs one and it's missing.

## 2b. Runs on Ollama by default, powered by Gemini as backup

Agent Builder is Ollama-first: every agent/tool step runs on a free, local,
unlimited model by default, with Gemini only kicking in as backup power if
Ollama isn't installed, isn't running, or errors out. That means it keeps
working even when Gemini's cloud quota is exhausted, and never depends on
an API key at all if you don't want it to.

Set up the local engine:

```bash
# 1. install Ollama (https://ollama.com), then:
ollama serve
ollama pull qwen2.5-coder:3b     # or llama3.2, mistral, etc.
```

Model field options on any node:

- **`auto` (default):** Ollama first; automatically falls back to Gemini
  as backup power if Ollama can't handle the step.
- **`ollama:<model>`**, e.g. `ollama:qwen2.5-coder:3b`: forces the local
  model only, no Gemini involved at all.
- **`gemini-2.5-flash`** (or any Gemini model id): forces the cloud model
  only, skipping Ollama entirely.

Optionally set a non-default host/model in `agent_builder/config/api_keys.json`:

```json
{ "ollama_host": "http://localhost:11434", "ollama_model": "qwen2.5-coder:3b" }
```

The **API Key** settings panel in the canvas UI shows whether Ollama is
currently reachable.

## 3. Run it

```bash
python3 server.py
```

This starts a local server at `http://127.0.0.1:5057` and opens it in your
browser automatically.

## Using the canvas

- **+ Add node** or the bottom chip bar adds a node; drag from a node's right
  dot to another node's left dot to connect them.
- **Click a node** to open its config panel on the right (Node tab) — pick a
  tool, write a prompt, set a condition expression, etc.
- **▶ Run** opens the Run tab: type an optional input (fed to every Trigger
  node), click **Run workflow**, and watch each node light up green/red with
  its real result as the graph executes top to bottom.
- **Save / load** workflows by name from the Run tab — they're stored as JSON
  under `workflows/`.
- The left tool rail (cursor / hand / pencil / note / image) is for canvas
  navigation and freeform sticky notes — it doesn't affect execution.
- **Preview / Dashboard** toggle switches between the graph view and a
  read-only card summary of every node.

## Node types

| Type      | What it does when run |
|-----------|------------------------|
| Trigger   | Supplies the starting text (from its own "initial input", or the Run tab's input box if you typed one) |
| Agent     | **One** LLM call: sends `prompt + upstream output` to Gemini and returns the reply. No memory, no tools, no autonomy. |
| Auto-Agent | **Genuinely autonomous.** Give it a goal and pick which real tools it's allowed to use. It reasons step by step (ReAct pattern): decides which tool to call, actually runs it, reads the real result, and decides the next step itself — repeating until it gives a Final Answer or hits its step limit. Click the node to see/edit its allowed tools and max steps. |
| Tool      | Calls one of `file_controller` / `file_processor` / `dev_agent` / `code_helper` with the fields you configured — deterministic, no reasoning |
| Condition | Evaluates a small boolean expression against the incoming text (`output`); if false, nothing downstream of it runs |
| Output    | Just displays whatever text reaches it — the end of a branch |

**Agent vs. Auto-Agent, honestly:** "Agent" is really just a single prompt-in/text-out call — useful for drafting, summarizing, or transforming text mid-pipeline. "Auto-Agent" is the one that's actually agentic: it decides on its own which tools to call and when to stop, based on what it observes. If you want something to "figure out" a multi-step task on its own, use Auto-Agent, not Agent.

Separately, `dev_agent` (one of the Tool options) is also genuinely autonomous on its own — it plans a project, writes files, installs deps, runs the code, and self-corrects in a loop, independent of whether you're using it from a Tool node or an Auto-Agent node.

### Condition expressions

You can use: comparisons (`==`, `!=`, `<`, `>`, ...), `and` / `or` / `not`,
`"x" in output`, `len(output)`, and `output.startswith(...)` /
`.endswith(...)` / `.lower()` / `.upper()` / `.strip()`. Nothing else is
evaluated — this is a small whitelisted interpreter, not Python's `eval()`.

## Safety notes (same as the original tools)

- `file_controller` restricts every path to inside your home folder and
  sends deletions to the Trash (via `send2trash`) rather than deleting
  permanently. Protected folders (Desktop, Downloads, Documents, etc.)
  can't be deleted themselves.
- `dev_agent` and `code_helper`'s `run`/`build` actions execute code via
  `subprocess` (with a timeout) and can install pip packages — only run
  workflows you wrote or trust.
- API keys live in `config/api_keys.json`, which is not checked into
  anything here — keep it out of version control.

## Project layout

```
agent_builder/
  server.py                 Flask app: serves the UI, runs workflows, saves/loads them
  engine.py                 Executes a graph (topological order, condition gating)
  tools/
    file_controller.py      (unmodified)
    file_processor.py       (unmodified)
    dev_agent.py             (unmodified)
    code_helper.py           (unmodified)
    gemini_agent.py          new: generic LLM step for "Agent" nodes
    registry.py               new: maps node config -> real function + form fields
  static/index.html          the canvas UI
  workflows/                  saved graphs (created at first save)
  config/api_keys.json.example
```
