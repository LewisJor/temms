"""
Evidence bundle tests.
"""

import json
from types import SimpleNamespace

from temms.core.cache import ModelFormat
from temms.evidence import (
    EvidenceBundleBuilder,
    _pending_operation_dead_letters,
    build_mission_replay,
    combined_timeline,
    runtime_fit_evidence_timeline,
    summarize_evidence_bundle,
)
from temms.hub_lite import HubLiteStore


def test_evidence_bundle_enriches_decision_with_model_and_package(
    model_cache,
    model_storage,
    slot_manager,
    condition_store,
    policy_engine,
    sample_model_file,
):
    package = model_cache.add_package(
        package_id="pkg-vision",
        name="vision-package",
        version="1.0.0",
        source="/mnt/usb/pkg-vision",
        manifest={"package_id": "pkg-vision", "signature_verified": True},
    )
    dest_path, sha256, size = model_storage.store_model(
        sample_model_file,
        "model-lowlight-v1",
        verify=True,
    )
    model_cache.add_cached_model(
        model_id="model-lowlight-v1",
        name="lowlight",
        version="1.0.0",
        format=ModelFormat.ONNX,
        path=dest_path,
        sha256=sha256,
        size_bytes=size,
        package_id=package.id,
        metadata={"runtime_constraints": {"runtimes": ["onnxruntime"]}},
    )
    slot_manager.create_slot(
        name="vision",
        description="Vision",
        required=True,
        default_model="daylight",
    )
    condition_store.set(
        path="environmental.visibility_m",
        value=40,
        source="operator",
        priority=1000,
    )
    slot_manager.activate_model(
        slot_name="vision",
        model_id="model-lowlight-v1",
        trigger_type="policy",
        trigger_detail="weather-adaptive/fog",
        conditions=condition_store.get_snapshot(),
    )

    bundle = EvidenceBundleBuilder(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
    ).build(slot_name="vision")

    assert bundle["schema_version"] == "temms-evidence-bundle/v1"
    assert bundle["integrity"]["payload_sha256"]
    assert len(bundle["decisions"]) == 1
    decision = bundle["decisions"][0]
    assert decision["to_model"] == "model-lowlight-v1"
    assert decision["conditions_snapshot"]["environmental"]["visibility_m"] == 40
    assert decision["model_evidence"]["to_model"]["sha256"] == sha256
    assert decision["model_evidence"]["to_package"]["manifest"]["signature_verified"] is True

    # The bundle is portable JSON, not a Python-only object graph.
    json.dumps(bundle)


