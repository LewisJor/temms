"""
End-to-end Hub Lite MVP flow.

This proves the local equivalent of a multi-VM rollout:
central Hub registers a signed package, assigns it, exports an air-gap bundle
with package artifacts, and an edge agent imports and applies that rollout.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from temms.conditions.store import ConditionStore
from temms.core.cache import ModelCache
from temms.core.storage import ModelStorage
from temms.daemon.service import DaemonConfig
from temms.hub_lite import HubLiteStore
from temms.inference.runtime import InferenceRuntime
from temms.inference.server import create_app
from temms.policy.engine import PolicyEngine
from temms.slots.manager import SlotManager


def test_mlflow_signed_airgap_rollout_bundle_imports_and_activates_on_edge(temp_dir, monkeypatch):
    """Test MLflow-sourced signed package rollout from central Hub to edge."""
    signing_key = "hub-secret"
    signing_key_file = temp_dir / "hub-signing.key"
    signing_key_file.write_text(signing_key, encoding="utf-8")
    _install_fake_mlflow(monkeypatch, temp_dir)

    central = _build_system(temp_dir / "central")
    central_app = create_app(
        slot_manager=central["slot_manager"],
        condition_store=central["condition_store"],
        policy_engine=central["policy_engine"],
        model_cache=central["model_cache"],
        model_storage=central["model_storage"],
        inference_runtime=central["inference_runtime"],
        daemon_config=DaemonConfig(
            model_dir=central["root"] / "models",
            policy_dir=central["root"] / "policies",
            rollout_signing_key_file=signing_key_file,
        ),
        hub_lite=central["hub_lite"],
    )
    central_client = TestClient(central_app)

    enroll = central_client.post(
        "/v1/hub/devices/enroll",
        json={
            "device_id": "edge-1",
            "profile": "x86_64-cpu",
            "labels": {"site": "lab"},
            "inventory": {"runtimes": {"onnxruntime": {"available": True}}},
        },
    )
    assert enroll.status_code == 200

    package = central_client.post(
        "/v1/hub/packages/from-mlflow",
        headers={"X-TEMMS-Actor": "operator:mlops"},
        json={
            "model_uri": "models:/detector/7",
            "slot": "vision",
            "tracking_uri": "http://mlflow.example:5000",
            "device_profile": "x86_64-cpu",
            "runtime_constraints": {"runtimes": ["onnx"]},
            "runtime_options": {"providers": ["CPUExecutionProvider"]},
            "archive": True,
        },
    )
    assert package.status_code == 200, package.text
    package_payload = package.json()
    assert package_payload["signed"] is True
    assert package_payload["package"]["package_id"] == "mlflow-detector-7"
    assert package_payload["package"]["created_by"] == "operator:mlops"
    assert package_payload["package"]["metadata"]["validation"]["signature_verified"] is True
    _api_release_package(central_client, "mlflow-detector-7")

    assign = central_client.post(
        "/v1/hub/rollouts",
        json={
            "rollout_id": "rollout-e2e",
            "device_id": "edge-1",
            "package_id": "mlflow-detector-7",
            "slot": "vision",
            "require_approval": True,
        },
    )
    assert assign.status_code == 200, assign.text
    assert assign.json()["state"] == "assigned"
    assert assign.json()["approval"]["state"] == "pending"

    approval = central_client.post(
        "/v1/hub/rollouts/rollout-e2e/approve",
        headers={"X-TEMMS-Actor": "operator:approver"},
        json={"reason": "mission policy approved"},
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["approval"]["approved"] is True
    assert approval.json()["history"][-1]["state"] == "approved"

    export = central_client.post(
        "/v1/hub/airgap/export",
        json={"include_packages": True},
    )
    assert export.status_code == 200
    bundle = export.json()
    assert "mlflow-detector-7" in bundle["package_artifacts"]

    edge = _build_system(temp_dir / "edge")
    edge["slot_manager"].create_slot(name="vision", description="Vision", required=True)
    edge["inference_runtime"].load_model = AsyncMock(return_value=True)
    edge_app = create_app(
        slot_manager=edge["slot_manager"],
        condition_store=edge["condition_store"],
        policy_engine=edge["policy_engine"],
        model_cache=edge["model_cache"],
        model_storage=edge["model_storage"],
        inference_runtime=edge["inference_runtime"],
        hub_lite=edge["hub_lite"],
    )
    edge_client = TestClient(edge_app)

    imported = edge_client.post("/v1/hub/airgap/import", json=bundle)
    assert imported.status_code == 200
    assert imported.json()["imported"]["package_artifacts"] == 1
    edge_package = edge["hub_lite"].get_package("mlflow-detector-7")
    assert edge_package is not None
    assert edge_package["path"].endswith(".temms.tar.zst")
    assert edge_package["metadata"]["provenance"]["run_id"] == "run-e2e"

    apply = edge_client.post(
        "/v1/hub/rollouts/rollout-e2e/apply",
        json={"require_signature": True, "signing_key": signing_key},
    )
    assert apply.status_code == 200
    assert apply.json()["status"] == "activated"
    assert apply.json()["model"] == "detector-7"

    rollout = edge["hub_lite"].get_rollout("rollout-e2e")
    assert rollout["state"] == "activated"
    assert rollout["approval"]["approved"] is True
    assert rollout["approval"]["actor"] == "operator:approver"
    assert edge["model_cache"].get_model("detector-7") is not None
    edge["inference_runtime"].load_model.assert_awaited_once_with(
        "vision",
        "detector-7",
    )


def _api_release_package(client: TestClient, package_id: str) -> None:
    for state, actor in [
        ("validated", "operator:validator"),
        ("approved", "operator:approver"),
        ("released", "operator:release"),
    ]:
        response = client.post(
            f"/v1/hub/packages/{package_id}/promote",
            json={
                "state": state,
                "actor": actor,
                "reason": f"package {state}",
            },
        )
        assert response.status_code == 200, response.text


def _build_system(root):
    root.mkdir(parents=True)
    db_path = root / "temms.db"
    model_dir = root / "models"
    model_dir.mkdir()
    model_cache = ModelCache(db_path)
    model_storage = ModelStorage(model_dir)
    slot_manager = SlotManager(db_path)
    condition_store = ConditionStore(db_path)
    policy_engine = PolicyEngine(condition_store)
    inference_runtime = InferenceRuntime(model_cache, model_storage)
    hub_lite = HubLiteStore(root / "hub_lite.json")
    return {
        "root": root,
        "model_cache": model_cache,
        "model_storage": model_storage,
        "slot_manager": slot_manager,
        "condition_store": condition_store,
        "policy_engine": policy_engine,
        "inference_runtime": inference_runtime,
        "hub_lite": hub_lite,
    }


def _install_fake_mlflow(monkeypatch, root):
    artifact_dir = root / "mlflow-artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "model.onnx").write_bytes(b"fake-e2e-model")

    class FakeClient:
        def get_model_version(self, name, version):
            return SimpleNamespace(
                version=version,
                run_id="run-e2e",
                source=f"s3://mlflow-artifacts/{name}/{version}",
                aliases=["champion"],
            )

        def get_run(self, run_id):
            return SimpleNamespace(
                info=SimpleNamespace(
                    run_id=run_id,
                    artifact_uri="s3://mlflow-artifacts/run-e2e/artifacts",
                ),
                data=SimpleNamespace(
                    params={
                        "input_schema": '{"shape":[1,3,224,224]}',
                        "output_schema": '{"shape":[1,1000]}',
                    },
                    metrics={"avg_latency_ms": 7.0},
                    tags={"mlflow.runName": "e2e-package"},
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
