# server.py
# ============================================================================
#  PRAGON — C.U.S.T.O.M FORGE  (bridge server)
#
#  This is the real backend for CUSTOM_FORGE.html. The original UI called
#  https://api.anthropic.com/v1/messages directly from the browser — that
#  can't work (no browser-safe way to hold a key, no CORS, no way to run
#  the "continue if truncated" loop safely). This server fixes that:
#
#     browser (drawing pad + prompt) ──POST /api/forge──> this server
#                                                              │
#                                                    builds one big prompt
#                                                    (sketch image + typed
#                                                     structure/name/position
#                                                     data + the user's brief)
#                                                              │
#                                                       Gemini (vision model)
#                                                              │
#                                                     safety-checked HTML
#                                                              │
#                                              saved to ./forgeoutput/*.html
#                                                              │
#                                        <── returns {code, filename, url} ──
#
#  Run:
#     pip install -r requirements.txt
#     python server.py
#     open http://127.0.0.1:5000
# ============================================================================

import os
import re
import json
import base64
import hashlib
import webbrowser
import urllib.parse
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, render_template, Response, stream_with_context

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from api.security import verify_token
except ImportError:
    verify_token = None
    print("[CustomForge] WARNING: api.security not importable -- "
          "state-changing routes running without auth.")

# ----------------------------------------------------------------------------
# Paths / setup
# ----------------------------------------------------------------------------
BASE_DIR        = Path(__file__).resolve().parent
CONFIG_PATH     = BASE_DIR / "config" / "api_keys.json"
OUTPUT_DIR      = BASE_DIR / "forgeoutput"
TEMPLATES_DIR   = BASE_DIR / "templates"
STYLE_LIB_DIR   = BASE_DIR / "style_templates"
STYLE_MANIFEST  = STYLE_LIB_DIR / "manifest.json"
IMAGE_LIB_DIR   = BASE_DIR / "image_library"
IMAGE_MANIFEST  = IMAGE_LIB_DIR / "manifest.json"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
STYLE_LIB_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_LIB_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(TEMPLATES_DIR), static_folder=str(BASE_DIR / "static"))


@app.before_request
def _require_auth_for_writes():
    """
    Root/static/GET routes (the F.O.R.G.E UI itself) stay open on this
    127.0.0.1-bound service. POST/PUT/DELETE routes -- /api/forge (code
    generation + execution), /api/config, template/image writes -- need a
    bearer token (see api/security.py).
    """
    if verify_token is None:
        return
    if request.method not in ("POST", "PUT", "DELETE"):
        return
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else None
    if not token or not verify_token(token):
        return jsonify({"error": "Missing or invalid bearer token."}), 401

# ----------------------------------------------------------------------------
# API key / credentials
# ----------------------------------------------------------------------------
def get_api_key() -> str:
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Missing {CONFIG_PATH}. Create config/api_keys.json with "
            '{"gemini_api_key": "YOUR_KEY_HERE"}'
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        key = json.load(f).get("gemini_api_key", "")
    if not key or "YOUR_" in key.upper():
        raise RuntimeError("gemini_api_key is not set in config/api_keys.json")
    return key


def get_groq_key() -> str | None:
    """Optional. Returns None (never raises) if Groq isn't configured —
    Groq is a fallback provider, not a hard requirement."""
    if not CONFIG_PATH.exists():
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        key = json.load(f).get("groq_api_key", "")
    if not key or "YOUR_" in key.upper():
        return None
    return key


def get_openrouter_key() -> str | None:
    """Optional. Returns None (never raises) if OpenRouter isn't
    configured — same "best-effort fallback" contract as get_groq_key()."""
    if not CONFIG_PATH.exists():
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        key = json.load(f).get("openrouter_api_key", "")
    if not key or "YOUR_" in key.upper():
        return None
    return key


# Default local model. 3B is the sweet spot for CPU-only boxes — a 7B
# model is noticeably higher quality but can take several minutes per
# response with no GPU; 1.5B is faster still but weaker at nailing a full
# single-file HTML/CSS/JS app in one shot. Swap by adding "ollama_model"
# to config/api_keys.json, e.g. "qwen2.5-coder:1.5b" or "qwen2.5-coder:7b"
# (pull it first with `ollama pull <name>`).
OLLAMA_DEFAULT_MODEL = "qwen2.5-coder:3b"
OLLAMA_DEFAULT_HOST = "http://localhost:11434"


def get_ollama_config() -> dict:
    """Third-tier fallback: a local model via Ollama. Unlike Gemini/Groq
    this needs no API key — it runs on the same machine — so it never
    raises; missing config just means the defaults above are used. Actual
    reachability (is Ollama installed & running, is the model pulled) is
    only discovered when run_completion_ollama() tries to call it."""
    host, model = OLLAMA_DEFAULT_HOST, OLLAMA_DEFAULT_MODEL
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        host = (data.get("ollama_host") or host).rstrip("/")
        model = data.get("ollama_model") or model
    return {"host": host, "model": model}


# Gemini errors that mean "this provider is out for now, try the other
# one" — quota exhaustion (429), rate limiting, AND a bad/invalid/revoked
# key. That last category used to be excluded on purpose (the idea being:
# surface a broken key loudly instead of silently masking it) — but with
# three more fallback tiers now in place (Groq, OpenRouter, Ollama), a bad
# Gemini key shouldn't take the whole chain down; it should just mean
# "skip Gemini, try the next one," same as a quota error. The `warning`
# field on the response still surfaces exactly what went wrong — nothing
# gets silently hidden, it just doesn't hard-stop the whole request.
GEMINI_FALLBACK_MARKERS = (
    "429", "RESOURCE_EXHAUSTED", "quota", "rate limit",
    "API_KEY_INVALID", "api key not valid", "400",
)


def is_gemini_capacity_error(exc: Exception) -> bool:
    text = str(exc)
    return any(marker.lower() in text.lower() for marker in GEMINI_FALLBACK_MARKERS)


def configure_genai() -> None:
    """Point the Gemini SDK at whichever credential type is stored in
    config/api_keys.json.

    - A static Google AI Studio key (looks like "AIzaSy...") is a plain
      API key and gets passed straight through as `api_key`.
    - An OAuth-style bearer/access token (e.g. starts with "AQ." or
      "ya29.") is NOT a valid `api_key` value for genai.configure() — it
      has to be wrapped in google.oauth2.credentials.Credentials and
      passed as `credentials` instead. These tokens are short-lived
      (Google access tokens typically expire after ~1 hour), so if a
      request suddenly starts failing with an auth error, the token in
      config/api_keys.json most likely just needs to be refreshed.

    Gotcha this guards against: genai.configure() ALWAYS tries to backfill
    `client_options.api_key` from the GEMINI_API_KEY / GOOGLE_API_KEY
    environment variables whenever you don't pass `api_key=` explicitly —
    even if you *are* passing `credentials=`. If either of those env vars
    happens to be set (e.g. left over from another setup), the SDK ends up
    sending BOTH an api-key header and the OAuth bearer token, Google
    validates the api-key first, and a stale/invalid one there produces
    exactly this error:
        401 ... "Expected OAuth 2 access token, login cookie or other
        valid authentication credential" / reason: API_KEY_SERVICE_BLOCKED
    So when using OAuth, those two env vars are temporarily cleared for the
    duration of this call to make sure only the bearer token is sent.
    """
    import google.generativeai as genai

    key = get_api_key()
    if key.startswith(("AQ.", "ya29.")):
        from google.oauth2.credentials import Credentials
        env_names = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
        saved = {name: os.environ.pop(name, None) for name in env_names}
        try:
            genai.configure(credentials=Credentials(token=key))
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value
    else:
        genai.configure(api_key=key)


# ----------------------------------------------------------------------------
# Safety net — same spirit as actions/pragon.py: never let generated code
# reach for filesystem / process / eval-style escape hatches in a file
# that's going to be opened straight in a browser tab.
# ----------------------------------------------------------------------------
BLOCKED_PATTERNS = [
    r"require\s*\(\s*['\"]child_process",
    r"require\s*\(\s*['\"]fs['\"]",
    r"\bnode:fs\b",
    r"\beval\s*\(",
    r"document\.write\s*\(\s*unescape",
    r"<\s*iframe[^>]+src\s*=\s*['\"]javascript:",
    r"new\s+ActiveXObject",
]
_BLOCKED_RE = re.compile("|".join(BLOCKED_PATTERNS), re.IGNORECASE)

# Separate from BLOCKED_PATTERNS above (which catches *how* code escapes the
# browser sandbox) — this catches *what the page is for*: a real, working
# lure that gets a visitor to download an executable. Legitimate sites
# essentially never trigger a download of one of these extensions via a
# button/link — the model saying UNSAFE at the prompt stage is the primary
# defense (see SAFETY block in build_instructions/build_edit_instructions),
# but a themed "prank"/"dangerous" brief can still slip past a weaker
# fallback model that treats the theme literally instead of recognizing the
# pattern, so this is a second, independent check on the actual output.
DANGEROUS_DOWNLOAD_RE = re.compile(
    r"""(?:href|download|src)\s*=\s*['"][^'"]*\.(exe|scr|bat|cmd|msi|vbs|ps1|jar|apk)['"]""",
    re.IGNORECASE,
)


def is_safe_html(code: str):
    match = _BLOCKED_RE.search(code)
    if match:
        return False, f"Blocked pattern: '{match.group()}'"
    dl_match = DANGEROUS_DOWNLOAD_RE.search(code)
    if dl_match:
        return False, f"Blocked: page triggers a download of an executable file ('{dl_match.group()}') — not something FORGE will build regardless of how the brief is framed."
    return True, "OK"


def slugify(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9\-_ ]", "", name)
    name = re.sub(r"\s+", "-", name).strip("-")
    return name or "forge-app"


# ----------------------------------------------------------------------------
# Turning the drawing-pad's structured state into a text brief the model
# can act on: element type, name, and *position* (as a % of canvas size so
# it survives any resolution), plus which named "Icons / Boxes / BG" bucket
# each one belongs to.
# ----------------------------------------------------------------------------
def describe_structure(elements, segments, canvas_w, canvas_h) -> str:
    canvas_w = canvas_w or 1
    canvas_h = canvas_h or 1
    lines = []

    seg_lookup = {}
    for seg_name, items in (segments or {}).items():
        for item in items:
            seg_lookup[item.get("num")] = seg_name

    for el in elements or []:
        num = el.get("num")
        if not num:
            continue
        name = el.get("name") or f"Element {num}"
        etype = el.get("type", "shape")
        bucket = seg_lookup.get(num, el.get("segment") or "unassigned")

        if etype == "instance":
            x, y = el.get("x", 0), el.get("y", 0)
        elif etype == "path" and el.get("path"):
            xs = [p["x"] for p in el["path"]]
            ys = [p["y"] for p in el["path"]]
            x, y = sum(xs) / len(xs), sum(ys) / len(ys)
        elif etype == "circle":
            x, y = el.get("x", 0), el.get("y", 0)
        elif etype in ("line", "rect"):
            x = el.get("x", 0) + el.get("w", 0) / 2
            y = el.get("y", 0) + el.get("h", 0) / 2
        else:
            x, y = el.get("x", 0), el.get("y", 0)

        px = round(100 * x / canvas_w)
        py = round(100 * y / canvas_h)
        region_x = "left" if px < 33 else "center" if px < 66 else "right"
        region_y = "top" if py < 33 else "middle" if py < 66 else "bottom"

        lines.append(
            f'- #{num} "{name}" — type: {etype}, group: {bucket}, '
            f"position: ~{region_y}-{region_x} ({px}%, {py}% of canvas)"
        )

    # Named library items that were never placed on the canvas — still
    # useful as "this should exist somewhere sensible" hints.
    placed_nums = {el.get("num") for el in (elements or []) if el.get("num")}
    for seg_name, items in (segments or {}).items():
        for item in items:
            if item.get("num") not in placed_nums:
                lines.append(f'- #{item.get("num")} "{item.get("name")}" — group: {seg_name} (not placed on canvas)')

    if lines:
        # The drawing pad's canvas can now be taller than one screen (the
        # user scrolls to keep sketching downward) — canvas_h reflects how
        # much of it was actually used. Tell the model roughly how long a
        # page that implies, so a short sketch yields a short page and a
        # long, multi-section sketch yields a proportionally long one.
        BASE_VIEWPORT_H = 800
        if canvas_h > BASE_VIEWPORT_H * 1.3:
            multiplier = round(canvas_h / BASE_VIEWPORT_H, 1)
            lines.insert(
                0,
                f"OVERALL SKETCH LENGTH: the sketch spans roughly {multiplier}x a "
                "standard viewport height of content — build a page with a "
                "proportional amount of vertical content/sections (a long "
                "scrolling page), not a single short screen.\n"
            )

    return "\n".join(lines)


