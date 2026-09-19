# split_builder.py
# ============================================================================
#  PRAGON — C.U.S.T.O.M FORGE — split build pipeline
#
#  server.py's original generation path (generate_code / generate_code_stream)
#  asks one model call to write an ENTIRE self-contained HTML page — nav,
#  hero, every content section, footer, all CSS, all JS — in one shot. That
#  works fine for small pages, but on a heavy brief (long sketch, lots of
#  features/animations, a style template attached) it's a lot of output to
#  hold in one generation: it's slower, it's the single most likely place a
#  provider truncates or degrades, and validate_and_fix() ends up having to
#  regenerate the WHOLE file to repair one broken section.
#
#  This module is dev_agent.py's plan -> write-each-file -> fix-just-the-
#  broken-file pattern, adapted from a multi-file Python project to a
#  single-file HTML page split into independent SECTIONS instead of files:
#
#      plan_sections()      -- one small, cheap call (mirrors dev_agent's
#                               _plan_project): breaks the brief into a short
#                               list of sections + one shared "design system"
#                               description so every section stays visually
#                               consistent without re-sending each other's
#                               full CSS.
#
#      generate_section()   -- one call per section (mirrors dev_agent's
#                               _write_file), each with only ITS OWN job
#                               description + the shared design system, not
#                               the whole page. Smaller prompt in, smaller
#                               fragment out, much less likely to truncate.
#
#      run_split_build()    -- fires every section's generate_section() call
#                               CONCURRENTLY (ThreadPoolExecutor — these are
#                               network calls, so threads genuinely overlap
#                               the wait time instead of paying for it N
#                               times in a row) and streams a progressively
#                               more-complete assembled document back to the
#                               caller as each section lands.
#
#  server.py's existing validate_and_fix() / resolve_section_images() /
#  is_safe_html() already work on the assembled output afterwards exactly as
#  they do for a single-shot build — nothing about post-processing needs to
#  change, this module only changes how the raw HTML gets written in the
#  first place.
# ============================================================================

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

# Reuse everything already built for the single-shot path instead of
# duplicating it — the shared prompt blocks (style/features/animations/image
# policy), the four-tier provider fallback chain, and clean_code() all behave
# identically whether the call is for one giant page or one small section.
from server import (
    KIND_GUIDANCE,
    build_style_reference_block,
    build_features_block,
    build_animations_block,
    build_image_policy_block,
    build_uploaded_images_block,
    run_completion,
    clean_code,
)

DEFAULT_MAX_WORKERS = 4

# Fallback section lists used only if the planning call fails outright (bad
# JSON, provider error) — keeps split-build from hard-failing a build that
# the single-shot path would have handled fine. Mirrors dev_agent's
# _fallback_plan() safety net.
_FALLBACK_SECTIONS = {
    "website": [
        {"id": "nav", "name": "Navigation Bar", "role": "nav", "brief": "Site nav with logo/name and links to the other sections on this page."},
        {"id": "hero", "name": "Hero", "role": "hero", "brief": "Main headline, supporting copy, and a primary call-to-action for the page."},
        {"id": "content", "name": "Main Content", "role": "content", "brief": "The core content sections the brief describes."},
        {"id": "footer", "name": "Footer", "role": "footer", "brief": "Footer with links/contact/copyright."},
    ],
    "frontend": [
        {"id": "shell", "name": "App Shell", "role": "nav", "brief": "Header/toolbar and overall app frame."},
        {"id": "main", "name": "Main Panel", "role": "content", "brief": "The primary interactive controls and working logic the brief describes."},
        {"id": "footer", "name": "Status Bar", "role": "footer", "brief": "Footer or status bar, if the brief implies one."},
    ],
    "game": [
        {"id": "shell", "name": "Game Shell", "role": "nav", "brief": "Title, canvas/board container, and score/status UI."},
        {"id": "loop", "name": "Game Logic", "role": "content", "brief": "The actual playable game loop: state, input handling, win/lose/scoring, restart."},
        {"id": "controls", "name": "Controls / Instructions", "role": "footer", "brief": "On-screen controls or how-to-play text."},
    ],
}

_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)

