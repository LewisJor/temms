#!/usr/bin/env python3
"""
Run the canonical TEMMS product demo without Docker or an external daemon.

The demo uses TEMMS' real package import, policy engine, API routes, slot state,
fallback, rollback, telemetry, and evidence export. Model loading is simulated
so the control loop is fast and portable on a fresh developer machine.
"""

import argparse
import hashlib
import json
import logging
import shlex
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

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

SIGNING_KEY = "canonical-demo-secret"
CANONICAL_ROLLOUT_PLAN_ID = "plan-canonical-demo"
DEMO_DEVICE_ID = "edge-demo"
DEMO_DEVICE_PROFILE = "x86_64-cpu"
DEMO_DAEMON_HOST = "127.0.0.1"
DEMO_DAEMON_PORT = 18080
DEMO_EDGE_HEARTBEAT_INTERVAL_S = 10


class DemoError(RuntimeError):
    """Raised when the demo loop does not reach the expected state."""


def print_header(text: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {text}")
    print(f"{'=' * 72}")


def print_step(step: int, total: int, text: str) -> None:
    print(f"\n[{step}/{total}] {text}")


def run_demo(
    *,
    evidence_output: Path,
    work_dir: Path | None = None,
    keep_work_dir: bool = False,
    json_summary: bool = False,
) -> dict[str, Any]:
    """Run the canonical product loop and return a compact summary."""
    run_root, temp_created = _resolve_run_root(work_dir)
    evidence_output = evidence_output.resolve()

    try:
        summary = _run_demo_in_workspace(run_root, evidence_output, json_summary=json_summary)
        daemon_config = _write_demo_daemon_config(run_root)
        summary["work_dir"] = str(run_root)
        summary["evidence_output"] = str(evidence_output)
        summary["daemon_config"] = str(daemon_config)
        summary["hub_url"] = f"http://{DEMO_DAEMON_HOST}:{DEMO_DAEMON_PORT}"
        summary["daemon_edge_identity"] = {
            "device_id": DEMO_DEVICE_ID,
            "device_profile": DEMO_DEVICE_PROFILE,
            "heartbeat_interval_s": DEMO_EDGE_HEARTBEAT_INTERVAL_S,
        }
        summary["daemon_start_command"] = _demo_daemon_start_command(daemon_config)
        if not json_summary:
            print(f"  Local Hub config: {daemon_config}")
            print(f"  Local Hub URL: {summary['hub_url']}/ui/hub")
            print(f"  Start command: {summary['daemon_start_command']}")
        return summary
    finally:
        if temp_created and not keep_work_dir:
            shutil.rmtree(run_root, ignore_errors=True)


def _write_demo_daemon_config(run_root: Path) -> Path:
    edge_root = run_root / "edge"
    config_path = run_root / "temms-demo.yaml"
    config_path.write_text(
        f"""database:
  path: {_yaml_string(edge_root / "temms.db")}
inference:
  grpc_port: 50051
  host: 0.0.0.0
  http_port: {DEMO_DAEMON_PORT}
  max_batch_size: 1
  timeout_ms: 5000
policy:
  enable_auto_switching: true
  evaluation_interval_s: 5
  policy_dir: {_yaml_string(edge_root / "policies")}
storage:
  cache_dir: {_yaml_string(edge_root / "cache")}
  model_dir: {_yaml_string(edge_root / "models")}
sync:
  cloud_endpoint: null
  enable_cloud_sync: false
  sync_interval_s: 300
""",
        encoding="utf-8",
    )
    return config_path


def _demo_daemon_start_command(config_path: Path) -> str:
    return shlex.join(
        [
            "env",
            f"TEMMS_PACKAGE_SIGNING_KEY={SIGNING_KEY}",
            f"TEMMS_DEVICE_ID={DEMO_DEVICE_ID}",
            f"TEMMS_DEVICE_PROFILE={DEMO_DEVICE_PROFILE}",
            f"TEMMS_EDGE_HEARTBEAT_INTERVAL_S={DEMO_EDGE_HEARTBEAT_INTERVAL_S}",
            "uv",
            "run",
            "temms",
            "daemon",
            "start",
            "--foreground",
            "--host",
            DEMO_DAEMON_HOST,
            "--port",
            str(DEMO_DAEMON_PORT),
            "--config",
            str(config_path),
        ]
    )


def _yaml_string(value: Path | str) -> str:
    return json.dumps(str(value))


def _run_demo_in_workspace(
    run_root: Path,
    evidence_output: Path,
    *,
    json_summary: bool,
) -> dict[str, Any]:
    total_steps = 9
    load_attempts: list[str] = []
    served_inferences: list[dict[str, Any]] = []

    if not json_summary:
        print_header("TEMMS Canonical Product Demo")
        print("  Controlled deployment and adaptive runtime decisioning for edge inference")
        print(f"  Workspace: {run_root}")

    _emit_step(json_summary, 1, total_steps, "Build and sign a model package")
    system = _build_system(run_root / "edge")
    package_dir = _write_canonical_package(run_root / "canonical-package")
    sign_package(package_dir, SIGNING_KEY, signer="temms-canonical-demo")
    if not json_summary:
        print(f"  Package: {package_dir}")
        print("  Signature: signature.json written by temms-canonical-demo")

    _emit_step(json_summary, 2, total_steps, "Catalog package and record runtime validation")
    package = system["hub_lite"].upsert_package_from_source(
        package_dir,
        require_signature=True,
        signing_key=SIGNING_KEY,
        device_profiles=["x86_64-cpu"],
        strict_metadata=True,
        actor="operator:demo",
    )
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
    _expect(runtime_target is not None, "runtime target not found")
    validation_result = validate_runtime_target_package(
        runtime_target,
        package_dir,
        require_signature=True,
        strict_metadata=True,
        signing_key=SIGNING_KEY,
        local=True,
    )
    _expect(validation_result.ok and not validation_result.dry_run, validation_result.stderr)
    validation = system["hub_lite"].record_runtime_validation(
        "temms-x86_64-cpu",
        validation_result.to_dict(),
        package_id="pkg-canonical-edge-demo",
        actor="operator:demo",
    )
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
    if not json_summary:
        print(f"  Catalog package: {package['package_id']}")
        print(f"  Runtime validation: {validation['validation_id']}")
        print(f"  Package promotion: {released_package['promotion']['state']}")

    _emit_step(json_summary, 3, total_steps, "Plan rollout, approve policy, and apply it")
    rollout_plan = system["hub_lite"].create_rollout_plan(
        plan_id=CANONICAL_ROLLOUT_PLAN_ID,
        package_id="pkg-canonical-edge-demo",
        model_id="model-yolov8-daylight-001",
        device_ids=["edge-demo"],
        slot="vision",
        runtime_target_id="temms-x86_64-cpu",
        require_runtime_validation=True,
        require_approval=True,
        actor="operator:demo",
    )
    _expect(rollout_plan["state"] == "ready", "rollout plan was not ready")
    advanced_plan = system["hub_lite"].advance_rollout_plan(
        CANONICAL_ROLLOUT_PLAN_ID,
        actor="operator:demo",
    )
    rollout_id = _assigned_rollout_id(advanced_plan)
    rollout = system["hub_lite"].get_rollout(rollout_id)
    _expect(rollout is not None, "rollout plan did not assign a rollout")
    system["slot_manager"].create_slot(
        name="vision",
        description="Canonical edge vision slot",
        required=True,
        default_model="yolov8-daylight",
    )

    async def load_model(slot_name: str, model_id: str) -> bool:
        _expect(slot_name == "vision", f"unexpected slot load: {slot_name}")
        load_attempts.append(model_id)
        if model_id == "model-yolov8-faulty-001":
            raise RuntimeError("simulated load failure for canonical demo")
        return True

    async def infer(
        slot_name: str,
        model_id: str,
        input_data: bytes,
        content_type: str,
    ) -> list[dict[str, Any]]:
        _expect(slot_name == "vision", f"unexpected slot inference: {slot_name}")
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

    system["inference_runtime"].load_model = load_model
    system["inference_runtime"].infer = infer
    client = TestClient(
        create_app(
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
    )
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
            "signing_key": SIGNING_KEY,
            "actor": "edge:edge-demo",
        },
    )
    _expect(blocked_apply.status_code == 409, blocked_apply.text)
    approval_response = client.post(
        f"/v1/hub/rollouts/{rollout_id}/approve",
        json={
            "actor": "operator:approver",
            "reason": "canonical mission policy approved",
        },
    )
    _expect(approval_response.status_code == 200, approval_response.text)
    _expect(
        approval_response.json()["approval"]["approved"] is True,
        "rollout approval did not record approved state",
    )
    apply_response = client.post(
        f"/v1/hub/rollouts/{rollout_id}/apply",
        json={
            "require_signature": True,
            "signing_key": SIGNING_KEY,
            "actor": "edge:edge-demo",
        },
    )
    _expect(apply_response.status_code == 200, apply_response.text)
    _expect(apply_response.json()["status"] == "activated", apply_response.text)
    models = {model.name: model for model in system["model_cache"].list_models()}
    _expect(
        {"yolov8-daylight", "yolov8-lowlight", "mobilenet-tiny", "yolov8-faulty"} <= set(models),
        "rollout import is missing expected models",
    )
    daylight = models["yolov8-daylight"]
    lowlight = models["yolov8-lowlight"]
    tiny = models["mobilenet-tiny"]
    faulty = models["yolov8-faulty"]
    _expect(apply_response.json()["model"] == daylight.id, "rollout did not activate daylight")
    _expect(_active_model(system) == daylight.id, "daylight model was not active after rollout")
    _expect(rollout["rollout_id"] == rollout_id, "rollout assignment failed")
    _expect(
        rollout["rollout_plan_id"] == CANONICAL_ROLLOUT_PLAN_ID,
        "rollout did not record rollout plan",
    )
    if not json_summary:
        print(f"  Rollout plan: {CANONICAL_ROLLOUT_PLAN_ID}")
        print(f"  Rollout: {rollout_id}")
        print("  Approval: operator:approver")
        print(f"  Active model: {daylight.name}")
    offline = client.post("/v1/control/offline")
    _expect(offline.status_code == 200, offline.text)
    _expect(offline.json()["offline_mode"] is True, "edge did not enter offline mode")
    if not json_summary:
        print("  Edge mode: offline")

    _emit_step(json_summary, 4, total_steps, "Simulate fog and switch to low-light model")
    _set_conditions(
        client,
        {
            "environmental.atmospheric.visibility_m": 60,
            "environmental.atmospheric.precipitation": "fog",
        },
    )
    fog_decision = _evaluate(client)
    _expect(fog_decision["status"] == "activated", f"fog decision failed: {fog_decision}")
    _expect(fog_decision["activated_model"] == lowlight.id, "fog did not activate low-light")
    offline_infer = client.post(
        "/v1/slots/vision/infer",
        files={"file": ("frame.jpg", b"canonical-frame", "image/jpeg")},
    )
    _expect(offline_infer.status_code == 200, offline_infer.text)
    _expect(offline_infer.json()["model"] == lowlight.name, "offline inference used wrong model")
    _expect(
        offline_infer.json()["predictions"][0]["offline"] is True,
        "offline inference was not served while disconnected",
    )
    _expect(
        served_inferences[-1]["model_id"] == lowlight.id,
        "offline inference did not use the active low-light model",
    )
    if not json_summary:
        print(f"  Policy decision: fog -> {lowlight.name}")
        print(f"  Offline inference: served by {lowlight.name}")

    _emit_step(json_summary, 5, total_steps, "Simulate low battery and downgrade to small model")
    _set_conditions(
        client,
        {
            "platform.power.battery_pct": 15,
            "platform.power.power_source": "battery",
        },
    )
    battery_decision = _evaluate(client)
    _expect(
        battery_decision["status"] == "activated",
        f"battery decision failed: {battery_decision}",
    )
    _expect(battery_decision["activated_model"] == tiny.id, "battery did not activate tiny model")
    if not json_summary:
        print(f"  Policy decision: low battery -> {tiny.name}")

    _emit_step(json_summary, 6, total_steps, "Trigger load failure and execute fallback")
    _set_conditions(client, {"simulation.force_model_load_failure": True})
    controller_logger = logging.getLogger("temms.controller")
    previous_disabled = controller_logger.disabled
    controller_logger.disabled = True
    try:
        fallback_decision = _evaluate(client)
    finally:
        controller_logger.disabled = previous_disabled
    _expect(
        fallback_decision["status"] == "fallback_activated",
        f"fallback decision failed: {fallback_decision}",
    )
    _expect(fallback_decision["selected_model"] == faulty.id, "faulty model was not selected")
    _expect(fallback_decision["activated_model"] == lowlight.id, "fallback did not recover")
    _expect(faulty.id in load_attempts, "faulty model load was not attempted")
    if not json_summary:
        print(f"  Failed model: {faulty.name}")
        print(f"  Fallback model: {lowlight.name}")

    _emit_step(json_summary, 7, total_steps, "Rollback, then apply operator override")
    rollback = client.post("/v1/control/slots/vision/rollback")
    _expect(rollback.status_code == 200, rollback.text)
    _expect(rollback.json()["model"] == tiny.id, "rollback did not restore previous model")
    override = client.post(
        "/v1/control/slots/vision/model",
        json={
            "model": "yolov8-daylight",
            "reason": "operator override after route clears",
        },
    )
    _expect(override.status_code == 200, override.text)
    _expect(override.json()["model"] == daylight.id, "operator override failed")
    override_check = _evaluate(client)
    _expect(override_check["status"] == "override_active", "operator override did not hold")
    if not json_summary:
        print(f"  Rollback restored: {tiny.name}")
        print(f"  Operator override: {daylight.name}")

    _emit_step(json_summary, 8, total_steps, "Export edge evidence bundle")
    evidence = client.get("/v1/evidence?limit=100")
    _expect(evidence.status_code == 200, evidence.text)
    bundle = evidence.json()
    _validate_evidence(
        bundle,
        lowlight.id,
        tiny.id,
        faulty.id,
        daylight.id,
        rollout_id=rollout_id,
        rollout_plan_id=CANONICAL_ROLLOUT_PLAN_ID,
    )
    replay_response = client.get("/v1/evidence?limit=100&replay=true&replay_limit=20")
    _expect(replay_response.status_code == 200, replay_response.text)
    replay = replay_response.json()
    _validate_replay(replay)
    evidence_output.parent.mkdir(parents=True, exist_ok=True)
    evidence_output.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
    if not json_summary:
        print(f"  Evidence: {evidence_output}")
        print(f"  Integrity: {bundle['integrity']['payload_sha256']}")
        print(f"  Replay: {replay['schema_version']}")

    _emit_step(json_summary, 9, total_steps, "Aggregate edge evidence in Hub")
    ingest_response = client.post(
        "/v1/hub/evidence/ingest",
        json={
            "bundle": bundle,
            "device_id": "edge-demo",
            "actor": "operator:auditor",
        },
    )
    _expect(ingest_response.status_code == 200, ingest_response.text)
    ingested_record = ingest_response.json()["evidence"]
    listed_response = client.get("/v1/hub/evidence")
    _expect(listed_response.status_code == 200, listed_response.text)
    listed_evidence = listed_response.json()
    hub_summary_response = client.post(
        "/v1/hub/evidence/export",
        json={
            "decision_limit": 100,
            "summary": True,
            "summary_limit": 10,
        },
    )
    _expect(hub_summary_response.status_code == 200, hub_summary_response.text)
    hub_summary = hub_summary_response.json()
    hub_replay_response = client.post(
        "/v1/hub/evidence/export",
        json={
            "decision_limit": 100,
            "replay": True,
            "replay_limit": 20,
        },
    )
    _expect(hub_replay_response.status_code == 200, hub_replay_response.text)
    hub_replay = hub_replay_response.json()
    _validate_hub_aggregation(
        ingested_record,
        listed_evidence,
        hub_summary,
        hub_replay,
        bundle,
    )
    if not json_summary:
        print(f"  Hub evidence: {ingested_record['evidence_id']}")
        print("  Hub replay phase: evidence_aggregation complete")

    decisions = bundle["decisions"]
    summary = {
        "ok": True,
        "active_model": _active_model(system),
        "package_id": package["package_id"],
        "runtime_validation_id": validation["validation_id"],
        "rollout_plan_id": CANONICAL_ROLLOUT_PLAN_ID,
        "rollout_id": rollout_id,
        "approval_actor": "operator:approver",
        "package_promotion": released_package["promotion"]["state"],
        "decisions": [
            {
                "trigger_type": decision.get("trigger_type"),
                "to_model": decision.get("to_model"),
                "trigger_detail": decision.get("trigger_detail"),
            }
            for decision in reversed(decisions)
        ],
        "evidence_sha256": bundle["integrity"]["payload_sha256"],
        "replay_schema": replay["schema_version"],
        "replay_completed_phases": replay["outcome"]["completed_phases"],
        "hub_evidence_id": ingested_record["evidence_id"],
        "hub_evidence_count": listed_evidence["count"],
        "hub_replay_completed_phases": hub_replay["outcome"]["completed_phases"],
        "load_attempts": load_attempts,
        "offline_inferences": served_inferences,
    }

    if not json_summary:
        print_header("Demo Complete")
        print("  TEMMS controlled what ran, adapted to local conditions, recovered from")
        print("  failure, honored an operator override, and exported evidence.")

    return summary