KIND_GUIDANCE = {
    "game": (
        "This is a GAME. It must have an actual playable game loop (state, "
        "input handling, win/lose or scoring conditions, restart), rendered "
        "with <canvas> + requestAnimationFrame or DOM+CSS depending on what "
        "best fits the brief. No placeholder 'coming soon' screens — the "
        "game must be playable start to finish with keyboard and/or "
        "mouse/touch controls."
    ),
    "website": (
        "This is a WEBSITE (multi-section marketing/content site feel). "
        "Include a real nav, hero section, content sections that match the "
        "sketch's regions, and a footer. All internal nav links should "
        "scroll to real sections on the same page (single file, so no "
        "separate pages) unless the brief clearly asks for something else."
    ),
    "frontend": (
        "This is a FRONTEND / APP UI (tool, dashboard, form, utility). "
        "Prioritize working interactivity over decoration: every button, "
        "input, and control sketched must actually do something with real "
        "JS logic and visible state changes — no dead buttons."
    ),
}


# ----------------------------------------------------------------------------
# Style template library — optional ready-made templates the user can pick
# as a visual style reference before forging, or add their own by uploading
# an HTML file. Builtin templates ship in style_templates/manifest.json and
# can't be deleted; user-added ones are appended to the same manifest.
# ----------------------------------------------------------------------------
STYLE_TEMPLATE_MAX_BYTES = 2_000_000        # 2MB cap on an uploaded template
STYLE_TEMPLATE_PROMPT_CHAR_CAP = 16_000     # keep the style-reference block bounded
# (was 60_000 — that alone could add ~15k tokens to the prompt, which by
# itself blew past Groq's free-tier TPM cap on gpt-oss-120b before anything
# else was even counted. 16_000 chars (~4k tokens) still gives the model a
# solid look at the template's colors/type/spacing without dominating the
# request.)


