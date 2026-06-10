"""
MLflow bridge for TEMMS local development.

Provides integration between TEMMS and MLflow for local dev/testing:
  - Register imported models in MLflow (visible in UI)
  - List models from MLflow registry
  - Pull models from MLflow to create TEMMS packages

This module is entirely optional. The daemon works fine without MLflow.
All MLflow imports are lazy to avoid hard dependency.
"""

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MLflowBridge:
    """Bridge between TEMMS and MLflow for local development."""

    def __init__(self, tracking_uri: str | None = None):
        """
        Initialize MLflow bridge.

        Args:
            tracking_uri: MLflow tracking URI. Defaults to MLFLOW_TRACKING_URI env var.
        """
        self.tracking_uri = tracking_uri or os.environ.get(
            "MLFLOW_TRACKING_URI", "http://localhost:5000"
        )
        self.last_error = ""
        self._available = self._check_mlflow()

    def _check_mlflow(self) -> bool:
        """Check if mlflow package is available."""
        try:
            import mlflow  # noqa: F401
            return True
        except ImportError:
            logger.debug("MLflow not installed, bridge will be inactive")
            return False

    @property
    def available(self) -> bool:
        """Whether MLflow is available and configured."""
        return self._available

    def register_imported_models(self, import_result) -> int:
        """
        Register imported models in MLflow for UI visibility.

        Args:
            import_result: ImportedPackageResult from PackageImporter

        Returns:
            Number of models registered
        """
        if not self._available:
            logger.info("MLflow not available, skipping registration")
            return 0

        import mlflow

        mlflow.set_tracking_uri(self.tracking_uri)

        registered_count = 0
        experiment_name = "temms-edge-models"

        try:
            mlflow.set_experiment(experiment_name)
        except Exception as e:
            logger.warning(f"Could not set MLflow experiment: {e}")
            return 0

        for model in import_result.models:
            try:
                run_name = f"{model.name}-v{model.version}"

                with mlflow.start_run(run_name=run_name):
                    # Log model metadata as parameters
                    mlflow.log_param("model_name", model.name)
                    mlflow.log_param("model_version", model.version)
                    mlflow.log_param("model_format", model.format.value)
                    mlflow.log_param("model_id", model.id)
                    mlflow.log_param("package_id", getattr(model, "package_id", "unknown"))

                    # Log metrics
                    mlflow.log_metric("size_bytes", model.size_bytes)

                    # Log metadata as JSON artifact
                    if model.metadata:
                        metadata_str = json.dumps(model.metadata, indent=2)
                        mlflow.log_text(metadata_str, "metadata.json")

                    # Log the model file as artifact
                    model_path = Path(str(model.path))
                    if model_path.exists():
                        if model_path.is_dir():
                            mlflow.log_artifacts(str(model_path), "model")
                        else:
                            mlflow.log_artifact(str(model_path))

                    # Register as MLflow model version
                    try:
                        run_id = mlflow.active_run().info.run_id
                        mlflow.register_model(
                            f"runs:/{run_id}/model",
                            model.name,
                        )
                    except Exception as reg_err:
                        # Registration may fail if model artifacts aren't in expected format
                        logger.debug(f"Could not register model version: {reg_err}")

                    registered_count += 1
                    logger.info(f"Registered {model.name} v{model.version} in MLflow")

            except Exception as e:
                logger.warning(f"Failed to register {model.name} in MLflow: {e}")

        logger.info(f"Registered {registered_count}/{len(import_result.models)} models in MLflow")
        return registered_count

    def list_models(self) -> list[dict[str, Any]]:
        """
        List models registered in MLflow.

        Returns:
            List of model info dicts
        """
        if not self._available:
            return []

        import mlflow

        self.last_error = ""
        mlflow.set_tracking_uri(self.tracking_uri)

        try:
            client = mlflow.tracking.MlflowClient()
            registered_models = client.search_registered_models()

            models = []
            for rm in registered_models:
                versions = self._list_model_versions(client, rm.name)
                model_info = {
                    "name": rm.name,
                    "versions": versions,
                    # Keep the old key for CLI/backward compatibility.
                    "latest_versions": versions,
                    "description": rm.description or "",
                    "tags": dict(rm.tags) if rm.tags else {},
                }

                models.append(model_info)

            return models

        except Exception as e:
            self.last_error = str(e)
            logger.warning(f"Failed to list MLflow models: {e}")
            return []

    def _list_model_versions(self, client, model_name: str) -> list[dict[str, Any]]:
        """List all versions for a registered model with useful Hub metadata."""
        try:
            model_versions = client.search_model_versions(f"name='{model_name}'")
        except Exception:
            model_versions = client.get_latest_versions(model_name)

        versions = []
        for version in model_versions or []:
            params: dict[str, str] = {}
            metrics: dict[str, float] = {}
            tags: dict[str, str] = {}
            experiment_id = ""
            if getattr(version, "run_id", None):
                try:
                    run = client.get_run(version.run_id)
                    params = dict(run.data.params or {})
                    metrics = dict(run.data.metrics or {})
                    tags = dict(run.data.tags or {})
                    experiment_id = str(getattr(run.info, "experiment_id", "") or "")
                except Exception as e:
                    logger.debug(
                        "Could not load MLflow run metadata for %s v%s: %s",
                        model_name,
                        getattr(version, "version", "-"),
                        e,
                    )

            versions.append({
                "version": str(version.version),
                "status": getattr(version, "status", "") or "",
                "stage": getattr(version, "current_stage", "") or "",
                "run_id": getattr(version, "run_id", "") or "",
                "experiment_id": experiment_id,
                "source": getattr(version, "source", "") or "",
                "aliases": list(getattr(version, "aliases", []) or []),
                "tags": {
                    **tags,
                    **dict(getattr(version, "tags", {}) or {}),
                },
                "format": params.get("model_format") or params.get("format") or "",
                "runtime_constraints": params.get("runtime_constraints", ""),
                "metrics": metrics,
                "validation": self._readiness_from_run(params, metrics, tags),
                "created_at": self._format_mlflow_time(
                    getattr(version, "creation_timestamp", None)
                ),
                "updated_at": self._format_mlflow_time(
                    getattr(version, "last_updated_timestamp", None)
                ),
            })

        return sorted(
            versions,
            key=lambda item: int(item["version"]) if item["version"].isdigit() else 0,
            reverse=True,
        )

    def pull_model(
        self,
        model_name: str,
        version: str | None = None,
        dest_dir: Path | None = None,
    ) -> Path | None:
        """
        Download a model from MLflow and create a TEMMS package.

        Args:
            model_name: Registered model name
            version: Specific version (default: latest)
            dest_dir: Where to create the package (default: /tmp/temms-pull-{name})

        Returns:
            Path to the created package directory, or None on failure
        """
        if not self._available:
            logger.error("MLflow not available")
            return None

        import mlflow

        mlflow.set_tracking_uri(self.tracking_uri)

        try:
            client = mlflow.tracking.MlflowClient()

            # Get model version
            if version:
                model_version = client.get_model_version(model_name, version)
            else:
                # Get latest version
                versions = client.get_latest_versions(model_name)
                if not versions:
                    logger.error(f"No versions found for model: {model_name}")
                    return None
                model_version = versions[0]

            # Download artifacts
            run_id = model_version.run_id
            dest = dest_dir or Path(f"/tmp/temms-pull-{model_name}")
            dest.mkdir(parents=True, exist_ok=True)

            # Download model artifacts into a staging directory, then flatten the
            # selected runtime artifact into TEMMS' package models/ directory.
            download_dir = dest / "_mlflow_download"
            models_dir = dest / "models"
            download_dir.mkdir(parents=True, exist_ok=True)
            models_dir.mkdir(parents=True, exist_ok=True)

            artifact_path = self._artifact_path_from_source(
                getattr(model_version, "source", None),
                run_id,
            )
            local_path = client.download_artifacts(
                run_id,
                artifact_path or "model",
                str(download_dir),
            )

            # Get run info for metadata
            run = client.get_run(run_id)
            experiment_id = str(getattr(run.info, "experiment_id", "") or "")
            params = dict(run.data.params or {})
            metrics = dict(run.data.metrics or {})
            tags = dict(run.data.tags or {})
            validation = self._readiness_from_run(params, metrics, tags)
            signature = self._json_param(params.get("signature"))

            # Create manifest
            local_artifact_path = Path(local_path)
            if local_artifact_path.is_file():
                model_files = [local_artifact_path]
            else:
                model_files = list(local_artifact_path.rglob("*"))
            supported_suffixes = {".onnx", ".tflite", ".pt"}
            model_file = next(
                (f for f in model_files if f.suffix in supported_suffixes),
                None,
            )

            if model_file is None:
                logger.error(f"No model file found in artifacts for {model_name}")
                return None

            package_model_file = models_dir / model_file.name
            if model_file.resolve() != package_model_file.resolve():
                shutil.copy2(model_file, package_model_file)

            # Compute hash
            sha256 = hashlib.sha256(package_model_file.read_bytes()).hexdigest()
            model_format = (
                params.get("model_format")
                or params.get("format")
                or self._infer_format(package_model_file)
            )

            manifest = {
                "schema_version": "v1",
                "package_id": f"mlflow-pull-{model_name}-{model_version.version}",
                "name": model_name,
                "version": model_version.version,
                "description": f"Pulled from MLflow: {model_name} v{model_version.version}",
                "created_at": self._format_mlflow_time(run.info.start_time),
                "created_by": "mlflow-bridge",
                "models": [{
                    "id": f"{model_name}-{model_version.version}",
                    "name": model_name,
                    "version": model_version.version,
                    "format": model_format,
                    "filename": package_model_file.name,
                    "sha256": sha256,
                    "size_bytes": package_model_file.stat().st_size,
                    "metadata": {
                        **self._json_param(params.get("metadata")),
                        "mlflow": {
                            "tracking_uri": self.tracking_uri,
                            "model_name": model_name,
                            "model_version": str(model_version.version),
                            "run_id": run_id,
                            "experiment_id": experiment_id,
                            "source": getattr(model_version, "source", "") or "",
                            "artifact_path": artifact_path or "model",
                        },
                        "validation": validation,
                    },
                }],
                "policies": [],
                "source_registry": self.tracking_uri,
                "mlflow_run_id": run_id,
                "mlflow_experiment_id": experiment_id,
                "tags": tags,
                "signature": signature or None,
                "attestations": {
                    "mlflow": {
                        "run_id": run_id,
                        "metrics": metrics,
                        "tags": tags,
                        "validation_params": self._selected_params(params, {
                            "signature_present",
                            "signature_verified",
                            "sim_passed",
                            "simulation_passed",
                            "tests_passed",
                            "test_passed",
                            "test_status",
                        }),
                    },
                },
                "validation": validation,
            }

            with open(dest / "manifest.json", "w") as f:
                json.dump(manifest, f, indent=2)

            logger.info(f"Pulled {model_name} v{model_version.version} to {dest}")
            return dest

        except Exception as e:
            logger.error(f"Failed to pull model from MLflow: {e}")
            return None

    @staticmethod
    def _artifact_path_from_source(source: str | None, run_id: str) -> str | None:
        """Extract the run-relative artifact path from an MLflow model source URI."""
        if not source:
            return None

        prefix = f"runs:/{run_id}/"
        if source.startswith(prefix):
            return source[len(prefix):] or None
        if source.startswith("runs:/"):
            parts = source.split("/", 2)
            if len(parts) == 3:
                return parts[2] or None
        return None

    @staticmethod
    def _format_mlflow_time(value: Any) -> str:
        """Normalize MLflow millisecond timestamps to ISO 8601 strings."""
        if value in (None, ""):
            return datetime.now(timezone.utc).isoformat()
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        return str(value)

    @staticmethod
    def _infer_format(model_file: Path) -> str:
        """Infer TEMMS model format from a runtime artifact filename."""
        suffix = model_file.suffix.lower()
        if suffix == ".tflite":
            return "tflite"
        if suffix in {".pt", ".pth"}:
            return "torchscript"
        if suffix in {".engine", ".plan"}:
            return "tensorrt"
        return "onnx"

    @classmethod
    def _readiness_from_run(
        cls,
        params: dict[str, Any],
        metrics: dict[str, Any],
        tags: dict[str, Any],
    ) -> dict[str, Any]:
        """Map MLflow run metadata into TEMMS operator-readiness evidence."""
        signature_blob = params.get("signature") or tags.get("signature")
        signed = cls._source_truthy(
            (params, tags),
            {"signed", "signature_present", "signature"},
        )
        signature_verified = cls._source_truthy(
            (params, tags),
            {"signature_verified", "signature_verification_passed"},
        )
        sim_passed = cls._source_truthy(
            (params, tags, metrics),
            {"sim_passed", "simulation_passed", "sim_verified"},
        )
        tests_passed = cls._source_truthy(
            (params, tags, metrics),
            {"tests_passed", "test_passed", "test_status"},
        )

        return {
            "hash_verified": False,
            "signature_present": bool(signature_blob or signed or signature_verified),
            "signature_verified": signature_verified,
            "sim_passed": sim_passed,
            "sim_evidence": cls._readiness_evidence_from_run(
                params,
                metrics,
                tags,
                category="sim",
                aliases={"simulation"},
                passed=sim_passed,
            ),
            "tests_passed": tests_passed,
            "test_evidence": cls._readiness_evidence_from_run(
                params,
                metrics,
                tags,
                category="test",
                aliases={"tests"},
                passed=tests_passed,
            ),
        }

    @classmethod
    def _readiness_evidence_from_run(
        cls,
        params: dict[str, Any],
        metrics: dict[str, Any],
        tags: dict[str, Any],
        *,
        category: str,
        aliases: set[str],
        passed: bool,
    ) -> dict[str, Any]:
        """Build operator provenance for MLflow readiness metadata."""
        names = {category, *aliases}
        source = cls._source_value(
            (params, tags),
            {*(f"{name}_source" for name in names), "source"},
        )
        detail = cls._source_value(
            (params, tags),
            {
                *(f"{name}_detail" for name in names),
                *(f"{name}_scenario" for name in names),
                *(f"{name}_suite" for name in names),
                "detail",
            },
        )
        run_id = cls._source_value(
            (params, tags),
            {*(f"{name}_run_id" for name in names), "run_id"},
        )
        recorded_at = cls._source_value(
            (params, tags),
            {*(f"{name}_at" for name in names), *(f"{name}_completed_at" for name in names)},
        )

        if passed and not source:
            source = "MLflow metadata"
        if passed and not detail:
            detail = "MLflow readiness claim"

        return {
            "passed": passed,
            "source": source or None,
            "detail": detail or None,
            "run_id": run_id or None,
            "recorded_at": recorded_at or None,
            "protected_by_signature": False,
        }

    @classmethod
    def _source_truthy(
        cls,
        sources: tuple[dict[str, Any], ...],
        keys: set[str],
    ) -> bool:
        """Look for explicit truthy evidence in MLflow params, tags, or metrics."""
        normalized_keys = {cls._normalize_key(key) for key in keys}
        for source in sources:
            for key, value in source.items():
                if cls._normalize_key(str(key)) in normalized_keys and cls._truthy(value):
                    return True
        return False

    @classmethod
    def _source_value(
        cls,
        sources: tuple[dict[str, Any], ...],
        keys: set[str],
    ) -> str | None:
        """Return the first non-empty value from MLflow params/tags."""
        normalized_keys = {cls._normalize_key(key) for key in keys}
        for source in sources:
            for key, value in source.items():
                if cls._normalize_key(str(key)) not in normalized_keys:
                    continue
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    return text
        return None

    @staticmethod
    def _selected_params(params: dict[str, Any], keys: set[str]) -> dict[str, Any]:
        normalized_keys = {
            key.strip().lower().replace("-", "_").replace(".", "_")
            for key in keys
        }
        return {
            key: value
            for key, value in params.items()
            if key.strip().lower().replace("-", "_").replace(".", "_")
            in normalized_keys
        }

    @staticmethod
    def _normalize_key(key: str) -> str:
        return key.strip().lower().replace("-", "_").replace(".", "_")

    @staticmethod
    def _truthy(value: Any) -> bool:
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

    @staticmethod
    def _json_param(value: str | None) -> dict[str, Any]:
        """Parse optional JSON metadata params from MLflow runs."""
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
