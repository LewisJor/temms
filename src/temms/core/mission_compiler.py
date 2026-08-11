"""Compile a ``mission.yaml`` into a signed TEMMS package (issue #43, slice 1).

``mission.yaml`` is the authored source of truth; ``manifest.json`` is a compiled
artifact. The compiler resolves every model source to concrete bytes, lays out a
package directory (artifacts + policy + manifest), and — when given a signing key
— signs the whole tree. The registry is touched only here, at build time; the
resulting package is self-contained and offline-verifiable.

The manifest keeps the established shape (existing catalog/hub readers keep
working) and *adds* the operating-envelope fields (``provides`` / ``optimal_when``
/ ``requires``) and a per-model ``provenance`` block for later slices.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temms.core.identifiers import slugify
from temms.core.mission_spec import (
    MISSION_PACKAGE_SPEC_SCHEMA_VERSION,
    MissionPackageSpec,
    load_mission_spec,
)
from temms.core.model_resolver import ResolvedModel, resolve_model_source
from temms.core.signing import sign_package

PACKAGE_SCHEMA_VERSION = "temms-package/v1"

_FORMAT_EXTENSIONS = {
    "onnx": ".onnx",
    "tflite": ".tflite",
    "torchscript": ".pt",
    "tensorrt": ".engine",
}


def _safe_id(value: str) -> str:
    # Lowercased, '-'-stripped slug for on-disk package/model directory names.
    return slugify(value, extra_allowed="-_", strip="-", lowercase=True)


def _artifact_filename(model_id: str, fmt: str, resolved: ResolvedModel) -> str:
    """Stable on-disk filename for a resolved artifact."""
    suffix = resolved.artifact_path.suffix or _FORMAT_EXTENSIONS.get(fmt, ".bin")
    return f"{_safe_id(model_id)}{suffix}"


def compile_mission_package(
    mission_path: Path,
    output_dir: Path,
    *,
    signing_key: str | None = None,
    signer: str = "temms",
    tracking_uri: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Compile mission.yaml at ``mission_path`` into a package under ``output_dir``.

    Returns the package directory. Validation (including the policy
    reference-integrity check) runs before anything is written, so a bad spec
    fails without leaving a partial package behind.
    """
    mission_path = Path(mission_path)
    spec = load_mission_spec(mission_path)  # raises MissionSpecError on any problem

    package_id = _safe_id(f"{spec.metadata.name}-{spec.metadata.version}")
    package_dir = Path(output_dir) / package_id
    if package_dir.exists():
        if not overwrite:
            raise FileExistsError(
                f"{package_dir} already exists (pass overwrite=True to replace)"
            )
        shutil.rmtree(package_dir)
    (package_dir / "models").mkdir(parents=True)

    models_manifest: list[dict[str, Any]] = []
    for model in spec.models:
        resolved = resolve_model_source(
            model.source,
            base_dir=mission_path.parent,
            tracking_uri=tracking_uri,
        )
        filename = _artifact_filename(model.id, model.format, resolved)
        dest = package_dir / "models" / _safe_id(model.id) / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved.artifact_path, dest)

        models_manifest.append(
            {
                "id": model.id,
                "name": model.id,
                "format": model.format,
                "filename": f"models/{_safe_id(model.id)}/{filename}",
                "sha256": resolved.sha256,
                "size_bytes": dest.stat().st_size,
                "provides": model.provides,
                "optimal_when": model.optimal_when,
                "requires": model.requires,
                "provenance": resolved.provenance,
            }
        )

    policies_manifest = _copy_policy(spec, mission_path.parent, package_dir)

    manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "package_id": package_id,
        "name": spec.metadata.name,
        "version": spec.metadata.version,
        "description": spec.metadata.description,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "created_by": f"temms-mission-compiler:{signer}",
        "models": models_manifest,
        "policies": policies_manifest,
        "slot": {"name": spec.slot.name},
        "target": {
            "device_profiles": spec.target.device_profiles,
            "runtime": spec.target.runtime,
            "requires": spec.target.requires,
        },
        "mission_spec": {
            "schema_version": MISSION_PACKAGE_SPEC_SCHEMA_VERSION,
            "source_filename": mission_path.name,
        },
    }
    _write_manifest(package_dir, manifest)

    if signing_key:
        sign_package(package_dir, signing_key, signer=signer)

    return package_dir


def _copy_policy(
    spec: MissionPackageSpec, mission_dir: Path, package_dir: Path
) -> list[dict[str, Any]]:
    """Copy the slot policy into the package and return its manifest entry."""
    if not spec.slot.policy:
        return []
    policy_src = Path(spec.slot.policy)
    if not policy_src.is_absolute():
        policy_src = mission_dir / policy_src
    policies_dir = package_dir / "policies"
    policies_dir.mkdir(exist_ok=True)
    shutil.copyfile(policy_src, policies_dir / policy_src.name)
    return [
        {
            "name": policy_src.stem,
            "filename": f"policies/{policy_src.name}",
            "slot": spec.slot.name,
        }
    ]


def _write_manifest(package_dir: Path, manifest: dict[str, Any]) -> None:
    import json

    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
