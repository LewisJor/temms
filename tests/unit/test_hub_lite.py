"""
Tests for Hub Lite state merge behavior.
"""

import base64
import hashlib
import json
from pathlib import Path

import pytest

from temms.core.signing import sign_package
from temms import hub_lite as hub_lite_module
from temms.hub_lite import HubLiteStore


def test_edge_runtime_proof_envelope_hash_and_gate_policy():
    readiness = {
        "schema_version": "temms-deployment-readiness/v1",
        "status": "go",
        "selection": {
            "package_id": "pkg-vision",
            "model_id": "model-vision",
            "device_id": "edge-1",
            "runtime_target_id": "temms-x86_64-cpu",
            "slot": "vision",
        },
        "runtime_fit": {
            "score": 97,
            "tier": "optimal",
            "runtime_capability_lock": {
                "schema_version": "temms-runtime-capability-lock/v1",
                "status": "locked",
                "capability_sha256": "a" * 64,
            },
            "target_selection": {
                "status": "best",
                "selected_runtime_target_id": "temms-x86_64-cpu",
                "best_runtime_target_id": "temms-x86_64-cpu",
                "score_delta": 0,
            },
        },
        "edge_runtime_mission": {
            "schema_version": "temms-edge-runtime-mission/v1",
            "status": "go",
            "path": {
                "package_id": "pkg-vision",
                "model_id": "model-vision",
                "device_id": "edge-1",
                "runtime_target_id": "temms-x86_64-cpu",
                "slot": "vision",
            },
            "metrics": {
                "runtime_fit": {
                    "status": "go",
                    "score": 97,
                    "tier": "optimal",
                }
            },
        },
    }

    proof = hub_lite_module.build_edge_runtime_proof(
        readiness,
        require_go=True,
        min_runtime_fit=95,
        require_best_runtime=True,
        require_capability_lock=True,
    )

    assert proof["schema_version"] == "temms-edge-runtime-proof/v1"
    assert proof["gate_status"] == "passed"
    assert proof["gate_policy"] == {
        "require_go": True,
        "min_runtime_fit": 95,
        "require_best_runtime": True,
        "require_capability_lock": True,
    }
    assert proof["gate_failures"] == []
    assert proof["runtime_fit_score"] == 97.0
    assert proof["selection"]["model_id"] == "model-vision"
    unsigned = dict(proof)
    recorded_hash = unsigned.pop("integrity")["payload_sha256"]
    assert hub_lite_module.canonical_json_hash(unsigned) == recorded_hash

    signed = hub_lite_module.build_edge_runtime_proof(
        readiness,
        require_go=True,
        min_runtime_fit=95,
        require_best_runtime=True,
        require_capability_lock=True,
        signing_key="proof-secret",
        signer="temms-test",
    )
    attestation = signed["integrity"]["attestation"]
    assert attestation["schema_version"] == "temms-edge-runtime-proof-attestation/v1"
    assert attestation["algorithm"] == "HMAC-SHA256"
    assert attestation["signer"] == "temms-test"
    assert attestation["payload_sha256"] == signed["integrity"]["payload_sha256"]

    attestation_verification = hub_lite_module.verify_edge_runtime_proof_attestation(
        signed,
        "proof-secret",
    )
    assert attestation_verification["verified"] is True
    assert attestation_verification["errors"] == []

    wrong_key = hub_lite_module.verify_edge_runtime_proof_attestation(
        signed,
        "wrong-secret",
    )
    assert wrong_key["verified"] is False
    assert "attestation signing key fingerprint mismatch" in wrong_key["errors"]

    failed = hub_lite_module.build_edge_runtime_proof(
        readiness,
        require_go=True,
        min_runtime_fit=99,
        require_best_runtime=True,
        require_capability_lock=True,
    )

    assert failed["gate_status"] == "failed"
    assert failed["gate_failures"] == [
        "runtime fit score 97/100 is below required 99/100"
    ]

    suboptimal = json.loads(json.dumps(readiness))
    suboptimal["runtime_fit"]["target_selection"] = {
        "status": "suboptimal",
        "selected_runtime_target_id": "temms-x86_64-cpu",
        "best_runtime_target_id": "temms-orin-tensorrt",
        "score_delta": 4,
    }
    suboptimal_proof = hub_lite_module.build_edge_runtime_proof(
        suboptimal,
        require_go=True,
        min_runtime_fit=95,
        require_best_runtime=True,
        require_capability_lock=True,
    )

    assert suboptimal_proof["gate_status"] == "failed"
    assert suboptimal_proof["gate_failures"] == [
        "selected runtime target temms-x86_64-cpu is not best measured target temms-orin-tensorrt"
    ]

    unlocked = json.loads(json.dumps(readiness))
    unlocked["runtime_fit"]["runtime_capability_lock"]["status"] = "blocked"
    unlocked["runtime_fit"]["runtime_capability_lock"]["failures"] = [
        "edge inventory cannot host runtime target temms-x86_64-cpu"
    ]
    unlocked_proof = hub_lite_module.build_edge_runtime_proof(
        unlocked,
        require_go=True,
        min_runtime_fit=95,
        require_best_runtime=True,
        require_capability_lock=True,
    )

    assert unlocked_proof["gate_status"] == "failed"
    assert unlocked_proof["gate_failures"] == [
        "runtime capability lock status is blocked, expected locked",
        "runtime capability lock has failures: edge inventory cannot host runtime target temms-x86_64-cpu",
    ]


def test_edge_mission_package_plan_binds_mission_runtime_and_policy():
    readiness = {
        "schema_version": "temms-deployment-readiness/v1",
        "status": "go",
        "headline": "Deployment is ready",
        "next_action": "Generate edge runtime proof",
        "checked_at": "2026-07-06T20:00:00Z",
        "selection": {
            "package_id": "pkg-vision",
            "model_id": "model-vision",
            "device_id": "edge-1",
            "runtime_target_id": "temms-x86_64-cpu",
            "slot": "vision",
        },
        "runtime_fit": {
            "score": 97,
            "tier": "optimal",
            "runtime_capability_lock": {
                "schema_version": "temms-runtime-capability-lock/v1",
                "status": "locked",
                "capability_sha256": "a" * 64,
            },
            "target_selection": {
                "status": "best",
                "selected_runtime_target_id": "temms-x86_64-cpu",
                "best_runtime_target_id": "temms-x86_64-cpu",
                "score_delta": 0,
            },
        },
        "edge_runtime_mission": {
            "schema_version": "temms-edge-runtime-mission/v1",
            "status": "go",
            "path": {
                "package_id": "pkg-vision",
                "model_id": "model-vision",
                "device_id": "edge-1",
                "runtime_target_id": "temms-x86_64-cpu",
                "slot": "vision",
            },
            "metrics": {
                "runtime_fit": {
                    "status": "go",
                    "score": 97,
                    "tier": "optimal",
                }
            },
        },
        "edge_execution_contract": {
            "schema_version": "temms-edge-execution-contract/v1",
            "recommended_action": "apply_or_stage",
        },
    }

    plan = hub_lite_module.build_edge_mission_package_plan(
        readiness,
        {
            "goal": "Detect vehicles locally during link loss.",
            "mission_yaml": "schema_version: temms-edge-mission/v1",
            "sensor": "camera.rgb",
            "slot": "vision",
            "latency_budget_ms": 95,
            "min_throughput_ips": 25,
            "switch_policy": "condition_and_confidence",
            "confidence_threshold": 0.65,
            "fallback_model_id": "auto",
            "ddil_mode": "queue_signed_intents",
        },
        require_go=True,
        min_runtime_fit=95,
        require_best_runtime=True,
        require_capability_lock=True,
        require_proof_signature=True,
    )

    assert plan["schema_version"] == "temms-edge-mission-package/v1"
    assert plan["mission"]["goal"] == "Detect vehicles locally during link loss."
    assert plan["mission"]["sensor"] == "camera.rgb"
    assert plan["mission"]["source"] == "yaml"
    assert plan["selection"]["model_id"] == "model-vision"
    assert plan["slo"] == {"latency_budget_ms": 95.0, "min_throughput_ips": 25.0}
    assert plan["model_handling"] == {
        "switch_policy": "condition_and_confidence",
        "confidence_threshold": 0.65,
        "fallback_model_id": "auto",
    }
    assert plan["ddil"] == {
        "mode": "queue_signed_intents",
        "replay_requires_readiness": True,
        "proof_required": True,
    }
    assert plan["runtime_plan"]["runtime_fit_score"] == 97
    assert plan["runtime_plan"]["runtime_capability_lock"]["status"] == "locked"
    assert plan["proof_gate"] == {
        "status": "passed",
        "policy": {
            "require_go": True,
            "min_runtime_fit": 95,
            "require_best_runtime": True,
            "require_capability_lock": True,
            "require_proof_signature": True,
        },
        "failures": [],
    }
    expected_rollout_id = hub_lite_module._readiness_command_id(
        "rollout",
        plan["selection"],
        ["package_id", "model_id", "device_id", "runtime_target_id", "slot"],
    )
    assert plan["deployment_intent"]["schema_version"] == (
        "temms-edge-deployment-intent/v1"
    )
    assert plan["deployment_intent"]["rollout_id"] == expected_rollout_id
    assert plan["package_identity"] == {
        "schema_version": "temms-edge-mission-package-identity/v1",
        "package_identity_sha256": hub_lite_module.edge_mission_package_identity_hash(
            plan
        ),
        "components": [
            "ddil",
            "mission",
            "model_handling",
            "proof_gate",
            "runtime_plan",
            "selection",
            "slo",
        ],
    }
    assert plan["integrity"]["package_identity_sha256"] == (
        plan["package_identity"]["package_identity_sha256"]
    )
    assert plan["deployment_intent"]["package_identity_sha256"] == (
        plan["package_identity"]["package_identity_sha256"]
    )
    assert plan["deployment_intent"]["mission_package_core_sha256"] == (
        plan["package_identity"]["package_identity_sha256"]
    )
    replanned = dict(plan)
    replanned["planned_at"] = "2099-01-01T00:00:00Z"
    replanned["readiness"] = {
        **replanned["readiness"],
        "checked_at": "2099-01-01T00:00:00Z",
    }
    replanned["selection"] = {
        **replanned["selection"],
        "rollout_id": "rollout-from-another-operator-action",
    }
    replanned["runtime_plan"] = {
        **replanned["runtime_plan"],
        "target_selection": {
            **replanned["runtime_plan"]["target_selection"],
            "detail": "Selected runtime was rechecked a few seconds later.",
            "alternatives": [
                {
                    "detail": "last heartbeat was 9 seconds ago",
                    "runtime_target_id": "temms-x86_64-cpu",
                }
            ],
        },
        "runtime_capability_lock": {
            **replanned["runtime_plan"]["runtime_capability_lock"],
            "edge_inventory": {
                "telemetry_freshness": {
                    "detail": "last heartbeat was 9 seconds ago",
                    "heartbeat_age_seconds": 9,
                }
            },
        },
    }
    assert hub_lite_module.edge_mission_package_identity_hash(replanned) == (
        plan["package_identity"]["package_identity_sha256"]
    )
    assert plan["deployment_intent"]["command"] == {
        "method": "POST",
        "path": "/v1/hub/rollouts",
        "body": {
            "rollout_id": expected_rollout_id,
            "package_id": "pkg-vision",
            "model_id": "model-vision",
            "device_id": "edge-1",
            "runtime_target_id": "temms-x86_64-cpu",
            "slot": "vision",
            "require_approval": True,
            "require_runtime_validation": True,
            "actor": "operator:readiness-remediation",
            "reason": (
                "mission package deployment handoff "
                f"{plan['deployment_intent']['mission_package_core_sha256'][:12]}"
            ),
        },
    }
    assert plan["component_digests"]["schema_version"] == (
        "temms-edge-mission-package-component-digests/v1"
    )
    assert plan["component_digests"]["mission_sha256"] == (
        hub_lite_module.canonical_json_hash(plan["mission"])
    )
    assert plan["component_digests"]["deployment_intent_sha256"] == (
        hub_lite_module.canonical_json_hash(plan["deployment_intent"])
    )
    unsigned = dict(plan)
    recorded_hash = unsigned.pop("integrity")["payload_sha256"]
    assert hub_lite_module.canonical_json_hash(unsigned) == recorded_hash


