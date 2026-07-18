"""
Post-mission evidence bundle construction.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import socket
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def build_evidence_bundle(
    state: Any,
    telemetry_limit: int | None = None,
    decision_limit: int = 100,
    include_benchmarks: bool = True,
) -> dict[str, Any]:
    """Build a portable evidence bundle from the current edge agent state."""
    decisions = decision_timeline(state, limit=decision_limit)
    telemetry_events = (
        state.telemetry.read(limit=telemetry_limit)
        if getattr(state, "telemetry", None) is not None
        else []
    )
    rollout_events = rollout_timeline(state, limit=decision_limit)
    runtime_validations = runtime_validation_timeline(state, limit=decision_limit)
    hub_benchmarks = hub_benchmark_timeline(state, limit=decision_limit)
    runtime_fit_evidence = runtime_fit_evidence_timeline(state, limit=decision_limit)
    package_imports = package_import_timeline(state, limit=decision_limit)
    package_promotions = package_promotion_timeline(state, limit=decision_limit)
    rollout_plans = rollout_plan_timeline(state, limit=decision_limit)
    slots = [_slot_to_dict(slot) for slot in state.slot_manager.list_slots()]
    active_slots = _active_slot_summaries({"slots": slots})

    payload = {
        "schema_version": "temms-evidence-bundle/v1",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "hub_lite": (
            state.hub_lite.export_bundle().get("hub_lite")
            if getattr(state, "hub_lite", None) is not None
            else None
        ),
        "runtime": _runtime_context(state),
        "deployment_state": _deployment_state(state),
        "diagnostics": _diagnostics(state),
        "slots": slots,
        "runtime_slots": state.inference_runtime.get_all_slots_info(),
        "conditions": {
            path: condition.to_dict() for path, condition in state.condition_store.get_all().items()
        },
        "condition_snapshot": state.condition_store.get_snapshot(),
        "models": [model.to_dict() for model in state.model_cache.list_models()],
        "packages": [_package_to_dict(package) for package in state.model_cache.list_packages()],
        "decisions": decisions,
        "decision_chain": _decision_chain_evidence(state),
        "telemetry": {
            "count": len(telemetry_events),
            "events": telemetry_events,
        },
        "rollout_events": rollout_events,
        "runtime_validations": runtime_validations,
        "hub_benchmarks": hub_benchmarks,
        "runtime_fit_evidence": runtime_fit_evidence,
        "package_imports": package_imports,
        "package_promotions": package_promotions,
        "rollout_plans": rollout_plans,
        "benchmarks": _benchmark_results(state) if include_benchmarks else [],
        "timeline": combined_timeline(
            decisions,
            telemetry_events,
            rollout_events,
            runtime_validations,
            hub_benchmarks,
            runtime_fit_evidence,
            package_imports,
            package_promotions,
            rollout_plans,
            active_slots=active_slots,
        ),
    }
    payload["integrity"] = {
        "payload_sha256": _canonical_hash(payload),
        "algorithm": "sha256/json-canonical-v1",
    }
    return payload


def summarize_evidence_bundle(
    bundle: dict[str, Any],
    limit: int = 20,
) -> dict[str, Any]:
    """Build an operator-readable mission replay summary from evidence JSON."""
    limit = max(limit, 0)
    runtime = _as_dict(bundle.get("runtime"))
    decisions = [
        _normalize_decision(decision)
        for decision in _as_list(bundle.get("decisions"))
        if isinstance(decision, dict)
    ]
    runtime_validations = [
        validation
        for validation in _as_list(bundle.get("runtime_validations"))
        if isinstance(validation, dict)
    ]
    active_slots = _active_slot_summaries(bundle)
    runtime_fit_evidence = _dedupe_runtime_fit_evidence(
        [
            evidence
            for evidence in _as_list(bundle.get("runtime_fit_evidence"))
            if isinstance(evidence, dict)
        ]
    )
    rollout_events = [
        event for event in _as_list(bundle.get("rollout_events")) if isinstance(event, dict)
    ]
    rollout_plans = [
        event for event in _as_list(bundle.get("rollout_plans")) if isinstance(event, dict)
    ]
    rollout_approvals = _rollout_approval_summaries(
        bundle,
        rollout_events,
        limit=limit,
    )
    ingested_evidence = _ingested_evidence_summaries(bundle, limit=limit)
    package_imports = _package_trust_records(bundle)
    package_promotions = _package_promotion_summaries(bundle, limit=limit)
    signed_package_ids = _signed_package_ids(package_imports) | _signed_catalog_package_ids(
        bundle
    )
    pending_operations = [
        operation
        for operation in _as_list(runtime.get("pending_operations"))
        if isinstance(operation, dict)
    ]
    pending_dead_letters = [
        {**operation, "acknowledged": bool(operation.get("acknowledged"))}
        for operation in _as_list(runtime.get("pending_operation_dead_letters"))
        if isinstance(operation, dict)
    ]
    unresolved_dead_letters = [
        operation
        for operation in pending_dead_letters
        if not operation.get("acknowledged") and not operation.get("requeued")
    ]
    acknowledged_dead_letters = [
        operation for operation in pending_dead_letters if operation.get("acknowledged")
    ]
    requeued_dead_letters = [
        operation for operation in pending_dead_letters if operation.get("requeued")
    ]
    deployment_state = _summary_deployment_state(bundle)
    models_by_id = _models_by_id(bundle)
    decision_summaries = [
        _summarize_decision(decision, models_by_id, signed_package_ids)
        for decision in decisions[:limit]
    ]

    summary: dict[str, Any] = {
        "schema_version": "temms-evidence-summary/v1",
        "source_schema_version": bundle.get("schema_version"),
        "exported_at": bundle.get("exported_at"),
        "integrity": bundle.get("integrity", {}),
        "headline": _mission_headline(
            decisions,
            runtime,
            deployment_state,
            approvals=rollout_approvals,
        ),
        "runtime": {
            "offline_mode": bool(runtime.get("offline_mode")),
            "deployment_state": deployment_state,
            "pending_operation_signature_required": bool(
                runtime.get("pending_operation_signature_required")
            ),
            "pending_operation_signing_key_configured": bool(
                runtime.get("pending_operation_signing_key_configured")
            ),
            "pending_operation_verification": _as_dict(
                runtime.get("pending_operation_verification")
            ),
            "pending_operation_preflight": _as_dict(
                runtime.get("pending_operation_preflight")
            ),
            "runtime_fit_evidence_count": len(runtime_fit_evidence),
            "runtime_fit_evidence": _runtime_fit_summaries(
                runtime_fit_evidence,
                limit=limit,
                active_slots=active_slots,
            ),
            "pending_operation_dead_letters_count": len(pending_dead_letters),
            "pending_operation_dead_letters_unresolved_count": len(
                unresolved_dead_letters
            ),
            "pending_operation_dead_letters_acknowledged_count": len(
                acknowledged_dead_letters
            ),
            "pending_operation_dead_letters_requeued_count": len(
                requeued_dead_letters
            ),
            "pending_operation_dead_letters": pending_dead_letters[:limit],
            "pending_operations_count": len(pending_operations),
            "pending_operation_types": sorted(
                {
                    str(operation.get("operation"))
                    for operation in pending_operations
                    if operation.get("operation")
                }
            ),
            "pending_operations": _pending_operation_summaries(
                pending_operations,
                limit=limit,
            ),
        },
        "counts": _summary_counts(
            bundle=bundle,
            decisions=decisions,
            runtime_validations=runtime_validations,
            runtime_fit_evidence=runtime_fit_evidence,
            rollout_events=rollout_events,
            package_imports=package_imports,
            package_promotions=package_promotions,
            rollout_plans=rollout_plans,
            ingested_evidence=ingested_evidence,
        ),
        "trust": _trust_summary(
            package_imports=package_imports,
            packages=_as_list(bundle.get("packages")),
            package_promotions=package_promotions,
            runtime_validations=runtime_validations,
            signed_package_ids=signed_package_ids,
        ),
        "active_slots": active_slots,
        "package_promotions": package_promotions,
        "rollout_plans": rollout_plans[:limit],
        "ingested_evidence": ingested_evidence,
        "approvals": rollout_approvals,
        "decisions": decision_summaries,
        "fallbacks": _fallback_summaries(
            decisions,
            models_by_id,
            signed_package_ids,
            limit=limit,
        ),
        "operator_overrides": [
            decision
            for decision in decision_summaries
            if decision.get("trigger_type") == "operator"
        ],
        "timeline": _mission_timeline(bundle, decisions, limit=limit),
    }
    return summary


def build_mission_replay(
    bundle: dict[str, Any],
    limit: int = 50,
) -> dict[str, Any]:
    """Build a chronological mission replay from portable evidence JSON."""
    limit = max(limit, 0)
    summary = summarize_evidence_bundle(bundle, limit=limit)
    raw_events = _raw_mission_timeline(bundle, limit=limit)
    events = [
        _mission_replay_event(entry, sequence=index + 1) for index, entry in enumerate(raw_events)
    ]
    phases = _mission_replay_phases(summary)
    incomplete = [
        phase["phase"] for phase in phases if phase["status"] in {"missing", "preview_only"}
    ]
    return {
        "schema_version": "temms-mission-replay/v1",
        "source_schema_version": bundle.get("schema_version"),
        "exported_at": bundle.get("exported_at"),
        "integrity": bundle.get("integrity", {}),
        "headline": summary["headline"],
        "outcome": {
            "offline_mode": summary["runtime"]["offline_mode"],
            "deployment_state": summary["runtime"]["deployment_state"],
            "active_slots": summary["active_slots"],
            "trust": summary["trust"],
            "counts": summary["counts"],
            "completed_phases": sum(1 for phase in phases if phase["status"] == "complete"),
            "incomplete_phases": incomplete,
        },
        "phases": phases,
        "incidents": {
            "approvals": summary["approvals"],
            "fallbacks": summary["fallbacks"],
            "operator_overrides": summary["operator_overrides"],
        },
        "events": events,
        "summary": summary,
    }


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _summary_deployment_state(bundle: dict[str, Any]) -> dict[str, Any] | None:
    state = bundle.get("deployment_state")
    if not isinstance(state, dict):
        state = _as_dict(bundle.get("runtime")).get("deployment_state")
    if not isinstance(state, dict):
        return None
    return {
        "state": state.get("state"),
        "reason": state.get("reason"),
        "updated_at": state.get("updated_at"),
    }


def _pending_operation_summaries(
    pending_operations: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    return [
        _pending_operation_summary(operation)
        for operation in pending_operations[:limit]
    ]


def _runtime_fit_summaries(
    runtime_fit_evidence: list[dict[str, Any]],
    *,
    limit: int,
    active_slots: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    summaries = [_runtime_fit_summary(evidence) for evidence in runtime_fit_evidence]
    summaries.sort(
        key=lambda summary: _runtime_fit_summary_rank(summary, active_slots or []),
        reverse=True,
    )
    return summaries[:limit]


def _runtime_fit_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    runtime_fit = _as_dict(evidence.get("runtime_fit"))
    selection = _as_dict(evidence.get("selection"))
    target_selection = _as_dict(runtime_fit.get("target_selection"))
    runtime_decision = _as_dict(evidence.get("runtime_decision"))
    decision_target = _as_dict(runtime_decision.get("target_selection"))
    edge_contract = _as_dict(evidence.get("edge_execution_contract"))
    contract_target = _as_dict(edge_contract.get("target_selection"))
    contract_path = _as_dict(edge_contract.get("path"))
    runtime_workbench = _as_dict(evidence.get("runtime_workbench"))
    workbench_selection = _as_dict(runtime_workbench.get("target_selection"))
    workbench_summary = _as_dict(runtime_workbench.get("summary"))
    optimizer_gate = _as_dict(evidence.get("runtime_optimizer_gate"))
    edge_mission = _as_dict(evidence.get("edge_runtime_mission"))
    mission_metrics = _as_dict(edge_mission.get("metrics"))
    ddil_repair = _as_dict(mission_metrics.get("ddil_repair"))
    summary = {
        "schema_version": "temms-runtime-fit-evidence-summary/v1",
        "checked_at": evidence.get("checked_at"),
        "readiness_status": evidence.get("readiness_status"),
        "readiness_headline": evidence.get("readiness_headline"),
        "package_id": selection.get("package_id") or runtime_fit.get("package_id"),
        "model_id": selection.get("model_id") or runtime_fit.get("model_id"),
        "device_id": selection.get("device_id") or runtime_fit.get("device_id"),
        "runtime_target_id": selection.get("runtime_target_id")
        or runtime_fit.get("runtime_target_id"),
        "slot": selection.get("slot"),
        "rollout_id": selection.get("rollout_id"),
        "score": runtime_fit.get("score"),
        "tier": runtime_fit.get("tier"),
        "detail": runtime_fit.get("detail"),
        "components": _runtime_fit_component_summary(runtime_fit),
        "target_selection_status": target_selection.get("status"),
        "selected_rank": target_selection.get("selected_rank"),
        "selected_score": target_selection.get("selected_score"),
        "best_runtime_target_id": target_selection.get("best_runtime_target_id"),
        "best_score": target_selection.get("best_score"),
        "score_delta": target_selection.get("score_delta"),
        "runtime_optimizer_status": optimizer_gate.get("status"),
        "runtime_optimizer_state": optimizer_gate.get("state"),
        "runtime_optimizer_detail": optimizer_gate.get("detail"),
        "runtime_decision_status": decision_target.get("status")
        or runtime_decision.get("readiness_status"),
        "runtime_decision_action": runtime_decision.get("recommended_action"),
        "runtime_decision_detail": runtime_decision.get("detail"),
        "edge_execution_contract_status": edge_contract.get("status"),
        "edge_execution_contract_action": edge_contract.get("recommended_action"),
        "edge_execution_contract_path": contract_path.get("label"),
        "edge_execution_contract_best_runtime_target_id": contract_target.get(
            "best_runtime_target_id"
        ),
        "runtime_workbench_schema_version": runtime_workbench.get("schema_version"),
        "runtime_workbench_status": runtime_workbench.get("status"),
        "runtime_workbench_action": runtime_workbench.get("recommended_action"),
        "runtime_workbench_selected_runtime_target_id": runtime_workbench.get(
            "selected_runtime_target_id"
        ),
        "runtime_workbench_best_runtime_target_id": runtime_workbench.get(
            "best_runtime_target_id"
        ),
        "runtime_workbench_target_selection_status": workbench_selection.get("status"),
        "runtime_workbench_target_count": workbench_summary.get("target_count"),
        "runtime_workbench_eligible_target_count": workbench_summary.get(
            "eligible_target_count"
        ),
        "runtime_workbench_blocked_target_count": workbench_summary.get(
            "blocked_target_count"
        ),
        "runtime_workbench_selected_is_best": workbench_summary.get(
            "selected_is_best"
        ),
        "edge_runtime_mission_status": edge_mission.get("status"),
        "edge_runtime_mission_headline": edge_mission.get("headline"),
        "edge_runtime_mission_path": _as_dict(edge_mission.get("path")).get("label"),
        "ddil_repair_state": ddil_repair.get("state"),
        "ddil_repair_detail": ddil_repair.get("detail"),
        **_runtime_fit_lane_fields(runtime_fit),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _runtime_fit_lane_fields(runtime_fit: dict[str, Any]) -> dict[str, Any]:
    runtime_lane = _as_dict(runtime_fit.get("runtime_lane"))
    artifact_lane = _as_dict(runtime_fit.get("artifact_lane"))
    return {
        "runtime_lane_id": runtime_lane.get("lane_id"),
        "runtime_lane_label": runtime_lane.get("label"),
        "runtime_lane_engine": runtime_lane.get("execution_engine"),
        "runtime_lane_acceleration": runtime_lane.get("acceleration"),
        "artifact_lane_status": artifact_lane.get("status"),
        "artifact_lane_state": artifact_lane.get("state"),
        "artifact_lane_detail": artifact_lane.get("detail"),
        "artifact_format": artifact_lane.get("model_format"),
    }


def _runtime_fit_component_summary(runtime_fit: dict[str, Any]) -> dict[str, Any]:
    components = _as_dict(runtime_fit.get("components"))
    return {
        name: {
            key: value
            for key, value in {
                "score": _as_dict(component).get("score"),
                "max_score": _as_dict(component).get("max_score"),
                "state": _as_dict(component).get("state"),
                "status": _as_dict(component).get("status"),
            }.items()
            if value is not None
        }
        for name, component in components.items()
        if isinstance(component, dict)
    }


def _dedupe_runtime_fit_evidence(
    runtime_fit_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_identity: dict[tuple[str, ...], dict[str, Any]] = {}
    for evidence in runtime_fit_evidence:
        summary = _runtime_fit_summary(evidence)
        identity = _runtime_fit_summary_identity(summary)
        existing = by_identity.get(identity)
        if existing is None or _runtime_fit_evidence_freshness_key(evidence) > (
            _runtime_fit_evidence_freshness_key(existing)
        ):
            by_identity[identity] = evidence
    records = list(by_identity.values())
    records.sort(key=_runtime_fit_evidence_freshness_key, reverse=True)
    return records


def _runtime_fit_summary_identity(summary: dict[str, Any]) -> tuple[str, ...]:
    rollout_id = str(summary.get("rollout_id") or "")
    if rollout_id:
        return ("rollout", rollout_id)
    return (
        "context",
        str(summary.get("package_id") or ""),
        str(summary.get("model_id") or ""),
        str(summary.get("device_id") or ""),
        str(summary.get("runtime_target_id") or ""),
        str(summary.get("slot") or ""),
    )


def _runtime_fit_evidence_freshness_key(evidence: dict[str, Any]) -> tuple[str, float]:
    summary = _runtime_fit_summary(evidence)
    return (
        str(summary.get("checked_at") or evidence.get("checked_at") or ""),
        _runtime_fit_score(summary),
    )


def _runtime_fit_summary_rank(
    summary: dict[str, Any],
    active_slots: list[dict[str, Any]],
) -> tuple[int, int, int, float, str]:
    active_by_slot = {
        str(slot.get("slot")): str(slot.get("active_model"))
        for slot in active_slots
        if isinstance(slot, dict) and slot.get("slot") and slot.get("active_model")
    }
    active_models = {
        str(slot.get("active_model"))
        for slot in active_slots
        if isinstance(slot, dict) and slot.get("active_model")
    }
    slot = str(summary.get("slot") or "")
    model = str(summary.get("model_id") or "")
    active_rank = 0
    if slot and model and active_by_slot.get(slot) == model:
        active_rank = 3
    elif model and model in active_models:
        active_rank = 2
    elif slot and slot in active_by_slot:
        active_rank = 1

    selection_status = str(summary.get("target_selection_status") or "")
    target_rank = 1 if selection_status == "best" else 0
    tier_rank = {
        "optimal": 3,
        "ready": 2,
        "degraded": 1,
    }.get(str(summary.get("tier") or ""), 0)
    return (
        active_rank,
        target_rank,
        tier_rank,
        _runtime_fit_score(summary),
        str(summary.get("checked_at") or ""),
    )


def _runtime_fit_score(summary: dict[str, Any]) -> float:
    score = summary.get("score")
    if isinstance(score, bool):
        return 0.0
    if isinstance(score, (int, float)):
        return float(score)
    try:
        return float(str(score))
    except (TypeError, ValueError):
        return 0.0


def _pending_operation_summary(operation: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(operation.get("payload"))
    request = _as_dict(payload.get("request"))
    signature = _as_dict(operation.get("signature"))
    verification = _as_dict(operation.get("verification"))
    preflight = _as_dict(operation.get("preflight"))
    retarget = _latest_runtime_retarget_record(payload)
    operation_name = str(operation.get("operation") or "operation")
    summary = {
        "operation": operation_name,
        "recorded_at": operation.get("recorded_at"),
        "actor": _first_string([payload, request], ("actor",)),
        "slot": _first_string([payload, request], ("slot", "slot_name")),
        "device_id": _first_string([payload, request], ("device_id",)),
        "package_id": _first_string([payload, request], ("package_id",)),
        "model_id": _first_string([payload, request], ("model_id", "model")),
        "runtime_target_id": _first_string([payload, request], ("runtime_target_id",)),
        "applied_locally": payload.get("applied_locally") if "applied_locally" in payload else None,
        "payload_sha256": _canonical_hash(payload),
        "signature_present": bool(signature),
        "signature_schema_version": signature.get("schema_version"),
        "signature_algorithm": signature.get("algorithm"),
        "signature_signer": signature.get("signer"),
        "signature_signed_at": signature.get("signed_at"),
        "signature_key_fingerprint": signature.get("key_fingerprint"),
        "signature_payload_sha256": signature.get("payload_sha256"),
        "signature_status": verification.get("status"),
        "signature_verified": verification.get("verified"),
        "signature_verification_reason": verification.get("reason"),
        "replay_status": preflight.get("replay_status"),
        "replay_ready": preflight.get("ready"),
        "replay_reason": preflight.get("reason"),
        "resolved_model_id": preflight.get("resolved_model_id"),
        "superseded": preflight.get("superseded"),
        "superseded_by_index": preflight.get("superseded_by_index"),
        "superseded_by_model_id": preflight.get("superseded_by_model_id"),
        "final_for_slot": preflight.get("final_for_slot"),
        "runtime_retargeted_at": retarget.get("retargeted_at"),
        "runtime_retargeted_by": retarget.get("actor"),
        "runtime_retarget_reason": retarget.get("reason"),
        "runtime_retargeted_from": retarget.get("previous_runtime_target_id"),
        "runtime_retargeted_to": retarget.get("runtime_target_id"),
        **_runtime_retarget_proof_fields(retarget),
        **_pending_preflight_runtime_proof_fields(preflight),
    }
    summary["summary"] = _pending_operation_label(summary)
    return {key: value for key, value in summary.items() if value is not None}


def _latest_runtime_retarget_record(payload: dict[str, Any]) -> dict[str, Any]:
    records = _as_list(payload.get("_temms_runtime_retarget"))
    if not records:
        return {}
    latest = records[-1]
    return _as_dict(latest)


def _pending_preflight_runtime_proof_fields(preflight: dict[str, Any]) -> dict[str, Any]:
    optimizer_gates = [
        _as_dict(gate)
        for gate in (
            _as_list(preflight.get("hub_optimization_gates"))
            + _as_list(preflight.get("hub_blocking_gates"))
        )
        if isinstance(gate, dict) and gate.get("gate_id") == "runtime_optimizer"
    ]
    optimizer_gate = optimizer_gates[0] if optimizer_gates else {}
    optimizer_refs = _as_dict(optimizer_gate.get("refs"))
    remediation_fields = _runtime_remediation_action_fields(optimizer_gate)
    return {
        "runtime_optimizer_status": optimizer_gate.get("status"),
        "runtime_optimizer_state": optimizer_gate.get("state"),
        "runtime_optimizer_detail": optimizer_gate.get("detail"),
        "runtime_fit_score": preflight.get("hub_runtime_fit_score"),
        "runtime_fit_tier": preflight.get("hub_runtime_fit_tier"),
        "runtime_fit_detail": preflight.get("hub_runtime_fit_detail"),
        "runtime_lane_id": preflight.get("hub_runtime_lane_id"),
        "runtime_lane_label": preflight.get("hub_runtime_lane_label"),
        "runtime_lane_engine": preflight.get("hub_runtime_lane_engine"),
        "runtime_lane_acceleration": preflight.get("hub_runtime_lane_acceleration"),
        "artifact_lane_status": preflight.get("hub_artifact_lane_status"),
        "artifact_lane_state": preflight.get("hub_artifact_lane_state"),
        "artifact_lane_detail": preflight.get("hub_artifact_lane_detail"),
        "artifact_format": preflight.get("hub_artifact_format"),
        "target_selection_status": preflight.get("hub_target_selection_status"),
        "best_runtime_target_id": preflight.get("hub_best_runtime_target_id")
        or optimizer_refs.get("best_runtime_target_id"),
        "runtime_score_delta": preflight.get("hub_runtime_score_delta")
        or optimizer_refs.get("score_delta"),
        "production_admission_status": preflight.get("hub_production_admission_status"),
        "production_apply_allowed": preflight.get("hub_production_apply_allowed"),
        "runtime_capability_lock_status": preflight.get("hub_capability_lock_status"),
        "runtime_capability_sha256": preflight.get("hub_capability_sha256"),
        "runtime_capability_runtime_target_id": preflight.get(
            "hub_capability_runtime_target_id"
        ),
        "runtime_capability_runtime_mode": preflight.get("hub_capability_runtime_mode"),
        "runtime_capability_edge_profile": preflight.get("hub_capability_edge_profile"),
        "runtime_capability_telemetry_status": preflight.get(
            "hub_capability_telemetry_status"
        ),
        "runtime_capability_telemetry_state": preflight.get(
            "hub_capability_telemetry_state"
        ),
        "runtime_capability_telemetry_detail": preflight.get(
            "hub_capability_telemetry_detail"
        ),
        "runtime_capability_heartbeat_age_seconds": preflight.get(
            "hub_capability_heartbeat_age_seconds"
        ),
        "runtime_capability_heartbeat_stale_after_seconds": preflight.get(
            "hub_capability_heartbeat_stale_after_seconds"
        ),
        "runtime_capability_failures": preflight.get("hub_capability_failures"),
        "edge_execution_contract_status": preflight.get(
            "hub_edge_execution_contract_status"
        ),
        "edge_execution_contract_action": preflight.get(
            "hub_edge_execution_contract_action"
        ),
        "edge_execution_contract_path": preflight.get(
            "hub_edge_execution_contract_path"
        ),
        "runtime_workbench_schema_version": preflight.get(
            "hub_runtime_workbench_schema_version"
        ),
        "runtime_workbench_status": preflight.get("hub_runtime_workbench_status"),
        "runtime_workbench_action": preflight.get("hub_runtime_workbench_action"),
        "runtime_workbench_selected_runtime_target_id": preflight.get(
            "hub_runtime_workbench_selected_runtime_target_id"
        ),
        "runtime_workbench_best_runtime_target_id": preflight.get(
            "hub_runtime_workbench_best_runtime_target_id"
        ),
        "runtime_workbench_target_selection_status": preflight.get(
            "hub_runtime_workbench_target_selection_status"
        ),
        "runtime_workbench_target_count": preflight.get(
            "hub_runtime_workbench_target_count"
        ),
        "runtime_workbench_eligible_target_count": preflight.get(
            "hub_runtime_workbench_eligible_target_count"
        ),
        "runtime_workbench_blocked_target_count": preflight.get(
            "hub_runtime_workbench_blocked_target_count"
        ),
        "runtime_workbench_selected_is_best": preflight.get(
            "hub_runtime_workbench_selected_is_best"
        ),
        "runtime_retarget_replay_proof_status": preflight.get(
            "hub_runtime_retarget_proof_status"
        ),
        "runtime_retarget_replay_signed_capability_sha256": preflight.get(
            "hub_runtime_retarget_proof_signed_capability_sha256"
        ),
        "runtime_retarget_replay_current_capability_sha256": preflight.get(
            "hub_runtime_retarget_proof_current_capability_sha256"
        ),
        "runtime_retarget_replay_signed_validation_id": preflight.get(
            "hub_runtime_retarget_proof_signed_validation_id"
        ),
        "runtime_retarget_replay_current_validation_id": preflight.get(
            "hub_runtime_retarget_proof_current_validation_id"
        ),
        "runtime_retarget_replay_signed_benchmark_id": preflight.get(
            "hub_runtime_retarget_proof_signed_benchmark_id"
        ),
        "runtime_retarget_replay_current_benchmark_id": preflight.get(
            "hub_runtime_retarget_proof_current_benchmark_id"
        ),
        "runtime_retarget_replay_signed_target_assessment_sha256": preflight.get(
            "hub_runtime_retarget_proof_signed_target_assessment_sha256"
        ),
        "runtime_retarget_replay_current_target_assessment_sha256": preflight.get(
            "hub_runtime_retarget_proof_current_target_assessment_sha256"
        ),
        **remediation_fields,
        **_target_assessment_remediation_fields(preflight, remediation_fields),
    }


def _runtime_remediation_action_fields(gate: dict[str, Any]) -> dict[str, Any]:
    actions = [
        _as_dict(action)
        for action in _as_list(gate.get("actions"))
        if isinstance(action, dict)
    ]
    action = next(
        (
            candidate
            for candidate in actions
            if candidate.get("kind") == "select_runtime_target"
        ),
        actions[0] if actions else {},
    )
    refs = _as_dict(action.get("refs"))
    fields = {
        "runtime_remediation_action_id": action.get("action_id"),
        "runtime_remediation_label": action.get("label"),
        "runtime_remediation_kind": action.get("kind"),
        "runtime_remediation_runtime_target_id": refs.get("runtime_target_id"),
        "runtime_remediation_previous_runtime_target_id": refs.get(
            "previous_runtime_target_id"
        ),
        "runtime_remediation_best_runtime_target_id": refs.get("best_runtime_target_id"),
        "runtime_remediation_score_delta": refs.get("score_delta"),
    }
    fields.update(
        _runtime_command_object_fields(
            action.get("command"),
            prefix="runtime_remediation_action_command",
        )
    )
    return fields


def _target_assessment_remediation_fields(
    preflight: dict[str, Any],
    remediation_fields: dict[str, Any],
) -> dict[str, Any]:
    assessment = _target_assessment_for_remediation(preflight, remediation_fields)
    if not assessment:
        return {}
    remediation = _as_dict(assessment.get("remediation"))
    command = _target_remediation_command_summary(remediation)
    return _without_empty_runtime_fields(
        {
            "runtime_remediation_contract_runtime_target_id": assessment.get(
                "runtime_target_id"
            ),
            "runtime_remediation_contract_action": remediation.get("action"),
            "runtime_remediation_contract_label": remediation.get("label"),
            "runtime_remediation_contract_kind": command.get("kind"),
            "runtime_remediation_contract_command_text": command.get("command_text"),
            "runtime_remediation_contract_command_note": command.get("note"),
            "runtime_remediation_contract_requires_edge_execution": command.get(
                "requires_edge_execution"
            ),
        }
    )


def _target_assessment_for_remediation(
    preflight: dict[str, Any],
    remediation_fields: dict[str, Any],
) -> dict[str, Any]:
    assessments = [
        _as_dict(assessment)
        for assessment in _as_list(preflight.get("hub_target_assessments"))
        if isinstance(assessment, dict)
    ]
    if not assessments:
        return {}
    preferred_ids = [
        remediation_fields.get("runtime_remediation_runtime_target_id"),
        preflight.get("hub_best_runtime_target_id"),
        preflight.get("runtime_target_id"),
        preflight.get("hub_capability_runtime_target_id"),
    ]
    for runtime_target_id in preferred_ids:
        target_id = str(runtime_target_id or "")
        if not target_id:
            continue
        match = next(
            (
                assessment
                for assessment in assessments
                if str(assessment.get("runtime_target_id") or "") == target_id
            ),
            None,
        )
        if match is not None:
            return match
    for key in ("best", "selected"):
        match = next(
            (assessment for assessment in assessments if assessment.get(key) is True),
            None,
        )
        if match is not None:
            return match
    return assessments[0]


def _target_remediation_command_summary(remediation: dict[str, Any]) -> dict[str, Any]:
    command = _as_dict(remediation.get("command"))
    requires_edge_execution = _runtime_bool(
        remediation.get("requires_edge_execution"),
        command.get("requires_edge_execution"),
    )
    edge_text = (
        _runtime_string(remediation.get("edge_command_text"))
        or _runtime_command_text(remediation.get("edge_command"))
        or _runtime_string(command.get("edge_command_text"))
        or _runtime_command_text(command.get("edge_command"))
    )
    if edge_text:
        return _without_empty_runtime_fields(
            {
                "kind": "edge",
                "command_text": edge_text,
                "note": remediation.get("edge_command_note")
                or command.get("edge_command_note"),
                "requires_edge_execution": True
                if requires_edge_execution is None
                else requires_edge_execution,
            }
        )

    operator_text = (
        _runtime_string(remediation.get("operator_command_text"))
        or _runtime_command_text(remediation.get("operator_command"))
        or _runtime_string(command.get("operator_command_text"))
        or _runtime_command_text(command.get("operator_command"))
    )
    if operator_text:
        return _without_empty_runtime_fields(
            {
                "kind": "operator",
                "command_text": operator_text,
                "note": remediation.get("operator_command_note")
                or command.get("operator_command_note"),
                "requires_edge_execution": False
                if requires_edge_execution is None
                else requires_edge_execution,
            }
        )

    command_fields = _runtime_command_object_fields(
        command,
        prefix="runtime_remediation_contract_command",
    )
    if command_fields:
        method = command_fields.get("runtime_remediation_contract_command_method")
        path = command_fields.get("runtime_remediation_contract_command_path")
        return _without_empty_runtime_fields(
            {
                "kind": "http",
                "command_text": " ".join(str(item) for item in (method, path) if item),
                "requires_edge_execution": command_fields.get(
                    "runtime_remediation_contract_command_requires_edge_execution"
                ),
            }
        )

    return {}


def _runtime_command_object_fields(command: Any, *, prefix: str) -> dict[str, Any]:
    command_dict = _as_dict(command)
    if not command_dict:
        return {}
    return _without_empty_runtime_fields(
        {
            f"{prefix}_method": command_dict.get("method"),
            f"{prefix}_path": command_dict.get("path"),
            f"{prefix}_requires_edge_execution": command_dict.get(
                "requires_edge_execution"
            ),
            f"{prefix}_edge_command_text": command_dict.get("edge_command_text")
            or _runtime_command_text(command_dict.get("edge_command")),
            f"{prefix}_edge_command_note": command_dict.get("edge_command_note"),
            f"{prefix}_operator_command_text": command_dict.get(
                "operator_command_text"
            )
            or _runtime_command_text(command_dict.get("operator_command")),
            f"{prefix}_operator_command_note": command_dict.get(
                "operator_command_note"
            ),
        }
    )


def _runtime_command_text(command: Any) -> str | None:
    if isinstance(command, str):
        return command or None
    if isinstance(command, list):
        parts = [str(part) for part in command if part not in (None, "")]
        return shlex.join(parts) if parts else None
    return None


def _runtime_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _runtime_bool(*values: Any) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            return value
    return None


def _without_empty_runtime_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if value is not None and value != ""
    }


def _first_string(records: list[dict[str, Any]], keys: tuple[str, ...]) -> str | None:
    for record in records:
        for key in keys:
            value = record.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _pending_operation_label(summary: dict[str, Any]) -> str:
    operation = str(summary.get("operation") or "operation")
    model = summary.get("model_id")
    package = summary.get("package_id")
    device = summary.get("device_id")
    slot = summary.get("slot")
    if operation == "deploy":
        target = device or slot or "edge target"
        artifact = model or package or "deployment"
        return f"{artifact} to {target}"
    if operation == "override_model":
        target = slot or "slot"
        artifact = model or "model"
        return f"override {target} to {artifact}"
    if operation == "update_conditions":
        return "sync condition updates"
    return operation.replace("_", " ")


def _mission_headline(
    decisions: list[dict[str, Any]],
    runtime: dict[str, Any],
    deployment_state: dict[str, Any] | None,
    approvals: list[dict[str, Any]] | None = None,
) -> str:
    triggers = {
        str(decision.get("trigger_type")) for decision in decisions if decision.get("trigger_type")
    }
    parts: list[str] = []
    if approvals:
        parts.append("approval gate")
    if "rollout" in triggers:
        parts.append("rollout applied")
    if "policy" in triggers:
        parts.append("policy-adaptive switching")
    if "fallback" in triggers:
        parts.append("fallback recovery")
    if "rollback" in triggers:
        parts.append("rollback")
    if "operator" in triggers:
        parts.append("operator override")
    if runtime.get("offline_mode") or (deployment_state or {}).get("state") == "OFFLINE":
        parts.append("offline operation")
    return ", ".join(parts) if parts else "evidence captured"


def _summary_counts(
    *,
    bundle: dict[str, Any],
    decisions: list[dict[str, Any]],
    runtime_validations: list[dict[str, Any]],
    rollout_events: list[dict[str, Any]],
    package_imports: list[dict[str, Any]],
    package_promotions: list[dict[str, Any]],
    rollout_plans: list[dict[str, Any]],
    runtime_fit_evidence: list[dict[str, Any]],
    ingested_evidence: list[dict[str, Any]],
) -> dict[str, int]:
    telemetry = bundle.get("telemetry")
    telemetry_events = _as_list(_as_dict(telemetry).get("events"))
    telemetry_count = (
        int(telemetry["count"])
        if isinstance(telemetry, dict) and isinstance(telemetry.get("count"), int)
        else len(telemetry_events)
    )
    return {
        "packages": len(_as_list(bundle.get("packages"))),
        "models": len(_as_list(bundle.get("models"))),
        "decisions": len(decisions),
        "rollout_events": len(rollout_events),
        "runtime_validations": len(runtime_validations),
        "runtime_fit_evidence": len(runtime_fit_evidence),
        "package_imports": len(package_imports),
        "package_promotions": len(package_promotions),
        "rollout_plans": len(rollout_plans),
        "ingested_evidence_bundles": len(ingested_evidence),
        "hub_benchmarks": len(_as_list(bundle.get("hub_benchmarks"))),
        "telemetry_events": telemetry_count,
        "timeline_entries": len(_as_list(bundle.get("timeline"))),
    }


def _trust_summary(
    *,
    package_imports: list[dict[str, Any]],
    packages: list[Any],
    package_promotions: list[dict[str, Any]],
    runtime_validations: list[dict[str, Any]],
    signed_package_ids: set[str] | None = None,
) -> dict[str, Any]:
    package_ids = sorted(
        {
            str(package_id)
            for package_id in (
                [event.get("package_id") for event in package_imports]
                + [_as_dict(package).get("id") for package in packages]
                + [event.get("package_id") for event in package_promotions]
            )
            if package_id
        }
    )
    signed_package_ids = signed_package_ids or _signed_package_ids(package_imports)
    released_package_ids = sorted(
        {
            str(_as_dict(package).get("package_id") or _as_dict(package).get("id"))
            for package in packages
            if _as_dict(_as_dict(package).get("promotion")).get("state") == "released"
            and (_as_dict(package).get("package_id") or _as_dict(package).get("id"))
        }
        | {
            str(event.get("package_id"))
            for event in package_promotions
            if event.get("package_id") and event.get("state") == "released"
        }
    )
    retired_package_ids = sorted(
        {
            str(_as_dict(package).get("package_id") or _as_dict(package).get("id"))
            for package in packages
            if _as_dict(_as_dict(package).get("promotion")).get("state") == "retired"
            and (_as_dict(package).get("package_id") or _as_dict(package).get("id"))
        }
        | {
            str(event.get("package_id"))
            for event in package_promotions
            if event.get("package_id") and event.get("state") == "retired"
        }
    )
    validation_package_ids = sorted(
        {
            str(validation.get("package_id"))
            for validation in runtime_validations
            if validation.get("package_id") and _as_dict(validation.get("result")).get("ok") is True
        }
    )
    passed_non_dry_run = [
        validation
        for validation in runtime_validations
        if _as_dict(validation.get("result")).get("ok") is True
        and _as_dict(validation.get("result")).get("dry_run") is False
    ]
    return {
        "package_ids": package_ids,
        "signed_package_imports": len(signed_package_ids),
        "signed_package_ids": sorted(signed_package_ids),
        "released_packages": len(released_package_ids),
        "released_package_ids": released_package_ids,
        "retired_package_ids": retired_package_ids,
        "runtime_validations_passed": sum(
            1
            for validation in runtime_validations
            if _as_dict(validation.get("result")).get("ok") is True
        ),
        "runtime_validations_non_dry_run": sum(
            1
            for validation in runtime_validations
            if _as_dict(validation.get("result")).get("dry_run") is False
        ),
        "runtime_validations_passed_non_dry_run": len(passed_non_dry_run),
        "local_runtime_validations": sum(
            1 for validation in runtime_validations if _is_local_runtime_validation(validation)
        ),
        "validated_package_ids": validation_package_ids,
    }


def _signed_package_ids(package_imports: list[dict[str, Any]]) -> set[str]:
    return {
        str(event.get("package_id"))
        for event in package_imports
        if event.get("package_id") and event.get("signature_verified") is True
    }


def _signed_catalog_package_ids(bundle: dict[str, Any]) -> set[str]:
    packages: list[Any] = list(_as_list(bundle.get("packages")))
    hub_packages = _as_dict(_as_dict(bundle.get("hub_lite")).get("packages"))
    packages.extend(hub_packages.values())

    signed_ids: set[str] = set()
    for package in packages:
        package_data = _as_dict(package)
        manifest = _as_dict(package_data.get("manifest"))
        metadata = _as_dict(package_data.get("metadata"))
        validation = _as_dict(metadata.get("validation"))
        manifest_validation = _as_dict(manifest.get("validation"))
        import_audit = _as_dict(manifest.get("_temms_import"))
        package_id = (
            package_data.get("package_id")
            or package_data.get("id")
            or manifest.get("package_id")
        )
        signature_verified = (
            package_data.get("signature_verified") is True
            or metadata.get("signature_verified") is True
            or validation.get("signature_verified") is True
            or manifest.get("signature_verified") is True
            or manifest_validation.get("signature_verified") is True
            or import_audit.get("signature_verified") is True
        )
        if package_id and signature_verified:
            signed_ids.add(str(package_id))
    return signed_ids


def _is_local_runtime_validation(validation: dict[str, Any]) -> bool:
    result = _as_dict(validation.get("result"))
    validation_payload = _as_dict(result.get("validation"))
    return validation_payload.get(
        "schema_version"
    ) == "temms-local-runtime-validation/v1" or "temms-local-runtime-validation/v1" in str(
        result.get("stdout", "")
    )


def _package_trust_records(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    imports = [
        event for event in _as_list(bundle.get("package_imports")) if isinstance(event, dict)
    ]
    if imports:
        return imports

    records: list[dict[str, Any]] = []
    for package in _as_list(bundle.get("packages")):
        package_data = _as_dict(package)
        manifest = _as_dict(package_data.get("manifest"))
        import_audit = _as_dict(manifest.get("_temms_import"))
        signature_verified = import_audit.get("signature_verified")
        if signature_verified is None:
            signature_verified = manifest.get("signature_verified")
        records.append(
            {
                "package_id": package_data.get("id") or manifest.get("package_id"),
                "name": package_data.get("name") or manifest.get("name"),
                "version": package_data.get("version") or manifest.get("version"),
                "signature_verified": signature_verified,
                "signature_required": import_audit.get("signature_required"),
                "imported_at": package_data.get("imported_at"),
                "signature": _as_dict(import_audit.get("signature")),
            }
        )
    return records


def _package_promotion_summaries(
    bundle: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    promotions = [
        event for event in _as_list(bundle.get("package_promotions")) if isinstance(event, dict)
    ]
    if not promotions:
        hub_lite = _as_dict(bundle.get("hub_lite"))
        packages = _as_dict(hub_lite.get("packages"))
        for package in packages.values():
            package_data = _as_dict(package)
            promotion = _as_dict(package_data.get("promotion"))
            for event in _as_list(promotion.get("history")):
                if not isinstance(event, dict):
                    continue
                promotions.append(
                    {
                        "package_id": package_data.get("package_id"),
                        "state": event.get("state"),
                        "from_state": event.get("from_state"),
                        "actor": event.get("actor"),
                        "reason": event.get("reason"),
                        "evidence": _as_dict(event.get("evidence")),
                        "updated_at": event.get("updated_at"),
                    }
                )
    promotions.sort(key=lambda event: str(event.get("updated_at") or ""), reverse=True)
    return promotions[:limit]


def _ingested_evidence_summaries(
    bundle: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    hub_lite = _as_dict(bundle.get("hub_lite"))
    records = _as_dict(hub_lite.get("evidence_bundles"))
    summaries: list[dict[str, Any]] = []
    for record in records.values():
        if not isinstance(record, dict):
            continue
        summary = _as_dict(record.get("summary"))
        counts = _as_dict(record.get("counts")) or _as_dict(summary.get("counts"))
        summaries.append(
            {
                "evidence_id": record.get("evidence_id"),
                "device_id": record.get("device_id"),
                "actor": record.get("actor") or record.get("last_actor"),
                "headline": record.get("headline") or summary.get("headline"),
                "exported_at": record.get("exported_at"),
                "ingested_at": record.get("ingested_at"),
                "last_ingested_at": record.get("last_ingested_at"),
                "duplicate_ingests": int(record.get("duplicate_ingests") or 0),
                "integrity": _as_dict(record.get("integrity")),
                "counts": counts,
            }
        )
    summaries.sort(
        key=lambda record: str(record.get("last_ingested_at") or record.get("ingested_at") or ""),
        reverse=True,
    )
    return summaries[:limit]


def _models_by_id(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {}
    for model in _as_list(bundle.get("models")):
        model_data = _as_dict(model)
        model_id = model_data.get("id")
        if model_id:
            models[str(model_id)] = model_data
    return models


def _model_label(model_id: Any, models_by_id: dict[str, dict[str, Any]]) -> str | None:
    if model_id is None:
        return None
    model_id_text = str(model_id)
    model = models_by_id.get(model_id_text, {})
    name = model.get("name")
    if name and name != model_id_text:
        return f"{name} ({model_id_text})"
    return model_id_text


def _summarize_decision(
    decision: dict[str, Any],
    models_by_id: dict[str, dict[str, Any]],
    signed_package_ids: set[str] | None = None,
) -> dict[str, Any]:
    audit_metadata = _as_dict(decision.get("audit_metadata"))
    retarget = _decision_runtime_retarget_fields(audit_metadata)
    summary = {
        "timestamp": decision.get("created_at"),
        "slot": decision.get("slot"),
        "from_model": decision.get("from_model"),
        "from_model_label": _model_label(decision.get("from_model"), models_by_id),
        "to_model": decision.get("to_model"),
        "to_model_label": _model_label(decision.get("to_model"), models_by_id),
        "trigger_type": decision.get("trigger_type"),
        "trigger_detail": decision.get("trigger_detail"),
        "package_id": _decision_package_id(decision),
        "signature_verified": _decision_signature_verified(decision, signed_package_ids),
        "summary": _decision_sentence(decision, models_by_id),
        **retarget,
    }
    if audit_metadata.get("benchmark"):
        summary["benchmark"] = audit_metadata.get("benchmark")
    if audit_metadata.get("runtime_constraints"):
        summary["runtime_constraints"] = audit_metadata.get("runtime_constraints")
    return summary


def _decision_runtime_retarget_fields(audit_metadata: dict[str, Any]) -> dict[str, Any]:
    retarget = _as_dict(audit_metadata.get("ddil_runtime_retarget"))
    latest = _as_dict(retarget.get("latest")) or retarget
    previous_target = latest.get("previous_runtime_target_id")
    runtime_target = latest.get("runtime_target_id")
    if not previous_target or not runtime_target:
        return {}
    return {
        "runtime_retargeted": True,
        "runtime_retargeted_at": latest.get("retargeted_at") or retarget.get("retargeted_at"),
        "runtime_retargeted_by": latest.get("actor") or retarget.get("actor"),
        "runtime_retarget_reason": latest.get("reason") or retarget.get("reason"),
        "runtime_retargeted_from": previous_target,
        "runtime_retargeted_to": runtime_target,
        "runtime_retarget_previous_payload_sha256": latest.get("previous_payload_sha256")
        or retarget.get("previous_payload_sha256"),
        **_runtime_retarget_proof_fields(latest),
    }


def _record_runtime_retarget_fields(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("runtime_retargeted"):
        previous_target = record.get("runtime_retargeted_from")
        runtime_target = record.get("runtime_retargeted_to")
        if previous_target and runtime_target:
            return {
                "runtime_retargeted": True,
                "runtime_retargeted_at": record.get("runtime_retargeted_at"),
                "runtime_retargeted_by": record.get("runtime_retargeted_by"),
                "runtime_retarget_reason": record.get("runtime_retarget_reason"),
                "runtime_retargeted_from": previous_target,
                "runtime_retargeted_to": runtime_target,
                "runtime_retarget_previous_payload_sha256": record.get(
                    "runtime_retarget_previous_payload_sha256"
                ),
                "runtime_retarget_proof_status": record.get(
                    "runtime_retarget_proof_status"
                ),
                "runtime_retarget_runtime_fit_score": record.get(
                    "runtime_retarget_runtime_fit_score"
                ),
                "runtime_retarget_best": record.get("runtime_retarget_best"),
                "runtime_retarget_eligible": record.get("runtime_retarget_eligible"),
                "runtime_retarget_capability_lock_status": record.get(
                    "runtime_retarget_capability_lock_status"
                ),
                "runtime_retarget_capability_sha256": record.get(
                    "runtime_retarget_capability_sha256"
                ),
                "runtime_retarget_validation_id": record.get(
                    "runtime_retarget_validation_id"
                ),
                "runtime_retarget_benchmark_id": record.get(
                    "runtime_retarget_benchmark_id"
                ),
            }
    return _decision_runtime_retarget_fields(_as_dict(record.get("audit_metadata")))


def _runtime_retarget_proof_fields(record: dict[str, Any]) -> dict[str, Any]:
    proof = _as_dict(record.get("runtime_target_proof"))
    if not proof:
        return {}
    lock = _as_dict(proof.get("runtime_capability_lock"))
    telemetry = _as_dict(proof.get("telemetry_freshness"))
    lane = _as_dict(proof.get("runtime_lane"))
    artifact = _as_dict(proof.get("artifact_lane"))
    fields = {
        "runtime_retarget_proof_status": proof.get("status"),
        "runtime_retarget_runtime_fit_score": proof.get("runtime_fit_score"),
        "runtime_retarget_best": proof.get("best"),
        "runtime_retarget_eligible": proof.get("eligible"),
        "runtime_retarget_capability_lock_status": lock.get("status"),
        "runtime_retarget_capability_sha256": proof.get("capability_sha256")
        or lock.get("capability_sha256"),
        "runtime_retarget_runtime_mode": lock.get("runtime_mode"),
        "runtime_retarget_runtime_lane_id": lane.get("lane_id"),
        "runtime_retarget_runtime_lane_label": lane.get("label"),
        "runtime_retarget_artifact_lane_state": artifact.get("state"),
        "runtime_retarget_validation_id": proof.get("runtime_validation_id"),
        "runtime_retarget_benchmark_id": proof.get("benchmark_id"),
        "runtime_retarget_target_assessment_sha256": proof.get(
            "target_assessment_sha256"
        ),
        "runtime_retarget_latency_ms_p95": proof.get("latency_ms_p95"),
        "runtime_retarget_throughput_ips": proof.get("throughput_ips"),
        "runtime_retarget_workbench_schema_version": proof.get(
            "runtime_workbench_schema_version"
        ),
        "runtime_retarget_workbench_status": proof.get("runtime_workbench_status"),
        "runtime_retarget_workbench_target_selection_status": proof.get(
            "runtime_workbench_target_selection_status"
        ),
        "runtime_retarget_workbench_previous_selected_runtime_target_id": proof.get(
            "runtime_workbench_previous_selected_runtime_target_id"
        ),
        "runtime_retarget_workbench_selected_runtime_target_id": proof.get(
            "runtime_workbench_selected_runtime_target_id"
        ),
        "runtime_retarget_workbench_best_runtime_target_id": proof.get(
            "runtime_workbench_best_runtime_target_id"
        ),
        "runtime_retarget_workbench_target_count": proof.get(
            "runtime_workbench_target_count"
        ),
        "runtime_retarget_workbench_eligible_target_count": proof.get(
            "runtime_workbench_eligible_target_count"
        ),
        "runtime_retarget_workbench_blocked_target_count": proof.get(
            "runtime_workbench_blocked_target_count"
        ),
        "runtime_retarget_workbench_selected_is_best": proof.get(
            "runtime_workbench_selected_is_best"
        ),
        "runtime_retarget_telemetry_status": telemetry.get("status"),
        "runtime_retarget_telemetry_state": telemetry.get("state"),
        "runtime_retarget_telemetry_detail": telemetry.get("detail"),
        "runtime_retarget_heartbeat_age_seconds": telemetry.get(
            "heartbeat_age_seconds"
        ),
        "runtime_retarget_heartbeat_stale_after_seconds": telemetry.get(
            "heartbeat_stale_after_seconds"
        ),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _decision_package_id(decision: dict[str, Any]) -> str | None:
    audit_metadata = _as_dict(decision.get("audit_metadata"))
    if audit_metadata.get("package_id"):
        return str(audit_metadata["package_id"])
    package = _as_dict(audit_metadata.get("package"))
    if package.get("package_id"):
        return str(package["package_id"])
    model_evidence = _as_dict(decision.get("model_evidence"))
    to_package = _as_dict(model_evidence.get("to_package"))
    package_id = to_package.get("id") or to_package.get("package_id")
    return str(package_id) if package_id else None


def _decision_signature_verified(
    decision: dict[str, Any],
    signed_package_ids: set[str] | None = None,
) -> bool | None:
    audit_metadata = _as_dict(decision.get("audit_metadata"))
    package = _as_dict(audit_metadata.get("package"))
    if package.get("signature_verified") is not None:
        return bool(package.get("signature_verified"))
    model_evidence = _as_dict(decision.get("model_evidence"))
    to_package = _as_dict(model_evidence.get("to_package"))
    manifest = _as_dict(to_package.get("manifest"))
    if manifest.get("signature_verified") is not None:
        return bool(manifest.get("signature_verified"))
    import_audit = _as_dict(manifest.get("_temms_import"))
    if import_audit.get("signature_verified") is not None:
        return bool(import_audit.get("signature_verified"))
    if signed_package_ids is not None:
        package_id = _decision_package_id(decision)
        if package_id in signed_package_ids:
            return True
    return None


def _decision_sentence(
    decision: dict[str, Any],
    models_by_id: dict[str, dict[str, Any]],
) -> str:
    slot = decision.get("slot") or "slot"
    trigger = decision.get("trigger_type") or "decision"
    to_model = _model_label(decision.get("to_model"), models_by_id) or "unknown model"
    from_model = _model_label(decision.get("from_model"), models_by_id)
    detail = decision.get("trigger_detail")
    if trigger == "fallback":
        fallback = _as_dict(_as_dict(decision.get("audit_metadata")).get("fallback"))
        failed = _model_label(fallback.get("selected_model"), models_by_id) or "selected model"
        return f"{slot} recovered to {to_model} after {failed} failed"
    if trigger == "rollback":
        return f"{slot} rolled back to {to_model}"
    if trigger == "operator":
        return f"{slot} held {to_model} by operator override"
    if trigger == "rollout":
        return f"{slot} activated {to_model} from rollout"
    if from_model:
        base = f"{slot} switched from {from_model} to {to_model} by {trigger}"
    else:
        base = f"{slot} activated {to_model} by {trigger}"
    return f"{base}: {detail}" if detail else base


def _fallback_summaries(
    decisions: list[dict[str, Any]],
    models_by_id: dict[str, dict[str, Any]],
    signed_package_ids: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    fallbacks: list[dict[str, Any]] = []
    for decision in decisions:
        fallback = _as_dict(_as_dict(decision.get("audit_metadata")).get("fallback"))
        if decision.get("trigger_type") != "fallback" and not fallback:
            continue
        fallbacks.append(
            {
                "timestamp": decision.get("created_at"),
                "slot": decision.get("slot"),
                "failed_model": fallback.get("selected_model"),
                "failed_model_label": _model_label(fallback.get("selected_model"), models_by_id),
                "activated_model": decision.get("to_model"),
                "activated_model_label": _model_label(decision.get("to_model"), models_by_id),
                "attempted": fallback.get("attempted", []),
                "failures": fallback.get("failures", []),
                "package_id": _decision_package_id(decision),
                "signature_verified": _decision_signature_verified(
                    decision,
                    signed_package_ids,
                ),
                "summary": _decision_sentence(decision, models_by_id),
            }
        )
    return fallbacks[:limit]


def _active_slot_summaries(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for slot in _as_list(bundle.get("slots")):
        slot_data = _as_dict(slot)
        override = _as_dict(slot_data.get("operator_override"))
        slots.append(
            {
                "slot": slot_data.get("name"),
                "state": slot_data.get("state"),
                "active_model": slot_data.get("active_model_id"),
                "default_model": slot_data.get("default_model"),
                "operator_override": bool(override),
                "operator_override_model": override.get("model_id"),
                "operator_override_reason": override.get("reason"),
            }
        )
    return slots


def _mission_timeline(
    bundle: dict[str, Any],
    decisions: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    entries = [entry for entry in _as_list(bundle.get("timeline")) if isinstance(entry, dict)]
    active_slots = _active_slot_summaries(bundle)
    if not entries:
        telemetry = _as_dict(bundle.get("telemetry"))
        entries = combined_timeline(
            decisions,
            _as_list(telemetry.get("events")),
            _as_list(bundle.get("rollout_events")),
            _as_list(bundle.get("runtime_validations")),
            _as_list(bundle.get("hub_benchmarks")),
            _as_list(bundle.get("runtime_fit_evidence")),
            _package_trust_records(bundle),
            _package_promotion_summaries(bundle, limit=limit),
            active_slots=active_slots,
        )
    return [_timeline_summary(entry, active_slots=active_slots) for entry in entries[:limit]]


def _raw_mission_timeline(bundle: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    entries = [entry for entry in _as_list(bundle.get("timeline")) if isinstance(entry, dict)]
    active_slots = _active_slot_summaries(bundle)
    if not entries:
        decisions = [
            _normalize_decision(decision)
            for decision in _as_list(bundle.get("decisions"))
            if isinstance(decision, dict)
        ]
        telemetry = _as_dict(bundle.get("telemetry"))
        entries = combined_timeline(
            decisions,
            _as_list(telemetry.get("events")),
            _as_list(bundle.get("rollout_events")),
            _as_list(bundle.get("runtime_validations")),
            _as_list(bundle.get("hub_benchmarks")),
            _as_list(bundle.get("runtime_fit_evidence")),
            _package_trust_records(bundle),
            _package_promotion_summaries(bundle, limit=limit),
            active_slots=active_slots,
        )

    entries = _sort_timeline_entries(entries, active_slots=active_slots, reverse=False)
    if limit == 0:
        return []
    if len(entries) > limit:
        entries = entries[-limit:]
    return entries


def _mission_replay_event(entry: dict[str, Any], sequence: int) -> dict[str, Any]:
    record = _as_dict(entry.get("record"))
    phase = _replay_phase_for_event(entry, record)
    event = {
        "sequence": sequence,
        "timestamp": entry.get("timestamp"),
        "phase": phase,
        "kind": entry.get("kind"),
        "slot": entry.get("slot") or record.get("slot"),
        "summary": str(entry.get("summary") or entry.get("kind") or "event"),
    }
    if entry.get("active_runtime_proof") is True:
        event["active_runtime_proof"] = True
    detail = _replay_event_detail(entry, record)
    if detail:
        event["detail"] = detail
    actor = record.get("actor")
    if actor:
        event["actor"] = actor
    return event


def _replay_phase_for_event(entry: dict[str, Any], record: dict[str, Any]) -> str:
    kind = entry.get("kind")
    if kind == "package_import":
        return "signed_package"
    if kind == "package_promotion":
        return "package_release"
    if kind == "rollout_plan":
        return "rollout_coordination"
    if kind == "runtime_validation":
        return "runtime_validation"
    if kind == "runtime_fit":
        return "runtime_fit"
    if kind == "benchmark":
        return "performance_evidence"
    if kind == "rollout":
        state = str(record.get("state") or "").lower()
        if state == "approved":
            return "policy_approval"
        if state == "rolled_back":
            return "fallback_rollback"
        return "edge_rollout"
    if kind == "decision":
        if record.get("runtime_retargeted") or _decision_runtime_retarget_fields(
            _as_dict(record.get("audit_metadata"))
        ):
            return "offline_operation"
        trigger = str(record.get("trigger_type") or "").lower()
        if trigger == "fallback" or trigger == "rollback":
            return "fallback_rollback"
        if trigger == "operator":
            return "operator_override"
        if trigger == "policy":
            return "policy_decision"
        if trigger == "rollout":
            return "edge_rollout"
        return "model_switch"
    if kind == "telemetry":
        event_type = str(record.get("event_type") or entry.get("summary") or "").lower()
        if (
            "offline" in event_type
            or "sync" in event_type
            or "replayed" in event_type
            or "pending_operations" in event_type
            or "dead_letter" in event_type
            or "quarantined" in event_type
        ):
            return "offline_operation"
        return "telemetry"
    return "evidence"


def _replay_event_detail(entry: dict[str, Any], record: dict[str, Any]) -> str | None:
    kind = entry.get("kind")
    if kind == "decision":
        retarget = _record_runtime_retarget_fields(record)
        if retarget:
            return (
                f"retargeted {retarget['runtime_retargeted_from']} -> "
                f"{retarget['runtime_retargeted_to']}"
            )
        detail = record.get("trigger_detail")
        trigger = record.get("trigger_type")
        if detail and trigger:
            return f"{trigger}: {detail}"
        return str(detail) if detail else None
    if kind == "rollout":
        detail = record.get("detail")
        if detail:
            return str(detail)
        state = record.get("state")
        rollout_id = record.get("rollout_id")
        if state or rollout_id:
            return " ".join(str(part) for part in (rollout_id, state) if part)
    if kind == "runtime_validation":
        result = _as_dict(record.get("result"))
        if result:
            return "passed" if result.get("ok") else "failed"
    if kind == "runtime_fit":
        runtime_fit = _as_dict(record.get("runtime_fit"))
        target_selection = _as_dict(runtime_fit.get("target_selection"))
        detail = runtime_fit.get("detail") or record.get("readiness_headline")
        if target_selection.get("status") == "upgrade_available":
            best_target = target_selection.get("best_runtime_target_id")
            score_delta = target_selection.get("score_delta")
            delta = f" (+{score_delta} fit)" if score_delta not in (None, "") else ""
            return f"better target {best_target}{delta}"
        return str(detail) if detail else None
    if kind == "package_import":
        return "signature verified" if record.get("signature_verified") else "imported"
    if kind == "package_promotion":
        state = record.get("state")
        reason = record.get("reason")
        if state and reason:
            return f"{state}: {reason}"
        return str(state) if state else None
    if kind == "rollout_plan":
        detail = record.get("detail")
        if detail:
            return str(detail)
        state = record.get("state")
        plan_id = record.get("plan_id")
        if state or plan_id:
            return " ".join(str(part) for part in (plan_id, state) if part)
    if kind == "telemetry":
        event_type = record.get("event_type")
        return str(event_type) if event_type else None
    return None


def _mission_replay_phases(summary: dict[str, Any]) -> list[dict[str, Any]]:
    counts = _as_dict(summary.get("counts"))
    trust = _as_dict(summary.get("trust"))
    runtime = _as_dict(summary.get("runtime"))
    decisions = [
        decision for decision in _as_list(summary.get("decisions")) if isinstance(decision, dict)
    ]
    package_promotions = [
        promotion
        for promotion in _as_list(summary.get("package_promotions"))
        if isinstance(promotion, dict)
    ]
    rollout_plans = [
        plan for plan in _as_list(summary.get("rollout_plans")) if isinstance(plan, dict)
    ]
    fallbacks = [
        fallback for fallback in _as_list(summary.get("fallbacks")) if isinstance(fallback, dict)
    ]
    approvals = [
        approval for approval in _as_list(summary.get("approvals")) if isinstance(approval, dict)
    ]
    overrides = [
        override
        for override in _as_list(summary.get("operator_overrides"))
        if isinstance(override, dict)
    ]
    active_slot_overrides = [
        slot
        for slot in _as_list(summary.get("active_slots"))
        if isinstance(slot, dict) and slot.get("operator_override")
    ]
    ingested_evidence = [
        evidence
        for evidence in _as_list(summary.get("ingested_evidence"))
        if isinstance(evidence, dict)
    ]

    phases = [
        _replay_phase(
            "signed_package",
            "Signed package",
            "complete" if trust.get("signed_package_imports", 0) > 0 else "missing",
            (
                f"{trust.get('signed_package_imports', 0)} signed package imports"
                if trust.get("signed_package_imports", 0) > 0
                else "no signed package import evidence"
            ),
            trust.get("signed_package_ids", []),
        ),
        _runtime_validation_phase(trust),
        _runtime_fit_phase(runtime, _as_list(summary.get("active_slots"))),
        _package_release_phase(trust, package_promotions),
        _replay_phase(
            "policy_approval",
            "Policy approval",
            "complete" if approvals else "missing",
            (
                f"{len(approvals)} rollout approvals recorded"
                if approvals
                else "no rollout approval gate recorded"
            ),
            [approval.get("rollout_id") for approval in approvals if approval.get("rollout_id")],
        ),
        _replay_phase(
            "edge_rollout",
            "Edge rollout",
            "complete" if counts.get("rollout_events", 0) > 0 else "missing",
            (
                f"{counts.get('rollout_events', 0)} rollout lifecycle events"
                if counts.get("rollout_events", 0) > 0
                else "no rollout lifecycle evidence"
            ),
            [],
        ),
        _replay_phase(
            "rollout_coordination",
            "Rollout coordination",
            "complete" if rollout_plans else "missing",
            (
                f"{len(rollout_plans)} rollout plan events"
                if rollout_plans
                else "no staged rollout plan evidence"
            ),
            [plan.get("plan_id") for plan in rollout_plans if plan.get("plan_id")],
        ),
        _replay_phase(
            "policy_decision",
            "Policy adaptation",
            "complete" if _trigger_count(decisions, "policy") > 0 else "missing",
            (
                f"{_trigger_count(decisions, 'policy')} policy-driven model switches"
                if _trigger_count(decisions, "policy") > 0
                else "no policy-driven model switch evidence"
            ),
            [
                decision.get("to_model")
                for decision in decisions
                if decision.get("trigger_type") == "policy"
            ],
        ),
        _replay_phase(
            "fallback_rollback",
            "Fallback or rollback",
            "complete" if fallbacks or _trigger_count(decisions, "rollback") > 0 else "missing",
            (
                f"{len(fallbacks)} fallback recoveries"
                if fallbacks
                else (
                    f"{_trigger_count(decisions, 'rollback')} rollbacks"
                    if _trigger_count(decisions, "rollback") > 0
                    else "no fallback or rollback evidence"
                )
            ),
            [
                fallback.get("activated_model")
                for fallback in fallbacks
                if fallback.get("activated_model")
            ],
        ),
        _replay_phase(
            "operator_override",
            "Operator override",
            "complete" if overrides or active_slot_overrides else "missing",
            (
                f"{len(overrides)} operator overrides"
                if overrides
                else (
                    f"{len(active_slot_overrides)} active slot overrides"
                    if active_slot_overrides
                    else "no operator override evidence"
                )
            ),
            (
                [override.get("to_model") for override in overrides if override.get("to_model")]
                if overrides
                else [
                    slot.get("operator_override_model") or slot.get("active_model")
                    for slot in active_slot_overrides
                    if slot.get("operator_override_model") or slot.get("active_model")
                ]
            ),
        ),
        _offline_operation_phase(runtime, decisions),
        _replay_phase(
            "evidence_export",
            "Evidence export",
            "complete",
            f"{counts.get('timeline_entries', 0)} timeline entries exported",
            [],
        ),
    ]
    if ingested_evidence:
        phases.insert(
            -1,
            _replay_phase(
                "evidence_aggregation",
                "Evidence aggregation",
                "complete",
                f"{len(ingested_evidence)} edge evidence bundles aggregated",
                [
                    evidence.get("evidence_id")
                    for evidence in ingested_evidence
                    if evidence.get("evidence_id")
                ],
            ),
        )
    return phases


def _runtime_validation_phase(trust: dict[str, Any]) -> dict[str, Any]:
    passed_non_dry_run = int(trust.get("runtime_validations_passed_non_dry_run", 0) or 0)
    passed = int(trust.get("runtime_validations_passed", 0) or 0)
    if passed_non_dry_run > 0:
        return _replay_phase(
            "runtime_validation",
            "Runtime/device validation",
            "complete",
            f"{passed_non_dry_run} non-dry-run validations passed",
            trust.get("validated_package_ids", []),
        )
    if passed > 0:
        return _replay_phase(
            "runtime_validation",
            "Runtime/device validation",
            "preview_only",
            f"{passed} validation previews passed",
            trust.get("validated_package_ids", []),
        )
    return _replay_phase(
        "runtime_validation",
        "Runtime/device validation",
        "missing",
        "no passing runtime validation evidence",
        [],
    )


def _runtime_fit_phase(
    runtime: dict[str, Any],
    active_slots: list[dict[str, Any]],
) -> dict[str, Any]:
    fits = [
        _as_dict(evidence)
        for evidence in _as_list(runtime.get("runtime_fit_evidence"))
        if isinstance(evidence, dict)
    ]
    if not fits:
        return _replay_phase(
            "runtime_fit",
            "Runtime fit",
            "missing",
            "no runtime fit evidence",
            [],
        )

    fit = max(
        fits,
        key=lambda summary: _runtime_fit_summary_rank(summary, active_slots),
    )
    tier = str(fit.get("tier") or "unknown")
    score = fit.get("score")
    target = fit.get("runtime_target_id")
    best_target = fit.get("best_runtime_target_id")
    target_selection_status = str(fit.get("target_selection_status") or "")
    score_delta = fit.get("score_delta")
    lane_text = _runtime_fit_phase_lane_text(fit)
    refs = [
        fit.get("model_id"),
        fit.get("device_id"),
        target,
        best_target if best_target != target else None,
    ]
    score_text = f"{score}/100 {tier.replace('_', ' ')}" if score is not None else tier
    if target_selection_status == "upgrade_available":
        delta_text = f" (+{score_delta} fit)" if score_delta not in (None, "") else ""
        return _replay_phase(
            "runtime_fit",
            "Runtime fit",
            "preview_only",
            f"{score_text} on {target}{lane_text}; better target {best_target}{delta_text}",
            refs,
        )
    if tier in {"optimal", "ready"}:
        return _replay_phase(
            "runtime_fit",
            "Runtime fit",
            "complete",
            f"{score_text} on {target}{lane_text}",
            refs,
        )
    return _replay_phase(
        "runtime_fit",
        "Runtime fit",
        "preview_only",
        f"{score_text} on {target}{lane_text}; more edge evidence required",
        refs,
    )


def _runtime_fit_phase_lane_text(fit: dict[str, Any]) -> str:
    context: list[str] = []
    runtime_lane = fit.get("runtime_lane_label") or fit.get("runtime_lane_id")
    acceleration = fit.get("runtime_lane_acceleration")
    if runtime_lane:
        lane = f"lane {runtime_lane}"
        if acceleration:
            lane += f" / {str(acceleration).replace('_', ' ')}"
        context.append(lane)
    artifact_state = fit.get("artifact_lane_state")
    if artifact_state:
        context.append(f"artifact {str(artifact_state).replace('_', ' ')}")
    return f"; {'; '.join(context)}" if context else ""


def _package_release_phase(
    trust: dict[str, Any],
    package_promotions: list[dict[str, Any]],
) -> dict[str, Any]:
    released_package_ids = trust.get("released_package_ids", [])
    if released_package_ids:
        return _replay_phase(
            "package_release",
            "Package release",
            "complete",
            f"{len(released_package_ids)} packages released for rollout",
            released_package_ids,
        )
    released_events = [event for event in package_promotions if event.get("state") == "released"]
    if released_events:
        return _replay_phase(
            "package_release",
            "Package release",
            "complete",
            f"{len(released_events)} release transitions recorded",
            [event.get("package_id") for event in released_events],
        )
    return _replay_phase(
        "package_release",
        "Package release",
        "missing",
        "no package release evidence",
        [],
    )


def _offline_operation_phase(
    runtime: dict[str, Any],
    decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    pending_count = int(runtime.get("pending_operations_count", 0) or 0)
    dead_letter_count = int(runtime.get("pending_operation_dead_letters_count", 0) or 0)
    acknowledged_count = int(
        runtime.get("pending_operation_dead_letters_acknowledged_count", 0) or 0
    )
    requeued_count = int(runtime.get("pending_operation_dead_letters_requeued_count", 0) or 0)
    preflight = _as_dict(runtime.get("pending_operation_preflight"))
    preflight_total = int(preflight.get("total", 0) or 0)
    preflight_superseded = int(preflight.get("superseded", 0) or 0)
    verification = _as_dict(runtime.get("pending_operation_verification"))
    verification_total = int(verification.get("total", 0) or 0)
    pending_types = runtime.get("pending_operation_types", [])
    dead_letters = [
        record
        for record in _as_list(runtime.get("pending_operation_dead_letters"))
        if isinstance(record, dict)
    ]
    retargeted_decisions = [
        decision
        for decision in decisions or []
        if decision.get("runtime_retargeted")
        or _decision_runtime_retarget_fields(_as_dict(decision.get("audit_metadata")))
    ]

    if runtime.get("offline_mode"):
        return _replay_phase(
            "offline_operation",
            "Offline operation",
            "complete",
            "offline mode recorded",
            pending_types,
        )
    if pending_count > 0:
        return _replay_phase(
            "offline_operation",
            "Offline operation",
            "complete",
            f"{pending_count} pending operations",
            pending_types,
        )
    if retargeted_decisions:
        return _replay_phase(
            "offline_operation",
            "Offline operation",
            "complete",
            f"{len(retargeted_decisions)} retargeted DDIL replays",
            [
                decision.get("to_model")
                for decision in retargeted_decisions
                if decision.get("to_model")
            ],
        )
    if dead_letter_count > 0:
        detail = f"{dead_letter_count} quarantined DDIL intents retained"
        if requeued_count > 0:
            detail = f"{detail}; {requeued_count} requeued"
        if acknowledged_count > 0:
            detail = f"{detail}; {acknowledged_count} acknowledged"
        return _replay_phase(
            "offline_operation",
            "Offline operation",
            "complete",
            detail,
            [record.get("payload_sha256") for record in dead_letters],
        )
    if preflight_total > 0:
        detail = f"{preflight_total} DDIL intents preflighted"
        if preflight_superseded > 0:
            detail = f"{detail}; {preflight_superseded} superseded"
        return _replay_phase(
            "offline_operation",
            "Offline operation",
            "complete",
            detail,
            pending_types,
        )
    if verification_total > 0:
        return _replay_phase(
            "offline_operation",
            "Offline operation",
            "complete",
            f"{verification_total} signed DDIL intents verified",
            pending_types,
        )
    return _replay_phase(
        "offline_operation",
        "Offline operation",
        "missing",
        "no offline operation evidence",
        [],
    )


def _replay_phase(
    phase: str,
    label: str,
    status: str,
    summary: str,
    evidence_refs: list[Any],
) -> dict[str, Any]:
    refs = [str(ref) for ref in evidence_refs if ref]
    return {
        "phase": phase,
        "label": label,
        "status": status,
        "summary": summary,
        "evidence_refs": refs,
    }


def _trigger_count(decisions: list[dict[str, Any]], trigger_type: str) -> int:
    return sum(1 for decision in decisions if decision.get("trigger_type") == trigger_type)


def _rollout_approval_summaries(
    bundle: dict[str, Any],
    rollout_events: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    approvals: list[dict[str, Any]] = []
    seen_rollouts: set[str] = set()
    for event in rollout_events:
        if event.get("state") != "approved":
            continue
        rollout_id = event.get("rollout_id")
        if rollout_id:
            seen_rollouts.add(str(rollout_id))
        approvals.append(
            {
                "rollout_id": rollout_id,
                "device_id": event.get("device_id"),
                "package_id": event.get("package_id"),
                "slot": event.get("slot"),
                "actor": event.get("actor"),
                "reason": event.get("detail"),
                "approved_at": event.get("updated_at"),
                "summary": (
                    f"{rollout_id or 'rollout'} approved by " f"{event.get('actor') or 'unknown'}"
                ),
            }
        )

    hub_lite = _as_dict(bundle.get("hub_lite"))
    for rollout in _as_dict(hub_lite.get("rollouts")).values():
        rollout_data = _as_dict(rollout)
        rollout_id = rollout_data.get("rollout_id")
        if rollout_id and str(rollout_id) in seen_rollouts:
            continue
        approval = _as_dict(rollout_data.get("approval"))
        if approval.get("approved") is not True:
            continue
        approvals.append(
            {
                "rollout_id": rollout_id,
                "device_id": rollout_data.get("device_id"),
                "package_id": rollout_data.get("package_id"),
                "slot": rollout_data.get("slot"),
                "actor": approval.get("actor"),
                "reason": approval.get("reason"),
                "approved_at": approval.get("updated_at"),
                "summary": (
                    f"{rollout_id or 'rollout'} approved by "
                    f"{approval.get('actor') or 'unknown'}"
                ),
            }
        )
    return approvals[:limit]


def _timeline_summary(
    entry: dict[str, Any],
    *,
    active_slots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    active_runtime_proof = entry.get("active_runtime_proof") is True or _is_active_runtime_fit_entry(
        entry,
        active_slots or [],
    )
    summary = {
        "timestamp": entry.get("timestamp"),
        "kind": entry.get("kind"),
        "slot": entry.get("slot"),
        "summary": str(entry.get("summary") or entry.get("kind") or "event"),
    }
    if active_runtime_proof:
        summary["active_runtime_proof"] = True
    return summary


def _sort_timeline_entries(
    entries: list[dict[str, Any]],
    *,
    active_slots: list[dict[str, Any]] | None = None,
    reverse: bool,
) -> list[dict[str, Any]]:
    active_slots = active_slots or []

    def key(entry: dict[str, Any]) -> tuple[str, int, float]:
        active_rank = 1 if _is_active_runtime_fit_entry(entry, active_slots) else 0
        score = _timeline_runtime_fit_score(entry)
        timestamp_bucket = _timeline_timestamp_bucket(entry)
        if reverse:
            return (timestamp_bucket, active_rank, score)
        return (timestamp_bucket, -active_rank, -score)

    return [
        _annotate_timeline_entry(entry, active_slots=active_slots)
        for entry in sorted(entries, key=key, reverse=reverse)
    ]


def _annotate_timeline_entry(
    entry: dict[str, Any],
    *,
    active_slots: list[dict[str, Any]],
) -> dict[str, Any]:
    if entry.get("active_runtime_proof") is True:
        return entry
    if _is_active_runtime_fit_entry(entry, active_slots):
        return {**entry, "active_runtime_proof": True}
    return entry


def _timeline_timestamp_bucket(entry: dict[str, Any]) -> str:
    timestamp = str(entry.get("timestamp") or "")
    if entry.get("kind") == "runtime_fit" and len(timestamp) >= 19:
        return timestamp[:19]
    return timestamp


def _is_active_runtime_fit_entry(
    entry: dict[str, Any],
    active_slots: list[dict[str, Any]],
) -> bool:
    if entry.get("kind") != "runtime_fit":
        return False
    record = _as_dict(entry.get("record"))
    selection = _as_dict(record.get("selection"))
    runtime_fit = _as_dict(record.get("runtime_fit"))
    model_id = str(selection.get("model_id") or runtime_fit.get("model_id") or "")
    slot = str(entry.get("slot") or selection.get("slot") or "")
    if not model_id:
        return False
    active_by_slot = {
        str(active_slot_data.get("slot")): str(active_slot_data.get("active_model"))
        for active_slot in active_slots
        if (active_slot_data := _as_dict(active_slot)).get("slot")
        and active_slot_data.get("active_model")
    }
    if slot and active_by_slot.get(slot) == model_id:
        return True
    return model_id in {
        str(active_slot_data.get("active_model"))
        for active_slot in active_slots
        if (active_slot_data := _as_dict(active_slot)).get("active_model")
    }


def _timeline_runtime_fit_score(entry: dict[str, Any]) -> float:
    record = _as_dict(entry.get("record"))
    runtime_fit = _as_dict(record.get("runtime_fit"))
    score = runtime_fit.get("score")
    if isinstance(score, bool):
        return 0.0
    if isinstance(score, (int, float)):
        return float(score)
    try:
        return float(str(score))
    except (TypeError, ValueError):
        return 0.0


def decision_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return recent decision log entries across all slots."""
    decisions: list[dict[str, Any]] = []
    slots = state.slot_manager.list_slots()
    if slots:
        per_slot_limit = max(limit, 1)
        for slot in slots:
            decisions.extend(
                _normalize_decision(entry)
                for entry in state.slot_manager.get_decision_log(
                    slot_name=slot.name,
                    limit=per_slot_limit,
                )
            )
    else:
        decisions.extend(
            _normalize_decision(entry) for entry in state.slot_manager.get_decision_log(limit=limit)
        )
    decisions.sort(key=lambda entry: entry.get("created_at", ""), reverse=True)
    return decisions[:limit]


