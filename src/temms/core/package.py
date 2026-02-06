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
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
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
    metadata: Dict[str, Any] = Field(default_factory=dict)


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

        manifest = PackageManifest.from_file(manifest_path)

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

            # Store model
            dest_path, _, size_bytes = self.storage.store_model(
                model_file, model_artifact.id, verify=False  # Already verified above
            )

            # Add to cache
            from temms.core.cache import ModelFormat
            cached_model = self.model_cache.add_cached_model(
                model_id=model_artifact.id,
                name=model_artifact.name,
                version=model_artifact.version,
                format=ModelFormat(model_artifact.format),
                path=dest_path,
                sha256=model_artifact.sha256,
                size_bytes=size_bytes,
                package_id=manifest.package_id,
                metadata=model_artifact.metadata,
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

        # Record package
        package = self.model_cache.add_package(
            package_id=manifest.package_id,
            name=manifest.name,
            version=manifest.version,
            source=str(package_path),
            manifest=manifest.model_dump(),
        )

        return ImportedPackageResult(
            package=package,
            models=imported_models,
            policies=imported_policies,
            manifest=manifest,
        )

    def _compute_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of file."""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()
