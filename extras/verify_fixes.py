"""
verify_fixes.py — checks that the 3 bug fixes are present and working.
Run from inside the pragon/ folder:

    python verify_fixes.py

Does NOT launch the full app (no audio/GUI deps needed), so it's safe
to run even before `pip install -r requirements.txt`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RESET = "\033[0m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"


def ok(msg):
    print(f"{GREEN}[PASS]{RESET} {msg}")


def fail(msg):
    print(f"{RED}[FAIL]{RESET} {msg}")


def warn(msg):
    print(f"{YELLOW}[WARN]{RESET} {msg}")


results = []

# ── 1. pragon_ui.py compiles + has both frontend fixes ──────────────────
print("\n--- 1. pragon_ui.py ---")
try:
    import py_compile
    py_compile.compile("pragon_ui.py", doraise=True)
    ok("pragon_ui.py compiles (no Python syntax errors)")
    results.append(True)
except py_compile.PyCompileError as e:
    fail(f"pragon_ui.py failed to compile: {e}")
    results.append(False)

with open("pragon_ui.py", encoding="utf-8") as f:
    ui_src = f.read()

if "if(pts.length===0){" in ui_src:
    ok("Particle-engine crash guard is present (Background Monitor / all Settings buttons fix)")
    results.append(True)
else:
    fail("Particle-engine crash guard NOT found — Settings buttons may still silently fail")
    results.append(False)

if "pathlib.Path(__file__).parent / 'uploads'" in ui_src:
    fail("Old buggy pathlib.Path(...) call still present — uploads will crash")
    results.append(False)
elif "Path(__file__).parent / 'uploads'" in ui_src:
    ok("File-upload pathlib fix is present (drag-and-drop / file picker fix)")
    results.append(True)
else:
    warn("Could not locate the upload-dir line at all — check manually")

# ── 2. pragon_whatsapp: node discovery ───────────────────────────────────
print("\n--- 2. pragon_whatsapp (WhatsApp bridge) ---")
try:
    from pragon_whatsapp import _find_node
    ok("_find_node() helper is present")
    results.append(True)
    node_path = _find_node()
    if node_path:
        ok(f"Node.js found at: {node_path}")
        results.append(True)
    else:
        fail("Node.js could NOT be located (checked PATH, registry, common install dirs). "
             "Install Node from https://nodejs.org")
        results.append(False)
except ImportError as e:
    fail(f"Could not import pragon_whatsapp._find_node — fix missing? ({e})")
    results.append(False)

# node_modules present? (needed for bot.js to actually run)
wa_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pragon_whatsapp")
if os.path.isdir(os.path.join(wa_dir, "node_modules")):
    ok("pragon_whatsapp/node_modules exists (npm install was run)")
    results.append(True)
else:
    warn("pragon_whatsapp/node_modules missing — run 'npm install' inside pragon_whatsapp/ "
         "once before connecting WhatsApp")

# ── 3. Full project compile check ────────────────────────────────────────
print("\n--- 3. Full project compile check ---")
import subprocess
bad = []
for root, dirs, files in os.walk("."):
    if "node_modules" in root or "__pycache__" in root:
        continue
    for fn in files:
        if fn.endswith(".py") and fn != "verify_fixes.py":
            path = os.path.join(root, fn)
            r = subprocess.run([sys.executable, "-m", "py_compile", path],
                                capture_output=True, text=True)
            if r.returncode != 0:
                bad.append((path, r.stderr.strip()))

if not bad:
    ok("All .py files in the project compile cleanly")
    results.append(True)
else:
    fail(f"{len(bad)} file(s) failed to compile:")
    for path, err in bad:
        print(f"    {path}: {err}")
    results.append(False)

# ── summary ───────────────────────────────────────────────────────────
print("\n" + "=" * 50)
if all(results):
    print(f"{GREEN}All checks passed.{RESET}")
else:
    print(f"{RED}Some checks failed — see [FAIL] lines above.{RESET}")
print("=" * 50)
