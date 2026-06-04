"""
Tests for production package builds from upstream registries.
"""

from __future__ import annotations

import sys
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from temms.core.package_builder import build_package_from_mlflow
from temms.core.signing import ValidationResult, validate_package


def test_build_package_from_mlflow_rejects_invalid_generated_package(temp_dir, monkeypatch):
    artifact_dir = _mlflow_artifact_dir(temp_dir, "invalid-package-artifacts")
    _install_fake_mlflow(monkeypatch, artifact_dir)

    def invalid_package(*args, **kwargs):
        return ValidationResult(False, ["model hash mismatch"], [])

    monkeypatch.setattr("temms.core.package_builder.validate_package", invalid_package)

    with pytest.raises(ValueError, match="Generated TEMMS package is invalid"):
        build_package_from_mlflow(
            model_uri="models:/detector/7",
            slot="vision",
            policy_path=None,
            output_dir=temp_dir,
        )


def test_build_package_from_mlflow_validates_signed_archive(temp_dir, monkeypatch):
    artifact_dir = _mlflow_artifact_dir(temp_dir, "archive-artifacts")
    _install_fake_mlflow(monkeypatch, artifact_dir)
    validation_calls = []

    def valid_package(package_path, **kwargs):
        validation_calls.append((Path(package_path).suffixes, kwargs))
        return ValidationResult(True, [], [])

    monkeypatch.setattr("temms.core.package_builder.validate_package", valid_package)

    archive_path = build_package_from_mlflow(
        model_uri="models:/detector/7",
        slot="vision",
        policy_path=None,
        output_dir=temp_dir,
        signing_key="secret",
        archive=True,
    )

    assert archive_path.name == "mlflow-detector-7.temms.tar.zst"
    assert validation_calls == [
        (
            [".temms"],
            {"require_signature": False, "signing_key": None, "strict_metadata": False},
        ),
        (
            [".temms"],
            {"require_signature": True, "signing_key": "secret", "strict_metadata": False},
        ),
        (
            [".temms", ".tar", ".zst"],
            {"require_signature": True, "signing_key": "secret", "strict_metadata": False},
        ),
    ]


def test_build_package_from_mlflow_does_not_overwrite_existing_output(temp_dir, monkeypatch):
    artifact_dir = _mlflow_artifact_dir(temp_dir, "immutable-artifacts")
    _install_fake_mlflow(monkeypatch, artifact_dir)
    existing = temp_dir / "mlflow-detector-7.temms"
    existing.mkdir()
    marker = existing / "marker.txt"
    marker.write_text("keep-me", encoding="utf-8")

    with pytest.raises(FileExistsError, match="TEMMS package output already exists"):
        build_package_from_mlflow(
            model_uri="models:/detector/7",
            slot="vision",
            policy_path=None,
            output_dir=temp_dir,
        )

    assert marker.read_text(encoding="utf-8") == "keep-me"

    package_dir = build_package_from_mlflow(
        model_uri="models:/detector/7",
        slot="vision",
        policy_path=None,
        output_dir=temp_dir,
        overwrite=True,
    )

    assert package_dir == existing
    assert (package_dir / "manifest.json").exists()
    assert not marker.exists()


def test_build_package_from_mlflow_does_not_publish_partial_package_on_failure(
    temp_dir, monkeypatch
):
    artifact_dir = _mlflow_artifact_dir(temp_dir, "partial-failure-artifacts")
    _install_fake_mlflow(monkeypatch, artifact_dir, params={})

    with pytest.raises(ValueError, match="requires input_schema and output_schema"):
        build_package_from_mlflow(
            model_uri="models:/detector/7",
            slot="vision",
            policy_path=None,
            output_dir=temp_dir,
        )

    assert not (temp_dir / "mlflow-detector-7.temms").exists()


def test_build_package_from_mlflow_requires_runtime_constraints_by_default(
    temp_dir,
    monkeypatch,
):
    artifact_dir = _mlflow_artifact_dir(temp_dir, "missing-runtime-artifacts")
    _install_fake_mlflow(
        monkeypatch,
        artifact_dir,
        params={
            "input_schema": '{"shape":[1,3,224,224]}',
            "output_schema": '{"shape":[1,1000]}',
        },
    )

    with pytest.raises(ValueError, match="requires runtime_constraints"):
        build_package_from_mlflow(
            model_uri="models:/detector/7",
            slot="vision",
            policy_path=None,
            output_dir=temp_dir,
        )

    assert not (temp_dir / "mlflow-detector-7.temms").exists()


def test_build_package_from_mlflow_records_explicit_empty_benchmark_metadata(
    temp_dir,
    monkeypatch,
):
    artifact_dir = _mlflow_artifact_dir(temp_dir, "no-benchmark-artifacts")
    _install_fake_mlflow(monkeypatch, artifact_dir)

    package_dir = build_package_from_mlflow(
        model_uri="models:/detector/7",
        slot="vision",
        policy_path=None,
        output_dir=temp_dir,
        runtime_constraints_override={"runtimes": ["onnx"]},
    )

    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    benchmark = manifest["models"][0]["benchmark"]
    assert benchmark["available"] is False
    assert benchmark["_source"]["type"] == "mlflow_run_metrics"
    assert benchmark["_source"]["metric_keys"] == {}
    assert benchmark["_source"]["metrics_sha256"]
    assert validate_package(package_dir, strict_metadata=True).valid is True


