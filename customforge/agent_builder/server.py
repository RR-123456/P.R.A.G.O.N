"""
server.py — standalone Agent Builder.

Run with:  python server.py
Then open: http://127.0.0.1:5057

Serves the visual canvas (static/index.html) and exposes a small API the
canvas talks to in order to list available tools, run a workflow for real
against tools/{file_controller,file_processor,dev_agent,code_helper,gemini_agent}.py,
and save/load workflow graphs as JSON files under workflows/.
"""

import os
import json
import re
import webbrowser
import threading
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory

from tools import get_tool_metadata
from tools.llm_client import ollama_available, list_ollama_models
from engine import run_workflow, GraphError
from skill_pack import export_skill_pack, import_skill_pack

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
try:
    from api.security import verify_token
except ImportError:
    verify_token = None
    print("[AgentBuilder] WARNING: api.security not importable -- "
          "state-changing routes running without auth.")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
WORKFLOWS_DIR = BASE_DIR / "workflows"
WORKFLOWS_DIR.mkdir(exist_ok=True)
EXPORTS_DIR = BASE_DIR / "exports"
EXPORTS_DIR.mkdir(exist_ok=True)
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

app = Flask(__name__, static_folder=None)


@app.before_request
def _require_auth_for_writes():
    """
    The canvas UI itself (GET /, GET /api/tools, etc.) stays open since this
    binds to 127.0.0.1 and is the local UI's own asset server. Anything that
    changes state or executes a workflow -- POST/PUT/DELETE -- needs a
    bearer token (see api/security.py), since /api/run ultimately executes
    real tool code (file_processor, dev_agent, code_helper...).
    """
    if verify_token is None:
        return  # security module unavailable; fail open with a startup warning already printed
    if request.method not in ("POST", "PUT", "DELETE"):
        return
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else None
    if not token or not verify_token(token):
        return jsonify({"error": "Missing or invalid bearer token."}), 401


def _read_raw_key(field: str) -> str | None:
    """Returns the stored value for `field`, or None if unset/placeholder."""
    if not CONFIG_PATH.exists():
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    value = (data.get(field) or "").strip()
    if not value or "your-" in value.lower():
        return None
    return value


