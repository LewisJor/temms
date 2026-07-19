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
from dataclasses import dataclass
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


@dataclass
class ValidationResult:
    """Result from package validation."""

    valid: bool
    errors: list[str]
    warnings: list[str]
    manifest: dict[str, Any] | None = None
    signature_verified: bool = False
    signature_metadata: dict[str, Any] | None = None


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


def validate_package(
    package_path: Path,
    require_signature: bool = False,
    signing_key: str | None = None,
    device_profile: str | None = None,
    check_runtime_constraints: bool = False,
    strict_metadata: bool = False,
    runtime_capabilities: Any | None = None,
    model_id: str | None = None,
) -> ValidationResult:
    """Validate package structure, manifest hashes, and optional signature."""
    from temms.core.package_archive import package_directory

    try:
        with package_directory(package_path) as package_dir:
            return _validate_package_dir(
                package_dir,
                require_signature=require_signature,
                signing_key=signing_key,
                device_profile=device_profile,
                check_runtime_constraints=check_runtime_constraints,
                strict_metadata=strict_metadata,
                runtime_capabilities=runtime_capabilities,
                model_id=model_id,
            )
    except Exception as exc:
        return ValidationResult(False, [str(exc)], [])


def _validate_package_dir(
    package_path: Path,
    require_signature: bool = False,
    signing_key: str | None = None,
    device_profile: str | None = None,
    check_runtime_constraints: bool = False,
    strict_metadata: bool = False,
    runtime_capabilities: Any | None = None,
    model_id: str | None = None,
) -> ValidationResult:
    """Validate a directory package."""
    errors: list[str] = []
    warnings: list[str] = []
    manifest: dict[str, Any] | None = None
    signature_verified = False
    signature_metadata = None

    manifest_path = package_path / "manifest.json"
    if not manifest_path.exists():
        return ValidationResult(False, [f"Missing manifest.json: {package_path}"], warnings)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ValidationResult(False, [f"Invalid manifest JSON: {exc}"], warnings)

    if not isinstance(manifest, dict):
        return ValidationResult(False, ["Manifest JSON must be an object"], warnings)

    _reject_unsafe_package_tree(package_path, errors)

    if manifest.get("schema_version") != "v1":
        errors.append(f"Unsupported package schema_version: {manifest.get('schema_version')}")

    _validate_package_posture(manifest, strict_metadata, errors, warnings)

    try:
        from temms.core.cache import ModelFormat
        from temms.core.package import PackageManifest

        PackageManifest.model_validate(manifest)
        supported_formats = {item.value for item in ModelFormat}
    except Exception as exc:
        errors.append(f"Invalid package manifest: {exc}")
        supported_formats = set()

    models_dir = package_path / "models"
    policies_dir = package_path / "policies"
    models = [model for model in manifest.get("models", []) if isinstance(model, dict)]
    policies = [policy for policy in manifest.get("policies", []) if isinstance(policy, dict)]

    _reject_duplicate_manifest_values(
        (model.get("id") for model in models),
        "model id",
        errors,
    )
    _reject_duplicate_manifest_values(
        (model.get("filename") for model in models),
        "model filename",
        errors,
    )
    _reject_duplicate_manifest_values(
        (policy.get("name") for policy in policies),
        "policy name",
        errors,
    )
    _reject_duplicate_manifest_values(
        (policy.get("filename") for policy in policies),
        "policy filename",
        errors,
    )
    _reject_unsafe_manifest_component(manifest.get("package_id"), "package_id", errors)
    for model in models:
        _reject_unsafe_manifest_component(model.get("id"), "model id", errors)

    declared_model_files: set[Path] = set()
    declared_policy_files: set[Path] = set()

    for model in models:
        filename = model.get("filename")
        expected_sha = model.get("sha256")
        expected_size = model.get("size_bytes")
        model_format = model.get("format")
        if supported_formats and model_format not in supported_formats:
            errors.append(f"Unsupported model format for models/{filename}: {model_format}")
        if not filename:
            errors.append("Model entry missing filename")
            continue
        model_path = _manifest_file_path(models_dir, filename, "model", errors)
        if model_path is None:
            continue
        declared_model_files.add(model_path)
        if not model_path.exists():
            errors.append(f"Missing model file: models/{filename}")
            continue
        if not model_path.is_file():
            errors.append(f"Model path is not a regular file: models/{filename}")
            continue
        actual_sha = sha256_file(model_path)
        if expected_sha and actual_sha != expected_sha:
            errors.append(f"Hash mismatch for models/{filename}")
        if expected_size is None:
            errors.append(f"Model entry missing size_bytes: models/{filename}")
        elif not isinstance(expected_size, int) or expected_size < 0:
            errors.append(f"Invalid size_bytes for models/{filename}: {expected_size}")
        elif model_path.stat().st_size != expected_size:
            errors.append(
                f"Size mismatch for models/{filename}: "
                f"expected {expected_size}, got {model_path.stat().st_size}"
            )
        _validate_model_metadata(model, filename, strict_metadata, errors, warnings)

    for policy in policies:
        filename = policy.get("filename")
        if filename:
            policy_path = _manifest_file_path(
                policies_dir,
                filename,
                "policy",
                errors,
                basename_only=True,
            )
            if policy_path is None:
                continue
            declared_policy_files.add(policy_path)
            if not policy_path.exists():
                errors.append(f"Missing policy file: policies/{filename}")
            elif not policy_path.is_file():
                errors.append(f"Policy path is not a regular file: policies/{filename}")
            else:
                _validate_policy_file(policy_path, filename, policy, errors)

    _reject_undeclared_files(models_dir, declared_model_files, "model", errors)
    _reject_undeclared_files(policies_dir, declared_policy_files, "policy", errors)

    if device_profile:
        from temms.core.runtime_profiles import normalize_device_profile

        checked_profile = normalize_device_profile(device_profile)
        allowed_profiles = {
            profile
            for profile in (
                normalize_device_profile(profile)
                for profile in manifest.get("compatibility", {}).get("device_profiles", [])
            )
            if profile
        }
        for model in manifest.get("models", []):
            allowed_profiles.update(
                profile
                for profile in (
                    normalize_device_profile(profile)
                    for profile in model.get("runtime_constraints", {}).get("device_profiles", [])
                )
                if profile
            )
        if allowed_profiles and checked_profile not in allowed_profiles:
            errors.append(
                f"Package is not compatible with device profile {checked_profile}; "
                f"allowed profiles: {sorted(allowed_profiles)}"
            )

    if check_runtime_constraints and manifest is not None:
        from temms.core.runtime_profiles import (
            detect_runtime_capabilities,
            normalize_device_profile,
            package_runtime_constraints,
            runtime_constraints_satisfied,
        )

        capabilities = runtime_capabilities or detect_runtime_capabilities()
        if hasattr(capabilities, "to_dict"):
            capabilities = capabilities.to_dict()
        else:
            capabilities = dict(capabilities or {})
        if device_profile:
            capabilities["device_profile"] = normalize_device_profile(device_profile)

        for constrained_model_id, constraints in package_runtime_constraints(
            manifest,
            model_id=model_id,
        ):
            satisfied, reasons = runtime_constraints_satisfied(
                constraints,
                capabilities,
            )
            if not satisfied:
                errors.extend(
                    "Runtime constraints are not satisfied for " f"{constrained_model_id}: {reason}"
                    for reason in reasons
                )

    signature_path = package_path / SIGNATURE_FILE
    if require_signature or signature_path.exists():
        if signing_key is None:
            errors.append("Signature verification requires a signing key")
        else:
            try:
                signature_metadata = verify_package_signature(package_path, signing_key)
                signature_verified = True
            except Exception as exc:
                errors.append(str(exc))
    else:
        warnings.append("Package is unsigned")

    return ValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        manifest=manifest,
        signature_verified=signature_verified,
        signature_metadata=signature_metadata,
    )


