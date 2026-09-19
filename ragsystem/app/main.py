"""
ragsystem/app/main.py -- Pragon's standalone offline knowledge-base service.

Implements the exact contract pragon_main.py expects (see its
_ensure_rag_service / knowledge_base helpers):

  GET  /health                -> {"status": "ok"}
  POST /upload  (multipart)   -> ingest a file (txt/md/pdf/docx), chunk +
                                  embed it, store in a local Chroma DB
  POST /query   {"question","top_k"} -> retrieve relevant chunks and, if a
                                  local Ollama server is reachable, ask it
                                  to answer using that context. If Ollama
                                  isn't running, falls back to returning the
                                  raw retrieved passages so the feature still
                                  degrades gracefully instead of failing.
  GET  /documents             -> list distinct source filenames ingested

Runs fully offline: embeddings are computed locally via
sentence-transformers, and storage is a local persistent ChromaDB directory
(./chroma_store next to this file). Ollama is optional -- only used for the
final answer-generation step in /query.

Start it the same way pragon_main.py launches it, now with TLS 1.3 and a
bearer token required on every route except /health (see api/security.py):
    python -c "from api.security import ensure_tls_cert; print(ensure_tls_cert())"
    uvicorn app.main:app --host 0.0.0.0 --port 8000 \
        --ssl-certfile <cert> --ssl-keyfile <key>
(run from inside the ragsystem/ folder, with this folder's requirements.txt
installed -- ideally into its own venv, see README.md. If api.security
isn't importable from that venv, the service still runs but logs a warning
and falls back to no auth -- fine for local dev, not for anything
LAN-exposed.)

Every request other than GET /health needs:
    Authorization: Bearer <token from api.security.issue_token(...)>
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import List, Optional

import requests
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
try:
    from api.security import require_auth_dep
except ImportError:
    require_auth_dep = None
    print("[ragsystem] WARNING: api.security not importable -- running "
          "without auth. Run from within the PRAGON_MOSS repo to enable it.")

HERE = Path(__file__).resolve().parent.parent
STORE_DIR = HERE / "chroma_store"
STORE_DIR.mkdir(exist_ok=True)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")
EMBED_MODEL_NAME = os.environ.get("RAG_EMBED_MODEL", "all-MiniLM-L6-v2")
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

app = FastAPI(title="Pragon Knowledge Base", version="1.0")

# Every route except /health requires a bearer token (see api/security.py).
# /health stays open so process-liveness checks don't need a token.
_AUTH = [Depends(require_auth_dep)] if require_auth_dep else []

# -- Lazy globals: heavy imports (chromadb, sentence-transformers) happen on
# first use, not at module import time, so `--reload` / quick health checks
# during startup don't pay the model-load cost every time.
_chroma_client = None
_collection = None
_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(EMBED_MODEL_NAME)
    return _embedder


def _get_collection():
    global _chroma_client, _collection
    if _collection is None:
        import chromadb
        _chroma_client = chromadb.PersistentClient(path=str(STORE_DIR))
        _collection = _chroma_client.get_or_create_collection("pragon_knowledge_base")
    return _collection


# -- Text extraction -------------------------------------------------------

def _extract_text(filename: str, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in (".txt", ".md", ".csv", ".log"):
        return raw.decode("utf-8", errors="ignore")
    if suffix == ".pdf":
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        import io
        from docx import Document
        doc = Document(io.BytesIO(raw))
        return "\n".join(p.text for p in doc.paragraphs)
    # Unknown type: best-effort decode rather than failing the whole upload.
    return raw.decode("utf-8", errors="ignore")


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start = end - overlap
        if start <= 0:
            break
    return [c.strip() for c in chunks if c.strip()]


# -- Routes -----------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload", dependencies=_AUTH)
async def upload(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")

    text = _extract_text(file.filename, raw)
    chunks = _chunk_text(text)
    if not chunks:
        raise HTTPException(400, f"Couldn't extract any text from '{file.filename}'")

    collection = _get_collection()
    embedder = _get_embedder()
    embeddings = embedder.encode(chunks).tolist()

    ids = [f"{file.filename}-{uuid.uuid4().hex[:8]}-{i}" for i in range(len(chunks))]
    metadatas = [{"source": file.filename, "chunk_index": i} for i in range(len(chunks))]

    collection.add(ids=ids, embeddings=embeddings, documents=chunks, metadatas=metadatas)

    return {"ok": True, "filename": file.filename, "chunks_ingested": len(chunks)}


class QueryRequest(BaseModel):
    # Input-validation gap flagged in review: no length/range bounds meant
    # an oversized question or an absurd top_k could be sent straight
    # through to the embedder / Chroma query.
    question: str = Field(..., min_length=1, max_length=2000)
    top_k: Optional[int] = Field(default=5, ge=1, le=50)


@app.post("/query", dependencies=_AUTH)
def query(req: QueryRequest):
    collection = _get_collection()
    if collection.count() == 0:
        return {"answer": "The knowledge base is empty -- nothing has been ingested yet.", "sources": []}

    embedder = _get_embedder()
    q_embedding = embedder.encode([req.question]).tolist()
    results = collection.query(query_embeddings=q_embedding, n_results=max(1, req.top_k or 5))

    docs = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    sources = sorted({m.get("source", "unknown") for m in metadatas})

    if not docs:
        return {"answer": "No relevant passages found.", "sources": []}

    context = "\n\n---\n\n".join(docs)
    answer = _generate_answer(req.question, context)
    return {"answer": answer, "sources": sources}


@app.get("/documents", dependencies=_AUTH)
def documents():
    collection = _get_collection()
    if collection.count() == 0:
        return {"documents": []}
    all_meta = collection.get(include=["metadatas"])["metadatas"]
    names = sorted({m.get("source", "unknown") for m in all_meta})
    return {"documents": names}


# -- Answer generation (optional Ollama step) --------------------------------

def _ollama_reachable() -> bool:
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=1.5)
        return r.ok
    except Exception:
        return False


def _generate_answer(question: str, context: str) -> str:
    if not _ollama_reachable():
        # Graceful degradation: no local LLM available, so hand back the
        # raw retrieved context instead of failing the request.
        return (
            "Ollama isn't running, so this is the raw retrieved context "
            "rather than a generated answer:\n\n" + context[:2000]
        )
    try:
        prompt = (
            "Answer the question using only the context below. "
            "If the context doesn't contain the answer, say so.\n\n"
            f"Context:\n{context}\n\nQuestion: {question}\nAnswer:"
        )
        r = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("response", "").strip() or "Ollama returned an empty response."
    except Exception as e:
        return f"Retrieved context, but Ollama generation failed ({e}). Raw context:\n\n{context[:2000]}"
