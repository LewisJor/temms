"""
Build TEMMS edge packages from upstream model registries.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from temms.core.package_archive import create_package_archive, default_archive_path
from temms.core.runtime_profiles import normalize_device_profile
from temms.core.signing import sha256_file, sign_package, validate_package


@dataclass
class MLflowModelRef:
    """Parsed MLflow model URI."""

    name: str
    version: str | None = None
    alias: str | None = None


def parse_mlflow_model_uri(uri: str) -> MLflowModelRef:
    """Parse models:/name/version or models:/name@alias."""
    if not uri.startswith("models:/"):
        raise ValueError("MLflow model URI must start with models:/")

    value = uri.removeprefix("models:/").strip("/")
    if not value:
        raise ValueError("MLflow model URI is missing a model name")

    if "@" in value:
        name, alias = value.split("@", 1)
        return MLflowModelRef(name=name.strip("/"), alias=alias)

    parts = value.split("/")
    if len(parts) == 1:
        return MLflowModelRef(name=parts[0])
    if len(parts) == 2:
        return MLflowModelRef(name=parts[0], version=parts[1])

    raise ValueError("Expected models:/name/version or models:/name@alias")


def build_package_from_mlflow(
    model_uri: str,
    slot: str,
    policy_path: Path | None,
    output_dir: Path,
    tracking_uri: str | None = None,
    model_format: str | None = None,
    device_profile: str | None = None,
    runtime_constraints_override: dict[str, Any] | None = None,
    runtime_options_override: dict[str, Any] | None = None,
    model_artifact_path: str | None = None,
    require_schema: bool = True,
    require_runtime_constraints: bool = True,
    signing_key: str | None = None,
    signer: str = "temms-hub-lite",
    strict_metadata: bool = False,
    archive: bool = False,
    overwrite: bool = False,
) -> Path:
    """Build a TEMMS package directory or archive from an MLflow registered model."""
    try:
        import mlflow
    except ImportError as exc:
        raise RuntimeError("MLflow is not installed. Install temms[mlflow].") from exc

    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)
    tracking_uri = tracking_uri or os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")

    ref = parse_mlflow_model_uri(model_uri)
    device_profile = normalize_device_profile(device_profile)
    client = mlflow.tracking.MlflowClient()

    if ref.version:
        model_version = client.get_model_version(ref.name, ref.version)
    elif ref.alias:
        model_version = client.get_model_version_by_alias(ref.name, ref.alias)
    else:
        versions = client.get_latest_versions(ref.name)
        if not versions:
            raise ValueError(f"No versions found for MLflow model: {ref.name}")
        model_version = versions[0]

    run_id = model_version.run_id
    run = client.get_run(run_id)

    package_version = str(model_version.version)
    package_id = _safe_id(f"mlflow-{ref.name}-{package_version}")
    output_dir.mkdir(parents=True, exist_ok=True)
    final_package_dir = output_dir / f"{package_id}.temms"
    final_archive_path = default_archive_path(final_package_dir) if archive else None
    _ensure_package_outputs_available(
        [path for path in (final_package_dir, final_archive_path) if path is not None],
        overwrite=overwrite,
    )

    with tempfile.TemporaryDirectory(prefix=f".{package_id}.build-", dir=output_dir) as tmp:
        package_dir = Path(tmp) / f"{package_id}.temms"
        models_dir = package_dir / "models"
        policies_dir = package_dir / "policies"
        models_dir.mkdir(parents=True)
        policies_dir.mkdir(parents=True)

        artifact_root = Path(
            client.download_artifacts(run_id, "", str(package_dir / "_mlflow"))
        ).resolve()
        model_file = _find_model_file(artifact_root, requested_path=model_artifact_path)
        if model_file is None:
            raise ValueError(f"No ONNX/TFLite/Torch/TensorRT model artifact found for {model_uri}")

        package_model_file = models_dir / model_file.name
        shutil.copy(model_file, package_model_file)

        policies = []
        if policy_path is not None:
            policy_dest = policies_dir / policy_path.name
            shutil.copy(policy_path, policy_dest)
            policies.append({"name": policy_path.stem, "filename": policy_path.name, "slot": slot})

        inferred_format = model_format or _infer_model_format(model_file)
        model_id = _safe_id(f"{ref.name}-{package_version}")
        params = dict(run.data.params)
        metrics = dict(run.data.metrics)
        mlmodel_signature = _mlmodel_signature(artifact_root, model_file)
        input_schema = _json_param(params, "input_schema") or mlmodel_signature.get(
            "input_schema",
            {},
        )
        output_schema = _json_param(params, "output_schema") or mlmodel_signature.get(
            "output_schema",
            {},
        )
        if require_schema and (not input_schema or not output_schema):
            missing = []
            if not input_schema:
                missing.append("input_schema")
            if not output_schema:
                missing.append("output_schema")
            raise ValueError(
                "MLflow package requires "
                + " and ".join(missing)
                + "; set MLflow run params, log an MLmodel signature, or use "
                "--allow-missing-schema for lab packages"
            )
        runtime_constraints = _json_param(params, "runtime_constraints")
        runtime_constraints.update(runtime_constraints_override or {})
        runtime_options = _json_param(params, "runtime_options")
        runtime_options.update(runtime_options_override or {})
        if device_profile:
            runtime_constraints.setdefault("device_profiles", [device_profile])
        if require_runtime_constraints and not runtime_constraints:
            raise ValueError(
                "MLflow package requires runtime_constraints; set MLflow run params, "
                "pass --runtime-constraint or --device-profile, or use "
                "--allow-missing-runtime-constraints for lab packages"
            )

        model_sha256 = sha256_file(package_model_file)
        artifact_path = str(model_file.relative_to(artifact_root))
        artifact_metadata = {
            "path": artifact_path,
            "format": inferred_format,
            "size_bytes": package_model_file.stat().st_size,
            "sha256": model_sha256,
        }
        artifact_metadata_sha256 = _stable_json_sha256(artifact_metadata)
        resolved_model_uri = f"models:/{ref.name}/{package_version}"
        model_version_metadata = _model_version_metadata(model_version)
        run_metadata = _run_metadata(run, params)
        model_provenance = {
            "source": "mlflow",
            "model_uri": model_uri,
            "resolved_model_uri": resolved_model_uri,
            "registered_model_name": ref.name,
            "model_version": package_version,
            "model_alias": ref.alias,
            "run_id": run_id,
            "artifact_path": artifact_path,
            "artifact_size_bytes": artifact_metadata["size_bytes"],
            "artifact_sha256": model_sha256,
            "artifact_metadata_sha256": artifact_metadata_sha256,
            "signature_path": mlmodel_signature.get("signature_path"),
            **model_version_metadata,
        }

        manifest = {
            "schema_version": "v1",
            "package_id": package_id,
            "name": ref.name,
            "version": package_version,
            "description": f"TEMMS edge package for {model_uri}",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "created_by": "temms-package-builder",
            "models": [
                {
                    "id": model_id,
                    "name": ref.name,
                    "version": package_version,
                    "format": inferred_format,
                    "filename": package_model_file.name,
                    "sha256": model_sha256,
                    "size_bytes": package_model_file.stat().st_size,
                    "metadata": _json_param(params, "metadata"),
                    "input_schema": input_schema,
                    "output_schema": output_schema,
                    "runtime_constraints": runtime_constraints,
                    "runtime_options": runtime_options,
                    "benchmark": _benchmark(metrics),
                    "provenance": model_provenance,
                }
            ],
            "policies": policies,
            "source_registry": tracking_uri,
            "mlflow_run_id": run_id,
            "provenance": {
                "source": "mlflow",
                "model_uri": model_uri,
                "resolved_model_uri": resolved_model_uri,
                "registered_model_name": ref.name,
                "model_version": package_version,
                "model_alias": ref.alias,
                "run_id": run_id,
                "artifact_metadata": artifact_metadata,
                "artifact_metadata_sha256": artifact_metadata_sha256,
                **model_version_metadata,
                **run_metadata,
            },
            "compatibility": {
                "slot": slot,
                "device_profiles": [device_profile] if device_profile else [],
                "runtime_constraints": runtime_constraints,
            },
            "tags": {
                "source": "mlflow",
                "slot": slot,
            },
            "metadata": {
                "build": {
                    "schema_version": "temms-package-build/v1",
                    "workflow": "temms package from-mlflow",
                    "builder": "temms-package-builder",
                    "tracking_uri": tracking_uri,
                    "requested_model_uri": model_uri,
                    "resolved_model_uri": resolved_model_uri,
                    "artifact_metadata_sha256": artifact_metadata_sha256,
                    "archive_requested": archive,
                    "signed": bool(signing_key),
                }
            },
        }

        (package_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        shutil.rmtree(package_dir / "_mlflow", ignore_errors=True)

        _raise_if_invalid_package(
            package_dir,
            strict_metadata=strict_metadata,
            label="Generated TEMMS package",
        )

        if signing_key:
            sign_package(package_dir, signing_key, signer=signer)
            _raise_if_invalid_package(
                package_dir,
                require_signature=True,
                signing_key=signing_key,
                strict_metadata=strict_metadata,
                label="Generated signed TEMMS package",
            )

        if archive:
            _remove_package_outputs(
                [path for path in (final_package_dir, final_archive_path) if path is not None],
                overwrite=overwrite,
            )
            archive_path = create_package_archive(package_dir, final_archive_path)
            _raise_if_invalid_package(
                archive_path,
                require_signature=bool(signing_key),
                signing_key=signing_key,
                strict_metadata=strict_metadata,
                label="Generated TEMMS archive",
            )
            return archive_path

        _remove_package_outputs([final_package_dir], overwrite=overwrite)
        shutil.move(str(package_dir), str(final_package_dir))
        return final_package_dir


def _find_model_file(root: Path, requested_path: str | None = None) -> Path | None:
    extensions = {".onnx", ".tflite", ".pt", ".pth", ".engine", ".plan"}
    if requested_path:
        requested = Path(requested_path)
        if requested.is_absolute() or any(part in ("", ".", "..") for part in requested.parts):
            raise ValueError(f"Unsafe MLflow model artifact path: {requested_path}")
        candidate = (root / requested).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            raise ValueError(f"MLflow model artifact path escapes download root: {requested_path}")
        if not candidate.is_file():
            raise ValueError(f"MLflow model artifact not found: {requested_path}")
        if candidate.suffix not in extensions:
            raise ValueError(
                f"Unsupported MLflow model artifact extension for {requested_path}: "
                f"{candidate.suffix}"
            )
        return candidate

    files = [path for path in root.rglob("*") if path.is_file() and path.suffix in extensions]
    if not files:
        return None
    if len(files) == 1:
        return files[0]

    mlmodel_file = _model_file_from_mlmodel(root, extensions)
    if mlmodel_file is not None:
        return mlmodel_file

    choices = ", ".join(str(path.relative_to(root)) for path in sorted(files))
    raise ValueError(
        "Multiple model artifacts found in MLflow download; pass --model-artifact with one of: "
        + choices
    )


def _ensure_package_outputs_available(paths: list[Path], overwrite: bool) -> None:
    """Fail fast when immutable package outputs already exist."""
    existing = [path for path in paths if path.exists()]
    if existing and not overwrite:
        outputs = ", ".join(str(path) for path in existing)
        raise FileExistsError(
            f"TEMMS package output already exists: {outputs}; pass --overwrite to replace it"
        )


def _remove_package_outputs(paths: list[Path], overwrite: bool) -> None:
    """Remove existing outputs only when an explicit overwrite was requested."""
    if not overwrite:
        return
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _infer_model_format(path: Path) -> str:
    mapping = {
        ".onnx": "onnx",
        ".tflite": "tflite",
        ".pt": "torchscript",
        ".pth": "torchscript",
        ".engine": "tensorrt",
        ".plan": "tensorrt",
    }
    return mapping.get(path.suffix, "onnx")


def _json_param(params: dict[str, str], key: str) -> dict[str, Any]:
    raw = params.get(key)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except Exception:
        return {"value": raw}


def _mlmodel_signature(artifact_root: Path, model_file: Path) -> dict[str, Any]:
    """Extract input/output schemas from an MLflow MLmodel signature."""
    mlmodel_path = _find_mlmodel_file(artifact_root, model_file)
    if mlmodel_path is None:
        return {}

    try:
        import yaml

        mlmodel = yaml.safe_load(mlmodel_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}

    signature = mlmodel.get("signature")
    if not isinstance(signature, dict):
        return {}

    extracted: dict[str, Any] = {
        "signature_path": str(mlmodel_path.relative_to(artifact_root)),
    }
    if signature.get("inputs"):
        extracted["input_schema"] = _mlflow_schema_value(signature["inputs"])
    if signature.get("outputs"):
        extracted["output_schema"] = _mlflow_schema_value(signature["outputs"])
    return extracted


def _find_mlmodel_file(artifact_root: Path, model_file: Path) -> Path | None:
    """Find the MLmodel file that describes a downloaded MLflow artifact."""
    candidates = [
        model_file.parent / "MLmodel",
        artifact_root / "MLmodel",
    ]
    candidates.extend(sorted(artifact_root.rglob("MLmodel")))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _model_file_from_mlmodel(root: Path, extensions: set[str]) -> Path | None:
    """Return the model data file declared by MLflow flavor metadata when unambiguous."""
    declared: list[Path] = []
    for mlmodel_path in sorted(root.rglob("MLmodel")):
        try:
            import yaml

            mlmodel = yaml.safe_load(mlmodel_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        flavors = mlmodel.get("flavors")
        if not isinstance(flavors, dict):
            continue
        for flavor in flavors.values():
            if not isinstance(flavor, dict):
                continue
            for key in ("data", "model_data", "model_path"):
                value = flavor.get(key)
                if not isinstance(value, str) or not value:
                    continue
                candidate = (mlmodel_path.parent / value).resolve()
                try:
                    candidate.relative_to(root.resolve())
                except ValueError:
                    continue
                if candidate.is_file() and candidate.suffix in extensions:
                    declared.append(candidate)

    unique = sorted(set(declared))
    if len(unique) == 1:
        return unique[0]
    return None


def _mlflow_schema_value(raw: Any) -> dict[str, Any]:
    """Normalize an MLflow signature value into a manifest schema object."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {"source": "MLmodel", "value": raw}
    else:
        parsed = raw

    if isinstance(parsed, dict):
        return {"source": "MLmodel", **parsed}
    return {"source": "MLmodel", "schema": parsed}