def load_template_manifest() -> list:
    if not STYLE_MANIFEST.exists():
        return []
    try:
        with open(STYLE_MANIFEST, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_template_manifest(manifest: list) -> None:
    with open(STYLE_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def find_template(template_id: str) -> dict | None:
    for entry in load_template_manifest():
        if entry.get("id") == template_id:
            return entry
    return None


def read_template_html(entry: dict) -> str:
    path = STYLE_LIB_DIR / entry["filename"]
    return path.read_text(encoding="utf-8")


def build_style_reference_block(template_entry: dict | None) -> str:
    """Returns the prompt block describing the chosen style template, or ''
    if none was selected. The model is told to draw on it for visual
    language only — colors, type, spacing, component shapes — and never to
    copy its text content, since the brief already supplies the content."""
    if not template_entry:
        return ""
    try:
        html = read_template_html(template_entry)
    except Exception:
        return ""
    if len(html) > STYLE_TEMPLATE_PROMPT_CHAR_CAP:
        html = html[:STYLE_TEMPLATE_PROMPT_CHAR_CAP] + "\n<!-- (truncated) -->"
    return (
        "\n\nSTYLE TEMPLATE REFERENCE "
        f'("{template_entry.get("name", "Untitled")}") — the user picked this '
        "as an optional style reference. Match its visual language: color "
        "palette, typography choices, spacing rhythm, button/card shapes, "
        "and overall mood. Do NOT copy its copy/text content, its specific "
        "business name, or its exact section structure verbatim — the "
        "USER BRIEF above defines what this page is actually about and "
        "what sections it needs; the template below only defines how it "
        "should look:\n\n```html\n" + html + "\n```"
    )


def build_features_block(features: list) -> str:
    """Returns the prompt block listing explicitly-selected feature chips, or
    '' if none were selected. Mirrors build_style_reference_block's style."""
    if not features:
        return ""
    bullet_list = "\n".join(f"- {f}" for f in features)
    return (
        "\n\nREQUIRED FEATURES — the user explicitly selected these and "
        "they MUST all be present and functional in the output (wire up "
        "real working behavior, not just visual placeholders):\n"
        f"{bullet_list}"
    )


def build_animations_block(animations: list) -> str:
    """Returns the prompt block attaching the exact HTML/CSS/JS for each
    selected Animation Template, or '' if none were selected. Unlike
    build_features_block (a checklist), this hands the model working code
    to integrate rather than build from scratch — the mechanism (the CSS
    animation/JS scroll logic) must survive; only the content/copy inside
    it should be adapted to match the page being built."""
    if not animations:
        return ""
    sections = []
    for anim in animations:
        name = anim.get("name") or anim.get("id") or "Animation"
        html = (anim.get("html") or "").strip()
        css = (anim.get("css") or "").strip()
        js = (anim.get("js") or "").strip()
        block = [f'--- "{name}" ---', "HTML:", "```html", html, "```"]
        if css:
            block += ["CSS:", "```css", css, "```"]
        if js:
            block += ["JS:", "```js", js, "```"]
        sections.append("\n".join(block))

    return (
        "\n\nREQUIRED ANIMATION COMPONENT(S) — the user explicitly selected "
        "these ready-made animations and they MUST be integrated into the "
        "output, working exactly as designed (same CSS animation / scroll "
        "logic / timing mechanism intact — do not simplify or drop the "
        "animation itself). You MAY and SHOULD: rewrite the placeholder "
        "text/labels inside them to match this page's real content, adjust "
        "colors to fit the overall design, and place each one at whichever "
        "spot on the page makes sense for what it does (e.g. a marquee near "
        "the top or as a logo strip, a scroll-reveal section wherever there's "
        "a natural pause in the content). Merge the provided CSS into your "
        "stylesheet and the provided JS into your script, keeping class "
        "names and structure intact so the animation mechanism keeps working:\n\n"
        + "\n\n".join(sections)
    )


def build_image_policy_block() -> str:
    """Always-included instructions telling the model how to source any
    <img>/background-image content it needs. Older prompts let the model
    default to https://via.placeholder.com — that domain has been dead/
    flapping (DNS timeouts, connection resets) since 2023-24, which is why
    generated pages ended up with blank boxes where images should be.
    This block replaces it with services that are actually alive, and
    tells the model to key each URL to *that section's own content* so
    every image is distinct rather than repeated generic art.
    Kept short on purpose — this gets appended to EVERY prompt, and a
    long block here eats into the budget weaker fallback models (Groq's
    smaller ones, local Ollama) have for actually following the rest of
    the instructions."""
    return (
        "\n\nIMAGES: never use via.placeholder.com or placeholder.com (both "
        "dead — leave blank boxes). Use, per image, whichever fits: photos → "
        "`https://picsum.photos/seed/<content-slug>/<w>/<h>`; people/avatars → "
        "`https://i.pravatar.cc/<size>?u=<slug>`; text-on-color placeholder → "
        "`https://placehold.co/<w>x<h>/<hex>/<hex>?text=<label>`. Unique slug "
        "per image, real descriptive `alt` on every <img>."
    )


# ----------------------------------------------------------------------------
# Per-section image parser / auto-repair
# ----------------------------------------------------------------------------
# Belt-and-suspenders for build_image_policy_block(): the model is told what
# to do, but coder models (especially the local Ollama/Groq fallbacks) can
# still slip back into via.placeholder.com or leave an <img> with an empty
# src. Rather than a single blanket find/replace, this walks the document
# one image AT A TIME, treating each <img>/background-image as its own
# "section" — it reads that element's own alt text / nearby heading to
# figure out what THAT specific picture is supposed to be, and only touches
# the ones that are actually broken (dead domain, empty, or missing).

DEAD_IMAGE_DOMAINS = (
    "via.placeholder.com",
    "placeholder.com",
    "place-hold.it",
    "placehold.it",
    "lorempixel.com",
    "unsplash.it",
)

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_BG_IMAGE_RE = re.compile(r"background-image\s*:\s*url\((['\"]?)([^'\")]*)\1\)", re.IGNORECASE)
_ATTR_RE = lambda name: re.compile(name + r'\s*=\s*"([^"]*)"|' + name + r"\s*=\s*'([^']*)'", re.IGNORECASE)
_SRC_RE = _ATTR_RE("src")
_ALT_RE = _ATTR_RE("alt")
_CLASS_RE = _ATTR_RE("class")
_HEADING_RE = re.compile(r"<(h[1-4]|figcaption)[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _is_broken_src(src: str) -> bool:
    src = (src or "").strip()
    if not src:
        return True
    if src.startswith("data:"):
        return False
    low = src.lower()
    return any(domain in low for domain in DEAD_IMAGE_DOMAINS)


def _guess_dims_from_class(class_val: str) -> tuple[int, int]:
    """Rough heuristics from Tailwind-style utility classes so a repaired
    avatar stays square-ish and a repaired banner stays wide, instead of
    every replacement defaulting to the same box."""
    c = class_val or ""
    if re.search(r"rounded-full", c):
        m = re.search(r"w-(\d+)", c)
        size = {8: 32, 10: 40, 12: 48, 16: 64, 20: 80, 24: 96, 32: 128}.get(int(m.group(1)), 200) if m else 200
        return size, size
    if re.search(r"h-48|h-56|h-64", c):
        return 800, 500
    return 1200, 700


def _slug_from_text(text: str, fallback: str) -> str:
    text = _TAG_STRIP_RE.sub(" ", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        text = fallback
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or fallback)[:60]


def _nearby_heading(html: str, tag_start: int) -> str:
    """Looks a bit before this image for the closest heading/figcaption so
    a section's own copy (e.g. 'Mastering React Hooks') drives what image
    it gets, instead of a generic 'blog post' seed shared by every card."""
    window = html[max(0, tag_start - 600):tag_start]
    matches = _HEADING_RE.findall(window)
    if matches:
        return matches[-1][1]
    return ""


def _pollinations_url(description: str, width: int, height: int, disambiguator: int = 0) -> str:
    prompt = urllib.parse.quote(description[:200])
    # disambiguator keeps two images with the same alt text (e.g. three
    # generic "Client Avatar" cards) from resolving to the exact same
    # picture — each section still gets its own distinct image.
    seed = int(hashlib.sha1(f"{description}#{disambiguator}".encode("utf-8")).hexdigest(), 16) % 100000
    return f"https://image.pollinations.ai/prompt/{prompt}?width={width}&height={height}&seed={seed}&nologo=true"


def resolve_section_images(html: str) -> tuple[str, int]:
    """Auto-image-generation pass: finds every <img> tag and every CSS
    background-image url(...) individually, and for any that's missing or
    pointing at a dead placeholder host, generates a real image for it
    on the spot (via Pollinations text-to-image, keyed to that element's
    own alt text / nearby heading) so the section is never left blank.
    Returns (patched_html, count_fixed)."""
    fixed = 0
    seen_descriptions: dict[str, int] = {}

    def next_disambiguator(description: str) -> int:
        n = seen_descriptions.get(description, 0)
        seen_descriptions[description] = n + 1
        return n

    def fix_img_tag(m: "re.Match") -> str:
        nonlocal fixed
        tag = m.group(0)
        src_m = _SRC_RE.search(tag)
        src = next((g for g in (src_m.groups() if src_m else ()) if g is not None), "")
        if not _is_broken_src(src):
            return tag
        alt_m = _ALT_RE.search(tag)
        alt = next((g for g in (alt_m.groups() if alt_m else ()) if g is not None), "")
        class_m = _CLASS_RE.search(tag)
        class_val = next((g for g in (class_m.groups() if class_m else ()) if g is not None), "")
        heading = _nearby_heading(html, m.start())
        description = alt or heading or "website content photo"
        w, h = _guess_dims_from_class(class_val)
        new_url = _pollinations_url(description, w, h, next_disambiguator(description))
        fixed += 1
        if src_m:
            return tag[:src_m.start()] + f'src="{new_url}"' + tag[src_m.end():]
        return tag[:4] + f' src="{new_url}"' + tag[4:]

    html = _IMG_TAG_RE.sub(fix_img_tag, html)

    def fix_bg(m: "re.Match") -> str:
        nonlocal fixed
        url = m.group(2)
        if not _is_broken_src(url):
            return m.group(0)
        # background-image has no alt text of its own — fall back to the
        # nearest heading text before it, same as the <img> path above.
        heading = _nearby_heading(html, m.start())
        description = heading or "website section background photo"
        new_url = _pollinations_url(description, 1200, 700, next_disambiguator(description))
        fixed += 1
        return f"background-image: url('{new_url}')"

    html = _BG_IMAGE_RE.sub(fix_bg, html)

    return html, fixed


def build_instructions(prompt_text: str, kind: str, structure_notes: str, has_sketch: bool, template_entry: dict | None = None, features: list | None = None, animations: list | None = None, uploaded_images: list | None = None) -> str:
    kind = kind if kind in KIND_GUIDANCE else "website"
    guidance = KIND_GUIDANCE[kind]

    parts = [
        "You are Pragon, an expert full-stack frontend engineer inside the "
        "C.U.S.T.O.M FORGE builder. You turn a rough sketch + a text brief "
        "into ONE complete, production-quality, self-contained HTML file.",
        "",
        f"OUTPUT TYPE: {kind.upper()}",
        guidance,
        "",
        f"USER BRIEF: {prompt_text}",
    ]

    if structure_notes:
        parts += [
            "",
            "STRUCTURE FROM THE DRAWING PAD (element name / type / group / "
            "approximate position, as sketched by the user — use this to "
            "decide layout regions, naming, and what each element should "
            "become, but improve on the raw geometry):",
            structure_notes,
        ]

    if has_sketch:
        parts += [
            "",
            "A screenshot of the rough sketch/wireframe is attached as an "
            "image. Use it only as a loose layout guide for regions and "
            "relative positioning — do not literally trace pixel "
            "coordinates, and do not reproduce it as an image, build real "
            "HTML/CSS structure instead.",
        ]

    if template_entry:
        parts.append(build_style_reference_block(template_entry))

    if features:
        parts.append(build_features_block(features))

    if animations:
        parts.append(build_animations_block(animations))

    parts.append(build_image_policy_block())

    if uploaded_images:
        parts.append(build_uploaded_images_block(uploaded_images))

    parts += [
        "",
        "HARD REQUIREMENTS:",
        "- Output ONLY raw HTML. No explanation, no markdown, no backticks, "
        "nothing before <!DOCTYPE html> and nothing after </html>.",
        "- The file MUST start with <!DOCTYPE html> and be fully "
        "self-contained: all CSS inside a single <style> tag, all JS inside "
        "a single <script> tag.",
        "- No external CDN links, no external fonts requiring network calls "
        "beyond Google Fonts (optional), no external API calls requiring "
        "an API key.",
        "- No Node.js APIs (require, fs, child_process) — this runs in a "
        "plain browser tab.",
        "- It must actually work end-to-end with zero console errors: "
        "every interactive element sketched or described must be wired up "
        "with real, working JavaScript.",
        "- Visually polished: consistent spacing, readable typography, a "
        "deliberate color scheme, responsive layout down to mobile widths.",
        "",
        "SAFETY — the ONLY reason to refuse: the brief itself explicitly "
        "asks for something genuinely dangerous or abusive to build — "
        "malware/exploit code, phishing or credential-harvesting pages, "
        "instructions for weapons or drugs, or sexual content involving "
        "minors. This includes a page whose actual mechanism is a lure to "
        "get a visitor to download a real or fake executable/virus — e.g. "
        "a 'Download Virus' button, a fake antivirus/update page, or any "
        "download wired to an .exe/.scr/.bat/.msi/.apk — refuse this even "
        "if the brief frames it as a joke, a 'dangerous theme', a prank, "
        "or a design exercise; the danger is in the working mechanism, not "
        "the label on it. An ordinary business site, portfolio, game, "
        "dashboard, landing page, or app UI is NEVER unsafe, no matter how "
        "unusual the theme or name sounds — build it. If (and only if) the "
        "brief truly falls in the dangerous list above, respond with "
        "EXACTLY and ONLY the single word UNSAFE and nothing else. "
        "Otherwise, always build the page — do not use the word UNSAFE "
        "anywhere in a normal response.",
        "",
        "Now output the complete file, starting immediately with "
        "<!DOCTYPE html>:",
    ]
    return "\n".join(parts)


# ----------------------------------------------------------------------------
# Full generation from a brief (+ optional sketch) — builds the prompt and
# hands it off to run_completion() below.
# ----------------------------------------------------------------------------
def generate_code(prompt_text: str, kind: str, structure_notes: str, image_bytes: bytes | None, template_entry: dict | None = None, features: list | None = None, animations: list | None = None, uploaded_images: list | None = None) -> tuple[str, str, str | None]:
    instructions = build_instructions(prompt_text, kind, structure_notes, image_bytes is not None, template_entry, features, animations, uploaded_images)

    content = []
    if image_bytes:
        content.append({"mime_type": "image/png", "data": image_bytes})
    content.append(instructions)

    return run_completion(content, prompt_text=instructions, image_bytes=image_bytes)


def generate_code_stream(prompt_text: str, kind: str, structure_notes: str, image_bytes: bytes | None = None, template_entry: dict | None = None, features: list | None = None, animations: list | None = None, uploaded_images: list | None = None):
    """Streaming twin of generate_code(): builds the same instructions, then
    hands off to run_completion_stream() so the route can forward chunks to
    the browser as they're generated instead of waiting for the full file."""
    instructions = build_instructions(prompt_text, kind, structure_notes, image_bytes is not None, template_entry, features, animations, uploaded_images)

    content = []
    if image_bytes:
        content.append({"mime_type": "image/png", "data": image_bytes})
    content.append(instructions)

    yield from run_completion_stream(content, prompt_text=instructions, image_bytes=image_bytes)


# ----------------------------------------------------------------------------
# Targeted element edit — used by the "select an element in the preview and
# describe a change" flow. Same one-file-out contract as generate_code, but
# the prompt is scoped to a single element inside the existing document
# instead of building something from scratch.
# ----------------------------------------------------------------------------
def build_edit_instructions(instruction: str, element_html: str, full_code: str) -> str:
    parts = [
        "You are Pragon, an expert frontend engineer inside the C.U.S.T.O.M "
        "FORGE builder. The user already has a complete, working, "
        "self-contained HTML file open. They selected ONE element in it "
        "(shown below) and typed an instruction describing a change they "
        "want made to that element.",
        "",
        "SELECTED ELEMENT (exact HTML, taken from the live DOM — use this "
        "to find the matching element in the full file below; note it may "
        "carry inline style attributes from earlier manual edits):",
        element_html,
        "",
        f"USER INSTRUCTION FOR THIS ELEMENT: {instruction}",
        "",
        "FULL CURRENT FILE:",
        full_code,
        "",
        "HARD REQUIREMENTS:",
        "- Make the requested change to the selected element (and, only if "
        "strictly necessary to fulfill the instruction — e.g. adding a new "
        "sibling element, wiring a new event handler — touch the minimum "
        "surrounding code needed). Do not rewrite, restyle, or restructure "
        "any other part of the page.",
        "- Preserve everything else in the file exactly as-is: other "
        "elements, styles, scripts, structure, and content.",
        "- Output ONLY the complete, updated, raw HTML file. No "
        "explanation, no markdown, no backticks, nothing before "
        "<!DOCTYPE html> and nothing after </html>.",
        "- It must still be one self-contained file (CSS in one <style>, "
        "JS in one <script>) with zero console errors.",
        "",
        "SAFETY — the ONLY reason to refuse: the instruction explicitly "
        "asks for something genuinely dangerous or abusive (malware, "
        "phishing, weapons/drug instructions, sexual content involving "
        "minors), including turning an element into a working malware/virus "
        "download lure even if framed as a joke or theme. An ordinary "
        "style/copy/behavior tweak is NEVER unsafe — make it. Only if the "
        "instruction truly falls in the dangerous list above, respond with "
        "EXACTLY and ONLY the single word UNSAFE. Otherwise never use that "
        "word.",
        "",
        "Now output the complete updated file, starting immediately with "
        "<!DOCTYPE html>:",
    ]
    return "\n".join(parts)


def generate_edit(instruction: str, element_html: str, full_code: str) -> tuple[str, str, str | None]:
    instructions = build_edit_instructions(instruction, element_html, full_code)
    content = [instructions]
    return run_completion(content, prompt_text=instructions, image_bytes=None)


# ----------------------------------------------------------------------------
# Gemini call, with automatic continuation if the model gets cut off by
# the token limit (mirrors the continuation loop the original UI tried to
# do client-side, but done safely on the server with the real key).
# ----------------------------------------------------------------------------
def run_completion_gemini(content: list) -> str:
    import google.generativeai as genai

    configure_genai()
    model = genai.GenerativeModel("gemini-2.5-flash")

    generation_config = {"max_output_tokens": 8192, "temperature": 0.6}

    full_text = ""
    chat_history = [{"role": "user", "parts": content}]
    MAX_CONTINUATIONS = 6

    for i in range(MAX_CONTINUATIONS):
        response = model.generate_content(chat_history, generation_config=generation_config)

        chunk = (response.text or "").strip() if hasattr(response, "text") else ""
        if not chunk and response.candidates:
            chunk = "".join(
                part.text for part in response.candidates[0].content.parts if hasattr(part, "text")
            ).strip()

        full_text += ("\n" if full_text and chunk else "") + chunk

        finish_reason = None
        if response.candidates:
            finish_reason = getattr(response.candidates[0], "finish_reason", None)
            finish_reason = str(finish_reason)

        truncated = finish_reason is not None and ("MAX_TOKENS" in finish_reason or finish_reason == "2")
        if not truncated:
            break

        chat_history.append({"role": "model", "parts": [chunk]})
        chat_history.append({
            "role": "user",
            "parts": ["Continue exactly where you left off. Do not repeat any earlier "
                      "text, do not add commentary or markdown fences — resume the raw "
                      "HTML mid-stream."]
        })

    return full_text.strip()


def run_completion_gemini_stream(content: list):
    """Streaming twin of run_completion_gemini(): yields text pieces as
    Gemini produces them instead of returning one final string. Same
    continuation-on-truncation loop, just driven token-by-token so the
    caller (the SSE route) can forward each piece to the browser as it
    arrives instead of waiting for the whole file."""
    import google.generativeai as genai

    configure_genai()
    model = genai.GenerativeModel("gemini-2.5-flash")
    generation_config = {"max_output_tokens": 8192, "temperature": 0.6}

    chat_history = [{"role": "user", "parts": content}]
    MAX_CONTINUATIONS = 6

    for i in range(MAX_CONTINUATIONS):
        response = model.generate_content(chat_history, generation_config=generation_config, stream=True)

        pieces_this_round: list[str] = []
        finish_reason = None
        for event in response:
            piece = getattr(event, "text", None) if hasattr(event, "text") else None
            if not piece and getattr(event, "candidates", None):
                piece = "".join(
                    part.text for part in event.candidates[0].content.parts if hasattr(part, "text")
                )
            if piece:
                pieces_this_round.append(piece)
                yield piece
            if getattr(event, "candidates", None):
                fr = getattr(event.candidates[0], "finish_reason", None)
                if fr is not None:
                    finish_reason = str(fr)

        truncated = finish_reason is not None and ("MAX_TOKENS" in finish_reason or finish_reason == "2")
        if not truncated:
            return

        chat_history.append({"role": "model", "parts": ["".join(pieces_this_round)]})
        chat_history.append({
            "role": "user",
            "parts": ["Continue exactly where you left off. Do not repeat any earlier "
                      "text, do not add commentary or markdown fences — resume the raw "
                      "HTML mid-stream."]
        })


# ----------------------------------------------------------------------------
# Groq fallback — used automatically when Gemini is out of quota / rate
# limited. Groq's chat-completions API is OpenAI-compatible.
#
# Model choice: Groq's lineup changes often (see console.groq.com/docs/
# deprecations), so this picks at call time rather than hardcoding one name
# that might get retired later:
#   - text-only brief  -> "openai/gpt-oss-120b"   (fast, strong, no vision)
#   - brief + sketch   -> "qwen/qwen3.6-27b"       (Groq's current vision
#                          model as of mid-2026 — marked preview by Groq,
#                          so treat it as good-enough, not guaranteed-stable)
# ----------------------------------------------------------------------------
def run_completion_groq(prompt_text: str, image_bytes: bytes | None) -> str:
    from groq import Groq

    key = get_groq_key()
    if not key:
        raise RuntimeError(
            "Gemini is out of quota and no Groq fallback is configured. Add "
            '"groq_api_key" to config/api_keys.json (get one free at '
            "https://console.groq.com/keys) to enable automatic fallback."
        )
    client = Groq(api_key=key)

    if image_bytes:
        model_name = "qwen/qwen3.6-27b"
        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
    else:
        # NOTE: llama-3.3-70b-versatile was deprecated by Groq (June 17,
        # 2026) and now 404s outright — openai/gpt-oss-120b is Groq's own
        # recommended replacement. Its free tier is only 8,000 TPM though
        # (see max_tokens_per_call below and the prompt-size guard in
        # generate_code / run_completion), so this only actually succeeds
        # when prompt + reserved output together stay under that.
        model_name = "openai/gpt-oss-120b"
        user_content = prompt_text

    messages = [{"role": "user", "content": user_content}]
    full_text = ""
    MAX_CONTINUATIONS = 6
    # Lowered from 8192: keeps prompt + reserved output comfortably under the
    # TPM cap above. The continuation loop below just does one more round
    # trip if a single call isn't enough to finish the file.
    max_tokens_per_call = 3500 if not image_bytes else 8192

    for i in range(MAX_CONTINUATIONS):
        completion = client.chat.completions.create(
            model=model_name,
            messages=messages,
            max_tokens=max_tokens_per_call,
            temperature=0.6,
        )
        choice = completion.choices[0]
        chunk = (choice.message.content or "").strip()
        full_text += ("\n" if full_text and chunk else "") + chunk

        if choice.finish_reason != "length":
            break

        messages.append({"role": "assistant", "content": chunk})
        messages.append({
            "role": "user",
            "content": "Continue exactly where you left off. Do not repeat any "
                       "earlier text, do not add commentary or markdown fences — "
                       "resume the raw HTML mid-stream.",
        })

    return full_text.strip()


def run_completion_groq_stream(prompt_text: str, image_bytes: bytes | None):
    """Streaming twin of run_completion_groq() — same model selection and
    continuation logic, but yields each delta chunk as Groq streams it back
    instead of building one final string."""
    from groq import Groq

    key = get_groq_key()
    if not key:
        raise RuntimeError(
            "Gemini is out of quota and no Groq fallback is configured. Add "
            '"groq_api_key" to config/api_keys.json (get one free at '
            "https://console.groq.com/keys) to enable automatic fallback."
        )
    client = Groq(api_key=key)

    if image_bytes:
        model_name = "qwen/qwen3.6-27b"
        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
    else:
        model_name = "openai/gpt-oss-120b"
        user_content = prompt_text

    messages = [{"role": "user", "content": user_content}]
    MAX_CONTINUATIONS = 6
    max_tokens_per_call = 3500 if not image_bytes else 8192

    for i in range(MAX_CONTINUATIONS):
        stream = client.chat.completions.create(
            model=model_name,
            messages=messages,
            max_tokens=max_tokens_per_call,
            temperature=0.6,
            stream=True,
        )

        pieces_this_round: list[str] = []
        finish_reason = None
        for event in stream:
            choice = event.choices[0]
            piece = getattr(choice.delta, "content", None)
            if piece:
                pieces_this_round.append(piece)
                yield piece
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        if finish_reason != "length":
            return

        messages.append({"role": "assistant", "content": "".join(pieces_this_round)})
        messages.append({
            "role": "user",
            "content": "Continue exactly where you left off. Do not repeat any "
                       "earlier text, do not add commentary or markdown fences — "
                       "resume the raw HTML mid-stream.",
        })


# ----------------------------------------------------------------------------
# OpenRouter fallback — third tier, tried after Groq and before local Ollama.
# Uses the "openrouter/free" auto-router rather than a hardcoded model name
# on purpose: OpenRouter's individual free-model lineup churns frequently
# (models get delisted/replaced with little notice — the exact same failure
# mode that hit the hardcoded Groq model above), so pointing at the
# auto-router means this keeps working even as the underlying free model
# rotates, instead of silently 404ing again down the road. OpenAI-compatible
# REST API, no SDK needed.
# ----------------------------------------------------------------------------
def run_completion_openrouter(prompt_text: str, image_bytes: bytes | None) -> str:
    import requests

    key = get_openrouter_key()
    if not key:
        raise RuntimeError(
            "Gemini and Groq are both unavailable and no OpenRouter fallback "
            'is configured. Add "openrouter_api_key" to config/api_keys.json '
            "(get one free, no card required, at https://openrouter.ai/keys) "
            "to enable this fallback tier."
        )

    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
    else:
        user_content = prompt_text

    messages = [{"role": "user", "content": user_content}]
    full_text = ""
    MAX_CONTINUATIONS = 6

    for i in range(MAX_CONTINUATIONS):
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openrouter/free",
                "messages": messages,
                "max_tokens": 4096,
                "temperature": 0.6,
            },
            timeout=120,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenRouter returned HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        choice = data["choices"][0]
        chunk = (choice["message"].get("content") or "").strip()
        full_text += ("\n" if full_text and chunk else "") + chunk

        if choice.get("finish_reason") != "length":
            break

        messages.append({"role": "assistant", "content": chunk})
        messages.append({
            "role": "user",
            "content": "Continue exactly where you left off. Do not repeat any "
                       "earlier text, do not add commentary or markdown fences — "
                       "resume the raw HTML mid-stream.",
        })

    return full_text.strip()


def run_completion_openrouter_stream(prompt_text: str, image_bytes: bytes | None):
    """Streaming twin of run_completion_openrouter(). OpenRouter's REST API
    is OpenAI-compatible, so "stream": true gets back a text/event-stream
    response of `data: {...}` lines (one JSON chunk per line, terminated by
    a literal `data: [DONE]`) rather than one JSON object — this reads that
    with requests' streaming mode instead of a single .json() call."""
    import requests

    key = get_openrouter_key()
    if not key:
        raise RuntimeError(
            "Gemini and Groq are both unavailable and no OpenRouter fallback "
            'is configured. Add "openrouter_api_key" to config/api_keys.json '
            "(get one free, no card required, at https://openrouter.ai/keys) "
            "to enable this fallback tier."
        )

    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
    else:
        user_content = prompt_text

    messages = [{"role": "user", "content": user_content}]
    MAX_CONTINUATIONS = 6

    for i in range(MAX_CONTINUATIONS):
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openrouter/free",
                "messages": messages,
                "max_tokens": 4096,
                "temperature": 0.6,
                "stream": True,
            },
            timeout=120,
            stream=True,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenRouter returned HTTP {resp.status_code}: {resp.text[:300]}")

        pieces_this_round: list[str] = []
        finish_reason = None
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choice = (event.get("choices") or [{}])[0]
            piece = (choice.get("delta") or {}).get("content") or ""
            if piece:
                pieces_this_round.append(piece)
                yield piece
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

        if finish_reason != "length":
            return

        messages.append({"role": "assistant", "content": "".join(pieces_this_round)})
        messages.append({
            "role": "user",
            "content": "Continue exactly where you left off. Do not repeat any "
                       "earlier text, do not add commentary or markdown fences — "
                       "resume the raw HTML mid-stream.",
        })


# ----------------------------------------------------------------------------
# Local fallback — Ollama running Qwen2.5-Coder. Fourth tier: free, no rate
# limits, no network dependency at all, but CPU-only inference is slow and
# the coder models don't do vision, so this only ever gets the text prompt
# (structure_notes already carries the sketch's layout info in words even
# when the raw image can't be sent).
# ----------------------------------------------------------------------------
def run_completion_ollama(prompt_text: str) -> str:
    import requests

    cfg = get_ollama_config()
    host, model = cfg["host"], cfg["model"]

    print(f"[forge] Using Ollama at {host} with model {model}")
    print(f"[forge] Prompt size: {len(prompt_text)} characters")

    # BUGFIX: Ollama defaults num_ctx to 2048 tokens if it isn't passed
    # explicitly — regardless of what the model itself supports. Our prompts
    # here can easily run 5-10k+ tokens once a style template, several
    # animation components, and a feature checklist are attached, so without
    # this the request was being silently truncated before the model ever
    # saw most of it (which is exactly what produced near-empty boilerplate
    # output instead of anything resembling the brief). Size num_ctx to the
    # actual prompt instead of guessing a fixed number, with headroom for
    # the reply, capped so it doesn't demand more RAM than a typical machine
    # has for a CPU-only model.
    est_input_tokens = len(prompt_text) // 4
    num_ctx = min(32768, max(8192, est_input_tokens + 6144))
    print(f"[forge] Estimated input tokens: ~{est_input_tokens}, using num_ctx={num_ctx}")

    messages = [{"role": "user", "content": prompt_text}]
    full_text = ""
    MAX_CONTINUATIONS = 6

    for continuation_idx in range(MAX_CONTINUATIONS):
        try:
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                # num_predict caps output per call, same role max_tokens
                # plays for Gemini/Groq above; CPU inference is slow so
                # this keeps any single call bounded. num_ctx (see above)
                # is the actual fix — without it the input itself was
                # getting cut off, not just the output.
                "options": {"num_predict": 4096, "temperature": 0.6, "num_ctx": num_ctx},
            }

            payload_json = json.dumps(payload)
            payload_size_kb = len(payload_json) / 1024
            print(f"[forge] Ollama request #{continuation_idx + 1}: {payload_size_kb:.1f} KB")

            resp = requests.post(
                f"{host}/api/chat",
                json=payload,
                timeout=600,  # 10 minute timeout for slow CPU
            )

            print(f"[forge] Ollama response: HTTP {resp.status_code}")

            if resp.status_code >= 400:
                error_text = resp.text[:500]
                print(f"[forge] HTTP error body: {error_text}")
                resp.raise_for_status()

        except requests.exceptions.Timeout:
            raise RuntimeError(
                f"Ollama request timed out after 600 seconds. The model {model} "
                f"is taking too long to generate a response (possibly CPU-bound). "
                f"Try with a smaller model (e.g., qwen2.5-coder:1.5b) or use a GPU."
            )
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                f"All three providers are unavailable: Gemini and Groq are out, "
                f"and Ollama isn't reachable at {host}. Install Ollama "
                f"(https://ollama.com), run `ollama pull {model}`, and make sure "
                f"`ollama serve` is running, or configure a different "
                f'"ollama_host" in config/api_keys.json.'
            ) from e
        except requests.exceptions.HTTPError as e:
            body_preview = resp.text[:300] if hasattr(e, 'response') and e.response else "unknown"
            if "not found" in body_preview.lower() or resp.status_code == 404:
                raise RuntimeError(
                    f'Model "{model}" isn\'t pulled in Ollama yet. Run '
                    f"`ollama pull {model}` (or set a different \"ollama_model\" "
                    f"in config/api_keys.json) and try again."
                ) from e
            elif resp.status_code == 400:
                raise RuntimeError(
                    f"Bad request to Ollama (400). This might mean the prompt is "
                    f"malformed or too large. Response: {body_preview}"
                ) from e
            else:
                raise RuntimeError(
                    f"Ollama returned HTTP {resp.status_code}: {body_preview}"
                ) from e
        except Exception as e:
            raise RuntimeError(f"Ollama request failed: {e}") from e

        try:
            payload_resp = resp.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Ollama returned invalid JSON. Response: {resp.text[:300]}"
            ) from e

        try:
            chunk = ((payload_resp.get("message") or {}).get("content") or "").strip()
        except (KeyError, TypeError, AttributeError) as e:
            raise RuntimeError(
                f"Unexpected Ollama response format: {payload_resp}"
            ) from e

        if chunk:
            full_text += ("\n" if full_text else "") + chunk
            print(f"[forge] Got {len(chunk)} chars from Ollama")

        done_reason = payload_resp.get("done_reason", "")
        if done_reason != "length":
            print(f"[forge] Ollama done (reason: {done_reason or 'stop'})")
            break

        print("[forge] Continuing (hit token limit)...")
        messages.append({"role": "assistant", "content": chunk})
        messages.append({
            "role": "user",
            "content": "Continue exactly where you left off. Do not repeat any "
                       "earlier text, do not add commentary or markdown fences — "
                       "resume the raw HTML mid-stream.",
        })

    print(f"[forge] Ollama generation complete: {len(full_text)} total characters")

    return full_text.strip()