def test_runtime_target_catalog_includes_defaults_and_byo_targets(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")

    defaults = store.list_runtime_targets()

    assert {target["runtime_target_id"] for target in defaults} >= {
        "temms-x86_64-cpu",
        "temms-arm64-jetson",
        "temms-rpi5-tflite",
        "temms-orin-tensorrt",
    }
    default_lanes = {
        target["runtime_target_id"]: target["runtime_lane"]["lane_id"]
        for target in defaults
    }
    assert default_lanes["temms-x86_64-cpu"] == "cpu-onnx"
    assert default_lanes["temms-arm64-jetson"] == "jetson-cuda"
    assert default_lanes["temms-rpi5-tflite"] == "rpi5-tflite"
    assert default_lanes["temms-orin-tensorrt"] == "orin-tensorrt"

    target = store.upsert_runtime_target(
        {
            "runtime_target_id": "customer-orin",
            "name": "Customer Orin",
            "image": "registry.example.com/edge/orin-runtime:2026.06",
            "os": "linux",
            "arch": "arm64",
            "device_profiles": ["jetson-orin"],
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CUDAExecutionProvider"],
                }
            },
            "accelerators": {"nvidia": {"available": True}},
        },
        actor="operator:alice",
    )

    assert target["device_profiles"] == ["orin-tensorrt"]
    assert target["runtime_lane"]["schema_version"] == "temms-runtime-lane/v1"
    assert target["runtime_lane"]["lane_id"] == "jetson-cuda"
    assert target["runtime_lane"]["providers"] == ["CUDAExecutionProvider"]
    assert target["metadata"]["audit"]["catalog_actor"] == "operator:alice"
    assert store.get_runtime_target("customer-orin")["image"].startswith("registry.example.com/")


def test_package_source_registration_requires_signature_by_default(temp_dir):
    """Hub source registration should be signing-first unless a lab opts out."""
    store = HubLiteStore(temp_dir / "hub.json")
    package_dir = _minimal_package(temp_dir / "pkg-source-default.temms", b"source-default")

    with pytest.raises(ValueError, match="Signature verification requires a signing key"):
        store.upsert_package_from_source(package_dir)

    assert store.list_packages() == []

    sign_package(package_dir, "hub-key", signer="unit-test-hub")

    package = store.upsert_package_from_source(
        package_dir,
        signing_key="hub-key",
        actor="operator:test",
    )

    assert package["package_id"] == "pkg-source-default"
    assert package["updated_by"] == "operator:test"
    assert package["metadata"]["validation"]["signature_verified"] is True
    assert (
        store.get_package("pkg-source-default")["metadata"]["validation"]["signature"]["signer"]
        == "unit-test-hub"
    )


def test_rollout_assignment_records_runtime_target_and_blocks_mismatch(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    package = store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "metadata": {
                "models": [
                    {
                        "id": "model-vision",
                        "runtime_constraints": {
                            "runtimes": ["onnxruntime"],
                            "providers": ["CPUExecutionProvider"],
                        },
                    }
                ]
            },
        }
    )
    assert package["promotion"]["state"] == "candidate"
    with pytest.raises(ValueError, match="not released"):
        store.assign_rollout(
            "edge-1",
            "pkg-vision",
            slot="vision",
            rollout_id="rollout-candidate",
            runtime_target_id="temms-x86_64-cpu",
        )
    released = _release_package(store, "pkg-vision")
    assert released["promotion"]["state"] == "released"

    rollout = store.assign_rollout(
        "edge-1",
        "pkg-vision",
        slot="vision",
        rollout_id="rollout-runtime",
        runtime_target_id="temms-x86_64-cpu",
        model_id="model-vision",
    )

    assert rollout["runtime_target_id"] == "temms-x86_64-cpu"
    assert rollout["model_id"] == "model-vision"
    assert rollout["runtime_target"]["image"] == "temms/agent:inference-amd64"

    with pytest.raises(ValueError, match="Model model-missing is not declared"):
        store.assign_rollout(
            "edge-1",
            "pkg-vision",
            slot="vision",
            rollout_id="rollout-missing-model",
            runtime_target_id="temms-x86_64-cpu",
            model_id="model-missing",
        )

    approval_gated = store.assign_rollout(
        "edge-1",
        "pkg-vision",
        slot="vision",
        rollout_id="rollout-approval",
        runtime_target_id="temms-x86_64-cpu",
        require_approval=True,
        actor="operator:planner",
    )

    assert approval_gated["approval_required"] is True
    assert approval_gated["approval"]["state"] == "pending"
    approved = store.approve_rollout(
        "rollout-approval",
        actor="operator:approver",
        reason="mission commander approved package policy",
    )
    assert approved["approval"]["approved"] is True
    assert approved["approval"]["actor"] == "operator:approver"
    assert approved["approval"]["reason"] == "mission commander approved package policy"
    assert approved["history"][-1]["state"] == "approved"
    assert approved["history"][-1]["actor"] == "operator:approver"

    store.upsert_runtime_target(
        {
            "runtime_target_id": "bad-arm-target",
            "image": "registry.example.com/edge/arm64:latest",
            "device_profiles": ["arm64-jetson"],
            "runtimes": {"onnxruntime": {"available": True}},
        }
    )

    with pytest.raises(ValueError, match="Runtime target bad-arm-target"):
        store.assign_rollout(
            "edge-1",
            "pkg-vision",
            slot="vision",
            rollout_id="rollout-bad-runtime",
            runtime_target_id="bad-arm-target",
        )


def test_runtime_target_assignment_checks_live_edge_inventory(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device(
        "edge-orin",
        profile="orin-tensorrt",
        inventory={
            "device_profile": "orin-tensorrt",
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                },
                "tensorrt": {"available": False},
            },
            "accelerators": {"nvidia": {"available": False}},
        },
    )
    store.upsert_package(
        {
            "package_id": "pkg-orin",
            "name": "orin-vision",
            "version": "1.0.0",
            "device_profiles": ["orin-tensorrt"],
            "metadata": {
                "models": [
                    {
                        "id": "model-orin",
                        "runtime_constraints": {
                            "device_profiles": ["orin-tensorrt"],
                            "runtimes": ["onnxruntime"],
                            "preferred_providers": ["TensorrtExecutionProvider"],
                            "accelerators": ["nvidia"],
                        },
                    }
                ]
            },
        }
    )
    _release_package(store, "pkg-orin")

    preview = store.preview_rollout_compatibility(
        "edge-orin",
        "pkg-orin",
        runtime_target_id="temms-orin-tensorrt",
        model_id="model-orin",
    )

    assert preview["compatible"] is False
    assert any(
        "edge inventory cannot host runtime target temms-orin-tensorrt" in failure
        for failure in preview["failures"]
    )
    assert any("missing accelerators: nvidia" in failure for failure in preview["failures"])

    with pytest.raises(ValueError, match="edge inventory cannot host runtime target"):
        store.assign_rollout(
            "edge-orin",
            "pkg-orin",
            slot="vision",
            rollout_id="rollout-orin-stale-inventory",
            runtime_target_id="temms-orin-tensorrt",
            model_id="model-orin",
        )

    store.heartbeat(
        "edge-orin",
        inventory={
            "device_profile": "orin-tensorrt",
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["TensorrtExecutionProvider", "CUDAExecutionProvider"],
                },
                "tensorrt": {"available": True},
            },
            "accelerators": {"nvidia": {"available": True}},
        },
    )

    ready = store.preview_rollout_compatibility(
        "edge-orin",
        "pkg-orin",
        runtime_target_id="temms-orin-tensorrt",
        model_id="model-orin",
    )
    assert ready["compatible"] is True

    rollout = store.assign_rollout(
        "edge-orin",
        "pkg-orin",
        slot="vision",
        rollout_id="rollout-orin-ready",
        runtime_target_id="temms-orin-tensorrt",
        model_id="model-orin",
    )
    assert rollout["state"] == "assigned"


def test_active_runtime_inventory_drift_offers_fallback_runtime(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device(
        "edge-orin",
        profile="orin-tensorrt",
        inventory={
            "device_profile": "orin-tensorrt",
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": [
                        "TensorrtExecutionProvider",
                        "CUDAExecutionProvider",
                        "CPUExecutionProvider",
                    ],
                },
                "tensorrt": {"available": True},
            },
            "accelerators": {"nvidia": {"available": True}},
            "memory": {"available_mb": 4096.0},
            "storage": {"available_mb": 8192.0},
        },
    )
    store.upsert_runtime_target(
        {
            "runtime_target_id": "customer-orin-cpu",
            "name": "Customer Orin CPU fallback",
            "image": "registry.example.com/edge/orin-cpu:2026.06",
            "os": "linux",
            "arch": "arm64",
            "device_profiles": ["orin-tensorrt"],
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                }
            },
            "runtime_constraints": {
                "device_profiles": ["orin-tensorrt"],
                "runtimes": ["onnxruntime"],
                "providers": ["CPUExecutionProvider"],
            },
        },
        actor="operator:runtime",
    )
    store.upsert_package(
        {
            "package_id": "pkg-orin-drift",
            "name": "orin-drift",
            "version": "1.0.0",
            "device_profiles": ["orin-tensorrt"],
            "sha256": "e" * 64,
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {
                        "id": "model-orin-trt",
                        "runtime_constraints": {
                            "device_profiles": ["orin-tensorrt"],
                            "runtimes": ["onnxruntime"],
                            "preferred_providers": ["TensorrtExecutionProvider"],
                            "accelerators": ["nvidia"],
                        },
                        "performance_slo": {
                            "max_latency_ms_p95": 12.0,
                            "min_throughput_ips": 80.0,
                        },
                    },
                    {
                        "id": "model-orin-cpu",
                        "runtime_constraints": {
                            "device_profiles": ["orin-tensorrt"],
                            "runtimes": ["onnxruntime"],
                            "providers": ["CPUExecutionProvider"],
                        },
                        "performance_slo": {
                            "max_latency_ms_p95": 25.0,
                            "min_throughput_ips": 30.0,
                        },
                    },
                ],
            },
        }
    )
    _release_package(store, "pkg-orin-drift")
    store.record_runtime_validation(
        "temms-orin-tensorrt",
        {
            "runtime_target_id": "temms-orin-tensorrt",
            "image": "temms/agent:inference-arm64-orin",
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-orin-drift",
        actor="operator:runtime",
    )
    fallback_validation = store.record_runtime_validation(
        "customer-orin-cpu",
        {
            "runtime_target_id": "customer-orin-cpu",
            "image": "registry.example.com/edge/orin-cpu:2026.06",
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-orin-drift",
        actor="operator:runtime",
    )
    fallback_benchmark = store.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-orin-cpu",
            "slot": "vision",
            "latency_ms": {"p95": 18.0},
            "throughput": {"inferences_per_second": 42.0},
        },
        device_id="edge-orin",
        package_id="pkg-orin-drift",
        runtime_target_id="customer-orin-cpu",
        actor="edge:edge-orin",
    )
    rollout = store.assign_rollout(
        "edge-orin",
        "pkg-orin-drift",
        slot="vision",
        rollout_id="rollout-orin-trt-active",
        runtime_target_id="temms-orin-tensorrt",
        model_id="model-orin-trt",
    )
    store.update_rollout_status(
        rollout["rollout_id"],
        "activated",
        detail="activated TensorRT model",
        actor="edge:edge-orin",
    )

    store.heartbeat(
        "edge-orin",
        status="online",
        inventory={
            "device_profile": "orin-tensorrt",
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                },
                "tensorrt": {"available": False},
            },
            "accelerators": {"nvidia": {"available": False}},
            "memory": {"available_mb": 4096.0},
            "storage": {"available_mb": 8192.0},
        },
    )

    readiness = store.deployment_readiness(
        package_id="pkg-orin-drift",
        model_id="model-orin-trt",
        device_id="edge-orin",
        runtime_target_id="temms-orin-tensorrt",
        slot="vision",
    )
    runtime_gate = {gate["gate_id"]: gate for gate in readiness["gates"]}[
        "runtime_target"
    ]

    assert readiness["status"] == "blocked"
    assert runtime_gate["status"] == "blocked"
    assert runtime_gate["state"] == "runtime drift"
    assert runtime_gate["refs"]["runtime_drift"] is True
    assert runtime_gate["refs"]["rollout_id"] == "rollout-orin-trt-active"
    assert any(
        "edge inventory cannot host runtime target temms-orin-tensorrt" in failure
        for failure in runtime_gate["refs"]["runtime_failures"]
    )
    fallback_action = runtime_gate["actions"][0]
    assert fallback_action["action_id"] == "stage_fallback_model"
    assert fallback_action["refs"]["model_id"] == "model-orin-cpu"
    assert fallback_action["refs"]["runtime_target_id"] == "customer-orin-cpu"
    assert fallback_action["refs"]["fallback_reason"] == "runtime drift"
    assert fallback_action["refs"]["fallback_runtime_validation_id"] == (
        fallback_validation["validation_id"]
    )
    assert fallback_action["refs"]["fallback_benchmark_id"] == (
        fallback_benchmark["benchmark_id"]
    )
    expected_fallback_rollout_id = hub_lite_module._readiness_command_id(
        "rollout",
        fallback_action["refs"],
        ["package_id", "model_id", "device_id", "runtime_target_id", "slot"],
    )
    assert fallback_action["command"] == {
        "method": "POST",
        "path": "/v1/hub/rollouts",
        "body": {
            "rollout_id": expected_fallback_rollout_id,
            "package_id": "pkg-orin-drift",
            "model_id": "model-orin-cpu",
            "device_id": "edge-orin",
            "runtime_target_id": "customer-orin-cpu",
            "slot": "vision",
            "require_approval": True,
            "require_runtime_validation": True,
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate fallback for runtime drift",
        },
    }
    assert runtime_gate["actions"][1]["kind"] == "rollback_rollout"
    assert runtime_gate["actions"][1]["command"] == {
        "method": "POST",
        "path": "/v1/hub/rollouts/rollout-orin-trt-active/rollback",
        "body": {
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate runtime drift",
        },
    }


