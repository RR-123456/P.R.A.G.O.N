"""
consciousness/rag_engine.py
════════════════════════════════════════════════════════════════════════════
PRAGON — RAG Knowledge Engine

Adds a real document knowledge base on top of the existing UnifiedMemory:
  • Drop files into a "knowledge_base/" folder (txt, md, pdf, docx supported)
  • ingest_folder() chunks + embeds new/changed files into their own Chroma
    collection ("documents") — tracked via a manifest so unchanged files are
    skipped on subsequent runs
  • search_documents() does semantic search over those chunks
  • search_all() merges document hits + UnifiedMemory's remembered facts
    (recall) + past conversation turns (recall_chat) into a single ranked,
    de-duplicated, source-labeled context block ready to hand to the LLM —
    this is what the "knowledge_search" tool in pragon_main.py calls.

Install deps:
    pip install chromadb sentence-transformers
Optional, for richer document support:
    pip install pypdf python-docx
"""

from __future__ import annotations

import hashlib
import json
import warnings
from pathlib import Path
from typing import Optional, Callable, Any

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    _CHROMA_AVAILABLE = True
except ImportError:
    _CHROMA_AVAILABLE = False
    warnings.warn("[RAG] ChromaDB not installed. Document search disabled. "
                  "Run: pip install chromadb", stacklevel=2)

try:
    from .embeddings import EmbeddingBackend
except ImportError:
    from embeddings import EmbeddingBackend

try:
    from .data_access import dal
except ImportError:
    from data_access import dal

SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}
CHUNK_SIZE = 900          # characters per chunk
CHUNK_OVERLAP = 150       # characters of overlap between consecutive chunks
MANIFEST_NAME = "ingested_manifest.json"


# ── File readers ─────────────────────────────────────────────────────────────

def _read_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        warnings.warn("[RAG] pypdf not installed — skipping PDF: "
                      f"{path.name}. Run: pip install pypdf", stacklevel=2)
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        print(f"[RAG] Failed to read PDF {path.name}: {exc}")
        return ""


def _read_docx(path: Path) -> str:
    try:
        import docx
    except ImportError:
        warnings.warn("[RAG] python-docx not installed — skipping DOCX: "
                      f"{path.name}. Run: pip install python-docx", stacklevel=2)
        return ""
    try:
        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs)
    except Exception as exc:
        print(f"[RAG] Failed to read DOCX {path.name}: {exc}")
        return ""


_READERS: dict[str, Callable[[Path], str]] = {
    ".txt": _read_txt,
    ".md": _read_txt,
    ".pdf": _read_pdf,
    ".docx": _read_docx,
}


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Simple paragraph-aware sliding-window chunker."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    # Split on blank lines first to avoid cutting mid-paragraph where possible.
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                chunks.append(buf)
            if len(para) > size:
                # Paragraph itself too long — hard-slice with overlap.
                start = 0
                while start < len(para):
                    chunks.append(para[start:start + size])
                    start += size - overlap
                buf = ""
            else:
                buf = para
    if buf:
        chunks.append(buf)
    return chunks


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(str(path.stat().st_mtime_ns).encode())
    h.update(str(path.stat().st_size).encode())
    return h.hexdigest()[:16]