def _safe_workflow_name(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[^\w\-]+", "_", name) or "workflow"
    return name[:80]


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/tools")
def api_tools():
    return jsonify(get_tool_metadata())


@app.route("/api/run", methods=["POST"])
def api_run():
    body = request.get_json(force=True, silent=True) or {}
    nodes = body.get("nodes", [])
    edges = body.get("edges", [])
    run_input = body.get("input", "")

    if not nodes:
        return jsonify({"error": "Workflow has no nodes."}), 400

    try:
        result = run_workflow(nodes, edges, run_input=run_input)
        return jsonify(result)
    except GraphError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.route("/api/workflows", methods=["GET"])
def api_list_workflows():
    names = sorted(p.stem for p in WORKFLOWS_DIR.glob("*.json"))
    return jsonify(names)


@app.route("/api/workflows/<name>", methods=["GET"])
def api_load_workflow(name):
    path = WORKFLOWS_DIR / f"{_safe_workflow_name(name)}.json"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


@app.route("/api/workflows/<name>", methods=["POST"])
def api_save_workflow(name):
    body = request.get_json(force=True, silent=True) or {}
    path = WORKFLOWS_DIR / f"{_safe_workflow_name(name)}.json"
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return jsonify({"saved": path.stem})


@app.route("/api/workflows/<name>", methods=["DELETE"])
def api_delete_workflow(name):
    path = WORKFLOWS_DIR / f"{_safe_workflow_name(name)}.json"
    if path.exists():
        path.unlink()
    return jsonify({"deleted": True})


@app.route("/api/skill-pack/export/<name>", methods=["POST"])
def api_export_skill_pack(name):
    """
    Opt-in sharing: package a saved workflow's logic (nodes/edges only --
    no credentials, no user data) into a signed .pragonskill file the user
    can hand to someone else's PRAGON install. See skill_pack.py.
    """
    body = request.get_json(force=True, silent=True) or {}
    workflow_path = WORKFLOWS_DIR / f"{_safe_workflow_name(name)}.json"
    if not workflow_path.exists():
        return jsonify({"error": "Workflow not found. Save it first."}), 404

    try:
        pack_path = export_skill_pack(
            workflow_path, output_dir=EXPORTS_DIR, description=body.get("description", "")
        )
    except Exception as e:
        return jsonify({"error": f"Export failed: {e}"}), 500

    return jsonify({"exported": pack_path.name, "download_url": f"/api/skill-pack/download/{pack_path.name}"})


@app.route("/api/skill-pack/download/<path:filename>")
def api_download_skill_pack(filename):
    return send_from_directory(EXPORTS_DIR, filename, as_attachment=True)


@app.route("/api/skill-pack/verify", methods=["POST"])
def api_verify_skill_pack():
    """
    Verifies a pack's signature and returns the exporting install's public
    fingerprint + manifest WITHOUT installing anything. The canvas should
    show this to the user (name, description, signer fingerprint) and let
    them explicitly choose to import -- verification is not auto-install.
    """
    uploaded = request.files.get("pack")
    if uploaded is None:
        return jsonify({"error": "No pack file uploaded (expected multipart field 'pack')."}), 400

    tmp_path = EXPORTS_DIR / f"_verify_{uploaded.filename}"
    uploaded.save(tmp_path)
    try:
        result = import_skill_pack(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return jsonify({
        "verified": result.verified,
        "reason": result.reason,
        "manifest": result.manifest,
        "signer_fingerprint": result.signer_fingerprint,
    })


@app.route("/api/skill-pack/import", methods=["POST"])
def api_import_skill_pack():
    """
    Re-verifies (never trusts a previous /verify call alone) and, only on a
    valid signature, writes the workflow into WORKFLOWS_DIR under a name
    the user confirms client-side. This is the explicit "yes, install this"
    step -- separate from /verify on purpose, so showing someone what a
    pack contains never has a side effect.
    """
    uploaded = request.files.get("pack")
    save_as = _safe_workflow_name(request.form.get("save_as", ""))
    if uploaded is None:
        return jsonify({"error": "No pack file uploaded (expected multipart field 'pack')."}), 400
    if not save_as:
        return jsonify({"error": "save_as name required."}), 400

    tmp_path = EXPORTS_DIR / f"_import_{uploaded.filename}"
    uploaded.save(tmp_path)
    try:
        result = import_skill_pack(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not result.verified:
        return jsonify({"error": result.reason}), 400

    dest = WORKFLOWS_DIR / f"{save_as}.json"
    dest.write_text(json.dumps(result.workflow, indent=2), encoding="utf-8")
    return jsonify({
        "installed": save_as,
        "signer_fingerprint": result.signer_fingerprint,
        "manifest": result.manifest,
    })


@app.route("/api/export-agent/<path:filename>")
def api_export_agent(filename):
    """Downloads a generated standalone agent .py file.

    The "Build Agent" tool (tools/agent_builder.py) always writes a copy
    here in addition to its Desktop/output_path save, specifically so the
    generated agent can be pulled down as a real browser download — the
    Desktop/output_path save happens on whatever machine is running this
    server, which the person using the canvas may have no filesystem
    access to at all (remote server, container, etc). The canvas's
    "⬇ Export Agent" button links straight to this route.
    """
    # send_from_directory already guards against path traversal (rejects
    # any filename that resolves outside EXPORTS_DIR), but a plain
    # basename check up front gives a clearer 404 than a stack trace for
    # anything obviously malformed.
    safe_name = Path(filename).name
    if safe_name != filename or not (EXPORTS_DIR / safe_name).is_file():
        return jsonify({"error": "Export not found — it may have been generated on a different run or already cleaned up."}), 404
    return send_from_directory(str(EXPORTS_DIR), safe_name, as_attachment=True)


@app.route("/api/export-agent", methods=["GET"])
def api_list_exports():
    """Lists exported agent files, most recent first — used by the canvas
    to show past exports even after a page refresh."""
    files = sorted(
        EXPORTS_DIR.glob("*.py"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return jsonify([
        {"filename": p.name, "url": f"/api/export-agent/{p.name}", "size": p.stat().st_size}
        for p in files
    ])


@app.route("/api/config", methods=["GET"])
def api_get_config():
    """Never returns the actual key — just whether it's set — so this is
    safe to call from the browser to drive a status indicator."""
    return jsonify({"ok": True, "gemini_configured": _read_raw_key("gemini_api_key") is not None})


@app.route("/api/config", methods=["POST"])
def api_set_config():
    """Saves the Gemini key to config/api_keys.json. A blank/omitted key
    leaves any existing stored value untouched, so this can't accidentally
    wipe a key that was set some other way (e.g. copied over by FORGE's
    main.py on startup)."""
    data = request.get_json(force=True, silent=True) or {}
    gemini_key = (data.get("gemini_api_key") or "").strip()

    existing = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    if gemini_key:
        existing["gemini_api_key"] = gemini_key

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    return jsonify({"ok": True, "gemini_configured": _read_raw_key("gemini_api_key") is not None})


@app.route("/api/ollama-status", methods=["GET"])
def api_ollama_status():
    """Lets the canvas show whether the free local Ollama fallback is
    available, and which models are already pulled and ready to use."""
    available = ollama_available()
    return jsonify({
        "ok": True,
        "available": available,
        "models": list_ollama_models() if available else [],
    })


def _open_browser():
    webbrowser.open("http://127.0.0.1:5057")


if __name__ == "__main__":
    # When launched by PRAGON FORGE's main.py alongside the main server,
    # AGENT_BUILDER_NO_AUTOOPEN is set so it doesn't pop its own browser
    # tab on startup — FORGE's "Agent Builder" topbar button opens it
    # on demand instead. Running this file directly (standalone) still
    # auto-opens a tab as before.
    if not os.environ.get("AGENT_BUILDER_NO_AUTOOPEN"):
        threading.Timer(1.0, _open_browser).start()
    print("Agent Builder running at http://127.0.0.1:5057")
    app.run(host="127.0.0.1", port=5057, debug=False)
