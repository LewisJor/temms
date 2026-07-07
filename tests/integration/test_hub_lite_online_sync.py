"""
Online Hub Lite sync integration test.

This exercises the multi-VM path over local HTTP:
central Hub serves a signed package artifact, the edge daemon mirrors the
assignment, downloads the package archive, auto-applies it through its local API,
and pushes the activated rollout state back to central Hub.
"""

import asyncio
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import uvicorn
import pytest
from fastapi.testclient import TestClient

from temms.conditions.store import ConditionStore
from temms.core.cache import ModelCache
from temms.core.storage import ModelStorage
from temms.daemon.service import DaemonConfig, TEMMSDaemon
from temms.hub_lite import HubLiteStore
from temms.inference.runtime import InferenceRuntime
from temms.inference.server import create_app
from temms.policy.engine import PolicyEngine
from temms.slots.manager import SlotManager


@pytest.mark.asyncio
async def test_online_sync_downloads_signed_package_and_auto_applies(temp_dir, monkeypatch):
    """Test central Hub to edge daemon rollout over HTTP."""
    signing_key = "hub-secret"
    signing_key_file = temp_dir / "hub-signing.key"
    signing_key_file.write_text(signing_key, encoding="utf-8")
    _install_fake_mlflow(monkeypatch, temp_dir)
    central = _build_system(temp_dir / "central")
    edge = _build_system(temp_dir / "edge")

    central_app = create_app(
        slot_manager=central["slot_manager"],
        condition_store=central["condition_store"],
        policy_engine=central["policy_engine"],
        model_cache=central["model_cache"],
        model_storage=central["model_storage"],
        inference_runtime=central["inference_runtime"],
        daemon_config=DaemonConfig(
            model_dir=central["model_dir"],
            policy_dir=central["policy_dir"],
            rollout_signing_key_file=signing_key_file,
        ),
        hub_lite=central["hub_lite"],
    )

    central["hub_lite"].enroll_device(
        "edge-online",
        profile="x86_64-cpu",
        inventory={"runtimes": {"onnxruntime": {"available": True}}},
    )
    central_client = TestClient(central_app)
    packaged = central_client.post(
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
    assert packaged.status_code == 200, packaged.text
    assert packaged.json()["package"]["package_id"] == "mlflow-detector-7"
    assert packaged.json()["package"]["metadata"]["validation"]["signature_verified"] is True
    _release_package(central["hub_lite"], "mlflow-detector-7")

    central["hub_lite"].assign_rollout(
        "edge-online",
        "mlflow-detector-7",
        slot="vision",
        rollout_id="rollout-online",
    )

    edge["slot_manager"].create_slot(name="vision", description="Vision", required=True)
    edge_config = DaemonConfig(
        db_path=edge["db_path"],
        model_dir=edge["model_dir"],
        policy_dir=edge["policy_dir"],
        hub_state_path=edge["root"] / "hub_lite.json",
        telemetry_path=edge["root"] / "telemetry.jsonl",
        hub_device_id="edge-online",
        hub_device_profile="x86_64-cpu",
        hub_auto_apply=True,
        rollout_require_signature=True,
        rollout_signing_key=signing_key,
    )
    edge_daemon = TEMMSDaemon(
        config=edge_config,
        slot_manager=edge["slot_manager"],
        condition_store=edge["condition_store"],
        policy_engine=edge["policy_engine"],
        model_cache=edge["model_cache"],
        model_storage=edge["model_storage"],
        collectors=[],
    )
    edge_daemon.inference_runtime.load_model = AsyncMock(return_value=True)

    edge_app = create_app(
        slot_manager=edge_daemon.slot_manager,
        condition_store=edge_daemon.condition_store,
        policy_engine=edge_daemon.policy_engine,
        model_cache=edge_daemon.model_cache,
        model_storage=edge_daemon.model_storage,
        inference_runtime=edge_daemon.inference_runtime,
        daemon_config=edge_daemon.config,
        hub_lite=edge_daemon.hub_lite,
        telemetry=edge_daemon.telemetry,
    )

    central_port = _free_port()
    edge_port = _free_port()
    edge_daemon.config.hub_url = f"http://127.0.0.1:{central_port}"
    edge_daemon.config.inference_port = edge_port

    async with _serve(central_app, central_port), _serve(edge_app, edge_port):
        await edge_daemon._hub_sync_once()

    edge_rollout = edge_daemon.hub_lite.get_rollout("rollout-online")
    edge_package = edge_daemon.hub_lite.get_package("mlflow-detector-7")
    central_rollout = central["hub_lite"].get_rollout("rollout-online")

    assert edge_rollout["state"] == "activated"
    assert central_rollout["state"] == "activated"
    assert [entry["state"] for entry in central_rollout["history"]] == [
        "assigned",
        "downloading",
        "imported",
        "activated",
    ]
    assert edge_package is not None
    assert edge_package["path"].endswith(".temms.tar.zst")
    assert edge_package["metadata"]["online_artifact"]["sha256"] == edge_package["sha256"]
    assert edge_package["metadata"]["provenance"]["run_id"] == "run-online"
    assert edge_daemon.model_cache.get_model("detector-7") is not None
    edge_daemon.inference_runtime.load_model.assert_awaited_once_with(
        "vision",
        "detector-7",
    )
    assert edge_daemon.telemetry.read()[-1]["event_type"] == "hub.synced"


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
    }


def _release_package(hub: HubLiteStore, package_id: str) -> dict:
    hub.promote_package(
        package_id,
        "validated",
        actor="operator:validator",
        reason="runtime validation passed",
    )
    hub.promote_package(
        package_id,
        "approved",
        actor="operator:approver",
        reason="package approved for release",
    )
    return hub.promote_package(
        package_id,
        "released",
        actor="operator:release",
        reason="package released for rollout",
    )


def _install_fake_mlflow(monkeypatch, root):
    artifact_dir = root / "mlflow-artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "model.onnx").write_bytes(b"fake-online-model")

    class FakeClient:
        def get_model_version(self, name, version):
            return SimpleNamespace(
                version=version,
                run_id="run-online",
                source=f"s3://mlflow-artifacts/{name}/{version}",
                aliases=["champion"],
            )

        def get_run(self, run_id):
            return SimpleNamespace(
                info=SimpleNamespace(
                    run_id=run_id,
                    artifact_uri="s3://mlflow-artifacts/run-online/artifacts",
                ),
                data=SimpleNamespace(
                    params={
                        "input_schema": '{"shape":[1,3,224,224]}',
                        "output_schema": '{"shape":[1,1000]}',
                    },
                    metrics={"avg_latency_ms": 8.0},
                    tags={"mlflow.runName": "online-package"},
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