def test_model_specific_rollout_filters_package_runtime_constraints(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.upsert_package(
        {
            "package_id": "pkg-multi-model",
            "name": "multi-model",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu", "rpi5-tflite"],
            "metadata": {
                "models": [
                    {
                        "id": "model-x86",
                        "runtime_constraints": {
                            "device_profiles": ["x86_64-cpu"],
                            "runtimes": ["onnxruntime"],
                        },
                    },
                    {
                        "id": "model-rpi",
                        "runtime_constraints": {
                            "device_profiles": ["rpi5-tflite"],
                            "runtimes": ["tflite"],
                        },
                    },
                ]
            },
        }
    )
    _release_package(store, "pkg-multi-model")

    package_level = store.preview_rollout_compatibility(
        "edge-1",
        "pkg-multi-model",
        runtime_target_id="temms-x86_64-cpu",
    )
    selected_model = store.preview_rollout_compatibility(
        "edge-1",
        "pkg-multi-model",
        runtime_target_id="temms-x86_64-cpu",
        model_id="model-x86",
    )

    assert package_level["compatible"] is False
    assert any("model-rpi" in failure for failure in package_level["failures"])
    assert selected_model["compatible"] is True
    assert selected_model["model_id"] == "model-x86"

    matrix = store.compatibility_matrix(
        package_ids=["pkg-multi-model"],
        device_ids=["edge-1"],
        runtime_target_ids=["temms-x86_64-cpu"],
    )
    assert matrix["dimensions"]["models"] == 2
    assert matrix["counts"]["cells"] == 2
    matrix_cells = {cell["model_id"]: cell for cell in matrix["cells"]}
    assert matrix_cells["model-x86"]["compatible"] is True
    assert matrix_cells["model-rpi"]["compatible"] is False
    assert any("model-rpi" in failure for failure in matrix_cells["model-rpi"]["failures"])

    filtered_matrix = store.compatibility_matrix(
        package_ids=["pkg-multi-model"],
        model_ids=["model-x86"],
        device_ids=["edge-1"],
        runtime_target_ids=["temms-x86_64-cpu"],
    )
    assert filtered_matrix["filters"]["model_ids"] == ["model-x86"]
    assert filtered_matrix["dimensions"]["models"] == 1
    assert filtered_matrix["counts"]["compatible"] == 1
    assert filtered_matrix["cells"][0]["model_id"] == "model-x86"

    rollout = store.assign_rollout(
        "edge-1",
        "pkg-multi-model",
        slot="vision",
        rollout_id="rollout-x86-model",
        runtime_target_id="temms-x86_64-cpu",
        model_id="model-x86",
    )

    assert rollout["model_id"] == "model-x86"


def test_deployment_draft_persists_active_mission_and_airgap_import(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
        }
    )

    draft = store.upsert_deployment_draft(
        package_id="pkg-vision",
        runtime_target_id="temms-x86_64-cpu",
        device_id="edge-1",
        slot="vision",
        actor="operator:test",
    )

    assert draft["schema_version"] == "temms-deployment-draft/v1"
    assert draft["draft_id"] == "active"
    assert draft["package_id"] == "pkg-vision"
    assert draft["device_id"] == "edge-1"
    assert draft["runtime_target_id"] == "temms-x86_64-cpu"
    assert draft["runtime_target"]["image"] == "temms/agent:inference-amd64"
    assert draft["actor"] == "operator:test"
    assert store.get_deployment_draft()["slot"] == "vision"

    imported = HubLiteStore(temp_dir / "imported-hub.json")
    counts = imported.import_bundle(store.export_bundle())

    assert counts["deployment_drafts"] == 1
    imported_draft = imported.get_deployment_draft()
    assert imported_draft["package_id"] == "pkg-vision"
    assert imported_draft["runtime_target_id"] == "temms-x86_64-cpu"


