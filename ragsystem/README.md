# Pragon Knowledge Base (ragsystem)

Standalone offline RAG service that `pragon_main.py` auto-starts (or you can
run manually). Runs fully offline: embeddings via `sentence-transformers`,
storage via a local persistent ChromaDB folder (`./chroma_store`). If a
local [Ollama](https://ollama.com) server is running, `/query` uses it to
generate a real answer from the retrieved passages; if not, it gracefully
falls back to returning the raw retrieved context instead of failing.

## Setup

```bash
cd ragsystem
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

Pragon's main process prefers this `venv/` automatically if it exists next
to this folder (see `pragon_main.py`'s `_rag_python_executable()`), so its
dependencies stay isolated from the main Pragon environment.

## Optional: local LLM answers via Ollama

```bash
# https://ollama.com/download
ollama pull llama3
ollama serve
```

Without this running, `/query` still works -- it just returns the raw
matched passages instead of a synthesized answer.

## Running manually

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Normally you don't need to do this yourself -- `pragon_main.py` starts it
automatically on launch if it isn't already running.

## Endpoints

- `GET /health` -> `{"status": "ok"}`
- `POST /upload` (multipart file) -> ingest + chunk + embed a document
- `POST /query` `{"question": "...", "top_k": 5}` -> retrieved answer + sources
- `GET /documents` -> list of ingested filenames
