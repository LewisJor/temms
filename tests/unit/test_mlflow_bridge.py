"""
Tests for the legacy local-development MLflow bridge.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from temms.core.signing import validate_package
from temms.mlflow_bridge import MLflowBridge


def test_mlflow_pull_creates_valid_development_package(temp_dir, monkeypatch):
    artifact_dir = temp_dir / "mlflow-artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "model.onnx").write_bytes(b"dev-onnx")
    (artifact_dir / "notes.txt").write_text("ignored")

    class FakeClient:
        def get_model_version(self, name, version):
            assert name == "detector"
            assert version == "7"
            return SimpleNamespace(version="7", run_id="run-dev")

        def download_artifacts(self, run_id, path, dst_path):
            assert run_id == "run-dev"
            assert path == "model"
            destination = Path(dst_path) / "downloaded"
            destination.mkdir(parents=True)
            for source in artifact_dir.iterdir():
                (destination / source.name).write_bytes(source.read_bytes())
            return str(destination)

        def get_run(self, run_id):
            assert run_id == "run-dev"
            return SimpleNamespace(
                info=SimpleNamespace(start_time=1_710_000_000_000),
                data=SimpleNamespace(
                    params={
                        "model_format": "onnx",
                        "metadata": '{"purpose":"dev"}',
                    },
                ),
            )

    fake_mlflow = SimpleNamespace(
        set_tracking_uri=lambda uri: None,
        tracking=SimpleNamespace(MlflowClient=FakeClient),
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

    package_dir = MLflowBridge("http://mlflow.local").pull_model(
        "detector",
        version="7",
        dest_dir=temp_dir / "pulled-package",
    )

    assert package_dir == temp_dir / "pulled-package"
    assert not (package_dir / "_mlflow").exists()
    assert (package_dir / "models" / "model.onnx").read_bytes() == b"dev-onnx"
    manifest = json.loads((package_dir / "manifest.json").read_text())
    assert manifest["created_at"].endswith("Z")
    assert manifest["metadata"]["development_only"] is True
    assert manifest["metadata"]["production_path"] == "temms package from-mlflow"
    assert manifest["models"][0]["filename"] == "model.onnx"
    assert manifest["models"][0]["metadata"] == {"purpose": "dev"}

    validation = validate_package(package_dir, require_signature=False)

    assert validation.valid is True
    assert any("development-only" in warning for warning in validation.warnings)

    strict_validation = validate_package(
        package_dir,
        require_signature=False,
        strict_metadata=True,
    )

    assert strict_validation.valid is False
    assert any("development-only" in error for error in strict_validation.errors)
