"""Mission package planning and edge handoff helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

EDGE_MISSION_PACKAGE_SCHEMA_VERSION = "temms-edge-mission-package/v1"
EDGE_MISSION_PACKAGE_IDENTITY_SCHEMA_VERSION = (
    "temms-edge-mission-package-identity/v1"
)
EDGE_MISSION_PACKAGE_COMPONENT_DIGESTS_SCHEMA_VERSION = (
    "temms-edge-mission-package-component-digests/v1"
)
READINESS_REMEDIATION_ACTOR = "operator:readiness-remediation"
READINESS_REMEDIATION_ID_PREFIX = "readiness"

EDGE_MISSION_PACKAGE_IDENTITY_COMPONENTS = (
    "mission",
    "selection",
    "slo",
    "model_handling",
    "ddil",
    "runtime_plan",
    "proof_gate",
)
EDGE_MISSION_PACKAGE_IDENTITY_TRANSIENT_KEYS = {
    "age_seconds",
    "checked_at",
    "created_at",
    "deployment_id",
    "heartbeat_age_seconds",
    "last_seen",
    "last_seen_at",
    "plan_id",
    "planned_at",
    "rollout_id",
    "rollout_plan_id",
    "updated_at",
}

ProofGateFailures = Callable[..., list[str]]
CapabilityLockProvider = Callable[[dict[str, Any]], dict[str, Any]]
CapabilityLockSummary = Callable[[dict[str, Any]], dict[str, Any]]

MISSION_PACKAGE_YAML_FIELD_PATHS: dict[str, tuple[str, ...]] = {
    "confidence_threshold": (
        "model_handling.confidence_threshold",
        "switching.confidence_threshold",
        "switch_confidence_threshold",
        "confidence_threshold",
    ),
    "ddil_mode": (
        "ddil.mode",
        "ddil_mode",
        "ddil.behavior",
        "ddil_behavior",
        "offline_behavior",
    ),
    "device_id": (
        "selection.device_id",
        "edge.device_id",
        "edge_device_id",
        "target_device_id",
        "device_id",
    ),
    "fallback_model_id": (
        "model_handling.fallback_model_id",
        "fallback_model_id",
        "fallback_model",
        "fallback",
    ),
    "goal": (
        "mission.goal",
        "mission_goal",
        "goal",
        "objective",
        "description",
    ),
    "latency_budget_ms": (
        "slo.latency_budget_ms",
        "latency_budget_ms",
        "max_latency_ms_p95",
        "latency_ms_p95",
        "latency_ms",
    ),
    "min_throughput_ips": (
        "slo.min_throughput_ips",
        "min_throughput_ips",
        "throughput_ips",
        "min_inferences_per_second",
    ),
    "model_id": (
        "selection.model_id",
        "model.id",
        "selected_model_id",
        "primary_model_id",
        "model_id",
    ),
    "package_id": (
        "selection.package_id",
        "model.package_id",
        "artifact.package_id",
        "package.id",
        "package_id",
    ),
    "runtime_target_id": (
        "selection.runtime_target_id",
        "runtime.runtime_target_id",
        "target_runtime_id",
        "runtime.id",
        "runtime_target_id",
    ),
    "sensor": (
        "mission.sensor",
        "input.sensor",
        "sensor_input",
        "sensor_id",
        "sensor",
    ),
    "slot": (
        "mission.slot",
        "selection.slot",
        "capability_slot",
        "slot",
    ),
    "switch_policy": (
        "model_handling.switch_policy",
        "switching.policy",
        "model_switch_policy",
        "switch_policy",
    ),
}

MISSION_PACKAGE_YAML_FLOAT_FIELDS = {
    "confidence_threshold",
    "latency_budget_ms",
    "min_throughput_ips",
}


def canonical_json_hash(payload: dict[str, Any]) -> str:
    """Return the canonical SHA256 used for portable proof envelopes."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def edge_mission_package_identity_payload(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the stable mission/runtime package identity payload."""
    components: dict[str, Any] = {}
    for component_name in EDGE_MISSION_PACKAGE_IDENTITY_COMPONENTS:
        component = plan.get(component_name)
        if isinstance(component, dict) and component:
            components[component_name] = _edge_mission_package_identity_component(
                component_name,
                component,
            )
    return {
        "schema_version": EDGE_MISSION_PACKAGE_IDENTITY_SCHEMA_VERSION,
        "components": components,
    }


