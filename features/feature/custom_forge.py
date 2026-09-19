import sys
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import customforge_launcher  # noqa: E402


def custom_forge(parameters=None, response=None, player=None, speak=None):
    """
    Open the C.U.S.T.O.M FORGE drawing-pad app builder.
    Trigger phrases: "open custom forge", "launch forge", "open the builder", etc.

    FORGE is a standalone Flask app (customforge/main.py) that this starts
    as a background subprocess on first use (127.0.0.1:5000) if it isn't
    already running, then opens. It's also reachable inside P.R.A.G.O.N's
    own web UI via the CustomDraw button/overlay, which starts it the same
    way.
    """
    port = 8080  # must match PragonUI._http_port in ui.py
    base_url = f"http://localhost:{port}"

    try:
        urllib.request.urlopen(base_url, timeout=1.5)
        ui_reachable = True
    except Exception:
        ui_reachable = False

    if not customforge_launcher.ensure_forge_running():
        return (
            "I couldn't start C.U.S.T.O.M FORGE. Check that its dependencies "
            "are installed (customforge/requirements.txt) and that a Gemini "
            "API key is set in customforge/config/api_keys.json."
        )

    if ui_reachable:
        webbrowser.open(f"{base_url}/customdraw/app")
    else:
        webbrowser.open(customforge_launcher.FORGE_URL)

    return "C.U.S.T.O.M FORGE opened in your browser."