def _benchmark(metrics: dict[str, float]) -> dict[str, Any]:
    metric_aliases = {
        "latency_ms": (
            "latency_ms",
            "avg_latency_ms",
            "average_latency_ms",
            "mean_latency_ms",
        ),
        "p50_latency_ms": ("p50_latency_ms", "median_latency_ms"),
        "p90_latency_ms": ("p90_latency_ms",),
        "p95_latency_ms": ("p95_latency_ms",),
        "p99_latency_ms": ("p99_latency_ms",),
        "throughput_fps": ("throughput_fps", "fps", "frames_per_second"),
        "throughput_rps": ("throughput_rps", "requests_per_second", "qps"),
        "memory_mb": ("memory_mb", "peak_memory_mb"),
        "accuracy": ("accuracy",),
        "f1": ("f1", "f1_score"),
        "precision": ("precision",),
        "recall": ("recall",),
    }
    benchmark: dict[str, Any] = {}
    source_metrics: dict[str, str] = {}
    for canonical_key, aliases in metric_aliases.items():
        for metric_key in aliases:
            if metric_key in metrics:
                benchmark[canonical_key] = metrics[metric_key]
                source_metrics[canonical_key] = metric_key
                break
    benchmark["available"] = bool(source_metrics)
    benchmark["_source"] = {
        "type": "mlflow_run_metrics",
        "metric_keys": source_metrics,
        "metrics_sha256": _stable_json_sha256(metrics),
    }
    return benchmark


