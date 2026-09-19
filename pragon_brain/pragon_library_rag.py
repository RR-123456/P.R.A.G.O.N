"""
pragon_library_rag.py
======================
Multi-format file ingestion + RAG (retrieval-augmented generation) for the
P.R.A.G.O.N Notepad's "Library" tab.

Supported inputs: .txt .md .py .json .bat .sh .html .htm .css .js .csv .log
.ini .xml .yaml .yml .cfg .toml (read as plain text) — .pdf (pypdf) —
.docx (python-docx) — .xlsx (openpyxl) — .zip (walks entries, extracts any
text-like member) — .png .jpg .jpeg .gif .webp .bmp (OCR via pytesseract +
a Gemini vision caption, combined).

Storage: flat files on disk + a JSON index under <base>/library_store/.
No external vector DB — each chunk's embedding is a plain list[float]
stored in the JSON index and compared with cosine similarity at query
time. That's fine up to a few thousand chunks; if the Library grows much
larger than that, swap this for sqlite + numpy or a real vector store.

Every extractor degrades gracefully: if an optional dependency (pypdf,
python-docx, openpyxl, pytesseract/Pillow) isn't installed, that file type
still ingests — you just get a message saying which package to add instead
of a crash. Embeddings fall back to a small deterministic local hashing
vector if the Gemini embeddings call fails (no network/no key), so search
still works, just less accurately.

Install extras on the machine actually running JARVIS/PRAGON:
    pip install pypdf python-docx openpyxl pytesseract Pillow
(pytesseract also needs the Tesseract OCR binary installed separately —
https://github.com/tesseract-ocr/tesseract — OCR is skipped gracefully if
it's missing; vision captioning via Gemini still runs either way.)
"""

from __future__ import annotations

import io
import json
import math
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import Callable, Optional

# ── optional extraction dependencies (each degrades gracefully) ────────────
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    import docx as _docx  # python-docx
except Exception:
    _docx = None

try:
    import openpyxl
except Exception:
    openpyxl = None

try:
    import pytesseract
    from PIL import Image
except Exception:
    pytesseract = None
    Image = None

TEXT_EXTS = {
    "txt", "md", "py", "json", "bat", "sh", "html", "htm", "css", "js",
    "csv", "log", "ini", "xml", "yaml", "yml", "cfg", "toml",
}
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
SUPPORTED_EXTS = TEXT_EXTS | IMAGE_EXTS | {"pdf", "docx", "xlsx", "zip"}

MIME_MAP = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
}

EMBED_MODEL = "text-embedding-004"
CAPTION_MODEL = "gemini-2.5-flash"
ASK_MODEL = "gemini-2.5-flash"
CHUNK_SIZE = 1400       # characters
CHUNK_OVERLAP = 200
_LOCAL_DIM = 256


# ═══════════════════════════════════════════════════════════════════════
# Extraction
# ═══════════════════════════════════════════════════════════════════════

def _extract_plain_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _extract_pdf(raw: bytes) -> str:
    if PdfReader is None:
        return "[PDF extraction unavailable on this server — install with: pip install pypdf]"
    reader = PdfReader(io.BytesIO(raw))
    out = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            out.append(f"--- page {i + 1} ---\n{t}")
    return "\n\n".join(out) if out else "[No extractable text found — this PDF may be scanned/image-only]"


def _extract_docx(raw: bytes) -> str:
    if _docx is None:
        return "[DOCX extraction unavailable on this server — install with: pip install python-docx]"
    doc = _docx.Document(io.BytesIO(raw))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts) or "[No extractable text found in this DOCX]"


def _extract_xlsx(raw: bytes) -> str:
    if openpyxl is None:
        return "[XLSX extraction unavailable on this server — install with: pip install openpyxl]"
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    out = []
    for name in wb.sheetnames:
        ws = wb[name]
        out.append(f"--- sheet: {name} ---")
        row_count = 0
        for row in ws.iter_rows(values_only=True):
            if row_count >= 500:
                out.append("... (truncated, sheet has more rows)")
                break
            vals = ["" if v is None else str(v) for v in row]
            if any(v.strip() for v in vals):
                out.append("\t".join(vals))
            row_count += 1
    return "\n".join(out) or "[No data found in this workbook]"


def _extract_zip(raw: bytes, max_inner_bytes: int = 400_000) -> str:
    out = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        out.append(f"[ZIP archive — {len(names)} entries]")
        for name in names:
            if name.endswith("/"):
                continue
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            info = zf.getinfo(name)
            if ext in TEXT_EXTS and info.file_size < 200_000 and total < max_inner_bytes:
                try:
                    content = zf.read(name).decode("utf-8", errors="replace")
                    out.append(f"\n--- {name} ---\n{content}")
                    total += len(content)
                except Exception:
                    out.append(f"\n--- {name} --- [could not read]")
            else:
                out.append(f"\n--- {name} --- [{info.file_size} bytes, not extracted]")
    return "\n".join(out)


