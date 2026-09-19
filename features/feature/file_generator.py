"""
file_generator.py
==================
Generates and saves files of (almost) any type on request: Word docs, PDFs,
spreadsheets, presentations, plain text/markdown, JSON/XML/CSV, source code
in any language, and raster images.

Design, mirrors pragon_library_rag.py's conventions:
  - Every renderer degrades gracefully: if an optional dependency
    (python-docx, reportlab, openpyxl, python-pptx, Pillow) isn't installed,
    that file type still "generates" as a plain-text fallback with a message
    telling you which package to add, instead of crashing.
  - Content strategy is hybrid, per the brief: Gemini drafts the actual
    words/structure (titles, paragraphs, bullet points, slide text, sheet
    data, code, raw pixels for images) and a real Python library renders
    the final file byte-for-byte (fonts, styles, cell types, slide layouts,
    PNG bytes) rather than Gemini trying to hand-author a binary format.
    This is both faster (small structured JSON responses instead of huge
    text dumps) and far more reliable (the library guarantees a valid
    .docx/.xlsx/.pptx/.png every time).

Install extras on the machine actually running JARVIS/PRAGON:
    pip install python-docx reportlab openpyxl python-pptx Pillow

Public entry point:
    file_generator(parameters, player, speak, api_key_fn) -> str
mirrors the calling convention already used by file_processor / compass /
pragon_builder elsewhere in pragon_main.py's tool dispatcher.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Callable, Optional

# ── optional rendering dependencies (each degrades gracefully) ─────────────
try:
    import docx as _docx  # python-docx
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except Exception:
    _docx = None

try:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem,
    )
except Exception:
    SimpleDocTemplate = None

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
except Exception:
    openpyxl = None

try:
    from pptx import Presentation
    from pptx.util import Inches as PptxInches, Pt as PptxPt
except Exception:
    Presentation = None

try:
    from PIL import Image
except Exception:
    Image = None


TEXT_MODEL = "gemini-2.5-flash"
IMAGE_MODEL = "gemini-2.5-flash-image"

# File types whose *content itself* is the file — Gemini's raw text output
# is written straight to disk (after light cleanup), no rendering library
# needed.
_PLAIN_TEXT_TYPES = {"txt", "md", "json", "xml", "yaml", "yml", "html", "css", "js", "code"}

_CODE_EXT_BY_LANGUAGE = {
    "python": "py", "javascript": "js", "typescript": "ts", "java": "java",
    "c": "c", "c++": "cpp", "cpp": "cpp", "c#": "cs", "csharp": "cs",
    "go": "go", "rust": "rs", "ruby": "rb", "php": "php", "swift": "swift",
    "kotlin": "kt", "html": "html", "css": "css", "sql": "sql", "bash": "sh",
    "shell": "sh", "powershell": "ps1", "r": "r", "lua": "lua",
}

_SAVE_SHORTCUTS = {
    "desktop": lambda: Path.home() / "Desktop",
    "downloads": lambda: Path.home() / "Downloads",
    "documents": lambda: Path.home() / "Documents",
    "home": lambda: Path.home(),
}


# ═══════════════════════════════════════════════════════════════════════
# Gemini drafting helpers
# ═══════════════════════════════════════════════════════════════════════

def _client(api_key_fn: Callable[[], str]):
    from google import genai
    return genai.Client(api_key=api_key_fn(), http_options={"api_version": "v1beta"})


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\n(.*)\n```$", text, re.DOTALL)
    return m.group(1).strip() if m else text


def _ask_text(prompt: str, api_key_fn: Callable[[], str], max_tokens: int = 4096) -> str:
    from google.genai import types
    client = _client(api_key_fn)
    resp = client.models.generate_content(
        model=TEXT_MODEL,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        config=types.GenerateContentConfig(max_output_tokens=max_tokens, temperature=0.5),
    )
    out = (resp.text or "").strip() if getattr(resp, "text", None) else ""
    if not out and resp.candidates:
        out = "".join(
            p.text for p in resp.candidates[0].content.parts if getattr(p, "text", None)
        ).strip()
    return _strip_fences(out)


def _ask_json(prompt: str, api_key_fn: Callable[[], str], max_tokens: int = 4096) -> dict:
    full_prompt = (
        prompt
        + "\n\nRespond with ONLY raw JSON — no markdown fences, no preamble, "
          "no trailing commentary. The response must parse with json.loads()."
    )
    raw = _ask_text(full_prompt, api_key_fn, max_tokens=max_tokens)
    try:
        return json.loads(raw)
    except Exception:
        # one repair attempt — models occasionally wrap JSON in prose anyway
        m = re.search(r"\{.*\}|\[.*\]", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


# ═══════════════════════════════════════════════════════════════════════
# Misc helpers
# ═══════════════════════════════════════════════════════════════════════

def _resolve_save_dir(save_location: Optional[str], base_dir: Path) -> Path:
    key = (save_location or "desktop").strip().lower()
    if key in _SAVE_SHORTCUTS:
        d = _SAVE_SHORTCUTS[key]()
    elif key == "library":
        d = base_dir / "library_store" / "generated"
    elif save_location:
        d = Path(save_location).expanduser()
    else:
        d = _SAVE_SHORTCUTS["desktop"]()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_filename(name: str, ext: str) -> str:
    name = (name or "").strip() or f"generated_{int(time.time())}"
    name = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", name)  # drop any extension the model/user added
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip() or f"generated_{int(time.time())}"
    return f"{name}.{ext}"


def _unique_path(directory: Path, filename: str) -> Path:
    p = directory / filename
    if not p.exists():
        return p
    stem, ext = p.stem, p.suffix
    i = 2
    while (directory / f"{stem} ({i}){ext}").exists():
        i += 1
    return directory / f"{stem} ({i}){ext}"


def _infer_file_type(description: str, explicit_type: Optional[str], language: Optional[str]) -> str:
    if explicit_type and explicit_type.lower() not in ("auto", ""):
        return explicit_type.lower()
    d = (description or "").lower()
    if language:
        return "code"
    if any(k in d for k in ("spreadsheet", "excel", "workbook", ".xlsx", "budget", "expense tracker")):
        return "xlsx"
    if any(k in d for k in ("slide", "presentation", "deck", "pptx", "powerpoint")):
        return "pptx"
    if any(k in d for k in ("pdf",)):
        return "pdf"
    if any(k in d for k in ("word document", "docx", "report", "cover letter", "resume", "cv ", "letter")):
        return "docx"
    if any(k in d for k in ("image", "picture", "photo", "logo", "illustration", "artwork", "wallpaper", "icon", "drawing")):
        return "image"
    if any(k in d for k in ("csv", "comma separated")):
        return "csv"
    if any(k in d for k in ("json",)):
        return "json"
    if any(k in d for k in ("code", "script", "function", "program", "class ", "algorithm")):
        return "code"
    if any(k in d for k in ("markdown", ".md", "readme")):
        return "md"
    return "txt"


# ═══════════════════════════════════════════════════════════════════════
# Renderers — one per file type. Each returns the saved Path.
# ═══════════════════════════════════════════════════════════════════════

def _render_plain_text(kind: str, description: str, file_name: Optional[str],
                        save_dir: Path, api_key_fn: Callable[[], str]) -> Path:
    prompts = {
        "txt": f"Write plain text content for: {description}\n\nOutput only the final text, no titles like 'Here is...'.",
        "md": f"Write a well-formatted Markdown document for: {description}\n\nUse headings, lists, and emphasis where appropriate.",
        "json": f"Produce a JSON document for: {description}\n\nOutput ONLY valid JSON.",
        "xml": f"Produce a well-formed XML document for: {description}\n\nOutput ONLY the XML.",
        "yaml": f"Produce a valid YAML document for: {description}\n\nOutput ONLY the YAML.",
        "html": f"Produce a complete, self-contained HTML page for: {description}\n\nInline any CSS/JS. Output ONLY the HTML.",
        "css": f"Produce CSS for: {description}\n\nOutput ONLY the CSS.",
        "js": f"Produce JavaScript for: {description}\n\nOutput ONLY the code.",
    }
    content = _ask_text(prompts.get(kind, prompts["txt"]), api_key_fn, max_tokens=8192)

    if kind == "json":
        try:
            content = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
        except Exception:
            pass  # save whatever Gemini gave us rather than fail the whole request

    ext = kind
    path = _unique_path(save_dir, _safe_filename(file_name, ext))
    path.write_text(content, encoding="utf-8")
    return path


def _render_code(description: str, language: Optional[str], file_name: Optional[str],
                  save_dir: Path, api_key_fn: Callable[[], str]) -> Path:
    language = (language or "python").strip()
    prompt = (
        f"Write complete, working {language} code for the following request. "
        f"Include brief comments where genuinely helpful. Do not include an "
        f"explanation before or after the code — output ONLY the code.\n\n"
        f"Request: {description}"
    )
    code = _ask_text(prompt, api_key_fn, max_tokens=8192)
    ext = _CODE_EXT_BY_LANGUAGE.get(language.lower(), "txt")
    path = _unique_path(save_dir, _safe_filename(file_name, ext))
    path.write_text(code, encoding="utf-8")
    return path


def _render_csv(description: str, file_name: Optional[str], save_dir: Path,
                 api_key_fn: Callable[[], str]) -> Path:
    plan = _ask_json(
        "Design tabular data for this request, as JSON of the exact shape "
        '{"headers": ["col1", "col2", ...], "rows": [["v1", "v2", ...], ...]}. '
        f"Request: {description}",
        api_key_fn,
    )
    headers = plan.get("headers", [])
    rows = plan.get("rows", [])
    path = _unique_path(save_dir, _safe_filename(file_name, "csv"))
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if headers:
            writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
    return path


def _render_docx(description: str, file_name: Optional[str], save_dir: Path,
                  api_key_fn: Callable[[], str]) -> Path:
    plan = _ask_json(
        "Plan a Word document for this request, as JSON of the exact shape "
        '{"title": "...", "sections": [{"heading": "...", "level": 1, '
        '"body": "paragraph text or empty string", "bullets": ["...", "..."]}]}. '
        "Levels go 1 (H1) to 3 (H3). Use bullets for lists, body for prose; "
        "either may be empty per section, not both.\n\n"
        f"Request: {description}",
        api_key_fn,
    )
    if _docx is None:
        return _fallback_as_text(plan, file_name, save_dir, "docx")

    doc = _docx.Document()
    if plan.get("title"):
        h = doc.add_heading(plan["title"], level=0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER

    for sec in plan.get("sections", []):
        if sec.get("heading"):
            doc.add_heading(sec["heading"], level=min(max(int(sec.get("level", 1)), 1), 3))
        if sec.get("body"):
            doc.add_paragraph(sec["body"])
        for bullet in sec.get("bullets", []) or []:
            doc.add_paragraph(str(bullet), style="List Bullet")

    path = _unique_path(save_dir, _safe_filename(file_name, "docx"))
    doc.save(str(path))
    return path


def _render_pdf(description: str, file_name: Optional[str], save_dir: Path,
                 api_key_fn: Callable[[], str]) -> Path:
    plan = _ask_json(
        "Plan a document for this request, as JSON of the exact shape "
        '{"title": "...", "sections": [{"heading": "...", "level": 1, '
        '"body": "paragraph text or empty string", "bullets": ["...", "..."]}]}. '
        f"Request: {description}",
        api_key_fn,
    )
    if SimpleDocTemplate is None:
        return _fallback_as_text(plan, file_name, save_dir, "pdf")

    path = _unique_path(save_dir, _safe_filename(file_name, "pdf"))
    styles = getSampleStyleSheet()
    heading_styles = {
        1: ParagraphStyle("H1", parent=styles["Heading1"]),
        2: ParagraphStyle("H2", parent=styles["Heading2"]),
        3: ParagraphStyle("H3", parent=styles["Heading3"]),
    }
    story = []
    if plan.get("title"):
        story.append(Paragraph(plan["title"], styles["Title"]))
        story.append(Spacer(1, 0.3 * inch))

    for sec in plan.get("sections", []):
        if sec.get("heading"):
            level = min(max(int(sec.get("level", 1)), 1), 3)
            story.append(Paragraph(sec["heading"], heading_styles[level]))
        if sec.get("body"):
            story.append(Paragraph(sec["body"], styles["BodyText"]))
        bullets = sec.get("bullets") or []
        if bullets:
            story.append(ListFlowable(
                [ListItem(Paragraph(str(b), styles["BodyText"])) for b in bullets],
                bulletType="bullet",
            ))
        story.append(Spacer(1, 0.15 * inch))

    doc = SimpleDocTemplate(str(path), pagesize=LETTER)
    doc.build(story)
    return path


def _render_pptx(description: str, file_name: Optional[str], save_dir: Path,
                  api_key_fn: Callable[[], str]) -> Path:
    plan = _ask_json(
        "Plan a slide deck for this request, as JSON of the exact shape "
        '{"title": "...", "subtitle": "...", "slides": [{"title": "...", '
        '"bullets": ["...", "..."], "notes": "speaker notes or empty string"}]}.'
        f"\n\nRequest: {description}",
        api_key_fn,
    )
    if Presentation is None:
        return _fallback_as_text(plan, file_name, save_dir, "pptx")

    prs = Presentation()
    title_layout = prs.slide_layouts[0]
    content_layout = prs.slide_layouts[1]

    slide = prs.slides.add_slide(title_layout)
    slide.shapes.title.text = plan.get("title", "Untitled")
    if len(slide.placeholders) > 1 and plan.get("subtitle"):
        slide.placeholders[1].text = plan["subtitle"]

    for s in plan.get("slides", []):
        slide = prs.slides.add_slide(content_layout)
        slide.shapes.title.text = s.get("title", "")
        body = slide.placeholders[1].text_frame
        bullets = s.get("bullets", []) or []
        if bullets:
            body.text = str(bullets[0])
            for b in bullets[1:]:
                p = body.add_paragraph()
                p.text = str(b)
        if s.get("notes"):
            slide.notes_slide.notes_text_frame.text = s["notes"]

    path = _unique_path(save_dir, _safe_filename(file_name, "pptx"))
    prs.save(str(path))
    return path


def _render_xlsx(description: str, file_name: Optional[str], save_dir: Path,
                  api_key_fn: Callable[[], str]) -> Path:
    plan = _ask_json(
        "Design a spreadsheet for this request, as JSON of the exact shape "
        '{"sheets": [{"name": "Sheet1", "headers": ["col1", "col2"], '
        '"rows": [["v1", "v2"], ...]}]}. Use multiple sheets only if the '
        f"request clearly needs them.\n\nRequest: {description}",
        api_key_fn,
    )
    if openpyxl is None:
        return _fallback_as_text(plan, file_name, save_dir, "xlsx")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet_plan in plan.get("sheets", [{"name": "Sheet1", "headers": [], "rows": []}]):
        ws = wb.create_sheet(title=(sheet_plan.get("name") or "Sheet1")[:31])
        headers = sheet_plan.get("headers", [])
        rows = sheet_plan.get("rows", [])
        if headers:
            ws.append(headers)
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="4472C4")
                cell.alignment = Alignment(horizontal="center")
            ws.freeze_panes = "A2"
        for row in rows:
            ws.append(row)
        # rough auto-width
        widths = [len(str(h)) for h in headers] if headers else [10] * (len(rows[0]) if rows else 1)
        for row in rows:
            for i, val in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(str(val)))
        for i, w in enumerate(widths):
            ws.column_dimensions[get_column_letter(i + 1)].width = min(max(w + 2, 8), 50)

    path = _unique_path(save_dir, _safe_filename(file_name, "xlsx"))
    wb.save(str(path))
    return path


def _render_image(description: str, file_name: Optional[str], save_dir: Path,
                   api_key_fn: Callable[[], str]) -> Path:
    from google.genai import types
    client = _client(api_key_fn)
    resp = client.models.generate_content(
        model=IMAGE_MODEL,
        contents=description,
        config=types.GenerateContentConfig(response_modalities=["Image"]),
    )
    image_bytes = None
    for part in resp.candidates[0].content.parts:
        if getattr(part, "inline_data", None):
            image_bytes = part.inline_data.data
            break
    if image_bytes is None:
        raise RuntimeError("The image model did not return image data.")

    path = _unique_path(save_dir, _safe_filename(file_name, "png"))
    if Image is not None:
        Image.open(io.BytesIO(image_bytes)).save(str(path))
    else:
        path.write_bytes(image_bytes)
    return path


def _fallback_as_text(plan: dict, file_name: Optional[str], save_dir: Path, missing_for: str) -> Path:
    """Used when the proper rendering library isn't installed — saves the
    drafted content as readable plain text instead of failing outright."""
    lines = [f"[Rendered as plain text — install the library for real .{missing_for}: "
              f"see file_generator.py header for pip install command]", ""]
    if plan.get("title"):
        lines.append(plan["title"].upper())
        lines.append("")
    for sec in plan.get("sections", plan.get("slides", [])):
        if sec.get("heading") or sec.get("title"):
            lines.append("## " + (sec.get("heading") or sec.get("title")))
        if sec.get("body"):
            lines.append(sec["body"])
        for b in sec.get("bullets", []) or []:
            lines.append(f"  - {b}")
        lines.append("")
    for sheet in plan.get("sheets", []):
        lines.append("## " + sheet.get("name", "Sheet"))
        lines.append("\t".join(sheet.get("headers", [])))
        for row in sheet.get("rows", []):
            lines.append("\t".join(str(v) for v in row))
        lines.append("")

    path = _unique_path(save_dir, _safe_filename(file_name, "txt"))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════

_RENDERERS = {
    "docx": lambda desc, name, dir_, key, **_: _render_docx(desc, name, dir_, key),
    "pdf": lambda desc, name, dir_, key, **_: _render_pdf(desc, name, dir_, key),
    "pptx": lambda desc, name, dir_, key, **_: _render_pptx(desc, name, dir_, key),
    "xlsx": lambda desc, name, dir_, key, **_: _render_xlsx(desc, name, dir_, key),
    "csv": lambda desc, name, dir_, key, **_: _render_csv(desc, name, dir_, key),
    "image": lambda desc, name, dir_, key, **_: _render_image(desc, name, dir_, key),
    "code": lambda desc, name, dir_, key, language=None, **_: _render_code(desc, language, name, dir_, key),
}
for _k in _PLAIN_TEXT_TYPES - {"code"}:
    _RENDERERS[_k] = (lambda kind: lambda desc, name, dir_, key, **_: _render_plain_text(kind, desc, name, dir_, key))(_k)


def file_generator(parameters: dict, player=None, speak: Optional[Callable[[str], None]] = None,
                    api_key_fn: Optional[Callable[[], str]] = None,
                    base_dir: Optional[Path] = None) -> str:
    """
    Generates a file from a natural-language description and saves it.

    parameters:
        file_type      docx | pdf | txt | md | json | xml | csv | xlsx |
                       pptx | image | code | auto (default: auto)
        description    what the file should contain (required) — pass the
                       user's request through in full; more detail = better
                       output.
        file_name      optional base name, extension is added automatically
        save_location  desktop | downloads | documents | library | home |
                       an explicit path (default: desktop)
        language       programming language, only used when file_type=code
                       or is inferred as code
    """
    if api_key_fn is None:
        return "Cannot generate the file — no API key was configured for file_generator."

    description = (parameters.get("description") or "").strip()
    if not description:
        return "Tell me what the file should contain and I'll generate it."

    file_type = _infer_file_type(description, parameters.get("file_type"), parameters.get("language"))
    file_name = parameters.get("file_name")
    save_location = parameters.get("save_location") or "desktop"
    language = parameters.get("language")

    save_dir = _resolve_save_dir(save_location, base_dir or Path.cwd())
    renderer = _RENDERERS.get(file_type, _RENDERERS["txt"])

    try:
        path = renderer(description, file_name, save_dir, api_key_fn, language=language)
    except Exception as e:
        msg = f"Couldn't generate the {file_type} file: {e}"
        if player is not None and hasattr(player, "write_log"):
            player.write_log(f"ERR: {msg}")
        return msg

    result = f"Created {path.name} and saved it to {path.parent}."

    if player is not None:
        if hasattr(player, "current_file"):
            try:
                player.current_file = str(path)
            except Exception:
                pass
        if hasattr(player, "show_content"):
            try:
                player.show_content(f"Generated — {path.name}", str(path))
            except Exception:
                pass
        if hasattr(player, "write_log"):
            player.write_log(f"SYS: {result}")

    return result
