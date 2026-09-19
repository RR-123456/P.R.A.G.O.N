import urllib.request
import webbrowser


def pragon_compass(parameters=None, response=None, player=None, speak=None):
    """
    Open PRAGON COMPASS — the live map / 3D globe / satellite tracking tool.
    Trigger phrases: "open compass", "show me the map", "launch the globe",
    "track satellites", "open pragon compass", etc.

    COMPASS is embedded directly in P.R.A.G.O.N's own web UI (ui.py) --
    served at /compass on the same HTTP server the JARVIS browser interface
    already runs (see PragonUI._http_port in ui.py), the same pattern used
    by C.U.S.T.O.M FORGE (see custom_forge.py).
    """
    port = 8080  # must match PragonUI._http_port in ui.py
    base_url = f"http://localhost:{port}"
    compass_url = f"{base_url}/compass"

    try:
        urllib.request.urlopen(base_url, timeout=1.5)
    except Exception:
        return (
            "P.R.A.G.O.N's web UI isn't reachable yet, so I can't open COMPASS. "
            "Make sure JARVIS is running, then try again."
        )

    webbrowser.open(compass_url)
    return "P.R.A.G.O.N COMPASS opened in your browser."