def _gemini_caption_image(raw: bytes, mime_type: str, api_key_fn: Callable[[], str]) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key_fn(), http_options={"api_version": "v1beta"})
    parts = [
        types.Part.from_bytes(data=raw, mime_type=mime_type or "image/png"),
        types.Part.from_text(text=(
            "Describe this image in detail for search-indexing purposes: "
            "first transcribe any visible text verbatim, then describe "
            "objects, layout, people, charts, or diagrams shown. Be "
            "concrete and literal, not poetic."
        )),
    ]
    resp = client.models.generate_content(
        model=CAPTION_MODEL,
        contents=[types.Content(role="user", parts=parts)],
    )
    return (resp.text or "").strip()


def _extract_image(raw: bytes, mime_type: str, api_key_fn: Optional[Callable[[], str]]) -> str:
    parts = []
    if pytesseract is not None and Image is not None:
        try:
            img = Image.open(io.BytesIO(raw))
            ocr_text = pytesseract.image_to_string(img).strip()
            if ocr_text:
                parts.append("OCR TEXT:\n" + ocr_text)
        except Exception:
            pass
    if api_key_fn is not None:
        try:
            caption = _gemini_caption_image(raw, mime_type, api_key_fn)
            if caption:
                parts.append("VISUAL DESCRIPTION:\n" + caption)
        except Exception as e:
            parts.append(f"[Vision description unavailable: {e}]")
    return "\n\n".join(parts) if parts else "[No text or description could be extracted from this image]"