def run_completion_ollama_stream(prompt_text: str):
    """Streaming twin of run_completion_ollama(). Ollama's /api/chat streams
    newline-delimited JSON objects (one per token/small batch) when
    "stream": true — each has a partial message.content and, on the final
    line, "done": true plus a "done_reason". Same num_ctx sizing fix and
    continuation loop as the non-streaming version, just forwarding each
    piece as it's read instead of collecting one final string."""
    import requests

    cfg = get_ollama_config()
    host, model = cfg["host"], cfg["model"]

    est_input_tokens = len(prompt_text) // 4
    num_ctx = min(32768, max(8192, est_input_tokens + 6144))
    print(f"[forge] Using Ollama at {host} with model {model} (streaming, num_ctx={num_ctx})")

    messages = [{"role": "user", "content": prompt_text}]
    MAX_CONTINUATIONS = 6

    for continuation_idx in range(MAX_CONTINUATIONS):
        try:
            resp = requests.post(
                f"{host}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "options": {"num_predict": 4096, "temperature": 0.6, "num_ctx": num_ctx},
                },
                timeout=600,
                stream=True,
            )
            if resp.status_code >= 400:
                resp.raise_for_status()
        except requests.exceptions.Timeout:
            raise RuntimeError(
                f"Ollama request timed out after 600 seconds. The model {model} "
                f"is taking too long to generate a response (possibly CPU-bound). "
                f"Try with a smaller model (e.g., qwen2.5-coder:1.5b) or use a GPU."
            )
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                f"All three providers are unavailable: Gemini and Groq are out, "
                f"and Ollama isn't reachable at {host}. Install Ollama "
                f"(https://ollama.com), run `ollama pull {model}`, and make sure "
                f"`ollama serve` is running, or configure a different "
                f'"ollama_host" in config/api_keys.json.'
            ) from e
        except requests.exceptions.HTTPError as e:
            body_preview = resp.text[:300] if hasattr(e, 'response') and e.response else "unknown"
            if "not found" in body_preview.lower() or resp.status_code == 404:
                raise RuntimeError(
                    f'Model "{model}" isn\'t pulled in Ollama yet. Run '
                    f"`ollama pull {model}` (or set a different \"ollama_model\" "
                    f"in config/api_keys.json) and try again."
                ) from e
            raise RuntimeError(f"Ollama returned HTTP {resp.status_code}: {body_preview}") from e
        except Exception as e:
            raise RuntimeError(f"Ollama request failed: {e}") from e

        pieces_this_round: list[str] = []
        done_reason = "stop"
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                payload_resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            piece = ((payload_resp.get("message") or {}).get("content") or "")
            if piece:
                pieces_this_round.append(piece)
                yield piece
            if payload_resp.get("done"):
                done_reason = payload_resp.get("done_reason", "stop")

        if done_reason != "length":
            return

        print("[forge] Continuing (hit token limit)...")
        messages.append({"role": "assistant", "content": "".join(pieces_this_round)})
        messages.append({
            "role": "user",
            "content": "Continue exactly where you left off. Do not repeat any "
                       "earlier text, do not add commentary or markdown fences — "
                       "resume the raw HTML mid-stream.",
        })


