"""
Unit tests for the inference server.
"""

import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock
from fastapi import HTTPException
from fastapi.testclient import TestClient

from temms.core.cache import ModelFormat
from temms.core.package_archive import create_package_archive
from temms.core.package_catalog import package_source_sha256
from temms.core.signing import sign_package
from temms.daemon.pending_ops import PendingOperationsStore, verify_pending_operation_signature
from temms.daemon.service import DaemonConfig
from temms.hub_lite import HubLiteStore, canonical_json_hash
from temms.telemetry import TelemetryBuffer
from temms.inference import server as inference_server
from temms.inference.server import create_app
from temms.inference.runtime import InferenceRuntime
from temms.policy.schema import (
    Condition,
    ConditionGroup,
    PolicyAction,
    PolicyRule,
    SlotPolicy,
    SlotPolicyMetadata,
    SlotPolicySpec,
)


def _pending_payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def test_runtime_retarget_proof_text_dedupes_replay_detail():
    proof = inference_server._runtime_retarget_proof_text(
        "model-lowlight DDIL replay retargeted temms-rpi5-tflite -> temms-x86_64-cpu",
        "retargeted temms-rpi5-tflite -> temms-x86_64-cpu",
    )

    assert proof == (
        "model-lowlight DDIL replay retargeted temms-rpi5-tflite -> temms-x86_64-cpu"
    )
    assert inference_server._runtime_retarget_proof_text(
        "model-lowlight activated",
        "retargeted temms-rpi5-tflite -> temms-x86_64-cpu",
    ) == (
        "model-lowlight activated; retargeted temms-rpi5-tflite -> temms-x86_64-cpu"
    )


def test_mission_package_request_payload_derives_missing_fields_from_yaml():
    request = inference_server.MissionPackagePlanRequest(
        model_id="explicit-model",
        mission_yaml="""
schema_version: temms-edge-mission/v1
mission:
  goal: Detect vehicles during DDIL link loss.
  sensor: camera.rgb
  slot: vision
selection:
  package_id: pkg-yaml
  model_id: yaml-model
  device_id: edge-yaml
  runtime_target_id: temms-x86_64-cpu
slo:
  latency_budget_ms: 95
  min_throughput_ips: 25
model_handling:
  switch_policy: condition_and_confidence
  confidence_threshold: 0.65
  fallback_model_id: model-fallback
ddil:
  mode: queue_signed_intents
""",
    )

    payload = inference_server._mission_package_request_payload(request)

    assert payload["model_id"] == "explicit-model"
    assert payload["package_id"] == "pkg-yaml"
    assert payload["device_id"] == "edge-yaml"
    assert payload["runtime_target_id"] == "temms-x86_64-cpu"
    assert payload["goal"] == "Detect vehicles during DDIL link loss."
    assert payload["sensor"] == "camera.rgb"
    assert payload["slot"] == "vision"
    assert payload["latency_budget_ms"] == 95.0
    assert payload["min_throughput_ips"] == 25.0
    assert payload["switch_policy"] == "condition_and_confidence"
    assert payload["confidence_threshold"] == 0.65
    assert payload["fallback_model_id"] == "model-fallback"
    assert payload["ddil_mode"] == "queue_signed_intents"


def test_runtime_retarget_target_proof_rejects_missing_hub_assessments():
    with pytest.raises(HTTPException) as exc:
        inference_server._runtime_retarget_target_proof(
            {"hub_best_runtime_target_id": "gpu-fit"},
            "gpu-fit",
        )

    assert exc.value.status_code == 409
    assert "requires Hub target assessments" in exc.value.detail


@pytest.fixture
def inference_runtime(model_cache, model_storage):
    """Create InferenceRuntime instance."""
    return InferenceRuntime(model_cache, model_storage)


@pytest.fixture
def app(
    slot_manager,
    condition_store,
    policy_engine,
    model_cache,
    model_storage,
    inference_runtime,
):
    """Create FastAPI test app."""
    return create_app(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
        model_storage=model_storage,
        inference_runtime=inference_runtime,
    )


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def hub_client(
    temp_dir,
    slot_manager,
    condition_store,
    policy_engine,
    model_cache,
    model_storage,
    inference_runtime,
):
    """Create test client with Hub Lite configured."""
    hub_app = create_app(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
        model_storage=model_storage,
        inference_runtime=inference_runtime,
        hub_lite=HubLiteStore(temp_dir / "hub_lite.json"),
    )
    return TestClient(hub_app)


@pytest.fixture
def telemetry_client(
    temp_dir,
    slot_manager,
    condition_store,
    policy_engine,
    model_cache,
    model_storage,
    inference_runtime,
):
    """Create test client with telemetry configured."""
    telemetry_app = create_app(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
        model_storage=model_storage,
        inference_runtime=inference_runtime,
        telemetry=TelemetryBuffer(temp_dir / "telemetry.jsonl"),
    )
    return TestClient(telemetry_app)


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


def test_readiness_ddil_action_refs_include_operational_context():
    gate = inference_server._ddil_readiness_gate(
        {
            "runtime": {
                "offline_mode": True,
                "pending_operations_count": 2,
                "pending_operation_types": ["deploy", "override_model"],
                "pending_operation_verification": {"verified": 1, "invalid": 1},
                "pending_operation_preflight": {
                    "ready": 1,
                    "blocked": 1,
                    "superseded": 0,
                },
                "pending_operations": [
                    {"payload_sha256": "sha256:deploy"},
                    {"payload_sha256": "sha256:override"},
                ],
            }
        }
    )

    assert gate["gate_id"] == "ddil_queue"
    assert gate["status"] == "blocked"
    assert gate["refs"]["offline_mode"] is True
    assert gate["refs"]["pending_operations"] == 2
    assert gate["refs"]["pending_operation_types"] == ["deploy", "override_model"]
    assert gate["refs"]["invalid_intents"] == 1
    assert gate["refs"]["replay_blocked_intents"] == 1
    assert gate["refs"]["pending_operation_hashes"] == [
        "sha256:deploy",
        "sha256:override",
    ]
    assert gate["actions"][0]["action_id"] == "quarantine_blocked_ddil"
    assert gate["actions"][0]["refs"] == gate["refs"]
    assert gate["actions"][0]["command"] == {
        "method": "POST",
        "path": "/v1/control/sync/quarantine-blocked",
        "body": {
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate quarantine",
        },
    }
    assert inference_server._readiness_action_command("acknowledge_dead_letters", {}) == {
        "method": "POST",
        "path": "/v1/control/sync/acknowledge-dead-letters",
        "body": {
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate acknowledgement",
        },
    }
    assert inference_server._readiness_action_command("requeue_dead_letters", {}) == {
        "method": "POST",
        "path": "/v1/control/sync/requeue-dead-letters",
        "body": {
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate requeue",
            "require_ready": True,
        },
    }


def test_readiness_ddil_gate_surfaces_runtime_optimization_advisories():
    gate = inference_server._ddil_readiness_gate(
        {
            "runtime": {
                "offline_mode": False,
                "pending_operations_count": 1,
                "pending_operation_verification": {"verified": 1, "invalid": 0},
                "pending_operation_preflight": {
                    "ready": 1,
                    "blocked": 0,
                    "superseded": 0,
                    "optimization_advisories": 1,
                },
                "pending_operations": [
                    {
                        "payload_sha256": "sha256:optimized",
                        "replay_status": "ready_with_runtime_advisory",
                        "runtime_optimizer_detail": (
                            "gpu-fit scores 100/100, 4 points above selected cpu-fit."
                        ),
                        "best_runtime_target_id": "gpu-fit",
                        "runtime_score_delta": 4.0,
                    }
                ],
            }
        }
    )

    assert gate["gate_id"] == "ddil_queue"
    assert gate["status"] == "attention"
    assert gate["state"] == "runtime advisory"
    assert "1 runtime optimization advisory" in gate["detail"]
    assert gate["refs"]["runtime_optimization_advisories"] == 1
    assert gate["refs"]["pending_operation_hashes"] == ["sha256:optimized"]
    assert gate["actions"][0]["kind"] == "sync_pending"


def test_control_sync_retargets_pending_runtime_and_resigns(
    temp_dir,
    slot_manager,
    condition_store,
    policy_engine,
    model_cache,
    model_storage,
    inference_runtime,
    sample_cached_model,
    sample_slot,
):
    pending_store = PendingOperationsStore(temp_dir / "pending_operations_route.json")
    hub = HubLiteStore(temp_dir / "hub_lite_runtime_retarget.json")
    hub.enroll_device(
        "edge-1",
        profile="x86_64-cpu",
        inventory={
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider", "CUDAExecutionProvider"],
                }
            },
            "memory": {"available_mb": 2048.0},
            "storage": {"available_mb": 4096.0},
        },
    )
    hub.upsert_package(
        {
            "package_id": sample_cached_model.package_id,
            "name": "runtime-retarget-package",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "e" * 64,
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {
                        "id": sample_cached_model.id,
                        "format": "onnx",
                        "filename": "test-model.onnx",
                        "runtime_constraints": {"runtimes": ["onnxruntime"]},
                        "performance_slo": {
                            "max_latency_ms_p95": 12.0,
                            "min_throughput_ips": 80.0,
                        },
                        "resource_requirements": {
                            "min_memory_available_mb": 512.0,
                            "min_storage_available_mb": 64.0,
                        },
                    }
                ],
            },
        }
    )
    _release_package(hub, sample_cached_model.package_id)
    for runtime_target_id, providers in {
        "cpu-fit": ["CPUExecutionProvider"],
        "gpu-fit": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    }.items():
        hub.upsert_runtime_target(
            {
                "runtime_target_id": runtime_target_id,
                "image": f"registry.example.com/{runtime_target_id}:latest",
                "device_profiles": ["x86_64-cpu"],
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": providers,
                    }
                },
                "runtime_constraints": {
                    "runtimes": ["onnxruntime"],
                    "preferred_providers": providers,
                },
            }
        )
        hub.record_runtime_validation(
            runtime_target_id,
            {
                "runtime_target_id": runtime_target_id,
                "image": f"registry.example.com/{runtime_target_id}:latest",
                "dry_run": False,
                "exit_code": 0,
                "ok": True,
            },
            package_id=sample_cached_model.package_id,
            actor="operator:test",
        )
    hub.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": sample_cached_model.id,
            "slot": sample_slot.name,
            "latency_ms": {"p95": 10.0},
            "throughput": {"inferences_per_second": 100.0},
        },
        device_id="edge-1",
        package_id=sample_cached_model.package_id,
        runtime_target_id="cpu-fit",
        actor="edge:edge-1",
    )
    hub.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": sample_cached_model.id,
            "slot": sample_slot.name,
            "latency_ms": {"p95": 4.0},
            "throughput": {"inferences_per_second": 230.0},
        },
        device_id="edge-1",
        package_id=sample_cached_model.package_id,
        runtime_target_id="gpu-fit",
        actor="edge:edge-1",
    )
    payload = {
        "actor": "operator:test",
        "source": "route-test",
        "slot": sample_slot.name,
        "model_id": sample_cached_model.id,
        "package_id": sample_cached_model.package_id,
        "device_id": "edge-1",
        "runtime_target_id": "cpu-fit",
    }
    pending_store.enqueue("deploy", payload, signing_key="ddil-secret", signer="operator:queue")
    inference_runtime.load_model = AsyncMock(return_value=True)
    control_app = create_app(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
        model_storage=model_storage,
        inference_runtime=inference_runtime,
        pending_operations=pending_store,
        hub_lite=hub,
        daemon_config=DaemonConfig(
            db_path=temp_dir / "daemon.db",
            model_dir=temp_dir / "daemon-models",
            rollout_signing_key="ddil-secret",
        ),
    )
    client = TestClient(control_app)

    response = client.post(
        "/v1/control/sync/retarget-runtime",
        json={
            "payload_sha256": _pending_payload_hash(payload),
            "runtime_target_id": "gpu-fit",
            "actor": "operator:test",
            "reason": "select measured runtime",
        },
    )

    assert response.status_code == 200, response.text
    result = response.json()
    updated = pending_store.read_all()[0]
    signature = verify_pending_operation_signature(updated, "ddil-secret")
    audit = updated["payload"]["_temms_runtime_retarget"][0]
    after_entry = result["preflight_after"]["entries"][0]
    proof = result["runtime_target_proof"]

    assert result["status"] == "success"
    assert result["previous_runtime_target_id"] == "cpu-fit"
    assert result["runtime_target_id"] == "gpu-fit"
    assert result["updated_payload_sha256"] == _pending_payload_hash(updated["payload"])
    assert updated["payload"]["runtime_target_id"] == "gpu-fit"
    assert audit["actor"] == "operator:test"
    assert audit["previous_runtime_target_id"] == "cpu-fit"
    assert audit["runtime_target_id"] == "gpu-fit"
    assert audit["runtime_target_proof"]["status"] == "proved"
    assert audit["runtime_target_proof"]["runtime_target_id"] == "gpu-fit"
    assert audit["runtime_target_proof"]["best"] is True
    assert audit["runtime_target_proof"]["eligible"] is True
    assert audit["runtime_target_proof"]["runtime_fit_score"] >= 95
    assert audit["runtime_target_proof"]["runtime_validation_id"]
    assert audit["runtime_target_proof"]["benchmark_id"]
    assert audit["runtime_target_proof"]["target_assessment_schema_version"] == (
        "temms-runtime-target-assessment-digest/v1"
    )
    assert len(audit["runtime_target_proof"]["target_assessment_sha256"]) == 64
    assert audit["runtime_target_proof"]["runtime_capability_lock"]["status"] == "locked"
    assert len(audit["runtime_target_proof"]["capability_sha256"]) == 64
    assert audit["runtime_target_proof"]["runtime_workbench_schema_version"] == (
        "temms-runtime-workbench/v1"
    )
    assert audit["runtime_target_proof"][
        "runtime_workbench_selected_runtime_target_id"
    ] == "gpu-fit"
    assert audit["runtime_target_proof"][
        "runtime_workbench_previous_selected_runtime_target_id"
    ] == "cpu-fit"
    assert audit["runtime_target_proof"][
        "runtime_workbench_best_runtime_target_id"
    ] == "gpu-fit"
    assert audit["runtime_target_proof"]["runtime_workbench_selected_is_best"] is True
    assert audit["runtime_target_proof"]["runtime_workbench_target_count"] >= 2
    assert proof == audit["runtime_target_proof"]
    assert signature["verified"] is True
    assert after_entry["runtime_target_id"] == "gpu-fit"
    assert after_entry["signature_status"] == "verified"
    assert after_entry["hub_runtime_workbench_schema_version"] == (
        "temms-runtime-workbench/v1"
    )
    assert after_entry["hub_runtime_workbench_selected_runtime_target_id"] == (
        "gpu-fit"
    )
    assert after_entry["hub_runtime_workbench_selected_is_best"] is True

    sync_response = client.post("/v1/control/sync")
    decision = slot_manager.get_decision_log(sample_slot.name, limit=1)[0]
    decision_audit = json.loads(decision["audit_metadata"])
    replay_retarget = decision_audit["ddil_runtime_retarget"]

    assert sync_response.status_code == 200, sync_response.text
    assert sync_response.json()["replayed"] == 1
    assert pending_store.read_all() == []
    assert replay_retarget["schema_version"] == "temms-ddil-runtime-retarget-audit/v1"
    assert replay_retarget["previous_runtime_target_id"] == "cpu-fit"
    assert replay_retarget["runtime_target_id"] == "gpu-fit"
    assert replay_retarget["actor"] == "operator:test"
    assert replay_retarget["reason"] == "select measured runtime"
    assert replay_retarget["latest"]["previous_payload_sha256"] == _pending_payload_hash(payload)
    assert replay_retarget["latest"]["runtime_target_proof"]["status"] == "proved"
    assert replay_retarget["latest"]["runtime_target_proof"]["benchmark_id"]