_SAFETY_BLOCK = (
    "SAFETY — the ONLY reason to refuse: this section's job, as described "
    "above, explicitly requires something genuinely dangerous or abusive — "
    "malware/exploit code, phishing or credential-harvesting UI, "
    "instructions for weapons or drugs, or sexual content involving minors "
    "(including a working lure that gets a visitor to download a real or "
    "fake executable, e.g. wiring a button to an .exe/.scr/.bat/.msi/.apk "
    "download, even if framed as a joke or theme). An ordinary section of a "
    "business site, portfolio, game, dashboard, landing page, or app UI is "
    "NEVER unsafe, no matter how unusual the theme sounds — build it. Only "
    "if this section truly falls in the dangerous list above, respond with "
    "EXACTLY and ONLY the single word UNSAFE and nothing else."
)


# ----------------------------------------------------------------------------
# Planning — one cheap call that breaks the brief into sections + a shared
# design system, instead of dev_agent's file list.
# ----------------------------------------------------------------------------
def build_plan_prompt(prompt_text, kind, structure_notes, template_entry, features, animations) -> str:
    kind = kind if kind in KIND_GUIDANCE else "website"
    guidance = KIND_GUIDANCE[kind]

    parts = [
        "You are Pragon's planning module inside the C.U.S.T.O.M FORGE "
        "builder. A page is about to be built by SEVERAL SEPARATE generation "
        "calls, one per section, running in parallel — the same way a lead "
        "engineer splits a big page into components before handing pieces "
        "to different developers. Your ONLY job is to produce that split "
        "plan. Do NOT write any HTML/CSS/JS yourself.",
        "",
        f"OUTPUT TYPE: {kind.upper()}",
        guidance,
        "",
        f"USER BRIEF: {prompt_text}",
    ]

    if structure_notes:
        parts += [
            "",
            "STRUCTURE FROM THE DRAWING PAD (use this to decide what "
            "sections exist and their order):",
            structure_notes,
        ]

    if template_entry:
        parts.append(build_style_reference_block(template_entry))
    if features:
        parts.append(build_features_block(features))
    if animations:
        names = ", ".join(a.get("name") or a.get("id") or "Animation" for a in animations)
        parts.append(f"\n\nANIMATION COMPONENTS AVAILABLE (assign each to whichever section fits, by name only — the actual code is supplied later): {names}")

    parts += [
        "",
        "SAFETY — the ONLY reason to refuse: the brief explicitly asks for "
        "something genuinely dangerous or abusive to build (malware/exploit "
        "code, phishing pages, weapons/drug instructions, sexual content "
        "involving minors, or a working malware-download lure). An ordinary "
        "site/game/app UI is NEVER unsafe, however unusual the theme — plan "
        "it normally. Only if the brief truly falls in the dangerous list, "
        'return exactly {"unsafe": true} and nothing else.',
        "",
        "Otherwise, return ONLY valid JSON — no markdown, no explanation:",
        "{",
        '  "unsafe": false,',
        '  "title": "short page/app title",',
        '  "design_system": "3-5 sentences: exact color palette (name the '
        'hex codes or color names), typography choices, spacing/mood, and '
        'overall visual language. Every section is built by a DIFFERENT '
        'call that will never see the others'"'"'s code, so this description '
        'is the ONLY thing keeping them visually consistent — be specific.",',
        '  "sections": [',
        '    {"id": "snake_case_id", "name": "Human-readable name", '
        '"role": "nav|hero|content|footer|other", "brief": "1-3 sentences: '
        'exactly what this section must contain and do"}',
        "  ]",
        "}",
        "",
        "Rules:",
        "- 3-8 sections is typical. Keep it minimal — only sections the "
        "brief/structure actually calls for.",
        "- List sections in the TOP-TO-BOTTOM order they should appear on "
        "the page.",
        "- A website almost always wants a nav section and a footer "
        "section; a game/frontend app usually doesn't.",
        "- Each section's brief must be self-contained enough that a "
        "developer who has NEVER seen the other sections' code could build "
        "it correctly from the brief + the design system alone.",
        "",
        "JSON:",
    ]
    return "\n".join(parts)


def _fallback_plan(kind: str) -> dict:
    kind = kind if kind in _FALLBACK_SECTIONS else "website"
    print(f"[split_builder] Planning failed — using fallback {kind} section list")
    return {
        "unsafe": False,
        "title": "",
        "design_system": "Clean, modern, professional look: a single "
        "accent color against a neutral background, one readable sans-serif "
        "font, generous spacing, consistent rounded corners on cards/buttons.",
        "sections": _FALLBACK_SECTIONS[kind],
    }


