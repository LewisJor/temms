"""
TEMMS package format and importer.

Package structure:
temms-package/
├── manifest.json     # What's in the package
├── models/           # Pre-validated artifacts
└── policies/         # Policy files to load
"""

import base64
import binascii
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ModelArtifact(BaseModel):
    """Model artifact in package."""
    id: str
    name: str
    version: str
    format: str  # onnx, tflite, torchscript
    filename: str
    sha256: str
    size_bytes: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyArtifact(BaseModel):
    """Policy artifact in package."""
    name: str
    filename: str
    slot: str | None = None  # Which slot this policy controls


class PackageManifest(BaseModel):
    """TEMMS package manifest."""
    schema_version: str = Field(default="v1")
    package_id: str
    name: str
    version: str
    description: str | None = None
    created_at: str
    created_by: str | None = None  # MLflow user, Hub identifier, etc.

    models: list[ModelArtifact] = Field(default_factory=list)
    policies: list[PolicyArtifact] = Field(default_factory=list)

    # Optional metadata
    source_registry: str | None = None  # MLflow tracking URI
    mlflow_run_id: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    signature: dict[str, Any] | None = None
    signatures: list[dict[str, Any]] = Field(default_factory=list)
    attestations: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path) -> "PackageManifest":
        """Load manifest from JSON file."""
        with open(path) as f:
            data = json.load(f)
        return cls(**data)

    def to_file(self, path: Path) -> None:
        """Save manifest to JSON file."""
        with open(path, "w") as f:
            json.dump(self.model_dump(), f, indent=2)


@dataclass
class ImportedPackageResult:
    """Result of package import operation."""
    package: Any  # ImportedPackage
    models: list[Any]  # List[CachedModel]
    policies: list[PolicyArtifact]
    manifest: PackageManifest


@dataclass
class SignatureVerification:
    """Result of package manifest signature verification."""
    present: bool = False
    verified: bool = False
    key_id: str | None = None
    algorithm: str | None = None
    detail: str = "No signature"