def test_rollout_compatibility_preview_is_side_effect_free(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device(
        "edge-rpi",
        profile="rpi5-tflite",
        inventory={"runtimes": {"tflite_runtime": {"available": True}}},
    )
    store.upsert_package(
        {
            "package_id": "pkg-tflite",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["rpi5-tflite"],
            "metadata": {
                "models": [
                    {
                        "id": "model-tflite",
                        "runtime_constraints": {"runtimes": ["tflite_runtime"]},
                    }
                ]
            },
        }
    )

    compatible = store.preview_rollout_compatibility("edge-rpi", "pkg-tflite")

    assert compatible["schema_version"] == "temms-rollout-compatibility/v1"
    assert compatible["compatible"] is True
    assert compatible["failures"] == []
    assert compatible["device"]["profile"] == "rpi5-tflite"
    assert compatible["runtime_target"] is None
    assert compatible["package"]["runtime_constraints"] == [
        {
            "model_id": "model-tflite",
            "constraints": {"runtimes": ["tflite_runtime"]},
        }
    ]
    assert store.list_rollouts() == []

    store.upsert_runtime_target(
        {
            "runtime_target_id": "x86-only",
            "image": "registry.example.com/x86:latest",
            "device_profiles": ["x86_64-cpu"],
            "runtimes": {"onnxruntime": {"available": True}},
        }
    )

    blocked = store.preview_rollout_compatibility(
        "edge-rpi",
        "pkg-tflite",
        runtime_target_id="x86-only",
    )

    assert blocked["compatible"] is False
    assert any("device profile rpi5-tflite" in failure for failure in blocked["failures"])
    assert any("missing runtimes: tflite_runtime" in failure for failure in blocked["failures"])
    assert store.list_rollouts() == []


def test_runtime_target_compatibility_blocks_artifact_lane_mismatch(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device(
        "edge-rpi",
        profile="rpi5-tflite",
        inventory={"runtimes": {"tflite_runtime": {"available": True}}},
    )
    store.upsert_package(
        {
            "package_id": "pkg-onnx-on-tflite",
            "name": "bad-artifact-lane",
            "version": "1.0.0",
            "device_profiles": ["rpi5-tflite"],
            "metadata": {
                "models": [
                    {
                        "id": "model-onnx",
                        "format": "onnx",
                        "filename": "model-onnx.onnx",
                        "runtime_constraints": {"runtimes": ["tflite_runtime"]},
                    }
                ]
            },
        }
    )

    preview = store.preview_rollout_compatibility(
        "edge-rpi",
        "pkg-onnx-on-tflite",
        runtime_target_id="temms-rpi5-tflite",
        model_id="model-onnx",
    )
    matrix = store.compatibility_matrix(
        package_ids=["pkg-onnx-on-tflite"],
        model_ids=["model-onnx"],
        device_ids=["edge-rpi"],
        runtime_target_ids=["temms-rpi5-tflite"],
    )

    assert preview["compatible"] is False
    assert any("onnx artifact is not compatible with Raspberry Pi 5 TFLite" in failure for failure in preview["failures"])
    cell = matrix["cells"][0]
    assert cell["compatible"] is False
    assert cell["runtime_fit"]["artifact_lane"] == {
        "schema_version": "temms-artifact-lane/v1",
        "lane_id": "rpi5-tflite",
        "lane_label": "Raspberry Pi 5 TFLite",
        "model_id": "model-onnx",
        "model_format": "onnx",
        "filename": "model-onnx.onnx",
        "native_formats": ["tflite"],
        "convertible_formats": [],
        "status": "blocked",
        "state": "artifact mismatch",
        "detail": (
            "onnx artifact is not compatible with Raspberry Pi 5 TFLite; "
            "package one of: tflite"
        ),
    }


def test_compatibility_matrix_summarizes_release_and_runtime_validation(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device(
        "edge-rpi",
        profile="rpi5-tflite",
        inventory={"runtimes": {"tflite_runtime": {"available": True}}},
    )
    store.upsert_package(
        {
            "package_id": "pkg-tflite",
            "name": "tflite",
            "version": "1.0.0",
            "device_profiles": ["rpi5-tflite"],
            "sha256": "a" * 64,
            "metadata": {
                "models": [
                    {
                        "id": "model-tflite",
                        "runtime_constraints": {"runtimes": ["tflite_runtime"]},
                    }
                ]
            },
        }
    )
    _release_package(store, "pkg-tflite")
    validation = store.record_runtime_validation(
        "temms-rpi5-tflite",
        {
            "runtime_target_id": "temms-rpi5-tflite",
            "image": "temms/agent:runtime-rpi5-tflite",
            "dry_run": False,
            "ok": True,
        },
        package_id="pkg-tflite",
        actor="operator:test",
    )

    matrix = store.compatibility_matrix(
        package_ids=["pkg-tflite"],
        device_ids=["edge-rpi"],
        runtime_target_ids=["temms-rpi5-tflite"],
        include_device_inventory=True,
    )

    assert matrix["schema_version"] == "temms-compatibility-matrix/v1"
    assert matrix["dimensions"] == {
        "packages": 1,
        "models": 1,
        "devices": 1,
        "runtime_targets": 1,
        "device_inventory": True,
        "cells": 2,
    }
    assert matrix["counts"]["compatible"] == 2
    assert matrix["counts"]["assignment_ready"] == 2
    assert matrix["counts"]["runtime_validation_ready"] == 1
    assert matrix["packages"][0]["promotion"]["state"] == "released"
    assert matrix["recommendations"][0]["decision"] == "deploy"
    assert matrix["recommendations"][0]["runtime_target_id"] == "temms-rpi5-tflite"
    assert matrix["recommendations"][0]["confidence"] == "high"
    assert matrix["recommendations"][0]["score"] > matrix["recommendations"][1]["score"]

    runtime_cell = next(
        cell for cell in matrix["cells"] if cell["runtime_target_id"] == "temms-rpi5-tflite"
    )
    assert runtime_cell["compatible"] is True
    assert runtime_cell["assignment_ready"] is True
    assert runtime_cell["runtime_validation_ready"] is True
    assert runtime_cell["runtime_validation"]["validation_id"] == validation["validation_id"]

    inventory_cell = next(cell for cell in matrix["cells"] if cell["runtime_target_id"] is None)
    assert inventory_cell["runtime_mode"] == "device_inventory"
    assert inventory_cell["assignment_blockers"] == []

    candidate = store.upsert_package(
        {
            "package_id": "pkg-candidate",
            "name": "candidate",
            "version": "1.0.0",
            "device_profiles": ["rpi5-tflite"],
            "metadata": {
                "models": [
                    {
                        "id": "model-candidate",
                        "runtime_constraints": {"runtimes": ["tflite_runtime"]},
                    }
                ]
            },
        }
    )
    candidate_matrix = store.compatibility_matrix(
        package_ids=[candidate["package_id"]],
        device_ids=["edge-rpi"],
        runtime_target_ids=["temms-rpi5-tflite"],
    )
    candidate_cell = candidate_matrix["cells"][0]
    assert candidate_cell["compatible"] is True
    assert candidate_cell["assignment_ready"] is False
    assert candidate_cell["package_promotion"]["state"] == "candidate"
    assert "not released" in candidate_cell["assignment_blockers"][0]
    assert candidate_matrix["recommendations"][0]["decision"] == "release_required"
    assert candidate_matrix["recommendations"][0]["required_actions"] == [
        "promote package to released"
    ]


def test_runtime_fit_scores_and_selects_best_measured_runtime_target(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    edge_inventory = {
        "runtimes": {
            "onnxruntime": {
                "available": True,
                "providers": ["CPUExecutionProvider", "CUDAExecutionProvider"],
            }
        },
        "memory": {"available_mb": 2048.0},
        "storage": {"available_mb": 4096.0},
    }
    store.enroll_device(
        "edge-1",
        profile="x86_64-cpu",
        inventory=edge_inventory,
    )
    store.upsert_package(
        {
            "package_id": "pkg-runtime-fit",
            "name": "runtime-fit",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "d" * 64,
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {
                        "id": "model-edge",
                        "format": "onnx",
                        "filename": "model-edge.onnx",
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
    _release_package(store, "pkg-runtime-fit")
    for runtime_target_id in ["cpu-fit", "gpu-fit"]:
        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if runtime_target_id == "gpu-fit"
            else ["CPUExecutionProvider"]
        )
        store.upsert_runtime_target(
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
        store.record_runtime_validation(
            runtime_target_id,
            {
                "runtime_target_id": runtime_target_id,
                "image": f"registry.example.com/{runtime_target_id}:latest",
                "dry_run": False,
                "exit_code": 0,
                "ok": True,
            },
            package_id="pkg-runtime-fit",
            actor="operator:test",
        )
    store.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-edge",
            "latency_ms": {"p95": 10.0},
            "throughput": {"inferences_per_second": 100.0},
        },
        device_id="edge-1",
        package_id="pkg-runtime-fit",
        runtime_target_id="cpu-fit",
        actor="edge:edge-1",
    )
    store.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-edge",
            "latency_ms": {"p95": 4.0},
            "throughput": {"inferences_per_second": 230.0},
        },
        device_id="edge-1",
        package_id="pkg-runtime-fit",
        runtime_target_id="gpu-fit",
        actor="edge:edge-1",
    )

    matrix = store.compatibility_matrix(
        package_ids=["pkg-runtime-fit"],
        model_ids=["model-edge"],
        device_ids=["edge-1"],
        runtime_target_ids=["cpu-fit", "gpu-fit"],
    )

    assert matrix["recommendations"][0]["runtime_target_id"] == "gpu-fit"
    assert matrix["recommendations"][0]["runtime_fit"]["tier"] == "optimal"
    assert matrix["recommendations"][0]["runtime_lane"]["lane_id"] == "jetson-cuda"
    cells = {cell["runtime_target_id"]: cell for cell in matrix["cells"]}
    cpu_fit = cells["cpu-fit"]["runtime_fit"]
    gpu_fit = cells["gpu-fit"]["runtime_fit"]
    assert cpu_fit["runtime_lane"]["lane_id"] == "cpu-onnx"
    assert gpu_fit["runtime_lane"]["schema_version"] == "temms-runtime-lane/v1"
    assert gpu_fit["runtime_lane"]["lane_id"] == "jetson-cuda"
    assert gpu_fit["artifact_lane"]["schema_version"] == "temms-artifact-lane/v1"
    assert gpu_fit["artifact_lane"]["state"] == "native artifact"
    assert gpu_fit["runtime_capability_lock"]["schema_version"] == (
        "temms-runtime-capability-lock/v1"
    )
    assert gpu_fit["runtime_capability_lock"]["status"] == "locked"
    assert gpu_fit["runtime_capability_lock"]["edge_inventory"][
        "telemetry_freshness"
    ]["status"] == "go"
    assert len(gpu_fit["runtime_capability_lock"]["capability_sha256"]) == 64
    assert gpu_fit["runtime_capability_lock"]["runtime_target"]["runtime_lane"][
        "lane_id"
    ] == "jetson-cuda"
    assert gpu_fit["runtime_capability_lock"]["edge_inventory"]["device_profile"] == (
        "x86_64-cpu"
    )
    assert gpu_fit["runtime_capability_lock"]["edge_inventory"]["runtimes"][
        "onnxruntime"
    ]["providers"] == ["CPUExecutionProvider", "CUDAExecutionProvider"]
    assert gpu_fit["score"] > cpu_fit["score"]
    assert gpu_fit["components"]["performance"]["score"] > (
        cpu_fit["components"]["performance"]["score"]
    )
    assert gpu_fit["optimization"]["latency_headroom_pct"] > (
        cpu_fit["optimization"]["latency_headroom_pct"]
    )
    assert matrix["counts"]["runtime_fit_optimal"] == 2

    readiness = store.deployment_readiness(
        package_id="pkg-runtime-fit",
        model_id="model-edge",
        device_id="edge-1",
    )

    assert readiness["selection"]["runtime_target_id"] == "gpu-fit"
    assert readiness["runtime_fit"]["runtime_target_id"] == "gpu-fit"
    assert readiness["runtime_fit"]["score"] == gpu_fit["score"]
    assert readiness["runtime_fit"]["target_selection"]["status"] == "best"
    assert readiness["runtime_fit"]["target_selection"]["selected_rank"] == 1
    assert readiness["runtime_fit"]["target_selection"]["best_runtime_target_id"] == "gpu-fit"
    target_assessments = readiness["runtime_fit"]["target_selection"]["target_assessments"]
    assessment_by_id = {
        assessment["runtime_target_id"]: assessment for assessment in target_assessments
    }
    assert len(target_assessments) == readiness["runtime_fit"]["target_selection"][
        "candidate_count"
    ]
    assert assessment_by_id["gpu-fit"]["selected"] is True
    assert assessment_by_id["gpu-fit"]["best"] is True
    assert assessment_by_id["gpu-fit"]["status"] == "eligible"
    assert assessment_by_id["gpu-fit"]["component_states"]["performance"]["state"] == (
        "slo met"
    )
    assert assessment_by_id["gpu-fit"]["runtime_capability_lock"]["status"] == "locked"
    assert assessment_by_id["gpu-fit"]["runtime_capability_lock"][
        "capability_sha256"
    ] == gpu_fit["runtime_capability_lock"]["capability_sha256"]
    assert assessment_by_id["gpu-fit"]["remediation"]["action"] == "ready"
    assert assessment_by_id["gpu-fit"]["remediation"]["label"] == "Use for field apply"
    assert assessment_by_id["gpu-fit"]["remediation"]["refs"]["package_id"] == (
        "pkg-runtime-fit"
    )
    assert assessment_by_id["gpu-fit"]["remediation"]["refs"]["model_id"] == "model-edge"
    assert assessment_by_id["gpu-fit"]["remediation"]["refs"]["device_id"] == "edge-1"
    assert assessment_by_id["gpu-fit"]["remediation"]["operator_command"][:5] == [
        "uv",
        "run",
        "temms",
        "hub",
        "edge-runtime-mission",
    ]
    assert "--require-best-runtime" in assessment_by_id["gpu-fit"]["remediation"][
        "operator_command"
    ]
    assert "pkg-runtime-fit" in assessment_by_id["gpu-fit"]["remediation"][
        "operator_command_text"
    ]
    assert assessment_by_id["cpu-fit"]["status"] == "eligible"
    assert assessment_by_id["temms-rpi5-tflite"]["status"] == "blocked"
    assert assessment_by_id["temms-rpi5-tflite"]["runtime_capability_lock"][
        "status"
    ] == "blocked"
    assert assessment_by_id["temms-rpi5-tflite"]["runtime_capability_lock"]["failures"]
    assert assessment_by_id["temms-rpi5-tflite"]["runtime_lane"]["lane_id"] == (
        "rpi5-tflite"
    )
    assert assessment_by_id["temms-rpi5-tflite"]["penalties"]
    assert assessment_by_id["temms-rpi5-tflite"]["remediation"]["action"] == (
        "select_matching_edge_class"
    )
    assert assessment_by_id["temms-rpi5-tflite"]["remediation"][
        "requires_edge_execution"
    ] is False
    assert assessment_by_id["temms-rpi5-tflite"]["remediation"][
        "operator_command"
    ][:5] == ["uv", "run", "temms", "hub", "compatibility-matrix"]
    assert "--include-device-inventory" in assessment_by_id["temms-rpi5-tflite"][
        "remediation"
    ]["operator_command"]
    assert "temms-rpi5-tflite" in assessment_by_id["temms-rpi5-tflite"][
        "remediation"
    ]["operator_command_text"]
    assert readiness["runtime_decision"]["schema_version"] == "temms-runtime-decision/v1"
    assert readiness["runtime_decision"]["recommended_action"] == "apply_or_stage"
    assert readiness["runtime_decision"]["target_selection"]["status"] == "best"
    assert readiness["runtime_decision"]["target_selection"]["best_runtime_target_id"] == "gpu-fit"
    assert readiness["runtime_decision"]["production_admission"]["apply_allowed"] is True
    assert readiness["runtime_decision"]["top_candidates"][0]["runtime_target_id"] == "gpu-fit"
    assert readiness["runtime_decision"]["top_candidates"][0]["runtime_lane"]["lane_id"] == (
        "jetson-cuda"
    )
    assert readiness["runtime_decision"]["runtime_capability_lock"]["status"] == "locked"
    assert readiness["runtime_decision"]["runtime_capability_lock"][
        "capability_sha256"
    ] == gpu_fit["runtime_capability_lock"]["capability_sha256"]
    assert readiness["runtime_decision"]["target_assessments"][0]["runtime_target_id"] == (
        "gpu-fit"
    )
    assert readiness["runtime_decision"]["target_assessments"][0]["best"] is True
    assert readiness["runtime_decision"]["target_assessments"][0]["remediation"][
        "action"
    ] == "ready"
    assert readiness["edge_execution_contract"]["schema_version"] == (
        "temms-edge-execution-contract/v1"
    )
    assert readiness["edge_execution_contract"]["path"]["label"] == (
        "model-edge -> gpu-fit -> edge-1"
    )
    assert readiness["edge_execution_contract"]["recommended_action"] == "apply_or_stage"
    assert readiness["edge_execution_contract"]["runtime_fit"]["score"] == gpu_fit["score"]
    assert readiness["edge_execution_contract"]["target_selection"]["status"] == "best"
    assert readiness["edge_execution_contract"]["runtime_capability_lock"]["status"] == (
        "locked"
    )
    assert readiness["edge_execution_contract"]["runtime_capability_lock"][
        "capability_sha256"
    ] == gpu_fit["runtime_capability_lock"]["capability_sha256"]
    assert readiness["edge_execution_contract"]["target_assessments"][0][
        "runtime_target_id"
    ] == "gpu-fit"
    assert readiness["edge_execution_contract"]["target_assessments"][0][
        "remediation"
    ]["label"] == "Use for field apply"
    assert {
        assessment["runtime_target_id"]
        for assessment in readiness["edge_execution_contract"]["target_assessments"]
        if assessment.get("status") == "blocked"
    } >= {"temms-rpi5-tflite", "temms-orin-tensorrt"}
    assert readiness["edge_execution_contract"]["proof_policy"] == {
        "require_go": True,
        "min_runtime_fit": 95,
        "require_best_runtime": True,
        "require_capability_lock": True,
        "require_proof_signature": True,
    }
    workbench = readiness["runtime_workbench"]
    assert workbench["schema_version"] == "temms-runtime-workbench/v1"
    assert workbench["selected_runtime_target_id"] == "gpu-fit"
    assert workbench["best_runtime_target_id"] == "gpu-fit"
    assert workbench["summary"]["target_count"] >= 4
    assert workbench["summary"]["selected_is_best"] is True
    assert workbench["selected_target"]["runtime_target_id"] == "gpu-fit"
    assert workbench["selected_target"]["proof"]["capability_lock_status"] == "locked"
    assert workbench["selected_target"]["proof"]["validation_id"]
    assert {
        target["runtime_target_id"]
        for target in workbench["targets"]
        if target.get("status") == "blocked"
    } >= {"temms-rpi5-tflite", "temms-orin-tensorrt"}
    assert readiness["runtime_fit"]["runtime_lane"]["lane_id"] == "jetson-cuda"
    assert readiness["runtime_fit"]["artifact_lane"]["state"] == "native artifact"
    assert (
        readiness["runtime_fit"]["target_selection"]["best_runtime_lane"]["lane_id"]
        == "jetson-cuda"
    )
    assert readiness["production_admission"] == {
        "schema_version": "temms-production-admission/v1",
        "status": "go",
        "apply_allowed": True,
        "detail": "Production apply is permitted for the selected edge runtime path",
        "blocking_gate_count": 0,
        "blocking_gates": [],
    }
    mission = readiness["edge_runtime_mission"]
    assert mission["schema_version"] == "temms-edge-runtime-mission/v1"
    assert mission["status"] == "attention"
    assert mission["headline"] == "Selected edge path needs operator proof"
    assert mission["path"]["label"] == "model-edge -> gpu-fit -> edge-1"
    assert mission["metrics"]["runtime_fit"]["score"] == gpu_fit["score"]
    assert mission["metrics"]["runtime_lane"]["lane_id"] == "jetson-cuda"
    assert mission["metrics"]["artifact_fit"]["state"] == "native artifact"
    assert mission["metrics"]["production_admission"]["apply_allowed"] is True
    assert mission["operator_focus"] == [
        "Create a rollout or staged rollout plan for the selected model"
    ]
    proof = hub_lite_module.build_edge_runtime_proof(
        readiness,
        require_go=False,
        min_runtime_fit=85,
    )
    assert proof["runtime_decision"]["schema_version"] == "temms-runtime-decision/v1"
    assert proof["runtime_decision"]["target_selection"]["status"] == "best"
    assert proof["edge_execution_contract"]["schema_version"] == (
        "temms-edge-execution-contract/v1"
    )
    assert proof["edge_execution_contract"]["target_selection"]["best_runtime_target_id"] == (
        "gpu-fit"
    )
    assert proof["edge_execution_contract"]["runtime_capability_lock"]["status"] == "locked"
    assert proof["edge_execution_contract"]["runtime_capability_lock"][
        "capability_sha256"
    ] == gpu_fit["runtime_capability_lock"]["capability_sha256"]
    assert proof["runtime_workbench"]["schema_version"] == "temms-runtime-workbench/v1"
    assert proof["runtime_workbench"]["selected_target"]["runtime_target_id"] == (
        "gpu-fit"
    )
    assert proof["runtime_workbench"]["selected_target"]["runtime_target"]["image"] == (
        "registry.example.com/gpu-fit:latest"
    )
    manifest = proof["edge_execution_manifest"]
    assert manifest["schema_version"] == "temms-edge-execution-manifest/v1"
    assert manifest["path"]["label"] == "model-edge -> gpu-fit -> edge-1"
    assert manifest["model"]["artifact_format"] == "onnx"
    assert manifest["execution"]["runtime_target_id"] == "gpu-fit"
    assert manifest["execution"]["runtime_image"] == (
        "registry.example.com/gpu-fit:latest"
    )
    assert manifest["execution"]["runtime_lane"]["lane_id"] == "jetson-cuda"
    assert manifest["execution"]["selected_is_best"] is True
    assert manifest["edge"]["capability_lock"]["status"] == "locked"
    assert manifest["edge"]["capability_lock"]["capability_sha256"] == (
        gpu_fit["runtime_capability_lock"]["capability_sha256"]
    )
    assert manifest["evidence"]["runtime_validation_id"]
    assert manifest["evidence"]["benchmark_id"]
    assert manifest["admission"]["gate_status"] == "passed"
    assert manifest["admission"]["gate_policy"]["min_runtime_fit"] == 85
    trace = proof["runtime_decision_trace"]
    assert trace["schema_version"] == "temms-runtime-decision-trace/v1"
    assert trace["source_schema_version"] == "temms-runtime-workbench/v1"
    assert trace["selected_runtime_target_id"] == "gpu-fit"
    assert trace["best_runtime_target_id"] == "gpu-fit"
    assert trace["selected_is_best"] is True
    assert trace["target_count"] == proof["runtime_workbench"]["summary"]["target_count"]
    assert trace["eligible_target_count"] >= 1
    assert trace["blocked_target_count"] >= 1
    trace_rows = {row["runtime_target_id"]: row for row in trace["rows"]}
    assert trace_rows["gpu-fit"]["selected"] is True
    assert trace_rows["gpu-fit"]["best"] is True
    assert trace_rows["gpu-fit"]["proof_components"]["runtime_validation"][
        "evidence_id"
    ]
    assert trace_rows["gpu-fit"]["proof_components"]["benchmark"]["evidence_id"]
    assert trace_rows["gpu-fit"]["capability_lock"]["status"] == "locked"
    assert any(command["runtime_target_id"] == "gpu-fit" for command in trace["commands"])
    component_digests = proof["component_digests"]
    assert component_digests["schema_version"] == (
        "temms-edge-runtime-proof-component-digests/v1"
    )
    assert component_digests["runtime_workbench_sha256"] == (
        hub_lite_module.canonical_json_hash(proof["runtime_workbench"])
    )
    assert component_digests["runtime_decision_trace_sha256"] == (
        hub_lite_module.canonical_json_hash(trace)
    )
    assert component_digests["edge_execution_manifest_sha256"] == (
        hub_lite_module.canonical_json_hash(manifest)
    )
    store.heartbeat("edge-1", status="online", inventory=edge_inventory)
    refreshed = store.deployment_readiness(
        package_id="pkg-runtime-fit",
        model_id="model-edge",
        device_id="edge-1",
    )
    assert refreshed["edge_execution_contract"]["runtime_capability_lock"][
        "capability_sha256"
    ] == gpu_fit["runtime_capability_lock"]["capability_sha256"]
    assert proof["edge_execution_contract"]["target_assessments"][0]["runtime_target_id"] == (
        "gpu-fit"
    )
    assert proof["edge_execution_contract"]["target_assessments"][0]["remediation"][
        "action"
    ] == "ready"
    unsigned_proof = dict(proof)
    recorded_proof_hash = unsigned_proof.pop("integrity")["payload_sha256"]
    assert hub_lite_module.canonical_json_hash(unsigned_proof) == recorded_proof_hash
    runtime_optimizer = {gate["gate_id"]: gate for gate in readiness["gates"]}[
        "runtime_optimizer"
    ]
    assert runtime_optimizer["status"] == "go"
    assert runtime_optimizer["state"] == "best target"

    pinned_cpu = store.deployment_readiness(
        package_id="pkg-runtime-fit",
        model_id="model-edge",
        device_id="edge-1",
        runtime_target_id="cpu-fit",
    )

    target_selection = pinned_cpu["runtime_fit"]["target_selection"]
    assert pinned_cpu["selection"]["runtime_target_id"] == "cpu-fit"
    assert target_selection["schema_version"] == "temms-runtime-target-selection/v1"
    assert target_selection["status"] == "upgrade_available"
    assert target_selection["selected_rank"] == 2
    assert target_selection["best_runtime_target_id"] == "gpu-fit"
    assert target_selection["best_score"] == gpu_fit["score"]
    assert target_selection["score_delta"] > 0
    alternative_ids = [target["runtime_target_id"] for target in target_selection["alternatives"]]
    assert alternative_ids[:2] == ["gpu-fit", "cpu-fit"]
    assert "temms-x86_64-cpu" in alternative_ids
    assert target_selection["alternatives"][0]["runtime_lane"]["lane_id"] == "jetson-cuda"
    pinned_optimizer = {gate["gate_id"]: gate for gate in pinned_cpu["gates"]}[
        "runtime_optimizer"
    ]
    assert pinned_optimizer["status"] == "attention"
    assert pinned_optimizer["state"] == "better target available"
    assert pinned_optimizer["actions"] == [
        {
            "action_id": "select_best_runtime_target",
            "label": "Use best runtime",
            "kind": "select_runtime_target",
            "refs": {
                "package_id": "pkg-runtime-fit",
                "model_id": "model-edge",
                "device_id": "edge-1",
                "runtime_target_id": "gpu-fit",
                "best_runtime_target_id": "gpu-fit",
                "previous_runtime_target_id": "cpu-fit",
                "selected_rank": 2,
                "best_score": gpu_fit["score"],
                "score_delta": target_selection["score_delta"],
            },
        }
    ]
    assert pinned_cpu["production_admission"]["apply_allowed"] is False
    assert pinned_cpu["production_admission"]["status"] == "blocked"
    assert pinned_cpu["production_admission"]["blocking_gate_count"] == 1
    assert pinned_cpu["runtime_decision"]["recommended_action"] == "use_best_runtime"
    assert pinned_cpu["edge_execution_contract"]["recommended_action"] == (
        "use_best_runtime"
    )
    assert pinned_cpu["edge_execution_contract"]["target_selection"]["best_runtime_target_id"] == (
        "gpu-fit"
    )
    assert pinned_cpu["runtime_decision"]["target_selection"]["status"] == "upgrade_available"
    assert pinned_cpu["runtime_decision"]["target_selection"]["best_runtime_target_id"] == (
        "gpu-fit"
    )
    assert pinned_cpu["runtime_workbench"]["target_selection"]["status"] == (
        "upgrade_available"
    )
    assert pinned_cpu["runtime_workbench"]["summary"]["selected_is_best"] is False
    assert pinned_cpu["runtime_workbench"]["selected_target"]["runtime_target_id"] == (
        "cpu-fit"
    )
    assert pinned_cpu["runtime_workbench"]["best_target"]["runtime_target_id"] == (
        "gpu-fit"
    )
    assert pinned_cpu["runtime_decision"].get("blocking_gates", []) == []
    assert pinned_cpu["runtime_decision"]["attention_gates"][0]["gate_id"] == (
        "runtime_optimizer"
    )
    pinned_mission = pinned_cpu["edge_runtime_mission"]
    assert pinned_mission["status"] == "attention"
    assert pinned_mission["metrics"]["target_selection"]["status"] == "upgrade_available"
    assert pinned_mission["metrics"]["runtime_decision"]["recommended_action"] == (
        "use_best_runtime"
    )
    assert pinned_mission["metrics"]["target_selection"]["best_runtime_target_id"] == "gpu-fit"
    assert pinned_mission["metrics"]["production_admission"]["apply_allowed"] is False
    assert any("gpu-fit scores" in item for item in pinned_mission["operator_focus"])
    admission_gate = pinned_cpu["production_admission"]["blocking_gates"][0]
    assert admission_gate["gate_id"] == "runtime_optimizer"
    assert admission_gate["refs"]["runtime_target_id"] == "cpu-fit"
    assert admission_gate["refs"]["best_runtime_target_id"] == "gpu-fit"

    blocked_runtime = store.deployment_readiness(
        package_id="pkg-runtime-fit",
        model_id="model-edge",
        device_id="edge-1",
        runtime_target_id="temms-rpi5-tflite",
    )
    blocked_optimizer = {gate["gate_id"]: gate for gate in blocked_runtime["gates"]}[
        "runtime_optimizer"
    ]
    assert blocked_runtime["status"] == "blocked"
    assert blocked_runtime["runtime_fit"]["target_selection"]["status"] == (
        "selected_not_eligible"
    )
    assert blocked_runtime["runtime_decision"]["recommended_action"] == "use_best_runtime"
    assert blocked_runtime["runtime_decision"]["target_selection"]["status"] == (
        "selected_not_eligible"
    )
    assert blocked_runtime["runtime_workbench"]["target_selection"]["status"] == (
        "selected_not_eligible"
    )
    assert blocked_runtime["runtime_workbench"]["selected_target"]["status"] == (
        "blocked"
    )
    assert blocked_runtime["runtime_workbench"]["best_target"]["runtime_target_id"] == (
        "gpu-fit"
    )
    assert blocked_runtime["runtime_decision"]["blocking_gates"]
    blocked_mission = blocked_runtime["edge_runtime_mission"]
    assert blocked_mission["status"] == "blocked"
    assert blocked_mission["metrics"]["runtime_fit"]["status"] == "blocked"
    assert blocked_mission["metrics"]["target_selection"]["status"] == "selected_not_eligible"
    assert blocked_mission["metrics"]["artifact_fit"]["status"] == "blocked"
    assert blocked_optimizer["status"] == "blocked"
    assert blocked_optimizer["actions"] == [
        {
            "action_id": "select_best_runtime_target",
            "label": "Use best runtime",
            "kind": "select_runtime_target",
            "refs": {
                "package_id": "pkg-runtime-fit",
                "model_id": "model-edge",
                "device_id": "edge-1",
                "runtime_target_id": "gpu-fit",
                "best_runtime_target_id": "gpu-fit",
                "previous_runtime_target_id": "temms-rpi5-tflite",
                "best_score": gpu_fit["score"],
                "score_delta": blocked_runtime["runtime_fit"]["target_selection"][
                    "score_delta"
                ],
            },
        }
    ]
    blocked_admission_optimizer = next(
        gate
        for gate in blocked_runtime["production_admission"]["blocking_gates"]
        if gate["gate_id"] == "runtime_optimizer"
    )
    assert blocked_admission_optimizer["actions"] == blocked_optimizer["actions"]


def test_rollout_plan_coordinates_batches_with_existing_assignment_gates(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    for device_id in ["edge-a", "edge-b"]:
        store.enroll_device(
            device_id,
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
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "b" * 64,
            "metadata": {
                "models": [
                    {
                        "id": "model-vision",
                        "runtime_constraints": {
                            "runtimes": ["onnxruntime"],
                            "providers": ["CPUExecutionProvider"],
                        },
                    }
                ]
            },
        }
    )
    _release_package(store, "pkg-vision")
    store.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "dry_run": False,
            "ok": True,
        },
        package_id="pkg-vision",
        actor="operator:test",
    )

    plan = store.create_rollout_plan(
        plan_id="plan-vision",
        package_id="pkg-vision",
        device_ids=["edge-a", "edge-b", "edge-a"],
        slot="vision",
        runtime_target_id="temms-x86_64-cpu",
        batch_size=1,
        require_runtime_validation=True,
        require_approval=True,
        actor="operator:planner",
    )

    assert plan["schema_version"] == "temms-rollout-plan/v1"
    assert plan["state"] == "ready"
    assert plan["counts"] == {
        "targets": 2,
        "pending": 2,
        "assigned": 0,
        "blocked": 0,
        "downloading": 0,
        "imported": 0,
        "activated": 0,
        "rolled_back": 0,
        "failed": 0,
    }
    assert [target["device_id"] for target in plan["targets"]] == ["edge-a", "edge-b"]

    first_batch = store.advance_rollout_plan("plan-vision", actor="operator:planner")

    assert first_batch["state"] == "ready"
    assert first_batch["counts"]["pending"] == 1
    assert first_batch["counts"]["assigned"] == 1
    first_rollout = store.get_rollout("plan-vision-b1-1")
    assert first_rollout["rollout_plan_id"] == "plan-vision"
    assert first_rollout["rollout_plan_batch"] == 1
    assert first_rollout["approval_required"] is True

    paused = store.pause_rollout_plan(
        "plan-vision",
        actor="operator:planner",
        reason="hold for canary health",
    )
    assert paused["state"] == "paused"
    with pytest.raises(ValueError, match="paused"):
        store.advance_rollout_plan("plan-vision", actor="operator:planner")

    resumed = store.resume_rollout_plan("plan-vision", actor="operator:planner")
    assert resumed["state"] == "ready"
    advancing = store.advance_rollout_plan("plan-vision", actor="operator:planner")
    assert advancing["state"] == "advancing"
    assert advancing["counts"]["pending"] == 0
    assert advancing["counts"]["assigned"] == 2
    assert store.get_rollout("plan-vision-b2-1")["device_id"] == "edge-b"
    assert [event["state"] for event in advancing["history"]] == [
        "created",
        "advanced",
        "paused",
        "ready",
        "advanced",
    ]

    activated = store.update_rollout_status(
        "plan-vision-b1-1",
        "activated",
        detail="edge-a activated",
        actor="edge:edge-a",
    )
    assert activated["state"] == "activated"
    plan_after_activation = store.get_rollout_plan("plan-vision")
    assert plan_after_activation["state"] == "advancing"
    assert plan_after_activation["counts"]["activated"] == 1
    assert plan_after_activation["counts"]["assigned"] == 1
    assert plan_after_activation["targets"][0]["state"] == "activated"
    assert plan_after_activation["targets"][0]["last_actor"] == "edge:edge-a"

    store.update_rollout_status(
        "plan-vision-b2-1",
        "rolled_back",
        detail="edge-b rollback complete",
        actor="edge:edge-b",
    )
    completed = store.get_rollout_plan("plan-vision")
    assert completed["state"] == "completed"
    assert completed["counts"]["activated"] == 1
    assert completed["counts"]["rolled_back"] == 1
    assert completed["counts"]["assigned"] == 0
    assert completed["targets"][1]["state"] == "rolled_back"
    assert completed["history"][-1]["state"] == "reconciled"
    assert completed["history"][-1]["counts"]["rolled_back"] == 1


def test_runtime_validation_records_redact_signing_key_and_export(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "a" * 64,
        }
    )

    record = store.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "package_path": "/tmp/pkg.temms.tar.zst",
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
        package_id="pkg-vision",
        actor="operator:alice",
    )

    assert record["validation_id"].startswith("runtime-validation-")
    assert record["actor"] == "operator:alice"
    assert record["source_sha256"] == "a" * 64
    assert "secret" not in " ".join(record["result"]["command"])
    assert "secret" not in record["result"]["command_text"]
    assert "TEMMS_PACKAGE_SIGNING_KEY=********" in record["result"]["command"]
    assert record["result"]["command"][-1] == "********"
    assert store.list_runtime_validations(package_id="pkg-vision")[0]["validation_id"] == (
        record["validation_id"]
    )
    exported = store.export_bundle()
    assert record["validation_id"] in exported["hub_lite"]["runtime_validations"]


