from __future__ import annotations

import hashlib
import json
from typing import Any

from temms.core.signing import read_signing_key
from temms.daemon.pending_ops import pending_operation_signature_status


RUNTIME_TARGET_ASSESSMENT_DIGEST_SCHEMA_VERSION = (
    "temms-runtime-target-assessment-digest/v1"
)

_ASSESSMENT_DIGEST_VOLATILE_KEYS = {
    "age_seconds",
    "benchmark_age_seconds",
    "checked_at",
    "created_at",
    "heartbeat_age_seconds",
    "last_heartbeat_at",
    "last_seen_at",
    "recorded_at",
    "reported_at",
    "retargeted_at",
    "updated_at",
}


def pending_sync_preflight(
    state: Any,
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a non-mutating replayability plan for queued DDIL operations."""
    if entries is None:
        entries = _read_pending_entries(state)
    signature_required, signing_key = _signature_policy(state)
    results = [
        _operation_preflight(
            state,
            entry,
            index=index,
            signature_required=signature_required,
            signing_key=signing_key,
        )
        for index, entry in enumerate(entries)
    ]
    results = _annotate_superseded_model_activations(results)
    blocked = [entry for entry in results if not entry.get("ready")]
    superseded = [entry for entry in results if entry.get("superseded")]
    optimization_advisories = [
        entry for entry in results if entry.get("hub_optimization_gates")
    ]
    return {
        "schema_version": "temms-pending-sync-preflight/v1",
        "status": "blocked" if blocked else "ready",
        "signature_required": signature_required,
        "signing_key_configured": bool(signing_key),
        "total": len(results),
        "ready": len(results) - len(blocked),
        "blocked": len(blocked),
        "superseded": len(superseded),
        "optimization_advisories": len(optimization_advisories),
        "slot_outcomes": _slot_outcomes(results),
        "entries": results,
    }


def _operation_preflight(
    state: Any,
    entry: dict[str, Any],
    *,
    index: int,
    signature_required: bool,
    signing_key: str | None,
) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return _blocked(index, "unknown", "pending entry is not an object")
    operation = str(entry.get("operation") or "operation")
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return _blocked(index, operation, "pending entry payload is not an object")

    signature = pending_operation_signature_status(
        entry,
        signing_key=signing_key,
        require_signature=signature_required,
    )
    result = {
        "index": index,
        "operation": operation,
        "payload_sha256": _canonical_hash(payload),
        "signature_status": signature.get("status"),
        "signature_verified": signature.get("verified"),
        "signature_reason": signature.get("reason"),
        **_intent_identifiers(operation, payload),
    }
    signature_status = signature.get("status")
    if signature_status == "invalid" or (
        signature_required and signature_status != "verified"
    ):
        return {
            **result,
            "ready": False,
            "replay_status": "blocked",
            "reason": str(signature.get("reason") or "signature verification failed"),
        }

    operation_result = _operation_replayability(state, operation, payload)
    return {**result, **operation_result}


def _operation_replayability(
    state: Any,
    operation: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if operation == "update_conditions":
        conditions = payload.get("conditions")
        if not isinstance(conditions, dict) or not conditions:
            return _blocked_payload("update_conditions has no conditions")
        return _ready("condition updates can be replayed", condition_count=len(conditions))

    if operation == "override_model":
        slot_name = payload.get("slot_name")
        request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
        model_name = request.get("model")
        if not slot_name:
            return _blocked_payload("override_model is missing slot_name")
        if not model_name:
            return _blocked_payload("override_model is missing request.model")
        if _slot(state, str(slot_name)) is None:
            return _blocked_payload(f"slot not found: {slot_name}")
        model = _model(state, str(model_name))
        if model is None:
            return _blocked_payload(f"model not found: {model_name}")
        return _ready("model override can be replayed", resolved_model_id=getattr(model, "id", None))

    if operation == "deploy":
        context = deploy_intent_context(payload)
        slot_name = context.get("slot")
        model_id = context.get("model_id")
        if not slot_name or not model_id:
            return _blocked_payload("deploy is missing slot and model_id")
        if _slot(state, str(slot_name)) is None:
            return _blocked_payload(f"slot not found: {slot_name}")
        model = _model(state, str(model_id))
        if model is None:
            return _blocked_payload(f"model not found: {model_id}")
        resolved_model_id = str(getattr(model, "id", None) or model_id)
        hub_readiness = _hub_deploy_replayability(
            state,
            payload,
            slot_name=str(slot_name),
            model_id=resolved_model_id,
            model=model,
        )
        if hub_readiness.get("ready") is False:
            return hub_readiness
        return _ready(
            "deploy can be replayed",
            resolved_model_id=resolved_model_id,
            **hub_readiness,
        )

    return _blocked_payload(f"unsupported pending operation: {operation}")


def _annotate_superseded_model_activations(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest_by_slot: dict[str, dict[str, Any]] = {}
    for result in results:
        slot = _model_activation_slot(result)
        if slot is None:
            continue
        latest_by_slot[slot] = result

    annotated: list[dict[str, Any]] = []
    for result in results:
        slot = _model_activation_slot(result)
        if slot is None:
            annotated.append(result)
            continue
        latest = latest_by_slot.get(slot)
        if latest is not None and latest.get("index") != result.get("index"):
            annotated.append(
                {
                    **result,
                    "replay_status": "superseded",
                    "reason": (
                        f"superseded by {latest.get('operation')} intent at index "
                        f"{latest.get('index')} for slot {slot}"
                    ),
                    "superseded": True,
                    "superseded_by_index": latest.get("index"),
                    "superseded_by_model_id": latest.get("resolved_model_id")
                    or latest.get("model_id"),
                    "final_for_slot": False,
                }
            )
            continue
        annotated.append({**result, "final_for_slot": True})
    return annotated


def _slot_outcomes(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for result in results:
        if result.get("final_for_slot") is not True:
            continue
        slot = _model_activation_slot(result)
        if slot is None:
            continue
        outcomes.append(
            {
                "slot": slot,
                "index": result.get("index"),
                "operation": result.get("operation"),
                "model_id": result.get("resolved_model_id") or result.get("model_id"),
                "payload_sha256": result.get("payload_sha256"),
            }
        )
    return outcomes


def _model_activation_slot(result: dict[str, Any]) -> str | None:
    if result.get("ready") is not True:
        return None
    if result.get("operation") not in {"deploy", "override_model"}:
        return None
    slot = result.get("slot")
    model_id = result.get("resolved_model_id") or result.get("model_id")
    if not slot or not model_id:
        return None
    return str(slot)


def _intent_identifiers(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _readiness_refs(deploy_intent_context(payload))


def deploy_intent_context(
    payload: dict[str, Any],
    *,
    model: Any | None = None,
) -> dict[str, Any]:
    """Return normalized context for queued deploy-style payloads."""
    package_id = _payload_value(payload, "package_id") or getattr(model, "package_id", None)
    return {
        "slot": _payload_value(payload, "slot", "slot_name"),
        "model_id": _payload_value(payload, "model_id", "model"),
        "device_id": _payload_value(payload, "device_id"),
        "package_id": package_id,
        "runtime_target_id": _payload_value(payload, "runtime_target_id"),
        "actor": _payload_value(payload, "actor"),
        "source": _payload_value(payload, "source"),
        "reason": _payload_value(payload, "reason"),
        "duration_s": _payload_value(payload, "duration_s"),
    }


def _hub_deploy_replayability(
    state: Any,
    payload: dict[str, Any],
    *,
    slot_name: str,
    model_id: str,
    model: Any,
) -> dict[str, Any]:
    """Return Hub Lite readiness refs for a queued deploy intent when available."""
    hub_lite = getattr(state, "hub_lite", None)
    if hub_lite is None:
        return {}

    context = deploy_intent_context(payload, model=model)
    intent_has_hub_context = any(
        context.get(key) for key in ("package_id", "device_id", "runtime_target_id")
    )
    if not intent_has_hub_context:
        return {}

    package_id = context.get("package_id")
    device_id = context.get("device_id")
    runtime_target_id = context.get("runtime_target_id")
    if not package_id or not device_id or not runtime_target_id:
        return {
            "ready": False,
            "replay_status": "blocked",
            "reason": (
                "deploy hub readiness is missing package_id, device_id, or "
                "runtime_target_id"
            ),
            "hub_readiness_status": "missing_context",
        }

    try:
        readiness = hub_lite.deployment_readiness(
            package_id=str(package_id),
            model_id=model_id,
            device_id=str(device_id),
            runtime_target_id=str(runtime_target_id),
            slot=slot_name,
        )
    except Exception as exc:
        return {
            "ready": False,
            "replay_status": "blocked",
            "reason": f"hub readiness check failed: {exc}",
            "hub_readiness_status": "error",
        }

    runtime_summary = _hub_runtime_readiness_summary(readiness)
    capability_lock_failure = _hub_capability_lock_failure(readiness)
    blocking_gates = _hub_replay_blocking_gates(readiness)
    if capability_lock_failure:
        return {
            "ready": False,
            "replay_status": "blocked",
            "reason": capability_lock_failure,
            "hub_readiness_status": readiness.get("status"),
            "hub_readiness_selection": readiness.get("selection"),
            "hub_blocking_gates": blocking_gates,
            **runtime_summary,
        }
    retarget_proof_failure = _hub_runtime_retarget_proof_failure(
        payload,
        runtime_summary,
        runtime_target_id=str(runtime_target_id),
    )
    if retarget_proof_failure:
        return {
            "ready": False,
            "replay_status": "blocked",
            "reason": str(retarget_proof_failure.get("reason")),
            "hub_readiness_status": readiness.get("status"),
            "hub_readiness_selection": readiness.get("selection"),
            "hub_blocking_gates": blocking_gates,
            **runtime_summary,
            **retarget_proof_failure,
        }
    if blocking_gates:
        return {
            "ready": False,
            "replay_status": "blocked",
            "reason": "hub readiness blocks replay: " + _gate_summary(blocking_gates),
            "hub_readiness_status": readiness.get("status"),
            "hub_readiness_selection": readiness.get("selection"),
            "hub_blocking_gates": blocking_gates,
            **runtime_summary,
        }

    attention_gates = [
        _gate_ref(gate)
        for gate in readiness.get("gates", [])
        if isinstance(gate, dict) and gate.get("status") == "attention"
    ]
    optimization_gates = [
        gate for gate in attention_gates if gate.get("gate_id") == "runtime_optimizer"
    ]
    return _readiness_refs({
        "replay_status": "ready_with_runtime_advisory" if optimization_gates else None,
        "hub_readiness_status": readiness.get("status"),
        "hub_readiness_selection": readiness.get("selection"),
        "hub_attention_gates": attention_gates,
        "hub_optimization_gates": optimization_gates,
        **runtime_summary,
    })


def _hub_runtime_readiness_summary(readiness: dict[str, Any]) -> dict[str, Any]:
    runtime_fit = (
        readiness.get("runtime_fit")
        if isinstance(readiness.get("runtime_fit"), dict)
        else {}
    )
    runtime_lane = (
        runtime_fit.get("runtime_lane")
        if isinstance(runtime_fit.get("runtime_lane"), dict)
        else {}
    )
    artifact_lane = (
        runtime_fit.get("artifact_lane")
        if isinstance(runtime_fit.get("artifact_lane"), dict)
        else {}
    )
    target_selection = (
        runtime_fit.get("target_selection")
        if isinstance(runtime_fit.get("target_selection"), dict)
        else {}
    )
    admission = (
        readiness.get("production_admission")
        if isinstance(readiness.get("production_admission"), dict)
        else {}
    )
    capability_lock = _hub_runtime_capability_lock(readiness)
    edge_inventory = (
        capability_lock.get("edge_inventory")
        if isinstance(capability_lock.get("edge_inventory"), dict)
        else {}
    )
    telemetry_freshness = (
        edge_inventory.get("telemetry_freshness")
        if isinstance(edge_inventory.get("telemetry_freshness"), dict)
        else {}
    )
    contract = (
        readiness.get("edge_execution_contract")
        if isinstance(readiness.get("edge_execution_contract"), dict)
        else {}
    )
    workbench = _hub_runtime_workbench(readiness)
    workbench_selection = (
        workbench.get("target_selection")
        if isinstance(workbench.get("target_selection"), dict)
        else {}
    )
    workbench_summary = (
        workbench.get("summary") if isinstance(workbench.get("summary"), dict) else {}
    )
    contract_path = (
        contract.get("path") if isinstance(contract.get("path"), dict) else {}
    )
    proof_policy = (
        contract.get("proof_policy")
        if isinstance(contract.get("proof_policy"), dict)
        else {}
    )
    target_assessments = _hub_target_assessments(readiness)
    return _readiness_refs(
        {
            "hub_runtime_fit_score": runtime_fit.get("score"),
            "hub_runtime_fit_tier": runtime_fit.get("tier"),
            "hub_runtime_fit_detail": runtime_fit.get("detail"),
            "hub_runtime_lane_id": runtime_lane.get("lane_id"),
            "hub_runtime_lane_label": runtime_lane.get("label"),
            "hub_runtime_lane_engine": runtime_lane.get("execution_engine"),
            "hub_runtime_lane_acceleration": runtime_lane.get("acceleration"),
            "hub_artifact_lane_status": artifact_lane.get("status"),
            "hub_artifact_lane_state": artifact_lane.get("state"),
            "hub_artifact_lane_detail": artifact_lane.get("detail"),
            "hub_artifact_format": artifact_lane.get("model_format"),
            "hub_target_selection_status": target_selection.get("status"),
            "hub_best_runtime_target_id": target_selection.get("best_runtime_target_id"),
            "hub_runtime_score_delta": target_selection.get("score_delta"),
            "hub_production_admission_status": admission.get("status"),
            "hub_production_apply_allowed": admission.get("apply_allowed"),
            "hub_runtime_capability_lock": _hub_runtime_capability_lock_ref(
                capability_lock
            ),
            "hub_capability_lock_status": capability_lock.get("status"),
            "hub_capability_sha256": capability_lock.get("capability_sha256"),
            "hub_capability_runtime_target_id": capability_lock.get(
                "runtime_target_id"
            ),
            "hub_capability_runtime_mode": capability_lock.get("runtime_mode"),
            "hub_capability_edge_profile": edge_inventory.get("device_profile"),
            "hub_capability_telemetry_status": telemetry_freshness.get("status"),
            "hub_capability_telemetry_state": telemetry_freshness.get("state"),
            "hub_capability_telemetry_detail": telemetry_freshness.get("detail"),
            "hub_capability_heartbeat_age_seconds": telemetry_freshness.get(
                "heartbeat_age_seconds"
            ),
            "hub_capability_heartbeat_stale_after_seconds": telemetry_freshness.get(
                "heartbeat_stale_after_seconds"
            ),
            "hub_capability_failures": (
                capability_lock.get("failures")
                if isinstance(capability_lock.get("failures"), list)
                else None
            ),
            "hub_edge_execution_contract_status": contract.get("status"),
            "hub_edge_execution_contract_action": contract.get("recommended_action"),
            "hub_edge_execution_contract_path": contract_path.get("label"),
            "hub_proof_policy": proof_policy,
            "hub_runtime_workbench_schema_version": workbench.get("schema_version"),
            "hub_runtime_workbench_status": workbench.get("status"),
            "hub_runtime_workbench_action": workbench.get("recommended_action"),
            "hub_runtime_workbench_selected_runtime_target_id": workbench.get(
                "selected_runtime_target_id"
            ),
            "hub_runtime_workbench_best_runtime_target_id": workbench.get(
                "best_runtime_target_id"
            ),
            "hub_runtime_workbench_target_selection_status": workbench_selection.get(
                "status"
            ),
            "hub_runtime_workbench_target_count": workbench_summary.get("target_count"),
            "hub_runtime_workbench_eligible_target_count": workbench_summary.get(
                "eligible_target_count"
            ),
            "hub_runtime_workbench_blocked_target_count": workbench_summary.get(
                "blocked_target_count"
            ),
            "hub_runtime_workbench_selected_is_best": workbench_summary.get(
                "selected_is_best"
            ),
            "hub_runtime_workbench_production_apply_allowed": workbench_summary.get(
                "production_apply_allowed"
            ),
            "hub_target_assessments": target_assessments,
        }
    )


def _hub_runtime_workbench(readiness: dict[str, Any]) -> dict[str, Any]:
    workbench = readiness.get("runtime_workbench")
    return workbench if isinstance(workbench, dict) else {}


def _hub_target_assessments(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    """Return compact per-target Hub runtime assessments for DDIL repair proof."""
    runtime_fit = (
        readiness.get("runtime_fit")
        if isinstance(readiness.get("runtime_fit"), dict)
        else {}
    )
    target_selection = (
        runtime_fit.get("target_selection")
        if isinstance(runtime_fit.get("target_selection"), dict)
        else {}
    )
    workbench = _hub_runtime_workbench(readiness)
    sources = (
        workbench,
        readiness.get("edge_execution_contract"),
        readiness.get("runtime_decision"),
        target_selection,
    )
    for source in sources:
        if not isinstance(source, dict):
            continue
        assessments = source.get("targets")
        if not isinstance(assessments, list):
            assessments = source.get("target_assessments")
        if not isinstance(assessments, list):
            continue
        compacted = [
            _hub_target_assessment_ref(assessment)
            for assessment in assessments
            if isinstance(assessment, dict)
        ]
        if compacted:
            return compacted
    return []


def _hub_target_assessment_ref(assessment: dict[str, Any]) -> dict[str, Any]:
    proof = (
        assessment.get("proof")
        if isinstance(assessment.get("proof"), dict)
        else {}
    )
    lock = (
        assessment.get("runtime_capability_lock")
        if isinstance(assessment.get("runtime_capability_lock"), dict)
        else {}
    )
    if not lock and proof:
        lock = _readiness_refs(
            {
                "status": proof.get("capability_lock_status"),
                "capability_sha256": proof.get("capability_sha256"),
                "runtime_target_id": assessment.get("runtime_target_id"),
            }
        )
    component_states = (
        assessment.get("component_states")
        if isinstance(assessment.get("component_states"), dict)
        else {}
    )
    if proof:
        runtime_validation = (
            component_states.get("runtime_validation")
            if isinstance(component_states.get("runtime_validation"), dict)
            else {}
        )
        if proof.get("validation_id") and not runtime_validation.get("validation_id"):
            component_states = {
                **component_states,
                "runtime_validation": _readiness_refs(
                    {
                        **runtime_validation,
                        "validation_id": proof.get("validation_id"),
                        "state": runtime_validation.get("state")
                        or proof.get("runtime_validation_state"),
                        "status": runtime_validation.get("status")
                        or proof.get("runtime_validation_status"),
                    }
                ),
            }
    return _readiness_refs(
        {
            "runtime_target_id": assessment.get("runtime_target_id"),
            "rank": assessment.get("rank"),
            "selected": assessment.get("selected"),
            "best": assessment.get("best"),
            "status": assessment.get("status"),
            "eligible": assessment.get("eligible"),
            "score": assessment.get("score"),
            "tier": assessment.get("tier"),
            "detail": assessment.get("detail"),
            "runtime_target": assessment.get("runtime_target")
            if isinstance(assessment.get("runtime_target"), dict)
            else None,
            "runtime_lane": assessment.get("runtime_lane")
            if isinstance(assessment.get("runtime_lane"), dict)
            else None,
            "artifact_lane": assessment.get("artifact_lane")
            if isinstance(assessment.get("artifact_lane"), dict)
            else None,
            "runtime_capability_lock": _hub_runtime_capability_lock_ref(lock),
            "latency_ms_p95": assessment.get("latency_ms_p95")
            or proof.get("latency_ms_p95"),
            "throughput_ips": assessment.get("throughput_ips")
            or proof.get("throughput_ips"),
            "benchmark_id": assessment.get("benchmark_id")
            or proof.get("benchmark_id"),
            "remediation": assessment.get("remediation")
            if isinstance(assessment.get("remediation"), dict)
            else None,
            "component_states": component_states,
            "reasons": assessment.get("reasons")
            if isinstance(assessment.get("reasons"), list)
            else None,
            "penalties": assessment.get("penalties")
            if isinstance(assessment.get("penalties"), list)
            else None,
            "blocked": assessment.get("blocked"),
        }
    )


def _hub_runtime_retarget_proof_failure(
    payload: dict[str, Any],
    runtime_summary: dict[str, Any],
    *,
    runtime_target_id: str,
) -> dict[str, Any] | None:
    proof = _latest_runtime_retarget_proof(payload)
    if not proof:
        return None

    proof_target_id = str(proof.get("runtime_target_id") or "").strip()
    if proof_target_id != runtime_target_id:
        return _retarget_proof_failure(
            "runtime retarget proof target does not match queued deploy target",
            status="target_mismatch",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
        )

    current_assessment = _target_assessment(
        runtime_summary.get("hub_target_assessments"),
        runtime_target_id,
    )
    if not current_assessment:
        return _retarget_proof_failure(
            "runtime retarget proof cannot be verified against current target assessments",
            status="missing_current_assessment",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
        )
    if str(proof.get("status") or "").lower() != "proved":
        return _retarget_proof_failure(
            "runtime retarget proof is not proved",
            status="unproved",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
            current_assessment=current_assessment,
        )
    if proof.get("best") is True and current_assessment.get("best") is not True:
        return _retarget_proof_failure(
            "runtime retarget proof is stale: current target is no longer best measured runtime",
            status="stale_best_runtime",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
            current_assessment=current_assessment,
        )
    if proof.get("eligible") is True and current_assessment.get("eligible") is not True:
        return _retarget_proof_failure(
            "runtime retarget proof is stale: current target is no longer eligible",
            status="stale_eligibility",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
            current_assessment=current_assessment,
        )

    proof_capability = _proof_capability_sha256(proof)
    current_capability = str(runtime_summary.get("hub_capability_sha256") or "").strip()
    if not proof_capability or not current_capability:
        return _retarget_proof_failure(
            "runtime retarget proof is missing capability hash comparison",
            status="missing_capability_hash",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
            current_assessment=current_assessment,
        )
    if proof_capability != current_capability:
        return _retarget_proof_failure(
            "runtime retarget proof is stale: capability hash changed",
            status="stale_capability_hash",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
            current_assessment=current_assessment,
        )

    proof_validation = str(proof.get("runtime_validation_id") or "").strip()
    current_validation = _assessment_runtime_validation_id(current_assessment)
    if proof_validation and current_validation and proof_validation != current_validation:
        return _retarget_proof_failure(
            "runtime retarget proof is stale: runtime validation evidence changed",
            status="stale_runtime_validation",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
            current_assessment=current_assessment,
        )

    proof_benchmark = str(proof.get("benchmark_id") or "").strip()
    current_benchmark = str(current_assessment.get("benchmark_id") or "").strip()
    if proof_benchmark and current_benchmark and proof_benchmark != current_benchmark:
        return _retarget_proof_failure(
            "runtime retarget proof is stale: benchmark evidence changed",
            status="stale_benchmark",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
            current_assessment=current_assessment,
        )

    proof_assessment = str(proof.get("target_assessment_sha256") or "").strip()
    current_assessment_sha256 = runtime_target_assessment_sha256(current_assessment)
    if not proof_assessment or not current_assessment_sha256:
        return _retarget_proof_failure(
            "runtime retarget proof is missing target assessment hash comparison",
            status="missing_target_assessment_hash",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
            current_assessment=current_assessment,
        )
    if proof_assessment != current_assessment_sha256:
        return _retarget_proof_failure(
            "runtime retarget proof is stale: target assessment changed",
            status="stale_target_assessment",
            proof=proof,
            runtime_summary=runtime_summary,
            runtime_target_id=runtime_target_id,
            current_assessment=current_assessment,
        )
    return None


def runtime_target_assessment_sha256(assessment: dict[str, Any]) -> str:
    """Return the stable hash binding a DDIL retarget proof to target evidence."""
    basis = runtime_target_assessment_digest_basis(assessment)
    if not basis.get("runtime_target_id"):
        return ""
    return _canonical_hash(basis)


def runtime_target_assessment_digest_basis(
    assessment: dict[str, Any],
) -> dict[str, Any]:
    """Return stable target facts that should make stale retarget proof fail."""
    if not isinstance(assessment, dict):
        return {}
    lock = (
        assessment.get("runtime_capability_lock")
        if isinstance(assessment.get("runtime_capability_lock"), dict)
        else {}
    )
    component_states = (
        assessment.get("component_states")
        if isinstance(assessment.get("component_states"), dict)
        else {}
    )
    runtime_validation = (
        component_states.get("runtime_validation")
        if isinstance(component_states.get("runtime_validation"), dict)
        else {}
    )
    runtime_target = (
        assessment.get("runtime_target")
        if isinstance(assessment.get("runtime_target"), dict)
        else lock.get("runtime_target")
        if isinstance(lock.get("runtime_target"), dict)
        else {}
    )
    artifact_lane = (
        assessment.get("artifact_lane")
        if isinstance(assessment.get("artifact_lane"), dict)
        else lock.get("artifact_lane")
        if isinstance(lock.get("artifact_lane"), dict)
        else {}
    )
    basis = {
        "schema_version": RUNTIME_TARGET_ASSESSMENT_DIGEST_SCHEMA_VERSION,
        "runtime_target_id": assessment.get("runtime_target_id"),
        "rank": assessment.get("rank"),
        "best": assessment.get("best"),
        "status": assessment.get("status"),
        "eligible": assessment.get("eligible"),
        "blocked": assessment.get("blocked"),
        "score": assessment.get("score"),
        "tier": assessment.get("tier"),
        "detail": assessment.get("detail"),
        "runtime_target": _stable_assessment_digest_value(runtime_target),
        "runtime_lane": _stable_assessment_digest_value(
            assessment.get("runtime_lane")
            if isinstance(assessment.get("runtime_lane"), dict)
            else {}
        ),
        "artifact_lane": _stable_assessment_digest_value(artifact_lane),
        "runtime_capability_lock": _stable_assessment_digest_value(
            {
                "schema_version": lock.get("schema_version"),
                "status": lock.get("status"),
                "capability_sha256": lock.get("capability_sha256"),
                "runtime_target_id": lock.get("runtime_target_id"),
                "runtime_mode": lock.get("runtime_mode"),
                "runtime_target": lock.get("runtime_target")
                if isinstance(lock.get("runtime_target"), dict)
                else None,
                "artifact_lane": lock.get("artifact_lane")
                if isinstance(lock.get("artifact_lane"), dict)
                else None,
                "failures": lock.get("failures")
                if isinstance(lock.get("failures"), list)
                else None,
            }
        ),
        "evidence": _stable_assessment_digest_value(
            {
                "runtime_validation_id": runtime_validation.get("validation_id"),
                "benchmark_id": assessment.get("benchmark_id"),
                "latency_ms_p95": assessment.get("latency_ms_p95"),
                "throughput_ips": assessment.get("throughput_ips"),
            }
        ),
        "component_states": _stable_assessment_digest_value(component_states),
        "reasons": _stable_assessment_digest_value(assessment.get("reasons")),
        "penalties": _stable_assessment_digest_value(assessment.get("penalties")),
    }
    return _stable_assessment_digest_value(basis)


def _stable_assessment_digest_value(value: Any) -> Any:
    if isinstance(value, dict):
        stable: dict[str, Any] = {}
        for key in sorted(value):
            if key in _ASSESSMENT_DIGEST_VOLATILE_KEYS:
                continue
            child = _stable_assessment_digest_value(value[key])
            if child is not None and child != "" and child != [] and child != {}:
                stable[str(key)] = child
        return stable
    if isinstance(value, list):
        stable_list = [
            child
            for item in value
            for child in [_stable_assessment_digest_value(item)]
            if child is not None and child != "" and child != [] and child != {}
        ]
        return stable_list
    return value


def _latest_runtime_retarget_proof(payload: dict[str, Any]) -> dict[str, Any]:
    records = payload.get("_temms_runtime_retarget")
    if not isinstance(records, list) or not records:
        return {}
    latest = records[-1]
    if not isinstance(latest, dict):
        return {}
    proof = latest.get("runtime_target_proof")
    return proof if isinstance(proof, dict) else {}


def _target_assessment(assessments: Any, runtime_target_id: str) -> dict[str, Any]:
    if not isinstance(assessments, list):
        return {}
    for assessment in assessments:
        if not isinstance(assessment, dict):
            continue
        if str(assessment.get("runtime_target_id") or "").strip() == runtime_target_id:
            return assessment
    return {}


def _proof_capability_sha256(proof: dict[str, Any]) -> str:
    lock = (
        proof.get("runtime_capability_lock")
        if isinstance(proof.get("runtime_capability_lock"), dict)
        else {}
    )
    return str(proof.get("capability_sha256") or lock.get("capability_sha256") or "").strip()


def _assessment_runtime_validation_id(assessment: dict[str, Any]) -> str:
    component_states = (
        assessment.get("component_states")
        if isinstance(assessment.get("component_states"), dict)
        else {}
    )
    runtime_validation = (
        component_states.get("runtime_validation")
        if isinstance(component_states.get("runtime_validation"), dict)
        else {}
    )
    proof = assessment.get("proof") if isinstance(assessment.get("proof"), dict) else {}
    return str(runtime_validation.get("validation_id") or proof.get("validation_id") or "").strip()


def _retarget_proof_failure(
    reason: str,
    *,
    status: str,
    proof: dict[str, Any],
    runtime_summary: dict[str, Any],
    runtime_target_id: str,
    current_assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_assessment = current_assessment or {}
    return _readiness_refs(
        {
            "reason": reason,
            "hub_runtime_retarget_proof_status": status,
            "hub_runtime_retarget_proof_runtime_target_id": runtime_target_id,
            "hub_runtime_retarget_proof_signed_capability_sha256": _proof_capability_sha256(
                proof
            ),
            "hub_runtime_retarget_proof_current_capability_sha256": runtime_summary.get(
                "hub_capability_sha256"
            ),
            "hub_runtime_retarget_proof_signed_validation_id": proof.get(
                "runtime_validation_id"
            ),
            "hub_runtime_retarget_proof_current_validation_id": (
                _assessment_runtime_validation_id(current_assessment)
            ),
            "hub_runtime_retarget_proof_signed_benchmark_id": proof.get(
                "benchmark_id"
            ),
            "hub_runtime_retarget_proof_current_benchmark_id": current_assessment.get(
                "benchmark_id"
            ),
            "hub_runtime_retarget_proof_signed_target_assessment_sha256": proof.get(
                "target_assessment_sha256"
            ),
            "hub_runtime_retarget_proof_current_target_assessment_sha256": (
                runtime_target_assessment_sha256(current_assessment)
            ),
            "hub_runtime_retarget_proof_target_selection_status": runtime_summary.get(
                "hub_target_selection_status"
            ),
        }
    )


def _hub_runtime_capability_lock(readiness: dict[str, Any]) -> dict[str, Any]:
    for source_name in ("edge_execution_contract", "runtime_decision", "runtime_fit"):
        source = readiness.get(source_name)
        if not isinstance(source, dict):
            continue
        lock = source.get("runtime_capability_lock")
        if isinstance(lock, dict) and lock:
            return lock
    return {}


def _hub_runtime_capability_lock_ref(lock: dict[str, Any]) -> dict[str, Any]:
    if not lock:
        return {}
    return _readiness_refs(
        {
            "schema_version": lock.get("schema_version"),
            "status": lock.get("status"),
            "capability_sha256": lock.get("capability_sha256"),
            "runtime_target_id": lock.get("runtime_target_id"),
            "runtime_mode": lock.get("runtime_mode"),
            "runtime_target": lock.get("runtime_target")
            if isinstance(lock.get("runtime_target"), dict)
            else None,
            "edge_inventory": lock.get("edge_inventory")
            if isinstance(lock.get("edge_inventory"), dict)
            else None,
            "artifact_lane": lock.get("artifact_lane")
            if isinstance(lock.get("artifact_lane"), dict)
            else None,
            "failures": lock.get("failures")
            if isinstance(lock.get("failures"), list)
            else None,
        }
    )


def _hub_capability_lock_failure(readiness: dict[str, Any]) -> str | None:
    lock = _hub_runtime_capability_lock(readiness)
    if not lock:
        return None
    status = str(lock.get("status") or "")
    failures = [
        str(failure)
        for failure in lock.get("failures", [])
        if failure is not None and str(failure)
    ]
    if status and status != "locked":
        detail = f"runtime capability lock status is {status}, expected locked"
        if failures:
            detail += ": " + "; ".join(failures[:3])
        return detail
    if failures:
        return "runtime capability lock has failures: " + "; ".join(failures[:3])
    return None


def _hub_replay_blocking_gates(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    safety_attention_gates = {
        "model_package",
        "runtime_target",
        "performance_fit",
        "resource_envelope",
        "edge_target",
    }
    blocking: list[dict[str, Any]] = []
    for gate in readiness.get("gates", []):
        if not isinstance(gate, dict):
            continue
        gate_id = str(gate.get("gate_id") or "")
        status = str(gate.get("status") or "")
        if status == "blocked" or (status == "attention" and gate_id in safety_attention_gates):
            blocking.append(_gate_ref(gate))
    return blocking


def _gate_ref(gate: dict[str, Any]) -> dict[str, Any]:
    return _readiness_refs(
        {
            "gate_id": gate.get("gate_id"),
            "label": gate.get("label"),
            "status": gate.get("status"),
            "state": gate.get("state"),
            "detail": gate.get("detail"),
            "refs": gate.get("refs") if isinstance(gate.get("refs"), dict) else None,
            "actions": gate.get("actions") if isinstance(gate.get("actions"), list) else None,
        }
    )


def _gate_summary(gates: list[dict[str, Any]]) -> str:
    parts = [
        f"{gate.get('label') or gate.get('gate_id')} {gate.get('state')}: {gate.get('detail')}"
        for gate in gates[:3]
    ]
    remaining = len(gates) - len(parts)
    if remaining > 0:
        parts.append(f"{remaining} more gate{'s' if remaining != 1 else ''}")
    return "; ".join(parts)


def _readiness_refs(refs: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in refs.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _payload_value(payload: dict[str, Any], *keys: str) -> Any:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    for source in (payload, request):
        for key in keys:
            value = source.get(key)
            if value is not None and value != "":
                return value
    return None


def _ready(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ready": True, "replay_status": "ready", "reason": reason, **extra}


def _blocked_payload(reason: str) -> dict[str, Any]:
    return {"ready": False, "replay_status": "blocked", "reason": reason}


def _blocked(index: int, operation: str, reason: str) -> dict[str, Any]:
    return {
        "index": index,
        "operation": operation,
        "ready": False,
        "replay_status": "blocked",
        "reason": reason,
    }


def _signature_policy(state: Any) -> tuple[bool, str | None]:
    daemon_config = getattr(state, "daemon_config", None)
    require_signature = bool(getattr(daemon_config, "rollout_require_signature", False))
    if daemon_config is None:
        return require_signature, None
    try:
        signing_key = read_signing_key(
            getattr(daemon_config, "rollout_signing_key", None),
            getattr(daemon_config, "rollout_signing_key_file", None),
        )
    except Exception:
        signing_key = None
    return require_signature, signing_key


def _read_pending_entries(state: Any) -> list[dict[str, Any]]:
    store = getattr(state, "pending_operations", None)
    if store is None:
        return []
    if isinstance(store, list):
        return [entry for entry in store if isinstance(entry, dict)]
    read_all = getattr(store, "read_all", None)
    if not callable(read_all):
        return []
    try:
        return [entry for entry in read_all() if isinstance(entry, dict)]
    except Exception:
        return []


def _slot(state: Any, slot_name: str) -> Any:
    slot_manager = getattr(state, "slot_manager", None)
    get_slot = getattr(slot_manager, "get_slot", None)
    return get_slot(slot_name) if callable(get_slot) else None


def _model(state: Any, model_id_or_name: str) -> Any:
    model_cache = getattr(state, "model_cache", None)
    get_model = getattr(model_cache, "get_model", None)
    find_model = getattr(model_cache, "find_model", None)
    model = get_model(model_id_or_name) if callable(get_model) else None
    if model is None and callable(find_model):
        model = find_model(model_id_or_name)
    return model


def _canonical_hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(data).hexdigest()