def test_summarize_evidence_bundle_replays_product_evidence():
    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "exported_at": "2026-06-11T12:00:00Z",
        "integrity": {"payload_sha256": "abc123"},
        "runtime": {
            "offline_mode": True,
            "pending_operation_signature_required": True,
            "pending_operation_signing_key_configured": True,
            "pending_operation_verification": {
                "total": 2,
                "verified": 1,
                "invalid": 0,
                "missing_signature": 1,
                "key_unavailable": 0,
                "unsigned_allowed": 0,
                "statuses": {"verified": 1, "missing_signature": 1},
            },
            "pending_operations": [
                {
                    "operation": "deploy",
                    "recorded_at": "2026-06-11T12:02:00Z",
                    "verification": {
                        "status": "verified",
                        "verified": True,
                        "reason": "signature verified",
                    },
                    "preflight": {
                        "replay_status": "superseded",
                        "ready": True,
                        "reason": "superseded by deploy intent at index 1 for slot vision",
                        "resolved_model_id": "lowlight",
                        "superseded": True,
                        "superseded_by_index": 1,
                        "superseded_by_model_id": "daylight",
                        "final_for_slot": False,
                        "hub_runtime_fit_score": 91,
                        "hub_runtime_fit_tier": "optimal",
                        "hub_runtime_fit_detail": "91/100 optimal runtime fit",
                        "hub_runtime_lane_id": "cpu-onnx",
                        "hub_runtime_lane_label": "CPU portable",
                        "hub_runtime_lane_engine": "onnxruntime",
                        "hub_runtime_lane_acceleration": "cpu",
                        "hub_artifact_lane_status": "go",
                        "hub_artifact_lane_state": "native artifact",
                        "hub_artifact_lane_detail": "onnx artifact is native for CPU portable",
                        "hub_artifact_format": "onnx",
                        "hub_target_selection_status": "best",
                        "hub_best_runtime_target_id": "temms-x86_64-cpu",
                        "hub_runtime_score_delta": 0.0,
                        "hub_production_admission_status": "go",
                        "hub_production_apply_allowed": True,
                    },
                    "signature": {
                        "schema_version": "temms-pending-operation-signature/v1",
                        "algorithm": "HMAC-SHA256",
                        "signed_at": "2026-06-11T12:02:01Z",
                        "signer": "temms-ddil",
                        "key_fingerprint": "sha256:abc123",
                        "payload_sha256": "digest-from-signed-envelope",
                        "signature": "signature-value",
                    },
                    "payload": {
                        "actor": "operator:model-deployment-control",
                        "slot": "vision",
                        "device_id": "edge-1",
                        "package_id": "pkg-canonical",
                        "model_id": "lowlight",
                        "runtime_target_id": "temms-x86_64-cpu",
                        "signing_key": "secret-value",
                    },
                },
                {
                    "operation": "override_model",
                    "recorded_at": "2026-06-11T12:03:00Z",
                    "verification": {
                        "status": "missing_signature",
                        "verified": False,
                        "reason": "signature required",
                    },
                    "payload": {
                        "slot_name": "vision",
                        "request": {
                            "model": "daylight",
                            "actor": "operator:field",
                            "reason": "route clears",
                        },
                    },
                },
            ],
            "pending_operation_dead_letters": [
                {
                    "schema_version": "temms-pending-operation-dead-letter/v1",
                    "quarantined_at": "2026-06-11T12:05:00Z",
                    "actor": "operator:model-deployment-control",
                    "reason": "blocked preflight",
                    "operation": "deploy",
                    "slot": "vision",
                    "device_id": "edge-1",
                    "package_id": "pkg-canonical",
                    "model_id": "missing-model",
                    "runtime_target_id": "temms-x86_64-cpu",
                    "payload_sha256": "deadbeef",
                    "signature_status": "verified",
                    "signature_verified": True,
                    "replay_status": "blocked",
                    "replay_ready": False,
                    "replay_reason": "model not found: missing-model",
                    "runtime_fit_score": 0,
                    "runtime_fit_tier": "blocked",
                    "runtime_lane_label": "CPU portable",
                    "runtime_lane_acceleration": "cpu",
                    "artifact_lane_state": "blocked",
                    "artifact_lane_detail": (
                        "tflite artifact is not compatible with CPU portable"
                    ),
                    "production_apply_allowed": False,
                    "summary": "missing-model to edge-1",
                },
                {
                    "schema_version": "temms-pending-operation-dead-letter/v1",
                    "quarantined_at": "2026-06-11T12:06:00Z",
                    "actor": "operator:model-deployment-control",
                    "reason": "blocked preflight",
                    "acknowledged": True,
                    "acknowledged_at": "2026-06-11T12:07:00Z",
                    "acknowledged_by": "operator:model-deployment-control",
                    "acknowledgement_reason": "reviewed by operator",
                    "operation": "deploy",
                    "slot": "vision",
                    "device_id": "edge-1",
                    "model_id": "old-missing-model",
                    "payload_sha256": "cafef00d",
                    "signature_status": "verified",
                    "signature_verified": True,
                    "replay_status": "blocked",
                    "replay_ready": False,
                    "replay_reason": "model not found: old-missing-model",
                    "summary": "old-missing-model to edge-1",
                }
            ],
        },
        "deployment_state": {"state": "OFFLINE", "reason": "field link down"},
        "slots": [
            {
                "name": "vision",
                "state": "READY",
                "active_model_id": "daylight",
                "default_model": "daylight",
                "operator_override": {
                    "model_id": "daylight",
                    "reason": "operator override after route clears",
                },
            }
        ],
        "models": [
            {"id": "daylight", "name": "yolov8-daylight"},
            {"id": "lowlight", "name": "yolov8-lowlight"},
            {"id": "tiny", "name": "mobilenet-tiny"},
            {"id": "faulty", "name": "yolov8-faulty"},
        ],
        "packages": [{"id": "pkg-canonical", "manifest": {"signature_verified": True}}],
        "decisions": [
            {
                "slot": "vision",
                "from_model": "faulty",
                "to_model": "lowlight",
                "trigger_type": "fallback",
                "trigger_detail": "fallback after simulated-load-failure",
                "created_at": "2026-06-11T12:04:00Z",
                "audit_metadata": {
                    "package_id": "pkg-canonical",
                    "package": {"signature_verified": True},
                    "fallback": {
                        "selected_model": "faulty",
                        "attempted": ["lowlight"],
                        "failures": ["faulty: simulated load failure"],
                    },
                },
            },
            {
                "slot": "vision",
                "from_model": "tiny",
                "to_model": "daylight",
                "trigger_type": "operator",
                "trigger_detail": "operator override after route clears",
                "created_at": "2026-06-11T12:05:00Z",
                "audit_metadata": {
                    "package_id": "pkg-canonical",
                    "package": {"signature_verified": True},
                },
            },
        ],
        "rollout_events": [
            {
                "rollout_id": "rollout-canonical",
                "device_id": "edge-1",
                "package_id": "pkg-canonical",
                "slot": "vision",
                "state": "approved",
                "actor": "operator:approver",
                "detail": "mission policy approved",
                "updated_at": "2026-06-11T12:00:45Z",
            },
            {
                "rollout_id": "rollout-canonical",
                "state": "activated",
                "updated_at": "2026-06-11T12:01:00Z",
            },
        ],
        "runtime_validations": [
            {
                "package_id": "pkg-canonical",
                "runtime_target_id": "edge-cpu",
                "created_at": "2026-06-11T12:00:30Z",
                "result": {
                    "ok": True,
                    "dry_run": False,
                    "stdout": '{"schema_version":"temms-local-runtime-validation/v1"}',
                },
            }
        ],
        "runtime_fit_evidence": [
            {
                "schema_version": "temms-runtime-fit-evidence/v1",
                "checked_at": "2026-06-11T12:00:35Z",
                "readiness_status": "go",
                "readiness_headline": "Deployment loop is ready",
                "selection": {
                    "package_id": "pkg-canonical",
                    "model_id": "lowlight",
                    "device_id": "edge-1",
                    "runtime_target_id": "edge-cpu",
                    "slot": "vision",
                    "rollout_id": "rollout-canonical",
                },
                    "runtime_fit": {
                        "schema_version": "temms-runtime-fit/v1",
                        "score": 91,
                        "tier": "optimal",
                        "detail": "91/100 optimal runtime fit for lowlight on edge-1 via edge-cpu",
                        "runtime_lane": {
                            "schema_version": "temms-runtime-lane/v1",
                            "lane_id": "cpu-onnx",
                            "label": "CPU portable",
                            "execution_engine": "onnxruntime",
                            "acceleration": "cpu",
                        },
                        "artifact_lane": {
                            "schema_version": "temms-artifact-lane/v1",
                            "status": "go",
                            "state": "native artifact",
                            "detail": "onnx artifact is native for CPU portable",
                            "model_format": "onnx",
                        },
                        "components": {
                            "compatibility": {
                                "score": 25,
                                "max_score": 25,
                            "state": "compatible",
                        },
                        "runtime_validation": {
                            "score": 20,
                            "max_score": 20,
                            "state": "validated",
                        },
                        "performance": {
                            "score": 22,
                            "max_score": 25,
                            "state": "slo met",
                        },
                        "resource": {
                            "score": 16,
                            "max_score": 20,
                            "state": "met",
                        },
                        "telemetry": {
                            "score": 8,
                            "max_score": 10,
                            "state": "telemetry fresh",
                        },
                    },
                    "target_selection": {
                        "status": "best",
                        "selected_rank": 1,
                        "selected_score": 91,
                        "best_runtime_target_id": "edge-cpu",
                        "best_score": 91,
                        "score_delta": 0,
                    },
                },
                "runtime_optimizer_gate": {
                    "gate_id": "runtime_optimizer",
                    "status": "go",
                    "state": "best target",
                    "detail": "edge-cpu is the highest-scoring eligible target",
                },
            }
        ],
        "package_imports": [
            {
                "package_id": "pkg-canonical",
                "signature_verified": True,
                "imported_at": "2026-06-11T12:00:20Z",
            }
        ],
        "package_promotions": [
            {
                "package_id": "pkg-canonical",
                "state": "released",
                "from_state": "approved",
                "actor": "operator:release",
                "reason": "mission package released",
                "updated_at": "2026-06-11T12:00:40Z",
                "evidence": {"validation_id": "validation-canonical"},
            }
        ],
        "telemetry": {"count": 3, "events": []},
        "timeline": [
            {
                "kind": "runtime_validation",
                "timestamp": "2026-06-11T12:00:30Z",
                "summary": "pkg-canonical passed on edge-cpu",
            },
            {
                "kind": "decision",
                "timestamp": "2026-06-11T12:04:00Z",
                "slot": "vision",
                "summary": "faulty -> lowlight (fallback)",
            },
        ],
    }

    summary = summarize_evidence_bundle(bundle, limit=5)

    assert summary["schema_version"] == "temms-evidence-summary/v1"
    assert "fallback recovery" in summary["headline"]
    assert "operator override" in summary["headline"]
    assert "approval gate" in summary["headline"]
    assert summary["runtime"]["offline_mode"] is True
    assert summary["runtime"]["deployment_state"]["state"] == "OFFLINE"
    assert summary["runtime"]["pending_operation_signature_required"] is True
    assert summary["runtime"]["pending_operation_signing_key_configured"] is True
    assert summary["runtime"]["pending_operation_verification"]["verified"] == 1
    assert summary["runtime"]["pending_operation_verification"]["missing_signature"] == 1
    assert summary["runtime"]["pending_operation_dead_letters_count"] == 2
    assert summary["runtime"]["pending_operation_dead_letters_unresolved_count"] == 1
    assert summary["runtime"]["pending_operation_dead_letters_acknowledged_count"] == 1
    assert summary["runtime"]["pending_operation_dead_letters"][0]["replay_status"] == "blocked"
    assert summary["runtime"]["pending_operation_dead_letters"][0]["acknowledged"] is False
    assert summary["runtime"]["pending_operation_dead_letters"][0]["runtime_lane_label"] == (
        "CPU portable"
    )
    assert summary["runtime"]["pending_operation_dead_letters"][0]["artifact_lane_state"] == (
        "blocked"
    )
    assert (
        summary["runtime"]["pending_operation_dead_letters"][0]["artifact_lane_detail"]
        == "tflite artifact is not compatible with CPU portable"
    )
    assert summary["runtime"]["pending_operation_dead_letters"][0]["production_apply_allowed"] is False
    assert summary["runtime"]["pending_operation_dead_letters"][0]["summary"] == (
        "missing-model to edge-1"
    )
    assert summary["runtime"]["pending_operation_dead_letters"][1]["acknowledged"] is True
    assert (
        summary["runtime"]["pending_operation_dead_letters"][1]["acknowledged_by"]
        == "operator:model-deployment-control"
    )
    assert summary["runtime"]["pending_operations_count"] == 2
    assert summary["runtime"]["pending_operation_types"] == [
        "deploy",
        "override_model",
    ]
    pending = summary["runtime"]["pending_operations"]
    assert pending[0]["operation"] == "deploy"
    assert pending[0]["summary"] == "lowlight to edge-1"
    assert pending[0]["actor"] == "operator:model-deployment-control"
    assert pending[0]["package_id"] == "pkg-canonical"
    assert pending[0]["signature_present"] is True
    assert pending[0]["signature_algorithm"] == "HMAC-SHA256"
    assert pending[0]["signature_signer"] == "temms-ddil"
    assert pending[0]["signature_key_fingerprint"] == "sha256:abc123"
    assert pending[0]["signature_status"] == "verified"
    assert pending[0]["signature_verified"] is True
    assert pending[0]["signature_verification_reason"] == "signature verified"
    assert pending[0]["replay_status"] == "superseded"
    assert pending[0]["superseded"] is True
    assert pending[0]["superseded_by_index"] == 1
    assert pending[0]["superseded_by_model_id"] == "daylight"
    assert pending[0]["final_for_slot"] is False
    assert pending[0]["runtime_fit_score"] == 91
    assert pending[0]["runtime_fit_tier"] == "optimal"
    assert pending[0]["runtime_lane_label"] == "CPU portable"
    assert pending[0]["runtime_lane_acceleration"] == "cpu"
    assert pending[0]["artifact_lane_state"] == "native artifact"
    assert pending[0]["artifact_lane_detail"] == "onnx artifact is native for CPU portable"
    assert pending[0]["production_apply_allowed"] is True
    assert len(pending[0]["payload_sha256"]) == 64
    assert "secret-value" not in json.dumps(summary)
    assert pending[1]["summary"] == "override vision to daylight"
    assert pending[1]["signature_status"] == "missing_signature"
    assert pending[1]["signature_verified"] is False
    assert summary["counts"]["decisions"] == 2
    assert summary["counts"]["runtime_validations"] == 1
    assert summary["counts"]["runtime_fit_evidence"] == 1
    runtime_fit = summary["runtime"]["runtime_fit_evidence"][0]
    assert runtime_fit["score"] == 91
    assert runtime_fit["tier"] == "optimal"
    assert runtime_fit["runtime_lane_label"] == "CPU portable"
    assert runtime_fit["runtime_lane_acceleration"] == "cpu"
    assert runtime_fit["artifact_lane_state"] == "native artifact"
    assert runtime_fit["artifact_lane_detail"] == "onnx artifact is native for CPU portable"
    assert runtime_fit["components"]["performance"]["score"] == 22
    assert runtime_fit["target_selection_status"] == "best"
    assert summary["trust"]["signed_package_imports"] == 1
    assert summary["trust"]["runtime_validations_passed_non_dry_run"] == 1
    assert summary["trust"]["local_runtime_validations"] == 1
    assert summary["trust"]["released_packages"] == 1
    assert summary["package_promotions"][0]["state"] == "released"
    assert summary["counts"]["package_promotions"] == 1
    assert summary["active_slots"][0]["operator_override"] is True
    assert summary["approvals"][0]["rollout_id"] == "rollout-canonical"
    assert summary["approvals"][0]["actor"] == "operator:approver"
    assert summary["approvals"][0]["reason"] == "mission policy approved"
    assert summary["fallbacks"][0]["failed_model"] == "faulty"
    assert summary["fallbacks"][0]["activated_model"] == "lowlight"
    assert summary["operator_overrides"][0]["to_model"] == "daylight"
    assert summary["timeline"][0]["kind"] == "runtime_validation"

    replay = build_mission_replay(bundle, limit=5)
    phases = {phase["phase"]: phase for phase in replay["phases"]}

    assert replay["schema_version"] == "temms-mission-replay/v1"
    assert replay["headline"] == summary["headline"]
    assert phases["signed_package"]["status"] == "complete"
    assert phases["runtime_validation"]["status"] == "complete"
    assert phases["runtime_fit"]["status"] == "complete"
    assert "91/100 optimal" in phases["runtime_fit"]["summary"]
    assert phases["package_release"]["status"] == "complete"
    assert phases["policy_approval"]["status"] == "complete"
    assert phases["fallback_rollback"]["status"] == "complete"
    assert phases["operator_override"]["status"] == "complete"
    assert phases["offline_operation"]["status"] == "complete"
    assert replay["incidents"]["approvals"][0]["actor"] == "operator:approver"
    assert replay["incidents"]["fallbacks"][0]["failed_model"] == "faulty"
    assert replay["outcome"]["completed_phases"] >= 6
    assert replay["events"][0]["sequence"] == 1
    assert replay["events"][0]["timestamp"] == "2026-06-11T12:00:30Z"
    assert replay["events"][-1]["timestamp"] == "2026-06-11T12:04:00Z"

    json.dumps(summary)
    json.dumps(replay)


