"""
Package validation and signing utilities.

Package signing is asymmetric by default: the Hub signs with an **Ed25519
private key** and an edge daemon verifies with the **public key only**, so a
device that can verify a package cannot forge one — the property that makes
provenance meaningful in a contested/disconnected (DDIL) environment.
Verification is fully offline (a provisioned public key, no online CA or
transparency log).

The legacy MVP signer used HMAC-SHA256 (a shared symmetric key). It remains
verifiable for backward compatibility — ``verify_package_signature`` dispatches
on the algorithm recorded in ``signature.json`` — but new packages should be
signed with Ed25519.

Key material is passed as the same ``key`` string used throughout the codebase;
its *kind* is auto-detected:

- an Ed25519 private key (PEM, or 64-hex / base64 raw 32 bytes) → can sign and
  verify;
- an Ed25519 public key (PEM, or raw) → can verify only;
- any other string → treated as a legacy HMAC shared secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SIGNATURE_FILE = "signature.json"
SIGNATURE_ALGORITHM = "HMAC-SHA256"  # legacy default; kept for compatibility
ED25519_ALGORITHM = "Ed25519"
KEY_FINGERPRINT_PREFIX = "sha256:"
ED25519_FINGERPRINT_PREFIX = "ed25519:"


def _load_ed25519_private(key: str) -> Ed25519PrivateKey | None:
    """Parse an Ed25519 private key from PEM or raw (hex/base64 32 bytes)."""
    text = key.strip()
    if "PRIVATE KEY" in text:
        try:
            loaded = serialization.load_pem_private_key(text.encode("utf-8"), password=None)
        except (ValueError, TypeError):
            return None
        return loaded if isinstance(loaded, Ed25519PrivateKey) else None
    raw = _decode_raw_key_bytes(text)
    if raw is not None and len(raw) == 32:
        try:
            return Ed25519PrivateKey.from_private_bytes(raw)
        except ValueError:
            return None
    return None


def _load_ed25519_public(key: str) -> Ed25519PublicKey | None:
    """Parse an Ed25519 public key; also derives it from a private key."""
    private = _load_ed25519_private(key)
    if private is not None:
        return private.public_key()
    text = key.strip()
    if "PUBLIC KEY" in text:
        try:
            loaded = serialization.load_pem_public_key(text.encode("utf-8"))
        except (ValueError, TypeError):
            return None
        return loaded if isinstance(loaded, Ed25519PublicKey) else None
    return None


def _decode_raw_key_bytes(text: str) -> bytes | None:
    """Decode a raw 32-byte key given as hex or base64, else None."""
    for decoder in (
        lambda s: binascii.unhexlify(s) if len(s) == 64 else None,
        lambda s: base64.b64decode(s, validate=True),
    ):
        try:
            decoded = decoder(text)
        except (binascii.Error, ValueError):
            continue
        if decoded:
            return decoded
    return None


def _ed25519_public_fingerprint(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return f"{ED25519_FINGERPRINT_PREFIX}{hashlib.sha256(raw).hexdigest()[:16]}"


def read_signing_key(value: str | None = None, key_file: Path | None = None) -> str | None:
    """Read a signing key from an inline value or file."""
    if value:
        return value
    if key_file:
        return key_file.read_text(encoding="utf-8").strip()
    return None


def classify_ed25519_key(key: str) -> str:
    """Return ``"private"``, ``"public"``, or ``"unknown"`` for a candidate key.

    Callers that must never hold secret material — a trust store provisioned
    onto edge devices, for instance — need to tell a private key from a public
    one *before* storing it. ``_load_ed25519_public`` deliberately derives the
    public half from a private key, so it cannot make that distinction alone.
    """
    if _load_ed25519_private(key) is not None:
        return "private"
    if _load_ed25519_public(key) is not None:
        return "public"
    return "unknown"


def signing_key_fingerprint(key: str) -> str:
    """Return a stable non-secret fingerprint for audit logs.

    For Ed25519 keys the fingerprint is derived from the public key, so the
    signer and any verifier compute the same value. For a legacy HMAC secret it
    is the hash of the secret string (unchanged).
    """
    public = _load_ed25519_public(key)
    if public is not None:
        return _ed25519_public_fingerprint(public)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{KEY_FINGERPRINT_PREFIX}{digest[:16]}"


def package_file_hashes(package_path: Path) -> dict[str, str]:
    """Return SHA256 hashes for all package files covered by the signature."""
    _ensure_safe_package_tree(package_path)
    hashes: dict[str, str] = {}
    for path in sorted(package_path.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(package_path).as_posix()
        if rel == SIGNATURE_FILE:
            continue
        hashes[rel] = sha256_file(path)
    return hashes


def sign_package(package_path: Path, key: str, signer: str = "temms") -> Path:
    """Create or replace signature.json for a package directory."""
    if not (package_path / "manifest.json").exists():
        raise ValueError(f"Missing manifest.json in package: {package_path}")
    _ensure_safe_package_tree(package_path)

    private = _load_ed25519_private(key)
    if private is not None:
        algorithm = ED25519_ALGORITHM
    elif _load_ed25519_public(key) is not None:
        raise ValueError("Signing requires an Ed25519 private key, not a public key")
    else:
        algorithm = SIGNATURE_ALGORITHM  # legacy HMAC

    payload = {
        "schema_version": "temms-signature/v1",
        "algorithm": algorithm,
        "signed_at": datetime.utcnow().isoformat() + "Z",
        "signer": signer,
        "key_fingerprint": signing_key_fingerprint(key),
        "manifest_sha256": sha256_file(package_path / "manifest.json"),
        "files": package_file_hashes(package_path),
    }
    if private is not None:
        payload["signature"] = base64.b64encode(
            private.sign(_canonical_payload_bytes(payload))
        ).decode("ascii")
    else:
        payload["signature"] = _signature_for_payload(payload, key)

    signature_path = package_path / SIGNATURE_FILE
    signature_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return signature_path


def _read_package_signature(package_path: Path) -> dict[str, Any]:
    """Read signature.json for a package."""
    signature_path = package_path / SIGNATURE_FILE
    if not signature_path.exists():
        raise ValueError(f"Missing {SIGNATURE_FILE}")
    return json.loads(signature_path.read_text(encoding="utf-8"))


def verify_package_signature_with_trust_store(
    package_path: Path,
    store: Any,
    now: Any = None,
) -> dict[str, Any]:
    """Verify a package against any trusted, unexpired key in ``store``.

    This is the DDIL verification path: no CA, no transparency log, just a set
    of provisioned public keys. Rotation works because both the outgoing and
    incoming keys can be trusted at once. The returned metadata records *which*
    key verified, so evidence answers "who signed this" and not merely "it was
    signed".
    """
    signature = _read_package_signature(package_path)
    if signature.get("algorithm") != ED25519_ALGORITHM:
        raise ValueError(
            "trust store verification requires an Ed25519 signature; "
            f"package is signed with {signature.get('algorithm')}"
        )

    # Validate the encoding once, up front. The trust store swallows exceptions
    # while probing candidate keys, so without this a corrupt signature would be
    # reported as "no trusted key verified it" — blaming the operator's trust
    # configuration for what is actually a malformed package.
    try:
        base64.b64decode(str(signature.get("signature")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Malformed Ed25519 signature encoding") from exc

    def _verifier(public_key: str) -> bool:
        _verify_ed25519_signature(signature, public_key)
        return True

    trusted = store.verify_with_any(_verifier, signature.get("key_fingerprint"), now)

    # Reuse the single-key path for the file/manifest hash checks so package
    # integrity is enforced in exactly one place.
    result = verify_package_signature(package_path, trusted.public_key)
    result["verified_by_fingerprint"] = trusted.fingerprint
    result["verified_by_label"] = trusted.label
    return result


def verify_package_signature(package_path: Path, key: str) -> dict[str, Any]:
    """Verify signature.json and all covered file hashes."""
    signature = _read_package_signature(package_path)
    algorithm = signature.get("algorithm")
    if algorithm == ED25519_ALGORITHM:
        _verify_ed25519_signature(signature, key)
    elif algorithm == SIGNATURE_ALGORITHM:
        expected_signature = signature.get("signature")
        computed_signature = _signature_for_payload(signature, key)
        if not hmac.compare_digest(str(expected_signature), computed_signature):
            raise ValueError("Package signature mismatch")
    else:
        raise ValueError(f"Unsupported signature algorithm: {algorithm}")

    key_fingerprint = signing_key_fingerprint(key)
    declared_fingerprint = signature.get("key_fingerprint")
    if declared_fingerprint and declared_fingerprint != key_fingerprint:
        raise ValueError("Signing key fingerprint mismatch")

    manifest_hash = sha256_file(package_path / "manifest.json")
    if manifest_hash != signature.get("manifest_sha256"):
        raise ValueError("Manifest hash does not match package signature")

    expected_files = signature.get("files", {})
    current_files = package_file_hashes(package_path)
    if expected_files != current_files:
        raise ValueError("Package file hashes do not match signature")

    return {
        "schema_version": signature.get("schema_version"),
        "algorithm": signature.get("algorithm"),
        "signed_at": signature.get("signed_at"),
        "signer": signature.get("signer"),
        "key_fingerprint": declared_fingerprint or key_fingerprint,
        "key_fingerprint_verified": bool(declared_fingerprint),
        "manifest_sha256": signature.get("manifest_sha256"),
    }


def sha256_file(path: Path) -> str:
    """Compute SHA256 for a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_safe_package_tree(package_path: Path) -> None:
    """Raise if a directory package contains links or special files."""
    errors: list[str] = []
    _reject_unsafe_package_tree(package_path, errors)
    if errors:
        raise ValueError("; ".join(errors))


