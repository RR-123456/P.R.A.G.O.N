"""
pragon_moss/livekit_check.py
════════════════════════════════════════════════════════════════════════════
Preflight for the LiveKit + Moss voice path. Run this before starting the
agent — it tells you exactly which of the five things is wrong instead of
leaving you to read a stack trace from inside a worker process.

    python -m pragon_moss.livekit_check

Checks
------
  1. api/api_keys.json parses, and which values came from it vs the env
  2. a room token mints and verifies against the secret (same code path as
     pragon_ui.py's /livekit/token endpoint)
  3. the LiveKit host resolves and accepts a TLS connection on :443
  4. livekit / livekit-agents / plugins import, and which API generation
  5. Moss engine health and that the voice hooks augment a prompt for real
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

os.environ["PYTHONIOENCODING"] = "utf-8"
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API_CONFIG_PATH = ROOT / "api" / "api_keys.json"
OK, BAD, WARN = "  ✓", "  ✗", "  !"
_fail = []


def check(label, cond, detail=""):
    print(f"{OK if cond else BAD} {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        _fail.append(label)
    return cond


def warn(label, detail=""):
    print(f"{WARN} {label}" + (f" — {detail}" if detail else ""))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mint(api_key, api_secret, room="pragon-preflight", identity="preflight", ttl=300):
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": api_key, "sub": identity, "iat": now, "nbf": now, "exp": now + ttl,
        "name": identity,
        "video": {"room": room, "roomJoin": True, "canPublish": True,
                  "canSubscribe": True, "canPublishData": True},
    }
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(payload, separators=(",", ":")).encode()))
    sig = hmac.new(api_secret.encode(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def main() -> int:
    print("\n── 1. configuration ──")
    cfg = {}
    try:
        cfg = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        check("api/api_keys.json parses", True)
    except FileNotFoundError:
        check("api/api_keys.json exists", False, str(API_CONFIG_PATH))
    except Exception as exc:
        check("api/api_keys.json parses", False, str(exc))

    url = os.environ.get("LIVEKIT_URL") or cfg.get("livekit_url", "")
    api_key = os.environ.get("LIVEKIT_API_KEY") or cfg.get("livekit_api_key", "")
    api_secret = os.environ.get("LIVEKIT_API_SECRET") or cfg.get("livekit_api_secret", "")

    check("livekit_url set", bool(url), url)
    check("livekit_api_key set", bool(api_key), (api_key[:6] + "…") if api_key else "")
    check("livekit_api_secret set", bool(api_secret),
          f"{len(api_secret)} chars" if api_secret else "")

    gem = os.environ.get("GEMINI_API_KEY") or cfg.get("gemini_api_key", "")
    if not gem or "YOUR" in gem.upper():
        warn("gemini_api_key is still a placeholder",
             "the voice agent needs it for STT/LLM/TTS; the UI token endpoint does not")
    else:
        check("gemini_api_key set", True)

    print("\n── 2. token minting ──")
    if api_key and api_secret:
        tok = _mint(api_key, api_secret)
        head, payload_b64, sig = tok.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad(payload_b64)))
        expect = _b64url(hmac.new(api_secret.encode(),
                                  f"{head}.{payload_b64}".encode("ascii"),
                                  hashlib.sha256).digest())
        check("token mints", bool(tok))
        check("signature verifies", hmac.compare_digest(sig, expect))
        check("grant has roomJoin", claims.get("video", {}).get("roomJoin") is True)
        check("issuer matches api key", claims.get("iss") == api_key)
    else:
        check("token minting", False, "no credentials to sign with")

    print("\n── 3. reachability ──")
    host, port = "", 443
    if url:
        p = urlparse(url)
        host = p.hostname or ""
        port = p.port or (443 if p.scheme in ("wss", "https") else 80)
    if host:
        try:
            ip = socket.gethostbyname(host)
            check(f"DNS resolves {host}", True, ip)
        except Exception as exc:
            check(f"DNS resolves {host}", False, str(exc))
            ip = None
        if ip:
            try:
                ctxs = ssl.create_default_context()
                with socket.create_connection((host, port), timeout=6) as sock:
                    with ctxs.wrap_socket(sock, server_hostname=host) as ss:
                        check(f"TLS connect {host}:{port}", True, ss.version())
            except Exception as exc:
                check(f"TLS connect {host}:{port}", False, f"{type(exc).__name__}: {exc}")
    else:
        check("livekit host parsed from url", False, url)

    print("\n── 4. packages ──")
    try:
        import livekit  # noqa: F401
        check("livekit importable", True)
    except Exception as exc:
        check("livekit importable", False, str(exc))
    api_gen = None
    try:
        from livekit.agents import AgentSession  # noqa: F401
        api_gen = "1.x (AgentSession / on_user_turn_completed)"
    except Exception:
        try:
            from livekit.agents.pipeline import VoicePipelineAgent  # noqa: F401
            api_gen = "0.x (VoicePipelineAgent / before_llm_cb)"
        except Exception:
            pass
    check("livekit-agents importable", api_gen is not None, api_gen or "not installed")
    for mod, label in (("livekit.plugins.google", "google plugin"),
                       ("livekit.plugins.silero", "silero VAD plugin")):
        try:
            __import__(mod)
            check(label, True)
        except Exception as exc:
            check(label, False, type(exc).__name__)

    print("\n── 5. moss ──")
    try:
        from pragon_moss import get_bridge
        from pragon_moss import livekit_hooks as lk

        moss = get_bridge()
        check("moss bridge enabled", moss.enabled, moss.error or "")
        if moss.enabled:
            check("embedding backend", bool(moss.engine.embed_backend_name),
                  moss.engine.embed_backend_name)
            probe = "preflight: the compressor housing must be isolated first"
            moss.remember(probe, {"type": "safety", "agent": "preflight",
                                  "confirmed": False}, channel=lk.CHANNEL)
            out = lk.augment_text("what must happen before opening the housing?")
            check("voice hook injects context", "MOSS SESSION CONTEXT" in out)
            check("original question preserved", "opening the housing" in out)
            budget = os.environ.get("PRAGON_MOSS_LK_TIMEOUT", "0.35")
            print(f"    voice retrieval budget: {budget}s · "
                  f"top_k {os.environ.get('PRAGON_MOSS_LK_TOPK', '4')}")
    except Exception as exc:
        check("moss voice hooks", False, f"{type(exc).__name__}: {exc}")

    print("\n" + ("PREFLIGHT PASSED — start the agent with: "
                  "python livekit_agent_moss.py dev"
                  if not _fail else f"{len(_fail)} PROBLEM(S): {_fail}"))
    return 0 if not _fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
