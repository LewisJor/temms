"""
Catalog helpers for TEMMS package artifacts.

Hub Lite catalog entries should be derived from the package artifact whenever
possible so operators do not have to duplicate manifest metadata by hand.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from temms.core.package import PackageManifest
from temms.core.package_archive import package_directory
from temms.core.runtime_profiles import normalize_device_profile
from temms.core.signing import read_signing_key, sha256_file, validate_package


def package_source_sha256(package_path: Path) -> str:
    """Return a stable SHA256 for a package archive or directory tree."""
    if package_path.is_file():
        return sha256_file(package_path)
    if package_path.is_dir():
        return sha256_directory(package_path)
    raise FileNotFoundError(f"Package not found: {package_path}")


def sha256_directory(directory: Path) -> str:
    """Return a deterministic SHA256 over all regular files in a directory."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Package links are not allowed: {path.relative_to(directory)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(
                "Package path must be a regular file or directory: "
                f"{path.relative_to(directory)}"
            )
        rel = path.relative_to(directory).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def load_package_manifest(package_path: Path) -> PackageManifest:
    """Load a manifest from a package directory or archive."""
    with package_directory(package_path) as package_dir:
        return PackageManifest.from_file(package_dir / "manifest.json")


def catalog_entry_from_package(
    package_path: Path,
    *,
    require_signature: bool = True,
    signing_key: str | None = None,
    signing_key_file: Path | None = None,
    device_profiles: list[str] | None = None,
    strict_metadata: bool = False,
    validate: bool = True,
) -> dict[str, Any]:
    """Build a Hub Lite catalog entry from a TEMMS package artifact."""
    if not package_path.exists():
        raise FileNotFoundError(f"Package not found: {package_path}")

    source_sha256 = package_source_sha256(package_path)
    source_type = "archive" if package_path.is_file() else "directory"
    key = read_signing_key(signing_key, signing_key_file)
    validation = None
    if validate:
        validation = validate_package(
            package_path,
            require_signature=require_signature,
            signing_key=key,
            strict_metadata=strict_metadata,
        )
        if not validation.valid:
            raise ValueError("Package validation failed: " + "; ".join(validation.errors))

    manifest = load_package_manifest(package_path)
    manifest_profiles = manifest.compatibility.get("device_profiles", [])
    raw_profiles = device_profiles if device_profiles is not None else list(manifest_profiles)
    profiles = [
        normalized
        for normalized in (normalize_device_profile(profile) for profile in raw_profiles)
        if normalized
    ]

    metadata: dict[str, Any] = {
        "schema_version": "temms-hub-package/v1",
        "package_schema_version": manifest.schema_version,
        "description": manifest.description,
        "created_at": manifest.created_at,
        "created_by": manifest.created_by,
        "source_registry": manifest.source_registry,
        "mlflow_run_id": manifest.mlflow_run_id,
        "source": {
            "type": source_type,
            "path": str(package_path.resolve()),
            "sha256": source_sha256,
        },
        "provenance": manifest.provenance,
        "compatibility": manifest.compatibility,
        "tags": manifest.tags,
        "models": [
            {
                "id": model.id,
                "name": model.name,
                "version": model.version,
                "format": model.format,
                "sha256": model.sha256,
                "size_bytes": model.size_bytes,
                "runtime_constraints": model.runtime_constraints,
                "runtime_options": model.runtime_options,
                "benchmark": model.benchmark,
                "provenance": model.provenance,
            }
            for model in manifest.models
        ],
        "policies": [policy.model_dump() for policy in manifest.policies],
    }
    if validation is not None:
        metadata["validation"] = {
            "valid": validation.valid,
            "errors": validation.errors,
            "warnings": validation.warnings,
            "strict_metadata": strict_metadata,
            "signature_verified": validation.signature_verified,
            "signature": validation.signature_metadata,
        }

    return {
        "package_id": manifest.package_id,
        "name": manifest.name,
        "version": manifest.version,
        "path": str(package_path.resolve()),
        "sha256": source_sha256,
        "source_sha256": source_sha256,
        "device_profiles": profiles,
        "metadata": metadata,
    }