class PackageImporter:
    """Imports TEMMS packages into local cache."""

    def __init__(self, cache_dir: Path, model_cache, storage):
        """
        Initialize importer.

        Args:
            cache_dir: Where to store imported packages
            model_cache: ModelCache instance
            storage: ModelStorage instance
        """
        self.cache_dir = cache_dir
        self.model_cache = model_cache
        self.storage = storage
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def import_package(self, package_path: Path, verify: bool = True) -> ImportedPackageResult:
        """
        Import a TEMMS package.

        Args:
            package_path: Path to package directory
            verify: Whether to verify model hashes

        Returns:
            ImportedPackageResult with details
        """
        if not package_path.exists():
            raise FileNotFoundError(f"Package not found: {package_path}")

        # Load manifest
        manifest_path = package_path / "manifest.json"
        if not manifest_path.exists():
            raise ValueError(f"Missing manifest.json in package: {package_path}")

        with open(manifest_path) as f:
            manifest_data = json.load(f)
        manifest = PackageManifest(**manifest_data)
        signature_verification = self._verify_manifest_signature(manifest_data)
        validation = self._build_import_validation(
            manifest,
            verify,
            signature_verification,
        )

        # Verify and import models
        models_dir = package_path / "models"
        imported_models = []

        for model_artifact in manifest.models:
            model_file = models_dir / model_artifact.filename
            if not model_file.exists():
                raise FileNotFoundError(f"Model file not found: {model_file}")

            # Verify hash if requested
            if verify:
                computed_hash = self._compute_hash(model_file)
                if computed_hash != model_artifact.sha256:
                    raise ValueError(
                        f"Hash mismatch for {model_artifact.filename}: "
                        f"expected {model_artifact.sha256}, got {computed_hash}"
                    )
                validation["model_hashes"][model_artifact.id] = {
                    "filename": model_artifact.filename,
                    "sha256": computed_hash,
                    "hash_verified": True,
                }
            else:
                validation["model_hashes"][model_artifact.id] = {
                    "filename": model_artifact.filename,
                    "sha256": model_artifact.sha256,
                    "hash_verified": False,
                }

            # Store model
            dest_path, _, size_bytes = self.storage.store_model(
                model_file, model_artifact.id, verify=False  # Already verified above
            )

            # Add to cache
            from temms.core.cache import ModelFormat
            model_metadata = dict(model_artifact.metadata)
            model_validation = {
                **model_metadata.get("validation", {}),
                "signature_present": validation["signature_present"],
                "signature_verified": validation["signature_verified"],
                "signature_key_id": validation["signature_key_id"],
                "signature_algorithm": validation["signature_algorithm"],
                "signature_detail": validation["signature_detail"],
                "sim_passed": validation["sim_passed"],
                "sim_evidence": validation["sim_evidence"],
                "tests_passed": validation["tests_passed"],
                "test_evidence": validation["test_evidence"],
            }
            model_metadata["validation"] = {
                **model_validation,
                "hash_verified": bool(verify),
                "sha256": model_artifact.sha256,
                "source_manifest": manifest.package_id,
            }
            cached_model = self.model_cache.add_cached_model(
                model_id=model_artifact.id,
                name=model_artifact.name,
                version=model_artifact.version,
                format=ModelFormat(model_artifact.format),
                path=dest_path,
                sha256=model_artifact.sha256,
                size_bytes=size_bytes,
                package_id=manifest.package_id,
                metadata=model_metadata,
            )
            imported_models.append(cached_model)

        # Copy policies
        policies_dir = package_path / "policies"
        imported_policies = []

        if policies_dir.exists():
            policy_dest_dir = self.cache_dir / "policies" / manifest.package_id
            policy_dest_dir.mkdir(parents=True, exist_ok=True)

            for policy_artifact in manifest.policies:
                policy_file = policies_dir / policy_artifact.filename
                if policy_file.exists():
                    shutil.copy(policy_file, policy_dest_dir / policy_artifact.filename)
                    imported_policies.append(policy_artifact)

        validation["hash_verified"] = bool(
            verify and validation["model_hashes"] and
            all(item["hash_verified"] for item in validation["model_hashes"].values())
        )
        manifest.validation = {
            **manifest.validation,
            **validation,
        }

        # Record package
        package = self.model_cache.add_package(
            package_id=manifest.package_id,
            name=manifest.name,
            version=manifest.version,
            source=str(package_path),
            manifest=manifest.model_dump(),
        )

        result = ImportedPackageResult(
            package=package,
            models=imported_models,
            policies=imported_policies,
            manifest=manifest,
        )

        # Optional local-dev mirror. Hub treats MLflow as an upstream registry by
        # default, so imports are not written back unless explicitly enabled.
        if os.environ.get("TEMMS_MLFLOW_AUTO_REGISTER", "").lower() == "true":
            try:
                from temms.mlflow_bridge import MLflowBridge
                bridge = MLflowBridge()
                if bridge.available:
                    bridge.register_imported_models(result)
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(f"MLflow registration skipped: {e}")

        return result

    def _build_import_validation(
        self,
        manifest: PackageManifest,
        verify: bool,
        signature_verification: SignatureVerification,
    ) -> dict[str, Any]:
        """Create operator-facing evidence recorded at import time."""
        existing = manifest.validation or {}
        sim_passed = self._truthy(existing.get("sim_passed"))
        tests_passed = self._truthy(existing.get("tests_passed"))

        return {
            "importer": "temms.PackageImporter",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "verify_requested": verify,
            "hash_verified": False,
            "model_hashes": {},
            "signature_present": signature_verification.present,
            "signature_verified": signature_verification.verified,
            "signature_key_id": signature_verification.key_id,
            "signature_algorithm": signature_verification.algorithm,
            "signature_detail": signature_verification.detail,
            "sim_passed": sim_passed,
            "sim_evidence": self._readiness_evidence(
                existing,
                manifest.attestations,
                category="sim",
                passed=sim_passed,
                signature_verified=signature_verification.verified,
                aliases=("simulation",),
            ),
            "tests_passed": tests_passed,
            "test_evidence": self._readiness_evidence(
                existing,
                manifest.attestations,
                category="test",
                passed=tests_passed,
                signature_verified=signature_verification.verified,
                aliases=("tests",),
            ),
        }

    def _readiness_evidence(
        self,
        validation: dict[str, Any],
        attestations: dict[str, Any],
        *,
        category: str,
        passed: bool,
        signature_verified: bool,
        aliases: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Build a compact provenance record for sim/test evidence."""
        names = (category, *aliases)
        nested_sources = [
            value
            for name in names
            for value in (
                validation.get(f"{name}_evidence"),
                validation.get(name),
                attestations.get(name),
            )
            if isinstance(value, dict)
        ]
        source = self._first_evidence_value(
            validation,
            *nested_sources,
            keys=tuple(f"{name}_source" for name in names) + ("source", "tool"),
        )
        detail = self._first_evidence_value(
            validation,
            *nested_sources,
            keys=tuple(f"{name}_detail" for name in names) + (
                "detail",
                "scenario",
                "suite",
                "name",
            ),
        )
        run_id = self._first_evidence_value(
            validation,
            *nested_sources,
            keys=tuple(f"{name}_run_id" for name in names) + ("run_id", "id"),
        )
        recorded_at = self._first_evidence_value(
            validation,
            *nested_sources,
            keys=tuple(f"{name}_at" for name in names) + (
                "recorded_at",
                "completed_at",
                "timestamp",
            ),
        )

        if passed and not source:
            source = "manifest.validation"
        if passed and not detail:
            detail = (
                "Protected by verified manifest signature"
                if signature_verified
                else "Unsigned manifest claim"
            )

        return {
            "passed": passed,
            "source": source or None,
            "detail": detail or None,
            "run_id": run_id or None,
            "recorded_at": recorded_at or None,
            "protected_by_signature": signature_verified,
        }

    @staticmethod
    def _first_evidence_value(
        *sources: dict[str, Any],
        keys: tuple[str, ...],
    ) -> str | None:
        """Find the first non-empty provenance value from flat/nested evidence."""
        normalized_keys = {
            key.strip().lower().replace("-", "_")
            for key in keys
        }
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                normalized_key = str(key).strip().lower().replace("-", "_")
                if normalized_key not in normalized_keys:
                    continue
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    return text
        return None

    def _verify_manifest_signature(
        self,
        manifest_data: dict[str, Any],
    ) -> SignatureVerification:
        """Verify an Ed25519 package manifest signature when trusted keys exist."""
        signatures = self._manifest_signature_entries(manifest_data)
        if not signatures:
            return SignatureVerification()

        trusted_keys, key_errors = self._trusted_signature_keys()
        if not trusted_keys:
            detail = "Trusted key not configured"
            if key_errors:
                detail = f"{detail}: {'; '.join(key_errors)}"
            return SignatureVerification(present=True, detail=detail)

        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
        except ImportError as exc:
            return SignatureVerification(
                present=True,
                detail=f"cryptography unavailable: {exc}",
            )

        payload = self._canonical_manifest_payload(manifest_data)
        failures = list(key_errors)

        for entry in signatures:
            if not isinstance(entry, dict):
                failures.append("Signature entry must be an object")
                continue

            algorithm = str(entry.get("algorithm") or entry.get("alg") or "ed25519")
            algorithm = algorithm.lower()
            key_id = str(entry.get("key_id") or entry.get("kid") or "")
            signature_value = (
                entry.get("signature")
                or entry.get("value")
                or entry.get("sig")
            )

            if algorithm != "ed25519":
                failures.append(f"{key_id or 'unknown'}: unsupported {algorithm}")
                continue
            if not key_id:
                failures.append("Signature missing key_id")
                continue
            if not signature_value:
                failures.append(f"{key_id}: signature value missing")
                continue

            public_key = trusted_keys.get(key_id)
            if public_key is None:
                failures.append(f"{key_id}: trusted key not found")
                continue

            signature_bytes = self._decode_key_material(signature_value)
            if signature_bytes is None:
                failures.append(f"{key_id}: signature is not base64 or hex")
                continue

            try:
                Ed25519PublicKey.from_public_bytes(public_key).verify(
                    signature_bytes,
                    payload,
                )
                return SignatureVerification(
                    present=True,
                    verified=True,
                    key_id=key_id,
                    algorithm=algorithm,
                    detail="Signature verified",
                )
            except InvalidSignature:
                failures.append(f"{key_id}: invalid signature")
            except ValueError as exc:
                failures.append(f"{key_id}: invalid public key: {exc}")

        detail = "; ".join(failures) if failures else "No usable signature"
        return SignatureVerification(present=True, detail=detail)

    @staticmethod
    def _manifest_signature_entries(manifest_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Return all top-level signature declarations from a manifest."""
        entries = []
        signature = manifest_data.get("signature")
        if signature:
            entries.append(signature)
        signatures = manifest_data.get("signatures") or []
        if isinstance(signatures, list):
            entries.extend(signatures)
        return entries

    @staticmethod
    def _canonical_manifest_payload(manifest_data: dict[str, Any]) -> bytes:
        """Canonical bytes signed by package builders."""
        signed_manifest = {
            key: value
            for key, value in manifest_data.items()
            if key not in {"signature", "signatures"}
        }
        return json.dumps(
            signed_manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def _trusted_signature_keys(self) -> tuple[dict[str, bytes], list[str]]:
        """Load locally trusted Ed25519 public keys from environment config."""
        keys: dict[str, bytes] = {}
        errors: list[str] = []

        inline_keys = os.environ.get("TEMMS_TRUSTED_SIGNATURE_KEYS", "").strip()
        if inline_keys:
            self._merge_trusted_keys(
                keys,
                errors,
                inline_keys,
                "TEMMS_TRUSTED_SIGNATURE_KEYS",
            )

        keys_file = os.environ.get("TEMMS_TRUSTED_SIGNATURE_KEYS_FILE", "").strip()
        if keys_file:
            try:
                file_value = Path(keys_file).read_text()
                self._merge_trusted_keys(keys, errors, file_value, keys_file)
            except OSError as exc:
                errors.append(f"{keys_file}: {exc}")

        return keys, errors

    def _merge_trusted_keys(
        self,
        keys: dict[str, bytes],
        errors: list[str],
        raw_value: str,
        source: str,
    ) -> None:
        """Merge JSON trusted-key config into a key map."""
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            errors.append(f"{source}: invalid JSON: {exc.msg}")
            return

        if isinstance(parsed, dict):
            entries = parsed.items()
        elif isinstance(parsed, list):
            entries = (
                (
                    item.get("key_id") or item.get("kid"),
                    item.get("public_key") or item.get("key"),
                )
                for item in parsed
                if isinstance(item, dict)
            )
        else:
            errors.append(f"{source}: expected object or list")
            return

        for key_id, material in entries:
            if not key_id:
                errors.append(f"{source}: trusted key missing key_id")
                continue
            public_key = self._decode_key_material(material)
            if public_key is None:
                errors.append(f"{source}: {key_id} key is not base64 or hex")
                continue
            if len(public_key) != 32:
                errors.append(f"{source}: {key_id} key must be 32 bytes")
                continue
            keys[str(key_id)] = public_key

    @staticmethod
    def _decode_key_material(value: Any) -> bytes | None:
        """Decode base64 or hex key/signature material."""
        if isinstance(value, bytes):
            return value
        if isinstance(value, dict):
            value = value.get("public_key") or value.get("key") or value.get("signature")
        if not isinstance(value, str):
            return None

        normalized = value.strip()
        for prefix in ("ed25519:", "base64:", "hex:"):
            if normalized.lower().startswith(prefix):
                normalized = normalized[len(prefix):]
                break

        try:
            return base64.b64decode(normalized, validate=True)
        except (binascii.Error, ValueError):
            pass

        try:
            return bytes.fromhex(normalized)
        except ValueError:
            return None

    @staticmethod
    def _truthy(value: Any) -> bool:
        """Interpret loose manifest values without treating unknown as pass."""
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return value > 0
        if isinstance(value, str):
            return value.strip().lower() not in {
                "",
                "0",
                "false",
                "fail",
                "failed",
                "no",
                "none",
                "unknown",
            }
        return bool(value)

    def _compute_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of file."""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()


def generate_ed25519_keypair() -> dict[str, str]:
    """Generate a raw Ed25519 keypair for TEMMS package signing."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Package signing requires Python cryptography. "
            "Install the TEMMS Docker/sim stack or add cryptography locally."
        ) from exc

    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "algorithm": "ed25519",
        "private_key": base64.b64encode(private_bytes).decode("ascii"),
        "public_key": base64.b64encode(public_bytes).decode("ascii"),
    }