def test_mission_replay_counts_resolved_ddil_as_offline_proof():
    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "runtime": {
            "offline_mode": False,
            "pending_operations": [],
            "pending_operation_dead_letters": [
                {
                    "schema_version": "temms-pending-operation-dead-letter/v1",
                    "quarantined_at": "2026-06-25T18:20:00Z",
                    "actor": "operator:model-deployment-control",
                    "reason": "blocked preflight",
                    "acknowledged": True,
                    "acknowledged_at": "2026-06-25T18:40:00Z",
                    "acknowledged_by": "operator:model-deployment-control",
                    "acknowledgement_reason": "reviewed before demo",
                    "operation": "deploy",
                    "slot": "vision",
                    "device_id": "edge-sim",
                    "model_id": "missing-model",
                    "payload_sha256": "deadbeef",
                    "replay_status": "blocked",
                    "replay_ready": False,
                }
            ],
        },
        "deployment_state": {
            "state": "READY",
            "reason": "activated model-yolov8-lowlight-001",
        },
        "slots": [
            {
                "name": "vision",
                "state": "running",
                "active_model_id": "model-yolov8-lowlight-001",
                "default_model": "model-yolov8-daylight-001",
                "operator_override": {
                    "model_id": "model-yolov8-lowlight-001",
                    "reason": "DDIL replay selected mission model",
                },
            }
        ],
        "timeline": [
            {
                "kind": "telemetry",
                "timestamp": "2026-06-25T18:40:00Z",
                "summary": "pending_operations.dead_letters_acknowledged",
            }
        ],
    }

    replay = build_mission_replay(bundle, limit=5)
    phases = {phase["phase"]: phase for phase in replay["phases"]}

    assert phases["offline_operation"]["status"] == "complete"
    assert phases["offline_operation"]["summary"] == (
        "1 quarantined DDIL intents retained; 1 acknowledged"
    )
    assert phases["offline_operation"]["evidence_refs"] == ["deadbeef"]
    assert phases["operator_override"]["status"] == "complete"
    assert phases["operator_override"]["evidence_refs"] == ["model-yolov8-lowlight-001"]
    assert replay["events"][0]["phase"] == "offline_operation"
    assert replay["outcome"]["incomplete_phases"].count("offline_operation") == 0


