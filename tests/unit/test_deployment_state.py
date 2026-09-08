import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from temms.daemon.deployment_state import DeploymentState, DeploymentStateStore
from temms.daemon.pending_ops import (
    PendingOperationsStore,
    pending_operation_signature_status,
    verify_pending_operation_signature,
)
from temms.daemon.pending_preflight import (
    pending_sync_preflight,
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
    _set_hub_device_last_seen(hub, "edge-1", datetime.now(UTC) - timedelta(minutes=10))
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
        last_seen_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    )
    hub._write(data)
