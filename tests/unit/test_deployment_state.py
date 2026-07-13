import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from temms.daemon.deployment_state import DeploymentStateStore, DeploymentState
from temms.daemon.pending_preflight import (
    pending_sync_preflight,
    runtime_target_assessment_sha256,
)
from temms.daemon.pending_ops import (
    PendingOperationsStore,
    pending_operation_signature_status,
    verify_pending_operation_signature,
)
from temms.hub_lite import HubLiteStore


def _payload_hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def test_deployment_state_store_roundtrip(tmp_path):
    store = DeploymentStateStore(tmp_path / "deployment_state.json")
    store.set_state(DeploymentState.READY, "test")
    assert store.get_state() == DeploymentState.READY


def test_deployment_state_write_failure_preserves_previous_state(tmp_path, monkeypatch):
    store = DeploymentStateStore(tmp_path / "deployment_state.json")
    store.set_state(DeploymentState.READY, "ready")
    previous_payload = store.path.read_text(encoding="utf-8")
    original_replace = type(store.path).replace

    def fail_replace(path, target):
        if target == store.path:
            raise OSError("simulated replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(type(store.path), "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        store.set_state(DeploymentState.FAILED, "failed")

    assert store.path.read_text(encoding="utf-8") == previous_payload
    assert store.get_state() == DeploymentState.READY
    assert not list(tmp_path.glob(".deployment_state.json-*"))


def test_pending_ops_enqueue_and_clear(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue("deploy", {"slot": "vision"})
    entries = store.read_all()
    assert len(entries) == 1
    store.clear()
    assert store.read_all() == []


def test_pending_ops_replace_all_preserves_selected_entries(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue("update_conditions", {"conditions": {"mission.phase": "egress"}})
    store.enqueue("deploy", {"slot": "vision", "model_id": "model-lowlight"})
    entries = store.read_all()

    store.replace_all(entries[1:])

    remaining = store.read_all()
    assert len(remaining) == 1
    assert remaining[0] == entries[1]


def test_pending_ops_signs_and_verifies_entries(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue(
        "deploy",
        {"slot": "vision", "model_id": "model-lowlight"},
        signing_key="ddil-secret",
        signer="operator:test",
    )

    entry = store.read_all()[0]
    signature = verify_pending_operation_signature(entry, "ddil-secret")

    assert entry["signature"]["signer"] == "operator:test"
    assert signature["verified"] is True
    assert signature["payload_sha256"] == entry["signature"]["payload_sha256"]


def test_pending_ops_rejects_tampered_signature(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue(
        "deploy",
        {"slot": "vision", "model_id": "model-lowlight"},
        signing_key="ddil-secret",
    )
    entry = store.read_all()[0]
    entry["payload"]["model_id"] = "model-daylight"

    with pytest.raises(ValueError, match="payload digest mismatch|signature mismatch"):
        verify_pending_operation_signature(entry, "ddil-secret")


def test_pending_ops_rejects_tampered_key_fingerprint(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue(
        "deploy",
        {"slot": "vision", "model_id": "model-lowlight"},
        signing_key="ddil-secret",
    )
    entry = store.read_all()[0]
    entry["signature"]["key_fingerprint"] = "sha256:wrong"

    with pytest.raises(ValueError, match="key fingerprint mismatch"):
        verify_pending_operation_signature(entry, "ddil-secret")


def test_pending_ops_reports_signature_status(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue(
        "deploy",
        {"slot": "vision", "model_id": "model-lowlight"},
        signing_key="ddil-secret",
    )
    entry = store.read_all()[0]

    verified = pending_operation_signature_status(entry, signing_key="ddil-secret")
    unavailable = pending_operation_signature_status(entry, require_signature=True)
    missing = pending_operation_signature_status(
        {"operation": "deploy", "payload": {}, "recorded_at": "2026-06-11T12:00:00"},
        require_signature=True,
    )

    assert verified["status"] == "verified"
    assert verified["verified"] is True
    assert unavailable["status"] == "key_unavailable"
    assert unavailable["verified"] is False
    assert unavailable["key_fingerprint"] == entry["signature"]["key_fingerprint"]
    assert missing == {
        "status": "missing_signature",
        "verified": False,
        "reason": "signature required",
    }


def test_pending_ops_retargets_deploy_runtime_and_resigns(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    payload = {
        "slot": "vision",
        "model_id": "model-lowlight",
        "runtime_target_id": "temms-rpi5-tflite",
    }
    store.enqueue("deploy", payload, signing_key="ddil-secret", signer="operator:queue")
    queued = store.read_all()[0]
    original_signature_payload = queued["signature"]["payload_sha256"]
    payload_sha256 = _payload_hash(payload)

    with pytest.raises(ValueError, match="requires a signing key"):
        store.retarget_runtime(
            payload_sha256=payload_sha256,
            runtime_target_id="temms-x86_64-cpu",
            actor="operator:test",
            reason="select measured runtime",
        )

    result = store.retarget_runtime(
        payload_sha256=payload_sha256,
        runtime_target_id="temms-x86_64-cpu",
        actor="operator:test",
        reason="select measured runtime",
        signing_key="ddil-secret",
    )

    updated = store.read_all()[0]
    audit = updated["payload"]["_temms_runtime_retarget"][0]
    signature = verify_pending_operation_signature(updated, "ddil-secret")

    assert result["retargeted"] == 1
    assert result["previous_runtime_target_id"] == "temms-rpi5-tflite"
    assert result["runtime_target_id"] == "temms-x86_64-cpu"
    assert result["updated_payload_sha256"] == _payload_hash(updated["payload"])
    assert updated["payload"]["runtime_target_id"] == "temms-x86_64-cpu"
    assert audit["previous_runtime_target_id"] == "temms-rpi5-tflite"
    assert audit["runtime_target_id"] == "temms-x86_64-cpu"
    assert audit["previous_payload_sha256"] == payload_sha256
    assert updated["signature"]["payload_sha256"] != original_signature_payload
    assert signature["verified"] is True


def test_pending_ops_quarantines_selected_entries(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue("deploy", {"slot": "vision", "model_id": "missing-model"})
    store.enqueue("update_conditions", {"conditions": {"mission.phase": "egress"}})

    result = store.quarantine(
        indexes={0},
        preflight_entries={
            0: {
                "index": 0,
                "operation": "deploy",
                "ready": False,
                "replay_status": "blocked",
                "reason": "model not found: missing-model",
            }
        },
        actor="operator:test",
        reason="blocked preflight",
    )

    assert result["quarantined"] == 1
    assert result["remaining"] == 1
    assert [entry["operation"] for entry in store.read_all()] == ["update_conditions"]
    dead_letters = store.read_dead_letter()
    assert len(dead_letters) == 1
    assert dead_letters[0]["actor"] == "operator:test"
    assert dead_letters[0]["reason"] == "blocked preflight"
    assert dead_letters[0]["preflight"]["reason"] == "model not found: missing-model"
    assert dead_letters[0]["entry"]["operation"] == "deploy"


def test_pending_ops_acknowledges_dead_letters_without_deleting_audit(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue("deploy", {"slot": "vision", "model_id": "missing-model"})
    store.quarantine(
        indexes={0},
        preflight_entries={
            0: {
                "index": 0,
                "operation": "deploy",
                "ready": False,
                "replay_status": "blocked",
                "reason": "model not found: missing-model",
            }
        },
        actor="operator:test",
        reason="blocked preflight",
    )

    result = store.acknowledge_dead_letters(
        actor="operator:model-deployment-control",
        reason="reviewed for demo readiness",
    )

    assert result["acknowledged"] == 1
    assert result["dead_letters"] == 1
    dead_letters = store.read_dead_letter()
    assert len(dead_letters) == 1
    assert dead_letters[0]["acknowledged"] is True
    assert dead_letters[0]["acknowledged_by"] == "operator:model-deployment-control"
    assert dead_letters[0]["acknowledgement_reason"] == "reviewed for demo readiness"
    assert dead_letters[0]["entry"]["operation"] == "deploy"

    second_result = store.acknowledge_dead_letters(
        actor="operator:model-deployment-control",
        reason="already handled",
    )
    assert second_result["acknowledged"] == 0
    assert len(store.read_dead_letter()) == 1


def test_pending_ops_requeues_dead_letters_without_deleting_audit(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue(
        "deploy",
        {
            "slot": "vision",
            "model_id": "missing-model",
            "runtime_target_id": "temms-rpi5-tflite",
        },
    )
    store.quarantine(
        indexes={0},
        preflight_entries={
            0: {
                "index": 0,
                "operation": "deploy",
                "ready": False,
                "replay_status": "blocked",
                "reason": "runtime target blocked",
            }
        },
        actor="operator:test",
        reason="blocked preflight",
    )
    assert store.read_all() == []
    dead_letter_digest = store.read_dead_letter()[0]["payload_sha256"]

    result = store.requeue_dead_letters(
        actor="operator:model-deployment-control",
        reason="edge runtime evidence refreshed",
        payload_sha256s={dead_letter_digest},
    )

    assert result["requeued"] == 1
    assert result["pending"] == 1
    assert store.read_all()[0]["operation"] == "deploy"
    dead_letters = store.read_dead_letter()
    assert len(dead_letters) == 1
    assert dead_letters[0]["requeued"] is True
    assert dead_letters[0]["requeued_by"] == "operator:model-deployment-control"
    assert dead_letters[0]["requeue_reason"] == "edge runtime evidence refreshed"

    second_result = store.requeue_dead_letters(
        actor="operator:model-deployment-control",
        reason="already requeued",
        payload_sha256s={dead_letter_digest},
    )
    assert second_result["requeued"] == 0
    assert len(store.read_all()) == 1
    acknowledge_result = store.acknowledge_dead_letters(
        actor="operator:model-deployment-control",
        reason="already requeued",
        payload_sha256s={dead_letter_digest},
    )
    assert acknowledge_result["acknowledged"] == 0


def test_pending_sync_preflight_blocks_deploy_when_hub_readiness_fails(tmp_path):
    hub = HubLiteStore(tmp_path / "hub.json")
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
            "accelerators": {"nvidia": {"available": False}},
        },
    )
    hub.upsert_runtime_target(
        {
            "runtime_target_id": "customer-gpu",
            "name": "Customer GPU runtime",
            "image": "registry.example.com/edge/gpu:2026.06",
            "device_profiles": ["x86_64-cpu"],
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CUDAExecutionProvider"],
                }
            },
            "accelerators": {"nvidia": {"available": True}},
            "runtime_constraints": {
                "device_profiles": ["x86_64-cpu"],
                "runtimes": ["onnxruntime"],
                "providers": ["CUDAExecutionProvider"],
                "accelerators": ["nvidia"],
            },
        }
    )
    hub.upsert_package(
        {
            "package_id": "pkg-gpu",
            "name": "gpu-package",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {
                        "id": "model-gpu",
                        "runtime_constraints": {
                            "device_profiles": ["x86_64-cpu"],
                            "runtimes": ["onnxruntime"],
                            "providers": ["CUDAExecutionProvider"],
                            "accelerators": ["nvidia"],
                        },
                    }
                ],
            },
        }
    )
    _release_package(hub, "pkg-gpu")
    state = _pending_preflight_state(
        hub=hub,
        model_id="model-gpu",
        package_id="pkg-gpu",
    )

    preflight = pending_sync_preflight(
        state,
        [
            {
                "operation": "deploy",
                "payload": {
                    "slot": "vision",
                    "model_id": "model-gpu",
                    "package_id": "pkg-gpu",
                    "device_id": "edge-1",
                    "runtime_target_id": "customer-gpu",
                },
            }
        ],
    )

    entry = preflight["entries"][0]
    assert preflight["status"] == "blocked"
    assert entry["ready"] is False
    assert entry["replay_status"] == "blocked"
    assert entry["reason"].startswith(
        "runtime capability lock status is blocked, expected locked"
    )
    assert entry["hub_readiness_status"] == "blocked"
    assert entry["hub_capability_lock_status"] == "blocked"
    assert entry["hub_blocking_gates"][0]["gate_id"] == "runtime_target"
    assert "edge inventory cannot host runtime target customer-gpu" in (
        entry["hub_blocking_gates"][0]["detail"]
    )


def test_pending_sync_preflight_allows_direct_deploy_when_only_rollout_gate_warns(tmp_path):
    hub = HubLiteStore(tmp_path / "hub.json")
    hub.enroll_device(
        "edge-1",
        profile="x86_64-cpu",
        inventory={
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                }
            }
        },
    )
    hub.upsert_package(
        {
            "package_id": "pkg-direct",
            "name": "direct-package",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {
                        "id": "model-direct",
                        "format": "onnx",
                        "filename": "model-direct.onnx",
                        "runtime_constraints": {
                            "runtimes": ["onnxruntime"],
                            "providers": ["CPUExecutionProvider"],
                        },
                    }
                ],
            },
        }
    )
    _release_package(hub, "pkg-direct")
    hub.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-direct",
        actor="operator:test",
    )
    state = _pending_preflight_state(
        hub=hub,
        model_id="model-direct",
        package_id="pkg-direct",
    )

    preflight = pending_sync_preflight(
        state,
        [
            {
                "operation": "deploy",
                "payload": {
                    "request": {
                        "slot": "vision",
                        "model_id": "model-direct",
                        "package_id": "pkg-direct",
                        "device_id": "edge-1",
                        "runtime_target_id": "temms-x86_64-cpu",
                    }
                },
            }
        ],
    )

    entry = preflight["entries"][0]
    assert preflight["status"] == "ready"
    assert entry["ready"] is True
    assert entry["resolved_model_id"] == "model-direct"
    assert entry["hub_readiness_status"] == "attention"
    assert entry["hub_attention_gates"][0]["gate_id"] == "rollout_gate"
    assert entry["hub_runtime_fit_score"] == 91
    assert entry["hub_runtime_fit_tier"] == "optimal"
    assert entry["hub_runtime_lane_id"] == "cpu-onnx"
    assert entry["hub_runtime_lane_label"] == "CPU portable"
    assert entry["hub_artifact_lane_status"] == "go"
    assert entry["hub_artifact_lane_state"] == "native artifact"
    assert entry["hub_artifact_lane_detail"] == "onnx artifact is native for CPU portable"
    assert entry["hub_production_apply_allowed"] is True
    assert entry["hub_capability_lock_status"] == "locked"
    assert len(entry["hub_capability_sha256"]) == 64
    assert entry["hub_capability_runtime_target_id"] == "temms-x86_64-cpu"
    assert entry["hub_capability_telemetry_status"] == "go"
    assert entry["hub_capability_telemetry_state"] == "telemetry fresh"
    assert entry["hub_capability_heartbeat_stale_after_seconds"] == 300
    assert entry["hub_runtime_capability_lock"]["status"] == "locked"
    assert entry["hub_runtime_capability_lock"]["capability_sha256"] == (
        entry["hub_capability_sha256"]
    )