def _reject_unsafe_package_tree(package_path: Path, errors: list[str]) -> None:
    """Reject links and special files in directory packages."""
    for path in sorted(package_path.rglob("*")):
        rel = path.relative_to(package_path).as_posix()
        if path.is_symlink():
            errors.append(f"Package links are not allowed: {rel}")
            continue
        if path.is_dir() or path.is_file():
            continue
        errors.append(f"Package path must be a regular file or directory: {rel}")


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical bytes covered by a signature (the payload minus 'signature')."""
    unsigned = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature_for_payload(payload: dict[str, Any], key: str) -> str:
    return hmac.new(
        key.encode("utf-8"), _canonical_payload_bytes(payload), hashlib.sha256
    ).hexdigest()


def _verify_ed25519_signature(signature: dict[str, Any], key: str) -> None:
    """Verify an Ed25519 package signature with the provided public/private key."""
    public = _load_ed25519_public(key)
    if public is None:
        raise ValueError("Ed25519 signature requires an Ed25519 public key to verify")
    try:
        raw_signature = base64.b64decode(str(signature.get("signature")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Malformed Ed25519 signature encoding") from exc
    try:
        public.verify(raw_signature, _canonical_payload_bytes(signature))
    except InvalidSignature as exc:
        raise ValueError("Package signature mismatch") from exc


def generate_ed25519_keypair() -> tuple[str, str, str]:
    """Return (private_pem, public_pem, fingerprint) for a fresh Ed25519 key."""
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    return private_pem, public_pem, _ed25519_public_fingerprint(private.public_key())


def ed25519_sign(data: bytes, key: str) -> str:
    """Sign raw bytes with an Ed25519 private key; return a base64 signature."""
    private = _load_ed25519_private(key)
    if private is None:
        raise ValueError("Ed25519 signing requires an Ed25519 private key")
    return base64.b64encode(private.sign(data)).decode("ascii")


def ed25519_verify(data: bytes, signature_b64: str, key: str) -> bool:
    """Verify a base64 Ed25519 signature over raw bytes with a public (or private) key."""
    public = _load_ed25519_public(key)
    if public is None:
        raise ValueError("Ed25519 verification requires an Ed25519 public key")
    try:
        public.verify(base64.b64decode(signature_b64, validate=True), data)
    except (InvalidSignature, binascii.Error, ValueError):
        return False
    return True
