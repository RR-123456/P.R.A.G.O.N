"""
consciousness/embeddings.py
════════════════════════════════════════════════════════════════════════════
Shared, pluggable embedding backend for PRAGON's memory + RAG systems.

Two backends, chosen at construction time:
  • "local"  — sentence-transformers (all-MiniLM-L6-v2), free, offline, fast.
  • "gemini" — Google's text-embedding-004 via google-genai, higher quality,
               needs an API key + network, small per-call cost.

Both unified_memory.py (facts + conversation recall) and rag_engine.py
(document knowledge base) import EmbeddingBackend from here, so switching
the backend in one place changes it everywhere.

NOTE: local and gemini embeddings live in different vector spaces and are
NOT interchangeable. Don't mix them inside a single Chroma collection —
if you switch backends, re-ingest (delete the relevant chroma_db folder).
"""

from __future__ import annotations

import warnings
from typing import Optional, Callable

# ── Local backend (sentence-transformers) ───────────────────────────────────

_LOCAL_MODEL = None


def _get_local_model():
    global _LOCAL_MODEL
    if _LOCAL_MODEL is None:
        from sentence_transformers import SentenceTransformer
        import torch

        device = "cpu"
        if torch.cuda.is_available():
            try:
                major, minor = torch.cuda.get_device_capability()
                arch = f"sm_{major}{minor}"
                supported = torch.cuda.get_arch_list()
                if arch in supported or any(s.startswith(f"sm_{major}") for s in supported):
                    device = "cuda"
            except Exception:
                device = "cpu"

        _LOCAL_MODEL = SentenceTransformer("all-MiniLM-L6-v2", device=device)
    return _LOCAL_MODEL


# ── Gemini backend ───────────────────────────────────────────────────────────

_GEMINI_CLIENTS: dict[str, object] = {}  # keyed by api_key, so a rotated key gets its own client
GEMINI_EMBED_MODEL = "models/text-embedding-004"


def _get_gemini_client(api_key: str):
    # Previously this cached a single client on first call and ignored every
    # api_key passed afterward — so rotating a dead/rate-limited key in
    # api_keys.json never actually took effect until the process restarted.
    client = _GEMINI_CLIENTS.get(api_key)
    if client is None:
        from google import genai
        client = genai.Client(api_key=api_key)
        _GEMINI_CLIENTS.clear()  # only keep the current key's client around
        _GEMINI_CLIENTS[api_key] = client
    return client


class EmbeddingBackend:
    """
    Uniform interface: embed_one(text) -> list[float], embed_many(texts) -> list[list[float]]

    backend: "local" | "gemini"
    api_key_fn: callable returning a fresh Gemini API key (only needed for "gemini")
    """

    def __init__(
        self,
        backend: str = "local",
        api_key_fn: Optional[Callable[[], str]] = None,
    ):
        self.backend = backend if backend in ("local", "gemini") else "local"
        self._api_key_fn = api_key_fn
        self.available = False
        self._init_backend()

    def _init_backend(self) -> None:
        if self.backend == "local":
            try:
                _get_local_model()
                self.available = True
            except ImportError:
                warnings.warn(
                    "[Embeddings] sentence-transformers not installed. "
                    "Run: pip install sentence-transformers",
                    stacklevel=2,
                )
                self.available = False
        else:  # gemini
            if not self._api_key_fn:
                warnings.warn(
                    "[Embeddings] Gemini backend selected but no api_key_fn provided.",
                    stacklevel=2,
                )
                self.available = False
                return
            try:
                from google import genai  # noqa: F401
                self.available = True
            except ImportError:
                warnings.warn(
                    "[Embeddings] google-genai not installed for Gemini embeddings.",
                    stacklevel=2,
                )
                self.available = False

    def embed_one(self, text: str) -> list[float]:
        return self.embed_many([text])[0] if text else []

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.available:
            return [[] for _ in texts]

        if self.backend == "local":
            try:
                model = _get_local_model()
                vectors = model.encode(list(texts))
                return [v.tolist() for v in vectors]
            except Exception as exc:
                print(f"[Embeddings] Local embed failed: {exc}")
                return [[] for _ in texts]

        # gemini
        try:
            client = _get_gemini_client(self._api_key_fn())
            result = client.models.embed_content(
                model=GEMINI_EMBED_MODEL,
                contents=list(texts),
            )
            embeddings = getattr(result, "embeddings", None)
            if not embeddings:
                return [[] for _ in texts]
            return [list(e.values) for e in embeddings]
        except Exception as exc:
            print(f"[Embeddings] Gemini embed failed: {exc}")
            return [[] for _ in texts]