def test_benchmark_records_filter_and_airgap_export(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "c" * 64,
        }
    )

    record = store.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-vision",
            "slot": "vision",
            "latency_ms": {"p50": 4.0, "p95": 8.0},
            "throughput": {"inferences_per_second": 125.0},
        },
        device_id="edge-1",
        package_id="pkg-vision",
        runtime_target_id="temms-x86_64-cpu",
        actor="edge:edge-1",
    )

    assert record["benchmark_id"].startswith("benchmark-")
    assert record["device"]["profile"] == "x86_64-cpu"
    assert record["package"]["package_id"] == "pkg-vision"
    assert record["source_sha256"] == "c" * 64
    assert record["runtime_target"]["image"] == "temms/agent:inference-amd64"
    assert record["result"]["latency_ms"]["p95"] == 8.0
    assert store.list_benchmarks(device_id="edge-1")[0]["benchmark_id"] == (record["benchmark_id"])
    assert store.list_benchmarks(package_id="missing") == []
    exported = store.export_bundle()
    assert record["benchmark_id"] in exported["hub_lite"]["benchmarks"]

    imported = HubLiteStore(temp_dir / "imported-hub.json")
    counts = imported.import_bundle(exported)
    assert counts["benchmarks"] == 1
    assert imported.list_benchmarks(model_id="model-vision")[0]["benchmark_id"] == (
        record["benchmark_id"]
    )