def rollout_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return rollout history entries across local Hub Lite state."""
    hub_lite = getattr(state, "hub_lite", None)
    if hub_lite is None:
        return []

    events: list[dict[str, Any]] = []
    for rollout in hub_lite.list_rollouts():
        rollout_id = rollout.get("rollout_id")
        for history in rollout.get("history", []) or []:
            event = {
                "rollout_id": rollout_id,
                "device_id": rollout.get("device_id"),
                "package_id": rollout.get("package_id"),
                "slot": rollout.get("slot"),
                "state": history.get("state"),
                "detail": history.get("detail"),
                "actor": history.get("actor"),
                "updated_at": history.get("updated_at"),
            }
            events.append(event)
    events.sort(key=lambda entry: entry.get("updated_at") or "", reverse=True)
    return events[:limit]


def runtime_validation_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return runtime target validation evidence records."""
    hub_lite = getattr(state, "hub_lite", None)
    if hub_lite is None:
        return []
    list_validations = getattr(hub_lite, "list_runtime_validations", None)
    if not callable(list_validations):
        return []
    return list_validations(limit=limit)


def hub_benchmark_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return hardware-aware benchmark evidence records from Hub Lite."""
    hub_lite = getattr(state, "hub_lite", None)
    if hub_lite is None:
        return []
    list_benchmarks = getattr(hub_lite, "list_benchmarks", None)
    if not callable(list_benchmarks):
        return []
    return list_benchmarks(limit=limit)


def runtime_fit_evidence_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return readiness-derived runtime fit proof for rollout contexts."""
    hub_lite = getattr(state, "hub_lite", None)
    if hub_lite is None:
        return []
    list_rollouts = getattr(hub_lite, "list_rollouts", None)
    deployment_readiness = getattr(hub_lite, "deployment_readiness", None)
    if not callable(list_rollouts) or not callable(deployment_readiness):
        return []

    records: list[dict[str, Any]] = []
    seen_contexts: set[tuple[str, str, str, str, str]] = set()
    for rollout in list_rollouts():
        if not isinstance(rollout, dict):
            continue
        context = {
            "package_id": rollout.get("package_id"),
            "model_id": rollout.get("model_id"),
            "device_id": rollout.get("device_id"),
            "runtime_target_id": rollout.get("runtime_target_id"),
            "slot": rollout.get("slot"),
        }
        key = tuple(str(context.get(name) or "") for name in sorted(context))
        if key in seen_contexts:
            continue
        seen_contexts.add(key)
        try:
            readiness = deployment_readiness(**context)
        except Exception:
            continue
        record = _readiness_runtime_fit_evidence(readiness)
        if record:
            records.append(record)
    records = _dedupe_runtime_fit_evidence(records)
    return records[:limit]