def test_build_package_from_mlflow_records_build_and_artifact_fingerprints(
    temp_dir,
    monkeypatch,
):
    artifact_dir = _mlflow_artifact_dir(temp_dir, "fingerprint-artifacts")
    _install_fake_mlflow(monkeypatch, artifact_dir)

    package_dir = build_package_from_mlflow(
        model_uri="models:/detector/7",
        slot="vision",
        policy_path=None,
        output_dir=temp_dir,
        tracking_uri="http://mlflow.example",
        runtime_constraints_override={"runtimes": ["onnx"]},
        signing_key="secret",
    )

    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    artifact_metadata = manifest["provenance"]["artifact_metadata"]
    assert artifact_metadata["path"] == "model.onnx"
    assert artifact_metadata["format"] == "onnx"
    assert artifact_metadata["size_bytes"] == len(b"mlflow-onnx")
    assert artifact_metadata["sha256"] == manifest["models"][0]["sha256"]
    assert manifest["provenance"]["artifact_metadata_sha256"]
    assert (
        manifest["models"][0]["provenance"]["artifact_metadata_sha256"]
        == manifest["provenance"]["artifact_metadata_sha256"]
    )
    assert manifest["metadata"]["build"] == {
        "schema_version": "temms-package-build/v1",
        "workflow": "temms package from-mlflow",
        "builder": "temms-package-builder",
        "tracking_uri": "http://mlflow.example",
        "requested_model_uri": "models:/detector/7",
        "resolved_model_uri": "models:/detector/7",
        "artifact_metadata_sha256": manifest["provenance"]["artifact_metadata_sha256"],
        "archive_requested": False,
        "signed": True,
    }


def test_build_package_from_mlflow_rejects_invalid_policy_yaml(
    temp_dir,
    monkeypatch,
):
    artifact_dir = _mlflow_artifact_dir(temp_dir, "invalid-policy-artifacts")
    _install_fake_mlflow(monkeypatch, artifact_dir)
    policy_path = temp_dir / "bad-policy.yaml"
    policy_path.write_text(
        """
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: bad-policy
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid policy file"):
        build_package_from_mlflow(
            model_uri="models:/detector/7",
            slot="vision",
            policy_path=policy_path,
            output_dir=temp_dir,
            runtime_constraints_override={"runtimes": ["onnx"]},
        )

    assert not (temp_dir / "mlflow-detector-7.temms").exists()


def test_build_package_from_mlflow_rejects_policy_slot_mismatch(
    temp_dir,
    monkeypatch,
):
    artifact_dir = _mlflow_artifact_dir(temp_dir, "mismatched-policy-artifacts")
    _install_fake_mlflow(monkeypatch, artifact_dir)
    policy_path = temp_dir / "targeting-policy.yaml"
    _write_policy(policy_path, slot="targeting")

    with pytest.raises(ValueError, match="Policy slot mismatch"):
        build_package_from_mlflow(
            model_uri="models:/detector/7",
            slot="vision",
            policy_path=policy_path,
            output_dir=temp_dir,
            runtime_constraints_override={"runtimes": ["onnx"]},
        )

    assert not (temp_dir / "mlflow-detector-7.temms").exists()


def _mlflow_artifact_dir(temp_dir: Path, name: str) -> Path:
    artifact_dir = temp_dir / name
    artifact_dir.mkdir()
    (artifact_dir / "model.onnx").write_bytes(b"mlflow-onnx")
    return artifact_dir


def _write_policy(path: Path, *, slot: str) -> None:
    path.write_text(
        f"""
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: {slot}-policy
spec:
  slot: {slot}
  rules:
    - name: route
      priority: 50
      conditions:
        all:
          - metric: mission.mode
            operator: eq
            value: active
      action:
        switch_to: detector-7
""".lstrip(),
        encoding="utf-8",
    )


def _install_fake_mlflow(
    monkeypatch,
    artifact_dir: Path,
    params: dict[str, str] | None = None,
) -> None:
    params = (
        params
        if params is not None
        else {
            "input_schema": '{"shape":[1,3,224,224]}',
            "output_schema": '{"shape":[1,1000]}',
            "runtime_constraints": '{"runtimes":["onnx"]}',
        }
    )

    class FakeClient:
        def get_model_version(self, name, version):
            return SimpleNamespace(version=version, run_id="run-123")

        def get_run(self, run_id):
            return SimpleNamespace(
                info=SimpleNamespace(run_id=run_id),
                data=SimpleNamespace(
                    params=params,
                    metrics={},
                    tags={},
                ),
            )

        def download_artifacts(self, run_id, path, dst_path):
            import shutil

            dest = Path(dst_path) / "model"
            shutil.copytree(artifact_dir, dest)
            return str(dest)

    fake_mlflow = SimpleNamespace(
        set_tracking_uri=lambda uri: None,
        tracking=SimpleNamespace(MlflowClient=FakeClient),
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)
