"""
MVP multi-VM acceptance flow.

This keeps the proof close to the product thesis: one central Hub Lite instance
coordinates multiple edge agents with different device profiles, while the
edges remain independently executable through online sync or air-gap import.
"""

import asyncio
import hashlib
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import uvicorn
from fastapi.testclient import TestClient

from temms.conditions.store import ConditionStore
from temms.core.cache import ModelCache, ModelFormat
from temms.core.storage import ModelStorage
from temms.daemon.service import DaemonConfig, TEMMSDaemon
from temms.hub_lite import HubLiteStore
from temms.inference.runtime import InferenceRuntime
from temms.inference.server import create_app
from temms.policy.engine import PolicyEngine
from temms.slots.manager import SlotManager
from temms.telemetry import TelemetryBuffer


@pytest.mark.asyncio
async def test_mvp_acceptance_two_edges_online_airgap_rollback_and_evidence(temp_dir, monkeypatch):
    """Prove the MVP fleet shape across two edge profiles and transfer modes."""
    signing_key = "hub-secret"
    signing_key_file = temp_dir / "hub-signing.key"
    signing_key_file.write_text(signing_key, encoding="utf-8")
    _install_fake_mlflow(monkeypatch, temp_dir)
    central = _build_system(temp_dir / "central")
    central_app = _create_app(
        central,
        daemon_config=DaemonConfig(
            model_dir=central["model_dir"],
            policy_dir=central["policy_dir"],
            rollout_signing_key_file=signing_key_file,
        ),
    )
    central_client = TestClient(central_app)

    central["hub_lite"].enroll_device(
        "edge-online",
        profile="x86_64-cpu",
        labels={"site": "lab-a", "mode": "online"},
        inventory={"runtimes": {"onnxruntime": {"available": True}}},
    )
    central["hub_lite"].enroll_device(
        "edge-airgap",
        profile="rpi5-tflite",
        labels={"site": "lab-b", "mode": "airgap"},
        inventory={"runtimes": {"tflite_runtime": {"available": True}}},
    )

    online_packaged = central_client.post(
        "/v1/hub/packages/from-mlflow",
        headers={"X-TEMMS-Actor": "operator:mlops"},
        json={
            "model_uri": "models:/online-detector/1",
            "slot": "vision",
            "tracking_uri": "http://mlflow.example:5000",
            "device_profile": "x86_64-cpu",
            "runtime_constraints": {"runtimes": ["onnx"]},
            "runtime_options": {"providers": ["CPUExecutionProvider"]},
            "archive": True,
        },
    )
    assert online_packaged.status_code == 200, online_packaged.text
    assert online_packaged.json()["package"]["package_id"] == "mlflow-online-detector-1"

    airgap_packaged = central_client.post(
        "/v1/hub/packages/from-mlflow",
        headers={"X-TEMMS-Actor": "operator:mlops"},
        json={
            "model_uri": "models:/airgap-detector/1",
            "slot": "vision",
            "tracking_uri": "http://mlflow.example:5000",
            "device_profile": "rpi5-tflite",
            "runtime_constraints": {"runtimes": ["tflite_runtime"]},
            "archive": True,
        },
    )
    assert airgap_packaged.status_code == 200, airgap_packaged.text
    assert airgap_packaged.json()["package"]["package_id"] == "mlflow-airgap-detector-1"

    with pytest.raises(ValueError, match="not compatible"):
        central["hub_lite"].assign_rollout(
            "edge-online",
            "mlflow-airgap-detector-1",
            slot="vision",
            rollout_id="rollout-wrong-profile",
        )

    central["hub_lite"].assign_rollout(
        "edge-online",
        "mlflow-online-detector-1",
        slot="vision",
        rollout_id="rollout-online",
    )
    central["hub_lite"].assign_rollout(
        "edge-airgap",
        "mlflow-airgap-detector-1",
        slot="vision",
        rollout_id="rollout-airgap",
    )

    central_port = _free_port()
    online_edge_port = _free_port()

    online_edge = _build_system(temp_dir / "edge-online")
    online_edge["slot_manager"].create_slot(
        name="vision",
        description="Vision",
        required=True,
    )
    _seed_cached_model(online_edge, "model-online-v0")
    online_edge["slot_manager"].activate_model(
        "vision",
        "model-online-v0",
        "startup",
        "seed previous known-good",
    )
    online_config = DaemonConfig(
        db_path=online_edge["db_path"],
        model_dir=online_edge["model_dir"],
        policy_dir=online_edge["policy_dir"],
        hub_state_path=online_edge["root"] / "hub_lite.json",
        telemetry_path=online_edge["root"] / "telemetry.jsonl",
        hub_url=f"http://127.0.0.1:{central_port}",
        inference_port=online_edge_port,
        hub_device_id="edge-online",
        hub_device_profile="x86_64-cpu",
        hub_auto_apply=True,
        rollout_require_signature=True,
        rollout_signing_key=signing_key,
    )
    online_daemon = TEMMSDaemon(
        config=online_config,
        slot_manager=online_edge["slot_manager"],
        condition_store=online_edge["condition_store"],
        policy_engine=online_edge["policy_engine"],
        model_cache=online_edge["model_cache"],
        model_storage=online_edge["model_storage"],
        collectors=[],
    )
    online_daemon.inference_runtime.load_model = AsyncMock(return_value=True)
    online_app = _create_app(
        online_edge,
        inference_runtime=online_daemon.inference_runtime,
        daemon_config=online_daemon.config,
        hub_lite=online_daemon.hub_lite,
        telemetry=online_daemon.telemetry,
    )

    async with _serve(central_app, central_port), _serve(online_app, online_edge_port):
        await online_daemon._hub_sync_once()

    assert online_daemon.hub_lite.get_rollout("rollout-online")["state"] == "activated"
    central_online_rollout = central["hub_lite"].get_rollout("rollout-online")
    assert central_online_rollout["state"] == "activated"
    assert [entry["state"] for entry in central_online_rollout["history"]] == [
        "assigned",
        "downloading",
        "imported",
        "activated",
    ]
    assert online_daemon.model_cache.get_model("online-detector-1") is not None
    online_daemon.inference_runtime.load_model.assert_awaited_once_with(
        "vision",
        "online-detector-1",
    )

    online_client = TestClient(online_app)
    rollback = online_client.post(
        "/v1/hub/rollouts/rollout-online/rollback",
        json={"reason": "acceptance test"},
    )
    assert rollback.status_code == 200
    assert rollback.json()["status"] == "rolled_back"
    assert rollback.json()["model"] == "model-online-v0"

    async with _serve(central_app, central_port):
        await online_daemon._hub_sync_once()
    assert central["hub_lite"].get_rollout("rollout-online")["state"] == "rolled_back"

    bundle_response = central_client.post(
        "/v1/hub/airgap/export",
        json={"include_packages": True},
    )
    assert bundle_response.status_code == 200
    bundle = bundle_response.json()
    assert {"mlflow-online-detector-1", "mlflow-airgap-detector-1"} <= set(
        bundle["package_artifacts"]
    )

    airgap_edge = _build_system(temp_dir / "edge-airgap")
    airgap_edge["slot_manager"].create_slot(
        name="vision",
        description="Vision",
        required=True,
    )
    airgap_edge["inference_runtime"].load_model = AsyncMock(return_value=True)
    airgap_app = _create_app(airgap_edge)
    airgap_client = TestClient(airgap_app)

    imported = airgap_client.post("/v1/hub/airgap/import", json=bundle)
    assert imported.status_code == 200
    assert imported.json()["imported"]["package_artifacts"] == 2
    assert airgap_edge["hub_lite"].get_device("edge-airgap")["profile"] == "rpi5-tflite"

    applied = airgap_client.post(
        "/v1/hub/rollouts/rollout-airgap/apply",
        json={"require_signature": True, "signing_key": signing_key},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "activated"
    assert applied.json()["model"] == "airgap-detector-1"
    assert airgap_edge["model_cache"].get_model("airgap-detector-1") is not None
    airgap_edge["inference_runtime"].load_model.assert_awaited_once_with(
        "vision",
        "airgap-detector-1",
    )

    central_status = central_client.get("/v1/hub/deployment-status")
    assert central_status.status_code == 200
    central_payload = central_status.json()
    assert set(central_payload["devices"]) == {"edge-online", "edge-airgap"}
    assert central_payload["rollouts"]["rollout-online"]["state"] == "rolled_back"
    assert central_payload["rollouts"]["rollout-airgap"]["state"] == "assigned"

    online_evidence = online_client.post("/v1/hub/evidence/export", json={})
    assert online_evidence.status_code == 200
    online_bundle = online_evidence.json()
    assert online_bundle["schema_version"] == "temms-evidence-bundle/v1"
    assert online_bundle["diagnostics"]["schema_version"] == "temms-diagnostics/v1"
    assert online_bundle["diagnostics"]["model_cache"]["models"] >= 2
    assert online_bundle["hub_lite"]["rollouts"]["rollout-online"]["state"] == "rolled_back"
    assert {event["event_type"] for event in online_bundle["telemetry"]["events"]} >= {
        "hub.synced",
        "rollout.activated",
        "slot.rollback",
        "rollout.rolled_back",
    }
    assert {event["state"] for event in online_bundle["rollout_events"]} >= {
        "assigned",
        "activated",
        "rolled_back",
    }
    assert "rollout" in {entry["kind"] for entry in online_bundle["timeline"]}

    airgap_evidence = airgap_client.post("/v1/hub/evidence/export", json={})
    assert airgap_evidence.status_code == 200
    airgap_bundle = airgap_evidence.json()
    assert airgap_bundle["diagnostics"]["schema_version"] == "temms-diagnostics/v1"
    assert airgap_bundle["diagnostics"]["model_cache"]["models"] >= 1
    assert airgap_bundle["hub_lite"]["rollouts"]["rollout-airgap"]["state"] == "activated"
    airgap_slots = {slot["name"]: slot for slot in airgap_bundle["slots"]}
    assert airgap_slots["vision"]["active_model_id"] == "airgap-detector-1"


@asynccontextmanager
async def _serve(app, port):
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        yield
    finally:
        server.should_exit = True
        await task


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _build_system(root):
    root.mkdir(parents=True)
    db_path = root / "temms.db"
    model_dir = root / "models"
    policy_dir = root / "policies"
    model_dir.mkdir()
    policy_dir.mkdir()
    model_cache = ModelCache(db_path)
    model_storage = ModelStorage(model_dir)
    slot_manager = SlotManager(db_path)
    condition_store = ConditionStore(db_path)
    policy_engine = PolicyEngine(condition_store)
    inference_runtime = InferenceRuntime(model_cache, model_storage)
    hub_lite = HubLiteStore(root / "hub_lite.json")
    telemetry = TelemetryBuffer(root / "telemetry.jsonl")
    return {
        "root": root,
        "db_path": db_path,
        "model_dir": model_dir,
        "policy_dir": policy_dir,
        "model_cache": model_cache,
        "model_storage": model_storage,
        "slot_manager": slot_manager,
        "condition_store": condition_store,
        "policy_engine": policy_engine,
        "inference_runtime": inference_runtime,
        "hub_lite": hub_lite,
        "telemetry": telemetry,
    }


def _create_app(
    system,
    *,
    inference_runtime=None,
    daemon_config=None,
    hub_lite=None,
    telemetry=None,
):
    return create_app(
        slot_manager=system["slot_manager"],
        condition_store=system["condition_store"],
        policy_engine=system["policy_engine"],
        model_cache=system["model_cache"],
        model_storage=system["model_storage"],
        inference_runtime=inference_runtime or system["inference_runtime"],
        daemon_config=daemon_config,
        hub_lite=hub_lite or system["hub_lite"],
        telemetry=telemetry or system["telemetry"],
    )


def _seed_cached_model(system, model_id):
    model_path = system["model_dir"] / f"{model_id}.onnx"
    model_bytes = f"seed-{model_id}".encode("utf-8")
    model_path.write_bytes(model_bytes)
    system["model_cache"].add_cached_model(
        model_id=model_id,
        name=model_id,
        version="0.0.0",
        format=ModelFormat.ONNX,
        path=model_path,
        sha256=hashlib.sha256(model_bytes).hexdigest(),
        size_bytes=len(model_bytes),
        package_id="seed",
    )


def _install_fake_mlflow(monkeypatch, root):
    artifacts_root = root / "mlflow-artifacts"
    online_artifacts = artifacts_root / "online"
    airgap_artifacts = artifacts_root / "airgap"
    online_artifacts.mkdir(parents=True)
    airgap_artifacts.mkdir(parents=True)
    (online_artifacts / "model.onnx").write_bytes(b"online-onnx-model")
    (airgap_artifacts / "model.tflite").write_bytes(b"airgap-tflite-model")

    model_artifacts = {
        "online-detector": online_artifacts,
        "airgap-detector": airgap_artifacts,
    }

    class FakeClient:
        def get_model_version(self, name, version):
            return SimpleNamespace(
                version=version,
                run_id=f"run-{name}",
                source=f"s3://mlflow-artifacts/{name}/{version}",
                aliases=["champion"],
            )

        def get_run(self, run_id):
            model_name = run_id.removeprefix("run-")
            return SimpleNamespace(
                info=SimpleNamespace(
                    run_id=run_id,
                    artifact_uri=f"s3://mlflow-artifacts/{run_id}/artifacts",
                ),
                data=SimpleNamespace(
                    params={
                        "input_schema": '{"shape":[1,3,224,224]}',
                        "output_schema": '{"shape":[1,1000]}',
                    },
                    metrics={"avg_latency_ms": 9.0},
                    tags={"mlflow.runName": f"{model_name}-package"},
                ),
            )

        def download_artifacts(self, run_id, path, dst_path):
            import shutil

            model_name = run_id.removeprefix("run-")
            dest = Path(dst_path) / "model"
            shutil.copytree(model_artifacts[model_name], dest)
            return str(dest)

    fake_mlflow = SimpleNamespace(
        set_tracking_uri=lambda uri: None,
        tracking=SimpleNamespace(MlflowClient=FakeClient),
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)