def plan_sections(prompt_text, kind, structure_notes, template_entry=None, features=None, animations=None) -> dict:
    prompt = build_plan_prompt(prompt_text, kind, structure_notes, template_entry, features, animations)
    try:
        raw, _provider, _warning = run_completion([prompt], prompt_text=prompt, image_bytes=None)
        text = clean_code(raw)
        plan = json.loads(text)
    except Exception as e:
        print(f"[split_builder] Plan parse/call failed: {e}")
        return _fallback_plan(kind)

    if plan.get("unsafe"):
        return {"unsafe": True}

    sections = plan.get("sections")
    if not isinstance(sections, list) or not sections:
        return _fallback_plan(kind)

    clean_sections = []
    seen_ids = set()
    for i, s in enumerate(sections):
        sid = re.sub(r"[^a-z0-9_]", "_", str(s.get("id") or f"section_{i}").strip().lower()) or f"section_{i}"
        while sid in seen_ids:
            sid = f"{sid}_{i}"
        seen_ids.add(sid)
        clean_sections.append({
            "id": sid,
            "name": s.get("name") or sid.replace("_", " ").title(),
            "role": s.get("role") or "content",
            "brief": s.get("brief") or "",
        })

    plan["sections"] = clean_sections
    plan["unsafe"] = False
    return plan


# ----------------------------------------------------------------------------
# Per-section generation — dev_agent's _write_file(), but for one HTML/CSS/JS
# fragment instead of one project file.
# ----------------------------------------------------------------------------
def build_section_prompt(section, section_index, all_sections, prompt_text, kind, design_system, title,
                          structure_notes=None, template_entry=None, features=None, animations=None,
                          uploaded_images=None) -> str:
    other_names = ", ".join(s["name"] for s in all_sections if s["id"] != section["id"]) or "(none)"

    parts = [
        "You are Pragon, an expert frontend engineer inside the C.U.S.T.O.M "
        "FORGE builder. The page is being assembled from several sections "
        "that are each being built by a SEPARATE generation call — you are "
        f'writing ONLY the "{section["name"]}" section '
        f"(section {section_index + 1} of {len(all_sections)}). You will "
        "never see the other sections' actual code, so follow the shared "
        "design system below exactly, or the finished page will look like "
        "several different sites stitched together.",
        "",
        f"PAGE TITLE: {title or prompt_text[:60]}",
        f"FULL PAGE BRIEF (context only — build ONLY your section below, not the others): {prompt_text}",
        f"OTHER SECTIONS ON THIS PAGE, in order, already being built separately: {other_names}",
        "",
        "SHARED DESIGN SYSTEM — every section must match this exactly:",
        design_system,
        "",
        f"YOUR SECTION: {section['name']} (role: {section.get('role', 'content')})",
        f"WHAT IT MUST CONTAIN AND DO: {section.get('brief', '')}",
    ]

    if structure_notes:
        parts += [
            "",
            "STRUCTURE FROM THE DRAWING PAD (full page — use only the parts "
            "relevant to your section):",
            structure_notes,
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
        "- Output ONLY this section's fragment. Do NOT include <!DOCTYPE "
        "html>, <html>, <head>, or <body> tags — the page shell is added "
        "separately once every section is done.",
        "- Wrap your section in one root element appropriate to its role "
        f'(e.g. <nav id="{section["id"]}">, <footer id="{section["id"]}">, '
        f'or <section id="{section["id"]}">) so it can be dropped into the '
        "page without colliding with the others.",
        "- If this section needs its own CSS, put it in ONE <style> block. "
        "SCOPE EVERY RULE to this section — prefix selectors with "
        f'#{section["id"]} (e.g. "#{section["id"]} h2 {{...}}"), and never '
        "write bare/global selectors like body{...}, *{...}, or generic "
        "class names another section might also use (.container, .btn) — "
        "those would leak into and break the other sections.",
        "- If this section needs its own JS, put it in ONE <script> block, "
        "wrapped in an IIFE (e.g. (function(){ ... })();) so its variables "
        "and function names can't collide with another section's script.",
        "- No external CDN links, no Node.js APIs (require/fs/child_process).",
        "- It must work with zero console errors: every interactive element "
        "this section is responsible for must be wired up with real JS.",
        "",
        _SAFETY_BLOCK,
        "",
        "Now output the complete fragment for this section only, nothing "
        "else before or after it:",
    ]
    return "\n".join(parts)


