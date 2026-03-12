"""
MLflow bridge for TEMMS local development.

Provides integration between TEMMS and MLflow for local dev/testing:
  - Register imported models in MLflow (visible in UI)
  - List models from MLflow registry
  - Pull models from MLflow to create TEMMS packages

This module is entirely optional. The daemon works fine without MLflow.
All MLflow imports are lazy to avoid hard dependency.
"""

import json
import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MLflowBridge:
    """Bridge between TEMMS and MLflow for local development."""

    def __init__(self, tracking_uri: Optional[str] = None):
        """
        Initialize MLflow bridge.

        Args:
            tracking_uri: MLflow tracking URI. Defaults to MLFLOW_TRACKING_URI env var.
        """
        self.tracking_uri = tracking_uri or os.environ.get(
            "MLFLOW_TRACKING_URI", "http://localhost:5000"
        )
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

    def list_models(self) -> List[Dict[str, Any]]:
        """
        List models registered in MLflow.

        Returns:
            List of model info dicts
        """
        if not self._available:
            return []

        import mlflow

        mlflow.set_tracking_uri(self.tracking_uri)

        try:
            client = mlflow.tracking.MlflowClient()
            registered_models = client.search_registered_models()

            models = []
            for rm in registered_models:
                model_info = {
                    "name": rm.name,
                    "latest_versions": [],
                    "description": rm.description or "",
                    "tags": dict(rm.tags) if rm.tags else {},
                }

                for version in (rm.latest_versions or []):
                    model_info["latest_versions"].append({
                        "version": version.version,
                        "status": version.status,
                        "run_id": version.run_id,
                    })

                models.append(model_info)

            return models

        except Exception as e:
            logger.warning(f"Failed to list MLflow models: {e}")
            return []

    def pull_model(
        self,
        model_name: str,
        version: Optional[str] = None,
        dest_dir: Optional[Path] = None,
    ) -> Optional[Path]:
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

            # Download model artifacts
            artifacts_dir = dest / "models" / model_name
            artifacts_dir.mkdir(parents=True, exist_ok=True)

            local_path = client.download_artifacts(run_id, "model", str(artifacts_dir))

            # Get run info for metadata
            run = client.get_run(run_id)
            params = run.data.params

            # Create manifest
            model_files = list(Path(local_path).rglob("*"))
            model_file = next((f for f in model_files if f.suffix in [".onnx", ".tflite", ".pt"]), None)

            if model_file is None:
                logger.error(f"No model file found in artifacts for {model_name}")
                return None

            # Compute hash
            sha256 = hashlib.sha256(model_file.read_bytes()).hexdigest()

            manifest = {
                "schema_version": "v1",
                "package_id": f"mlflow-pull-{model_name}-{model_version.version}",
                "name": model_name,
                "version": model_version.version,
                "description": f"Pulled from MLflow: {model_name} v{model_version.version}",
                "created_at": run.info.start_time,
                "created_by": "mlflow-bridge",
                "models": [{
                    "id": f"{model_name}-{model_version.version}",
                    "name": model_name,
                    "version": model_version.version,
                    "format": params.get("model_format", "onnx"),
                    "filename": model_file.name,
                    "sha256": sha256,
                    "size_bytes": model_file.stat().st_size,
                    "metadata": json.loads(params.get("metadata", "{}")),
                }],
                "policies": [],
                "source_registry": self.tracking_uri,
                "mlflow_run_id": run_id,
            }

            with open(dest / "manifest.json", "w") as f:
                json.dump(manifest, f, indent=2)

            logger.info(f"Pulled {model_name} v{model_version.version} to {dest}")
            return dest

        except Exception as e:
            logger.error(f"Failed to pull model from MLflow: {e}")
            return None