class RAGEngine:
    def __init__(
        self,
        base_dir: str | Path,
        knowledge_dir: Optional[str | Path] = None,
        embedding_backend: str = "local",
        gemini_api_key_fn: Optional[Callable[[], str]] = None,
    ):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._knowledge_dir = Path(knowledge_dir) if knowledge_dir else self._base / "knowledge_base"
        self._knowledge_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self._base / MANIFEST_NAME

        self._embedder = EmbeddingBackend(backend=embedding_backend, api_key_fn=gemini_api_key_fn)
        self._available = _CHROMA_AVAILABLE and self._embedder.available
        self._collection: Optional[Any] = None

        if self._available:
            chroma_dir = self._base / "chroma_db"
            # Routed through the shared DataAccessLayer -- see
            # consciousness/data_access.py -- so if base_dir ever overlaps
            # with UnifiedMemory's chroma_db (e.g. both pointed at
            # consciousness/unified), they share one client instead of
            # racing two independent ones against the same files.
            client = dal.get_chroma(chroma_dir)
            self._collection = client.get_or_create_collection(name="documents")
        else:
            print("[RAG] ChromaDB or embeddings unavailable — document search disabled.")

        self._manifest = self._load_manifest()

    # ── manifest (tracks which files/versions are already ingested) ────────

    def _load_manifest(self) -> dict:
        if self._manifest_path.exists():
            try:
                return json.loads(self._manifest_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_manifest(self) -> None:
        self._manifest_path.write_text(json.dumps(self._manifest, indent=2), encoding="utf-8")

    # ── ingestion ────────────────────────────────────────────────────────────

    def ingest_file(self, path: str | Path, force: bool = False) -> int:
        """Chunk + embed a single file. Returns number of chunks added.
        Skips the file if it's unchanged since last ingestion (unless force=True)."""
        path = Path(path)
        if not self._available or not path.exists():
            return 0
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return 0

        key = str(path.resolve())
        current_hash = _file_hash(path)
        if not force and self._manifest.get(key) == current_hash:
            return 0  # unchanged, already ingested

        reader = _READERS.get(path.suffix.lower())
        text = reader(path) if reader else ""
        if not text.strip():
            return 0

        chunks = _chunk_text(text)
        if not chunks:
            return 0

        # Remove any previously-ingested chunks for this file before re-adding.
        self._delete_file_chunks(key)

        vectors = self._embedder.embed_many(chunks)
        ids = [f"{current_hash}:{i}" for i in range(len(chunks))]
        metadatas = [
            {"source": path.name, "path": key, "chunk_index": i, "total_chunks": len(chunks)}
            for i in range(len(chunks))
        ]
        try:
            self._collection.add(ids=ids, documents=chunks, embeddings=vectors, metadatas=metadatas)
        except Exception as exc:
            print(f"[RAG] Failed to add chunks for {path.name}: {exc}")
            return 0

        self._manifest[key] = current_hash
        self._save_manifest()
        print(f"[RAG] Ingested {path.name}: {len(chunks)} chunks")
        return len(chunks)

    def _delete_file_chunks(self, resolved_path: str) -> None:
        if not self._available:
            return
        try:
            self._collection.delete(where={"path": resolved_path})
        except Exception:
            pass

    def ingest_folder(self, folder: Optional[str | Path] = None) -> dict:
        """Walk the knowledge folder and ingest every new/changed supported file.
        Safe to call repeatedly (e.g. on every startup) — unchanged files are skipped.
        Also purges chunks for any previously-ingested file that's no longer
        on disk, so search results don't keep citing deleted files forever."""
        folder = Path(folder) if folder else self._knowledge_dir
        if not folder.exists():
            return {"ingested_files": 0, "chunks": 0, "removed_files": 0}

        ingested_files = 0
        total_chunks = 0
        seen_keys: set[str] = set()
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                seen_keys.add(str(path.resolve()))
                n = self.ingest_file(path)
                if n > 0:
                    ingested_files += 1
                    total_chunks += n

        removed_files = self._purge_missing(seen_keys)
        if ingested_files or removed_files:
            print(f"[RAG] Folder scan complete: {ingested_files} file(s) added, "
                  f"{total_chunks} chunk(s), {removed_files} stale file(s) removed.")
        return {"ingested_files": ingested_files, "chunks": total_chunks, "removed_files": removed_files}

    def _purge_missing(self, present_keys: set[str]) -> int:
        """Remove chunks + manifest entries for files that were ingested
        previously but are no longer present under the knowledge folder."""
        stale = [key for key in self._manifest if key not in present_keys]
        for key in stale:
            self._delete_file_chunks(key)
            del self._manifest[key]
            print(f"[RAG] Removed stale file from index: {key}")
        if stale:
            self._save_manifest()
        return len(stale)

    def add_text(self, text: str, source: str, metadata: Optional[dict] = None) -> int:
        """Ingest an ad-hoc piece of text (not backed by a file) — e.g. a saved
        web page, a pasted note, or a generated summary."""
        if not self._available or not text.strip():
            return 0
        chunks = _chunk_text(text)
        if not chunks:
            return 0
        doc_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        vectors = self._embedder.embed_many(chunks)
        ids = [f"adhoc:{doc_hash}:{i}" for i in range(len(chunks))]
        meta_base = {"source": source, "path": f"adhoc:{source}"}
        if metadata:
            meta_base.update(metadata)
        metadatas = [{**meta_base, "chunk_index": i, "total_chunks": len(chunks)} for i in range(len(chunks))]
        try:
            self._collection.add(ids=ids, documents=chunks, embeddings=vectors, metadatas=metadatas)
            return len(chunks)
        except Exception as exc:
            print(f"[RAG] Failed to add ad-hoc text ({source}): {exc}")
            return 0

    # ── retrieval ────────────────────────────────────────────────────────────

    def search_documents(self, query: str, n_results: int = 5) -> list[dict]:
        if not self._available or self._collection is None:
            return []
        try:
            vec = self._embedder.embed_one(query)
            if not vec:
                return []
            results = self._collection.query(query_embeddings=[vec], n_results=n_results)
            output = []
            for i in range(len(results["ids"][0])):
                output.append({
                    "id": results["ids"][0][i],
                    "text": results["documents"][0][i],
                    "metadata": results["metadatas"][0][i],
                    "distance": results["distances"][0][i] if results.get("distances") else None,
                })
            return output
        except Exception as exc:
            print(f"[RAG] Document search failed: {exc}")
            return []

    def search_all(
        self,
        query: str,
        unified_mem=None,
        n_docs: int = 4,
        n_facts: int = 3,
        n_chat: int = 3,
        max_chars: int = 3000,
    ) -> str:
        """Combined RAG retrieval: document chunks + remembered facts +
        past conversation turns, merged into one formatted, source-labeled
        context block. This is what the knowledge_search tool returns to the LLM."""
        sections: list[str] = []

        doc_hits = self.search_documents(query, n_docs)
        if doc_hits:
            lines = ["[From your documents]"]
            for h in doc_hits:
                src = h["metadata"].get("source", "unknown file")
                lines.append(f"- ({src}) {h['text'].strip()}")
            sections.append("\n".join(lines))

        if unified_mem is not None:
            try:
                fact_hits = unified_mem.recall(query, n_facts)
            except Exception:
                fact_hits = []
            if fact_hits:
                lines = ["[From what you remember about the user]"]
                for h in fact_hits:
                    lines.append(f"- {h['text'].strip()}")
                sections.append("\n".join(lines))

            try:
                chat_hits = unified_mem.recall_chat(query, n_chat)
            except Exception:
                chat_hits = []
            if chat_hits:
                lines = ["[From earlier conversations]"]
                for h in chat_hits:
                    role = h["metadata"].get("role", "?")
                    ts = h["metadata"].get("timestamp", "")
                    lines.append(f"- ({role}, {ts}) {h['text'].strip()}")
                sections.append("\n".join(lines))

        if not sections:
            return ""

        result = "\n\n".join(sections)
        if len(result) > max_chars:
            result = result[:max_chars].rstrip() + "…"
        return result

    def stats(self) -> dict:
        count = 0
        if self._available and self._collection is not None:
            try:
                count = self._collection.count()
            except Exception:
                count = 0
        return {
            "available": self._available,
            "documents_chunks": count,
            "files_ingested": len(self._manifest),
            "knowledge_dir": str(self._knowledge_dir),
        }