def _readiness_runtime_fit_evidence(readiness: dict[str, Any]) -> dict[str, Any] | None:
    runtime_fit = _as_dict(readiness.get("runtime_fit"))
    if not runtime_fit:
        return None
    gates = [gate for gate in _as_list(readiness.get("gates")) if isinstance(gate, dict)]
    runtime_optimizer_gate = next(
        (gate for gate in gates if gate.get("gate_id") == "runtime_optimizer"),
        None,
    )
    return {
        "schema_version": "temms-runtime-fit-evidence/v1",
        "checked_at": readiness.get("checked_at"),
        "readiness_status": readiness.get("status"),
        "readiness_headline": readiness.get("headline"),
        "selection": _as_dict(readiness.get("selection")),
        "runtime_fit": runtime_fit,
        "runtime_decision": _as_dict(readiness.get("runtime_decision")),
        "edge_execution_contract": _as_dict(readiness.get("edge_execution_contract")),
        "runtime_workbench": _as_dict(readiness.get("runtime_workbench")),
        "edge_runtime_mission": _as_dict(readiness.get("edge_runtime_mission")),
        "runtime_optimizer_gate": _as_dict(runtime_optimizer_gate),
    }


def package_import_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return package import audit records from the local model cache."""
    model_cache = getattr(state, "model_cache", None)
    if model_cache is None:
        return []

    events: list[dict[str, Any]] = []
    for package in model_cache.list_packages():
        manifest = package.manifest if isinstance(package.manifest, dict) else {}
        import_audit = manifest.get("_temms_import")
        if not isinstance(import_audit, dict):
            import_audit = {}
        signature = import_audit.get("signature") if isinstance(import_audit, dict) else None
        signature_summary = None
        if isinstance(signature, dict):
            signature_summary = {
                "schema_version": signature.get("schema_version"),
                "algorithm": signature.get("algorithm"),
                "signer": signature.get("signer"),
                "key_fingerprint": signature.get("key_fingerprint"),
                "signed_at": signature.get("signed_at"),
                "manifest_sha256": signature.get("manifest_sha256"),
            }
        events.append(
            {
                "schema_version": "temms-package-import-event/v1",
                "package_id": package.id,
                "name": package.name,
                "version": package.version,
                "source": package.source,
                "slot": _package_import_slot(manifest),
                "slots": _package_import_slots(manifest),
                "imported_at": import_audit.get("imported_at") or package.imported_at.isoformat(),
                "source_sha256": import_audit.get("source_sha256"),
                "source_type": import_audit.get("source_type"),
                "hashes_verified": import_audit.get("hashes_verified"),
                "signature_required": import_audit.get("signature_required"),
                "signature_verified": import_audit.get("signature_verified"),
                "signature": signature_summary,
                "device_profile": import_audit.get("device_profile"),
                "warnings": import_audit.get("warnings", []),
                "import": import_audit,
            }
        )
    events.sort(key=lambda entry: entry.get("imported_at") or "", reverse=True)
    return events[:limit]


def package_promotion_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return package promotion lifecycle evidence from Hub Lite."""
    hub_lite = getattr(state, "hub_lite", None)
    if hub_lite is None:
        return []
    list_packages = getattr(hub_lite, "list_packages", None)
    if not callable(list_packages):
        return []
    promotions: list[dict[str, Any]] = []
    for package in list_packages():
        if not isinstance(package, dict):
            continue
        promotion = _as_dict(package.get("promotion"))
        for event in _as_list(promotion.get("history")):
            if not isinstance(event, dict):
                continue
            promotions.append(
                {
                    "package_id": package.get("package_id"),
                    "state": event.get("state"),
                    "from_state": event.get("from_state"),
                    "actor": event.get("actor"),
                    "reason": event.get("reason"),
                    "evidence": _as_dict(event.get("evidence")),
                    "updated_at": event.get("updated_at"),
                }
            )
    promotions.sort(key=lambda entry: entry.get("updated_at") or "", reverse=True)
    return promotions[:limit]


