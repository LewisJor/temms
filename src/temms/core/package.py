"""
TEMMS package format and importer.

Package structure:
temms-package/
├── manifest.json     # What's in the package
├── models/           # Pre-validated artifacts
└── policies/         # Policy files to load
"""

import json
import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ModelArtifact(BaseModel):
    """Model artifact in package."""

    id: str
    name: str
    version: str
    format: str  # onnx, tflite, torchscript
    filename: str
    sha256: str
    size_bytes: int
    metadata: Dict[str, Any] = Field(default_factory=dict)
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    runtime_constraints: Dict[str, Any] = Field(default_factory=dict)
    runtime_options: Dict[str, Any] = Field(default_factory=dict)
    benchmark: Dict[str, Any] = Field(default_factory=dict)
    provenance: Dict[str, Any] = Field(default_factory=dict)


class PolicyArtifact(BaseModel):
    """Policy artifact in package."""

    name: str
    filename: str
    slot: Optional[str] = None  # Which slot this policy controls


class PackageManifest(BaseModel):
    """TEMMS package manifest."""

    schema_version: str = Field(default="v1")
    package_id: str
    name: str
    version: str
    description: Optional[str] = None
    created_at: str
    created_by: Optional[str] = None  # MLflow user, Hub identifier, etc.

    models: List[ModelArtifact] = Field(default_factory=list)
    policies: List[PolicyArtifact] = Field(default_factory=list)

    # Optional metadata
    source_registry: Optional[str] = None  # MLflow tracking URI
    mlflow_run_id: Optional[str] = None
    provenance: Dict[str, Any] = Field(default_factory=dict)
    compatibility: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tags: Dict[str, str] = Field(default_factory=dict)

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
    models: List[Any]  # List[CachedModel]
    policies: List[PolicyArtifact]
    manifest: PackageManifest