def generate_section(section, section_index, all_sections, prompt_text, kind, design_system, title,
                      structure_notes=None, template_entry=None, features=None, animations=None,
                      uploaded_images=None):
    prompt = build_section_prompt(section, section_index, all_sections, prompt_text, kind, design_system, title,
                                   structure_notes, template_entry, features, animations, uploaded_images)
    raw, provider, warning = run_completion([prompt], prompt_text=prompt, image_bytes=None)
    return clean_code(raw), provider, warning


def split_fragment(frag: str) -> tuple[str, str, str]:
    """Pulls a section fragment's <style>/<script> blocks out into their
    own strings so run_split_build() can merge every section's CSS into one
    stylesheet and every section's JS into one script, leaving just the
    body markup to place inline. Mirrors what a real bundler does with
    per-component styles/scripts."""
    css = "\n\n".join(m.strip() for m in _STYLE_RE.findall(frag))
    js = "\n\n".join(m.strip() for m in _SCRIPT_RE.findall(frag))
    body = _SCRIPT_RE.sub("", _STYLE_RE.sub("", frag)).strip()
    return body, css, js


def assemble_document(title: str, order: list[str], bodies: dict, css_map: dict, js_map: dict) -> str:
    body_html = "\n\n".join(bodies[sid] for sid in order if bodies.get(sid))
    css = "\n\n".join(css_map[sid] for sid in order if css_map.get(sid))
    js = "\n\n".join(js_map[sid] for sid in order if js_map.get(sid))
    safe_title = (title or "Pragon Build").replace("<", "").replace(">", "")

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>{safe_title}</title>\n"
        "<style>\n" + css + "\n</style>\n"
        "</head>\n"
        "<body>\n" + body_html + "\n"
        "<script>\n" + js + "\n</script>\n"
        "</body>\n"
        "</html>"
    )


# ----------------------------------------------------------------------------
# Orchestration — plan, then fire every section concurrently, streaming a
# progressively-more-complete document back as each one lands.
# ----------------------------------------------------------------------------
def run_split_build(prompt_text, kind, structure_notes, template_entry=None, features=None, animations=None,
                     uploaded_images=None, max_workers=DEFAULT_MAX_WORKERS):
    """Generator of event dicts, same vocabulary as generate_code_stream()'s
    caller loop expects (`provider`, `code_replaced`, `error`) plus two new
    ones specific to this pipeline (`plan`, `section_done`), and a final
    `done_split` carrying the fully assembled document. server.py's route
    maps these onto the exact same SSE event types the frontend already
    understands — no frontend changes required to see it build live."""
    yield {"type": "provider", "name": "planning"}
    plan = plan_sections(prompt_text, kind, structure_notes, template_entry, features, animations)

    if plan.get("unsafe"):
        yield {"type": "error", "error": "unsafe"}
        return

    sections = plan["sections"]
    design_system = plan.get("design_system", "")
    title = plan.get("title") or ""
    order = [s["id"] for s in sections]

    yield {"type": "plan", "sections": [{"id": s["id"], "name": s["name"]} for s in sections]}

    bodies, css_map, js_map = {}, {}, {}
    providers_used, warnings = set(), []

    def work(section, idx):
        return generate_section(section, idx, sections, prompt_text, kind, design_system, title,
                                 structure_notes, template_entry, features, animations, uploaded_images)

    worker_count = max(1, min(max_workers, len(sections)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(work, s, i): s for i, s in enumerate(sections)}
        for future in as_completed(futures):
            section = futures[future]
            try:
                frag, provider, warning = future.result()
            except Exception as e:
                frag, provider, warning = "", None, f"Section '{section['name']}' failed: {e}"

            if provider:
                providers_used.add(provider)
            if warning:
                warnings.append(warning)

            if frag.strip().upper() == "UNSAFE":
                yield {"type": "error", "error": "unsafe"}
                return

            body, css, js = split_fragment(frag)
            bodies[section["id"]] = body
            css_map[section["id"]] = css
            js_map[section["id"]] = js

            yield {"type": "section_done", "id": section["id"], "name": section["name"]}
            yield {"type": "code_replaced", "text": assemble_document(title, order, bodies, css_map, js_map)}

    final_code = assemble_document(title, order, bodies, css_map, js_map)
    provider_summary = "+".join(sorted(providers_used)) if providers_used else "gemini"
    warning_summary = " ".join(warnings) if warnings else None
    yield {"type": "done_split", "code": final_code, "provider": provider_summary, "warning": warning_summary}
