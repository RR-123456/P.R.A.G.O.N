"""
customforge/agent_builder/skill_pack.py
════════════════════════════════════════════════════════════════════════════
Opt-in, signed Skill Pack export/import.

WHY THIS EXISTS
    Ties into the "Small Cloud" idea: a local-first alternative to a
    central app-store/marketplace for sharing what you built. A workflow
    made on the visual canvas (tools/nodes/edges -- see engine.py) is pure
    logic, no user data, so it's safe to hand to someone else's install
    with nothing routed through a server PRAGON's authors operate.

    "Signed" matters because importing a workflow means running whatever
    logic it describes on your machine. Without a signature, a skill pack
    is just an untrusted file -- with one, an importer can at least verify
    the pack hasn't been tampered with since it left the exporting install,
    and can see which install (by public-key fingerprint) produced it.
    This is a trust-on-first-use model, the same one SSH host keys use --
    not a PKI, no central authority, no server round-trip.

WHAT'S IN A .pragonskill FILE (a zip)
    manifest.json   -- name, description, exported_at, pragon-side version
                       tag, and the exporting install's public-key fingerprint
    workflow.json    -- the exact nodes/edges graph engine.py already saves
                       under workflows/<name>.json -- logic only, no
                       credentials, no file contents, no personal data
    public_key.pem   -- the exporting install's Ed25519 public key
    signature.bin    -- Ed25519 signature over manifest.json + workflow.json
                       bytes, produced with the matching private key (which
                       never leaves the exporting machine)

USAGE
    from skill_pack import export_skill_pack, import_skill_pack

    zip_path = export_skill_pack(WORKFLOWS_DIR / "morning_brief.json",
                                  description="Summarizes overnight email + calendar.")

    result = import_skill_pack(uploaded_zip_path)
    if result.verified:
        # caller decides whether to actually write result.workflow into
        # WORKFLOWS_DIR -- verification alone is not auto-install
        ...
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PACK_FORMAT_VERSION = 1


def _keypair_dir() -> Path:
    d = Path.home() / ".pragon" / "skill_signing"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_or_create_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives import serialization

    priv_path = _keypair_dir() / "private.pem"
    pub_path = _keypair_dir() / "public.pem"

    if priv_path.exists() and pub_path.exists():
        private_key = serialization.load_pem_private_key(priv_path.read_bytes(), password=None)
        public_bytes = pub_path.read_bytes()
        return private_key, public_bytes

    private_key = Ed25519PrivateKey.generate()
    priv_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path.write_bytes(priv_bytes)
    pub_path.write_bytes(public_bytes)
    try:
        import os
        os.chmod(priv_path, 0o600)
    except Exception:
        pass
    return private_key, public_bytes


def _fingerprint(public_key_pem: bytes) -> str:
    import hashlib
    return hashlib.sha256(public_key_pem).hexdigest()[:16]


@dataclass
class ImportResult:
    verified: bool
    reason: str
    manifest: Optional[dict] = None
    workflow: Optional[dict] = None
    signer_fingerprint: Optional[str] = None


def export_skill_pack(
    workflow_path: str | Path,
    output_dir: str | Path,
    description: str = "",
) -> Path:
    """
    Build a signed .pragonskill zip from an already-saved workflow JSON
    file (nodes/edges, as engine.py's format already produces). Returns the
    path to the created pack.
    """
    workflow_path = Path(workflow_path)
    workflow_bytes = workflow_path.read_bytes()
    workflow = json.loads(workflow_bytes)

    private_key, public_key_pem = _get_or_create_keypair()

    manifest = {
        "format_version": PACK_FORMAT_VERSION,
        "name": workflow_path.stem,
        "description": description[:500],
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "signer_fingerprint": _fingerprint(public_key_pem),
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

    # Sign the concatenation of manifest + workflow bytes exactly as written
    # into the zip, so any change to either after signing invalidates it.
    signature = private_key.sign(manifest_bytes + workflow_bytes)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pack_path = output_dir / f"{workflow_path.stem}.pragonskill"

    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", manifest_bytes)
        z.writestr("workflow.json", workflow_bytes)
        z.writestr("public_key.pem", public_key_pem)
        z.writestr("signature.bin", signature)

    return pack_path


def import_skill_pack(pack_path: str | Path) -> ImportResult:
    """
    Verify a .pragonskill file's signature against the public key bundled
    inside it. This proves internal consistency (the pack wasn't altered
    after signing) and tells you *which* install produced it (the
    fingerprint) -- it does NOT mean you should trust that install. Callers
    should surface the fingerprint to the user and let them decide, same as
    an SSH "unknown host key, trust it?" prompt.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    from cryptography.exceptions import InvalidSignature

    pack_path = Path(pack_path)
    try:
        with zipfile.ZipFile(pack_path, "r") as z:
            names = set(z.namelist())
            required = {"manifest.json", "workflow.json", "public_key.pem", "signature.bin"}
            if not required.issubset(names):
                return ImportResult(False, f"Missing files in pack: {required - names}")

            manifest_bytes = z.read("manifest.json")
            workflow_bytes = z.read("workflow.json")
            public_key_pem = z.read("public_key.pem")
            signature = z.read("signature.bin")
    except zipfile.BadZipFile:
        return ImportResult(False, "Not a valid skill pack (bad zip).")

    try:
        manifest = json.loads(manifest_bytes)
        workflow = json.loads(workflow_bytes)
    except json.JSONDecodeError as e:
        return ImportResult(False, f"Corrupt manifest or workflow JSON: {e}")

    try:
        public_key = load_pem_public_key(public_key_pem)
        public_key.verify(signature, manifest_bytes + workflow_bytes)
    except InvalidSignature:
        return ImportResult(False, "Signature verification failed -- pack was modified after signing.")
    except Exception as e:
        return ImportResult(False, f"Could not verify signature: {e}")

    fingerprint = _fingerprint(public_key_pem)
    claimed_fingerprint = manifest.get("signer_fingerprint")
    if claimed_fingerprint and claimed_fingerprint != fingerprint:
        return ImportResult(False, "Manifest fingerprint doesn't match the bundled public key.")

    return ImportResult(
        True,
        "Signature valid.",
        manifest=manifest,
        workflow=workflow,
        signer_fingerprint=fingerprint,
    )
