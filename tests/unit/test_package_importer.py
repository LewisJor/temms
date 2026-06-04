"""
Tests for TEMMS package import behavior.
"""

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from temms.core.package import PackageImporter
from temms.core.signing import sign_package


def _write_signed_production_package(
    package_dir,
    *,
    package_id="pkg-repeat",
    version="1.0.0",
    model_id="model-repeat-v1",
    model_bytes=b"repeat-model-v1",
    policy_names=("policy-a",),
):
    """Write a signed package with production-shaped metadata."""
    models_dir = package_dir / "models"
    policies_dir = package_dir / "policies"
    models_dir.mkdir(parents=True, exist_ok=True)
    policies_dir.mkdir(parents=True, exist_ok=True)

    model_file = models_dir / "model.onnx"
    model_file.write_bytes(model_bytes)
    model_sha = hashlib.sha256(model_bytes).hexdigest()

    for old_policy in policies_dir.glob("*.yaml"):
        old_policy.unlink()

    policies = []
    for policy_name in policy_names:
        filename = f"{policy_name}.yaml"
        (policies_dir / filename).write_text(
            f"""
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: {package_id}-{policy_name}
spec:
  slot: vision
  rules:
    - name: route-{policy_name}
      priority: 50
      conditions:
        all:
          - metric: mission.mode
            operator: eq
            value: active
      action:
        switch_to: {model_id}
""".lstrip(),
            encoding="utf-8",
        )
        policies.append({"name": policy_name, "filename": filename, "slot": "vision"})

    manifest = {
        "schema_version": "v1",
        "package_id": package_id,
        "name": f"{package_id}-package",
        "version": version,
        "created_at": "2024-01-01T00:00:00Z",
        "models": [
            {
                "id": model_id,
                "name": "repeat-model",
                "version": version,
                "format": "onnx",
                "filename": "model.onnx",
                "sha256": model_sha,
                "size_bytes": len(model_bytes),
                "input_schema": {"shape": [1, 3, 224, 224], "dtype": "float32"},
                "output_schema": {"shape": [1, 1000], "dtype": "float32"},
                "runtime_constraints": {"device_profiles": ["x86_64-cpu"]},
                "benchmark": {"available": False, "source": "unit-test"},
                "provenance": {
                    "source": "unit-test",
                    "run_id": f"run-{version}",
                    "artifact_sha256": model_sha,
                },
            }
        ],
        "policies": policies,
        "compatibility": {"device_profiles": ["x86_64-cpu"]},
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    sign_package(package_dir, "test-key", signer="unit-test")
    return manifest


def test_package_importer_requires_signature_by_default(temp_dir, model_cache, model_storage):
    """Direct edge imports should be signing-first unless a lab explicitly opts out."""
    package_dir = temp_dir / "pkg-unsigned-default.temms"
    models_dir = package_dir / "models"
    models_dir.mkdir(parents=True)
    model_bytes = b"unsigned-default-onnx"
    model_file = models_dir / "model.onnx"
    model_file.write_bytes(model_bytes)
    manifest = {
        "schema_version": "v1",
        "package_id": "pkg-unsigned-default",
        "name": "unsigned-default-package",
        "version": "1.0.0",
        "created_at": "2024-01-01T00:00:00Z",
        "models": [
            {
                "id": "model-unsigned-default-v1",
                "name": "model-unsigned-default",
                "version": "1.0.0",
                "format": "onnx",
                "filename": "model.onnx",
                "sha256": hashlib.sha256(model_bytes).hexdigest(),
                "size_bytes": len(model_bytes),
            }
        ],
        "policies": [],
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    importer = PackageImporter(
        cache_dir=temp_dir / "cache",
        model_cache=model_cache,
        storage=model_storage,
    )

    with pytest.raises(ValueError, match="Signature verification requires a signing key"):
        importer.import_package(package_dir)

    assert model_cache.list_models() == []
    assert model_cache.list_packages() == []


def test_reimport_same_signed_package_is_idempotent(
    temp_dir,
    model_cache,
    model_storage,
):
    """Repeated imports should upsert cache rows and converge policy files."""
    package_dir = temp_dir / "pkg-repeat.temms"
    _write_signed_production_package(package_dir)
    active_policy_dir = temp_dir / "active-policies"
    importer = PackageImporter(
        cache_dir=temp_dir / "cache",
        model_cache=model_cache,
        storage=model_storage,
        active_policy_dir=active_policy_dir,
        require_signature=True,
        signing_key="test-key",
        device_profile="x86_64-cpu",
    )

    first = importer.import_package(package_dir)
    second = importer.import_package(package_dir)

    assert [model.id for model in first.models] == ["model-repeat-v1"]
    assert [model.id for model in second.models] == ["model-repeat-v1"]
    assert [model.id for model in model_cache.list_models()] == ["model-repeat-v1"]
    assert [package.id for package in model_cache.list_packages()] == ["pkg-repeat"]
    assert sorted(path.name for path in active_policy_dir.iterdir()) == [
        "pkg-repeat-policy-a.yaml"
    ]
    cached_policy_dir = temp_dir / "cache" / "policies" / "pkg-repeat"
    assert sorted(path.name for path in cached_policy_dir.iterdir()) == ["policy-a.yaml"]
    imported_package = model_cache.get_package("pkg-repeat")
    assert imported_package is not None
    assert imported_package.manifest["_temms_import"]["signature_verified"] is True


def test_reimport_changed_package_removes_stale_active_policies(
    temp_dir,
    model_cache,
    model_storage,
):
    """Changed package contents should remove stale package-scoped active policies."""
    package_dir = temp_dir / "pkg-repeat.temms"
    _write_signed_production_package(
        package_dir,
        policy_names=("policy-a", "policy-b"),
    )
    active_policy_dir = temp_dir / "active-policies"
    importer = PackageImporter(
        cache_dir=temp_dir / "cache",
        model_cache=model_cache,
        storage=model_storage,
        active_policy_dir=active_policy_dir,
        require_signature=True,
        signing_key="test-key",
        device_profile="x86_64-cpu",
    )

    importer.import_package(package_dir)
    assert sorted(path.name for path in active_policy_dir.iterdir()) == [
        "pkg-repeat-policy-a.yaml",
        "pkg-repeat-policy-b.yaml",
    ]

    new_bytes = b"repeat-model-v1-updated"
    new_manifest = _write_signed_production_package(
        package_dir,
        version="1.0.1",
        model_bytes=new_bytes,
        policy_names=("policy-a",),
    )
    importer.import_package(package_dir)

    assert [model.id for model in model_cache.list_models()] == ["model-repeat-v1"]
    cached_model = model_cache.get_model("model-repeat-v1")
    assert cached_model is not None
    assert cached_model.sha256 == new_manifest["models"][0]["sha256"]
    assert cached_model.path.read_bytes() == new_bytes
    cached_package = model_cache.get_package("pkg-repeat")
    assert cached_package is not None
    assert cached_package.version == "1.0.1"
    assert sorted(path.name for path in active_policy_dir.iterdir()) == [
        "pkg-repeat-policy-a.yaml"
    ]
    cached_policy_dir = temp_dir / "cache" / "policies" / "pkg-repeat"
    assert sorted(path.name for path in cached_policy_dir.iterdir()) == ["policy-a.yaml"]


def test_import_package_skips_mlflow_registration_by_default(
    temp_dir,
    model_cache,
    model_storage,
    monkeypatch,
):
    """Edge package imports should not contact MLflow unless explicitly enabled."""
    package_dir = temp_dir / "pkg-edge.temms"
    models_dir = package_dir / "models"
    models_dir.mkdir(parents=True)
    model_bytes = b"tiny-onnx"
    model_file = models_dir / "model.onnx"
    model_file.write_bytes(model_bytes)
    manifest = {
        "schema_version": "v1",
        "package_id": "pkg-edge",
        "name": "edge-package",
        "version": "1.0.0",
        "created_at": "2024-01-01T00:00:00Z",
        "models": [
            {
                "id": "model-edge-v1",
                "name": "model-edge",
                "version": "1.0.0",
                "format": "onnx",
                "filename": "model.onnx",
                "sha256": hashlib.sha256(model_bytes).hexdigest(),
                "size_bytes": len(model_bytes),
            }
        ],
        "policies": [],
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    sign_package(package_dir, "test-key", signer="unit-test")

    mlflow_bridge_touched = False

    class FakeMLflowBridge:
        def __init__(self):
            nonlocal mlflow_bridge_touched
            mlflow_bridge_touched = True
            self.available = True

        def register_imported_models(self, import_result):
            return len(import_result.models)

    monkeypatch.delenv("TEMMS_MLFLOW_AUTO_REGISTER", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "temms.mlflow_bridge",
        SimpleNamespace(MLflowBridge=FakeMLflowBridge),
    )

    importer = PackageImporter(
        cache_dir=temp_dir / "cache",
        model_cache=model_cache,
        storage=model_storage,
        require_signature=True,
        signing_key="test-key",
        strict_metadata=False,
    )

    result = importer.import_package(package_dir)

    assert [model.id for model in result.models] == ["model-edge-v1"]
    assert mlflow_bridge_touched is False


def test_signed_import_requires_production_metadata_by_default(
    temp_dir,
    model_cache,
    model_storage,
):
    """Signed edge imports should not accept lab-shaped metadata by default."""
    package_dir = temp_dir / "pkg-lab-signed.temms"
    models_dir = package_dir / "models"
    models_dir.mkdir(parents=True)
    model_bytes = b"lab-signed-onnx"
    model_file = models_dir / "model.onnx"
    model_file.write_bytes(model_bytes)
    manifest = {
        "schema_version": "v1",
        "package_id": "pkg-lab-signed",
        "name": "lab-signed-package",
        "version": "1.0.0",
        "created_at": "2024-01-01T00:00:00Z",
        "models": [
            {
                "id": "model-lab-signed-v1",
                "name": "model-lab-signed",
                "version": "1.0.0",
                "format": "onnx",
                "filename": "model.onnx",
                "sha256": hashlib.sha256(model_bytes).hexdigest(),
                "size_bytes": len(model_bytes),
            }
        ],
        "policies": [],
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    sign_package(package_dir, "test-key", signer="unit-test")

    importer = PackageImporter(
        cache_dir=temp_dir / "cache",
        model_cache=model_cache,
        storage=model_storage,
        require_signature=True,
        signing_key="test-key",
    )

    with pytest.raises(ValueError, match="Model metadata incomplete"):
        importer.import_package(package_dir)

    assert model_cache.list_models() == []
    assert model_cache.list_packages() == []