def test_pending_sync_preflight_blocks_deploy_when_capability_lock_is_stale(tmp_path):
    hub = HubLiteStore(tmp_path / "hub.json")
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
            "storage": {"available_mb": 4096.0},
        },
    )
    hub.upsert_package(
        {
            "package_id": "pkg-stale-heartbeat",
            "name": "stale-heartbeat-package",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {
                        "id": "model-stale-heartbeat",
                        "format": "onnx",
                        "filename": "model-stale-heartbeat.onnx",
                        "runtime_constraints": {
                            "runtimes": ["onnxruntime"],
                            "providers": ["CPUExecutionProvider"],
                        },
                    }
                ],
            },
        }
    )
    _release_package(hub, "pkg-stale-heartbeat")
    hub.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-stale-heartbeat",
        actor="operator:test",
    )
    _set_hub_device_last_seen(hub, "edge-1", datetime.now(timezone.utc) - timedelta(minutes=10))
    state = _pending_preflight_state(
        hub=hub,
        model_id="model-stale-heartbeat",
        package_id="pkg-stale-heartbeat",
    )

    preflight = pending_sync_preflight(
        state,
        [
            {
                "operation": "deploy",
                "payload": {
                    "slot": "vision",
                    "model_id": "model-stale-heartbeat",
                    "package_id": "pkg-stale-heartbeat",
                    "device_id": "edge-1",
                    "runtime_target_id": "temms-x86_64-cpu",
                },
            }
        ],
    )

    entry = preflight["entries"][0]
    assert preflight["status"] == "blocked"
    assert entry["ready"] is False
    assert entry["replay_status"] == "blocked"
    assert entry["reason"].startswith(
        "runtime capability lock status is blocked, expected locked"
    )
    assert "edge inventory freshness is not locked" in entry["reason"]
    assert entry["hub_capability_lock_status"] == "blocked"
    assert len(entry["hub_capability_sha256"]) == 64
    assert entry["hub_capability_telemetry_status"] == "attention"
    assert entry["hub_capability_telemetry_state"] == "telemetry stale"
    assert entry["hub_capability_heartbeat_age_seconds"] > (
        entry["hub_capability_heartbeat_stale_after_seconds"]
    )
    assert entry["hub_runtime_capability_lock"]["status"] == "blocked"
    assert entry["hub_runtime_capability_lock"]["failures"][0].startswith(
        "edge inventory freshness is not locked"
    )