def _resolve_run_root(work_dir: Path | None) -> tuple[Path, bool]:
    if work_dir is None:
        return Path(tempfile.mkdtemp(prefix="temms-canonical-demo-")), True

    root = work_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.utcnow().strftime("run-%Y%m%d%H%M%S%f")
    run_root = root / run_id
    run_root.mkdir()
    return run_root, False


def _emit_step(json_summary: bool, step: int, total: int, text: str) -> None:
    if not json_summary:
        print_step(step, total, text)


def _build_system(root: Path) -> dict[str, Any]:
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
        rollout_signing_key=SIGNING_KEY,
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
        "created_by": "temms-canonical-demo",
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
            "source": "canonical-product-demo",
            "run_id": "run-canonical-demo",
        },
    }
    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return package_dir


def _write_model(models_dir: Path, model_id: str, model_name: str) -> dict[str, Any]:
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
            "source": "canonical-product-demo",
        },
        "provenance": {
            "source": "canonical-product-demo",
            "run_id": "run-canonical-demo",
            "artifact_sha256": digest,
        },
    }


def _set_conditions(client: TestClient, conditions: dict[str, Any]) -> None:
    response = client.post("/v1/control/conditions", json={"conditions": conditions})
    _expect(response.status_code == 200, response.text)


def _evaluate(client: TestClient) -> dict[str, Any]:
    response = client.post("/v1/control/slots/vision/evaluate", json={"apply": True})
    _expect(response.status_code == 200, response.text)
    return response.json()


