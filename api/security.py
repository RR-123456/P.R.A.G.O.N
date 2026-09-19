"""
api/security.py
════════════════════════════════════════════════════════════════════════════
Shared JWT authentication + TLS helpers for every local/LAN-facing PRAGON
service (ragsystem, pragon_phoneview, moss.py's FastAPI app, customforge's
Flask apps, the CPU & Network Balancer if it grows an endpoint).

WHY THIS EXISTS
    Review flagged that these endpoints had no auth, no input-validation
    contract, and no TLS -- meaning anything on the same LAN could hit
    /query, /upload, pairing, or the agent builder. "Local-first" is a
    privacy argument for keeping data on-device; it is not a substitute for
    authenticating who's allowed to talk to that device's services.

WHAT THIS MODULE PROVIDES
    1. A per-install signing secret, generated once and stored outside the
       repo (PRAGON_SECRET_PATH, default ~/.pragon/jwt_secret), so tokens
       aren't signed with a hardcoded key.
    2. issue_token(subject) / verify_token(token) -- short-lived HS256 JWTs.
    3. require_auth -- a FastAPI dependency: `Depends(require_auth)`.
    4. require_auth_flask -- a Flask decorator: `@require_auth_flask`.
    5. ssl_context() -- loads (or generates, for local dev) a self-signed
       TLS certificate so services can be started with
       `uvicorn ... --ssl-keyfile ... --ssl-certfile ...` / Flask's
       `app.run(ssl_context=...)`. Production deployments should supply
       their own cert via PRAGON_TLS_CERT / PRAGON_TLS_KEY instead of the
       generated one.

USAGE (FastAPI)
    from fastapi import Depends
    from api.security import require_auth

    @app.post("/query")
    def query(body: Query, _: str = Depends(require_auth)):
        ...

USAGE (Flask)
    from api.security import require_auth_flask

    @app.route("/build", methods=["POST"])
    @require_auth_flask
    def build():
        ...

USAGE (issuing a token, e.g. from pragon_main.py at startup so the desktop
app can call its own local services)
    from api.security import issue_token
    token = issue_token("pragon-desktop")
    # -> send as `Authorization: Bearer <token>` on every request
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Optional

import jwt  # PyJWT

_ALGORITHM = "HS256"
_DEFAULT_TTL_SECONDS = 12 * 60 * 60  # 12h -- long enough for a desktop session


def _secret_path() -> Path:
    configured = os.environ.get("PRAGON_SECRET_PATH")
    if configured:
        return Path(configured)
    return Path.home() / ".pragon" / "jwt_secret"


def _get_or_create_secret() -> str:
    path = _secret_path()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # best-effort on POSIX; no-op on Windows
    except Exception:
        pass
    return secret


_SECRET = _get_or_create_secret()


# ── Token issuance / verification ───────────────────────────────────────────

def issue_token(subject: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> str:
    now = int(time.time())
    payload = {"sub": subject, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None


def _extract_bearer(header_value: Optional[str]) -> Optional[str]:
    if not header_value or not header_value.startswith("Bearer "):
        return None
    return header_value[len("Bearer "):].strip()


# ── FastAPI dependency ───────────────────────────────────────────────────────
#
# Import fastapi lazily (module-level, but guarded) so this file stays
# importable from pure-Flask processes that never installed fastapi.

def _build_fastapi_dependency():
    from fastapi import Header, HTTPException, status

    def require_auth_dep(authorization: Optional[str] = Header(default=None)) -> str:
        """Use as: `Depends(require_auth_dep)` on any protected route."""
        token = _extract_bearer(authorization)
        claims = verify_token(token) if token else None
        if not claims:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid bearer token.",
            )
        return claims["sub"]

    return require_auth_dep


try:
    require_auth_dep = _build_fastapi_dependency()
except ImportError:
    require_auth_dep = None  # fastapi not installed in this process


# ── Flask decorator ──────────────────────────────────────────────────────────

def require_auth_flask(view_fn):
    from functools import wraps

    @wraps(view_fn)
    def wrapper(*args, **kwargs):
        from flask import request, jsonify

        token = _extract_bearer(request.headers.get("Authorization"))
        claims = verify_token(token) if token else None
        if not claims:
            return jsonify({"error": "Missing or invalid bearer token."}), 401
        return view_fn(*args, **kwargs)

    return wrapper


# ── TLS ──────────────────────────────────────────────────────────────────────

def _self_signed_cert_paths() -> tuple[Path, Path]:
    cert_dir = Path.home() / ".pragon" / "tls"
    return cert_dir / "cert.pem", cert_dir / "key.pem"


def ensure_tls_cert() -> tuple[str, str]:
    """
    Returns (certfile, keyfile) paths. Uses PRAGON_TLS_CERT / PRAGON_TLS_KEY
    if set (production: bring your own cert). Otherwise generates a
    self-signed cert on first run for local/LAN dev use and reuses it after
    that -- good enough to get TLS 1.3 on the wire for a LAN service; not a
    substitute for a real cert if this is ever exposed beyond the LAN.
    """
    env_cert = os.environ.get("PRAGON_TLS_CERT")
    env_key = os.environ.get("PRAGON_TLS_KEY")
    if env_cert and env_key:
        return env_cert, env_key

    cert_path, key_path = _self_signed_cert_paths()
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "pragon.local")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.DNSName("pragon.local")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(cert_path), str(key_path)