def test_pending_sync_preflight_surfaces_runtime_optimizer_advisory(tmp_path):
    hub = HubLiteStore(tmp_path / "hub.json")
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
            "storage": {"available_mb": 4096.0},
        },
    )
    hub.upsert_package(
        {
            "package_id": "pkg-optimized",
            "name": "optimized-package",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {
                        "id": "model-optimized",
                        "format": "onnx",
                        "filename": "model-optimized.onnx",
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
    _release_package(hub, "pkg-optimized")
    for runtime_target_id in ["cpu-fit", "gpu-fit"]:
        hub.upsert_runtime_target(
            {
                "runtime_target_id": runtime_target_id,
                "image": f"registry.example.com/{runtime_target_id}:latest",
                "device_profiles": ["x86_64-cpu"],
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CPUExecutionProvider"],
                    }
                },
                "runtime_constraints": {
                    "runtimes": ["onnxruntime"],
                    "providers": ["CPUExecutionProvider"],
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
            package_id="pkg-optimized",
            actor="operator:test",
        )
    hub.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-optimized",
            "latency_ms": {"p95": 10.0},
            "throughput": {"inferences_per_second": 100.0},
        },
        device_id="edge-1",
        package_id="pkg-optimized",
        runtime_target_id="cpu-fit",
        actor="edge:edge-1",
    )
    hub.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-optimized",
            "latency_ms": {"p95": 4.0},
            "throughput": {"inferences_per_second": 230.0},
        },
        device_id="edge-1",
        package_id="pkg-optimized",
        runtime_target_id="gpu-fit",
        actor="edge:edge-1",
    )
    state = _pending_preflight_state(
        hub=hub,
        model_id="model-optimized",
        package_id="pkg-optimized",
    )

    preflight = pending_sync_preflight(
        state,
        [
            {
                "operation": "deploy",
                "payload": {
                    "slot": "vision",
                    "model_id": "model-optimized",
                    "package_id": "pkg-optimized",
                    "device_id": "edge-1",
                    "runtime_target_id": "cpu-fit",
                },
            }
        ],
    )

    entry = preflight["entries"][0]
    assert preflight["status"] == "ready"
    assert preflight["optimization_advisories"] == 1
    assert entry["ready"] is True
    assert entry["replay_status"] == "ready_with_runtime_advisory"
    assert entry["hub_readiness_status"] == "attention"
    assert entry["hub_runtime_workbench_schema_version"] == (
        "temms-runtime-workbench/v1"
    )
    assert entry["hub_runtime_workbench_selected_runtime_target_id"] == "cpu-fit"
    assert entry["hub_runtime_workbench_best_runtime_target_id"] == "gpu-fit"
    assert entry["hub_runtime_workbench_target_selection_status"] == (
        "upgrade_available"
    )
    assert entry["hub_runtime_workbench_target_count"] >= 2
    assert entry["hub_runtime_workbench_selected_is_best"] is False
    optimizer_gate = entry["hub_optimization_gates"][0]
    assert optimizer_gate["gate_id"] == "runtime_optimizer"
    assert optimizer_gate["status"] == "attention"
    assert optimizer_gate["refs"]["runtime_target_id"] == "cpu-fit"
    assert optimizer_gate["refs"]["best_runtime_target_id"] == "gpu-fit"
    assert optimizer_gate["actions"][0]["label"] == "Use best runtime"
    assert optimizer_gate["actions"][0]["kind"] == "select_runtime_target"
    assert optimizer_gate["actions"][0]["refs"]["runtime_target_id"] == "gpu-fit"
    assert optimizer_gate["actions"][0]["refs"]["previous_runtime_target_id"] == "cpu-fit"
    assessments = {
        assessment["runtime_target_id"]: assessment
        for assessment in entry["hub_target_assessments"]
    }
    assert assessments["gpu-fit"]["best"] is True
    assert assessments["gpu-fit"]["eligible"] is True
    assert assessments["gpu-fit"]["runtime_capability_lock"]["status"] == "locked"
    assert len(assessments["gpu-fit"]["runtime_capability_lock"]["capability_sha256"]) == 64
    assert assessments["gpu-fit"]["benchmark_id"]
    assert assessments["gpu-fit"]["component_states"]["runtime_validation"]["validation_id"]
    assert assessments["gpu-fit"]["remediation"]["action"] == "use_best_runtime"
    assert assessments["gpu-fit"]["remediation"]["operator_command"][:5] == [
        "uv",
        "run",
        "temms",
        "hub",
        "edge-runtime-mission",
    ]
    assert "gpu-fit" in assessments["gpu-fit"]["remediation"]["operator_command_text"]

    blocked_preflight = pending_sync_preflight(
        state,
        [
            {
                "operation": "deploy",
                "payload": {
                    "slot": "vision",
                    "model_id": "model-optimized",
                    "package_id": "pkg-optimized",
                    "device_id": "edge-1",
                    "runtime_target_id": "temms-rpi5-tflite",
                },
            }
        ],
    )

    blocked_entry = blocked_preflight["entries"][0]
    blocked_optimizer = next(
        gate
        for gate in blocked_entry["hub_blocking_gates"]
        if gate["gate_id"] == "runtime_optimizer"
    )
    assert blocked_preflight["status"] == "blocked"
    assert blocked_entry["ready"] is False
    assert blocked_entry["hub_target_selection_status"] == "selected_not_eligible"
    assert blocked_entry["hub_best_runtime_target_id"] == "gpu-fit"
    assert blocked_entry["hub_runtime_workbench_schema_version"] == (
        "temms-runtime-workbench/v1"
    )
    assert blocked_entry["hub_runtime_workbench_selected_runtime_target_id"] == (
        "temms-rpi5-tflite"
    )
    assert blocked_entry["hub_runtime_workbench_best_runtime_target_id"] == "gpu-fit"
    assert blocked_entry["hub_runtime_workbench_selected_is_best"] is False
    blocked_assessments = {
        assessment["runtime_target_id"]: assessment
        for assessment in blocked_entry["hub_target_assessments"]
    }
    assert blocked_assessments["gpu-fit"]["best"] is True
    assert blocked_assessments["temms-rpi5-tflite"]["status"] == "blocked"
    assert blocked_optimizer["actions"][0]["label"] == "Use best runtime"
    assert blocked_optimizer["actions"][0]["refs"]["runtime_target_id"] == "gpu-fit"
    assert blocked_optimizer["actions"][0]["refs"]["previous_runtime_target_id"] == (
        "temms-rpi5-tflite"
    )

    gpu_assessment = assessments["gpu-fit"]
    gpu_lock = gpu_assessment["runtime_capability_lock"]
    gpu_validation_id = gpu_assessment["component_states"]["runtime_validation"][
        "validation_id"
    ]
    retarget_proof = {
        "schema_version": "temms-ddil-runtime-retarget-proof/v1",
        "status": "proved",
        "runtime_target_id": "gpu-fit",
        "best": True,
        "eligible": True,
        "runtime_fit_score": gpu_assessment["score"],
        "capability_sha256": gpu_lock["capability_sha256"],
        "runtime_capability_lock": gpu_lock,
        "runtime_validation_id": gpu_validation_id,
        "benchmark_id": gpu_assessment["benchmark_id"],
        "target_assessment_sha256": runtime_target_assessment_sha256(gpu_assessment),
    }
    retargeted_payload = {
        "slot": "vision",
        "model_id": "model-optimized",
        "package_id": "pkg-optimized",
        "device_id": "edge-1",
        "runtime_target_id": "gpu-fit",
        "_temms_runtime_retarget": [
            {
                "schema_version": "temms-runtime-retarget/v1",
                "previous_runtime_target_id": "cpu-fit",
                "runtime_target_id": "gpu-fit",
                "runtime_target_proof": retarget_proof,
            }
        ],
    }

    fresh_retarget_preflight = pending_sync_preflight(
        state,
        [{"operation": "deploy", "payload": retargeted_payload}],
    )

    assert fresh_retarget_preflight["status"] == "ready"
    assert fresh_retarget_preflight["entries"][0]["ready"] is True

    hub.upsert_runtime_target(
        {
            "runtime_target_id": "gpu-fit",
            "image": "registry.example.com/gpu-fit:v2",
        }
    )
    stale_retarget_preflight = pending_sync_preflight(
        state,
        [{"operation": "deploy", "payload": retargeted_payload}],
    )
    stale_entry = stale_retarget_preflight["entries"][0]

    assert stale_retarget_preflight["status"] == "blocked"
    assert stale_entry["ready"] is False
    assert stale_entry["replay_status"] == "blocked"
    assert stale_entry["reason"] == (
        "runtime retarget proof is stale: capability hash changed"
    )
    assert stale_entry["hub_runtime_retarget_proof_status"] == "stale_capability_hash"
    assert stale_entry["hub_runtime_retarget_proof_signed_capability_sha256"] == (
        gpu_lock["capability_sha256"]
    )
    assert stale_entry["hub_runtime_retarget_proof_current_capability_sha256"] != (
        gpu_lock["capability_sha256"]
    )
    assert stale_entry["hub_runtime_retarget_proof_signed_validation_id"] == (
        gpu_validation_id
    )
    assert stale_entry["hub_runtime_retarget_proof_current_validation_id"] == (
        gpu_validation_id
    )
    assert stale_entry["hub_runtime_retarget_proof_signed_benchmark_id"] == (
        gpu_assessment["benchmark_id"]
    )
    assert stale_entry["hub_runtime_retarget_proof_current_benchmark_id"] == (
        gpu_assessment["benchmark_id"]
    )