def test_rollout_assignment_can_require_passing_runtime_validation(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "b" * 64,
            "metadata": {
                "models": [
                    {
                        "id": "model-vision",
                        "runtime_constraints": {
                            "runtimes": ["onnxruntime"],
                            "providers": ["CPUExecutionProvider"],
                        },
                    }
                ]
            },
        }
    )
    _release_package(store, "pkg-vision")

    with pytest.raises(ValueError, match="No passing runtime validation"):
        store.assign_rollout(
            "edge-1",
            "pkg-vision",
            slot="vision",
            runtime_target_id="temms-x86_64-cpu",
            require_runtime_validation=True,
        )

    store.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "package_path": "/tmp/pkg-vision.temms.tar.zst",
            "command": ["docker", "run", "temms/agent:inference-amd64"],
            "dry_run": True,
            "ok": True,
        },
        package_id="pkg-vision",
        actor="operator:preview",
    )

    with pytest.raises(ValueError, match="No passing runtime validation"):
        store.assign_rollout(
            "edge-1",
            "pkg-vision",
            slot="vision",
            runtime_target_id="temms-x86_64-cpu",
            require_runtime_validation=True,
        )

    validation = store.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "package_path": "/tmp/pkg-vision.temms.tar.zst",
            "command": ["docker", "run", "temms/agent:inference-amd64"],
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-vision",
        actor="operator:alice",
    )

    rollout = store.assign_rollout(
        "edge-1",
        "pkg-vision",
        slot="vision",
        rollout_id="rollout-gated",
        runtime_target_id="temms-x86_64-cpu",
        require_runtime_validation=True,
    )

    assert rollout["runtime_validation_required"] is True
    assert rollout["runtime_validation"]["validation_id"] == validation["validation_id"]
    assert rollout["runtime_validation"]["dry_run"] is False
    assert rollout["runtime_validation"]["ok"] is True