def rollout_plan_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return coordinated rollout-plan history entries from Hub Lite."""
    hub_lite = getattr(state, "hub_lite", None)
    if hub_lite is None:
        return []
    list_plans = getattr(hub_lite, "list_rollout_plans", None)
    if not callable(list_plans):
        return []
    events: list[dict[str, Any]] = []
    for plan in list_plans():
        if not isinstance(plan, dict):
            continue
        for history in _as_list(plan.get("history")):
            if not isinstance(history, dict):
                continue
            events.append(
                {
                    "schema_version": "temms-rollout-plan-event/v1",
                    "plan_id": plan.get("plan_id"),
                    "package_id": plan.get("package_id"),
                    "slot": plan.get("slot"),
                    "runtime_target_id": plan.get("runtime_target_id"),
                    "state": history.get("state"),
                    "detail": history.get("detail"),
                    "actor": history.get("actor"),
                    "batch": history.get("batch"),
                    "rollout_ids": _as_list(history.get("rollout_ids")),
                    "counts": _as_dict(history.get("counts")),
                    "updated_at": history.get("updated_at"),
                }
            )
    events.sort(key=lambda entry: entry.get("updated_at") or "", reverse=True)
    return events[:limit]


def combined_timeline(
    decisions: list[dict[str, Any]],
    telemetry_events: list[dict[str, Any]],
    rollout_events: list[dict[str, Any]] | None = None,
    runtime_validations: list[dict[str, Any]] | None = None,
    hub_benchmarks: list[dict[str, Any]] | None = None,
    runtime_fit_evidence: list[dict[str, Any]] | None = None,
    package_imports: list[dict[str, Any]] | None = None,
    package_promotions: list[dict[str, Any]] | None = None,
    rollout_plans: list[dict[str, Any]] | None = None,
    *,
    active_slots: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Merge decisions, rollouts, telemetry, validation, benchmark, and import evidence."""
    timeline: list[dict[str, Any]] = []
    for decision in decisions:
        retarget = _record_runtime_retarget_fields(decision)
        if retarget:
            summary = (
                f"{decision.get('to_model') or 'model'} DDIL replay retargeted "
                f"{retarget['runtime_retargeted_from']} -> "
                f"{retarget['runtime_retargeted_to']}"
            )
        else:
            summary = (
                f"{decision.get('from_model') or 'none'} -> "
                f"{decision.get('to_model')} "
                f"({decision.get('trigger_type')})"
            )
        timeline.append(
            {
                "kind": "decision",
                "timestamp": decision.get("created_at"),
                "slot": decision.get("slot"),
                "summary": summary,
                "record": decision,
            }
        )
    for event in telemetry_events:
        timeline.append(
            {
                "kind": "telemetry",
                "timestamp": event.get("timestamp"),
                "slot": event.get("payload", {}).get("slot"),
                "summary": event.get("event_type"),
                "record": event,
            }
        )
    for event in rollout_events or []:
        actor = event.get("actor") or "unknown"
        timeline.append(
            {
                "kind": "rollout",
                "timestamp": event.get("updated_at"),
                "slot": event.get("slot"),
                "summary": (f"{event.get('rollout_id')} {event.get('state')} " f"by {actor}"),
                "record": event,
            }
        )
    for validation in runtime_validations or []:
        result = validation.get("result") if isinstance(validation.get("result"), dict) else {}
        status = "passed" if result.get("ok") else "failed"
        if result.get("dry_run"):
            status = "previewed"
        runtime_target = validation.get("runtime_target_id") or "runtime"
        timeline.append(
            {
                "kind": "runtime_validation",
                "timestamp": validation.get("created_at"),
                "slot": None,
                "summary": (
                    f"{validation.get('package_id') or validation.get('package_path')} "
                    f"{status} on {runtime_target}"
                ),
                "record": validation,
            }
        )
    for benchmark in hub_benchmarks or []:
        result = benchmark.get("result") if isinstance(benchmark.get("result"), dict) else {}
        latency = result.get("latency_ms") if isinstance(result.get("latency_ms"), dict) else {}
        p95 = latency.get("p95")
        summary = f"{benchmark.get('model_id') or 'model'} benchmarked"
        if p95 is not None:
            summary += f" p95={p95}ms"
        if benchmark.get("device_id"):
            summary += f" on {benchmark['device_id']}"
        timeline.append(
            {
                "kind": "benchmark",
                "timestamp": benchmark.get("created_at"),
                "slot": result.get("slot"),
                "summary": summary,
                "record": benchmark,
            }
        )
    for evidence in runtime_fit_evidence or []:
        runtime_fit = evidence.get("runtime_fit") if isinstance(evidence.get("runtime_fit"), dict) else {}
        selection = evidence.get("selection") if isinstance(evidence.get("selection"), dict) else {}
        lane = _as_dict(runtime_fit.get("runtime_lane"))
        score = runtime_fit.get("score")
        tier = str(runtime_fit.get("tier") or "runtime fit").replace("_", " ")
        runtime_target = (
            selection.get("runtime_target_id")
            or runtime_fit.get("runtime_target_id")
            or "runtime"
        )
        model_id = selection.get("model_id") or runtime_fit.get("model_id") or "model"
        summary = f"{model_id} runtime fit {tier}"
        if score is not None:
            summary = f"{model_id} runtime fit {score}/100 {tier}"
        summary += f" on {runtime_target}"
        lane_label = lane.get("label") or lane.get("lane_id")
        if lane_label:
            summary += f" / {lane_label}"
        timeline.append(
            {
                "kind": "runtime_fit",
                "timestamp": evidence.get("checked_at"),
                "slot": selection.get("slot"),
                "summary": summary,
                "record": evidence,
            }
        )
    for package_import in package_imports or []:
        status = "verified" if package_import.get("signature_verified") else "imported"
        signer = (package_import.get("signature") or {}).get("signer")
        summary = f"{package_import.get('package_id')} {status}"
        if signer:
            summary += f" by {signer}"
        timeline.append(
            {
                "kind": "package_import",
                "timestamp": package_import.get("imported_at"),
                "slot": package_import.get("slot"),
                "summary": summary,
                "record": package_import,
            }
        )
    for promotion in package_promotions or []:
        state = promotion.get("state") or "candidate"
        package_id = promotion.get("package_id") or "package"
        actor = promotion.get("actor") or "unknown"
        timeline.append(
            {
                "kind": "package_promotion",
                "timestamp": promotion.get("updated_at"),
                "slot": None,
                "summary": f"{package_id} promoted to {state} by {actor}",
                "record": promotion,
            }
        )
    for plan_event in rollout_plans or []:
        state = plan_event.get("state") or "updated"
        plan_id = plan_event.get("plan_id") or "rollout plan"
        actor = plan_event.get("actor") or "unknown"
        timeline.append(
            {
                "kind": "rollout_plan",
                "timestamp": plan_event.get("updated_at"),
                "slot": plan_event.get("slot"),
                "summary": f"{plan_id} {state} by {actor}",
                "record": plan_event,
            }
        )
    return _sort_timeline_entries(timeline, active_slots=active_slots, reverse=True)