def extract_text(raw: bytes, filename: str, api_key_fn: Optional[Callable[[], str]] = None) -> str:
    """Dispatches to the right extractor based on file extension. Always
    returns a string — never raises — so ingestion never hard-fails on a
    single bad file."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if ext in TEXT_EXTS:
            return _extract_plain_text(raw)
        if ext == "pdf":
            return _extract_pdf(raw)
        if ext == "docx":
            return _extract_docx(raw)
        if ext == "xlsx":
            return _extract_xlsx(raw)
        if ext == "zip":
            return _extract_zip(raw)
        if ext in IMAGE_EXTS:
            return _extract_image(raw, MIME_MAP.get(ext, "image/png"), api_key_fn)
        return _extract_plain_text(raw)  # best-effort fallback for unknown types
    except Exception as e:
        return f"[Extraction failed for {filename}: {e}]"


# ═══════════════════════════════════════════════════════════════════════
# Chunking
# ═══════════════════════════════════════════════════════════════════════

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            cut = text.rfind("\n\n", start, end)
            if cut == -1 or cut <= start + chunk_size // 2:
                cut = text.rfind(". ", start, end)
            if cut != -1 and cut > start + chunk_size // 2:
                end = cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = end - overlap
    return chunks


# ═══════════════════════════════════════════════════════════════════════
# Embeddings
# ═══════════════════════════════════════════════════════════════════════

def _local_embed(text: str) -> list[float]:
    """Deterministic offline fallback: a hashed bag-of-words vector. Used
    only when the Gemini embeddings call fails (no network/no API key), so
    search keeps working — just less accurately than real embeddings."""
    vec = [0.0] * _LOCAL_DIM
    for w in re.findall(r"[a-zA-Z0-9]+", (text or "").lower()):
        vec[hash(w) % _LOCAL_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_texts(texts: list[str], api_key_fn: Callable[[], str]) -> list[list[float]]:
    """Embeds via Gemini text-embedding-004. Raises on failure — callers
    catch this and fall back to _local_embed so ingestion/search never
    hard-depend on network access."""
    if not texts:
        return []
    from google import genai
    client = genai.Client(api_key=api_key_fn(), http_options={"api_version": "v1beta"})
    out = []
    batch_size = 20
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = client.models.embed_content(model=EMBED_MODEL, contents=batch)
        out.extend(list(e.values) for e in resp.embeddings)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(y * y for y in b)) or 1e-9
    return dot / (na * nb)


# ═══════════════════════════════════════════════════════════════════════
# Persistent store + retrieval + ask
# ═══════════════════════════════════════════════════════════════════════

class LibraryStore:
    """One JSON index + a folder of raw files. Every item holds its chunks
    with embeddings inline. `list_items()`/anything sent to the browser
    only ever gets the public metadata view — chunk text/embeddings never
    leave the server."""

    def __init__(self, base_dir: "str | Path", api_key_fn: Callable[[], str]):
        self.base_dir = Path(base_dir)
        self.files_dir = self.base_dir / "files"
        self.index_path = self.base_dir / "index.json"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.api_key_fn = api_key_fn
        self.items: list[dict] = []
        self._load()

    def _load(self):
        if self.index_path.exists():
            try:
                self.items = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                self.items = []

    def _save(self):
        self.index_path.write_text(json.dumps(self.items, ensure_ascii=False), encoding="utf-8")

    def _public(self, item: dict) -> dict:
        return {
            "id": item["id"], "name": item["name"], "ext": item["ext"],
            "size": item["size"], "created_at": item["created_at"],
            "preview": item["preview"], "chunk_count": item["chunk_count"],
        }

    def add_item(self, raw: bytes, filename: str) -> dict:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        item_id = "lib_" + uuid.uuid4().hex[:12]
        safe_name = f"{item_id}_{re.sub(r'[^A-Za-z0-9._-]', '_', filename)}"
        (self.files_dir / safe_name).write_bytes(raw)

        text = extract_text(raw, filename, self.api_key_fn)
        chunks = chunk_text(text)
        try:
            embeddings = embed_texts(chunks, self.api_key_fn) if chunks else []
            backend = "gemini"
        except Exception:
            embeddings = [_local_embed(c) for c in chunks]
            backend = "local"

        item = {
            "id": item_id,
            "name": filename,
            "ext": ext,
            "stored_as": safe_name,
            "size": len(raw),
            "created_at": time.time(),
            "preview": text[:600],
            "chunk_count": len(chunks),
            "chunks": [{"text": c, "embedding": e, "backend": backend} for c, e in zip(chunks, embeddings)],
        }
        self.items.append(item)
        self._save()
        return self._public(item)

    def list_items(self) -> list[dict]:
        return [self._public(i) for i in self.items]

    def get_filepath(self, item_id: str) -> Optional[Path]:
        item = next((i for i in self.items if i["id"] == item_id), None)
        if not item:
            return None
        return self.files_dir / item["stored_as"], item["name"]  # type: ignore[return-value]

    def delete_item(self, item_id: str) -> bool:
        before = len(self.items)
        keep = []
        for i in self.items:
            if i["id"] == item_id:
                try:
                    (self.files_dir / i["stored_as"]).unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                keep.append(i)
        self.items = keep
        self._save()
        return len(self.items) < before

    def search(self, query: str, item_id: Optional[str] = None, top_k: int = 6) -> list[dict]:
        try:
            q_gemini = embed_texts([query], self.api_key_fn)[0]
        except Exception:
            q_gemini = None
        q_local = _local_embed(query)

        candidates = self.items if not item_id else [i for i in self.items if i["id"] == item_id]
        scored = []
        for item in candidates:
            for ch in item.get("chunks", []):
                qv = q_gemini if ch.get("backend") == "gemini" and q_gemini is not None else q_local
                score = _cosine(qv, ch["embedding"])
                if score > -1:
                    scored.append((score, item["name"], item["id"], ch["text"]))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"score": round(s, 4), "name": n, "item_id": iid, "text": t} for s, n, iid, t in scored[:top_k]]

    def ask(self, query: str, item_id: Optional[str] = None, top_k: int = 6) -> dict:
        hits = self.search(query, item_id=item_id, top_k=top_k)
        if not hits:
            return {"answer": "Nothing relevant found in the Library for that question.", "sources": []}

        context = "\n\n".join(f"[Source: {h['name']}]\n{h['text']}" for h in hits)
        prompt = (
            "You are answering a question using ONLY the reference material "
            "below, pulled from the user's P.R.A.G.O.N Library. Cite which "
            "file each fact came from by name. If the material doesn't "
            "contain the answer, say so plainly instead of guessing.\n\n"
            f"REFERENCE MATERIAL:\n{context}\n\n"
            f"QUESTION: {query}"
        )
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.api_key_fn(), http_options={"api_version": "v1beta"})
        resp = client.models.generate_content(
            model=ASK_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(max_output_tokens=2048, temperature=0.3),
        )
        answer = (resp.text or "").strip() if getattr(resp, "text", None) else ""
        if not answer and resp.candidates:
            answer = "".join(p.text for p in resp.candidates[0].content.parts if getattr(p, "text", None)).strip()

        sources = sorted({(h["name"], h["item_id"]) for h in hits})
        return {
            "answer": answer or "(Omnix returned an empty response.)",
            "sources": [{"name": n, "item_id": i} for n, i in sources],
        }
