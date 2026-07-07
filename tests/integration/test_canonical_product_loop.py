"""
Canonical TEMMS product loop.

This test is intentionally close to the demo narrative: signed package import,
condition-driven model switching, low-power downgrade, load-failure fallback,
rollout approval, operator override, and post-run evidence export.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from temms.conditions.store import ConditionStore
from temms.core.cache import ModelCache
from temms.core.runtime_target_runner import validate_runtime_target_package
from temms.core.signing import sign_package
from temms.core.storage import ModelStorage
from temms.daemon.deployment_state import DeploymentStateStore
from temms.daemon.pending_ops import PendingOperationsStore
from temms.daemon.service import DaemonConfig
from temms.hub_lite import HubLiteStore
from temms.inference.runtime import InferenceRuntime
from temms.inference.server import create_app
from temms.policy.engine import PolicyEngine
from temms.slots.manager import SlotManager
from temms.telemetry import TelemetryBuffer

CANONICAL_ROLLOUT_PLAN_ID = "plan-canonical-demo"


def test_canonical_signed_adaptive_fallback_override_and_evidence_loop(temp_dir):
    """Prove the product loop that TEMMS should make easy to demo."""
    signing_key = "canonical-demo-secret"
    system = _build_system(temp_dir / "edge")
    package_dir = _write_canonical_package(temp_dir / "canonical-package")
    sign_package(package_dir, signing_key, signer="temms-canonical-demo")

    package = system["hub_lite"].upsert_package_from_source(
        package_dir,
        require_signature=True,
        signing_key=signing_key,
        device_profiles=["x86_64-cpu"],
        strict_metadata=True,
        actor="operator:demo",
    )
    assert package["package_id"] == "pkg-canonical-edge-demo"
    system["hub_lite"].enroll_device(
        "edge-demo",
        profile="x86_64-cpu",
        labels={"site": "canonical-demo"},
        inventory={
            "device_profile": "x86_64-cpu",
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                }
            },
        },
    )
    runtime_target = system["hub_lite"].get_runtime_target("temms-x86_64-cpu")
    assert runtime_target is not None
    validation_result = validate_runtime_target_package(
        runtime_target,
        package_dir,
        require_signature=True,
        strict_metadata=True,
        signing_key=signing_key,
        local=True,
    )
    assert validation_result.ok is True
    assert validation_result.dry_run is False
    validation = system["hub_lite"].record_runtime_validation(
        "temms-x86_64-cpu",
        validation_result.to_dict(),
        package_id="pkg-canonical-edge-demo",
        actor="operator:demo",
    )
    assert validation["package_id"] == "pkg-canonical-edge-demo"
    system["hub_lite"].promote_package(
        "pkg-canonical-edge-demo",
        "validated",
        actor="operator:validator",
        reason="runtime validation passed",
        evidence={"validation_id": validation["validation_id"]},
    )
    system["hub_lite"].promote_package(
        "pkg-canonical-edge-demo",
        "approved",
        actor="operator:approver",
        reason="package approved for mission release",
        evidence={"validation_id": validation["validation_id"]},
    )
    released_package = system["hub_lite"].promote_package(
        "pkg-canonical-edge-demo",
        "released",
        actor="operator:release",
        reason="package released for edge rollout",
        evidence={"validation_id": validation["validation_id"]},
    )
    assert released_package["promotion"]["state"] == "released"
    rollout_plan = system["hub_lite"].create_rollout_plan(
        plan_id=CANONICAL_ROLLOUT_PLAN_ID,
        package_id="pkg-canonical-edge-demo",
        device_ids=["edge-demo"],
        slot="vision",
        runtime_target_id="temms-x86_64-cpu",
        require_runtime_validation=True,
        require_approval=True,
        actor="operator:demo",
    )
    assert rollout_plan["state"] == "ready"
    advanced_plan = system["hub_lite"].advance_rollout_plan(
        CANONICAL_ROLLOUT_PLAN_ID,
        actor="operator:demo",
    )
    rollout_id = _assigned_rollout_id(advanced_plan)
    rollout = system["hub_lite"].get_rollout(rollout_id)
    assert rollout is not None
    assert rollout["runtime_validation"]["validation_id"] == validation["validation_id"]
    assert rollout["approval"]["state"] == "pending"
    assert rollout["rollout_plan_id"] == CANONICAL_ROLLOUT_PLAN_ID

    system["slot_manager"].create_slot(
        name="vision",
        description="Canonical edge vision slot",
        required=True,
        default_model="yolov8-daylight",
    )

    load_attempts: list[str] = []
    served_inferences: list[dict[str, object]] = []

    async def load_model(slot_name: str, model_id: str) -> bool:
        assert slot_name == "vision"
        load_attempts.append(model_id)
        if model_id == "model-yolov8-faulty-001":
            raise RuntimeError("simulated load failure for canonical demo")
        return True

    async def infer(
        slot_name: str,
        model_id: str,
        input_data: bytes,
        content_type: str,
    ) -> list[dict[str, object]]:
        assert slot_name == "vision"
        served = {
            "slot": slot_name,
            "model_id": model_id,
            "bytes": len(input_data),
            "content_type": content_type,
            "offline": system["daemon_config"].offline_mode,
        }
        served_inferences.append(served)
        return [
            {
                "label": "canonical-detection",
                "model_id": model_id,
                "offline": served["offline"],
            }
        ]

    system["inference_runtime"].load_model = AsyncMock(side_effect=load_model)
    system["inference_runtime"].infer = AsyncMock(side_effect=infer)

    app = create_app(
        slot_manager=system["slot_manager"],
        condition_store=system["condition_store"],
        policy_engine=system["policy_engine"],
        model_cache=system["model_cache"],
        model_storage=system["model_storage"],
        inference_runtime=system["inference_runtime"],
        pending_operations=system["pending_operations"],
        deployment_state=system["deployment_state"],
        daemon_config=system["daemon_config"],
        hub_lite=system["hub_lite"],
        telemetry=system["telemetry"],
    )
    client = TestClient(app)

    _set_conditions(
        client,
        {
            "environmental.atmospheric.visibility_m": 10000,
            "environmental.atmospheric.precipitation": "none",
            "environmental.celestial.ambient": "bright",
            "platform.power.battery_pct": 88,
            "platform.power.power_source": "battery",
            "simulation.force_model_load_failure": False,
        },
    )
    blocked_apply = client.post(
        f"/v1/hub/rollouts/{rollout_id}/apply",
        json={
            "require_signature": True,
            "signing_key": signing_key,
            "actor": "edge:edge-demo",
        },
    )
    assert blocked_apply.status_code == 409, blocked_apply.text
    assert "requires approval" in blocked_apply.json()["detail"]

    approval_response = client.post(
        f"/v1/hub/rollouts/{rollout_id}/approve",
        json={
            "actor": "operator:approver",
            "reason": "canonical mission policy approved",
        },
    )
    assert approval_response.status_code == 200, approval_response.text
    assert approval_response.json()["approval"]["approved"] is True
    assert approval_response.json()["approval"]["actor"] == "operator:approver"

    apply_response = client.post(
        f"/v1/hub/rollouts/{rollout_id}/apply",
        json={
            "require_signature": True,
            "signing_key": signing_key,
            "actor": "edge:edge-demo",
        },
    )
    assert apply_response.status_code == 200, apply_response.text
    assert apply_response.json()["status"] == "activated"

    daylight = system["model_cache"].find_model("yolov8-daylight")
    lowlight = system["model_cache"].find_model("yolov8-lowlight")
    tiny = system["model_cache"].find_model("mobilenet-tiny")
    faulty = system["model_cache"].find_model("yolov8-faulty")
    assert daylight is not None
    assert lowlight is not None
    assert tiny is not None
    assert faulty is not None
    assert apply_response.json()["model"] == daylight.id
    assert system["slot_manager"].get_slot("vision").active_model_id == daylight.id
    assert {model.name for model in system["model_cache"].list_models()} == {
        "yolov8-daylight",
        "yolov8-lowlight",
        "mobilenet-tiny",
        "yolov8-faulty",
    }
    offline = client.post("/v1/control/offline")
    assert offline.status_code == 200, offline.text
    assert offline.json()["offline_mode"] is True

    _set_conditions(
        client,
        {
            "environmental.atmospheric.visibility_m": 60,
            "environmental.atmospheric.precipitation": "fog",
        },
    )
    fog_decision = _evaluate(client)
    assert fog_decision["status"] == "activated"
    assert fog_decision["activated_model"] == lowlight.id
    assert system["slot_manager"].get_slot("vision").active_model_id == lowlight.id

    offline_infer = client.post(
        "/v1/slots/vision/infer",
        files={"file": ("frame.jpg", b"canonical-frame", "image/jpeg")},
    )
    assert offline_infer.status_code == 200, offline_infer.text
    assert offline_infer.json()["model"] == lowlight.name
    assert offline_infer.json()["predictions"][0]["offline"] is True
    assert served_inferences[-1]["model_id"] == lowlight.id
    assert served_inferences[-1]["content_type"] == "image/jpeg"

    _set_conditions(
        client,
        {
            "platform.power.battery_pct": 15,
            "platform.power.power_source": "battery",
        },
    )
    battery_decision = _evaluate(client)
    assert battery_decision["status"] == "activated"
    assert battery_decision["activated_model"] == tiny.id
    assert system["slot_manager"].get_slot("vision").active_model_id == tiny.id

    _set_conditions(client, {"simulation.force_model_load_failure": True})
    fallback_decision = _evaluate(client)
    assert fallback_decision["status"] == "fallback_activated"
    assert fallback_decision["selected_model"] == faulty.id
    assert fallback_decision["activated_model"] == lowlight.id
    assert faulty.id in load_attempts
    assert fallback_decision["fallback_failures"][0].startswith(f"{faulty.id}:")
    assert system["slot_manager"].get_slot("vision").active_model_id == lowlight.id

    rollback = client.post("/v1/control/slots/vision/rollback")
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["model"] == tiny.id
    assert system["slot_manager"].get_slot("vision").active_model_id == tiny.id

    override = client.post(
        "/v1/control/slots/vision/model",
        json={
            "model": "yolov8-daylight",
            "reason": "operator override after route clears",
        },
    )
    assert override.status_code == 200, override.text
    assert override.json()["model"] == daylight.id
    assert system["slot_manager"].get_slot("vision").active_model_id == daylight.id

    override_check = _evaluate(client)
    assert override_check["status"] == "override_active"
    assert override_check["activated_model"] == daylight.id

    evidence = client.get("/v1/evidence?limit=100")
    assert evidence.status_code == 200
    bundle = evidence.json()

    assert bundle["schema_version"] == "temms-evidence-bundle/v1"
    assert bundle["integrity"]["payload_sha256"]
    assert bundle["runtime"]["offline_mode"] is True
    assert bundle["deployment_state"]["state"] == "OFFLINE"
    pending_operations = bundle["runtime"]["pending_operations"]
    assert any(operation["operation"] == "update_conditions" for operation in pending_operations)
    assert any(operation["operation"] == "override_model" for operation in pending_operations)
    assert {package["id"] for package in bundle["packages"]} == {"pkg-canonical-edge-demo"}
    assert any(
        event["package_id"] == "pkg-canonical-edge-demo" and event["signature_verified"] is True
        for event in bundle["package_imports"]
    )
    assert any(
        event["event_type"] == "inference.served"
        and event["payload"]["model_id"] == lowlight.id
        and event["payload"]["content_type"] == "image/jpeg"
        for event in bundle["telemetry"]["events"]
    )
    runtime_validation = next(
        (
            record
            for record in bundle["runtime_validations"]
            if record["package_id"] == "pkg-canonical-edge-demo"
            and record["result"]["ok"] is True
            and record["result"]["dry_run"] is False
        ),
        None,
    )
    assert runtime_validation is not None
    validation_result = runtime_validation["result"]
    assert "temms package validate" in validation_result["command_text"]
    assert "--check-runtime" in validation_result["command"]
    assert "temms-local-runtime-validation/v1" in validation_result["stdout"]
    assert {
        "assigned",
        "approved",
        "downloading",
        "imported",
        "activated",
        "rolled_back",
    } <= {event["state"] for event in bundle["rollout_events"]}
    assert (
        bundle["hub_lite"]["packages"]["pkg-canonical-edge-demo"]["promotion"]["state"]
        == "released"
    )
    assert "released" in {event["state"] for event in bundle["package_promotions"]}
    assert any(
        event["plan_id"] == CANONICAL_ROLLOUT_PLAN_ID
        and event["state"] in {"advanced", "completed"}
        and rollout_id in event["rollout_ids"]
        for event in bundle["rollout_plans"]
    )
    assert bundle["hub_lite"]["rollouts"][rollout_id]["state"] == "rolled_back"
    assert bundle["hub_lite"]["rollouts"][rollout_id]["approval"]["approved"] is True
    assert bundle["hub_lite"]["rollouts"][rollout_id]["approval"]["actor"] == "operator:approver"

    decisions = bundle["decisions"]
    assert _has_decision(decisions, "rollout", daylight.id)
    assert _has_decision(decisions, "policy", lowlight.id)
    assert _has_decision(decisions, "policy", tiny.id)
    fallback_record = _matching_decision(decisions, "fallback", lowlight.id)
    assert fallback_record is not None
    assert fallback_record["audit_metadata"]["package_id"] == "pkg-canonical-edge-demo"
    assert fallback_record["audit_metadata"]["fallback"]["selected_model"] == faulty.id
    assert _has_decision(decisions, "rollback", tiny.id)
    assert _has_decision(decisions, "operator", daylight.id)
    assert {
        "decision",
        "telemetry",
        "package_import",
        "package_promotion",
        "runtime_validation",
        "rollout",
        "rollout_plan",
    } <= {entry["kind"] for entry in bundle["timeline"]}

    summary_response = client.get("/v1/evidence?limit=100&summary=true&summary_limit=10")
    assert summary_response.status_code == 200
    summary = summary_response.json()
    assert summary["schema_version"] == "temms-evidence-summary/v1"
    for headline_part in [
        "rollout applied",
        "policy-adaptive switching",
        "fallback recovery",
        "approval gate",
        "rollback",
        "operator override",
        "offline operation",
    ]:
        assert headline_part in summary["headline"]
    assert summary["runtime"]["offline_mode"] is True
    assert summary["runtime"]["deployment_state"]["state"] == "OFFLINE"
    assert summary["runtime"]["pending_operation_types"] == [
        "override_model",
        "update_conditions",
    ]
    assert summary["trust"]["signed_package_imports"] == 1
    assert summary["trust"]["runtime_validations_passed_non_dry_run"] == 1
    assert summary["trust"]["local_runtime_validations"] == 1
    assert summary["trust"]["released_packages"] == 1
    assert summary["package_promotions"][0]["state"] == "released"
    assert summary["counts"]["rollout_plans"] >= 1
    assert summary["rollout_plans"][0]["plan_id"] == CANONICAL_ROLLOUT_PLAN_ID
    assert summary["approvals"][0]["rollout_id"] == rollout_id
    assert summary["approvals"][0]["actor"] == "operator:approver"
    assert summary["fallbacks"][0]["failed_model"] == faulty.id
    assert summary["fallbacks"][0]["activated_model"] == lowlight.id
    assert summary["operator_overrides"][0]["to_model"] == daylight.id

    replay_response = client.get("/v1/evidence?limit=100&replay=true&replay_limit=20")
    assert replay_response.status_code == 200
    replay = replay_response.json()
    phases = {phase["phase"]: phase for phase in replay["phases"]}
    assert replay["schema_version"] == "temms-mission-replay/v1"
    for phase_name in [
        "signed_package",
        "runtime_validation",
        "package_release",
        "rollout_coordination",
        "policy_approval",
        "edge_rollout",
        "policy_decision",
        "fallback_rollback",
        "operator_override",
        "offline_operation",
        "evidence_export",
    ]:
        assert phases[phase_name]["status"] == "complete"
    assert replay["incidents"]["fallbacks"][0]["failed_model"] == faulty.id
    assert replay["incidents"]["operator_overrides"][0]["to_model"] == daylight.id
    assert replay["events"][0]["sequence"] == 1

    ingested = client.post(
        "/v1/hub/evidence/ingest",
        json={
            "bundle": bundle,
            "device_id": "edge-demo",
            "actor": "operator:auditor",
        },
    )
    assert ingested.status_code == 200, ingested.text
    ingested_record = ingested.json()["evidence"]
    assert ingested_record["schema_version"] == "temms-ingested-evidence/v1"
    assert ingested_record["device_id"] == "edge-demo"
    assert ingested_record["integrity"]["payload_sha256"] == bundle["integrity"]["payload_sha256"]

    listed_evidence = client.get("/v1/hub/evidence")
    assert listed_evidence.status_code == 200
    assert listed_evidence.json()["count"] == 1
    assert listed_evidence.json()["evidence_bundles"][0]["evidence_id"] == (
        ingested_record["evidence_id"]
    )

    hub_summary_response = client.post(
        "/v1/hub/evidence/export",
        json={
            "decision_limit": 100,
            "summary": True,
            "summary_limit": 10,
        },
    )
    assert hub_summary_response.status_code == 200
    hub_summary = hub_summary_response.json()
    assert hub_summary["schema_version"] == "temms-evidence-summary/v1"
    assert hub_summary["counts"]["ingested_evidence_bundles"] == 1
    assert hub_summary["ingested_evidence"][0]["evidence_id"] == (ingested_record["evidence_id"])

    hub_replay_response = client.post(
        "/v1/hub/evidence/export",
        json={
            "decision_limit": 100,
            "replay": True,
            "replay_limit": 20,
        },
    )
    assert hub_replay_response.status_code == 200
    hub_replay = hub_replay_response.json()
    hub_phases = {phase["phase"]: phase for phase in hub_replay["phases"]}
    assert hub_replay["schema_version"] == "temms-mission-replay/v1"
    assert hub_replay["outcome"]["counts"]["ingested_evidence_bundles"] == 1
    assert hub_phases["evidence_aggregation"]["status"] == "complete"


def _assigned_rollout_id(plan: dict) -> str:
    for target in plan.get("targets", []):
        rollout_id = target.get("rollout_id")
        if rollout_id:
            return str(rollout_id)
    raise AssertionError("rollout plan did not assign any target")


def _build_system(root: Path) -> dict:
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
    deployment_state = DeploymentStateStore(root / "deployment_state.json")
    pending_operations = PendingOperationsStore(root / "pending_operations.json")
    daemon_config = DaemonConfig(
        db_path=db_path,
        model_dir=model_dir,
        policy_dir=policy_dir,
        hub_state_path=root / "hub_lite.json",
        rollout_require_signature=True,
        rollout_signing_key="canonical-demo-secret",
    )
    telemetry = TelemetryBuffer(root / "telemetry.jsonl")
    return {
        "root": root,
        "model_cache": model_cache,
        "model_storage": model_storage,
        "slot_manager": slot_manager,
        "condition_store": condition_store,
        "policy_engine": policy_engine,
        "inference_runtime": inference_runtime,
        "hub_lite": hub_lite,
        "deployment_state": deployment_state,
        "pending_operations": pending_operations,
        "daemon_config": daemon_config,
        "telemetry": telemetry,
        "policy_dir": policy_dir,
    }


def _write_canonical_package(package_dir: Path) -> Path:
    models_dir = package_dir / "models"
    policies_dir = package_dir / "policies"
    models_dir.mkdir(parents=True)
    policies_dir.mkdir(parents=True)

    models = [
        _write_model(models_dir, "model-yolov8-daylight-001", "yolov8-daylight"),
        _write_model(models_dir, "model-yolov8-lowlight-001", "yolov8-lowlight"),
        _write_model(models_dir, "model-mobilenet-tiny-001", "mobilenet-tiny"),
        _write_model(models_dir, "model-yolov8-faulty-001", "yolov8-faulty"),
    ]

    (policies_dir / "canonical-adaptive.yaml").write_text(
        """
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: canonical-edge-demo
  description: Canonical fog, low-battery, and fallback demo policy