def test_deployment_readiness_evaluates_model_performance_slo(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device(
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
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "c" * 64,
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {
                        "id": "model-fast",
                        "runtime_constraints": {"runtimes": ["onnxruntime"]},
                        "performance_slo": {
                            "max_latency_ms_p95": 10.0,
                            "min_throughput_ips": 90.0,
                        },
                    },
                    {
                        "id": "model-efficient",
                        "runtime_constraints": {"runtimes": ["onnxruntime"]},
                        "performance_slo": {
                            "max_latency_ms_p95": 5.0,
                            "min_throughput_ips": 200.0,
                        },
                    }
                ],
            },
        }
    )
    _release_package(store, "pkg-vision")
    store.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "package_path": "/tmp/pkg-vision.temms.tar.zst",
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-vision",
        actor="operator:test",
    )
    rollout = store.assign_rollout(
        "edge-1",
        "pkg-vision",
        model_id="model-fast",
        slot="vision",
        rollout_id="rollout-fast",
        runtime_target_id="temms-x86_64-cpu",
    )
    store.update_rollout_status(rollout["rollout_id"], "activated", detail="activated")

    missing = store.deployment_readiness(
        package_id="pkg-vision",
        model_id="model-fast",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    missing_gates = {gate["gate_id"]: gate for gate in missing["gates"]}
    assert missing_gates["performance_fit"]["status"] == "attention"
    assert missing_gates["performance_fit"]["state"] == "drift unverified"
    assert "cannot prove its performance SLO" in missing_gates["performance_fit"]["detail"]
    assert missing_gates["performance_fit"]["refs"]["max_latency_ms_p95"] == 10.0
    assert missing["actions"][0]["action_id"] == "record_performance_benchmark"
    assert missing["actions"][0]["command"] == {
        "method": "POST",
        "path": "/v1/hub/benchmarks",
        "body": {
            "device_id": "edge-1",
            "package_id": "pkg-vision",
            "runtime_target_id": "temms-x86_64-cpu",
            "actor": "edge-agent",
            "result": {
                "schema_version": "temms-benchmark/v1",
                "model_id": "model-fast",
                "slot": "vision",
            },
        },
        "requires_edge_execution": True,
        "edge_command": [
            "temms",
            "benchmark",
            "model-fast",
            "--slot",
            "vision",
            "--samples",
            "10",
            "--warmup",
            "2",
            "--hub-url",
            "${TEMMS_HUB_URL}",
            "--device-id",
            "edge-1",
            "--package-id",
            "pkg-vision",
            "--runtime-target-id",
            "temms-x86_64-cpu",
            "--actor",
            "edge-agent",
        ],
        "edge_command_text": (
            "temms benchmark model-fast --slot vision --samples 10 --warmup 2 "
            "--hub-url '${TEMMS_HUB_URL}' --device-id edge-1 --package-id "
            "pkg-vision --runtime-target-id temms-x86_64-cpu --actor edge-agent"
        ),
        "edge_command_note": (
            "Run on the selected edge after the model package is cached; "
            "the central API body is only the target envelope for the published result."
        ),
    }

    store.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-efficient",
            "slot": "vision",
            "latency_ms": {"p95": 3.0},
            "throughput": {"inferences_per_second": 250.0},
        },
        device_id="edge-1",
        package_id="pkg-vision",
        runtime_target_id="temms-x86_64-cpu",
        actor="edge:edge-1",
    )
    slow_benchmark = store.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-fast",
            "slot": "vision",
            "latency_ms": {"p95": 12.0},
            "throughput": {"inferences_per_second": 100.0},
        },
        device_id="edge-1",
        package_id="pkg-vision",
        runtime_target_id="temms-x86_64-cpu",
        actor="edge:edge-1",
    )
    slow = store.deployment_readiness(
        package_id="pkg-vision",
        model_id="model-fast",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    slow_gate = {gate["gate_id"]: gate for gate in slow["gates"]}["performance_fit"]
    assert slow["status"] == "blocked"
    assert slow_gate["status"] == "blocked"
    assert slow_gate["state"] == "performance drift"
    assert slow_gate["refs"]["performance_drift"] is True
    assert slow_gate["refs"]["rollout_id"] == "rollout-fast"
    assert "Active rollout rollout-fast no longer meets" in slow_gate["detail"]
    assert "p95 latency 12 ms exceeds SLO 10 ms" in slow_gate["detail"]
    fallback_action = slow_gate["actions"][0]
    assert fallback_action["action_id"] == "stage_fallback_model"
    assert fallback_action["kind"] == "create_rollout"
    assert fallback_action["refs"]["model_id"] == "model-efficient"
    assert fallback_action["refs"]["fallback_for_model_id"] == "model-fast"
    assert fallback_action["refs"]["fallback_reason"] == "performance drift"
    assert fallback_action["refs"]["require_runtime_validation"] is True
    assert fallback_action["refs"]["fallback_runtime_validation_id"]
    assert fallback_action["refs"]["fallback_benchmark_id"]
    expected_fallback_rollout_id = hub_lite_module._readiness_command_id(
        "rollout",
        fallback_action["refs"],
        ["package_id", "model_id", "device_id", "runtime_target_id", "slot"],
    )
    assert fallback_action["command"] == {
        "method": "POST",
        "path": "/v1/hub/rollouts",
        "body": {
            "rollout_id": expected_fallback_rollout_id,
            "package_id": "pkg-vision",
            "model_id": "model-efficient",
            "device_id": "edge-1",
            "runtime_target_id": "temms-x86_64-cpu",
            "slot": "vision",
            "require_approval": True,
            "require_runtime_validation": True,
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate fallback for performance drift",
        },
    }
    assert slow_gate["actions"][1]["kind"] == "rollback_rollout"
    assert slow_gate["actions"][1]["command"] == {
        "method": "POST",
        "path": "/v1/hub/rollouts/rollout-fast/rollback",
        "body": {
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate performance drift",
        },
    }

    passing_benchmark = store.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-fast",
            "slot": "vision",
            "latency_ms": {"p95": 8.0},
            "throughput": {"inferences_per_second": 125.0},
        },
        device_id="edge-1",
        package_id="pkg-vision",
        runtime_target_id="temms-x86_64-cpu",
        actor="edge:edge-1",
    )
    ready = store.deployment_readiness(
        package_id="pkg-vision",
        model_id="model-fast",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    ready_gate = {gate["gate_id"]: gate for gate in ready["gates"]}["performance_fit"]
    assert ready_gate["status"] == "go"
    assert ready_gate["state"] == "slo met"
    assert ready_gate["refs"]["benchmark_id"] == passing_benchmark["benchmark_id"]
    assert ready_gate["refs"]["latency_ms_p95"] == 8.0
    assert ready_gate["refs"]["throughput_ips"] == 125.0

    matrix = store.compatibility_matrix(
        package_ids=["pkg-vision"],
        model_ids=["model-fast"],
        device_ids=["edge-1"],
        runtime_target_ids=["temms-x86_64-cpu"],
    )
    runtime_cell = matrix["cells"][0]
    assert runtime_cell["performance_ready"] is True
    assert runtime_cell["performance"]["benchmark"]["benchmark_id"] == (
        passing_benchmark["benchmark_id"]
    )
    assert matrix["counts"]["performance_ready"] == 1
    assert matrix["counts"]["performance_attention"] == 0

    _backdate_benchmark_created_at(
        store,
        slow_benchmark["benchmark_id"],
        "2026-01-01T00:00:00Z",
    )
    _backdate_benchmark_created_at(
        store,
        passing_benchmark["benchmark_id"],
        "2026-01-02T00:00:00Z",
    )
    stale = store.deployment_readiness(
        package_id="pkg-vision",
        model_id="model-fast",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    stale_gate = {gate["gate_id"]: gate for gate in stale["gates"]}["performance_fit"]
    assert stale["status"] == "attention"
    assert stale_gate["status"] == "attention"
    assert stale_gate["state"] == "drift unverified"
    assert "benchmark evidence is" in stale_gate["detail"]
    assert stale_gate["refs"]["benchmark_id"] == passing_benchmark["benchmark_id"]
    assert stale_gate["refs"]["benchmark_age_seconds"] > (
        stale_gate["refs"]["benchmark_stale_after_seconds"]
    )

    stale_matrix = store.compatibility_matrix(
        package_ids=["pkg-vision"],
        model_ids=["model-fast"],
        device_ids=["edge-1"],
        runtime_target_ids=["temms-x86_64-cpu"],
    )
    stale_cell = stale_matrix["cells"][0]
    assert stale_cell["performance_ready"] is False
    assert stale_cell["performance"]["state"] == "benchmark stale"
    assert stale_matrix["counts"]["performance_ready"] == 0
    assert stale_matrix["counts"]["performance_attention"] == 1


def test_deployment_readiness_evaluates_resource_envelope(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device(
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
    store.upsert_package(
        {
            "package_id": "pkg-resource",
            "name": "resource",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "d" * 64,
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {
                        "id": "model-edge",
                        "size_bytes": 10485760,
                        "runtime_constraints": {"runtimes": ["onnxruntime"]},
                        "resource_requirements": {
                            "min_memory_available_mb": 512.0,
                            "min_storage_available_mb": 128.0,
                            "max_temperature_c": 80.0,
                        },
                    },
                    {
                        "id": "model-lite",
                        "size_bytes": 1048576,
                        "runtime_constraints": {"runtimes": ["onnxruntime"]},
                        "resource_requirements": {
                            "min_memory_available_mb": 128.0,
                            "min_storage_available_mb": 64.0,
                            "max_temperature_c": 85.0,
                        },
                    }
                ],
            },
        }
    )
    _release_package(store, "pkg-resource")
    store.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-resource",
        actor="operator:test",
    )

    missing = store.deployment_readiness(
        package_id="pkg-resource",
        model_id="model-edge",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    missing_gate = {gate["gate_id"]: gate for gate in missing["gates"]}[
        "resource_envelope"
    ]
    assert missing_gate["status"] == "attention"
    assert missing_gate["state"] == "telemetry missing"
    assert "available memory" in missing_gate["refs"]["resource_missing"]
    assert "available storage" in missing_gate["refs"]["resource_missing"]

    store.heartbeat(
        "edge-1",
        status="online",
        inventory={
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                }
            },
            "memory": {"available_mb": 256.0},
            "storage": {"available_mb": 256.0},
            "thermal": {"temperature_c": 72.0},
        },
    )
    constrained = store.deployment_readiness(
        package_id="pkg-resource",
        model_id="model-edge",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    constrained_gate = {gate["gate_id"]: gate for gate in constrained["gates"]}[
        "resource_envelope"
    ]
    assert constrained_gate["status"] == "blocked"
    assert constrained_gate["state"] == "constrained"
    assert "available memory 256 MB below required 512 MB" in constrained_gate["detail"]

    store.heartbeat(
        "edge-1",
        status="online",
        inventory={
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                }
            },
            "memory": {"available_mb": 2048.0},
            "storage": {"available_mb": 1024.0},
            "thermal": {"temperature_c": 45.0},
        },
    )
    ready = store.deployment_readiness(
        package_id="pkg-resource",
        model_id="model-edge",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    ready_gate = {gate["gate_id"]: gate for gate in ready["gates"]}[
        "resource_envelope"
    ]
    assert ready_gate["status"] == "go"
    assert ready_gate["state"] == "met"
    assert ready_gate["refs"]["memory_available_mb"] == 2048.0
    assert ready_gate["refs"]["artifact_size_mb"] == 10.0

    _backdate_device_last_seen(store, "edge-1", "2026-01-01T00:00:00Z")
    stale = store.deployment_readiness(
        package_id="pkg-resource",
        model_id="model-edge",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    stale_gates = {gate["gate_id"]: gate for gate in stale["gates"]}
    assert stale["status"] == "attention"
    assert stale_gates["runtime_target"]["status"] == "attention"
    assert stale_gates["runtime_target"]["state"] == "inventory stale"
    assert stale_gates["runtime_target"]["refs"]["telemetry_state"] == "telemetry stale"
    assert stale_gates["resource_envelope"]["status"] == "attention"
    assert stale_gates["resource_envelope"]["state"] == "telemetry stale"
    assert stale_gates["resource_envelope"]["refs"]["heartbeat_age_seconds"] > (
        stale_gates["resource_envelope"]["refs"]["heartbeat_stale_after_seconds"]
    )
    assert stale_gates["edge_target"]["status"] == "attention"
    assert stale_gates["edge_target"]["state"] == "telemetry stale"
    stale_lock = stale["runtime_fit"]["runtime_capability_lock"]
    assert stale_lock["status"] == "blocked"
    assert stale_lock["edge_inventory"]["telemetry_freshness"]["status"] == "attention"
    assert stale_lock["edge_inventory"]["telemetry_freshness"]["state"] == (
        "telemetry stale"
    )
    assert len(stale_lock["failures"]) == 1
    assert stale_lock["failures"][0].startswith(
        "edge inventory freshness is not locked: last heartbeat was "
    )
    assert "freshness budget is 5 minutes" in stale_lock["failures"][0]
    stale_proof = hub_lite_module.build_edge_runtime_proof(
        stale,
        require_capability_lock=True,
    )
    assert stale_proof["gate_status"] == "failed"
    assert stale_proof["gate_failures"] == [
        "runtime capability lock status is blocked, expected locked",
        "runtime capability lock has failures: " + stale_lock["failures"][0],
    ]

    store.heartbeat(
        "edge-1",
        status="online",
        inventory={
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                }
            },
            "memory": {"available_mb": 2048.0},
            "storage": {"available_mb": 1024.0},
            "thermal": {"temperature_c": 45.0},
        },
    )
    matrix = store.compatibility_matrix(
        package_ids=["pkg-resource"],
        model_ids=["model-edge"],
        device_ids=["edge-1"],
        runtime_target_ids=["temms-x86_64-cpu"],
    )
    cell = matrix["cells"][0]
    assert cell["resource_ready"] is True
    assert cell["resource_envelope"]["state"] == "met"
    assert matrix["counts"]["resource_ready"] == 1
    assert matrix["counts"]["resource_blocked"] == 0

    rollout = store.assign_rollout(
        "edge-1",
        "pkg-resource",
        slot="vision",
        rollout_id="rollout-resource-drift",
        runtime_target_id="temms-x86_64-cpu",
        model_id="model-edge",
    )
    assert rollout["state"] == "assigned"
    store.update_rollout_status(
        "rollout-resource-drift",
        "activated",
        detail="activated model-edge",
        actor="edge:test",
    )
    store.heartbeat(
        "edge-1",
        status="online",
        inventory={
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                }
            },
            "memory": {"available_mb": 256.0},
            "storage": {"available_mb": 1024.0},
            "thermal": {"temperature_c": 45.0},
        },
    )
    drifted = store.deployment_readiness(
        package_id="pkg-resource",
        model_id="model-edge",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    drift_gate = {gate["gate_id"]: gate for gate in drifted["gates"]}[
        "resource_envelope"
    ]
    assert drifted["status"] == "blocked"
    assert drift_gate["status"] == "blocked"
    assert drift_gate["state"] == "resource drift"
    assert drift_gate["refs"]["resource_drift"] is True
    assert drift_gate["refs"]["rollout_id"] == "rollout-resource-drift"
    assert "Active rollout rollout-resource-drift no longer satisfies" in drift_gate["detail"]
    fallback_action = drift_gate["actions"][0]
    assert fallback_action["action_id"] == "stage_fallback_model"
    assert fallback_action["kind"] == "create_rollout"
    assert fallback_action["refs"]["model_id"] == "model-lite"
    assert fallback_action["refs"]["fallback_for_model_id"] == "model-edge"
    assert fallback_action["refs"]["fallback_reason"] == "resource drift"
    assert fallback_action["refs"]["require_runtime_validation"] is True
    assert fallback_action["refs"]["fallback_runtime_validation_id"]
    expected_fallback_rollout_id = hub_lite_module._readiness_command_id(
        "rollout",
        fallback_action["refs"],
        ["package_id", "model_id", "device_id", "runtime_target_id", "slot"],
    )
    assert fallback_action["command"] == {
        "method": "POST",
        "path": "/v1/hub/rollouts",
        "body": {
            "rollout_id": expected_fallback_rollout_id,
            "package_id": "pkg-resource",
            "model_id": "model-lite",
            "device_id": "edge-1",
            "runtime_target_id": "temms-x86_64-cpu",
            "slot": "vision",
            "require_approval": True,
            "require_runtime_validation": True,
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate fallback for resource drift",
        },
    }
    assert drift_gate["actions"][1]["kind"] == "rollback_rollout"
    assert drift_gate["actions"][1]["command"] == {
        "method": "POST",
        "path": "/v1/hub/rollouts/rollout-resource-drift/rollback",
        "body": {
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate resource drift",
        },
    }


def test_deployment_readiness_requires_exact_model_rollout(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.heartbeat("edge-1", status="online")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "path": "/tmp/pkg-vision.temms.tar.zst",
            "sha256": "c" * 64,
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {"id": "model-a", "runtime_constraints": {"runtimes": ["onnxruntime"]}},
                    {"id": "model-b", "runtime_constraints": {"runtimes": ["onnxruntime"]}},
                ],
            },
        }
    )
    _release_package(store, "pkg-vision")
    store.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "package_path": "/tmp/pkg-vision.temms.tar.zst",
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-vision",
        actor="operator:test",
    )
    package_level = store.assign_rollout(
        "edge-1",
        "pkg-vision",
        slot="vision",
        rollout_id="rollout-package-level",
        runtime_target_id="temms-x86_64-cpu",
    )
    store.update_rollout_status(
        package_level["rollout_id"],
        "activated",
        detail="activated package-level rollout",
    )
    model_a = store.assign_rollout(
        "edge-1",
        "pkg-vision",
        model_id="model-a",
        slot="vision",
        rollout_id="rollout-model-a",
        runtime_target_id="temms-x86_64-cpu",
    )
    store.update_rollout_status(
        model_a["rollout_id"],
        "activated",
        detail="activated model-a",
    )

    readiness_a = store.deployment_readiness(
        package_id="pkg-vision",
        model_id="model-a",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    readiness_b = store.deployment_readiness(
        package_id="pkg-vision",
        model_id="model-b",
        device_id="edge-1",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    gates_a = {gate["gate_id"]: gate for gate in readiness_a["gates"]}
    gates_b = {gate["gate_id"]: gate for gate in readiness_b["gates"]}

    assert gates_a["rollout_gate"]["status"] == "go"
    assert gates_a["rollout_gate"]["refs"]["rollout_id"] == "rollout-model-a"
    assert readiness_b["status"] == "attention"
    assert gates_b["rollout_gate"]["status"] == "attention"
    assert gates_b["rollout_gate"]["state"] == "not assigned"
    assert gates_b["rollout_gate"]["refs"] == {
        "package_id": "pkg-vision",
        "model_id": "model-b",
        "device_id": "edge-1",
        "runtime_target_id": "temms-x86_64-cpu",
        "slot": "vision",
        "require_approval": True,
    }
    assert [action["label"] for action in gates_b["rollout_gate"]["actions"]] == [
        "Create rollout",
        "Create staged plan",
    ]
    assert [action["action_id"] for action in readiness_b["actions"]] == [
        "create_rollout",
        "create_rollout_plan",
    ]
    assert readiness_b["actions"][0]["refs"] == {
        "package_id": "pkg-vision",
        "model_id": "model-b",
        "device_id": "edge-1",
        "runtime_target_id": "temms-x86_64-cpu",
        "slot": "vision",
        "require_approval": True,
    }
    expected_rollout_id = hub_lite_module._readiness_command_id(
        "rollout",
        readiness_b["actions"][0]["refs"],
        ["package_id", "model_id", "device_id", "runtime_target_id", "slot"],
    )
    assert readiness_b["actions"][0]["command"] == {
        "method": "POST",
        "path": "/v1/hub/rollouts",
        "body": {
            "rollout_id": expected_rollout_id,
            "package_id": "pkg-vision",
            "model_id": "model-b",
            "device_id": "edge-1",
            "runtime_target_id": "temms-x86_64-cpu",
            "slot": "vision",
            "require_approval": True,
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate rollout assignment",
        },
    }
    assert readiness_b["actions"][1]["refs"] == {
        "package_id": "pkg-vision",
        "model_id": "model-b",
        "device_ids": ["edge-1"],
        "runtime_target_id": "temms-x86_64-cpu",
        "slot": "vision",
        "batch_size": 1,
        "require_approval": True,
    }
    expected_plan_id = hub_lite_module._readiness_command_id(
        "plan",
        readiness_b["actions"][1]["refs"],
        ["package_id", "model_id", "device_ids", "runtime_target_id", "slot"],
    )
    assert readiness_b["actions"][1]["command"] == {
        "method": "POST",
        "path": "/v1/hub/rollout-plans",
        "body": {
            "plan_id": expected_plan_id,
            "package_id": "pkg-vision",
            "model_id": "model-b",
            "device_ids": ["edge-1"],
            "runtime_target_id": "temms-x86_64-cpu",
            "slot": "vision",
            "batch_size": 1,
            "require_approval": True,
            "actor": "operator:readiness-remediation",
            "reason": "readiness gate staged rollout plan",
        },
    }


def test_explicit_rollout_ids_are_retry_safe(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
        }
    )
    _release_package(store, "pkg-vision")

    first = store.assign_rollout(
        "edge-1",
        "pkg-vision",
        slot="vision",
        rollout_id="rollout-deterministic",
        runtime_target_id="temms-x86_64-cpu",
        require_approval=True,
        actor="operator:first",
        reason="readiness gate rollout assignment",
    )
    retried = store.assign_rollout(
        "edge-1",
        "pkg-vision",
        slot="vision",
        rollout_id="rollout-deterministic",
        runtime_target_id="temms-x86_64-cpu",
        require_approval=True,
        actor="operator:retry",
        reason="readiness gate rollout assignment",
    )

    assert retried == first
    assert retried["history"] == first["history"]
    assert retried["reason"] == "readiness gate rollout assignment"
    assert retried["history"][0]["detail"] == "readiness gate rollout assignment"

    with pytest.raises(ValueError, match="already exists with different slot"):
        store.assign_rollout(
            "edge-1",
            "pkg-vision",
            slot="thermal",
            rollout_id="rollout-deterministic",
            runtime_target_id="temms-x86_64-cpu",
            require_approval=True,
        )


def test_explicit_rollout_plan_ids_are_retry_safe(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
        }
    )
    _release_package(store, "pkg-vision")

    first = store.create_rollout_plan(
        plan_id="plan-deterministic",
        package_id="pkg-vision",
        device_ids=["edge-1", "edge-1"],
        slot="vision",
        runtime_target_id="temms-x86_64-cpu",
        batch_size=1,
        require_approval=True,
        actor="operator:first",
        reason="readiness gate staged rollout plan",
    )
    retried = store.create_rollout_plan(
        plan_id="plan-deterministic",
        package_id="pkg-vision",
        device_ids=["edge-1"],
        slot="vision",
        runtime_target_id="temms-x86_64-cpu",
        batch_size=1,
        require_approval=True,
        actor="operator:retry",
        reason="readiness gate staged rollout plan",
    )

    assert retried == first
    assert retried["history"] == first["history"]
    assert retried["reason"] == "readiness gate staged rollout plan"
    assert retried["history"][0]["detail"] == "readiness gate staged rollout plan"

    with pytest.raises(ValueError, match="already exists with different batch_size"):
        store.create_rollout_plan(
            plan_id="plan-deterministic",
            package_id="pkg-vision",
            device_ids=["edge-1"],
            slot="vision",
            runtime_target_id="temms-x86_64-cpu",
            batch_size=2,
            require_approval=True,
        )