def test_pending_ops_write_failure_preserves_previous_queue(tmp_path, monkeypatch):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue("deploy", {"slot": "vision"})
    previous_payload = store.path.read_text(encoding="utf-8")
    original_replace = type(store.path).replace

    def fail_replace(path, target):
        if target == store.path:
            raise OSError("simulated replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(type(store.path), "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        store.enqueue("rollback", {"slot": "vision"})

    assert store.path.read_text(encoding="utf-8") == previous_payload
    assert [entry["operation"] for entry in store.read_all()] == ["deploy"]
    assert not list(tmp_path.glob(".pending_operations.json-*"))


def _pending_preflight_state(
    *,
    hub: HubLiteStore,
    model_id: str,
    package_id: str,
) -> SimpleNamespace:
    model = SimpleNamespace(id=model_id, package_id=package_id)
    return SimpleNamespace(
        hub_lite=hub,
        slot_manager=SimpleNamespace(get_slot=lambda slot_name: {"name": slot_name}),
        model_cache=SimpleNamespace(
            get_model=lambda candidate: model if candidate == model_id else None,
            find_model=lambda candidate: model if candidate == model_id else None,
        ),
    )


def _release_package(hub: HubLiteStore, package_id: str) -> None:
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
    hub.promote_package(
        package_id,
        "released",
        actor="operator:release",
        reason="released for rollout",
    )


def _set_hub_device_last_seen(
    hub: HubLiteStore,
    device_id: str,
    last_seen_at: datetime,
) -> None:
    data = hub._read()
    data["devices"][device_id]["last_seen_at"] = (
        last_seen_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    hub._write(data)