def run_completion_stream(content: list, prompt_text: str = "", image_bytes: bytes | None = None):
    """Streaming twin of run_completion(). Same four-tier fallback chain
    (Gemini -> Groq -> OpenRouter -> Ollama) and the same rule for when a
    failure is allowed to fall through (Gemini only falls through on a
    quota/rate-limit/bad-key error — see is_gemini_capacity_error; Groq and
    OpenRouter fall through on *any* failure; Ollama is the last resort).

    Because this streams, a provider can fail *after* it has already sent
    some chunks to the browser (e.g. Groq accepts the request but then the
    connection drops mid-generation). There's no way to "unsend" bytes the
    browser already rendered, so on any mid-stream failure this yields a
    provider_switch event telling the caller to discard whatever it has
    buffered from that provider and start clean with the next one, rather
    than silently splicing two different models' output together.

    Yields dicts:
      {"type": "provider", "name": ...}                    - about to try this provider
      {"type": "chunk", "text": ...}                        - a piece of generated text
      {"type": "provider_switch", "from": ..., "to": ..., "reason": ...}
                                                              - discard buffer, provider changed
      {"type": "done", "provider": ..., "warning": ...}      - this provider finished cleanly
      {"type": "error", "error": ...}                        - nothing left to fall back to
    """
    gemini_reason = groq_reason = or_reason = None

    yield {"type": "provider", "name": "gemini"}
    try:
        for piece in run_completion_gemini_stream(content):
            yield {"type": "chunk", "text": piece}
        yield {"type": "done", "provider": "gemini", "warning": None}
        return
    except Exception as e_gemini:
        if not is_gemini_capacity_error(e_gemini):
            yield {"type": "error", "error": str(e_gemini)}
            return
        gemini_reason = str(e_gemini).strip().splitlines()[0][:200]
        print(f"[forge] Gemini unavailable ({e_gemini}); falling back to Groq")
        yield {"type": "provider_switch", "from": "gemini", "to": "groq", "reason": gemini_reason}

    try:
        for piece in run_completion_groq_stream(prompt_text or "", image_bytes):
            yield {"type": "chunk", "text": piece}
        yield {
            "type": "done", "provider": "groq",
            "warning": f"Gemini unavailable ({gemini_reason}) — used Groq instead.",
        }
        return
    except Exception as e_groq:
        groq_reason = str(e_groq).strip().splitlines()[0][:200]
        print(f"[forge] Groq unavailable ({e_groq}); falling back to OpenRouter")
        yield {"type": "provider_switch", "from": "groq", "to": "openrouter", "reason": groq_reason}

    try:
        for piece in run_completion_openrouter_stream(prompt_text or "", image_bytes):
            yield {"type": "chunk", "text": piece}
        yield {
            "type": "done", "provider": "openrouter",
            "warning": f"Gemini unavailable ({gemini_reason}) and Groq unavailable "
                       f"({groq_reason}) — used OpenRouter instead.",
        }
        return
    except Exception as e_or:
        or_reason = str(e_or).strip().splitlines()[0][:200]
        print(f"[forge] OpenRouter unavailable ({e_or}); falling back to local Ollama")
        yield {"type": "provider_switch", "from": "openrouter", "to": "ollama", "reason": or_reason}

    try:
        for piece in run_completion_ollama_stream(prompt_text or ""):
            yield {"type": "chunk", "text": piece}
        yield {
            "type": "done", "provider": "ollama",
            "warning": f"Gemini unavailable ({gemini_reason}), Groq unavailable ({groq_reason}), "
                       f"and OpenRouter unavailable ({or_reason}) — fell back to the local model, "
                       "which is much weaker and may not follow every feature/animation/style "
                       "selection on a heavy brief. Check your Gemini quota/key, Groq key, and "
                       "OpenRouter key to get full-quality output back.",
        }
    except Exception as e_ollama:
        yield {
            "type": "error",
            "error": f"All four providers are unavailable. Gemini: {gemini_reason}. "
                     f"Groq: {groq_reason}. OpenRouter: {or_reason}. Ollama: {e_ollama}",
        }


def run_completion(content: list, prompt_text: str = "", image_bytes: bytes | None = None) -> tuple[str, str, str | None]:
    """Four tiers, in order: Gemini -> Groq -> OpenRouter -> local Ollama.

    - Gemini is tried first; any quota/rate-limit error (not a bad key or
      safety block — those raise immediately) falls through to Groq.
    - Groq is tried next. Any failure there at all — not configured, its
      own rate limit, a network hiccup — falls through to OpenRouter.
    - OpenRouter is tried next, using the "openrouter/free" auto-router
      (not a hardcoded model — see run_completion_openrouter's docstring
      for why) so a large prompt that blew Groq's TPM cap still has a
      real shot before dropping all the way to the local model.
    - Ollama is the last resort: local, no key, no rate limit, but slower
      on CPU and (for the coder models) text-only, so the sketch image is
      dropped at this tier — only prompt_text (which already includes the
      structure_notes description of the sketch) is sent. It's also by far
      the weakest model of the four, so heavy briefs (lots of features/
      animations/a style template) may come out thinner than the others
      would produce — that's a real quality ceiling, not a bug.

    Returns (code, provider_used, warning) — warning is None when Gemini
    answered directly, otherwise a short human-readable string explaining
    why it didn't, so the UI can surface it instead of it living only in
    the server console.
    """
    try:
        return run_completion_gemini(content), "gemini", None
    except Exception as e_gemini:
        if not is_gemini_capacity_error(e_gemini):
            raise
        gemini_reason = str(e_gemini).strip().splitlines()[0][:200]
        print(f"[forge] Gemini unavailable ({e_gemini}); falling back to Groq")

        try:
            code = run_completion_groq(prompt_text or "", image_bytes)
            return code, "groq", f"Gemini unavailable ({gemini_reason}) — used Groq instead."
        except Exception as e_groq:
            groq_reason = str(e_groq).strip().splitlines()[0][:200]
            print(f"[forge] Groq unavailable ({e_groq}); falling back to OpenRouter")

            try:
                code = run_completion_openrouter(prompt_text or "", image_bytes)
                return (
                    code,
                    "openrouter",
                    f"Gemini unavailable ({gemini_reason}) and Groq unavailable "
                    f"({groq_reason}) — used OpenRouter instead.",
                )
            except Exception as e_or:
                or_reason = str(e_or).strip().splitlines()[0][:200]
                print(f"[forge] OpenRouter unavailable ({e_or}); falling back to local Ollama")
                if image_bytes:
                    print("[forge] Note: local model has no vision support — the "
                          "sketch image is dropped, only the text brief + structure "
                          "notes are sent.")
                code = run_completion_ollama(prompt_text or "")
                return (
                    code,
                    "ollama",
                    f"Gemini unavailable ({gemini_reason}), Groq unavailable ({groq_reason}), "
                    f"and OpenRouter unavailable ({or_reason}) — fell back to the local model, "
                    "which is much weaker and may not follow every feature/animation/style "
                    "selection on a heavy brief. Check your Gemini quota/key, Groq key, and "
                    "OpenRouter key to get full-quality output back."
                )


def clean_code(raw: str) -> str:
    code = raw.strip()
    code = re.sub(r"^```(?:html)?\s*", "", code)
    code = re.sub(r"```\s*$", "", code)
    return code.strip()


# ----------------------------------------------------------------------------
# Post-generation validate → fix loop.
#
# Inspired by dev_agent.py's write-run-fix cycle for multi-file Python
# projects, adapted to what PRAGON actually produces: one self-contained
# HTML file with inline JS, not something with a subprocess and stderr to
# read. "Running" it isn't meaningful server-side (it's client-rendered),
# so this substitutes static validation for real execution:
#   - is the file actually complete, or did it get cut off mid-stream?
#   - does each inline <script> block actually parse as valid JS?
#     (uses `node --check` when Node is on PATH for a real parser; falls
#     back to a brace/bracket/paren balance heuristic when it isn't —
#     that catches the same class of "model dropped a closing brace"
#     errors dev_agent would catch by literally running the file and
#     reading the traceback, just without needing Node installed)
#
# When validate_html_output() finds something, fix_html_output() builds a
# dev_agent-style fix prompt (broken file + specific issues + "return the
# complete corrected file") and sends it back through the same provider
# fallback chain used for the original generation.
# ----------------------------------------------------------------------------
def _extract_inline_scripts(html: str) -> list[str]:
    """Inline <script>...</script> blocks only — skips <script src="...">
    since there's nothing local to check there."""
    scripts = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE):
        body = m.group(1).strip()
        if body:
            scripts.append(body)
    return scripts