def _active_model(system: dict[str, Any]) -> str | None:
    slot = system["slot_manager"].get_slot("vision")
    return slot.active_model_id if slot is not None else None


def _assigned_rollout_id(plan: dict[str, Any]) -> str:
    for target in plan.get("targets", []):
        rollout_id = target.get("rollout_id")
        if rollout_id:
            return str(rollout_id)
    raise DemoError("rollout plan did not assign any target")


def _validate_evidence(
    bundle: dict[str, Any],
    lowlight_id: str,
    tiny_id: str,
    faulty_id: str,
    daylight_id: str,
    *,
    rollout_id: str,
    rollout_plan_id: str,
) -> None:
    _expect(bundle["schema_version"] == "temms-evidence-bundle/v1", "bad evidence schema")
    _expect(bool(bundle["integrity"]["payload_sha256"]), "missing evidence integrity hash")
    _expect(bundle["runtime"]["offline_mode"] is True, "evidence does not show offline mode")
    _expect(
        bundle["deployment_state"]["state"] == "OFFLINE",
        "deployment state does not show offline mode",
    )
    pending_operations = bundle["runtime"]["pending_operations"]
    _expect(
        any(operation["operation"] == "update_conditions" for operation in pending_operations),
        "offline condition updates were not buffered",
    )
    _expect(
        any(operation["operation"] == "override_model" for operation in pending_operations),
        "offline operator override was not buffered",
    )
    _expect(
        any(
            event["package_id"] == "pkg-canonical-edge-demo" and event["signature_verified"] is True
            for event in bundle["package_imports"]
        ),
        "evidence does not prove signed package import",
    )
    _expect(
        any(
            event.get("event_type") == "inference.served"
            and event.get("payload", {}).get("model_id") == lowlight_id
            and event.get("payload", {}).get("content_type") == "image/jpeg"
            for event in bundle["telemetry"]["events"]
        ),
        "evidence does not prove offline inference serving",
    )
    runtime_validation = next(
        (
            record
            for record in bundle["runtime_validations"]
            if record["package_id"] == "pkg-canonical-edge-demo"
            and record["result"].get("ok") is True
            and record["result"].get("dry_run") is False
        ),
        None,
    )
    _expect(runtime_validation is not None, "evidence does not prove runtime validation")
    validation_result = runtime_validation["result"]
    _expect(
        "temms package validate" in validation_result.get("command_text", "")
        and "--check-runtime" in validation_result.get("command", []),
        "runtime validation evidence did not run package validation",
    )
    _expect(
        "temms-local-runtime-validation/v1" in validation_result.get("stdout", ""),
        "runtime validation evidence is missing local validation payload",
    )
    rollout_states = {event["state"] for event in bundle["rollout_events"]}
    _expect(
        {"assigned", "approved", "downloading", "imported", "activated", "rolled_back"}
        <= rollout_states,
        "evidence does not prove rollout lifecycle",
    )
    package_promotion = bundle["hub_lite"]["packages"]["pkg-canonical-edge-demo"]["promotion"]
    _expect(
        package_promotion["state"] == "released",
        "evidence does not prove package release",
    )
    _expect(
        "released" in {event.get("state") for event in bundle["package_promotions"]},
        "evidence timeline does not include package release event",
    )
    _expect(
        any(
            event.get("plan_id") == rollout_plan_id
            and event.get("state") in {"advanced", "completed"}
            and rollout_id in event.get("rollout_ids", [])
            for event in bundle["rollout_plans"]
        ),
        "evidence does not prove rollout plan coordination",
    )
    approval = bundle["hub_lite"]["rollouts"][rollout_id]["approval"]
    _expect(approval["approved"] is True, "evidence does not prove rollout approval")
    _expect(
        approval["actor"] == "operator:approver",
        "evidence does not record approval actor",
    )
    _expect(
        bundle["hub_lite"]["rollouts"][rollout_id]["state"] == "rolled_back",
        "hub lite rollout state did not track rollback",
    )
    decisions = bundle["decisions"]
    _expect(_has_decision(decisions, "rollout", daylight_id), "missing rollout decision")
    _expect(_has_decision(decisions, "policy", lowlight_id), "missing fog switch decision")
    _expect(_has_decision(decisions, "policy", tiny_id), "missing battery downgrade decision")
    fallback = _matching_decision(decisions, "fallback", lowlight_id)
    _expect(fallback is not None, "missing fallback decision")
    _expect(
        fallback["audit_metadata"]["fallback"]["selected_model"] == faulty_id,
        "fallback evidence does not name failed model",
    )
    _expect(_has_decision(decisions, "rollback", tiny_id), "missing rollback decision")
    _expect(_has_decision(decisions, "operator", daylight_id), "missing operator override decision")
    _expect(
        {
            "decision",
            "telemetry",
            "package_import",
            "package_promotion",
            "runtime_validation",
            "rollout",
            "rollout_plan",
        }
        <= {entry["kind"] for entry in bundle["timeline"]},
        "evidence timeline is missing expected event classes",
    )