spec:
  slot: vision
  default_model: yolov8-daylight
  rules:
    - name: simulated-load-failure
      priority: 200
      conditions:
        all:
          - metric: simulation.force_model_load_failure
            operator: eq
            value: true
      action:
        switch_to: yolov8-faulty
    - name: low-battery
      priority: 120
      conditions:
        all:
          - metric: platform.power.battery_pct
            operator: lte
            value: 20
          - metric: platform.power.power_source
            operator: eq
            value: battery
      action:
        switch_to: mobilenet-tiny
    - name: fog-conditions
      priority: 80
      conditions:
        any:
          - metric: environmental.atmospheric.visibility_m
            operator: lte
            value: 100
          - metric: environmental.atmospheric.precipitation
            operator: in
            value: [fog, mist]
      action:
        switch_to: yolov8-lowlight
  allow_operator_override: true
  fallback_chain:
    - yolov8-lowlight
    - mobilenet-tiny
    - yolov8-daylight
""".lstrip(),
        encoding="utf-8",
    )

    manifest = {
        "schema_version": "v1",
        "package_id": "pkg-canonical-edge-demo",
        "name": "canonical-edge-demo",
        "version": "1.0.0",
        "description": "Canonical TEMMS adaptive edge inference demo package",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "created_by": "temms-canonical-test",
        "models": models,
        "policies": [
            {
                "name": "canonical-adaptive",
                "filename": "canonical-adaptive.yaml",
                "slot": "vision",
            }
        ],
        "source_registry": "mlflow://canonical-demo",
        "mlflow_run_id": "run-canonical-demo",
        "compatibility": {
            "device_profiles": ["x86_64-cpu"],
        },
        "provenance": {
            "source": "canonical-product-loop-test",
            "run_id": "run-canonical-demo",
        },
    }
    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return package_dir


def _write_model(models_dir: Path, model_id: str, model_name: str) -> dict:
    filename = f"{model_name}.onnx"
    content = f"canonical demo model bytes for {model_id}\n".encode()
    path = models_dir / filename
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    return {
        "id": model_id,
        "name": model_name,
        "version": "1.0.0",
        "format": "onnx",
        "filename": filename,
        "sha256": digest,
        "size_bytes": len(content),
        "metadata": {
            "input_shape": [1, 3, 224, 224],
            "description": f"{model_name} canonical demo artifact",
        },
        "input_schema": {
            "shape": [1, 3, 224, 224],
            "dtype": "float32",
        },
        "output_schema": {
            "shape": [1, 1000],
            "dtype": "float32",
        },
        "runtime_constraints": {
            "device_profiles": ["x86_64-cpu"],
            "runtimes": ["onnxruntime"],
            "providers": ["CPUExecutionProvider"],
        },
        "benchmark": {
            "latency_ms_p95": 12.0 if "tiny" not in model_name else 5.0,
            "source": "canonical-product-loop-test",
        },
        "provenance": {
            "source": "canonical-product-loop-test",
            "run_id": "run-canonical-demo",
            "artifact_sha256": digest,
        },
    }


def _set_conditions(client: TestClient, conditions: dict) -> None:
    response = client.post("/v1/control/conditions", json={"conditions": conditions})
    assert response.status_code == 200, response.text


def _evaluate(client: TestClient) -> dict:
    response = client.post("/v1/control/slots/vision/evaluate", json={"apply": True})
    assert response.status_code == 200, response.text
    return response.json()


def _has_decision(decisions: list[dict], trigger_type: str, to_model: str) -> bool:
    return _matching_decision(decisions, trigger_type, to_model) is not None


def _matching_decision(
    decisions: list[dict],
    trigger_type: str,
    to_model: str,
) -> dict | None:
    for decision in decisions:
        if decision.get("trigger_type") == trigger_type and decision.get("to_model") == to_model:
            return decision
    return None