def _check_js_with_node(js: str) -> str | None:
    """Returns an error string if Node found a syntax error, None if the
    check passed OR Node isn't available (caller falls back to the
    heuristic check in that case)."""
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("node"):
        return None

    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["node", "--check", tmp_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return result.stderr.strip()[:400]
        return ""  # empty string = "checked, and it's clean" (not None = "unchecked")
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _check_js_heuristic(js: str) -> str | None:
    """Brace/bracket/paren balance check, roughly ignoring string/template
    literals and comments. Not a real parser — just enough to catch the
    common "model got cut off" or "dropped a closing brace" failure without
    needing Node installed."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    closers = {v: k for k, v in pairs.items()}
    stack = []
    i, n = 0, len(js)
    in_str = None       # active quote char, or None
    in_line_comment = False
    in_block_comment = False

    while i < n:
        c = js[i]
        nxt = js[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if c == "\n":
                in_line_comment = False
        elif in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 1
        elif in_str:
            if c == "\\":
                i += 1  # skip escaped char
            elif c == in_str:
                in_str = None
        elif c == "/" and nxt == "/":
            in_line_comment = True
            i += 1
        elif c == "/" and nxt == "*":
            in_block_comment = True
            i += 1
        elif c in ("'", '"', "`"):
            in_str = c
        elif c in pairs:
            stack.append(c)
        elif c in closers:
            if not stack or stack[-1] != closers[c]:
                return f"Unbalanced '{c}' in inline script (heuristic check, not a full parse)."
            stack.pop()

        i += 1

    if stack:
        return f"Unclosed '{stack[-1]}' in inline script (heuristic check, not a full parse)."
    return None


def validate_html_output(html: str) -> list[str]:
    issues: list[str] = []

    if not html.rstrip().lower().endswith("</html>"):
        issues.append(
            "The file appears truncated — it doesn't end with </html>, "
            "suggesting generation was cut off mid-file."
        )

    for script in _extract_inline_scripts(html):
        node_result = _check_js_with_node(script)
        if node_result:  # non-empty string = real syntax error from Node
            issues.append(f"JavaScript syntax error (Node): {node_result}")
        elif node_result is None:  # Node unavailable — use the heuristic instead
            heuristic_result = _check_js_heuristic(script)
            if heuristic_result:
                issues.append(heuristic_result)
        # node_result == "" means Node checked it and it's clean — no issue

    return issues


def fix_html_output(
    broken_code: str,
    issues: list[str],
    prompt_text: str,
) -> str:
    """One dev_agent-style fix pass: broken file + specific issues found ->
    complete corrected file, through the same provider fallback chain used
    for the original generation."""
    fix_prompt = "\n".join([
        "You are fixing a single self-contained HTML file that was just "
        "generated for this brief:",
        f"  {prompt_text[:500]}",
        "",
        "Static validation found these specific problems:",
        *[f"  - {issue}" for issue in issues],
        "",
        "Current (broken) file:",
        broken_code,
        "",
        "Rules:",
        "- Fix ONLY the problems listed above. Keep everything else — "
        "layout, styling, content, features — exactly as it is.",
        "- Output ONLY the complete corrected file. No explanation, no "
        "markdown, no backticks.",
        "- The file must be complete and self-contained, starting with "
        "<!DOCTYPE html> and ending with </html>.",
        "",
        "Corrected file:",
    ])
    code, _provider, _warning = run_completion([fix_prompt], prompt_text=fix_prompt, image_bytes=None)
    return clean_code(code)


def validate_and_fix(code: str, prompt_text: str, max_attempts: int = 2) -> tuple[str, list[str]]:
    """Runs validate_html_output(), and if it finds anything, tries
    fix_html_output() up to max_attempts times, re-validating after each
    attempt. Returns (final_code, fixes_applied) — fixes_applied is a list
    of human-readable strings describing what got fixed, empty if nothing
    needed fixing. Always returns the last *valid-doctype* version it has;
    never discards a working file for a fix attempt that made things worse.
    """
    fixes_applied: list[str] = []
    current = code

    for attempt in range(1, max_attempts + 1):
        issues = validate_html_output(current)
        if not issues:
            break

        print(f"[forge] Validation found {len(issues)} issue(s) (attempt {attempt}/{max_attempts}): {issues}")
        try:
            fixed = fix_html_output(current, issues, prompt_text)
        except Exception as e:
            print(f"[forge] Fix attempt failed: {e}")
            break

        if not fixed.lstrip().lower().startswith("<!doctype html"):
            print("[forge] Fix attempt didn't return valid HTML — keeping previous version.")
            break

        current = fixed
        fixes_applied.append(f"Attempt {attempt}: fixed {len(issues)} issue(s) — {'; '.join(issues)[:200]}")

    return current, fixes_applied


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


def _ollama_reachable(host: str) -> bool:
    try:
        import requests
        requests.get(f"{host}/api/tags", timeout=1.5)
        return True
    except Exception:
        return False


@app.route("/api/health")
def health():
    key_ok = True
    try:
        get_api_key()
    except Exception:
        key_ok = False
    ollama_cfg = get_ollama_config()
    return jsonify({
        "ok": True,
        "api_key_configured": key_ok,
        "groq_fallback_configured": get_groq_key() is not None,
        "openrouter_fallback_configured": get_openrouter_key() is not None,
        "ollama_fallback_model": ollama_cfg["model"],
        "ollama_fallback_reachable": _ollama_reachable(ollama_cfg["host"]),
    })


@app.route("/api/config", methods=["GET"])
def get_config():
    """Never returns the actual key values — just whether each is set —
    so this is safe to call from the browser."""
    ollama_cfg = get_ollama_config()
    return jsonify({
        "ok": True,
        "gemini_configured": _read_raw_key("gemini_api_key") is not None,
        "groq_configured": _read_raw_key("groq_api_key") is not None,
        "openrouter_configured": _read_raw_key("openrouter_api_key") is not None,
        "figma_configured": _read_raw_key("figma_api_token") is not None,
        "ollama_model": ollama_cfg["model"],
        "ollama_host": ollama_cfg["host"],
        "ollama_reachable": _ollama_reachable(ollama_cfg["host"]),
    })


def _read_raw_key(field: str) -> str | None:
    if not CONFIG_PATH.exists():
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    value = data.get(field, "")
    if not value or "YOUR_" in value.upper():
        return None
    return value


@app.route("/api/config", methods=["POST"])
def set_config():
    """Saves whichever fields were provided; a blank/omitted field leaves
    the existing stored value untouched (so re-saving one field doesn't
    wipe the others). gemini/groq/openrouter keys are secrets (password
    inputs, never echoed back); ollama_host/ollama_model aren't secret, so
    those just overwrite directly when present."""
    data = request.get_json(force=True, silent=True) or {}
    gemini_key      = (data.get("gemini_api_key") or "").strip()
    groq_key        = (data.get("groq_api_key") or "").strip()
    openrouter_key  = (data.get("openrouter_api_key") or "").strip()
    figma_token     = (data.get("figma_api_token") or "").strip()
    ollama_host     = (data.get("ollama_host") or "").strip()
    ollama_model    = (data.get("ollama_model") or "").strip()

    existing = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)

    if gemini_key:
        existing["gemini_api_key"] = gemini_key
    if groq_key:
        existing["groq_api_key"] = groq_key
    if openrouter_key:
        existing["openrouter_api_key"] = openrouter_key
    if figma_token:
        existing["figma_api_token"] = figma_token
    if ollama_host:
        existing["ollama_host"] = ollama_host
    if ollama_model:
        existing["ollama_model"] = ollama_model

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    ollama_cfg = get_ollama_config()
    return jsonify({
        "ok": True,
        "gemini_configured": _read_raw_key("gemini_api_key") is not None,
        "groq_configured": _read_raw_key("groq_api_key") is not None,
        "openrouter_configured": _read_raw_key("openrouter_api_key") is not None,
        "figma_configured": _read_raw_key("figma_api_token") is not None,
        "ollama_model": ollama_cfg["model"],
        "ollama_host": ollama_cfg["host"],
        "ollama_reachable": _ollama_reachable(ollama_cfg["host"]),
    })


@app.route("/api/templates", methods=["GET"])
def list_templates():
    """Metadata only (id, name, tagline, builtin) — no HTML content, so
    this stays light even with many custom templates added."""
    manifest = load_template_manifest()
    return jsonify({
        "ok": True,
        "templates": [
            {"id": t["id"], "name": t["name"], "tagline": t.get("tagline", ""), "builtin": bool(t.get("builtin"))}
            for t in manifest
        ],
    })


@app.route("/api/templates/<template_id>/preview")
def preview_template(template_id):
    entry = find_template(template_id)
    if not entry:
        return jsonify({"ok": False, "error": "Template not found."}), 404
    return send_from_directory(STYLE_LIB_DIR, entry["filename"])


@app.route("/api/templates", methods=["POST"])
def add_template():
    """Add a custom style template: paste/upload a full HTML file the user
    likes the look of. Stored alongside the builtins and selectable the
    same way; only non-builtin entries can be deleted later."""
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    tagline = (data.get("tagline") or "").strip()
    html = data.get("html") or ""

    if not name:
        return jsonify({"ok": False, "error": "Give the template a name."}), 400
    if not html.strip():
        return jsonify({"ok": False, "error": "No HTML content received."}), 400
    if len(html.encode("utf-8")) > STYLE_TEMPLATE_MAX_BYTES:
        return jsonify({"ok": False, "error": "Template file is too large (2MB max)."}), 400
    if "<html" not in html.lower():
        return jsonify({"ok": False, "error": "That doesn't look like a full HTML file — expected an <html> tag."}), 400

    manifest = load_template_manifest()
    template_id = slugify(name)[:40] or "template"
    existing_ids = {t["id"] for t in manifest}
    base_id = template_id
    counter = 2
    while template_id in existing_ids:
        template_id = f"{base_id}-{counter}"
        counter += 1

    filename = f"{template_id}.html"
    (STYLE_LIB_DIR / filename).write_text(html, encoding="utf-8")

    entry = {"id": template_id, "name": name, "tagline": tagline, "filename": filename, "builtin": False}
    manifest.append(entry)
    save_template_manifest(manifest)

    return jsonify({"ok": True, "template": {"id": entry["id"], "name": entry["name"], "tagline": entry["tagline"], "builtin": False}})


@app.route("/api/templates/<template_id>", methods=["DELETE"])
def delete_template(template_id):
    manifest = load_template_manifest()
    entry = next((t for t in manifest if t["id"] == template_id), None)
    if not entry:
        return jsonify({"ok": False, "error": "Template not found."}), 404
    if entry.get("builtin"):
        return jsonify({"ok": False, "error": "Built-in templates can't be deleted."}), 400

    manifest = [t for t in manifest if t["id"] != template_id]
    save_template_manifest(manifest)
    try:
        (STYLE_LIB_DIR / entry["filename"]).unlink(missing_ok=True)
    except Exception:
        pass

    return jsonify({"ok": True})


# ----------------------------------------------------------------------------
# Image library — "Images to Use (optional)": real user-uploaded photos the
# model should use verbatim instead of inventing/generating a placeholder
# for that slot. Same manifest-file pattern as the style template library
# above, but multi-select (a page can use several real images) and each
# entry carries a `tag` describing what it's for (e.g. "hero background",
# "team photo of Jane") so the prompt can tell the model where it fits.
# ----------------------------------------------------------------------------
IMAGE_MAX_BYTES = 4_000_000          # 4MB cap per uploaded image
IMAGE_PROMPT_MAX_COUNT = 12          # don't blow up the prompt with too many embedded images
IMAGE_MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
}


def load_image_manifest() -> list:
    if not IMAGE_MANIFEST.exists():
        return []
    try:
        with open(IMAGE_MANIFEST, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_image_manifest(manifest: list) -> None:
    with open(IMAGE_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def find_image(image_id: str) -> dict | None:
    for entry in load_image_manifest():
        if entry.get("id") == image_id:
            return entry
    return None


def store_image_bytes(name: str, tag: str, raw: bytes, mime: str) -> dict:
    """Shared by manual upload and Figma import: writes the file, adds a
    manifest entry with a unique id, and returns that entry."""
    ext = IMAGE_MIME_EXT.get(mime, ".png")
    manifest = load_image_manifest()
    image_id = slugify(name)[:40] or "image"
    existing_ids = {im["id"] for im in manifest}
    base_id = image_id
    counter = 2
    while image_id in existing_ids:
        image_id = f"{base_id}-{counter}"
        counter += 1
    filename = f"{image_id}{ext}"
    (IMAGE_LIB_DIR / filename).write_bytes(raw)
    entry = {"id": image_id, "name": name, "tag": tag, "filename": filename, "mime": mime}
    manifest.append(entry)
    save_image_manifest(manifest)
    return entry


def build_uploaded_images_block(image_entries: list) -> str:
    """Returns the prompt block embedding each selected real image as a
    data URI, or '' if none were selected. Unlike build_image_policy_block
    (which tells the model how to invent a placeholder), this hands over
    actual content the model must use as-is wherever its `tag` fits —
    everything else on the page still follows the placeholder policy."""
    if not image_entries:
        return ""
    entries = image_entries[:IMAGE_PROMPT_MAX_COUNT]
    lines = []
    for entry in entries:
        path = IMAGE_LIB_DIR / entry["filename"]
        try:
            raw = path.read_bytes()
        except Exception:
            continue
        if len(raw) > IMAGE_MAX_BYTES:
            continue
        b64 = base64.b64encode(raw).decode("ascii")
        data_uri = f"data:{entry.get('mime', 'image/png')};base64,{b64}"
        label = entry.get("tag") or entry.get("name") or "image"
        lines.append(f'- "{entry.get("name", "Untitled")}" — use for: {label}\n  src="{data_uri}"')

    if not lines:
        return ""

    return (
        "\n\nUPLOADED IMAGES — the user provided these REAL images and they "
        "MUST be used verbatim (exact src, unmodified) wherever their "
        "'use for' description fits on the page — do not regenerate, "
        "recolor, or replace them with a placeholder. If the page needs "
        "more images than are provided here, use the IMAGES policy above "
        "for those additional, uncovered slots only:\n\n" + "\n".join(lines)
    )


@app.route("/api/images", methods=["GET"])
def list_images():
    """Metadata only (id, name, tag) — thumbnails are fetched separately
    via /api/images/<id>/file so this stays light with many uploads."""
    manifest = load_image_manifest()
    return jsonify({
        "ok": True,
        "images": [{"id": im["id"], "name": im["name"], "tag": im.get("tag", "")} for im in manifest],
    })


@app.route("/api/images/<image_id>/file")
def serve_image_file(image_id):
    entry = find_image(image_id)
    if not entry:
        return jsonify({"ok": False, "error": "Image not found."}), 404
    return send_from_directory(IMAGE_LIB_DIR, entry["filename"])


@app.route("/api/images", methods=["POST"])
def add_image():
    """Add an image to the 'Images to Use' library: upload a real photo
    plus a short description of what it's for. Stored on disk and
    selectable in the picker the same way Style Templates are."""
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    tag = (data.get("tag") or "").strip()
    data_url = data.get("dataUrl") or ""

    if not name:
        return jsonify({"ok": False, "error": "Give the image a name."}), 400
    if not data_url or "," not in data_url or not data_url.startswith("data:"):
        return jsonify({"ok": False, "error": "No image file received."}), 400

    header, b64 = data_url.split(",", 1)
    mime = header.split(";")[0].replace("data:", "") or "image/png"
    ext = IMAGE_MIME_EXT.get(mime, ".png")

    try:
        raw = base64.b64decode(b64)
    except Exception:
        return jsonify({"ok": False, "error": "Could not decode image data."}), 400

    if len(raw) > IMAGE_MAX_BYTES:
        return jsonify({"ok": False, "error": "Image is too large (4MB max)."}), 400

    entry = store_image_bytes(name, tag, raw, mime)

    return jsonify({"ok": True, "image": {"id": entry["id"], "name": entry["name"], "tag": entry["tag"]}})


@app.route("/api/images/<image_id>", methods=["DELETE"])
def delete_image(image_id):
    manifest = load_image_manifest()
    entry = next((im for im in manifest if im["id"] == image_id), None)
    if not entry:
        return jsonify({"ok": False, "error": "Image not found."}), 404

    manifest = [im for im in manifest if im["id"] != image_id]
    save_image_manifest(manifest)
    try:
        (IMAGE_LIB_DIR / entry["filename"]).unlink(missing_ok=True)
    except Exception:
        pass

    return jsonify({"ok": True})


# ----------------------------------------------------------------------------
# Figma import — pulls rendered frames from a Figma file straight into the
# "Images to Use" library above, using the user's own Figma personal access
# token (stored server-side in config/api_keys.json, same as the Gemini/Groq
# keys — never sent back to the browser).
# ----------------------------------------------------------------------------
FIGMA_API_BASE = "https://api.figma.com/v1"


def _figma_file_key_from_url(raw: str) -> str | None:
    """Accepts either a bare file key or a full figma.com/file|design/<key>/... URL."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.search(r"figma\.com/(?:file|design|proto)/([a-zA-Z0-9]+)", raw)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9]+", raw):
        return raw
    return None