def _validate_replay(replay: dict[str, Any]) -> None:
    _expect(
        replay["schema_version"] == "temms-mission-replay/v1",
        "bad mission replay schema",
    )
    phases = {
        phase["phase"]: phase
        for phase in replay.get("phases", [])
        if isinstance(phase, dict) and phase.get("phase")
    }
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
        phase = phases.get(phase_name)
        _expect(phase is not None, f"mission replay missing {phase_name} phase")
        _expect(
            phase["status"] == "complete",
            f"mission replay phase {phase_name} was {phase['status']}",
        )
    _expect(replay["incidents"]["fallbacks"], "mission replay missing fallback incident")
    _expect(
        replay["incidents"]["operator_overrides"],
        "mission replay missing operator override incident",
    )
    _expect(replay["events"], "mission replay missing chronological events")


def _validate_hub_aggregation(
    ingested_record: dict[str, Any],
    listed_evidence: dict[str, Any],
    hub_summary: dict[str, Any],
    hub_replay: dict[str, Any],
    source_bundle: dict[str, Any],
) -> None:
    _expect(
        ingested_record["schema_version"] == "temms-ingested-evidence/v1",
        "bad ingested evidence schema",
    )
    _expect(
        ingested_record["device_id"] == "edge-demo",
        "Hub evidence did not preserve the edge device id",
    )
    _expect(
        ingested_record["integrity"]["payload_sha256"]
        == source_bundle["integrity"]["payload_sha256"],
        "Hub evidence integrity does not match edge export",
    )
    _expect(listed_evidence["count"] == 1, "Hub evidence list did not include one bundle")
    _expect(
        listed_evidence["evidence_bundles"][0]["evidence_id"] == ingested_record["evidence_id"],
        "Hub evidence list did not return the ingested bundle",
    )
    _expect(
        hub_summary["schema_version"] == "temms-evidence-summary/v1",
        "bad Hub evidence summary schema",
    )
    _expect(
        hub_summary["counts"]["ingested_evidence_bundles"] == 1,
        "Hub summary did not count ingested evidence",
    )
    _expect(
        hub_summary["ingested_evidence"][0]["evidence_id"] == ingested_record["evidence_id"],
        "Hub summary did not include the ingested evidence id",
    )
    _expect(
        hub_replay["schema_version"] == "temms-mission-replay/v1",
        "bad Hub mission replay schema",
    )
    phases = {
        phase["phase"]: phase
        for phase in hub_replay.get("phases", [])
        if isinstance(phase, dict) and phase.get("phase")
    }
    _expect(
        hub_replay["outcome"]["counts"]["ingested_evidence_bundles"] == 1,
        "Hub replay did not count ingested evidence",
    )
    aggregation_phase = phases.get("evidence_aggregation")
    _expect(aggregation_phase is not None, "Hub replay missing evidence aggregation phase")
    _expect(
        aggregation_phase["status"] == "complete",
        "Hub replay did not mark evidence aggregation complete",
    )


def _has_decision(decisions: list[dict[str, Any]], trigger_type: str, to_model: str) -> bool:
    return _matching_decision(decisions, trigger_type, to_model) is not None


def _matching_decision(
    decisions: list[dict[str, Any]],
    trigger_type: str,
    to_model: str,
) -> dict[str, Any] | None:
    for decision in decisions:
        if decision.get("trigger_type") == trigger_type and decision.get("to_model") == to_model:
            return decision
    return None


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise DemoError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the canonical TEMMS product demo")
    parser.add_argument(
        "--evidence-output",
        type=Path,
        default=Path("temms-canonical-evidence.json"),
        help="Where to write the evidence bundle",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Directory to keep demo workspace artifacts under",
    )
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help="Keep the auto-created temporary workspace",
    )
    parser.add_argument(
        "--json-summary",
        action="store_true",
        help="Print only a machine-readable JSON summary",
    )
    args = parser.parse_args()

    try:
        summary = run_demo(
            evidence_output=args.evidence_output,
            work_dir=args.work_dir,
            keep_work_dir=args.keep_work_dir,
            json_summary=args.json_summary,
        )
    except DemoError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json_summary:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