def _normalize_decision(entry: dict[str, Any]) -> dict[str, Any]:
    decision = dict(entry)
    snapshot = decision.get("conditions_snapshot")
    if isinstance(snapshot, str):
        try:
            decision["conditions_snapshot"] = json.loads(snapshot)
        except Exception:
            pass
    metadata = decision.get("audit_metadata")
    if isinstance(metadata, str):
        try:
            decision["audit_metadata"] = json.loads(metadata)
        except Exception:
            pass
    for key, value in list(decision.items()):
        if hasattr(value, "isoformat"):
            decision[key] = value.isoformat()
    return decision


def _slot_to_dict(slot: Any) -> dict[str, Any]:
    data = {
        "name": slot.name,
        "description": slot.description,
        "required": slot.required,
        "default_model": slot.default_model,
        "active_model_id": slot.active_model_id,
        "state": slot.state.value,
        "candidates": slot.candidates,
        "metadata": slot.metadata,
        "updated_at": slot.updated_at.isoformat(),
    }
    if slot.operator_override is not None:
        data["operator_override"] = {
            "model_id": slot.operator_override.model_id,
            "reason": slot.operator_override.reason,
            "source": slot.operator_override.source,
            "set_at": slot.operator_override.set_at.isoformat(),
            "expires_at": (
                slot.operator_override.expires_at.isoformat()
                if slot.operator_override.expires_at
                else None
            ),
        }
    return data