@app.route("/api/figma/frames", methods=["POST"])
def figma_list_frames():
    """Lists top-level frames/pages in a Figma file so the user can pick
    which ones to import, instead of importing the whole file blind."""
    import requests
    token = _read_raw_key("figma_api_token")
    if not token:
        return jsonify({"ok": False, "error": "No Figma token set. Add it in Settings first."}), 400

    data = request.get_json(force=True, silent=True) or {}
    file_key = _figma_file_key_from_url(data.get("fileUrl") or data.get("fileKey") or "")
    if not file_key:
        return jsonify({"ok": False, "error": "Could not find a file key in that link."}), 400

    try:
        resp = requests.get(
            f"{FIGMA_API_BASE}/files/{file_key}",
            headers={"X-Figma-Token": token},
            params={"depth": 2},
            timeout=20,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not reach Figma: {e}"}), 502

    if resp.status_code == 403:
        return jsonify({"ok": False, "error": "Figma rejected the token (invalid, or no access to this file)."}), 403
    if resp.status_code == 404:
        return jsonify({"ok": False, "error": "File not found — check the link/file key."}), 404
    if resp.status_code != 200:
        return jsonify({"ok": False, "error": f"Figma API error ({resp.status_code})."}), 502

    doc = resp.json().get("document", {})
    frames = []
    for page in doc.get("children", []):
        for node in page.get("children", []):
            if node.get("type") in ("FRAME", "COMPONENT", "COMPONENT_SET", "SECTION"):
                frames.append({"id": node["id"], "name": node.get("name", "Untitled"), "page": page.get("name", "")})

    return jsonify({"ok": True, "fileKey": file_key, "frames": frames[:200]})


@app.route("/api/figma/import", methods=["POST"])
def figma_import_frames():
    """Renders the chosen frames as PNGs via Figma's images API and drops
    each one straight into the Images library, tagged with its frame name
    so it's immediately usable/selectable like any other uploaded image."""
    import requests
    token = _read_raw_key("figma_api_token")
    if not token:
        return jsonify({"ok": False, "error": "No Figma token set. Add it in Settings first."}), 400

    data = request.get_json(force=True, silent=True) or {}
    file_key = _figma_file_key_from_url(data.get("fileUrl") or data.get("fileKey") or "")
    node_ids = data.get("nodeIds") or []
    node_names = data.get("nodeNames") or {}
    if not file_key:
        return jsonify({"ok": False, "error": "Could not find a file key in that link."}), 400
    if not node_ids:
        return jsonify({"ok": False, "error": "No frames selected to import."}), 400

    try:
        resp = requests.get(
            f"{FIGMA_API_BASE}/images/{file_key}",
            headers={"X-Figma-Token": token},
            params={"ids": ",".join(node_ids), "format": "png", "scale": 2},
            timeout=30,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not reach Figma: {e}"}), 502

    if resp.status_code != 200:
        return jsonify({"ok": False, "error": f"Figma image export failed ({resp.status_code})."}), 502

    render_urls = (resp.json() or {}).get("images", {}) or {}
    imported, failed = [], []
    for node_id in node_ids:
        img_url = render_urls.get(node_id)
        name = node_names.get(node_id) or f"figma-{node_id}"
        if not img_url:
            failed.append(name)
            continue
        try:
            img_resp = requests.get(img_url, timeout=30)
            if img_resp.status_code != 200 or not img_resp.content:
                failed.append(name)
                continue
            entry = store_image_bytes(name, f"Imported from Figma ({name})", img_resp.content, "image/png")
            imported.append({"id": entry["id"], "name": entry["name"], "tag": entry["tag"]})
        except Exception:
            failed.append(name)

    return jsonify({"ok": True, "imported": imported, "failed": failed})


@app.route("/api/edit-element", methods=["POST"])
def api_edit_element():
    data = request.get_json(force=True, silent=True) or {}

    instruction   = (data.get("instruction") or "").strip()
    element_html  = (data.get("elementHtml") or "").strip()
    full_code     = data.get("fullCode") or ""

    if not instruction:
        return jsonify({"ok": False, "error": "Describe what to change first."}), 400
    if not element_html:
        return jsonify({"ok": False, "error": "No element selected."}), 400
    if not full_code.strip():
        return jsonify({"ok": False, "error": "Nothing forged yet to edit."}), 400

    try:
        raw_code, provider, warning = generate_edit(instruction, element_html, full_code)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Edit failed: {e}"}), 500

    if raw_code.strip().upper() == "UNSAFE":
        print(f"[edit-element] first attempt returned UNSAFE (provider={provider}) — retrying once")
        try:
            raw_code, provider, warning = generate_edit(instruction, element_html, full_code)
        except Exception as e:
            print(f"[edit-element] retry after UNSAFE failed: {e}")

    if raw_code.strip().upper() == "UNSAFE":
        print(f"[edit-element] confirmed UNSAFE on retry (provider={provider}) — instruction: {instruction[:200]!r}")
        return jsonify({"ok": False, "error": "Request rejected: unsafe or not applicable to this element."}), 400

    code = clean_code(raw_code)

    if not code.lstrip().lower().startswith("<!doctype html"):
        return jsonify({"ok": False, "error": "Edit failed — model didn't return a valid HTML file."}), 502

    safe, reason = is_safe_html(code)
    if not safe:
        return jsonify({"ok": False, "error": f"Blocked for safety: {reason}"}), 400

    code, _ = resolve_section_images(code)

    return jsonify({"ok": True, "code": code, "provider": provider, "warning": warning})


@app.route("/api/forge", methods=["POST"])
def api_forge():
    data = request.get_json(force=True, silent=True) or {}

    prompt_text = (data.get("prompt") or "").strip()
    kind        = (data.get("kind") or "website").strip().lower()
    elements    = data.get("elements") or []
    segments    = data.get("segments") or {}
    canvas_w    = data.get("canvasWidth") or 1
    canvas_h    = data.get("canvasHeight") or 1
    image_data_url = data.get("image")  # "data:image/png;base64,...."
    filename_hint  = (data.get("filename") or "").strip()
    template_id    = (data.get("templateId") or "").strip()
    features       = data.get("features") or []
    animations     = data.get("animations") or []
    image_ids      = data.get("images") or []

    if not prompt_text:
        return jsonify({"ok": False, "error": "Please describe what to forge."}), 400

    image_bytes = None
    if image_data_url and "," in image_data_url:
        try:
            image_bytes = base64.b64decode(image_data_url.split(",", 1)[1])
        except Exception:
            image_bytes = None

    template_entry = find_template(template_id) if template_id else None
    uploaded_images = [e for e in (find_image(i) for i in image_ids) if e]

    structure_notes = describe_structure(elements, segments, canvas_w, canvas_h)

    try:
        raw_code, provider, warning = generate_code(prompt_text, kind, structure_notes, image_bytes, template_entry, features, animations, uploaded_images)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Generation failed: {e}"}), 500

    # A single "UNSAFE" reply is sometimes a genuine refusal, but weaker
    # fallback models (Groq's smaller ones, local Ollama) occasionally
    # echo that literal word back out of confusion on a long prompt
    # rather than actually refusing. One free, silent retry filters out
    # that noise before we tell the user their request was rejected.
    if raw_code.strip().upper() == "UNSAFE":
        print(f"[forge] first attempt returned UNSAFE (provider={provider}, kind={kind}, "
              f"prompt_chars={len(prompt_text)}) — retrying once before rejecting")
        try:
            raw_code, provider, warning = generate_code(prompt_text, kind, structure_notes, image_bytes, template_entry, features, animations, uploaded_images)
        except Exception as e:
            print(f"[forge] retry after UNSAFE failed: {e}")

    if raw_code.strip().upper() == "UNSAFE":
        print(f"[forge] confirmed UNSAFE on retry (provider={provider}, kind={kind}) — "
              f"prompt: {prompt_text[:200]!r}")
        return jsonify({"ok": False, "error": "Request rejected: not something that can be built as a safe webpage."}), 400

    code = clean_code(raw_code)

    if not code.lstrip().lower().startswith("<!doctype html"):
        return jsonify({"ok": False, "error": "Generation failed — model didn't return a valid HTML file."}), 502

    safe, reason = is_safe_html(code)
    if not safe:
        return jsonify({"ok": False, "error": f"Blocked for safety: {reason}"}), 400

    # Validate → fix loop (see validate_and_fix's docstring): catches
    # truncated files and broken inline JS that slipped past the doctype
    # check above, and tries to repair them via a targeted fix prompt
    # before this ever reaches the user.
    code, fixes_applied = validate_and_fix(code, prompt_text)
    if fixes_applied:
        fix_note = f"Auto-fixed after generation: {'; '.join(fixes_applied)}"
        warning = f"{warning} {fix_note}" if warning else fix_note

    # Auto-image-repair pass: even though build_instructions() already
    # tells the model never to use via.placeholder.com, coder models
    # (especially local/fallback ones) sometimes slip and use it anyway —
    # this walks every image individually and generates a real one for
    # any that's missing or dead, so nothing ships with blank image boxes.
    code, images_fixed = resolve_section_images(code)

    base_name = slugify(filename_hint or prompt_text)[:50]
    save_path = OUTPUT_DIR / f"{base_name}.html"
    counter = 2
    while save_path.exists():
        save_path = OUTPUT_DIR / f"{base_name}-{counter}.html"
        counter += 1

    save_path.write_text(code, encoding="utf-8")

    return jsonify({
        "ok": True,
        "code": code,
        "filename": save_path.name,
        "url": f"/forgeoutput/{save_path.name}",
        "provider": provider,
        "warning": warning,
        "imagesFixed": images_fixed,
    })


@app.route("/api/forge/stream", methods=["POST"])
def api_forge_stream():
    """Streaming twin of /api/forge. Same inputs, same post-processing
    (UNSAFE retry, doctype check, safety check, validate/fix loop,
    auto-image-repair, save-to-disk), but the model's output is forwarded
    to the browser as Server-Sent Events while it's still being generated
    instead of only after the whole thing (plus every post-processing
    pass) is done. This is what lets the UI show code appearing live,
    the way the model is actually writing it, instead of a blank loading
    spinner followed by one big paste-in at the end.

    Event stream (each line is `data: {"type": ..., ...}\\n\\n`):
      provider          - {"name": "gemini"|"groq"|"openrouter"|"ollama"} generation is starting/switching to this provider
      chunk             - {"text": "..."} a piece of raw model output, append it to the live view
      provider_switch   - {"from", "to", "reason"} the previous provider failed；discard everything streamed so far and start clean
      retry             - {"reason"} the first full attempt was rejected as UNSAFE; one silent retry is starting
      postprocessing    - {"stage": "validating"|"checking_images"} generation finished, running a post-pass (no live diff to show, but keeps the UI's status line accurate)
      code_replaced     - {"text": "..."} a post-processing pass rewrote the file; replace the live view wholesale with this
      error             - {"error": "..."} unrecoverable — show this and stop
      done              - {"code","filename","url","provider","warning","imagesFixed"} final result, same shape /api/forge returns
    """
    data = request.get_json(force=True, silent=True) or {}

    prompt_text = (data.get("prompt") or "").strip()
    kind        = (data.get("kind") or "website").strip().lower()
    elements    = data.get("elements") or []
    segments    = data.get("segments") or {}
    canvas_w    = data.get("canvasWidth") or 1
    canvas_h    = data.get("canvasHeight") or 1
    image_data_url = data.get("image")
    filename_hint  = (data.get("filename") or "").strip()
    template_id    = (data.get("templateId") or "").strip()
    features       = data.get("features") or []
    animations     = data.get("animations") or []
    image_ids      = data.get("images") or []
    split_build    = bool(data.get("splitBuild"))

    if not prompt_text:
        return jsonify({"ok": False, "error": "Please describe what to forge."}), 400

    image_bytes = None
    if image_data_url and "," in image_data_url:
        try:
            image_bytes = base64.b64decode(image_data_url.split(",", 1)[1])
        except Exception:
            image_bytes = None

    template_entry = find_template(template_id) if template_id else None
    uploaded_images = [e for e in (find_image(i) for i in image_ids) if e]
    structure_notes = describe_structure(elements, segments, canvas_w, canvas_h)

    def sse(event_type: str, **payload) -> str:
        return f"data: {json.dumps({'type': event_type, **payload})}\n\n"

    def run_one_pass():
        """Drives generate_code_stream() once, forwarding provider/chunk/
        provider_switch events straight through and returning the
        accumulated text (reset to '' every time the provider switches, so
        two different models' half-finished output never gets spliced
        together). Returns (full_text, provider, warning) or, on an
        unrecoverable error, yields the error event itself and returns
        None so the caller knows to stop."""
        full_text = ""
        provider = None
        warning = None
        for event in generate_code_stream(prompt_text, kind, structure_notes, image_bytes, template_entry, features, animations, uploaded_images):
            etype = event["type"]
            if etype == "provider":
                yield sse("provider", name=event["name"])
            elif etype == "chunk":
                full_text += event["text"]
                yield sse("chunk", text=event["text"])
            elif etype == "provider_switch":
                full_text = ""
                yield sse("provider_switch", **{k: v for k, v in event.items() if k != "type"})
            elif etype == "done":
                provider = event["provider"]
                warning = event["warning"]
            elif etype == "error":
                yield sse("error", error=event["error"])
                yield "__STOP__"
                return
        yield ("__RESULT__", full_text, provider, warning)

    def run_split_pass():
        """Drives split_builder.run_split_build() — the plan-then-parallel-
        sections pipeline (see split_builder.py) — forwarding its events as
        the SAME sse() event types the single-shot path already uses
        (provider/code_replaced/error), plus two split-only ones (plan/
        section_done) the frontend degrades gracefully on. Yields sse()
        strings directly and returns the final (code, provider, warning)
        wrapped in a __RESULT__ tuple, exactly like run_one_pass()."""
        from split_builder import run_split_build  # lazy: avoids import cycle with server.py

        code = provider = warning = None
        for item in run_split_build(prompt_text, kind, structure_notes, template_entry, features, animations, uploaded_images):
            t = item["type"]
            if t == "provider":
                yield sse("provider", name=item["name"])
            elif t == "plan":
                yield sse("plan", sections=item["sections"])
            elif t == "section_done":
                yield sse("section_done", id=item["id"], name=item["name"])
            elif t == "code_replaced":
                yield sse("code_replaced", text=item["text"])
            elif t == "error":
                if item["error"] == "unsafe":
                    yield sse("error", error="Request rejected: not something that can be built as a safe webpage.")
                else:
                    yield sse("error", error=item["error"])
                yield "__STOP__"
                return
            elif t == "done_split":
                code, provider, warning = item["code"], item["provider"], item["warning"]
        yield ("__RESULT__", code, provider, warning)

    def generate():
        raw_code = provider = warning = None

        if split_build:
            # Split-build path: split_builder already returns one fully
            # assembled, doctype-complete document — no clean_code() /
            # UNSAFE-retry / doctype-check needed, those only apply to the
            # single raw-text-stream path below.
            for item in run_split_pass():
                if isinstance(item, tuple) and item and item[0] == "__RESULT__":
                    _, raw_code, provider, warning = item
                elif item == "__STOP__":
                    return
                else:
                    yield item

            if raw_code is None:
                yield sse("error", error="Split build produced no output.")
                return

            code = raw_code
        else:
            gen = run_one_pass()
            for item in gen:
                if isinstance(item, tuple) and item and item[0] == "__RESULT__":
                    _, raw_code, provider, warning = item
                elif item == "__STOP__":
                    return
                else:
                    yield item

            if raw_code is None:
                yield sse("error", error="Generation failed — no output produced.")
                return

            # Same one-free-retry-on-UNSAFE behavior as /api/forge — a single
            # "UNSAFE" reply is occasionally a weaker fallback model echoing
            # the word back out of confusion on a long prompt, not a genuine
            # refusal.
            if raw_code.strip().upper() == "UNSAFE":
                print(f"[forge] (stream) first attempt returned UNSAFE (provider={provider}, kind={kind}) — retrying once")
                yield sse("retry", reason="First attempt was rejected — retrying once before giving up.")
                retry_gen = run_one_pass()
                raw_code = None
                for item in retry_gen:
                    if isinstance(item, tuple) and item and item[0] == "__RESULT__":
                        _, raw_code, provider, warning = item
                    elif item == "__STOP__":
                        return
                    else:
                        yield item
                if raw_code is None:
                    yield sse("error", error="Retry failed — no output produced.")
                    return

            if raw_code.strip().upper() == "UNSAFE":
                print(f"[forge] (stream) confirmed UNSAFE on retry (provider={provider}, kind={kind}) — prompt: {prompt_text[:200]!r}")
                yield sse("error", error="Request rejected: not something that can be built as a safe webpage.")
                return

            code = clean_code(raw_code)

            if not code.lstrip().lower().startswith("<!doctype html"):
                yield sse("error", error="Generation failed — model didn't return a valid HTML file.")
                return

        safe, reason = is_safe_html(code)
        if not safe:
            yield sse("error", error=f"Blocked for safety: {reason}")
            return

        yield sse("postprocessing", stage="validating")
        code, fixes_applied = validate_and_fix(code, prompt_text)
        if fixes_applied:
            fix_note = f"Auto-fixed after generation: {'; '.join(fixes_applied)}"
            warning = f"{warning} {fix_note}" if warning else fix_note
            # The fix pass rewrites the whole file server-side (it isn't
            # streamed token-by-token like the original generation) — push
            # the corrected version to the live view in one shot instead of
            # leaving the stale pre-fix code showing.
            yield sse("code_replaced", text=code)

        yield sse("postprocessing", stage="checking_images")
        code, images_fixed = resolve_section_images(code)
        if images_fixed:
            yield sse("code_replaced", text=code)

        base_name = slugify(filename_hint or prompt_text)[:50]
        save_path = OUTPUT_DIR / f"{base_name}.html"
        counter = 2
        while save_path.exists():
            save_path = OUTPUT_DIR / f"{base_name}-{counter}.html"
            counter += 1
        save_path.write_text(code, encoding="utf-8")

        yield sse(
            "done",
            code=code,
            filename=save_path.name,
            url=f"/forgeoutput/{save_path.name}",
            provider=provider,
            warning=warning,
            imagesFixed=images_fixed,
        )

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx-style proxy buffering if deployed behind one
            "Connection": "keep-alive",
        },
    )