def test_readiness_evidence_action_refs_include_replay_context():
    gate = inference_server._evidence_chain_readiness_gate(
        {"counts": {"timeline_entries": 4}, "trust": {"signed_package_imports": 1}},
        {
            "outcome": {
                "completed_phases": 2,
                "incomplete_phases": ["runtime_validation"],
            },
            "phases": [{"phase": "signed_package"}, {"phase": "runtime_validation"}],
        },
    )

    assert gate["gate_id"] == "evidence_chain"
    assert gate["status"] == "attention"
    assert gate["refs"] == {
        "proof_events": 4,
        "signed_package_imports": 1,
        "completed_phases": 2,
        "total_phases": 2,
        "incomplete_phases": ["runtime_validation"],
        "export_mode": "replay",
    }
    assert gate["actions"][0]["action_id"] == "export_mission_replay"
    assert gate["actions"][0]["refs"] == gate["refs"]
    assert gate["actions"][0]["command"] == {
        "method": "POST",
        "path": "/v1/hub/evidence/export",
        "body": {"replay": True, "replay_limit": 50},
    }


class TestHealthEndpoint:
    """Tests for health endpoint."""

    def test_health_returns_ok(self, client):
        """Test health endpoint returns OK."""
        response = client.get("/v1/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "timestamp" in data


class TestStatusEndpoint:
    """Tests for system status endpoint."""

    def test_status_no_slots(self, client):
        """Test status with no slots."""
        response = client.get("/v1/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["slots"] == {}
        assert data["conditions_count"] == 0
        assert data["policies_count"] == 0
        assert data["uptime_seconds"] >= 0

    def test_status_with_slot(self, client, sample_slot):
        """Test status with a slot."""
        response = client.get("/v1/status")

        assert response.status_code == 200
        data = response.json()
        assert "vision" in data["slots"]


class TestSlotStatusEndpoint:
    """Tests for slot status endpoint."""

    def test_slot_status_not_found(self, client):
        """Test slot status for non-existent slot."""
        response = client.get("/v1/slots/nonexistent/status")

        assert response.status_code == 404

    def test_slot_status_exists(self, client, sample_slot):
        """Test slot status for existing slot."""
        response = client.get("/v1/slots/vision/status")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "vision"
        assert data["description"] == "Vision processing slot"
        assert data["required"] is True
        assert data["state"] == "stopped"


class TestInferenceEndpoint:
    """Tests for inference endpoint."""

    def test_infer_slot_not_found(self, client):
        """Test inference on non-existent slot."""
        response = client.post(
            "/v1/slots/nonexistent/infer",
            files={"file": ("test.jpg", b"fake image data", "image/jpeg")},
        )

        assert response.status_code == 404

    def test_infer_slot_not_running(self, client, sample_slot):
        """Test inference on slot that's not running."""
        response = client.post(
            "/v1/slots/vision/infer",
            files={"file": ("test.jpg", b"fake image data", "image/jpeg")},
        )

        assert response.status_code == 503
        assert "not running" in response.json()["detail"]

    def test_infer_runtime_failure_activates_policy_fallback(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test active inference runtime failure hot-swaps to fallback and retries."""
        for model_id, model_name in [
            ("daylight-model-v1", "daylight-model"),
            ("lowlight-model-v1", "lowlight-model"),
        ]:
            model_path = temp_dir / f"{model_id}.onnx"
            model_bytes = f"{model_id}-bytes".encode()
            model_path.write_bytes(model_bytes)
            model_cache.add_cached_model(
                model_id=model_id,
                name=model_name,
                version="1",
                format=ModelFormat.ONNX,
                path=model_path,
                sha256=hashlib.sha256(model_bytes).hexdigest(),
                size_bytes=len(model_bytes),
                package_id="pkg-runtime-fallback",
            )
        slot_manager.create_slot(
            "vision",
            "Vision slot",
            candidates=["daylight-model", "lowlight-model"],
        )
        slot_manager.activate_model("vision", "daylight-model-v1", "startup", "seed")
        policy_engine.load_policy(
            SlotPolicy(
                metadata=SlotPolicyMetadata(name="runtime-fallback-policy"),
                spec=SlotPolicySpec(
                    slot="vision",
                    rules=[
                        PolicyRule(
                            name="placeholder",
                            priority=1,
                            conditions=ConditionGroup(all=[]),
                            action=PolicyAction(switch_to="daylight-model"),
                        )
                    ],
                    fallback_chain=["lowlight-model", "daylight-model"],
                ),
            )
        )

        async def infer_once_then_fallback(slot_name, model_id, input_data, content_type):
            if model_id == "daylight-model-v1":
                raise RuntimeError("runtime crashed")
            return [{"label": "recovered"}]

        inference_runtime.infer = AsyncMock(side_effect=infer_once_then_fallback)
        inference_runtime.load_model = AsyncMock(return_value=True)
        telemetry = TelemetryBuffer(temp_dir / "runtime-fallback-telemetry.jsonl")
        fallback_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            telemetry=telemetry,
        )
        fallback_client = TestClient(fallback_app)

        response = fallback_client.post(
            "/v1/slots/vision/infer",
            files={"file": ("test.jpg", b"fake image data", "image/jpeg")},
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["model"] == "lowlight-model"
        assert payload["predictions"] == [{"label": "recovered"}]
        assert [call.kwargs["model_id"] for call in inference_runtime.infer.await_args_list] == [
            "daylight-model-v1",
            "lowlight-model-v1",
        ]
        inference_runtime.load_model.assert_awaited_once_with("vision", "lowlight-model-v1")
        assert slot_manager.get_slot("vision").active_model_id == "lowlight-model-v1"
        decision = slot_manager.get_decision_log("vision", limit=1)[0]
        assert decision["trigger_type"] == "fallback"
        assert decision["to_model"] == "lowlight-model-v1"
        assert decision["trigger_detail"] == "fallback after runtime inference failure"
        audit = json.loads(decision["audit_metadata"])
        assert audit["fallback"]["selected_model"] == "daylight-model-v1"
        assert audit["fallback"]["failures"][0] == "daylight-model-v1: runtime crashed"
        decision_conditions = json.loads(decision["conditions_snapshot"])
        assert decision_conditions["runtime"]["inference"]["vision"]["healthy"] is False
        assert (
            decision_conditions["runtime"]["inference"]["vision"]["failed_model"]
            == "daylight-model-v1"
        )
        assert condition_store.get("runtime.inference.vision.healthy").value is True
        assert condition_store.get("runtime.inference.vision.last_error").value is None
        assert condition_store.get("runtime.inference.vision.failed_model").value is None
        event_types = [event["event_type"] for event in telemetry.read()]
        assert "inference.failed" in event_types
        assert "inference.fallback" in event_types
        assert "inference.served" in event_types


class TestControlEndpoints:
    """Tests for control endpoints."""


    def test_control_auth_token_when_configured(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test configured API token protects control endpoints."""
        protected_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            api_token="secret-token",
        )
        protected_client = TestClient(protected_app)

        unauthorized = protected_client.post(
            "/v1/control/conditions",
            json={"conditions": {"test.condition": 1}},
        )
        assert unauthorized.status_code == 401

        authorized = protected_client.post(
            "/v1/control/conditions",
            headers={"X-TEMMS-Token": "secret-token"},
            json={"conditions": {"test.condition": 1}},
        )
        assert authorized.status_code == 200

    def test_control_evaluate_respects_hub_activation_preflight(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test adaptive control apply cannot bypass Hub Lite edge admission."""
        for model_id, model_name in [
            ("daylight-model-v1", "daylight-model"),
            ("heavy-model-v1", "heavy-model"),
            ("safe-model-v1", "safe-model"),
        ]:
            model_bytes = f"{model_id}-bytes".encode()
            model_path = temp_dir / f"{model_id}.onnx"
            model_path.write_bytes(model_bytes)
            model_cache.add_cached_model(
                model_id=model_id,
                name=model_name,
                version="1",
                format=ModelFormat.ONNX,
                path=model_path,
                sha256=hashlib.sha256(model_bytes).hexdigest(),
                size_bytes=len(model_bytes),
                package_id="pkg-adaptive",
            )

        slot_manager.create_slot(
            "vision",
            "Vision slot",
            candidates=["daylight-model", "heavy-model", "safe-model"],
        )
        slot_manager.activate_model("vision", "daylight-model-v1", "startup", "seed")
        condition_store.set("mission.mode", "survey", "operator", 100)
        policy_engine.load_policy(
            SlotPolicy(
                metadata=SlotPolicyMetadata(name="adaptive-resource-policy"),
                spec=SlotPolicySpec(
                    slot="vision",
                    rules=[
                        PolicyRule(
                            name="survey-heavy-rule",
                            priority=100,
                            conditions=ConditionGroup(
                                all=[
                                    Condition(
                                        metric="mission.mode",
                                        operator="eq",
                                        value="survey",
                                    )
                                ]
                            ),
                            action=PolicyAction(switch_to="heavy-model"),
                        )
                    ],
                    fallback_chain=["safe-model", "daylight-model"],
                ),
            )
        )
        hub = HubLiteStore(temp_dir / "hub_lite_control_preflight.json")
        hub.enroll_device(
            "edge-1",
            profile="x86_64-cpu",
            inventory={
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CPUExecutionProvider"],
                    }
                },
                "memory": {"available_mb": 256.0},
                "storage": {"available_mb": 2048.0},
            },
        )
        hub.upsert_package(
            {
                "package_id": "pkg-adaptive",
                "name": "adaptive-models",
                "version": "1",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "validation": {
                        "signature_verified": True,
                        "strict_metadata": True,
                    },
                    "models": [
                        {
                            "id": "heavy-model-v1",
                            "runtime_constraints": {"runtimes": ["onnxruntime"]},
                            "resource_requirements": {
                                "min_memory_available_mb": 1024.0,
                                "min_storage_available_mb": 128.0,
                            },
                        },
                        {
                            "id": "safe-model-v1",
                            "runtime_constraints": {"runtimes": ["onnxruntime"]},
                            "resource_requirements": {
                                "min_memory_available_mb": 128.0,
                                "min_storage_available_mb": 64.0,
                            },
                        },
                        {
                            "id": "daylight-model-v1",
                            "runtime_constraints": {"runtimes": ["onnxruntime"]},
                            "resource_requirements": {
                                "min_memory_available_mb": 128.0,
                                "min_storage_available_mb": 64.0,
                            },
                        },
                    ],
                },
            }
        )
        _release_package(hub, "pkg-adaptive")
        telemetry = TelemetryBuffer(temp_dir / "control-preflight-telemetry.jsonl")
        inference_runtime.load_model = AsyncMock(return_value=True)
        control_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            daemon_config=DaemonConfig(
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                hub_device_id="edge-1",
                rollout_require_signature=False,
            ),
            hub_lite=hub,
            telemetry=telemetry,
        )
        control_client = TestClient(control_app)

        response = control_client.post(
            "/v1/control/slots/vision/evaluate",
            json={"apply": True},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "fallback_activated"
        assert body["selected_model"] == "heavy-model-v1"
        assert body["activated_model"] == "safe-model-v1"
        inference_runtime.load_model.assert_awaited_once_with("vision", "safe-model-v1")
        assert slot_manager.get_slot("vision").active_model_id == "safe-model-v1"
        events = telemetry.read()
        preflight_event = next(
            event for event in events if event["event_type"] == "slot.activation_preflight_blocked"
        )
        assert preflight_event["payload"]["model_id"] == "heavy-model-v1"
        assert preflight_event["payload"]["blocking_gates"][0]["gate_id"] == (
            "resource_envelope"
        )

    def test_hub_lite_api_uses_control_auth_token_when_configured(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test Hub Lite API routes use the control token when configured."""
        hub = HubLiteStore(temp_dir / "hub_lite_protected.json")
        protected_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            api_token="secret-token",
            hub_lite=hub,
        )
        protected_client = TestClient(protected_app)

        health = protected_client.get("/v1/health")
        assert health.status_code == 200

        unauthorized_write = protected_client.post(
            "/v1/hub/devices/enroll",
            json={"device_id": "edge-auth", "profile": "x86_64-cpu"},
        )
        assert unauthorized_write.status_code == 401
        assert hub.get_device("edge-auth") is None

        unauthorized_read = protected_client.get("/v1/hub/devices")
        assert unauthorized_read.status_code == 401

        authorized_write = protected_client.post(
            "/v1/hub/devices/enroll",
            headers={"Authorization": "Bearer secret-token"},
            json={"device_id": "edge-auth", "profile": "x86_64-cpu"},
        )
        assert authorized_write.status_code == 200
        assert hub.get_device("edge-auth") is not None

        authorized_read = protected_client.get(
            "/v1/hub/devices",
            headers={"X-TEMMS-Token": "secret-token"},
        )
        assert authorized_read.status_code == 200
        assert authorized_read.json()["devices"][0]["device_id"] == "edge-auth"

    def test_hub_lite_api_enforces_optional_rbac_roles(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test role-scoped tokens gate sensitive Hub actions when configured."""
        hub = HubLiteStore(temp_dir / "hub_lite_rbac.json")
        hub.enroll_device("edge-rbac", profile="x86_64-cpu")
        hub.upsert_package(
            {
                "package_id": "pkg-rbac",
                "name": "rbac-package",
                "version": "1.0.0",
                "device_profiles": ["x86_64-cpu"],
            }
        )
        protected_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
            rbac_token_roles={
                "operator-token": {"operator"},
                "approver-token": {"approver"},
                "edge-token": {"edge"},
                "auditor-token": {"auditor"},
            },
        )
        protected_client = TestClient(protected_app)

        unauthenticated = protected_client.get("/v1/hub/devices")
        assert unauthenticated.status_code == 401

        wrong_role_assign = protected_client.post(
            "/v1/hub/rollouts",
            headers={"X-TEMMS-Token": "approver-token"},
            json={
                "device_id": "edge-rbac",
                "package_id": "pkg-rbac",
                "slot": "vision",
                "rollout_id": "rollout-rbac",
                "require_approval": True,
            },
        )
        assert wrong_role_assign.status_code == 403

        validated = protected_client.post(
            "/v1/hub/packages/pkg-rbac/promote",
            headers={"X-TEMMS-Token": "operator-token"},
            json={"state": "validated", "reason": "runtime validation passed"},
        )
        assert validated.status_code == 200, validated.text
        operator_approval_promotion = protected_client.post(
            "/v1/hub/packages/pkg-rbac/promote",
            headers={"X-TEMMS-Token": "operator-token"},
            json={"state": "approved", "reason": "operator tried package approval"},
        )
        assert operator_approval_promotion.status_code == 403
        approved_promotion = protected_client.post(
            "/v1/hub/packages/pkg-rbac/promote",
            headers={"X-TEMMS-Token": "approver-token"},
            json={
                "state": "approved",
                "actor": "operator:release-approver",
                "reason": "release approved by RBAC approver",
            },
        )
        assert approved_promotion.status_code == 200, approved_promotion.text
        released = protected_client.post(
            "/v1/hub/packages/pkg-rbac/promote",
            headers={"X-TEMMS-Token": "operator-token"},
            json={"state": "released", "reason": "ready for rollout"},
        )
        assert released.status_code == 200, released.text

        assigned = protected_client.post(
            "/v1/hub/rollouts",
            headers={"X-TEMMS-Token": "operator-token"},
            json={
                "device_id": "edge-rbac",
                "package_id": "pkg-rbac",
                "slot": "vision",
                "rollout_id": "rollout-rbac",
                "require_approval": True,
            },
        )
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["approval"]["state"] == "pending"

        operator_approval = protected_client.post(
            "/v1/hub/rollouts/rollout-rbac/approve",
            headers={"X-TEMMS-Token": "operator-token"},
            json={"reason": "operator tried to approve"},
        )
        assert operator_approval.status_code == 403

        edge_status = protected_client.post(
            "/v1/hub/rollouts/rollout-rbac/status",
            headers={"X-TEMMS-Token": "edge-token"},
            json={"state": "downloading", "detail": "edge received rollout"},
        )
        assert edge_status.status_code == 200
        assert edge_status.json()["state"] == "downloading"

        approved = protected_client.post(
            "/v1/hub/rollouts/rollout-rbac/approve",
            headers={"X-TEMMS-Token": "approver-token"},
            json={
                "reason": "mission approved by RBAC approver",
                "actor": "operator:approver",
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["approval"]["approved"] is True
        assert approved.json()["approval"]["actor"] == "operator:approver"

        edge_evidence = protected_client.post(
            "/v1/hub/evidence/export",
            headers={"X-TEMMS-Token": "edge-token"},
            json={},
        )
        assert edge_evidence.status_code == 403
        edge_evidence_list = protected_client.get(
            "/v1/hub/evidence",
            headers={"X-TEMMS-Token": "edge-token"},
        )
        assert edge_evidence_list.status_code == 403
        for audit_path in [
            "/v1/hub/deployment-status",
            "/v1/hub/telemetry",
            "/v1/hub/benchmarks",
            "/v1/hub/runtime-targets/validations",
        ]:
            edge_audit_read = protected_client.get(
                audit_path,
                headers={"X-TEMMS-Token": "edge-token"},
            )
            assert edge_audit_read.status_code == 403

        auditor_evidence = protected_client.post(
            "/v1/hub/evidence/export",
            headers={"X-TEMMS-Token": "auditor-token"},
            json={"summary": True},
        )
        assert auditor_evidence.status_code == 200
        assert auditor_evidence.json()["schema_version"] == "temms-evidence-summary/v1"
        auditor_evidence_list = protected_client.get(
            "/v1/hub/evidence",
            headers={"X-TEMMS-Token": "auditor-token"},
        )
        assert auditor_evidence_list.status_code == 200
        assert auditor_evidence_list.json()["count"] == 0
        for audit_path in [
            "/v1/hub/deployment-status",
            "/v1/hub/telemetry",
            "/v1/hub/benchmarks",
            "/v1/hub/runtime-targets/validations",
        ]:
            auditor_audit_read = protected_client.get(
                audit_path,
                headers={"X-TEMMS-Token": "auditor-token"},
            )
            assert auditor_audit_read.status_code == 200










    def test_hub_runtime_target_api_register_byo_image(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test Hub Lite exposes default and BYO container runtime targets."""
        hub = HubLiteStore(temp_dir / "hub_lite_runtime_targets.json")
        hub_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
        )
        hub_client = TestClient(hub_app)

        defaults = hub_client.get("/v1/hub/runtime-targets")

        assert defaults.status_code == 200
        assert {target["runtime_target_id"] for target in defaults.json()["runtime_targets"]} >= {
            "temms-x86_64-cpu",
            "temms-orin-tensorrt",
        }

        created = hub_client.post(
            "/v1/hub/runtime-targets",
            json={
                "runtime_target_id": "customer-rpi",
                "name": "Customer RPi Runtime",
                "image": "registry.example.com/customer/rpi5-runtime:1.0.0",
                "os": "linux",
                "arch": "arm64",
                "device_profiles": ["rpi5"],
                "runtimes": {"tflite_runtime": {"available": True}},
                "actor": "operator:test",
            },
        )

        assert created.status_code == 200
        assert created.json()["device_profiles"] == ["rpi5-tflite"]
        assert hub.get_runtime_target("customer-rpi")["image"].startswith(
            "registry.example.com/customer"
        )

        recorded = hub_client.post(
            "/v1/hub/runtime-targets/validations",
            json={
                "runtime_target_id": "customer-rpi",
                "package_path": "/tmp/pkg-rpi.temms.tar.zst",
                "result": {
                    "runtime_target_id": "customer-rpi",
                    "image": "registry.example.com/customer/rpi5-runtime:1.0.0",
                    "package_path": "/tmp/pkg-rpi.temms.tar.zst",
                    "command": [
                        "temms",
                        "package",
                        "validate",
                        "/temms-input/package",
                    ],
                    "dry_run": True,
                    "ok": True,
                },
                "actor": "operator:test",
            },
        )

        assert recorded.status_code == 200
        assert recorded.json()["validation_id"].startswith("runtime-validation-")
        assert recorded.json()["actor"] == "operator:test"

        hub.enroll_device("edge-rpi", profile="rpi5-tflite")
        hub.upsert_package(
            {
                "package_id": "pkg-rpi",
                "name": "rpi-package",
                "version": "1.0.0",
                "device_profiles": ["rpi5-tflite"],
                "sha256": "d" * 64,
            }
        )
        benchmark = hub_client.post(
            "/v1/hub/benchmarks",
            json={
                "device_id": "edge-rpi",
                "package_id": "pkg-rpi",
                "runtime_target_id": "customer-rpi",
                "result": {
                    "schema_version": "temms-benchmark/v1",
                    "model_id": "model-rpi",
                    "slot": "vision",
                    "latency_ms": {"p95": 11.0},
                },
                "actor": "edge:edge-rpi",
            },
        )

        assert benchmark.status_code == 200
        assert benchmark.json()["benchmark_id"].startswith("benchmark-")
        assert benchmark.json()["source_sha256"] == "d" * 64
        listed = hub_client.get("/v1/hub/benchmarks", params={"device_id": "edge-rpi"})
        assert listed.status_code == 200
        assert listed.json()["benchmarks"][0]["benchmark_id"] == benchmark.json()["benchmark_id"]


    def test_update_conditions(self, client):
        """Test updating conditions via API."""
        response = client.post(
            "/v1/control/conditions",
            json={
                "conditions": {
                    "platform.compute.cpu_temp_c": 75.5,
                    "environmental.atmospheric.visibility_m": 500,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["updated"]) == 2
        assert "platform.compute.cpu_temp_c" in data["updated"]

    def test_clear_condition_overrides(self, client, condition_store):
        """Test clearing condition overrides."""
        # First set an override
        condition_store.set(
            path="test.condition",
            value=100,
            source="test",
            priority=1000,  # Operator priority
        )

        response = client.delete("/v1/control/conditions/overrides")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["cleared_count"] >= 0

    def test_override_model_slot_not_found(self, client):
        """Test model override on non-existent slot."""
        response = client.post(
            "/v1/control/slots/nonexistent/model",
            json={"model": "test-model", "reason": "testing"},
        )

        assert response.status_code == 404

    def test_override_model_model_not_found(self, client, sample_slot):
        """Test model override with non-existent model."""
        response = client.post(
            "/v1/control/slots/vision/model",
            json={"model": "nonexistent-model", "reason": "testing"},
        )

        assert response.status_code == 404
        assert "Model not found" in response.json()["detail"]

    def test_override_model_respects_hub_activation_preflight(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test operator override cannot bypass Hub Lite edge admission."""
        model_bytes = b"heavy-override-model"
        model_path = temp_dir / "heavy-override.onnx"
        model_path.write_bytes(model_bytes)
        model_cache.add_cached_model(
            model_id="heavy-override-v1",
            name="heavy-override",
            version="1",
            format=ModelFormat.ONNX,
            path=model_path,
            sha256=hashlib.sha256(model_bytes).hexdigest(),
            size_bytes=len(model_bytes),
            package_id="pkg-override",
        )
        slot_manager.create_slot(
            "vision",
            "Vision slot",
            candidates=["heavy-override"],
        )
        hub = HubLiteStore(temp_dir / "hub_lite_override_preflight.json")
        hub.enroll_device(
            "edge-1",
            profile="x86_64-cpu",
            inventory={
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CPUExecutionProvider"],
                    }
                },
                "memory": {"available_mb": 256.0},
                "storage": {"available_mb": 2048.0},
            },
        )
        hub.upsert_package(
            {
                "package_id": "pkg-override",
                "name": "override-models",
                "version": "1",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "validation": {
                        "signature_verified": True,
                        "strict_metadata": True,
                    },
                    "models": [
                        {
                            "id": "heavy-override-v1",
                            "runtime_constraints": {"runtimes": ["onnxruntime"]},
                            "resource_requirements": {
                                "min_memory_available_mb": 1024.0,
                                "min_storage_available_mb": 128.0,
                            },
                        }
                    ],
                },
            }
        )
        _release_package(hub, "pkg-override")
        telemetry = TelemetryBuffer(temp_dir / "override-preflight-telemetry.jsonl")
        inference_runtime.load_model = AsyncMock(return_value=True)
        override_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            daemon_config=DaemonConfig(
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                hub_device_id="edge-1",
            ),
            hub_lite=hub,
            telemetry=telemetry,
        )
        override_client = TestClient(override_app)

        response = override_client.post(
            "/v1/control/slots/vision/model",
            json={"model": "heavy-override", "reason": "operator drill"},
        )

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["message"] == "Activation preflight failed"
        assert detail["trigger_type"] == "operator"
        assert detail["blocking_gates"][0]["gate_id"] == "resource_envelope"
        inference_runtime.load_model.assert_not_awaited()
        assert slot_manager.get_slot("vision").active_model_id is None
        events = telemetry.read()
        preflight_event = next(
            event for event in events if event["event_type"] == "slot.activation_preflight_blocked"
        )
        assert preflight_event["payload"]["trigger_type"] == "operator"
        assert preflight_event["payload"]["model_id"] == "heavy-override-v1"


    def test_override_model_records_activation_preflight_for_safe_model(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test operator override audit includes admission proof when safe."""
        model_bytes = b"safe-override-model"
        model_path = temp_dir / "safe-override.onnx"
        model_path.write_bytes(model_bytes)
        model_cache.add_cached_model(
            model_id="safe-override-v1",
            name="safe-override",
            version="1",
            format=ModelFormat.ONNX,
            path=model_path,
            sha256=hashlib.sha256(model_bytes).hexdigest(),
            size_bytes=len(model_bytes),
            package_id="pkg-override-safe",
        )
        slot_manager.create_slot(
            "vision",
            "Vision slot",
            candidates=["safe-override"],
        )
        hub = HubLiteStore(temp_dir / "hub_lite_override_safe.json")
        hub.enroll_device(
            "edge-1",
            profile="x86_64-cpu",
            inventory={
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CPUExecutionProvider"],
                    }
                },
                "memory": {"available_mb": 256.0},
                "storage": {"available_mb": 2048.0},
            },
        )
        hub.upsert_package(
            {
                "package_id": "pkg-override-safe",
                "name": "override-safe-models",
                "version": "1",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "validation": {
                        "signature_verified": True,
                        "strict_metadata": True,
                    },
                    "models": [
                        {
                            "id": "safe-override-v1",
                            "runtime_constraints": {"runtimes": ["onnxruntime"]},
                            "resource_requirements": {
                                "min_memory_available_mb": 128.0,
                                "min_storage_available_mb": 64.0,
                            },
                        }
                    ],
                },
            }
        )
        _release_package(hub, "pkg-override-safe")
        telemetry = TelemetryBuffer(temp_dir / "override-safe-telemetry.jsonl")
        inference_runtime.load_model = AsyncMock(return_value=True)
        override_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            daemon_config=DaemonConfig(
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                hub_device_id="edge-1",
            ),
            hub_lite=hub,
            telemetry=telemetry,
        )
        override_client = TestClient(override_app)

        response = override_client.post(
            "/v1/control/slots/vision/model",
            json={"model": "safe-override", "reason": "operator drill"},
        )

        assert response.status_code == 200, response.text
        inference_runtime.load_model.assert_awaited_once_with("vision", "safe-override-v1")
        assert slot_manager.get_slot("vision").active_model_id == "safe-override-v1"
        decision = slot_manager.get_decision_log("vision", limit=1)[0]
        audit = json.loads(decision["audit_metadata"])
        assert audit["activation_preflight"]["selection"]["device_id"] == "edge-1"
        assert audit["activation_preflight"]["selection"]["model_id"] == "safe-override-v1"
        override_event = next(
            event for event in telemetry.read() if event["event_type"] == "slot.override"
        )
        assert override_event["payload"]["model"]["activation_preflight"]["selection"][
            "model_id"
        ] == "safe-override-v1"

    def test_telemetry_export_replay_and_clear(self, telemetry_client):
        """Test control-plane telemetry buffering endpoints."""
        update = telemetry_client.post(
            "/v1/control/conditions",
            json={"conditions": {"platform.compute.cpu_temp_c": 72}},
        )
        assert update.status_code == 200

        exported = telemetry_client.post("/v1/control/telemetry/export", json={})
        assert exported.status_code == 200
        bundle = exported.json()
        assert bundle["schema_version"] == "temms-telemetry-bundle/v1"
        assert bundle["count"] == 1
        assert bundle["events"][0]["event_type"] == "conditions.updated"

        replayed = telemetry_client.post(
            "/v1/control/telemetry/replay",
            json={"clear": True},
        )
        assert replayed.status_code == 200
        assert replayed.json()["replayed"] == 1

        empty = telemetry_client.post("/v1/control/telemetry/export", json={})
        assert empty.status_code == 200
        assert empty.json()["count"] == 0

    def test_audit_timeline_merges_decisions_rollouts_and_telemetry(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test audit timeline includes decision, rollout, and telemetry events."""
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        slot_manager.activate_model(
            "vision",
            "model-a",
            "policy",
            "thermal-adaptive",
            {"platform": {"compute": {"cpu_temp_c": 72}}},
        )
        telemetry = TelemetryBuffer(temp_dir / "audit-telemetry.jsonl")
        telemetry.append("slot.model_switched", {"slot": "vision"}, source="daemon")
        hub = HubLiteStore(temp_dir / "hub_lite_audit.json")
        hub.enroll_device("edge-1", profile="x86_64-cpu")
        hub.upsert_package(
            {
                "package_id": "pkg-audit",
                "name": "audit-package",
                "version": "1.0.0",
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(hub, "pkg-audit")
        hub.assign_rollout(
            "edge-1",
            "pkg-audit",
            slot="vision",
            rollout_id="rollout-audit",
            actor="operator:alice",
        )
        model_cache.add_package(
            package_id="pkg-audit",
            name="audit-package",
            version="1.0.0",
            source="/tmp/pkg-audit.temms.tar.zst",
            manifest={
                "schema_version": "v1",
                "package_id": "pkg-audit",
                "name": "audit-package",
                "version": "1.0.0",
                "policies": [{"name": "vision", "filename": "vision.yaml", "slot": "vision"}],
                "_temms_import": {
                    "schema_version": "temms-import-audit/v1",
                    "imported_at": "2026-01-01T00:00:03Z",
                    "source": "/tmp/pkg-audit.temms.tar.zst",
                    "source_type": "archive",
                    "source_sha256": "e" * 64,
                    "hashes_verified": True,
                    "signature_required": True,
                    "signature_verified": True,
                    "signature": {
                        "schema_version": "temms-signature/v1",
                        "algorithm": "HMAC-SHA256",
                        "signer": "temms-hub-lite",
                        "key_fingerprint": "sha256:test",
                    },
                    "warnings": [],
                },
            },
        )
        audit_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            telemetry=telemetry,
            hub_lite=hub,
        )
        audit_client = TestClient(audit_app)

        response = audit_client.get("/v1/control/audit/timeline?slot=vision")

        assert response.status_code == 200
        data = response.json()
        assert data["schema_version"] == "temms-audit-timeline/v1"
        assert data["count"] == 4
        assert {entry["kind"] for entry in data["timeline"]} == {
            "decision",
            "package_import",
            "rollout",
            "telemetry",
        }
        rollout_entry = next(entry for entry in data["timeline"] if entry["kind"] == "rollout")
        assert rollout_entry["record"]["actor"] == "operator:alice"
        assert rollout_entry["record"]["rollout_id"] == "rollout-audit"
        import_entry = next(
            entry for entry in data["timeline"] if entry["kind"] == "package_import"
        )
        assert import_entry["record"]["package_id"] == "pkg-audit"
        assert import_entry["record"]["signature_verified"] is True
        assert import_entry["record"]["signature"]["signer"] == "temms-hub-lite"

    def test_rollback_slot_to_previous_model(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
        sample_model_file,
    ):
        """Test slot rollback uses previous model from decision log."""
        dest_path, sha256, size = model_storage.store_model(
            sample_model_file,
            "test-model-v1",
            verify=True,
        )
        model_cache.add_cached_model(
            model_id="test-model-v1",
            name="test-model",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=dest_path,
            sha256=sha256,
            size_bytes=size,
            package_id="test-package",
        )
        model_cache.add_cached_model(
            model_id="test-model-v2",
            name="test-model",
            version="2.0.0",
            format=ModelFormat.ONNX,
            path=dest_path,
            sha256=sha256,
            size_bytes=size,
            package_id="test-package",
        )
        slot_manager.create_slot(
            name="vision",
            description="Vision",
            required=True,
        )
        slot_manager.activate_model("vision", "test-model-v1", "test", "initial")
        slot_manager.activate_model("vision", "test-model-v2", "test", "upgrade")
        inference_runtime.load_model = AsyncMock(return_value=True)

        rollback_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
        )
        rollback_client = TestClient(rollback_app)

        response = rollback_client.post("/v1/control/slots/vision/rollback")

        assert response.status_code == 200
        assert response.json()["model"] == "test-model-v1"
        inference_runtime.load_model.assert_awaited_once_with("vision", "test-model-v1")


class TestHubLiteEndpoints:
    """Tests for Hub Lite MVP endpoints."""

    def test_enrollment_catalog_rollout_status_and_airgap(self, hub_client):
        """Test Hub Lite enrollment, inventory, catalog, rollout, status, and air-gap."""
        enrolled = hub_client.post(
            "/v1/hub/devices/enroll",
            json={
                "device_id": "edge-1",
                "profile": "x86_64-cpu",
                "labels": {"site": "lab"},
                "inventory": {"python": "3.11"},
            },
        )
        assert enrolled.status_code == 200
        assert enrolled.json()["device_id"] == "edge-1"
        second_enrolled = hub_client.post(
            "/v1/hub/devices/enroll",
            json={
                "device_id": "edge-2",
                "profile": "x86_64-cpu",
                "labels": {"site": "lab"},
                "inventory": {"runtime": "onnx"},
            },
        )
        assert second_enrolled.status_code == 200

        heartbeat = hub_client.post(
            "/v1/hub/devices/edge-1/heartbeat",
            json={
                "status": "online",
                "inventory": {"runtime": "onnx"},
                "deployment_status": {"state": "READY"},
            },
        )
        assert heartbeat.status_code == 200

        package = hub_client.post(
            "/v1/hub/packages",
            headers={"X-TEMMS-Actor": "operator:catalog"},
            json={
                "package_id": "pkg-vision-1",
                "name": "vision",
                "version": "1.0.0",
                "path": "/packages/pkg-vision-1.temms",
                "device_profiles": ["x86_64-cpu"],
            },
        )
        assert package.status_code == 200
        assert package.json()["created_by"] == "operator:catalog"
        assert package.json()["updated_by"] == "operator:catalog"
        assert package.json()["metadata"]["audit"]["catalog_actor"] == "operator:catalog"
        _api_release_package(hub_client, "pkg-vision-1")

        rollout = hub_client.post(
            "/v1/hub/rollouts",
            headers={"X-TEMMS-Actor": "operator:alice"},
            json={
                "rollout_id": "rollout-1",
                "device_id": "edge-1",
                "package_id": "pkg-vision-1",
                "slot": "vision",
            },
        )
        assert rollout.status_code == 200
        assert rollout.json()["state"] == "assigned"
        assert rollout.json()["history"][-1]["actor"] == "operator:alice"

        plan = hub_client.post(
            "/v1/hub/rollout-plans",
            headers={"X-TEMMS-Actor": "operator:planner"},
            json={
                "plan_id": "plan-vision",
                "package_id": "pkg-vision-1",
                "device_ids": ["edge-1", "edge-2"],
                "slot": "vision",
                "batch_size": 1,
                "require_approval": True,
            },
        )
        assert plan.status_code == 200, plan.text
        assert plan.json()["state"] == "ready"
        assert plan.json()["counts"]["pending"] == 2

        advanced_plan = hub_client.post(
            "/v1/hub/rollout-plans/plan-vision/advance",
            json={"actor": "operator:planner"},
        )
        assert advanced_plan.status_code == 200, advanced_plan.text
        assert advanced_plan.json()["counts"]["assigned"] == 1
        assert advanced_plan.json()["counts"]["pending"] == 1
        assert advanced_plan.json()["targets"][0]["rollout_id"] == "plan-vision-b1-1"

        plans = hub_client.get("/v1/hub/rollout-plans")
        assert plans.status_code == 200
        assert plans.json()["count"] == 1
        assert plans.json()["rollout_plans"][0]["plan_id"] == "plan-vision"

        paused_plan = hub_client.post(
            "/v1/hub/rollout-plans/plan-vision/pause",
            json={"reason": "canary hold", "actor": "operator:planner"},
        )
        assert paused_plan.status_code == 200
        assert paused_plan.json()["state"] == "paused"
        resumed_plan = hub_client.post(
            "/v1/hub/rollout-plans/plan-vision/resume",
            json={"reason": "canary healthy", "actor": "operator:planner"},
        )
        assert resumed_plan.status_code == 200
        assert resumed_plan.json()["state"] == "ready"

        incompatible = hub_client.post(
            "/v1/hub/packages",
            json={
                "package_id": "pkg-orin",
                "name": "vision-orin",
                "version": "1.0.0",
                "device_profiles": ["orin-tensorrt"],
            },
        )
        assert incompatible.status_code == 200
        _api_release_package(hub_client, "pkg-orin")
        incompatible_rollout = hub_client.post(
            "/v1/hub/rollouts",
            json={
                "device_id": "edge-1",
                "package_id": "pkg-orin",
                "slot": "vision",
            },
        )
        assert incompatible_rollout.status_code == 400
        assert "not compatible" in incompatible_rollout.json()["detail"]

        updated = hub_client.post(
            "/v1/hub/rollouts/rollout-1/status",
            json={
                "state": "activated",
                "detail": "loaded on edge-1",
                "actor": "edge:edge-1",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["state"] == "activated"
        assert updated.json()["history"][-1]["actor"] == "edge:edge-1"

        status = hub_client.get("/v1/hub/deployment-status")
        assert status.status_code == 200
        assert status.json()["deployment_status"]["edge-1"]["state"] == "READY"

        bundle = hub_client.post("/v1/hub/airgap/export")
        assert bundle.status_code == 200
        assert bundle.json()["schema_version"] == "temms-hub-lite-bundle/v1"

        imported = hub_client.post("/v1/hub/airgap/import", json=bundle.json())
        assert imported.status_code == 200
        assert imported.json()["imported"]["devices"] == 2

    def test_telemetry_bundle_replay_is_idempotent(self, hub_client):
        """Test Hub Lite ingests exported edge telemetry bundles after a mission."""
        enrolled = hub_client.post(
            "/v1/hub/devices/enroll",
            json={"device_id": "edge-telemetry", "profile": "x86_64-cpu"},
        )
        assert enrolled.status_code == 200
        bundle = {
            "schema_version": "temms-telemetry-bundle/v1",
            "exported_at": "2026-01-01T00:00:00Z",
            "events": [
                {
                    "event_id": "evt-1",
                    "event_type": "rollout.activated",
                    "source": "daemon",
                    "timestamp": "2026-01-01T00:01:00Z",
                    "payload": {"rollout_id": "rollout-1", "device_id": "edge-telemetry"},
                }
            ],
            "count": 1,
        }

        replayed = hub_client.post(
            "/v1/hub/telemetry/replay",
            headers={"X-TEMMS-Actor": "operator:alice"},
            json={"device_id": "edge-telemetry", "bundle": bundle},
        )

        assert replayed.status_code == 200
        replay = replayed.json()["replay"]
        assert replay["ingested"] == 1
        assert replay["duplicates"] == 0
        assert replay["actor"] == "operator:alice"

        duplicate = hub_client.post(
            "/v1/hub/telemetry/replay",
            json={"device_id": "edge-telemetry", "bundle": bundle},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["replay"]["ingested"] == 0
        assert duplicate.json()["replay"]["duplicates"] == 1

        listed = hub_client.get("/v1/hub/telemetry")
        assert listed.status_code == 200
        assert listed.json()["count"] == 1
        assert listed.json()["events"][0]["event_id"] == "evt-1"
        assert listed.json()["events"][0]["device_id"] == "edge-telemetry"

        status = hub_client.get("/v1/hub/deployment-status")
        assert status.status_code == 200
        assert status.json()["deployment_status"]["edge-telemetry"]["state"] == (
            "telemetry_replayed"
        )
        assert len(status.json()["telemetry_replays"]) == 2

    def test_rollout_assignment_accepts_device_profile_alias(self, hub_client):
        """Test Hub Lite normalizes profile aliases before compatibility checks."""
        enrolled = hub_client.post(
            "/v1/hub/devices/enroll",
            json={"device_id": "edge-alias", "profile": "amd64-cpu"},
        )
        assert enrolled.status_code == 200
        assert enrolled.json()["profile"] == "x86_64-cpu"

        package = hub_client.post(
            "/v1/hub/packages",
            json={
                "package_id": "pkg-alias",
                "name": "alias-package",
                "version": "1.0.0",
                "device_profiles": ["x86_64-cpu"],
            },
        )
        assert package.status_code == 200
        _api_release_package(hub_client, "pkg-alias")

        rollout = hub_client.post(
            "/v1/hub/rollouts",
            json={
                "device_id": "edge-alias",
                "package_id": "pkg-alias",
                "slot": "vision",
            },
        )
        assert rollout.status_code == 200
        assert rollout.json()["state"] == "assigned"

    def test_rollout_assignment_checks_runtime_inventory(self, hub_client):
        """Test Hub Lite rejects assignments that device inventory cannot run."""
        enrolled = hub_client.post(
            "/v1/hub/devices/enroll",
            json={
                "device_id": "edge-runtime",
                "profile": "x86_64-cpu",
                "inventory": {
                    "device_profile": "x86_64-cpu",
                    "runtimes": {"tflite_runtime": {"available": False}},
                    "accelerators": {},
                },
            },
        )
        assert enrolled.status_code == 200
        package = hub_client.post(
            "/v1/hub/packages",
            json={
                "package_id": "pkg-tflite",
                "name": "vision-tflite",
                "version": "1.0.0",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "models": [
                        {
                            "id": "model-tflite",
                            "runtime_constraints": {"runtimes": ["tflite"]},
                        }
                    ]
                },
            },
        )
        assert package.status_code == 200
        _api_release_package(hub_client, "pkg-tflite")

        rejected = hub_client.post(
            "/v1/hub/rollouts",
            json={
                "device_id": "edge-runtime",
                "package_id": "pkg-tflite",
                "slot": "vision",
            },
        )
        assert rejected.status_code == 400
        assert "runtime constraints" in rejected.json()["detail"]
        assert "missing runtimes: tflite" in rejected.json()["detail"]

        heartbeat = hub_client.post(
            "/v1/hub/devices/edge-runtime/heartbeat",
            json={
                "inventory": {
                    "device_profile": "x86_64-cpu",
                    "runtimes": {"tflite_runtime": {"available": True}},
                    "accelerators": {},
                },
            },
        )
        assert heartbeat.status_code == 200
        accepted = hub_client.post(
            "/v1/hub/rollouts",
            json={
                "device_id": "edge-runtime",
                "package_id": "pkg-tflite",
                "slot": "vision",
                "rollout_id": "rollout-tflite",
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["state"] == "assigned"

    def test_register_package_from_artifact(self, hub_client, temp_dir):
        """Test deriving a Hub Lite catalog entry from a package artifact."""
        pkg = temp_dir / "pkg-register.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_bytes = b"fake-register-model"
        model_file.write_bytes(model_bytes)
        model_sha = hashlib.sha256(model_bytes).hexdigest()

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-register",
            "name": "registered-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
            "models": [
                {
                    "id": "model-register-1",
                    "name": "model-register",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": model_sha,
                    "size_bytes": len(model_bytes),
                    "input_schema": {"shape": [1, 3, 224, 224]},
                    "output_schema": {"shape": [1, 1000]},
                    "runtime_constraints": {"runtimes": ["onnxruntime"]},
                    "benchmark": {
                        "available": False,
                        "_source": {"type": "unit-test", "metrics_sha256": "none"},
                    },
                    "provenance": {
                        "source": "unit-test",
                        "run_id": "run-register",
                        "artifact_sha256": model_sha,
                    },
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))
        sign_package(pkg, "secret")

        response = hub_client.post(
            "/v1/hub/packages/register",
            headers={"X-TEMMS-Actor": "operator:alice"},
            json={
                "package_path": str(pkg),
                "require_signature": True,
                "signing_key": "secret",
            },
        )

        assert response.status_code == 200
        entry = response.json()
        assert entry["package_id"] == "pkg-register"
        assert entry["device_profiles"] == ["x86_64-cpu"]
        assert entry["sha256"]
        assert entry["source_sha256"] == entry["sha256"]
        assert entry["metadata"]["source"]["type"] == "directory"
        assert entry["metadata"]["source"]["sha256"] == entry["sha256"]
        assert entry["metadata"]["validation"]["signature_verified"] is True
        assert entry["metadata"]["validation"]["strict_metadata"] is True
        assert entry["metadata"]["models"][0]["id"] == "model-register-1"
        assert entry["created_by"] == "operator:alice"
        assert entry["updated_by"] == "operator:alice"
        assert entry["metadata"]["audit"]["catalog_actor"] == "operator:alice"
        assert entry["metadata"]["audit"]["cataloged_at"]

        listed = hub_client.get("/v1/hub/packages")
        assert listed.status_code == 200
        assert listed.json()["packages"][0]["package_id"] == "pkg-register"

        artifact = hub_client.get("/v1/hub/packages/pkg-register/artifact")
        assert artifact.status_code == 200
        assert artifact.headers["x-temms-package-filename"].endswith(".temms.tar.zst")
        assert artifact.headers["x-temms-package-source-sha256"] == entry["source_sha256"]
        assert artifact.headers["x-temms-package-artifact-sha256"] == (
            artifact.headers["x-temms-package-sha256"]
        )
        assert (
            hashlib.sha256(artifact.content).hexdigest()
            == artifact.headers["x-temms-package-sha256"]
        )

        bundle = hub_client.post(
            "/v1/hub/airgap/export",
            json={"include_packages": True},
        )
        assert bundle.status_code == 200
        payload = bundle.json()
        assert "pkg-register" in payload["package_artifacts"]
        assert (
            payload["package_artifacts"]["pkg-register"]["source_sha256"] == entry["source_sha256"]
        )

        imported_hub = HubLiteStore(temp_dir / "imported_hub.json")
        counts = imported_hub.import_bundle(payload)
        assert counts["package_artifacts"] == 1
        imported_package = imported_hub.get_package("pkg-register")
        assert imported_package is not None
        assert imported_package["source_sha256"] == entry["source_sha256"]
        assert Path(imported_package["path"]).exists()
        assert imported_package["metadata"]["airgap_artifact"]["sha256"]
        assert (
            imported_package["metadata"]["airgap_artifact"]["source_sha256"]
            == entry["source_sha256"]
        )

        model_file.write_bytes(b"mutated-after-registration")
        drifted_artifact = hub_client.get("/v1/hub/packages/pkg-register/artifact")
        assert drifted_artifact.status_code == 409
        assert "changed after registration" in drifted_artifact.json()["detail"]

        drifted_bundle = hub_client.post(
            "/v1/hub/airgap/export",
            json={"include_packages": True},
        )
        assert drifted_bundle.status_code == 409
        assert "changed after registration" in drifted_bundle.json()["detail"]

    def test_register_package_requires_strict_metadata_by_default(self, hub_client, temp_dir):
        """Test Hub registration rejects lab-thin packages unless explicitly allowed."""
        pkg = temp_dir / "pkg-lab-thin.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_bytes = b"fake-lab-thin-model"
        (models / "model.onnx").write_bytes(model_bytes)
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-lab-thin",
            "name": "lab-thin-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-lab-thin-1",
                    "name": "model-lab-thin",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                    "size_bytes": len(model_bytes),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        rejected = hub_client.post(
            "/v1/hub/packages/register",
            json={"package_path": str(pkg)},
        )

        assert rejected.status_code == 400
        assert "Model metadata incomplete" in rejected.json()["detail"]

        accepted = hub_client.post(
            "/v1/hub/packages/register",
            json={"package_path": str(pkg), "strict_metadata": False},
        )

        assert accepted.status_code == 200
        assert accepted.json()["metadata"]["validation"]["strict_metadata"] is False

    def test_package_from_mlflow_builds_signs_and_registers(
        self,
        temp_dir,
        monkeypatch,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test Hub Lite can package a registry model and catalog the signed artifact."""
        artifact_dir = temp_dir / "mlflow-artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "model.onnx").write_bytes(b"hub-mlflow-model")
        signing_key_file = temp_dir / "hub-signing.key"
        signing_key_file.write_text("hub-secret", encoding="utf-8")

        class FakeClient:
            def get_model_version(self, name, version):
                assert name == "detector"
                assert version == "7"
                return SimpleNamespace(
                    version=version,
                    run_id="run-hub-mlflow",
                    source="s3://mlflow-artifacts/detector/7",
                    aliases=["champion"],
                )

            def get_run(self, run_id):
                return SimpleNamespace(
                    info=SimpleNamespace(
                        run_id=run_id,
                        artifact_uri="s3://mlflow-artifacts/run-hub-mlflow/artifacts",
                    ),
                    data=SimpleNamespace(
                        params={
                            "input_schema": '{"shape":[1,3,224,224]}',
                            "output_schema": '{"shape":[1,1000]}',
                            "runtime_constraints": '{"runtimes":["onnx"]}',
                        },
                        metrics={"avg_latency_ms": 5.5},
                        tags={"mlflow.runName": "hub-package"},
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

        hub = HubLiteStore(temp_dir / "hub_lite_from_mlflow.json")
        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            daemon_config=DaemonConfig(
                model_dir=temp_dir / "edge-models",
                policy_dir=temp_dir / "policies",
                rollout_signing_key_file=signing_key_file,
            ),
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/hub/packages/from-mlflow",
            headers={"X-TEMMS-Actor": "operator:mlops"},
            json={
                "model_uri": "models:/detector/7",
                "slot": "vision",
                "tracking_uri": "http://mlflow.example:5000",
                "device_profile": "x86_64-cpu",
                "runtime_options": {"providers": ["CPUExecutionProvider"]},
                "archive": True,
            },
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["signed"] is True
        package_path = Path(payload["package_path"])
        assert package_path == temp_dir / "packages" / "mlflow-detector-7.temms.tar.zst"
        assert package_path.exists()

        entry = payload["package"]
        assert entry["package_id"] == "mlflow-detector-7"
        assert entry["device_profiles"] == ["x86_64-cpu"]
        assert entry["created_by"] == "operator:mlops"
        assert entry["metadata"]["validation"]["signature_verified"] is True
        assert entry["metadata"]["validation"]["strict_metadata"] is True
        assert entry["metadata"]["validation"]["signature"]["signer"] == "temms-hub-lite"
        assert entry["metadata"]["provenance"]["run_id"] == "run-hub-mlflow"
        assert entry["metadata"]["models"][0]["benchmark"]["latency_ms"] == 5.5
        assert entry["metadata"]["models"][0]["runtime_options"]["providers"] == [
            "CPUExecutionProvider"
        ]
        assert hub.get_package("mlflow-detector-7")["path"] == str(package_path.resolve())

    def test_register_package_uses_daemon_signature_defaults(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test package registration inherits daemon signature policy."""
        signing_key = "hub-signing-key"
        signing_key_file = temp_dir / "hub-signing.key"
        signing_key_file.write_text(signing_key)

        pkg = temp_dir / "pkg-register-default.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_bytes = b"register-default-model"
        (models / "model.onnx").write_bytes(model_bytes)
        model_sha = hashlib.sha256(model_bytes).hexdigest()
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-register-default",
            "name": "registered-default-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
            "models": [
                {
                    "id": "model-register-default-1",
                    "name": "model-register-default",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": model_sha,
                    "size_bytes": len(model_bytes),
                    "input_schema": {"shape": [1, 3, 224, 224]},
                    "output_schema": {"shape": [1, 1000]},
                    "runtime_constraints": {"runtimes": ["onnxruntime"]},
                    "benchmark": {
                        "available": False,
                        "_source": {"type": "unit-test", "metrics_sha256": "none"},
                    },
                    "provenance": {
                        "source": "unit-test",
                        "run_id": "run-register-default",
                        "artifact_sha256": model_sha,
                    },
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))
        archive = create_package_archive(pkg)

        hub = HubLiteStore(temp_dir / "hub_lite_register_default.json")
        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            daemon_config=DaemonConfig(
                model_dir=temp_dir / "edge-models",
                policy_dir=temp_dir / "policies",
                rollout_signing_key_file=signing_key_file,
            ),
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/hub/packages/register",
            json={"package_path": str(archive)},
        )

        assert response.status_code == 200
        entry = response.json()
        assert entry["metadata"]["validation"]["signature_verified"] is True
        assert entry["metadata"]["validation"]["strict_metadata"] is True
        assert entry["metadata"]["validation"]["signature"]["signer"] == "temms-hub-lite"
        _api_release_package(client, "pkg-register-default")

        enrolled = client.post(
            "/v1/hub/devices/enroll",
            json={
                "device_id": "edge-1",
                "profile": "x86_64-cpu",
                "inventory": {"runtimes": {"onnxruntime": {"available": True}}},
            },
        )
        assert enrolled.status_code == 200
        rollout = client.post(
            "/v1/hub/rollouts",
            json={
                "device_id": "edge-1",
                "package_id": "pkg-register-default",
                "slot": "vision",
            },
        )
        assert rollout.status_code == 200

    def test_manual_catalog_rejects_unverified_package_when_daemon_requires_signatures(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test daemon signature policy blocks unverified manual catalog entries."""
        hub = HubLiteStore(temp_dir / "hub_lite_unsigned_assign.json")
        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            daemon_config=DaemonConfig(
                model_dir=temp_dir / "edge-models",
                policy_dir=temp_dir / "policies",
                rollout_signing_key="hub-signing-key",
            ),
            hub_lite=hub,
        )
        client = TestClient(app)
        assert (
            client.post(
                "/v1/hub/devices/enroll",
                json={"device_id": "edge-1", "profile": "x86_64-cpu"},
            ).status_code
            == 200
        )
        response = client.post(
            "/v1/hub/packages",
            json={
                "package_id": "pkg-unsigned",
                "name": "unsigned-package",
                "version": "1.0.0",
                "device_profiles": ["x86_64-cpu"],
            },
        )

        assert response.status_code == 400
        assert "verified signature" in response.json()["detail"]
        assert "packages/register" in response.json()["detail"]

    def test_manual_catalog_accepts_verified_metadata_when_daemon_requires_signatures(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test artifact-derived package metadata can still be cataloged manually."""
        hub = HubLiteStore(temp_dir / "hub_lite_verified_manual.json")
        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            daemon_config=DaemonConfig(
                model_dir=temp_dir / "edge-models",
                policy_dir=temp_dir / "policies",
                rollout_signing_key="hub-signing-key",
            ),
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/hub/packages",
            json={
                "package_id": "pkg-signed",
                "name": "signed-package",
                "version": "1.0.0",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "validation": {
                        "signature_verified": True,
                        "strict_metadata": True,
                        "signature": {"signer": "temms-hub-lite"},
                    }
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["package_id"] == "pkg-signed"

    def test_apply_rollout_imports_package_and_activates_slot(
        self,
        temp_dir,
        monkeypatch,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test applying a rollout imports the package and activates a model."""
        pkg = temp_dir / "pkg-apply.temms"
        models_dir = pkg / "models"
        models_dir.mkdir(parents=True)
        model_bytes = b"fake-rollout-model"
        model_file = models_dir / "model.onnx"
        model_file.write_bytes(model_bytes)
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-apply",
            "name": "apply-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-apply-001",
                    "name": "model-apply",
                    "version": "1.0.0",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                    "size_bytes": len(model_bytes),
                }
            ],
            "policies": [],
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))
        archive = create_package_archive(pkg)

        hub = HubLiteStore(temp_dir / "hub_lite_apply.json")
        hub.enroll_device("edge-1", profile="x86_64-cpu")
        hub.upsert_package(
            {
                "package_id": "pkg-apply",
                "name": "apply-package",
                "version": "1.0.0",
                "path": str(archive),
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(hub, "pkg-apply")
        hub.assign_rollout(
            "edge-1",
            "pkg-apply",
            slot="vision",
            rollout_id="rollout-apply",
            require_approval=True,
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        inference_runtime.load_model = AsyncMock(return_value=True)
        importer_kwargs = {}

        from temms.core import package as package_module

        original_importer = package_module.PackageImporter

        class RecordingPackageImporter(original_importer):
            def __init__(self, *args, **kwargs):
                importer_kwargs.update(kwargs)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(package_module, "PackageImporter", RecordingPackageImporter)

        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/hub/rollouts/rollout-apply/apply",
            headers={"X-TEMMS-Actor": "edge:edge-1"},
            json={},
        )

        assert response.status_code == 409
        assert "requires approval" in response.json()["detail"]
        inference_runtime.load_model.assert_not_awaited()

        approval = client.post(
            "/v1/hub/rollouts/rollout-apply/approve",
            headers={"X-TEMMS-Actor": "operator:approver"},
            json={"reason": "policy approved for mission"},
        )

        assert approval.status_code == 200
        assert approval.json()["approval"]["approved"] is True
        assert approval.json()["approval"]["actor"] == "operator:approver"

        response = client.post(
            "/v1/hub/rollouts/rollout-apply/apply",
            headers={"X-TEMMS-Actor": "edge:edge-1"},
            json={},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "activated"
        assert response.json()["model"] == "model-apply-001"
        assert hub.get_rollout("rollout-apply")["state"] == "activated"
        assert hub.get_rollout("rollout-apply")["history"][-1]["actor"] == "edge:edge-1"
        assert model_cache.get_model("model-apply-001") is not None
        assert importer_kwargs["check_runtime_constraints"] is False
        inference_runtime.load_model.assert_awaited_once_with("vision", "model-apply-001")

    def test_apply_rollout_defaults_to_rollout_model_id(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """A model-specific rollout should activate that model without an apply override."""
        pkg = temp_dir / "pkg-apply-selected.temms"
        models_dir = pkg / "models"
        models_dir.mkdir(parents=True)
        daylight_bytes = b"fake-daylight-model"
        lowlight_bytes = b"fake-lowlight-model"
        (models_dir / "daylight.onnx").write_bytes(daylight_bytes)
        (models_dir / "lowlight.onnx").write_bytes(lowlight_bytes)
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-apply-selected",
            "name": "apply-selected-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-daylight-001",
                    "name": "daylight",
                    "version": "1.0.0",
                    "format": "onnx",
                    "filename": "daylight.onnx",
                    "sha256": hashlib.sha256(daylight_bytes).hexdigest(),
                    "size_bytes": len(daylight_bytes),
                },
                {
                    "id": "model-lowlight-001",
                    "name": "lowlight",
                    "version": "1.0.0",
                    "format": "onnx",
                    "filename": "lowlight.onnx",
                    "sha256": hashlib.sha256(lowlight_bytes).hexdigest(),
                    "size_bytes": len(lowlight_bytes),
                },
            ],
            "policies": [],
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))
        archive = create_package_archive(pkg)

        hub = HubLiteStore(temp_dir / "hub_lite_apply_selected.json")
        hub.enroll_device("edge-1", profile="x86_64-cpu")
        hub.upsert_package(
            {
                "package_id": "pkg-apply-selected",
                "name": "apply-selected-package",
                "version": "1.0.0",
                "path": str(archive),
                "device_profiles": ["x86_64-cpu"],
                "metadata": {"models": manifest["models"]},
            }
        )
        _release_package(hub, "pkg-apply-selected")
        hub.assign_rollout(
            "edge-1",
            "pkg-apply-selected",
            slot="vision",
            rollout_id="rollout-apply-selected",
            model_id="model-lowlight-001",
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        inference_runtime.load_model = AsyncMock(return_value=True)
        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/hub/rollouts/rollout-apply-selected/apply",
            headers={"X-TEMMS-Actor": "edge:edge-1"},
            json={},
        )

        assert response.status_code == 200
        assert response.json()["model"] == "model-lowlight-001"
        assert hub.get_rollout("rollout-apply-selected")["state"] == "activated"
        inference_runtime.load_model.assert_awaited_once_with("vision", "model-lowlight-001")

    def test_apply_rollout_uses_daemon_signature_defaults(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test daemon signature policy applies even when API body omits flags."""
        signing_key = "edge-signing-key"
        signing_key_file = temp_dir / "signing.key"
        signing_key_file.write_text(signing_key)

        pkg = temp_dir / "pkg-signed-apply.temms"
        models_dir = pkg / "models"
        models_dir.mkdir(parents=True)
        model_bytes = b"signed-rollout-model"
        model_file = models_dir / "model.onnx"
        model_file.write_bytes(model_bytes)
        policies_dir = pkg / "policies"
        policies_dir.mkdir()
        policy_file = policies_dir / "signed-apply-policy.yaml"
        policy_file.write_text("""
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: signed-apply-policy
spec:
  slot: vision
  rules:
    - name: route-signed-model
      priority: 100
      conditions:
        all:
          - metric: mission.mode
            operator: eq
            value: active
      action:
        switch_to: model-signed-apply-001
""".lstrip())
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-signed-apply",
            "name": "signed-apply-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-signed-apply-001",
                    "name": "model-signed-apply",
                    "version": "1.0.0",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                    "size_bytes": len(model_bytes),
                    "input_schema": {"shape": [1, 3, 224, 224]},
                    "output_schema": {"shape": [1, 1000]},
                    "runtime_constraints": {"device_profiles": ["x86_64-cpu"]},
                    "benchmark": {"available": False},
                    "provenance": {
                        "source": "unit-test",
                        "run_id": "run-signed-apply",
                        "artifact_sha256": hashlib.sha256(model_bytes).hexdigest(),
                    },
                }
            ],
            "policies": [
                {
                    "name": "signed-apply-policy",
                    "filename": "signed-apply-policy.yaml",
                    "slot": "vision",
                }
            ],
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))
        sign_package(pkg, signing_key, signer="hub-lite-test")
        archive = create_package_archive(pkg)

        hub = HubLiteStore(temp_dir / "hub_lite_signed_apply.json")
        hub.enroll_device("edge-1", profile="x86_64-cpu")
        hub.upsert_package(
            {
                "package_id": "pkg-signed-apply",
                "name": "signed-apply-package",
                "version": "1.0.0",
                "path": str(archive),
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(hub, "pkg-signed-apply")
        hub.assign_rollout(
            "edge-1",
            "pkg-signed-apply",
            slot="vision",
            rollout_id="rollout-signed-apply",
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        telemetry = TelemetryBuffer(temp_dir / "signed-apply-telemetry.jsonl")
        inference_runtime.load_model = AsyncMock(return_value=True)

        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            daemon_config=DaemonConfig(
                model_dir=temp_dir / "edge-models",
                policy_dir=temp_dir / "policies",
                rollout_signing_key_file=signing_key_file,
            ),
            hub_lite=hub,
            telemetry=telemetry,
        )
        client = TestClient(app)

        response = client.post("/v1/hub/rollouts/rollout-signed-apply/apply", json={})

        assert response.status_code == 200
        imported_history = [
            event
            for event in hub.get_rollout("rollout-signed-apply")["history"]
            if event["state"] == "imported"
        ]
        assert imported_history[-1]["detail"] == "package imported; active policies reloaded: 1"
        assert (temp_dir / "policies" / "pkg-signed-apply-signed-apply-policy.yaml").exists()
        assert {policy.metadata.name for policy in policy_engine.list_policies()} == {
            "signed-apply-policy"
        }
        audit = model_cache.list_packages()[0].manifest["_temms_import"]
        assert audit["signature_required"] is True
        assert audit["signature_verified"] is True
        assert audit["signature"]["signer"] == "hub-lite-test"
        decision = slot_manager.get_decision_log("vision", limit=1)[0]
        decision_audit = json.loads(decision["audit_metadata"])
        assert decision_audit["package"]["package_id"] == "pkg-signed-apply"
        assert decision_audit["package"]["import"]["signature_required"] is True
        assert decision_audit["package"]["import"]["signature_verified"] is True
        assert decision_audit["package"]["import"]["signature"]["signer"] == "hub-lite-test"
        assert decision_audit["package"]["import"]["source_sha256"] == package_source_sha256(
            archive
        )
        events = telemetry.read()
        assert events[-1]["event_type"] == "rollout.activated"
        assert events[-1]["payload"]["model"]["package"]["import"]["signature_verified"] is True

    def test_apply_rollout_blocks_missing_performance_benchmark_preflight(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Targeted edge apply should fail closed when declared SLO evidence is missing."""
        pkg = temp_dir / "pkg-edge-slo.temms"
        models_dir = pkg / "models"
        models_dir.mkdir(parents=True)
        model_bytes = b"edge-slo-rollout-model"
        (models_dir / "model.onnx").write_bytes(model_bytes)
        model_metadata = {
            "id": "model-edge-slo-001",
            "name": "edge-slo",
            "version": "1.0.0",
            "format": "onnx",
            "filename": "model.onnx",
            "sha256": hashlib.sha256(model_bytes).hexdigest(),
            "size_bytes": len(model_bytes),
            "runtime_constraints": {"runtimes": ["onnxruntime"]},
            "performance_slo": {
                "max_latency_ms_p95": 8.0,
                "min_throughput_ips": 120.0,
            },
        }
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-edge-slo",
            "name": "edge-slo-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [model_metadata],
            "policies": [],
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        hub = HubLiteStore(temp_dir / "hub_lite_edge_slo_apply.json")
        hub.enroll_device(
            "edge-1",
            profile="x86_64-cpu",
            inventory={
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CPUExecutionProvider"],
                    }
                },
                "memory": {"available_mb": 2048.0},
                "storage": {"available_mb": 2048.0},
            },
        )
        hub.upsert_package(
            {
                "package_id": "pkg-edge-slo",
                "name": "edge-slo-package",
                "version": "1.0.0",
                "path": str(pkg),
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "validation": {
                        "valid": True,
                        "signature_verified": True,
                        "strict_metadata": True,
                    },
                    "models": [model_metadata],
                },
            }
        )
        _release_package(hub, "pkg-edge-slo")
        hub.record_runtime_validation(
            "temms-x86_64-cpu",
            {
                "runtime_target_id": "temms-x86_64-cpu",
                "image": "temms/agent:inference-amd64",
                "dry_run": False,
                "exit_code": 0,
                "ok": True,
            },
            package_id="pkg-edge-slo",
            actor="operator:test",
        )
        hub.assign_rollout(
            "edge-1",
            "pkg-edge-slo",
            slot="vision",
            rollout_id="rollout-edge-slo",
            runtime_target_id="temms-x86_64-cpu",
            model_id="model-edge-slo-001",
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        inference_runtime.load_model = AsyncMock(return_value=True)

        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post("/v1/hub/rollouts/rollout-edge-slo/apply", json={})

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["message"] == "Rollout apply preflight failed"
        assert detail["blocking_gates"][0]["gate_id"] == "performance_fit"
        assert detail["blocking_gates"][0]["state"] == "benchmark missing"
        assert "No benchmark evidence" in detail["blocking_gates"][0]["detail"]
        assert hub.get_rollout("rollout-edge-slo")["state"] == "assigned"
        assert model_cache.get_model("model-edge-slo-001") is None
        inference_runtime.load_model.assert_not_awaited()

    def test_apply_rollout_blocks_suboptimal_pinned_runtime_preflight(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Targeted edge apply should fail closed when a better measured runtime exists."""
        pkg = temp_dir / "pkg-edge-runtime-fit.temms"
        models_dir = pkg / "models"
        models_dir.mkdir(parents=True)
        model_bytes = b"edge-runtime-fit-rollout-model"
        (models_dir / "model.onnx").write_bytes(model_bytes)
        model_metadata = {
            "id": "model-runtime-fit-001",
            "name": "runtime-fit",
            "version": "1.0.0",
            "format": "onnx",
            "filename": "model.onnx",
            "sha256": hashlib.sha256(model_bytes).hexdigest(),
            "size_bytes": len(model_bytes),
            "runtime_constraints": {"runtimes": ["onnxruntime"]},
            "performance_slo": {
                "max_latency_ms_p95": 12.0,
                "min_throughput_ips": 80.0,
            },
        }
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-edge-runtime-fit",
            "name": "edge-runtime-fit-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [model_metadata],
            "policies": [],
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        hub = HubLiteStore(temp_dir / "hub_lite_edge_runtime_fit_apply.json")
        hub.enroll_device(
            "edge-1",
            profile="x86_64-cpu",
            inventory={
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CPUExecutionProvider", "CUDAExecutionProvider"],
                    }
                },
                "memory": {"available_mb": 2048.0},
                "storage": {"available_mb": 2048.0},
            },
        )
        hub.upsert_package(
            {
                "package_id": "pkg-edge-runtime-fit",
                "name": "edge-runtime-fit-package",
                "version": "1.0.0",
                "path": str(pkg),
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "validation": {
                        "valid": True,
                        "signature_verified": True,
                        "strict_metadata": True,
                    },
                    "models": [model_metadata],
                },
            }
        )
        _release_package(hub, "pkg-edge-runtime-fit")
        for runtime_target_id in ["cpu-fit", "gpu-fit"]:
            hub.upsert_runtime_target(
                {
                    "runtime_target_id": runtime_target_id,
                    "image": f"registry.example.com/{runtime_target_id}:latest",
                    "device_profiles": ["x86_64-cpu"],
                    "runtimes": {"onnxruntime": {"available": True}},
                    "runtime_constraints": {"runtimes": ["onnxruntime"]},
                }
            )
            hub.record_runtime_validation(
                runtime_target_id,
                {
                    "runtime_target_id": runtime_target_id,
                    "image": f"registry.example.com/{runtime_target_id}:latest",
                    "dry_run": False,
                    "exit_code": 0,
                    "ok": True,
                },
                package_id="pkg-edge-runtime-fit",
                actor="operator:test",
            )
        hub.record_benchmark(
            {
                "schema_version": "temms-benchmark/v1",
                "model_id": "model-runtime-fit-001",
                "latency_ms": {"p95": 10.0},
                "throughput": {"inferences_per_second": 100.0},
            },
            device_id="edge-1",
            package_id="pkg-edge-runtime-fit",
            runtime_target_id="cpu-fit",
            actor="edge:edge-1",
        )
        hub.record_benchmark(
            {
                "schema_version": "temms-benchmark/v1",
                "model_id": "model-runtime-fit-001",
                "latency_ms": {"p95": 4.0},
                "throughput": {"inferences_per_second": 230.0},
            },
            device_id="edge-1",
            package_id="pkg-edge-runtime-fit",
            runtime_target_id="gpu-fit",
            actor="edge:edge-1",
        )
        hub.assign_rollout(
            "edge-1",
            "pkg-edge-runtime-fit",
            slot="vision",
            rollout_id="rollout-edge-runtime-fit",
            runtime_target_id="cpu-fit",
            model_id="model-runtime-fit-001",
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        inference_runtime.load_model = AsyncMock(return_value=True)

        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post("/v1/hub/rollouts/rollout-edge-runtime-fit/apply", json={})

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["message"] == "Rollout apply preflight failed"
        assert detail["blocking_gates"][0]["gate_id"] == "runtime_optimizer"
        assert detail["blocking_gates"][0]["state"] == "better target available"
        assert detail["blocking_gates"][0]["refs"]["runtime_target_id"] == "cpu-fit"
        assert detail["blocking_gates"][0]["refs"]["best_runtime_target_id"] == "gpu-fit"
        assert detail["readiness"]["production_admission"]["apply_allowed"] is False
        assert hub.get_rollout("rollout-edge-runtime-fit")["state"] == "assigned"
        assert model_cache.get_model("model-runtime-fit-001") is None
        inference_runtime.load_model.assert_not_awaited()

    def test_apply_rollout_rejects_package_changed_after_registration(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test rollout apply refuses a package whose catalog digest drifted."""
        pkg = temp_dir / "pkg-drift-apply.temms"
        models_dir = pkg / "models"
        models_dir.mkdir(parents=True)
        model_bytes = b"drift-rollout-model"
        model_file = models_dir / "model.onnx"
        model_file.write_bytes(model_bytes)
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-drift-apply",
            "name": "drift-apply-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-drift-apply-001",
                    "name": "model-drift-apply",
                    "version": "1.0.0",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                    "size_bytes": len(model_bytes),
                }
            ],
            "policies": [],
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))
        archive = create_package_archive(pkg)
        source_sha = package_source_sha256(archive)
        archive.write_bytes(b"tampered-package-archive")

        hub = HubLiteStore(temp_dir / "hub_lite_drift_apply.json")
        hub.enroll_device("edge-1", profile="x86_64-cpu")
        hub.upsert_package(
            {
                "package_id": "pkg-drift-apply",
                "name": "drift-apply-package",
                "version": "1.0.0",
                "path": str(archive),
                "sha256": source_sha,
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(hub, "pkg-drift-apply")
        hub.assign_rollout(
            "edge-1",
            "pkg-drift-apply",
            slot="vision",
            rollout_id="rollout-drift-apply",
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        inference_runtime.load_model = AsyncMock(return_value=True)

        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post("/v1/hub/rollouts/rollout-drift-apply/apply", json={})

        assert response.status_code == 409
        assert "changed after registration" in response.json()["detail"]
        assert hub.get_rollout("rollout-drift-apply")["state"] == "failed"
        assert "changed after registration" in (
            hub.get_rollout("rollout-drift-apply")["history"][-1]["detail"]
        )
        assert model_cache.get_model("model-drift-apply-001") is None
        inference_runtime.load_model.assert_not_awaited()

    def test_apply_rollout_rejects_unsatisfied_runtime_constraints(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test rollout apply fails before import when runtimes are missing."""
        pkg = temp_dir / "pkg-constrained.temms"
        models_dir = pkg / "models"
        models_dir.mkdir(parents=True)
        model_bytes = b"fake-rollout-model"
        model_file = models_dir / "model.onnx"
        model_file.write_bytes(model_bytes)
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-constrained",
            "name": "constrained-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-constrained-001",
                    "name": "model-constrained",
                    "version": "1.0.0",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                    "size_bytes": len(model_bytes),
                    "runtime_constraints": {"runtimes": ["missing-runtime"]},
                }
            ],
            "policies": [],
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        hub = HubLiteStore(temp_dir / "hub_lite_constraints.json")
        hub.enroll_device("edge-1", profile="x86_64-cpu")
        hub.upsert_package(
            {
                "package_id": "pkg-constrained",
                "name": "constrained-package",
                "version": "1.0.0",
                "path": str(pkg),
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(hub, "pkg-constrained")
        hub.assign_rollout(
            "edge-1",
            "pkg-constrained",
            slot="vision",
            rollout_id="rollout-constrained",
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        inference_runtime.load_model = AsyncMock(return_value=True)

        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post("/v1/hub/rollouts/rollout-constrained/apply", json={})

        assert response.status_code == 500
        assert "Runtime constraints are not satisfied" in response.json()["detail"]
        assert hub.get_rollout("rollout-constrained")["state"] == "failed"
        assert model_cache.get_model("model-constrained-001") is None
        inference_runtime.load_model.assert_not_awaited()

    def test_apply_rollout_uses_device_inventory_for_runtime_constraints(
        self,
        temp_dir,
        monkeypatch,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test rollout apply evaluates constraints against the target device inventory."""
        from temms.core import runtime_profiles

        monkeypatch.setattr(
            runtime_profiles,
            "detect_runtime_capabilities",
            lambda: runtime_profiles.RuntimeCapabilities(
                os="test-os",
                machine="x86_64",
                python="3.11",
                device_profile="x86_64-cpu",
                runtimes={
                    "tflite_runtime": {"available": False},
                    "tflite": {"available": False},
                },
                accelerators={},
            ),
        )

        pkg = temp_dir / "pkg-tflite-target.temms"
        models_dir = pkg / "models"
        models_dir.mkdir(parents=True)
        model_bytes = b"fake-tflite-rollout-model"
        model_file = models_dir / "model.tflite"
        model_file.write_bytes(model_bytes)
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-tflite-target",
            "name": "tflite-target-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-tflite-target-001",
                    "name": "model-tflite-target",
                    "version": "1.0.0",
                    "format": "tflite",
                    "filename": "model.tflite",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                    "size_bytes": len(model_bytes),
                    "runtime_constraints": {"runtimes": ["tflite_runtime"]},
                }
            ],
            "policies": [],
            "compatibility": {"device_profiles": ["rpi5-tflite"]},
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        hub = HubLiteStore(temp_dir / "hub_lite_target_inventory.json")
        hub.enroll_device(
            "edge-rpi",
            profile="rpi5-tflite",
            inventory={
                "runtimes": {
                    "tflite_runtime": {"available": True},
                    "tflite": {"available": True},
                }
            },
        )
        hub.upsert_package(
            {
                "package_id": "pkg-tflite-target",
                "name": "tflite-target-package",
                "version": "1.0.0",
                "path": str(pkg),
                "device_profiles": ["rpi5-tflite"],
            }
        )
        _release_package(hub, "pkg-tflite-target")
        hub.assign_rollout(
            "edge-rpi",
            "pkg-tflite-target",
            slot="vision",
            rollout_id="rollout-tflite-target",
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        inference_runtime.load_model = AsyncMock(return_value=True)

        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post("/v1/hub/rollouts/rollout-tflite-target/apply", json={})

        assert response.status_code == 200
        assert response.json()["status"] == "activated"
        assert response.json()["model"] == "model-tflite-target-001"
        assert hub.get_rollout("rollout-tflite-target")["state"] == "activated"
        imported = model_cache.get_model("model-tflite-target-001")
        assert imported is not None
        assert imported.metadata["runtime_constraints"]["runtimes"] == ["tflite_runtime"]
        assert model_cache.list_packages()[0].manifest["_temms_import"]["device_profile"] == (
            "rpi5-tflite"
        )
        inference_runtime.load_model.assert_awaited_once_with("vision", "model-tflite-target-001")

    def test_apply_rollout_rejects_compatibility_runtime_constraints(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test rollout apply checks package-level compatibility constraints."""
        pkg = temp_dir / "pkg-compat-constrained.temms"
        models_dir = pkg / "models"
        models_dir.mkdir(parents=True)
        model_bytes = b"fake-compat-constrained-model"
        model_file = models_dir / "model.onnx"
        model_file.write_bytes(model_bytes)
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-compat-constrained",
            "name": "compat-constrained-package",
            "version": "1.0.0",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-compat-constrained-001",
                    "name": "model-compat-constrained",
                    "version": "1.0.0",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                    "size_bytes": len(model_bytes),
                }
            ],
            "policies": [],
            "compatibility": {
                "device_profiles": ["x86_64-cpu"],
                "runtime_constraints": {"runtimes": ["missing-runtime"]},
            },
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        hub = HubLiteStore(temp_dir / "hub_lite_compat_constraints.json")
        hub.enroll_device("edge-1", profile="x86_64-cpu")
        hub.upsert_package(
            {
                "package_id": "pkg-compat-constrained",
                "name": "compat-constrained-package",
                "version": "1.0.0",
                "path": str(pkg),
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(hub, "pkg-compat-constrained")
        hub.assign_rollout(
            "edge-1",
            "pkg-compat-constrained",
            slot="vision",
            rollout_id="rollout-compat-constrained",
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        inference_runtime.load_model = AsyncMock(return_value=True)

        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/hub/rollouts/rollout-compat-constrained/apply",
            json={},
        )

        assert response.status_code == 500
        assert "Runtime constraints are not satisfied" in response.json()["detail"]
        assert "missing runtimes: missing-runtime" in response.json()["detail"]
        assert hub.get_rollout("rollout-compat-constrained")["state"] == "failed"
        assert model_cache.get_model("model-compat-constrained-001") is None
        inference_runtime.load_model.assert_not_awaited()

    def test_evidence_export_includes_fleet_audit_and_benchmarks(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
    ):
        """Test post-mission evidence bundle exports operator evidence."""
        hub = HubLiteStore(temp_dir / "hub_lite_evidence.json")
        hub.enroll_device("edge-1", profile="x86_64-cpu")
        hub.upsert_package(
            {
                "package_id": "pkg-vision-1",
                "name": "vision",
                "version": "1.0.0",
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(hub, "pkg-vision-1")
        hub.assign_rollout(
            "edge-1",
            "pkg-vision-1",
            slot="vision",
            rollout_id="rollout-1",
            actor="operator:alice",
        )
        validation = hub.record_runtime_validation(
            "temms-x86_64-cpu",
            {
                "runtime_target_id": "temms-x86_64-cpu",
                "image": "temms/agent:inference-amd64",
                "package_path": "/tmp/pkg-vision-1.temms.tar.zst",
                "command": [
                    "docker",
                    "run",
                    "-e",
                    "TEMMS_PACKAGE_SIGNING_KEY=secret",
                    "temms/agent:inference-amd64",
                    "temms",
                    "package",
                    "validate",
                    "/temms-input/package",
                    "--signing-key",
                    "secret",
                ],
                "dry_run": True,
                "exit_code": None,
                "ok": True,
            },
            package_id="pkg-vision-1",
            actor="operator:alice",
        )
        benchmark = hub.record_benchmark(
            {
                "schema_version": "temms-benchmark/v1",
                "model_id": "model-a",
                "slot": "vision",
                "latency_ms": {"p95": 7.5},
                "throughput": {"inferences_per_second": 133.0},
            },
            device_id="edge-1",
            package_id="pkg-vision-1",
            runtime_target_id="temms-x86_64-cpu",
            actor="edge:edge-1",
        )
        telemetry = TelemetryBuffer(temp_dir / "evidence-telemetry.jsonl")
        telemetry.append("rollout.activated", {"slot": "vision", "rollout_id": "rollout-1"})
        plan = hub.create_rollout_plan(
            plan_id="plan-evidence",
            package_id="pkg-vision-1",
            device_ids=["edge-1"],
            slot="vision",
            batch_size=1,
            actor="operator:planner",
        )
        assert plan["state"] == "ready"
        hub.advance_rollout_plan("plan-evidence", actor="operator:planner")
        model_file = temp_dir / "models" / "model-a.onnx"
        model_file.parent.mkdir(parents=True, exist_ok=True)
        model_file.write_bytes(b"model-a-bytes")
        model_cache.add_cached_model(
            model_id="model-a",
            name="model-a",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=model_file,
            sha256="0" * 64,
            size_bytes=999,
            package_id="pkg-vision-1",
        )
        model_cache.add_package(
            package_id="pkg-vision-1",
            name="vision",
            version="1.0.0",
            source="/tmp/pkg-vision-1.temms.tar.zst",
            manifest={
                "schema_version": "v1",
                "package_id": "pkg-vision-1",
                "name": "vision",
                "version": "1.0.0",
                "policies": [{"name": "vision", "filename": "vision.yaml", "slot": "vision"}],
                "_temms_import": {
                    "schema_version": "temms-import-audit/v1",
                    "imported_at": "2026-01-01T00:00:04Z",
                    "source": "/tmp/pkg-vision-1.temms.tar.zst",
                    "source_type": "archive",
                    "source_sha256": "f" * 64,
                    "hashes_verified": True,
                    "signature_required": True,
                    "signature_verified": True,
                    "signature": {
                        "schema_version": "temms-signature/v1",
                        "algorithm": "HMAC-SHA256",
                        "signer": "temms-hub-lite",
                        "key_fingerprint": "sha256:test",
                    },
                    "warnings": [],
                },
            },
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        slot_manager.activate_model(
            "vision",
            "model-a",
            "rollout",
            "rollout-1",
            audit_metadata={
                "package_id": "pkg-vision-1",
                "model_version": "1.0.0",
                "provenance": {"source": "mlflow", "run_id": "run-a"},
            },
        )
        condition_store.set("environment.visibility_m", 50, "sensor", 100)
        benchmark_dir = temp_dir / "benchmarks"
        benchmark_dir.mkdir()
        (benchmark_dir / "model-a.json").write_text(
            json.dumps({"schema_version": "temms-benchmark/v1", "model_id": "model-a"})
        )
        daemon_config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
        )
        evidence_app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            daemon_config=daemon_config,
            hub_lite=hub,
            telemetry=telemetry,
        )
        evidence_client = TestClient(evidence_app)

        response = evidence_client.post(
            "/v1/hub/evidence/export",
            json={"decision_limit": 10, "include_benchmarks": True},
        )

        assert response.status_code == 200
        bundle = response.json()
        assert bundle["schema_version"] == "temms-evidence-bundle/v1"
        assert bundle["hub_lite"]["devices"]["edge-1"]["profile"] == "x86_64-cpu"
        assert bundle["diagnostics"]["schema_version"] == "temms-diagnostics/v1"
        assert bundle["diagnostics"]["system"]["device_profile"]
        assert "x86_64-cpu" in bundle["diagnostics"]["known_device_profiles"]
        assert bundle["diagnostics"]["model_cache"]["models"] == 1
        assert bundle["diagnostics"]["model_cache"]["health"]["status"] == "degraded"
        assert {
            issue["type"] for issue in bundle["diagnostics"]["model_cache"]["health"]["issues"]
        } == {
            "size_mismatch",
            "sha256_mismatch",
        }
        assert {path["name"] for path in bundle["diagnostics"]["paths"]} >= {
            "database_dir",
            "model_dir",
            "cache_dir",
            "package_dir",
            "policy_dir",
        }
        assert all(path["write_probe"]["attempted"] for path in bundle["diagnostics"]["paths"])
        assert bundle["diagnostics"]["port"] == bundle["diagnostics"]["ports"][0]
        assert bundle["diagnostics"]["ports"][0]["name"] == "api"
        assert bundle["diagnostics"]["ports"][0]["status"] in {"free", "in use"}
        assert bundle["conditions"]["environment.visibility_m"]["value"] == 50
        assert bundle["decisions"][0]["conditions_snapshot"] == {}
        assert bundle["decisions"][0]["audit_metadata"]["package_id"] == "pkg-vision-1"
        assert bundle["decisions"][0]["audit_metadata"]["provenance"]["run_id"] == "run-a"
        assert bundle["telemetry"]["count"] == 1
        assert any(event["actor"] == "operator:alice" for event in bundle["rollout_events"])
        assert bundle["runtime_validations"][0]["validation_id"] == validation["validation_id"]
        assert "secret" not in bundle["runtime_validations"][0]["result"]["command_text"]
        assert validation["validation_id"] in bundle["hub_lite"]["runtime_validations"]
        assert bundle["hub_benchmarks"][0]["benchmark_id"] == benchmark["benchmark_id"]
        assert benchmark["benchmark_id"] in bundle["hub_lite"]["benchmarks"]
        assert bundle["package_imports"][0]["package_id"] == "pkg-vision-1"
        assert bundle["package_imports"][0]["signature_verified"] is True
        assert bundle["package_promotions"][0]["state"] == "released"
        assert bundle["rollout_plans"][0]["plan_id"] == "plan-evidence"
        assert bundle["rollout_plans"][0]["state"] in {"advanced", "completed"}
        assert bundle["benchmarks"][0]["model_id"] == "model-a"
        assert {entry["kind"] for entry in bundle["timeline"]} == {
            "benchmark",
            "decision",
            "package_import",
            "package_promotion",
            "rollout_plan",
            "runtime_validation",
            "rollout",
            "telemetry",
        }

        edge_summary_response = evidence_client.get(
            "/v1/evidence?limit=10&summary=true&summary_limit=5"
        )
        assert edge_summary_response.status_code == 200
        edge_summary = edge_summary_response.json()
        assert edge_summary["schema_version"] == "temms-evidence-summary/v1"
        assert edge_summary["source_schema_version"] == "temms-evidence-bundle/v1"
        assert "rollout applied" in edge_summary["headline"]
        assert edge_summary["trust"]["signed_package_imports"] == 1
        assert edge_summary["trust"]["runtime_validations_passed"] == 1
        assert edge_summary["active_slots"][0]["slot"] == "vision"
        assert edge_summary["decisions"][0]["package_id"] == "pkg-vision-1"
        assert edge_summary["decisions"][0]["signature_verified"] is True

        edge_replay_response = evidence_client.get(
            "/v1/evidence?limit=10&replay=true&replay_limit=5"
        )
        assert edge_replay_response.status_code == 200
        edge_replay = edge_replay_response.json()
        edge_phases = {phase["phase"]: phase for phase in edge_replay["phases"]}
        assert edge_replay["schema_version"] == "temms-mission-replay/v1"
        assert edge_phases["signed_package"]["status"] == "complete"
        assert edge_phases["runtime_validation"]["status"] == "preview_only"
        assert edge_phases["edge_rollout"]["status"] == "complete"
        assert edge_phases["rollout_coordination"]["status"] == "complete"
        assert edge_replay["events"][0]["sequence"] == 1
        assert edge_replay["events"]

        ingested = evidence_client.post(
            "/v1/hub/evidence/ingest",
            json={
                "bundle": bundle,
                "device_id": "edge-1",
                "actor": "operator:auditor",
            },
        )
        assert ingested.status_code == 200, ingested.text
        ingested_record = ingested.json()["evidence"]
        assert ingested_record["schema_version"] == "temms-ingested-evidence/v1"
        assert ingested_record["device_id"] == "edge-1"
        assert ingested_record["actor"] == "operator:auditor"
        assert ingested_record["integrity"]["payload_sha256"] == (
            bundle["integrity"]["payload_sha256"]
        )
        assert ingested_record["summary"]["schema_version"] == "temms-evidence-summary/v1"

        duplicate_ingest = evidence_client.post(
            "/v1/hub/evidence/ingest",
            json={"bundle": bundle, "device_id": "edge-1", "actor": "operator:auditor"},
        )
        assert duplicate_ingest.status_code == 200, duplicate_ingest.text
        assert duplicate_ingest.json()["evidence"]["duplicate"] is True

        listed_evidence = evidence_client.get("/v1/hub/evidence")
        assert listed_evidence.status_code == 200
        assert listed_evidence.json()["count"] == 1
        assert listed_evidence.json()["evidence_bundles"][0]["evidence_id"] == (
            ingested_record["evidence_id"]
        )

        hub_summary_response = evidence_client.post(
            "/v1/hub/evidence/export",
            json={
                "decision_limit": 10,
                "include_benchmarks": True,
                "summary": True,
                "summary_limit": 5,
            },
        )
        assert hub_summary_response.status_code == 200
        hub_summary = hub_summary_response.json()
        assert hub_summary["schema_version"] == "temms-evidence-summary/v1"
        assert hub_summary["counts"]["hub_benchmarks"] == 1
        assert hub_summary["counts"]["package_imports"] == 1
        assert hub_summary["counts"]["rollout_plans"] >= 1
        assert hub_summary["counts"]["ingested_evidence_bundles"] == 1
        assert hub_summary["ingested_evidence"][0]["evidence_id"] == (
            ingested_record["evidence_id"]
        )
        assert hub_summary["timeline"]

        hub_replay_response = evidence_client.post(
            "/v1/hub/evidence/export",
            json={
                "decision_limit": 10,
                "include_benchmarks": True,
                "replay": True,
                "replay_limit": 5,
            },
        )
        assert hub_replay_response.status_code == 200
        hub_replay = hub_replay_response.json()
        assert hub_replay["schema_version"] == "temms-mission-replay/v1"
        assert hub_replay["outcome"]["counts"]["hub_benchmarks"] == 1
        assert hub_replay["outcome"]["counts"]["rollout_plans"] >= 1
        assert hub_replay["outcome"]["counts"]["ingested_evidence_bundles"] == 1
        hub_phases = {phase["phase"]: phase for phase in hub_replay["phases"]}
        assert hub_phases["evidence_aggregation"]["status"] == "complete"
        assert hub_replay["events"]

    def test_evidence_path_report_uses_actual_write_probe(self, temp_dir, monkeypatch):
        """Test evidence diagnostics surface failed write probes."""
        from temms import evidence

        def fake_probe(path):
            return {
                "ok": False,
                "path": str(path),
                "attempted": True,
                "error": "permission denied",
            }

        monkeypatch.setattr(evidence, "_probe_path_writable", fake_probe)

        report = evidence._path_report("policy_dir", temp_dir)

        assert report["writable"] is False
        assert report["write_probe"]["attempted"] is True
        assert report["write_probe"]["error"] == "permission denied"

    def test_hub_rollout_rollback_to_previous_model(
        self,
        temp_dir,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        inference_runtime,
        sample_model_file,
    ):
        """Test Hub Lite rollback targets one rollout and restores previous model."""
        dest_path, sha256, size = model_storage.store_model(
            sample_model_file,
            "model-rollback-v1",
            verify=True,
        )
        model_cache.add_cached_model(
            model_id="model-rollback-v1",
            name="model-rollback",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=dest_path,
            sha256=sha256,
            size_bytes=size,
            package_id="pkg-rollback-old",
        )
        model_cache.add_cached_model(
            model_id="model-rollback-v2",
            name="model-rollback",
            version="2.0.0",
            format=ModelFormat.ONNX,
            path=dest_path,
            sha256=sha256,
            size_bytes=size,
            package_id="pkg-rollback-new",
        )
        hub = HubLiteStore(temp_dir / "hub_lite_rollback.json")
        hub.enroll_device("edge-1", profile="x86_64-cpu")
        hub.upsert_package(
            {
                "package_id": "pkg-rollback-new",
                "name": "rollback-package",
                "version": "2.0.0",
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(hub, "pkg-rollback-new")
        hub.assign_rollout(
            "edge-1",
            "pkg-rollback-new",
            slot="vision",
            rollout_id="rollout-rollback",
        )
        slot_manager.create_slot(name="vision", description="Vision", required=True)
        slot_manager.activate_model("vision", "model-rollback-v1", "startup", "seed")
        slot_manager.activate_model(
            "vision",
            "model-rollback-v2",
            "rollout",
            "rollout-rollback",
        )
        telemetry = TelemetryBuffer(temp_dir / "rollback-telemetry.jsonl")
        inference_runtime.load_model = AsyncMock(return_value=True)
        app = create_app(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            inference_runtime=inference_runtime,
            hub_lite=hub,
            telemetry=telemetry,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/hub/rollouts/rollout-rollback/rollback",
            headers={"X-TEMMS-Actor": "operator:bob"},
            json={"reason": "operator requested"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "rolled_back"
        assert response.json()["model"] == "model-rollback-v1"
        assert hub.get_rollout("rollout-rollback")["state"] == "rolled_back"
        assert hub.get_rollout("rollout-rollback")["history"][-1]["actor"] == "operator:bob"
        assert slot_manager.get_slot("vision").active_model_id == "model-rollback-v1"
        inference_runtime.load_model.assert_awaited_once_with("vision", "model-rollback-v1")
        events = telemetry.read()
        assert {event["event_type"] for event in events} == {
            "slot.rollback",
            "rollout.rolled_back",
        }
        assert all(event["payload"]["actor"] == "operator:bob" for event in events)