def _package_to_dict(package: Any) -> dict[str, Any]:
    return {
        "id": package.id,
        "name": package.name,
        "version": package.version,
        "source": package.source,
        "imported_at": package.imported_at.isoformat(),
        "manifest": package.manifest,
    }


def _safe_json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _model_dict(model: Any | None) -> dict[str, Any] | None:
    return model.to_dict() if model is not None else None


class EvidenceBundleBuilder:
    """Build portable evidence directly from local stores."""

    def __init__(
        self,
        slot_manager: Any,
        condition_store: Any,
        policy_engine: Any,
        model_cache: Any,
    ):
        self.slot_manager = slot_manager
        self.condition_store = condition_store
        self.policy_engine = policy_engine
        self.model_cache = model_cache

    def build(
        self,
        slot_name: str | None = None,
        limit: int = 100,
        runtime_slots: dict[str, dict[str, Any]] | None = None,
        offline_mode: bool = False,
        pending_operations: list[dict[str, Any]] | None = None,
        deployment_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        models = {model.id: model for model in self.model_cache.list_models()}
        packages = {package.id: package for package in self.model_cache.list_packages()}
        decisions = self.slot_manager.get_decision_log(slot_name=slot_name, limit=limit)
        enriched_decisions = [
            self._decision_evidence(decision, models, packages) for decision in decisions
        ]
        policies = [
            policy.model_dump(mode="json", exclude_none=True)
            for policy in self.policy_engine.list_policies()
        ]
        conditions = self.condition_store.get_all()

        payload: dict[str, Any] = {
            "schema_version": "temms-evidence-bundle/v1",
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "scope": {
                "slot": slot_name,
                "decision_limit": limit,
            },
            "runtime": {
                "offline_mode": offline_mode,
                "deployment_state": deployment_state,
                "pending_operations": pending_operations or [],
                "runtime_slots": runtime_slots or {},
            },
            "slots": [_slot_to_dict(slot) for slot in self.slot_manager.list_slots()],
            "conditions": {
                "snapshot": self.condition_store.get_snapshot(),
                "values": {path: value.to_dict() for path, value in sorted(conditions.items())},
            },
            "policies": policies,
            "models": [_model_dict(model) for model in models.values()],
            "packages": [_package_to_dict(package) for package in packages.values()],
            "decisions": enriched_decisions,
        }
        payload["integrity"] = {
            "payload_sha256": _canonical_hash(payload),
            "algorithm": "sha256/json-canonical-v1",
        }
        return payload

    def _decision_evidence(
        self,
        decision: dict[str, Any],
        models: dict[str, Any],
        packages: dict[str, Any],
    ) -> dict[str, Any]:
        to_model = models.get(decision.get("to_model"))
        from_model = models.get(decision.get("from_model"))
        package = packages.get(to_model.package_id) if to_model is not None else None

        return {
            "id": decision.get("id"),
            "slot": decision.get("slot"),
            "from_model": decision.get("from_model"),
            "to_model": decision.get("to_model"),
            "trigger_type": decision.get("trigger_type"),
            "trigger_detail": decision.get("trigger_detail"),
            "created_at": decision.get("created_at"),
            "conditions_snapshot": _safe_json_loads(
                decision.get("conditions_snapshot"),
                {},
            ),
            "audit_metadata": _safe_json_loads(decision.get("audit_metadata"), {}),
            "model_evidence": {
                "from_model": _model_dict(from_model),
                "to_model": _model_dict(to_model),
                "to_package": _package_to_dict(package) if package is not None else None,
            },
        }


def _package_import_slot(manifest: dict[str, Any]) -> str | None:
    slots = _package_import_slots(manifest)
    return slots[0] if slots else None


def _package_import_slots(manifest: dict[str, Any]) -> list[str]:
    slots: set[str] = set()
    for policy in manifest.get("policies", []) or []:
        if isinstance(policy, dict) and policy.get("slot"):
            slots.add(str(policy["slot"]))
    compatibility = manifest.get("compatibility") if isinstance(manifest, dict) else {}
    if isinstance(compatibility, dict):
        declared = compatibility.get("slots")
        if isinstance(declared, list):
            slots.update(str(slot) for slot in declared if slot)
    return sorted(slots)


def _deployment_state(state: Any) -> dict[str, Any] | None:
    store = getattr(state, "deployment_state", None)
    if store is None:
        return None
    payload = store._read()
    return {
        "state": payload.get("state"),
        "reason": payload.get("reason"),
        "updated_at": payload.get("updated_at"),
    }


def _runtime_context(state: Any) -> dict[str, Any]:
    """Return edge runtime context that matters for post-mission evidence."""
    from temms.daemon.pending_preflight import pending_sync_preflight

    signature_required, signing_key = _pending_operation_signature_policy(state)
    preflight = pending_sync_preflight(state)
    pending_operations = _pending_operations(
        state,
        require_signature=signature_required,
        signing_key=signing_key,
    )
    _attach_pending_operation_preflight(pending_operations, preflight)
    return {
        "offline_mode": bool(getattr(state, "offline_mode", False)),
        "pending_operation_signature_required": signature_required,
        "pending_operation_signing_key_configured": bool(signing_key),
        "pending_operation_verification": _pending_operation_verification_summary(
            pending_operations
        ),
        "pending_operation_preflight": preflight,
        "pending_operation_dead_letters": _pending_operation_dead_letters(state),
        "pending_operations": pending_operations,
    }


def _pending_operations(
    state: Any,
    *,
    require_signature: bool = False,
    signing_key: str | None = None,
) -> list[dict[str, Any]]:
    from temms.daemon.pending_ops import pending_operation_signature_status

    store = getattr(state, "pending_operations", None)
    if store is None:
        return []
    if isinstance(store, list):
        entries = store
    else:
        read_all = getattr(store, "read_all", None)
        if not callable(read_all):
            return []
        try:
            entries = list(read_all())
        except Exception:
            return []
    operations: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        operation = dict(entry)
        operation["verification"] = pending_operation_signature_status(
            operation,
            signing_key=signing_key,
            require_signature=require_signature,
        )
        operations.append(operation)
    return operations


def _decision_chain_evidence(state: Any) -> dict[str, Any]:
    """Return the tamper-evident decision-chain summary (signed if a key exists).

    The decision entries themselves are already in the bundle under ``decisions``
    (each carries ``entry_hash``/``prev_hash``); this block records the verified
    head so an offline auditor can confirm nothing was deleted, reordered, or
    mutated, and — when the daemon holds a signing key — that the head is
    authentic (issue #27).
    """
    slot_manager = getattr(state, "slot_manager", None)
    verify = getattr(slot_manager, "verify_decision_chain", None)
    if not callable(verify):
        return {}
    verification = verify()
    block: dict[str, Any] = {
        "schema_version": "temms-decision-chain/v1",
        "head_hash": slot_manager.decision_chain_head(),
        "length": verification.get("length", 0),
        "verification": verification,
        # The ordered chain so a recipient can re-verify offline without the DB.
        "entries": slot_manager.export_decision_chain(),
    }
    _, signing_key = _pending_operation_signature_policy(state)
    if signing_key:
        try:
            block["head_signature"] = slot_manager.sign_decision_chain_head(signing_key)
        except Exception:
            block["head_signature_error"] = "could not sign decision chain head"
    return block


def verify_decision_chain_export(
    block: dict[str, Any], public_key: str | None = None
) -> dict[str, Any]:
    """Re-verify an exported decision chain offline, without the source DB.

    Walks the ordered ``entries``, recomputing each link, and (if a public key
    and head signature are present) verifies the head signature. This is the
    "verify after capture, on any machine" path for issue #27.
    """
    from temms.core.signing import ed25519_verify
    from temms.slots.manager import DECISION_CHAIN_GENESIS, SlotManager

    content_keys = (
        "slot", "from_model", "to_model", "trigger_type", "trigger_detail",
        "conditions_snapshot", "audit_metadata", "created_at",
    )
    entries = block.get("entries") or []
    prev_hash = DECISION_CHAIN_GENESIS
    for index, entry in enumerate(entries):
        if entry.get("prev_hash") != prev_hash:
            return {"valid": False, "length": len(entries), "broken_at": index,
                    "reason": "prev_hash link mismatch"}
        expected = SlotManager._decision_entry_hash(
            {key: entry.get(key) for key in content_keys}, prev_hash
        )
        if expected != entry.get("entry_hash"):
            return {"valid": False, "length": len(entries), "broken_at": index,
                    "reason": "entry content does not match its hash"}
        prev_hash = entry["entry_hash"]

    result: dict[str, Any] = {"valid": True, "length": len(entries), "head_hash": prev_hash}
    head_sig = block.get("head_signature") or {}
    if head_sig:
        result["head_matches_signed_head"] = head_sig.get("head_hash") == prev_hash
        result["key_fingerprint"] = head_sig.get("key_fingerprint")
        if public_key and head_sig.get("signature"):
            result["signature_valid"] = ed25519_verify(
                str(head_sig.get("head_hash", "")).encode(), head_sig["signature"], public_key
            )
    return result


def _pending_operation_signature_policy(state: Any) -> tuple[bool, str | None]:
    from temms.core.signing import read_signing_key

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


def _pending_operation_verification_summary(
    pending_operations: list[dict[str, Any]],
) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for operation in pending_operations:
        verification = _as_dict(operation.get("verification"))
        status = str(verification.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "total": len(pending_operations),
        "verified": statuses.get("verified", 0),
        "invalid": statuses.get("invalid", 0),
        "missing_signature": statuses.get("missing_signature", 0),
        "key_unavailable": statuses.get("key_unavailable", 0),
        "unsigned_allowed": statuses.get("unsigned_allowed", 0),
        "statuses": statuses,
    }


def _attach_pending_operation_preflight(
    pending_operations: list[dict[str, Any]],
    preflight: dict[str, Any],
) -> None:
    preflight_by_digest = {
        str(entry.get("payload_sha256")): entry
        for entry in _as_list(preflight.get("entries"))
        if isinstance(entry, dict) and entry.get("payload_sha256")
    }
    for operation in pending_operations:
        payload_digest = _canonical_hash(_as_dict(operation.get("payload")))
        preflight_entry = preflight_by_digest.get(payload_digest)
        if preflight_entry is not None:
            operation["preflight"] = preflight_entry


def _pending_operation_dead_letters(state: Any) -> list[dict[str, Any]]:
    store = getattr(state, "pending_operations", None)
    read_dead_letter = getattr(store, "read_dead_letter", None)
    if not callable(read_dead_letter):
        return []
    try:
        records = read_dead_letter()
    except Exception:
        return []
    return [
        _pending_operation_dead_letter_summary(record)
        for record in records
        if isinstance(record, dict)
    ]


def _pending_operation_dead_letter_summary(record: dict[str, Any]) -> dict[str, Any]:
    entry = _as_dict(record.get("entry"))
    payload = _as_dict(entry.get("payload"))
    request = _as_dict(payload.get("request"))
    preflight = _as_dict(record.get("preflight"))
    summary = {
        "schema_version": record.get("schema_version"),
        "quarantined_at": record.get("quarantined_at"),
        "actor": record.get("actor"),
        "reason": record.get("reason"),
        "acknowledged": bool(record.get("acknowledged")),
        "acknowledged_at": record.get("acknowledged_at"),
        "acknowledged_by": record.get("acknowledged_by"),
        "acknowledgement_reason": record.get("acknowledgement_reason"),
        "requeued": bool(record.get("requeued")),
        "requeued_at": record.get("requeued_at"),
        "requeued_by": record.get("requeued_by"),
        "requeue_reason": record.get("requeue_reason"),
        "operation": entry.get("operation") or preflight.get("operation"),
        "recorded_at": entry.get("recorded_at"),
        "slot": _first_string([payload, request, preflight], ("slot", "slot_name")),
        "device_id": _first_string([payload, request, preflight], ("device_id",)),
        "package_id": _first_string([payload, request, preflight], ("package_id",)),
        "model_id": _first_string([payload, request, preflight], ("model_id", "model")),
        "runtime_target_id": _first_string(
            [payload, request, preflight],
            ("runtime_target_id",),
        ),
        "payload_sha256": record.get("payload_sha256") or preflight.get("payload_sha256"),
        "signature_status": preflight.get("signature_status"),
        "signature_verified": preflight.get("signature_verified"),
        "replay_status": preflight.get("replay_status"),
        "replay_ready": preflight.get("ready"),
        "replay_reason": preflight.get("reason"),
        **_pending_preflight_runtime_proof_fields(preflight),
    }
    summary["summary"] = _pending_operation_label(summary)
    return {key: value for key, value in summary.items() if value is not None}


def _diagnostics(state: Any) -> dict[str, Any]:
    """Return a doctor-like diagnostic snapshot for evidence bundles."""
    from temms import __version__
    from temms.core.cache_health import model_cache_health
    from temms.core.runtime_profiles import detect_runtime_capabilities, known_device_profiles

    capabilities = detect_runtime_capabilities()
    daemon_config = getattr(state, "daemon_config", None)
    model_cache = getattr(state, "model_cache", None)
    model_storage = getattr(state, "model_storage", None)

    path_candidates: list[tuple[str, Path]] = []
    if daemon_config is not None:
        if getattr(daemon_config, "db_path", None) is not None:
            path_candidates.append(("database_dir", daemon_config.db_path.parent))
        if getattr(daemon_config, "model_dir", None) is not None:
            path_candidates.append(("model_dir", daemon_config.model_dir))
            path_candidates.append(("cache_dir", daemon_config.model_dir.parent / "cache"))
            path_candidates.append(("package_dir", daemon_config.model_dir.parent / "packages"))
        if getattr(daemon_config, "policy_dir", None) is not None:
            path_candidates.append(("policy_dir", daemon_config.policy_dir))
    else:
        if model_cache is not None and getattr(model_cache, "db_path", None) is not None:
            path_candidates.append(("database_dir", model_cache.db_path.parent))
        if model_storage is not None and getattr(model_storage, "model_dir", None) is not None:
            path_candidates.append(("model_dir", model_storage.model_dir))

    paths = [_path_report(name, path) for name, path in path_candidates]

    cache_report = None
    if model_cache is not None:
        models = model_cache.list_models()
        storage_stats = (
            model_storage.get_storage_stats()
            if model_storage is not None
            else {"model_count": None, "total_size_bytes": None, "storage_path": None}
        )
        cache_report = {
            "database": str(getattr(model_cache, "db_path", "")),
            "models": len(models),
            "packages": len(model_cache.list_packages()),
            "model_count_on_disk": storage_stats.get("model_count"),
            "total_size_bytes": storage_stats.get("total_size_bytes"),
            "storage_path": storage_stats.get("storage_path"),
            "health": model_cache_health(models),
        }

    port = None
    ports = []
    if daemon_config is not None:
        host = getattr(daemon_config, "inference_host", "0.0.0.0")
        port_number = getattr(daemon_config, "inference_port", None)
        if port_number is not None:
            port = {
                "name": "api",
                "host": host,
                "check_host": "127.0.0.1",
                "port": port_number,
                "status": _port_status("127.0.0.1", int(port_number)),
            }
            ports.append(port)

    return {
        "schema_version": "temms-diagnostics/v1",
        "temms_version": __version__,
        "system": capabilities.to_dict(),
        "known_device_profiles": known_device_profiles(),
        "paths": paths,
        "port": port,
        "ports": ports,
        "model_cache": cache_report,
    }


def _path_report(name: str, path: Path) -> dict[str, Any]:
    exists = path.exists()
    writable_target = path if exists else _nearest_existing_parent(path)
    write_probe = _probe_path_writable(writable_target)
    return {
        "name": name,
        "path": str(path),
        "exists": exists,
        "writable_target": str(writable_target),
        "writable": write_probe["ok"],
        "write_probe": write_probe,
    }


def _nearest_existing_parent(path: Path) -> Path:
    """Return the closest existing parent for a path that may not exist yet."""
    current = path.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _probe_path_writable(path: Path) -> dict[str, Any]:
    """Create and delete a tiny probe file to prove directory writability."""
    if not path.exists():
        return {
            "ok": False,
            "path": str(path),
            "attempted": False,
            "error": "target does not exist",
        }
    if not path.is_dir():
        return {
            "ok": False,
            "path": str(path),
            "attempted": False,
            "error": "target is not a directory",
        }

    try:
        with tempfile.NamedTemporaryFile(
            prefix=".temms-evidence-",
            dir=path,
            delete=True,
        ) as probe:
            probe.write(b"ok")
            probe.flush()
        return {"ok": True, "path": str(path), "attempted": True, "error": None}
    except Exception as e:
        return {
            "ok": False,
            "path": str(path),
            "attempted": True,
            "error": str(e),
        }


def _port_status(host: str, port: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex((host, port)) == 0:
            return "in use"
    return "free"


def _benchmark_results(state: Any) -> list[dict[str, Any]]:
    benchmark_dir = _benchmark_dir(state)
    if benchmark_dir is None or not benchmark_dir.exists():
        return []

    results: list[dict[str, Any]] = []
    for path in sorted(benchmark_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.setdefault("path", str(path))
            results.append(payload)
        except Exception:
            results.append({"path": str(path), "error": "invalid benchmark JSON"})
    return results


def _benchmark_dir(state: Any) -> Path | None:
    daemon_config = getattr(state, "daemon_config", None)
    if daemon_config is not None and getattr(daemon_config, "model_dir", None) is not None:
        return daemon_config.model_dir.parent / "benchmarks"
    model_cache = getattr(state, "model_cache", None)
    if model_cache is not None and getattr(model_cache, "db_path", None) is not None:
        return model_cache.db_path.parent / "benchmarks"
    return None