def test_dead_letter_summary_preserves_edge_runtime_remediation_command():
    record = {
        "schema_version": "temms-pending-operation-dead-letter/v1",
        "quarantined_at": "2026-06-26T18:22:00Z",
        "actor": "operator:field",
        "reason": "blocked preflight",
        "payload_sha256": "deadbeef",
        "entry": {
            "operation": "deploy",
            "recorded_at": "2026-06-26T18:20:00Z",
            "payload": {
                "actor": "operator:field",
                "slot": "vision",
                "device_id": "edge-sim",
                "package_id": "pkg-vision",
                "model_id": "model-lowlight",
                "runtime_target_id": "temms-x86_64-cpu",
            },
        },
        "preflight": {
            "operation": "deploy",
            "replay_status": "blocked",
            "ready": False,
            "reason": "hub readiness blocks replay",
            "runtime_target_id": "temms-x86_64-cpu",
            "hub_runtime_fit_score": 88,
            "hub_runtime_fit_tier": "degraded",
            "hub_best_runtime_target_id": "temms-x86_64-cpu",
            "hub_capability_lock_status": "blocked",
            "hub_capability_runtime_target_id": "temms-x86_64-cpu",
            "hub_edge_execution_contract_action": "collect_evidence",
            "hub_target_assessments": [
                {
                    "runtime_target_id": "temms-x86_64-cpu",
                    "selected": True,
                    "best": True,
                    "status": "eligible",
                    "remediation": {
                        "action": "refresh_edge_inventory",
                        "label": "Refresh edge inventory",
                        "requires_edge_execution": True,
                        "edge_command": [
                            "env",
                            "TEMMS_HUB_URL=${TEMMS_HUB_URL}",
                            "TEMMS_DEVICE_ID=edge-sim",
                            "temms",
                            "daemon",
                            "start",
                            "--foreground",
                        ],
                        "edge_command_note": (
                            "Run on the edge node to refresh runtime/provider inventory."
                        ),
                    },
                }
            ],
        },
    }
    state = SimpleNamespace(
        pending_operations=SimpleNamespace(read_dead_letter=lambda: [record])
    )

    dead_letters = _pending_operation_dead_letters(state)

    assert dead_letters[0]["summary"] == "model-lowlight to edge-sim"
    assert dead_letters[0]["runtime_remediation_contract_runtime_target_id"] == (
        "temms-x86_64-cpu"
    )
    assert dead_letters[0]["runtime_remediation_contract_action"] == (
        "refresh_edge_inventory"
    )
    assert dead_letters[0]["runtime_remediation_contract_label"] == (
        "Refresh edge inventory"
    )
    assert dead_letters[0]["runtime_remediation_contract_kind"] == "edge"
    assert dead_letters[0]["runtime_remediation_contract_requires_edge_execution"] is True
    assert "TEMMS_DEVICE_ID=edge-sim" in dead_letters[0][
        "runtime_remediation_contract_command_text"
    ]
    assert "temms daemon start --foreground" in dead_letters[0][
        "runtime_remediation_contract_command_text"
    ]
    assert dead_letters[0]["runtime_remediation_contract_command_note"] == (
        "Run on the edge node to refresh runtime/provider inventory."
    )


def test_evidence_summary_counts_requeued_dead_letters_as_resolved():
    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "runtime": {
            "pending_operation_dead_letters": [
                {
                    "schema_version": "temms-pending-operation-dead-letter/v1",
                    "payload_sha256": "deadbeef",
                    "operation": "deploy",
                    "replay_status": "blocked",
                    "requeued": True,
                    "requeued_at": "2026-06-26T18:30:00Z",
                    "requeued_by": "operator:field",
                    "requeue_reason": "edge inventory refreshed",
                },
                {
                    "schema_version": "temms-pending-operation-dead-letter/v1",
                    "payload_sha256": "cafef00d",
                    "operation": "deploy",
                    "replay_status": "blocked",
                },
            ]
        },
    }

    summary = summarize_evidence_bundle(bundle)
    runtime = summary["runtime"]

    assert runtime["pending_operation_dead_letters_count"] == 2
    assert runtime["pending_operation_dead_letters_unresolved_count"] == 1
    assert runtime["pending_operation_dead_letters_requeued_count"] == 1
    assert runtime["pending_operation_dead_letters"][0]["requeued"] is True
    assert runtime["pending_operation_dead_letters"][0]["requeued_by"] == "operator:field"