def edge_mission_package_identity_hash(plan: dict[str, Any]) -> str:
    """Return the stable identity hash shared by plan, download, and deploy intent."""
    return canonical_json_hash(edge_mission_package_identity_payload(plan))


def edge_mission_package_component_digests(plan: dict[str, Any]) -> dict[str, Any]:
    """Return stable digests for package-plan components handed to the edge."""
    digests: dict[str, Any] = {
        "schema_version": EDGE_MISSION_PACKAGE_COMPONENT_DIGESTS_SCHEMA_VERSION,
    }
    for component_name in (
        "mission",
        "selection",
        "slo",
        "model_handling",
        "ddil",
        "runtime_plan",
        "proof_gate",
        "deployment_intent",
        "edge_handoff",
        "edge_execution_contract",
        "runtime_workbench",
    ):
        component = plan.get(component_name)
        if isinstance(component, dict) and component:
            digests[f"{component_name}_sha256"] = canonical_json_hash(component)
    return digests


def hydrate_mission_spec_from_yaml(
    mission_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return mission spec fields, deriving missing package-planning values from YAML."""
    hydrated = dict(mission_spec or {})
    yaml_source = _mission_yaml_source(hydrated)
    if not yaml_source:
        return hydrated

    yaml_payload = _load_mission_yaml_payload(yaml_source)
    if not yaml_payload:
        return hydrated

    for field_name, yaml_paths in MISSION_PACKAGE_YAML_FIELD_PATHS.items():
        if hydrated.get(field_name) not in (None, ""):
            continue
        yaml_value = _mission_yaml_value(yaml_payload, *yaml_paths)
        if yaml_value in (None, "", [], {}):
            continue
        if field_name in MISSION_PACKAGE_YAML_FLOAT_FIELDS:
            numeric_value = _coerce_mission_yaml_float(yaml_value)
            if numeric_value is not None:
                hydrated[field_name] = numeric_value
            continue
        hydrated[field_name] = str(yaml_value)

    return hydrated


def build_edge_mission_package_plan(
    readiness: dict[str, Any],
    mission_spec: dict[str, Any] | None = None,
    *,
    proof_gate_failures: ProofGateFailures,
    capability_lock_for_proof_gate: CapabilityLockProvider,
    capability_lock_summary: CapabilityLockSummary,
    require_go: bool = True,
    min_runtime_fit: float | None = 95,
    require_best_runtime: bool = True,
    require_capability_lock: bool = True,
    require_proof_signature: bool = True,
) -> dict[str, Any]:
    """Build the mission-to-edge package plan from the readiness engine."""
    mission_spec = hydrate_mission_spec_from_yaml(mission_spec)
    readiness_selection = (
        readiness.get("selection")
        if isinstance(readiness.get("selection"), dict)
        else {}
    )
    edge_runtime_mission = (
        readiness.get("edge_runtime_mission")
        if isinstance(readiness.get("edge_runtime_mission"), dict)
        else {}
    )
    runtime_fit = (
        readiness.get("runtime_fit")
        if isinstance(readiness.get("runtime_fit"), dict)
        else {}
    )
    runtime_decision = (
        readiness.get("runtime_decision")
        if isinstance(readiness.get("runtime_decision"), dict)
        else {}
    )
    edge_execution_contract = (
        readiness.get("edge_execution_contract")
        if isinstance(readiness.get("edge_execution_contract"), dict)
        else {}
    )
    runtime_workbench = (
        readiness.get("runtime_workbench")
        if isinstance(readiness.get("runtime_workbench"), dict)
        else {}
    )
    mission_payload = edge_runtime_mission or readiness
    gate_failures = proof_gate_failures(
        "edge-runtime-mission",
        mission_payload,
        require_go=require_go,
        min_runtime_fit=min_runtime_fit,
        require_best_runtime=require_best_runtime,
        require_capability_lock=require_capability_lock,
        runtime_context=readiness,
    )
    gate_policy = _refs(
        {
            "require_go": require_go,
            "min_runtime_fit": min_runtime_fit,
            "require_best_runtime": require_best_runtime,
            "require_capability_lock": require_capability_lock,
            "require_proof_signature": require_proof_signature,
        }
    )
    selection = _refs(
        {
            "package_id": mission_spec.get("package_id"),
            "model_id": mission_spec.get("model_id"),
            "device_id": mission_spec.get("device_id"),
            "runtime_target_id": mission_spec.get("runtime_target_id"),
            "slot": mission_spec.get("slot"),
            **readiness_selection,
        }
    )
    yaml_source = _mission_yaml_source(mission_spec)
    mission = _refs(
        {
            "goal": mission_spec.get("goal"),
            "sensor": mission_spec.get("sensor"),
            "slot": mission_spec.get("slot") or selection.get("slot"),
            "source": "yaml" if yaml_source else "operator_form",
            "source_yaml": yaml_source,
            "source_yaml_sha256": hashlib.sha256(
                yaml_source.encode("utf-8")
            ).hexdigest()
            if yaml_source
            else None,
        }
    )
    target_selection = (
        runtime_fit.get("target_selection")
        if isinstance(runtime_fit.get("target_selection"), dict)
        else runtime_decision.get("target_selection")
        if isinstance(runtime_decision.get("target_selection"), dict)
        else edge_execution_contract.get("target_selection")
        if isinstance(edge_execution_contract.get("target_selection"), dict)
        else {}
    )
    capability_lock = capability_lock_for_proof_gate(readiness)
    runtime_plan = _refs(
        {
            "status": readiness.get("status"),
            "runtime_target_id": selection.get("runtime_target_id"),
            "runtime_fit_score": runtime_fit.get("score"),
            "runtime_fit_tier": runtime_fit.get("tier"),
            "target_selection": target_selection,
            "runtime_capability_lock": capability_lock_summary(capability_lock)
            if capability_lock
            else None,
            "recommended_action": edge_execution_contract.get("recommended_action")
            or runtime_decision.get("recommended_action"),
            "production_admission": readiness.get("production_admission"),
        }
    )
    selection_refs = _refs(selection)
    package_plan = {
        "schema_version": EDGE_MISSION_PACKAGE_SCHEMA_VERSION,
        "planned_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mission": mission,
        "selection": selection_refs,
        "slo": _refs(
            {
                "latency_budget_ms": _optional_float(
                    mission_spec.get("latency_budget_ms")
                ),
                "min_throughput_ips": _optional_float(
                    mission_spec.get("min_throughput_ips")
                    or mission_spec.get("throughput_min_ips")
                ),
            }
        ),
        "model_handling": _refs(
            {
                "switch_policy": mission_spec.get("switch_policy"),
                "confidence_threshold": _optional_float(
                    mission_spec.get("confidence_threshold")
                ),
                "fallback_model_id": mission_spec.get("fallback_model_id") or "auto",
            }
        ),
        "ddil": _refs(
            {
                "mode": mission_spec.get("ddil_mode") or "queue_signed_intents",
                "replay_requires_readiness": True,
                "proof_required": True,
            }
        ),
        "runtime_plan": runtime_plan,
        "proof_gate": {
            "status": "passed" if not gate_failures else "failed",
            "policy": gate_policy,
            "failures": gate_failures,
        },
        "readiness": _refs(
            {
                "schema_version": readiness.get("schema_version"),
                "status": readiness.get("status"),
                "headline": readiness.get("headline"),
                "next_action": readiness.get("next_action"),
                "checked_at": readiness.get("checked_at"),
            }
        ),
        "package": {
            "includes": [
                "mission_spec",
                "model_artifacts",
                "runtime_contract",
                "sensor_bindings",
                "model_switch_policy",
                "ddil_replay_policy",
                "edge_runtime_proof",
            ]
        },
    }
    package_identity_payload = edge_mission_package_identity_payload(package_plan)
    package_identity_components = sorted(
        package_identity_payload.get("components", {}).keys()
    )
    package_identity_sha256 = canonical_json_hash(package_identity_payload)
    package_plan["package_identity"] = {
        "schema_version": EDGE_MISSION_PACKAGE_IDENTITY_SCHEMA_VERSION,
        "package_identity_sha256": package_identity_sha256,
        "components": package_identity_components,
    }
    deployment_intent = _edge_mission_package_deployment_intent(
        selection_refs,
        mission_package_core_sha256=package_identity_sha256,
    )
    if deployment_intent:
        package_plan["deployment_intent"] = deployment_intent
        package_plan["edge_handoff"] = _edge_mission_package_edge_handoff(
            package_plan,
            deployment_intent,
            package_identity_sha256=package_identity_sha256,
        )
    if edge_execution_contract:
        package_plan["edge_execution_contract"] = edge_execution_contract
    if runtime_workbench:
        package_plan["runtime_workbench"] = runtime_workbench

    component_digests = edge_mission_package_component_digests(package_plan)
    if len(component_digests) > 1:
        package_plan["component_digests"] = component_digests
    package_plan["integrity"] = {
        "package_identity_sha256": package_identity_sha256,
        "payload_sha256": canonical_json_hash(package_plan),
    }
    return package_plan


def _edge_mission_package_identity_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _refs(
            {
                key: _edge_mission_package_identity_value(nested)
                for key, nested in value.items()
                if key not in EDGE_MISSION_PACKAGE_IDENTITY_TRANSIENT_KEYS
            }
        )
    if isinstance(value, list):
        return [
            _edge_mission_package_identity_value(item)
            for item in value
            if item not in (None, "", [], {})
        ]
    return value


def _edge_mission_package_identity_component(
    component_name: str,
    component: dict[str, Any],
) -> dict[str, Any]:
    if component_name != "runtime_plan":
        return _edge_mission_package_identity_value(component)

    target_selection = (
        component.get("target_selection")
        if isinstance(component.get("target_selection"), dict)
        else {}
    )
    capability_lock = (
        component.get("runtime_capability_lock")
        if isinstance(component.get("runtime_capability_lock"), dict)
        else {}
    )
    artifact_lane = (
        capability_lock.get("artifact_lane")
        if isinstance(capability_lock.get("artifact_lane"), dict)
        else {}
    )
    production_admission = (
        component.get("production_admission")
        if isinstance(component.get("production_admission"), dict)
        else {}
    )
    return _refs(
        {
            "status": component.get("status"),
            "runtime_target_id": component.get("runtime_target_id"),
            "runtime_fit_score": component.get("runtime_fit_score"),
            "runtime_fit_tier": component.get("runtime_fit_tier"),
            "target_selection": _refs(
                {
                    "schema_version": target_selection.get("schema_version"),
                    "status": target_selection.get("status"),
                    "selected_runtime_target_id": target_selection.get(
                        "selected_runtime_target_id"
                    ),
                    "best_runtime_target_id": target_selection.get(
                        "best_runtime_target_id"
                    ),
                    "selected_score": target_selection.get("selected_score"),
                    "best_score": target_selection.get("best_score"),
                    "score_delta": target_selection.get("score_delta"),
                    "selected_rank": target_selection.get("selected_rank"),
                    "eligible_target_count": target_selection.get(
                        "eligible_target_count"
                    ),
                    "candidate_count": target_selection.get("candidate_count"),
                }
            ),
            "runtime_capability_lock": _refs(
                {
                    "schema_version": capability_lock.get("schema_version"),
                    "status": capability_lock.get("status"),
                    "capability_sha256": capability_lock.get("capability_sha256"),
                    "runtime_target_id": capability_lock.get("runtime_target_id"),
                    "runtime_mode": capability_lock.get("runtime_mode"),
                    "artifact_lane": _edge_mission_package_identity_value(
                        artifact_lane
                    ),
                }
            ),
            "recommended_action": component.get("recommended_action"),
            "production_admission": _refs(
                {
                    "schema_version": production_admission.get("schema_version"),
                    "status": production_admission.get("status"),
                    "apply_allowed": production_admission.get("apply_allowed"),
                    "blocking_gate_count": production_admission.get(
                        "blocking_gate_count"
                    ),
                }
            ),
        }
    )


def _edge_mission_package_deployment_intent(
    selection: dict[str, Any],
    *,
    mission_package_core_sha256: str,
) -> dict[str, Any]:
    refs = _refs(
        {
            "package_id": selection.get("package_id"),
            "model_id": selection.get("model_id"),
            "device_id": selection.get("device_id"),
            "runtime_target_id": selection.get("runtime_target_id"),
            "slot": selection.get("slot"),
        }
    )
    if not refs.get("package_id") or not refs.get("device_id"):
        return {}
    rollout_id = _command_id(
        "rollout",
        refs,
        ["package_id", "model_id", "device_id", "runtime_target_id", "slot"],
    )
    body = _refs(
        {
            "rollout_id": rollout_id,
            "package_id": refs.get("package_id"),
            "model_id": refs.get("model_id"),
            "device_id": refs.get("device_id"),
            "runtime_target_id": refs.get("runtime_target_id"),
            "slot": refs.get("slot"),
            "require_approval": True,
            "require_runtime_validation": True,
            "actor": READINESS_REMEDIATION_ACTOR,
            "reason": f"mission package deployment handoff {mission_package_core_sha256[:12]}",
        }
    )
    return {
        "schema_version": "temms-edge-deployment-intent/v1",
        "mode": "stage_rollout",
        "rollout_id": rollout_id,
        "package_identity_sha256": mission_package_core_sha256,
        "mission_package_core_sha256": mission_package_core_sha256,
        "requires": {
            "approval": True,
            "runtime_validation": True,
            "edge_readiness": True,
        },
        "command": {
            "method": "POST",
            "path": "/v1/hub/rollouts",
            "body": body,
        },
    }


def _edge_mission_package_edge_handoff(
    package_plan: dict[str, Any],
    deployment_intent: dict[str, Any],
    *,
    package_identity_sha256: str,
) -> dict[str, Any]:
    """Return the package-to-edge runbook embedded in the artifact."""
    selection = (
        package_plan.get("selection")
        if isinstance(package_plan.get("selection"), dict)
        else {}
    )
    proof_gate = (
        package_plan.get("proof_gate")
        if isinstance(package_plan.get("proof_gate"), dict)
        else {}
    )
    rollout_id = str(deployment_intent.get("rollout_id") or "")
    if not rollout_id:
        return {}
    return {
        "schema_version": "temms-edge-mission-package-handoff/v1",
        "mode": "stage_approve_apply",
        "package_identity_sha256": package_identity_sha256,
        "selection": _refs(selection),
        "stage_gate": {
            "proof_gate": "passed",
            "package_identity": "verified",
            "deployment_intent": "verified",
            "current_proof_gate_status": proof_gate.get("status"),
        },
        "artifact_integrity": {
            "package_identity_sha256": package_identity_sha256,
            "payload_digest_header": "X-TEMMS-Mission-Package-SHA256",
            "identity_digest_header": "X-TEMMS-Mission-Package-Identity-SHA256",
            "deployment_intent_digest_header": (
                "X-TEMMS-Mission-Package-Deployment-Intent-SHA256"
            ),
        },
        "commands": {
            "stage_package": {
                "method": "POST",
                "path": "/v1/hub/mission-package/stage",
                "body": {"mission_package": "<temms-edge-mission-package/v1>"},
            },
            "create_rollout_intent": deployment_intent.get("command"),
            "approve_rollout": {
                "method": "POST",
                "path": f"/v1/hub/rollouts/{rollout_id}/approve",
                "body": {"actor": READINESS_REMEDIATION_ACTOR},
            },
            "apply_rollout": {
                "method": "POST",
                "path": f"/v1/hub/rollouts/{rollout_id}/apply",
                "body": {"actor": READINESS_REMEDIATION_ACTOR},
            },
        },
        "sequence": [
            "verify package identity and payload digest",
            "stage package artifact through /v1/hub/mission-package/stage",
            "approve rollout policy gate when required",
            "apply rollout on the target edge runtime",
            "export evidence or replay DDIL queue after field operation",
        ],
    }


def _command_id(kind: str, refs: dict[str, Any], keys: list[str]) -> str:
    payload = _refs({key: refs.get(key) for key in keys})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    return f"{READINESS_REMEDIATION_ID_PREFIX}-{kind}-{digest}"


def _mission_yaml_source(mission_spec: dict[str, Any]) -> str:
    return str(
        mission_spec.get("mission_yaml")
        or mission_spec.get("yaml")
        or mission_spec.get("source_yaml")
        or ""
    )


@lru_cache(maxsize=128)
def _load_mission_yaml_payload(mission_yaml: str) -> dict[str, Any]:
    try:
        import yaml

        payload = yaml.safe_load(mission_yaml)
    except Exception as exc:
        raise ValueError(f"Mission YAML could not be parsed: {exc}") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("Mission YAML must be a mapping object")
    return payload


def _mission_yaml_value(payload: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = payload
        for part in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = _mission_yaml_child(current, part)
            if current is None:
                break
        if current not in (None, "", [], {}):
            return current
    return None


def _mission_yaml_child(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    normalized_key = _normalize_mission_yaml_key(key)
    for candidate_key, candidate_value in payload.items():
        if _normalize_mission_yaml_key(str(candidate_key)) == normalized_key:
            return candidate_value
    return None


def _normalize_mission_yaml_key(key: str) -> str:
    return "".join(char for char in key.lower() if char.isalnum())


def _coerce_mission_yaml_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _refs(refs: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in refs.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