def sign_manifest_data(
    manifest_data: dict[str, Any],
    *,
    key_id: str,
    private_key_material: str,
) -> dict[str, str]:
    """Sign canonical manifest data with a raw or PEM Ed25519 private key."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Package signing requires Python cryptography. "
            "Install the TEMMS Docker/sim stack or add cryptography locally."
        ) from exc

    private_key = None
    private_bytes = PackageImporter._decode_key_material(private_key_material)
    if private_bytes is not None:
        if len(private_bytes) != 32:
            raise ValueError("Ed25519 private key material must be 32 bytes")
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    else:
        loaded_key = serialization.load_pem_private_key(
            private_key_material.encode("utf-8"),
            password=None,
        )
        if not isinstance(loaded_key, Ed25519PrivateKey):
            raise ValueError("Private key must be an Ed25519 key")
        private_key = loaded_key

    signature = private_key.sign(
        PackageImporter._canonical_manifest_payload(manifest_data)
    )
    return {
        "algorithm": "ed25519",
        "key_id": key_id,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def sign_package_manifest(
    package_path: Path,
    *,
    key_id: str,
    private_key_material: str,
) -> dict[str, str]:
    """Sign package manifest.json in place and return the signature entry."""
    manifest_path = package_path / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest.json in package: {package_path}")

    with open(manifest_path) as f:
        manifest_data = json.load(f)

    signature = sign_manifest_data(
        manifest_data,
        key_id=key_id,
        private_key_material=private_key_material,
    )
    manifest_data["signature"] = signature
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2)
    return signature