def _model_version_metadata(model_version: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    fields = {
        "model_source": "source",
        "model_status": "status",
        "model_current_stage": "current_stage",
        "model_version_creation_timestamp": "creation_timestamp",
        "model_version_last_updated_timestamp": "last_updated_timestamp",
    }
    for manifest_key, mlflow_attr in fields.items():
        value = getattr(model_version, mlflow_attr, None)
        if value is not None:
            metadata[manifest_key] = value

    aliases = getattr(model_version, "aliases", None)
    if aliases:
        metadata["model_aliases"] = sorted(str(alias) for alias in aliases)

    return metadata


def _run_metadata(run: Any, params: dict[str, str]) -> dict[str, Any]:
    info = getattr(run, "info", None)
    data = getattr(run, "data", None)
    tags = dict(getattr(data, "tags", {}) or {})
    metadata: dict[str, Any] = {
        "run_name": tags.get("mlflow.runName"),
        "run_params_sha256": _stable_json_sha256(params),
        "run_tags_sha256": _stable_json_sha256(tags),
    }
    artifact_uri = getattr(info, "artifact_uri", None)
    if artifact_uri:
        metadata["run_artifact_uri"] = artifact_uri
    user_id = getattr(info, "user_id", None)
    if user_id:
        metadata["run_user_id"] = user_id
    status = getattr(info, "status", None)
    if status:
        metadata["run_status"] = status
    start_time = getattr(info, "start_time", None)
    if start_time is not None:
        metadata["run_start_time"] = start_time
    end_time = getattr(info, "end_time", None)
    if end_time is not None:
        metadata["run_end_time"] = end_time
    return metadata


def _stable_json_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)


def _raise_if_invalid_package(
    package_path: Path,
    *,
    require_signature: bool = False,
    signing_key: str | None = None,
    strict_metadata: bool = False,
    label: str,
) -> None:
    result = validate_package(
        package_path,
        require_signature=require_signature,
        signing_key=signing_key,
        strict_metadata=strict_metadata,
    )
    if not result.valid:
        detail = "; ".join(result.errors) if result.errors else "unknown validation error"
        raise ValueError(f"{label} is invalid: {detail}")