def _validate_package_posture(
    manifest: dict[str, Any],
    strict_metadata: bool,
    errors: list[str],
    warnings: list[str],
) -> None:
    """Surface package-level posture markers such as local-development shortcuts."""
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("development_only"):
        return

    message = (
        "Package is marked development-only; rebuild with "
        "`temms package from-mlflow` for production edge deployment"
    )
    if strict_metadata:
        errors.append(message)
    else:
        warnings.append(message)


def _validate_model_metadata(
    model: dict[str, Any],
    filename: Any,
    strict_metadata: bool,
    errors: list[str],
    warnings: list[str],
) -> None:
    """Validate production metadata that makes a package auditable at the edge."""
    issues: list[str] = []
    input_schema = model.get("input_schema")
    output_schema = model.get("output_schema")
    provenance = model.get("provenance")
    runtime_constraints = model.get("runtime_constraints")
    benchmark = model.get("benchmark")

    if not isinstance(input_schema, dict) or not input_schema:
        issues.append("input_schema")
    if not isinstance(output_schema, dict) or not output_schema:
        issues.append("output_schema")
    if not isinstance(runtime_constraints, dict) or not runtime_constraints:
        issues.append("runtime_constraints")
    if not isinstance(benchmark, dict) or not benchmark:
        issues.append("benchmark")
    if not isinstance(provenance, dict) or not provenance:
        issues.append("provenance")
    else:
        required_provenance = ("source", "run_id", "artifact_sha256")
        missing_provenance = [key for key in required_provenance if not provenance.get(key)]
        if missing_provenance:
            issues.append("provenance." + ",".join(missing_provenance))

    if not issues:
        return

    message = (
        f"Model metadata incomplete for models/{filename}: "
        + ", ".join(str(issue) for issue in issues)
    )
    if strict_metadata:
        errors.append(message)
    else:
        warnings.append(message)


