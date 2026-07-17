"""Ed25519 asymmetric package signing (#14).

The property that matters for DDIL provenance: a device provisioned with the
public key can verify a package but cannot forge one, and verification is fully
offline. Legacy HMAC packages remain verifiable.
"""

from __future__ import annotations

import base64
import json

import pytest

from temms.core import signing


@pytest.fixture
def package(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "manifest.json").write_text(json.dumps({"package_id": "pkg-x", "name": "x"}))
    (pkg / "model.onnx").write_bytes(b"onnx-bytes")
    return pkg


@pytest.fixture
def keypair():
    return signing.generate_ed25519_keypair()  # (private_pem, public_pem, fingerprint)


def test_sign_with_private_verify_with_public(package, keypair):
    private_pem, public_pem, fingerprint = keypair
    signing.sign_package(package, private_pem, signer="hub")

    sig = json.loads((package / "signature.json").read_text())
    assert sig["algorithm"] == "Ed25519"
    assert sig["key_fingerprint"] == fingerprint

    # Verification with the PUBLIC key only (no private material).
    meta = signing.verify_package_signature(package, public_pem)
    assert meta["algorithm"] == "Ed25519"
    assert meta["key_fingerprint"] == fingerprint


def test_public_key_cannot_sign(package, keypair):
    _, public_pem, _ = keypair
    with pytest.raises(ValueError, match="private key"):
        signing.sign_package(package, public_pem)


def test_tamper_is_rejected(package, keypair):
    private_pem, public_pem, _ = keypair
    signing.sign_package(package, private_pem)
    (package / "model.onnx").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hashes do not match"):
        signing.verify_package_signature(package, public_pem)


def test_signature_forgery_is_rejected(package, keypair):
    private_pem, public_pem, _ = keypair
    signing.sign_package(package, private_pem)
    sig_path = package / "signature.json"
    sig = json.loads(sig_path.read_text())
    # Flip the signature bytes: a valid-shaped but wrong signature.
    forged = bytearray(base64.b64decode(sig["signature"]))
    forged[0] ^= 0xFF
    sig["signature"] = base64.b64encode(bytes(forged)).decode("ascii")
    sig_path.write_text(json.dumps(sig, indent=2, sort_keys=True))
    with pytest.raises(ValueError, match="signature mismatch"):
        signing.verify_package_signature(package, public_pem)


def test_wrong_public_key_is_rejected(package, keypair):
    private_pem, _, _ = keypair
    signing.sign_package(package, private_pem)
    _, other_public, _ = signing.generate_ed25519_keypair()
    # Fingerprint mismatch is caught before/independent of signature check.
    with pytest.raises(ValueError):
        signing.verify_package_signature(package, other_public)


def test_fingerprint_is_stable_and_public_derived(keypair):
    private_pem, public_pem, fingerprint = keypair
    assert fingerprint.startswith("ed25519:")
    # Signer (private) and verifier (public) compute the same fingerprint.
    assert signing.signing_key_fingerprint(private_pem) == fingerprint
    assert signing.signing_key_fingerprint(public_pem) == fingerprint


def test_raw_hex_private_key_is_accepted(package):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    raw_hex = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ).hex()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")

    signing.sign_package(package, raw_hex, signer="hub")
    assert json.loads((package / "signature.json").read_text())["algorithm"] == "Ed25519"
    signing.verify_package_signature(package, public_pem)


def test_legacy_hmac_still_round_trips(package):
    signing.sign_package(package, "shared-secret", signer="legacy")
    sig = json.loads((package / "signature.json").read_text())
    assert sig["algorithm"] == "HMAC-SHA256"
    signing.verify_package_signature(package, "shared-secret")
    with pytest.raises(ValueError):
        signing.verify_package_signature(package, "wrong-secret")


def test_generate_keypair_shapes(keypair):
    private_pem, public_pem, fingerprint = keypair
    assert "PRIVATE KEY" in private_pem
    assert "PUBLIC KEY" in public_pem
    assert fingerprint.startswith("ed25519:")