def test_readiness_remediation_commands_include_audit_actor():
    actor = "operator:readiness-remediation"

    promote = hub_lite_module._readiness_action_command(
        "promote_package",
        {"package_id": "pkg-vision", "target_state": "released"},
    )
    approve = hub_lite_module._readiness_action_command(
        "approve_rollout",
        {"rollout_id": "rollout-1"},
    )
    apply = hub_lite_module._readiness_action_command(
        "apply_rollout",
        {"rollout_id": "rollout-1", "model_id": "model-a"},
    )

    assert promote == {
        "method": "POST",
        "path": "/v1/hub/packages/pkg-vision/promote",
        "body": {
            "state": "released",
            "actor": actor,
            "reason": "readiness gate package promotion",
        },
    }
    assert approve == {
        "method": "POST",
        "path": "/v1/hub/rollouts/rollout-1/approve",
        "body": {
            "actor": actor,
            "reason": "readiness gate approval",
        },
    }
    assert apply == {
        "method": "POST",
        "path": "/v1/hub/rollouts/rollout-1/apply",
        "body": {
            "model_id": "model-a",
            "actor": actor,
        },
    }


def test_state_write_failure_preserves_previous_hub_lite_state(temp_dir, monkeypatch):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    previous_payload = store.path.read_text(encoding="utf-8")
    original_replace = Path.replace

    def fail_state_replace(path, target):
        if Path(target) == store.path:
            raise OSError("simulated replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_state_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        store.heartbeat("edge-1", status="online")

    assert store.path.read_text(encoding="utf-8") == previous_payload
    device = store.get_device("edge-1")
    assert device["profile"] == "x86_64-cpu"
    assert "status" not in device
    assert not list(temp_dir.glob(".hub.json-*"))


def test_airgap_import_preserves_newer_local_rollout_state(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    stale_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {},
            "rollouts": {
                "rollout-1": {
                    "rollout_id": "rollout-1",
                    "device_id": "edge-1",
                    "package_id": "pkg-1",
                    "slot": "vision",
                    "state": "assigned",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "history": [
                        {
                            "state": "assigned",
                            "updated_at": "2026-01-01T00:00:00Z",
                            "detail": "assigned centrally",
                            "actor": "operator:alice",
                        }
                    ],
                }
            },
            "deployment_status": {
                "edge-1": {
                    "device_id": "edge-1",
                    "state": "ASSIGNED",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            },
        },
    }
    newer_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {},
            "rollouts": {
                "rollout-1": {
                    "rollout_id": "rollout-1",
                    "device_id": "edge-1",
                    "package_id": "pkg-1",
                    "slot": "vision",
                    "state": "activated",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:05:00Z",
                    "history": [
                        {
                            "state": "activated",
                            "updated_at": "2026-01-01T00:05:00Z",
                            "detail": "loaded locally",
                            "actor": "edge:edge-1",
                        }
                    ],
                }
            },
            "deployment_status": {
                "edge-1": {
                    "device_id": "edge-1",
                    "state": "READY",
                    "updated_at": "2026-01-01T00:05:00Z",
                }
            },
        },
    }

    store.import_bundle(newer_bundle)
    counts = store.import_bundle(stale_bundle)

    rollout = store.get_rollout("rollout-1")
    assert counts["rollouts"] == 1
    assert rollout["state"] == "activated"
    assert [event["state"] for event in rollout["history"]] == [
        "assigned",
        "activated",
    ]
    deployment = store.deployment_status()["deployment_status"]["edge-1"]
    assert deployment["state"] == "READY"


def test_airgap_import_accepts_newer_rollout_state(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    old_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {},
            "rollouts": {
                "rollout-1": {
                    "rollout_id": "rollout-1",
                    "state": "assigned",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "history": [
                        {
                            "state": "assigned",
                            "updated_at": "2026-01-01T00:00:00Z",
                        }
                    ],
                }
            },
        },
    }
    new_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {},
            "rollouts": {
                "rollout-1": {
                    "rollout_id": "rollout-1",
                    "state": "rolled_back",
                    "updated_at": "2026-01-01T00:10:00Z",
                    "history": [
                        {
                            "state": "rolled_back",
                            "updated_at": "2026-01-01T00:10:00Z",
                        }
                    ],
                }
            },
        },
    }

    store.import_bundle(old_bundle)
    store.import_bundle(new_bundle)

    rollout = store.get_rollout("rollout-1")
    assert rollout["state"] == "rolled_back"
    assert [event["state"] for event in rollout["history"]] == [
        "assigned",
        "rolled_back",
    ]


def test_airgap_import_skips_stale_package_artifact(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")

    def bundle(updated_at: str, content: bytes) -> dict:
        artifact_sha = hashlib.sha256(content).hexdigest()
        return {
            "schema_version": "temms-hub-lite-bundle/v1",
            "hub_lite": {
                "devices": {},
                "packages": {
                    "pkg-1": {
                        "package_id": "pkg-1",
                        "name": "vision",
                        "version": "1",
                        "updated_at": updated_at,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                },
                "rollouts": {},
                "deployment_status": {},
            },
            "package_artifacts": {
                "pkg-1": {
                    "filename": "pkg-1.temms.tar.zst",
                    "sha256": artifact_sha,
                    "source_sha256": artifact_sha,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
            },
        }

    imported = store.import_bundle(bundle("2026-01-01T00:05:00Z", b"new-package"))
    assert imported["package_artifacts"] == 1
    package = store.get_package("pkg-1")
    package_path = package["path"]
    assert (temp_dir / "packages" / "pkg-1.temms.tar.zst").read_bytes() == b"new-package"

    stale = store.import_bundle(bundle("2026-01-01T00:00:00Z", b"old-package"))

    package = store.get_package("pkg-1")
    assert stale["package_artifacts"] == 0
    assert stale["package_artifacts_skipped"] == 1
    assert package["updated_at"] == "2026-01-01T00:05:00Z"
    assert package["path"] == package_path
    assert (temp_dir / "packages" / "pkg-1.temms.tar.zst").read_bytes() == b"new-package"


def test_airgap_import_hash_mismatch_preserves_existing_package_file(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    existing_content = b"known-good-package"
    existing_sha = hashlib.sha256(existing_content).hexdigest()
    good_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {
                "pkg-1": {
                    "package_id": "pkg-1",
                    "name": "vision",
                    "version": "1",
                    "updated_at": "2026-01-01T00:05:00Z",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            },
            "rollouts": {},
            "deployment_status": {},
        },
        "package_artifacts": {
            "pkg-1": {
                "filename": "pkg-1.temms.tar.zst",
                "sha256": existing_sha,
                "source_sha256": existing_sha,
                "content_base64": base64.b64encode(existing_content).decode("ascii"),
            }
        },
    }
    store.import_bundle(good_bundle)
    package_path = temp_dir / "packages" / "pkg-1.temms.tar.zst"

    bad_content = b"corrupted-transfer"
    expected_new_sha = hashlib.sha256(b"different-new-package").hexdigest()
    bad_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {
                "pkg-1": {
                    "package_id": "pkg-1",
                    "name": "vision",
                    "version": "2",
                    "updated_at": "2026-01-01T00:10:00Z",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            },
            "rollouts": {},
            "deployment_status": {},
        },
        "package_artifacts": {
            "pkg-1": {
                "filename": "pkg-1.temms.tar.zst",
                "sha256": expected_new_sha,
                "source_sha256": expected_new_sha,
                "content_base64": base64.b64encode(bad_content).decode("ascii"),
            }
        },
    }

    with pytest.raises(ValueError, match="Package artifact hash mismatch"):
        store.import_bundle(bad_bundle)

    package = store.get_package("pkg-1")
    assert package["version"] == "1"
    assert package["path"] == str(package_path)
    assert package_path.read_bytes() == existing_content


def _release_package(store: HubLiteStore, package_id: str) -> dict:
    store.promote_package(
        package_id,
        "validated",
        actor="operator:validator",
        reason="runtime validation passed",
    )
    store.promote_package(
        package_id,
        "approved",
        actor="operator:approver",
        reason="package approved for release",
    )
    return store.promote_package(
        package_id,
        "released",
        actor="operator:release",
        reason="package released for rollout",
    )


def _backdate_device_last_seen(
    store: HubLiteStore,
    device_id: str,
    last_seen_at: str,
) -> None:
    data = json.loads(store.path.read_text())
    data["devices"][device_id]["last_seen_at"] = last_seen_at
    store.path.write_text(json.dumps(data))


def _backdate_benchmark_created_at(
    store: HubLiteStore,
    benchmark_id: str,
    created_at: str,
) -> None:
    data = json.loads(store.path.read_text())
    data["benchmarks"][benchmark_id]["created_at"] = created_at
    store.path.write_text(json.dumps(data))


def _minimal_package(package_dir: Path, model_bytes: bytes) -> Path:
    """Create a small valid directory package for Hub Lite source-registration tests."""
    models_dir = package_dir / "models"
    models_dir.mkdir(parents=True)
    model_path = models_dir / "model.onnx"
    model_path.write_bytes(model_bytes)
    manifest = {
        "schema_version": "v1",
        "package_id": "pkg-source-default",
        "name": "source-default",
        "version": "1.0.0",
        "created_at": "2026-01-01T00:00:00Z",
        "models": [
            {
                "id": "model-source-default",
                "name": "source-default",
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
    (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return package_dir