def test_pending_operation_summary_preserves_runtime_remediation_action():
    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "runtime": {
            "pending_operations": [
                {
                    "operation": "deploy",
                    "recorded_at": "2026-06-26T17:52:36Z",
                    "verification": {"status": "verified", "verified": True},
                    "payload": {
                        "actor": "operator:field",
                        "slot": "vision",
                        "device_id": "edge-sim",
                        "package_id": "pkg-vision",
                        "model_id": "model-lowlight",
                        "runtime_target_id": "temms-rpi5-tflite",
                    },
                    "preflight": {
                        "replay_status": "blocked",
                        "ready": False,
                        "reason": "hub readiness blocks replay",
                        "hub_runtime_fit_score": 38,
                        "hub_runtime_fit_tier": "blocked",
                        "hub_best_runtime_target_id": "temms-x86_64-cpu",
                        "hub_runtime_score_delta": 60.0,
                        "hub_production_apply_allowed": False,
                        "hub_capability_lock_status": "blocked",
                        "hub_capability_sha256": "a" * 64,
                        "hub_capability_runtime_target_id": "temms-rpi5-tflite",
                        "hub_capability_edge_profile": "raspberry-pi-5",
                        "hub_capability_telemetry_status": "attention",
                        "hub_capability_telemetry_state": "telemetry stale",
                        "hub_capability_telemetry_detail": (
                            "last heartbeat was 10 minutes ago; freshness budget is 5 minutes"
                        ),
                        "hub_capability_heartbeat_age_seconds": 600,
                        "hub_capability_heartbeat_stale_after_seconds": 300,
                        "hub_capability_failures": [
                            "edge inventory freshness is not locked: last heartbeat was 10 minutes ago"
                        ],
                        "hub_edge_execution_contract_status": "attention",
                        "hub_edge_execution_contract_action": "use_best_runtime",
                        "hub_target_assessments": [
                            {
                                "runtime_target_id": "temms-rpi5-tflite",
                                "selected": True,
                                "best": False,
                                "status": "blocked",
                                "remediation": {
                                    "action": "select_matching_edge_class",
                                    "label": "Use matching edge class",
                                    "requires_edge_execution": False,
                                    "operator_command": [
                                        "uv",
                                        "run",
                                        "temms",
                                        "hub",
                                        "compatibility-matrix",
                                        "--hub-url",
                                        "${TEMMS_HUB_URL}",
                                        "--device-id",
                                        "edge-sim",
                                        "--package-id",
                                        "pkg-vision",
                                        "--model-id",
                                        "model-lowlight",
                                        "--runtime-target-id",
                                        "temms-rpi5-tflite",
                                        "--include-device-inventory",
                                        "--json",
                                    ],
                                },
                            },
                            {
                                "runtime_target_id": "temms-x86_64-cpu",
                                "selected": False,
                                "best": True,
                                "status": "eligible",
                                "remediation": {
                                    "action": "use_best_runtime",
                                    "label": "Use best runtime",
                                    "requires_edge_execution": False,
                                    "operator_command": [
                                        "uv",
                                        "run",
                                        "temms",
                                        "hub",
                                        "edge-runtime-mission",
                                        "--hub-url",
                                        "${TEMMS_HUB_URL}",
                                        "--package-id",
                                        "pkg-vision",
                                        "--model-id",
                                        "model-lowlight",
                                        "--device-id",
                                        "edge-sim",
                                        "--runtime-target-id",
                                        "temms-x86_64-cpu",
                                        "--slot",
                                        "vision",
                                        "--require-go",
                                        "--require-best-runtime",
                                        "--require-capability-lock",
                                        "--min-runtime-fit",
                                        "95",
                                        "--json",
                                    ],
                                    "operator_command_note": (
                                        "Re-check this runtime path against the signed "
                                        "edge-runtime gate."
                                    ),
                                },
                            },
                        ],
                        "hub_blocking_gates": [
                            {
                                "gate_id": "runtime_optimizer",
                                "label": "Runtime optimizer",
                                "status": "blocked",
                                "state": "selected not eligible",
                                "detail": "Use the measured CPU runtime target",
                                "refs": {
                                    "runtime_target_id": "temms-rpi5-tflite",
                                    "best_runtime_target_id": "temms-x86_64-cpu",
                                    "score_delta": 60.0,
                                },
                                "actions": [
                                    {
                                        "action_id": "select_best_runtime_target",
                                        "label": "Use best runtime",
                                        "kind": "select_runtime_target",
                                        "refs": {
                                            "runtime_target_id": "temms-x86_64-cpu",
                                            "previous_runtime_target_id": "temms-rpi5-tflite",
                                            "best_runtime_target_id": "temms-x86_64-cpu",
                                            "score_delta": 60.0,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            ]
        },
    }

    summary = summarize_evidence_bundle(bundle)
    pending = summary["runtime"]["pending_operations"][0]

    assert pending["runtime_optimizer_status"] == "blocked"
    assert pending["runtime_remediation_label"] == "Use best runtime"
    assert pending["runtime_remediation_kind"] == "select_runtime_target"
    assert pending["runtime_remediation_runtime_target_id"] == "temms-x86_64-cpu"
    assert pending["runtime_remediation_previous_runtime_target_id"] == (
        "temms-rpi5-tflite"
    )
    assert pending["runtime_remediation_score_delta"] == 60.0
    assert pending["runtime_capability_lock_status"] == "blocked"
    assert pending["runtime_capability_sha256"] == "a" * 64
    assert pending["runtime_capability_edge_profile"] == "raspberry-pi-5"
    assert pending["runtime_capability_telemetry_state"] == "telemetry stale"
    assert pending["runtime_capability_heartbeat_age_seconds"] == 600
    assert pending["runtime_capability_heartbeat_stale_after_seconds"] == 300
    assert pending["edge_execution_contract_action"] == "use_best_runtime"
    assert pending["runtime_remediation_contract_runtime_target_id"] == (
        "temms-x86_64-cpu"
    )
    assert pending["runtime_remediation_contract_action"] == "use_best_runtime"
    assert pending["runtime_remediation_contract_label"] == "Use best runtime"
    assert pending["runtime_remediation_contract_kind"] == "operator"
    assert pending["runtime_remediation_contract_requires_edge_execution"] is False
    assert "edge-runtime-mission" in pending[
        "runtime_remediation_contract_command_text"
    ]
    assert "temms-x86_64-cpu" in pending["runtime_remediation_contract_command_text"]
    assert pending["runtime_remediation_contract_command_note"] == (
        "Re-check this runtime path against the signed edge-runtime gate."
    )


def test_pending_operation_summary_preserves_runtime_retarget_audit():
    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "runtime": {
            "pending_operations": [
                {
                    "operation": "deploy",
                    "recorded_at": "2026-06-26T18:03:00Z",
                    "verification": {"status": "verified", "verified": True},
                    "payload": {
                        "actor": "operator:field",
                        "slot": "vision",
                        "model_id": "model-lowlight",
                        "runtime_target_id": "temms-x86_64-cpu",
                        "_temms_runtime_retarget": [
                            {
                                "schema_version": "temms-runtime-retarget/v1",
                                "retargeted_at": "2026-06-26T18:02:10Z",
                                "actor": "operator:model-deployment-control",
                                "reason": "operator selected measured best runtime target",
                                "previous_runtime_target_id": "temms-rpi5-tflite",
                                "runtime_target_id": "temms-x86_64-cpu",
                                "previous_payload_sha256": "old-digest",
                                "runtime_target_proof": {
                                    "schema_version": "temms-ddil-runtime-retarget-proof/v1",
                                    "status": "proved",
                                    "runtime_target_id": "temms-x86_64-cpu",
                                    "best": True,
                                    "eligible": True,
                                    "runtime_fit_score": 98.0,
                                    "runtime_validation_id": "runtime-validation-abc123",
                                    "benchmark_id": "benchmark-def456",
                                    "target_assessment_sha256": "c" * 64,
                                    "latency_ms_p95": 8.0,
                                    "throughput_ips": 125.0,
                                    "runtime_capability_lock": {
                                        "status": "locked",
                                        "capability_sha256": "b" * 64,
                                        "runtime_mode": "runtime_target",
                                    },
                                    "telemetry_freshness": {
                                        "status": "go",
                                        "state": "telemetry fresh",
                                        "heartbeat_age_seconds": 12,
                                        "heartbeat_stale_after_seconds": 300,
                                    },
                                    "runtime_workbench_schema_version": (
                                        "temms-runtime-workbench/v1"
                                    ),
                                    "runtime_workbench_status": "go",
                                    "runtime_workbench_target_selection_status": "best",
                                    "runtime_workbench_previous_selected_runtime_target_id": (
                                        "temms-rpi5-tflite"
                                    ),
                                    "runtime_workbench_selected_runtime_target_id": (
                                        "temms-x86_64-cpu"
                                    ),
                                    "runtime_workbench_best_runtime_target_id": (
                                        "temms-x86_64-cpu"
                                    ),
                                    "runtime_workbench_target_count": 4,
                                    "runtime_workbench_eligible_target_count": 1,
                                    "runtime_workbench_blocked_target_count": 3,
                                    "runtime_workbench_selected_is_best": True,
                                },
                            }
                        ],
                    },
                    "preflight": {
                        "replay_status": "ready",
                        "ready": True,
                    },
                }
            ]
        },
    }

    summary = summarize_evidence_bundle(bundle)
    pending = summary["runtime"]["pending_operations"][0]

    assert pending["runtime_retargeted_at"] == "2026-06-26T18:02:10Z"
    assert pending["runtime_retargeted_by"] == "operator:model-deployment-control"
    assert pending["runtime_retarget_reason"] == "operator selected measured best runtime target"
    assert pending["runtime_retargeted_from"] == "temms-rpi5-tflite"
    assert pending["runtime_retargeted_to"] == "temms-x86_64-cpu"
    assert pending["runtime_retarget_proof_status"] == "proved"
    assert pending["runtime_retarget_runtime_fit_score"] == 98.0
    assert pending["runtime_retarget_capability_lock_status"] == "locked"
    assert pending["runtime_retarget_capability_sha256"] == "b" * 64
    assert pending["runtime_retarget_validation_id"] == "runtime-validation-abc123"
    assert pending["runtime_retarget_benchmark_id"] == "benchmark-def456"
    assert pending["runtime_retarget_target_assessment_sha256"] == "c" * 64
    assert pending["runtime_retarget_heartbeat_age_seconds"] == 12
    assert pending["runtime_retarget_workbench_schema_version"] == (
        "temms-runtime-workbench/v1"
    )
    assert pending["runtime_retarget_workbench_selected_runtime_target_id"] == (
        "temms-x86_64-cpu"
    )
    assert pending[
        "runtime_retarget_workbench_previous_selected_runtime_target_id"
    ] == "temms-rpi5-tflite"
    assert pending["runtime_retarget_workbench_best_runtime_target_id"] == (
        "temms-x86_64-cpu"
    )
    assert pending["runtime_retarget_workbench_target_count"] == 4
    assert pending["runtime_retarget_workbench_selected_is_best"] is True


def test_replayed_decision_summary_preserves_runtime_retarget_audit():
    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "exported_at": "2026-06-26T18:20:00Z",
        "runtime": {
            "offline_mode": False,
            "pending_operations": [],
            "pending_operation_dead_letters": [],
        },
        "models": [
            {
                "id": "model-lowlight",
                "name": "lowlight",
            }
        ],
        "decisions": [
            {
                "slot": "vision",
                "from_model": None,
                "to_model": "model-lowlight",
                "trigger_type": "deploy",
                "trigger_detail": "ddil-retarget-sync",
                "created_at": "2026-06-26T18:19:00Z",
                "audit_metadata": {
                    "package_id": "pkg-vision",
                    "runtime_target_id": "temms-x86_64-cpu",
                    "ddil_runtime_retarget": {
                        "schema_version": "temms-ddil-runtime-retarget-audit/v1",
                        "retargeted_at": "2026-06-26T18:18:30Z",
                        "actor": "operator:model-deployment-control",
                        "reason": "operator selected measured best runtime target",
                        "previous_runtime_target_id": "temms-rpi5-tflite",
                        "runtime_target_id": "temms-x86_64-cpu",
                        "previous_payload_sha256": "bad-runtime-digest",
                        "latest": {
                            "retargeted_at": "2026-06-26T18:18:30Z",
                            "actor": "operator:model-deployment-control",
                            "reason": "operator selected measured best runtime target",
                            "previous_runtime_target_id": "temms-rpi5-tflite",
                            "runtime_target_id": "temms-x86_64-cpu",
                            "previous_payload_sha256": "bad-runtime-digest",
                            "runtime_target_proof": {
                                "schema_version": "temms-ddil-runtime-retarget-proof/v1",
                                "status": "proved",
                                "runtime_target_id": "temms-x86_64-cpu",
                                "best": True,
                                "eligible": True,
                                "runtime_fit_score": 99.0,
                                "runtime_validation_id": "runtime-validation-replay",
                                "benchmark_id": "benchmark-replay",
                                "runtime_capability_lock": {
                                    "status": "locked",
                                    "capability_sha256": "c" * 64,
                                },
                                "runtime_workbench_schema_version": (
                                    "temms-runtime-workbench/v1"
                                ),
                                "runtime_workbench_target_selection_status": "best",
                                "runtime_workbench_previous_selected_runtime_target_id": (
                                    "temms-rpi5-tflite"
                                ),
                                "runtime_workbench_selected_runtime_target_id": (
                                    "temms-x86_64-cpu"
                                ),
                                "runtime_workbench_best_runtime_target_id": (
                                    "temms-x86_64-cpu"
                                ),
                                "runtime_workbench_target_count": 4,
                                "runtime_workbench_selected_is_best": True,
                            },
                        },
                    },
                },
            }
        ],
    }

    summary = summarize_evidence_bundle(bundle)
    decision = summary["decisions"][0]
    replay = build_mission_replay(bundle)
    offline_phase = next(
        phase for phase in replay["phases"] if phase["phase"] == "offline_operation"
    )
    timeline_event = summary["timeline"][0]
    replay_event = replay["events"][0]

    assert decision["runtime_retargeted"] is True
    assert decision["runtime_retargeted_at"] == "2026-06-26T18:18:30Z"
    assert decision["runtime_retargeted_by"] == "operator:model-deployment-control"
    assert decision["runtime_retargeted_from"] == "temms-rpi5-tflite"
    assert decision["runtime_retargeted_to"] == "temms-x86_64-cpu"
    assert decision["runtime_retarget_previous_payload_sha256"] == "bad-runtime-digest"
    assert decision["runtime_retarget_proof_status"] == "proved"
    assert decision["runtime_retarget_runtime_fit_score"] == 99.0
    assert decision["runtime_retarget_capability_lock_status"] == "locked"
    assert decision["runtime_retarget_capability_sha256"] == "c" * 64
    assert decision["runtime_retarget_validation_id"] == "runtime-validation-replay"
    assert decision["runtime_retarget_benchmark_id"] == "benchmark-replay"
    assert decision["runtime_retarget_workbench_schema_version"] == (
        "temms-runtime-workbench/v1"
    )
    assert decision["runtime_retarget_workbench_selected_runtime_target_id"] == (
        "temms-x86_64-cpu"
    )
    assert decision[
        "runtime_retarget_workbench_previous_selected_runtime_target_id"
    ] == "temms-rpi5-tflite"
    assert decision["runtime_retarget_workbench_best_runtime_target_id"] == (
        "temms-x86_64-cpu"
    )
    assert decision["runtime_retarget_workbench_target_count"] == 4
    assert decision["runtime_retarget_workbench_selected_is_best"] is True
    assert offline_phase["status"] == "complete"
    assert offline_phase["summary"] == "1 retargeted DDIL replays"
    assert offline_phase["evidence_refs"] == ["model-lowlight"]
    assert timeline_event["kind"] == "decision"
    assert timeline_event["summary"] == (
        "model-lowlight DDIL replay retargeted "
        "temms-rpi5-tflite -> temms-x86_64-cpu"
    )
    assert replay_event["phase"] == "offline_operation"
    assert replay_event["summary"] == timeline_event["summary"]
    assert replay_event["detail"] == "retargeted temms-rpi5-tflite -> temms-x86_64-cpu"


def test_mission_replay_flags_runtime_fit_upgrade_as_preview_only():
    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "exported_at": "2026-06-11T12:00:00Z",
        "runtime": {},
        "runtime_fit_evidence": [
            {
                "schema_version": "temms-runtime-fit-evidence/v1",
                "checked_at": "2026-06-11T12:00:35Z",
                "selection": {
                    "model_id": "lowlight",
                    "device_id": "edge-1",
                    "runtime_target_id": "cpu-fit",
                },
                "runtime_fit": {
                    "score": 82,
                    "tier": "ready",
                    "target_selection": {
                        "status": "upgrade_available",
                        "best_runtime_target_id": "gpu-fit",
                        "score_delta": 14,
                    },
                },
            }
        ],
    }

    summary = summarize_evidence_bundle(bundle)
    replay = build_mission_replay(bundle)
    phases = {phase["phase"]: phase for phase in replay["phases"]}

    assert summary["runtime"]["runtime_fit_evidence"][0]["score_delta"] == 14
    assert phases["runtime_fit"]["status"] == "preview_only"
    assert "better target gpu-fit" in phases["runtime_fit"]["summary"]
    assert "runtime_fit" in replay["outcome"]["incomplete_phases"]


def test_runtime_fit_replay_prefers_active_slot_and_dedupes_rollout_proof():
    def runtime_fit_record(
        *,
        checked_at: str,
        model_id: str,
        score: int,
        rollout_id: str,
    ) -> dict[str, object]:
        return {
            "schema_version": "temms-runtime-fit-evidence/v1",
            "checked_at": checked_at,
            "selection": {
                "package_id": "pkg-vision",
                "model_id": model_id,
                "device_id": "edge-1",
                "runtime_target_id": "edge-cpu",
                "slot": "vision",
                "rollout_id": rollout_id,
            },
            "runtime_fit": {
                "score": score,
                "tier": "optimal",
                "runtime_lane": {
                    "lane_id": "cpu-onnx",
                    "label": "CPU portable",
                    "execution_engine": "onnxruntime",
                    "acceleration": "cpu",
                },
                "artifact_lane": {
                    "status": "go",
                    "state": "native artifact",
                    "detail": "onnx artifact is native for CPU portable",
                    "model_format": "onnx",
                },
                "target_selection": {
                    "status": "best",
                    "best_runtime_target_id": "edge-cpu",
                    "score_delta": 0,
                },
            },
        }

    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "runtime": {},
        "slots": [
            {
                "name": "vision",
                "state": "running",
                "active_model_id": "lowlight",
                "default_model": "daylight",
            }
        ],
        "runtime_fit_evidence": [
            runtime_fit_record(
                checked_at="2026-06-11T12:00:40Z",
                model_id="daylight",
                score=95,
                rollout_id="rollout-daylight",
            ),
            runtime_fit_record(
                checked_at="2026-06-11T12:00:35Z",
                model_id="lowlight",
                score=96,
                rollout_id="rollout-lowlight",
            ),
            runtime_fit_record(
                checked_at="2026-06-11T12:00:36Z",
                model_id="lowlight",
                score=98,
                rollout_id="rollout-lowlight",
            ),
        ],
    }

    summary = summarize_evidence_bundle(bundle, limit=10)
    runtime_fits = summary["runtime"]["runtime_fit_evidence"]

    assert summary["counts"]["runtime_fit_evidence"] == 2
    assert [fit["model_id"] for fit in runtime_fits] == ["lowlight", "daylight"]
    assert runtime_fits[0]["score"] == 98

    replay = build_mission_replay(bundle, limit=10)
    phases = {phase["phase"]: phase for phase in replay["phases"]}

    assert phases["runtime_fit"]["status"] == "complete"
    assert "98/100 optimal on edge-cpu" in phases["runtime_fit"]["summary"]
    assert "lane CPU portable / cpu" in phases["runtime_fit"]["summary"]
    assert "artifact native artifact" in phases["runtime_fit"]["summary"]
    assert "95/100" not in phases["runtime_fit"]["summary"]
    runtime_fit_events = [
        event for event in replay["events"] if event["kind"] == "runtime_fit"
    ]
    assert runtime_fit_events
    assert {event["phase"] for event in runtime_fit_events} == {"runtime_fit"}


def test_summary_timeline_marks_active_runtime_fit_before_same_second_inactive():
    def runtime_fit_record(model_id: str, score: int, checked_at: str) -> dict[str, object]:
        return {
            "schema_version": "temms-runtime-fit-evidence/v1",
            "checked_at": checked_at,
            "selection": {
                "package_id": "pkg-vision",
                "model_id": model_id,
                "device_id": "edge-1",
                "runtime_target_id": "edge-cpu",
                "slot": "vision",
                "rollout_id": f"rollout-{model_id}",
            },
            "runtime_fit": {
                "score": score,
                "tier": "optimal",
                "target_selection": {
                    "status": "best",
                    "best_runtime_target_id": "edge-cpu",
                    "score_delta": 0,
                },
            },
        }

    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "runtime": {},
        "slots": [
            {
                "name": "vision",
                "state": "running",
                "active_model_id": "lowlight",
            }
        ],
        "runtime_fit_evidence": [
            runtime_fit_record("daylight", 95, "2026-06-11T12:00:35.263419Z"),
            runtime_fit_record("lowlight", 98, "2026-06-11T12:00:35.261738Z"),
        ],
    }

    summary = summarize_evidence_bundle(bundle, limit=4)
    runtime_events = [
        event for event in summary["timeline"] if event["kind"] == "runtime_fit"
    ]

    assert runtime_events[0]["active_runtime_proof"] is True
    assert "lowlight runtime fit 98/100 optimal" in runtime_events[0]["summary"]
    assert "active_runtime_proof" not in runtime_events[1]
    assert "daylight runtime fit 95/100 optimal" in runtime_events[1]["summary"]

    raw_timeline = combined_timeline(
        [],
        [],
        runtime_fit_evidence=bundle["runtime_fit_evidence"],
        active_slots=summary["active_slots"],
    )
    assert raw_timeline[0]["active_runtime_proof"] is True
    assert "lowlight runtime fit 98/100 optimal" in raw_timeline[0]["summary"]

    replay = build_mission_replay(bundle, limit=4)
    replay_runtime_events = [
        event for event in replay["events"] if event["kind"] == "runtime_fit"
    ]
    assert replay_runtime_events[0]["active_runtime_proof"] is True
    assert replay_runtime_events[0]["summary"] == raw_timeline[0]["summary"]


def test_runtime_fit_evidence_timeline_exports_hub_readiness_fit(tmp_path):
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
            "package_id": "pkg-fit",
            "name": "fit-package",
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
                        "id": "model-fit",
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
    hub.promote_package("pkg-fit", "validated", actor="operator:test")
    hub.promote_package("pkg-fit", "approved", actor="operator:test")
    hub.promote_package("pkg-fit", "released", actor="operator:test")
    hub.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-fit",
        actor="operator:test",
    )
    hub.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-fit",
            "latency_ms": {"p95": 8.0},
            "throughput": {"inferences_per_second": 120.0},
        },
        device_id="edge-1",
        package_id="pkg-fit",
        runtime_target_id="temms-x86_64-cpu",
        actor="edge:edge-1",
    )
    hub.assign_rollout(
        "edge-1",
        "pkg-fit",
        slot="vision",
        rollout_id="rollout-fit",
        runtime_target_id="temms-x86_64-cpu",
        model_id="model-fit",
        actor="operator:test",
    )

    records = runtime_fit_evidence_timeline(SimpleNamespace(hub_lite=hub))

    assert records[0]["schema_version"] == "temms-runtime-fit-evidence/v1"
    assert records[0]["selection"]["rollout_id"] == "rollout-fit"
    assert records[0]["runtime_fit"]["schema_version"] == "temms-runtime-fit/v1"
    assert records[0]["runtime_fit"]["score"] >= 85
    assert records[0]["runtime_fit"]["components"]["performance"]["state"] == "slo met"
    assert records[0]["runtime_optimizer_gate"]["gate_id"] == "runtime_optimizer"
    mission = records[0]["edge_runtime_mission"]
    assert mission["schema_version"] == "temms-edge-runtime-mission/v1"
    assert mission["path"]["label"] == "model-fit -> temms-x86_64-cpu -> edge-1"
    assert mission["metrics"]["runtime_fit"]["score"] >= 85
    assert mission["metrics"]["runtime_lane"]["lane_id"] == "cpu-onnx"
    assert mission["metrics"]["artifact_fit"]["status"] == "attention"
    contract = records[0]["edge_execution_contract"]
    assert contract["schema_version"] == "temms-edge-execution-contract/v1"
    assert contract["path"]["label"] == "model-fit -> temms-x86_64-cpu -> edge-1"
    assert contract["runtime_fit"]["score"] >= 85
    workbench = records[0]["runtime_workbench"]
    assert workbench["schema_version"] == "temms-runtime-workbench/v1"
    assert workbench["selected_runtime_target_id"] == "temms-x86_64-cpu"
    assert workbench["summary"]["target_count"] >= 1

    bundle = {"runtime_fit_evidence": records}
    summary = summarize_evidence_bundle(bundle, limit=5)
    runtime_summary = summary["runtime"]["runtime_fit_evidence"][0]
    assert runtime_summary["edge_runtime_mission_status"] == "attention"
    assert runtime_summary["edge_runtime_mission_path"] == (
        "model-fit -> temms-x86_64-cpu -> edge-1"
    )
    assert runtime_summary["edge_execution_contract_status"] == "attention"
    assert runtime_summary["edge_execution_contract_path"] == (
        "model-fit -> temms-x86_64-cpu -> edge-1"
    )
    assert runtime_summary["runtime_workbench_schema_version"] == (
        "temms-runtime-workbench/v1"
    )
    assert runtime_summary["runtime_workbench_selected_runtime_target_id"] == (
        "temms-x86_64-cpu"
    )
    assert runtime_summary["runtime_workbench_target_count"] >= 1


def test_summary_counts_signed_hub_package_when_cache_import_is_unsigned():
    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "packages": [
            {
                "id": "pkg-vision",
                "manifest": {
                    "_temms_import": {
                        "signature_verified": False,
                    }
                },
            }
        ],
        "package_imports": [
            {
                "package_id": "pkg-vision",
                "signature_verified": False,
                "imported_at": "2026-06-25T16:17:16Z",
            }
        ],
        "hub_lite": {
            "packages": {
                "pkg-vision": {
                    "package_id": "pkg-vision",
                    "metadata": {
                        "validation": {
                            "signature_verified": True,
                            "strict_metadata": True,
                        }
                    },
                    "promotion": {"state": "released"},
                }
            }
        },
        "package_promotions": [
            {
                "package_id": "pkg-vision",
                "state": "released",
                "updated_at": "2026-06-25T16:17:16Z",
            }
        ],
    }

    summary = summarize_evidence_bundle(bundle)

    assert summary["trust"]["signed_package_imports"] == 1
    assert summary["trust"]["signed_package_ids"] == ["pkg-vision"]
    assert summary["trust"]["released_packages"] == 1