class PackageImporter:
    """Imports TEMMS packages into local cache."""

    def __init__(
        self,
        cache_dir: Path,
        model_cache,
        storage,
        active_policy_dir: Optional[Path] = None,
        require_signature: bool = True,
        signing_key: Optional[str] = None,
        device_profile: Optional[str] = None,
        check_runtime_constraints: bool = True,
        strict_metadata: Optional[bool] = None,
    ):
        """
        Initialize importer.

        Args:
            cache_dir: Where to store imported packages
            model_cache: ModelCache instance
            storage: ModelStorage instance
            active_policy_dir: Optional policy directory watched by the daemon
            require_signature: Require signature.json before import
            signing_key: Key used to verify signature.json
            device_profile: Optional local device profile to validate against
            check_runtime_constraints: Enforce runtime constraints before import
            strict_metadata: Enforce production metadata; defaults to signature policy
        """
        self.cache_dir = cache_dir
        self.model_cache = model_cache
        self.storage = storage
        self.active_policy_dir = active_policy_dir
        self.require_signature = require_signature
        self.signing_key = signing_key
        self.device_profile = device_profile
        self.check_runtime_constraints = check_runtime_constraints
        self.strict_metadata = require_signature if strict_metadata is None else strict_metadata
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

        from temms.core.package_archive import is_package_archive, package_directory
        from temms.core.package_catalog import package_source_sha256
        from temms.core.signing import validate_package

        validation = validate_package(
            package_path,
            require_signature=self.require_signature,
            signing_key=self.signing_key,
            device_profile=self.device_profile,
            check_runtime_constraints=self.check_runtime_constraints,
            strict_metadata=self.strict_metadata,
        )
        if not validation.valid:
            raise ValueError("Package validation failed: " + "; ".join(validation.errors))
        if validation.signature_verified:
            logger.info("Verified package signature for %s", package_path)

        source_type = "archive" if is_package_archive(package_path) else "directory"
        source_sha256 = package_source_sha256(package_path)
        with package_directory(package_path, work_dir=self.cache_dir / "archives") as package_dir:
            return self._import_package_dir(
                package_dir,
                source=str(package_path),
                import_audit={
                    "schema_version": "temms-import-audit/v1",
                    "imported_at": datetime.utcnow().isoformat() + "Z",
                    "source": str(package_path),
                    "source_type": source_type,
                    "source_sha256": source_sha256,
                    "archive_sha256": source_sha256 if source_type == "archive" else None,
                    "directory_sha256": source_sha256 if source_type == "directory" else None,
                    "hashes_verified": verify,
                    "signature_required": self.require_signature,
                    "signature_verified": validation.signature_verified,
                    "signature": validation.signature_metadata,
                    "device_profile": self.device_profile,
                    "validation": {
                        "warnings": validation.warnings,
                        "signature_verified": validation.signature_verified,
                        "signature": validation.signature_metadata,
                        "strict_metadata": self.strict_metadata,
                    },
                    "warnings": validation.warnings,
                },
                verify=verify,
            )

    def _import_package_dir(
        self,
        package_path: Path,
        source: str,
        import_audit: Dict[str, Any],
        verify: bool = True,
    ) -> ImportedPackageResult:
        """Import an already validated directory package."""
        # Load manifest
        manifest_path = package_path / "manifest.json"
        if not manifest_path.exists():
            raise ValueError(f"Missing manifest.json in package: {package_path}")

        manifest = PackageManifest.from_file(manifest_path)
        package_component = _safe_manifest_component(manifest.package_id, "package_id")

        # Verify and import models
        models_dir = package_path / "models"
        imported_models = []

        for model_artifact in manifest.models:
            _safe_manifest_component(model_artifact.id, "model id")
            model_file = _safe_package_file(
                models_dir,
                model_artifact.filename,
                "model",
            )
            if not model_file.exists():
                raise FileNotFoundError(f"Model file not found: {model_file}")
            if not model_file.is_file():
                raise ValueError(f"Model path is not a regular file: {model_file}")

            # Verify hash if requested
            if verify:
                computed_hash = self._compute_hash(model_file)
                if computed_hash != model_artifact.sha256:
                    raise ValueError(
                        f"Hash mismatch for {model_artifact.filename}: "
                        f"expected {model_artifact.sha256}, got {computed_hash}"
                    )

            # Store model
            dest_path, _, size_bytes = self.storage.store_model(
                model_file, model_artifact.id, verify=False  # Already verified above
            )

            # Add to cache
            from temms.core.cache import ModelFormat

            model_metadata = dict(model_artifact.metadata)
            model_metadata.update(
                {
                    "input_schema": model_artifact.input_schema,
                    "output_schema": model_artifact.output_schema,
                    "runtime_constraints": model_artifact.runtime_constraints,
                    "runtime_options": model_artifact.runtime_options,
                    "benchmark": model_artifact.benchmark,
                    "provenance": model_artifact.provenance,
                }
            )
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

        # Copy policies and converge the active policy store to this package version.
        policies_dir = package_path / "policies"
        imported_policies = []
        validated_policy_paths = {
            policy_artifact.filename: _safe_package_file(
                policies_dir,
                policy_artifact.filename,
                "policy",
                basename_only=True,
            )
            for policy_artifact in manifest.policies
        }

        policy_root = self.cache_dir / "policies"
        policy_root.mkdir(parents=True, exist_ok=True)
        policy_dest_dir = policy_root / package_component
        previous_policy_filenames = (
            {path.name for path in policy_dest_dir.iterdir() if path.is_file()}
            if policy_dest_dir.exists()
            else set()
        )
        incoming_policy_filenames = set(validated_policy_paths)

        staged_policy_dir = Path(tempfile.mkdtemp(prefix=f".{package_component}-", dir=policy_root))
        staged_active_paths: Dict[Path, Path] = {}
        try:
            if policies_dir.exists():
                for policy_artifact in manifest.policies:
                    policy_file = validated_policy_paths[policy_artifact.filename]
                    if policy_file.exists():
                        cache_policy_path = staged_policy_dir / policy_artifact.filename
                        shutil.copy(policy_file, cache_policy_path)
                        imported_policies.append(policy_artifact)

            if self.active_policy_dir is not None:
                self.active_policy_dir.mkdir(parents=True, exist_ok=True)
                for policy_artifact in manifest.policies:
                    policy_file = validated_policy_paths[policy_artifact.filename]
                    if policy_file.exists():
                        active_name = f"{package_component}-{policy_artifact.filename}"
                        temp_fd, temp_active_name = tempfile.mkstemp(
                            prefix=f".{active_name}-",
                            dir=self.active_policy_dir,
                        )
                        os.close(temp_fd)
                        temp_active_path = Path(temp_active_name)
                        staged_active_paths[temp_active_path] = self.active_policy_dir / active_name
                        shutil.copy(policy_file, temp_active_path)
        except Exception:
            shutil.rmtree(staged_policy_dir, ignore_errors=True)
            for temp_active_path in staged_active_paths:
                temp_active_path.unlink(missing_ok=True)
            raise

        backup_policy_dir = None
        if policy_dest_dir.exists():
            backup_policy_dir = Path(
                tempfile.mkdtemp(prefix=f".{package_component}-previous-", dir=policy_root)
            )
            backup_policy_dir.rmdir()
            policy_dest_dir.replace(backup_policy_dir)

        try:
            staged_policy_dir.replace(policy_dest_dir)
        except Exception:
            _restore_policy_dir(policy_dest_dir, backup_policy_dir)
            shutil.rmtree(staged_policy_dir, ignore_errors=True)
            for temp_active_path in staged_active_paths:
                temp_active_path.unlink(missing_ok=True)
            raise

        if self.active_policy_dir is not None:
            try:
                for temp_active_path, active_path in staged_active_paths.items():
                    temp_active_path.replace(active_path)
                for filename in previous_policy_filenames - incoming_policy_filenames:
                    active_name = f"{package_component}-{filename}"
                    active_path = self.active_policy_dir / active_name
                    if active_path.exists():
                        active_path.unlink()
            except Exception:
                _restore_policy_dir(policy_dest_dir, backup_policy_dir)
                for temp_active_path in staged_active_paths:
                    temp_active_path.unlink(missing_ok=True)
                raise
        if backup_policy_dir is not None:
            shutil.rmtree(backup_policy_dir, ignore_errors=True)

        # Record package
        manifest_record = manifest.model_dump()
        manifest_record["_temms_import"] = import_audit
        package = self.model_cache.add_package(
            package_id=manifest.package_id,
            name=manifest.name,
            version=manifest.version,
            source=source,
            manifest=manifest_record,
        )

        result = ImportedPackageResult(
            package=package,
            models=imported_models,
            policies=imported_policies,
            manifest=manifest,
        )

        # Registering imported models with MLflow is a local-development hook.
        # Edge rollouts must not block on an unavailable tracking server.
        if _env_bool("TEMMS_MLFLOW_AUTO_REGISTER"):
            try:
                from temms.mlflow_bridge import MLflowBridge

                bridge = MLflowBridge()
                if bridge.available:
                    bridge.register_imported_models(result)
            except Exception as e:
                logger.debug("MLflow registration skipped: %s", e)

        return result

    def _compute_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of file."""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()


def _safe_package_file(
    base_dir: Path,
    filename: str,
    kind: str,
    basename_only: bool = False,
) -> Path:
    """Resolve a manifest filename without allowing package path escapes."""
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError(f"{kind.title()} filename must be a non-empty string")

    relative_path = Path(filename)
    if relative_path.is_absolute():
        raise ValueError(f"Unsafe {kind} filename: {filename} is absolute")
    if any(part in ("", ".", "..") for part in relative_path.parts):
        raise ValueError(f"Unsafe {kind} filename: {filename} contains path traversal")
    if basename_only and len(relative_path.parts) != 1:
        raise ValueError(f"Unsafe {kind} filename: {filename} must be a file name, not a path")

    candidate = base_dir / relative_path
    try:
        candidate.resolve().relative_to(base_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"Unsafe {kind} filename: {filename} escapes {base_dir.name}/") from exc
    return candidate


def _safe_manifest_component(value: str, label: str) -> str:
    """Return a manifest ID only when it is safe as one filesystem component."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    component = Path(value)
    if component.is_absolute() or len(component.parts) != 1 or component.name in {".", ".."}:
        raise ValueError(f"Unsafe {label}: {value}")
    return value


def _restore_policy_dir(policy_dest_dir: Path, backup_policy_dir: Optional[Path]) -> None:
    """Restore the previous cached policy directory after a promotion failure."""
    if policy_dest_dir.exists():
        shutil.rmtree(policy_dest_dir, ignore_errors=True)
    if backup_policy_dir is not None and backup_policy_dir.exists():
        backup_policy_dir.replace(policy_dest_dir)


def _env_bool(name: str) -> bool:
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}
