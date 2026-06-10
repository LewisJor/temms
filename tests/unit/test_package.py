"""
Unit tests for package import evidence handling.
"""

import base64
import hashlib
import json
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from temms.core.package import PackageImporter


def _write_package(temp_dir, manifest):
    """Create a minimal package directory for importer tests."""
    package_dir = temp_dir / manifest["package_id"]
    models_dir = package_dir / "models"
    models_dir.mkdir(parents=True)

    model_file = models_dir / "detector.onnx"
    model_file.write_bytes(b"fake model bytes")
    sha256 = hashlib.sha256(model_file.read_bytes()).hexdigest()
    manifest["models"] = [{
        "id": "detector-v1",
        "name": "detector",
        "version": "1.0.0",
        "format": "onnx",
        "filename": model_file.name,
        "sha256": sha256,
        "size_bytes": model_file.stat().st_size,
        "metadata": {},
    }]
    manifest.setdefault("policies", [])

    (package_dir / "manifest.json").write_text(json.dumps(manifest))
    return package_dir


def _sign_manifest(manifest, private_key):
    """Sign the canonical TEMMS manifest payload."""
    payload = json.dumps(
        {
            key: value
            for key, value in manifest.items()
            if key not in {"signature", "signatures"}
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.b64encode(private_key.sign(payload)).decode()


def _public_key_b64(private_key):
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(public_key).decode()


def test_import_package_verifies_ed25519_manifest_signature(
    temp_dir,
    model_cache,
    model_storage,
):
    """A trusted Ed25519 manifest signature should become Signed evidence."""
    private_key = Ed25519PrivateKey.generate()
    manifest = {
        "schema_version": "v1",
        "package_id": "signed-package",
        "name": "detector",
        "version": "1.0.0",
        "created_at": "2026-06-09T00:00:00Z",
        "validation": {
            "sim_passed": True,
            "sim_evidence": {
                "source": "temms-sim",
                "scenario": "fog-regression",
                "run_id": "sim-42",
            },
            "tests_passed": True,
            "test_evidence": {
                "source": "pytest",
                "suite": "unit-readiness",
                "run_id": "ci-99",
            },
        },
    }
    package_dir = _write_package(temp_dir, manifest)

    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": "builder-key",
        "signature": _sign_manifest(manifest, private_key),
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest))

    importer = PackageImporter(temp_dir / "cache", model_cache, model_storage)
    with patch.dict(
        "os.environ",
        {
            "TEMMS_TRUSTED_SIGNATURE_KEYS": json.dumps({
                "builder-key": _public_key_b64(private_key),
            })
        },
        clear=False,
    ):
        result = importer.import_package(package_dir, verify=True)

    validation = result.manifest.validation
    assert validation["hash_verified"] is True
    assert validation["signature_present"] is True
    assert validation["signature_verified"] is True
    assert validation["signature_key_id"] == "builder-key"
    assert validation["signature_algorithm"] == "ed25519"
    assert validation["signature_detail"] == "Signature verified"
    assert validation["sim_passed"] is True
    assert validation["sim_evidence"]["source"] == "temms-sim"
    assert validation["sim_evidence"]["detail"] == "fog-regression"
    assert validation["sim_evidence"]["run_id"] == "sim-42"
    assert validation["sim_evidence"]["protected_by_signature"] is True
    assert validation["tests_passed"] is True
    assert validation["test_evidence"]["source"] == "pytest"
    assert validation["test_evidence"]["detail"] == "unit-readiness"
    assert validation["test_evidence"]["run_id"] == "ci-99"
    assert validation["test_evidence"]["protected_by_signature"] is True
    assert result.models[0].metadata["validation"]["sim_evidence"]["source"] == "temms-sim"
    assert result.models[0].metadata["validation"]["test_evidence"]["source"] == "pytest"


def test_import_package_does_not_trust_builder_signature_verified_claim(
    temp_dir,
    model_cache,
    model_storage,
):
    """Importer should not treat unsigned builder claims as verified signatures."""
    manifest = {
        "schema_version": "v1",
        "package_id": "claim-only-package",
        "name": "detector",
        "version": "1.0.0",
        "created_at": "2026-06-09T00:00:00Z",
        "validation": {
            "signature_verified": True,
            "sim_passed": True,
            "tests_passed": True,
        },
    }
    package_dir = _write_package(temp_dir, manifest)

    importer = PackageImporter(temp_dir / "cache", model_cache, model_storage)
    result = importer.import_package(package_dir, verify=True)

    validation = result.manifest.validation
    assert validation["signature_present"] is False
    assert validation["signature_verified"] is False
    assert validation["signature_detail"] == "No signature"
    assert validation["sim_passed"] is True
    assert validation["sim_evidence"]["detail"] == "Unsigned manifest claim"
    assert validation["sim_evidence"]["protected_by_signature"] is False
    assert validation["tests_passed"] is True
    assert validation["test_evidence"]["detail"] == "Unsigned manifest claim"
    assert validation["test_evidence"]["protected_by_signature"] is False
    assert result.models[0].metadata["validation"]["signature_verified"] is False


def test_import_package_requires_locally_trusted_signature_key(
    temp_dir,
    model_cache,
    model_storage,
):
    """Signed packages should stay unverified until the key is trusted locally."""
    private_key = Ed25519PrivateKey.generate()
    manifest = {
        "schema_version": "v1",
        "package_id": "untrusted-signed-package",
        "name": "detector",
        "version": "1.0.0",
        "created_at": "2026-06-09T00:00:00Z",
        "validation": {
            "sim_passed": True,
            "tests_passed": True,
        },
    }
    package_dir = _write_package(temp_dir, manifest)
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": "builder-key",
        "signature": _sign_manifest(manifest, private_key),
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest))

    importer = PackageImporter(temp_dir / "cache", model_cache, model_storage)
    with patch.dict("os.environ", {}, clear=True):
        result = importer.import_package(package_dir, verify=True)

    validation = result.manifest.validation
    assert validation["signature_present"] is True
    assert validation["signature_verified"] is False
    assert validation["signature_detail"] == "Trusted key not configured"
