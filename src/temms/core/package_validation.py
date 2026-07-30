"""Package structure and posture validation (extracted from signing.py; #16/review).

``signing.py`` owns the cryptographic primitives and package sign/verify. This
module owns the separate concern of *validating* a package's structure, manifest
posture, model metadata, and file-tree safety — the checks that decide whether a
package is well-formed enough to catalog and deploy, independent of who signed it.

It depends on ``signing`` (one direction) for the low-level hash and signature
checks; ``signing`` does not depend on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temms.core.signing import (
    SIGNATURE_FILE,
    _reject_unsafe_package_tree,
    sha256_file,
    verify_package_signature,
)


@dataclass
class ValidationResult:
    """Result from package validation."""

    valid: bool
    errors: list[str]
    warnings: list[str]
    manifest: dict[str, Any] | None = None
    signature_verified: bool = False
    signature_metadata: dict[str, Any] | None = None


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