def _validate_policy_file(
    policy_path: Path,
    filename: str,
    manifest_policy: dict[str, Any],
    errors: list[str],
) -> None:
    """Validate that packaged policy YAML can be loaded by the edge policy engine."""
    try:
        from temms.policy.schema import SlotPolicy

        policy = SlotPolicy.from_yaml(policy_path)
    except Exception as exc:
        errors.append(f"Invalid policy file: policies/{filename}: {exc}")
        return

    manifest_slot = manifest_policy.get("slot")
    if manifest_slot and policy.spec.slot != manifest_slot:
        errors.append(
            f"Policy slot mismatch for policies/{filename}: "
            f"manifest declares {manifest_slot}, policy declares {policy.spec.slot}"
        )


def sha256_file(path: Path) -> str:
    """Compute SHA256 for a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_file_path(
    base_dir: Path,
    filename: Any,
    kind: str,
    errors: list[str],
    basename_only: bool = False,
) -> Path | None:
    """Resolve a manifest filename only if it stays within the package subdir."""
    if not isinstance(filename, str) or not filename.strip():
        errors.append(f"{kind.title()} filename must be a non-empty string")
        return None

    relative_path = Path(filename)
    if relative_path.is_absolute():
        errors.append(f"Unsafe {kind} filename: {filename} is absolute")
        return None
    if any(part in ("", ".", "..") for part in relative_path.parts):
        errors.append(f"Unsafe {kind} filename: {filename} contains path traversal")
        return None
    if basename_only and len(relative_path.parts) != 1:
        errors.append(f"Unsafe {kind} filename: {filename} must be a file name, not a path")
        return None

    candidate = base_dir / relative_path
    try:
        candidate.resolve().relative_to(base_dir.resolve())
    except ValueError:
        errors.append(f"Unsafe {kind} filename: {filename} escapes {base_dir.name}/")
        return None
    return candidate


def _reject_unsafe_manifest_component(value: Any, label: str, errors: list[str]) -> None:
    """Reject manifest identifiers that are unsafe as filesystem components."""
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return
    component = Path(value)
    if component.is_absolute() or len(component.parts) != 1 or component.name in {".", ".."}:
        errors.append(f"Unsafe {label}: {value}")


def _reject_duplicate_manifest_values(
    values: Any,
    label: str,
    errors: list[str],
) -> None:
    """Reject repeated non-empty manifest values that would make imports ambiguous."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    for value in sorted(duplicates):
        errors.append(f"Duplicate {label} in manifest: {value}")


def _reject_undeclared_files(
    base_dir: Path,
    declared_files: set[Path],
    kind: str,
    errors: list[str],
) -> None:
    """Reject files in package artifact directories that the manifest does not declare."""
    if not base_dir.exists():
        return
    declared = {path.resolve() for path in declared_files}
    for path in sorted(base_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.resolve() in declared:
            continue
        rel = path.relative_to(base_dir).as_posix()
        errors.append(f"Undeclared {kind} file in package: {base_dir.name}/{rel}")


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