def _safe_output_path(filename: str):
    """Resolve a client-supplied filename to a path strictly inside
    OUTPUT_DIR, or None if it isn't. Guards api_fix_images() and
    api_open_in_browser() below, which — unlike serve_forge_output()
    (which delegates to Flask's own send_from_directory, safe by
    default against ".." traversal) — used to build the path manually
    and trust the filename as-is. Without this, a filename like
    "../../../etc/passwd" would let those endpoints read (and, for
    fix-images, potentially overwrite) arbitrary files outside
    ./forgeoutput. Path(filename).name strips any directory components
    before the path is even joined, so no ".." can survive; the
    resolve()+relative_to() check afterward is defense in depth in case
    a future edit changes how `name` is built.
    """
    name = Path(filename).name
    if not name:
        return None
    candidate = (OUTPUT_DIR / name).resolve()
    try:
        candidate.relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        return None
    return candidate


@app.route("/api/forge/fix-images", methods=["POST"])
def api_fix_images():
    """Repair broken images in either the code currently in the editor
    (pass {"code": "..."}) or an already-saved file in ./forgeoutput
    (pass {"filename": "..."}, which also re-saves the patched result to
    disk). Runs resolve_section_images() — see there for how each image
    is individually inspected and, if dead, auto-generated fresh."""
    data = request.get_json(force=True, silent=True) or {}
    filename = (data.get("filename") or "").strip()
    code = data.get("code") or ""

    if filename:
        path = _safe_output_path(filename)
        if path is None:
            return jsonify({"ok": False, "error": "invalid filename"}), 400
        if not path.exists():
            return jsonify({"ok": False, "error": "file not found"}), 404
        code = path.read_text(encoding="utf-8")

    if not code.strip():
        return jsonify({"ok": False, "error": "No code to fix — nothing forged yet."}), 400

    patched, images_fixed = resolve_section_images(code)

    if filename and images_fixed:
        # filename was already validated by _safe_output_path() above
        # when it was read; re-derive the same safe path rather than
        # trusting the raw client value again for the write.
        write_path = _safe_output_path(filename)
        if write_path is not None:
            write_path.write_text(patched, encoding="utf-8")

    return jsonify({"ok": True, "code": patched, "imagesFixed": images_fixed})


@app.route("/forgeoutput/<path:filename>")
def serve_forge_output(filename):
    return send_from_directory(str(OUTPUT_DIR), filename)


@app.route("/api/forgeoutput/list")
def list_forge_output():
    files = sorted(
        (p.name for p in OUTPUT_DIR.glob("*.html")),
        key=lambda n: (OUTPUT_DIR / n).stat().st_mtime,
        reverse=True,
    )
    return jsonify({"ok": True, "files": files})


@app.route("/api/forge/open", methods=["POST"])
def api_open_in_browser():
    data = request.get_json(force=True, silent=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "filename required"}), 400
    path = _safe_output_path(filename)
    if path is None:
        return jsonify({"ok": False, "error": "invalid filename"}), 400
    if not path.exists():
        return jsonify({"ok": False, "error": "file not found"}), 404
    try:
        webbrowser.open(path.resolve().as_uri())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    print("=" * 60)
    print("  PRAGON — C.U.S.T.O.M FORGE bridge server")
    print(f"  Output folder : {OUTPUT_DIR}")
    print(f"  Config        : {CONFIG_PATH}")
    print("  URL           : http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5000, debug=True)
