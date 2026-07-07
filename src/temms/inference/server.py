"""
FastAPI inference server with per-slot endpoints.

Endpoints:
  POST /v1/slots/{slot_name}/infer     - Run inference on slot's active model
  GET  /v1/slots/{slot_name}/status    - Slot status
  GET  /v1/health                      - Health check
  GET  /v1/status                      - Full system status
  POST /v1/control/slots/{slot}/model  - Operator override
  POST /v1/control/conditions          - Inject conditions
"""

import json
import logging
import os
import socket
import time
from typing import Any, Dict, List, Optional
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Request, Body, Response
from prometheus_client import make_asgi_app
from pydantic import BaseModel, Field

from temms.controller import ActivationPreflightBlocked, AdaptiveInferenceController
from temms.slots.manager import SlotManager, SlotState
from temms.conditions.store import ConditionStore
from temms.policy.engine import PolicyEngine
from temms.core.cache import ModelCache
from temms.core.storage import ModelStorage
from temms.core.mission_package import hydrate_mission_spec_from_yaml
from temms.inference.runtime import InferenceRuntime
from temms.hub_lite import (
    EDGE_MISSION_PACKAGE_SCHEMA_VERSION,
    PackageArtifactIntegrityError,
    build_edge_mission_package_plan,
    build_edge_runtime_proof,
    canonical_json_hash,
    deployment_readiness_apply_blocking_gates,
    edge_mission_package_identity_hash,
)
from temms.daemon.pending_preflight import (
    RUNTIME_TARGET_ASSESSMENT_DIGEST_SCHEMA_VERSION,
    deploy_intent_context,
    pending_sync_preflight,
    runtime_target_assessment_sha256,
)
from temms.observability import (
    inference_request_count,
    inference_latency_ms,
    condition_update_count,
    deployment_count,
)

logger = logging.getLogger(__name__)

READINESS_REMEDIATION_ACTOR = "operator:readiness-remediation"


# ----- Request/Response Models -----


class InferenceResponse(BaseModel):
    """Response from inference endpoint."""

    slot: str
    model: str
    model_version: str
    predictions: List[Any]
    latency_ms: float
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class SlotStatusResponse(BaseModel):
    """Detailed slot status response."""

    name: str
    description: str
    state: str
    required: bool
    active_model: Optional[str]
    default_model: Optional[str]
    candidates: List[str]
    updated_at: str


class SystemStatusResponse(BaseModel):
    """Full system status."""

    status: str  # healthy, degraded, error
    slots: Dict[str, Dict[str, Any]]
    conditions_count: int
    policies_count: int
    uptime_seconds: float


class SlotOverrideRequest(BaseModel):
    """Request to override slot's model."""

    model: str
    reason: Optional[str] = None
    duration_s: Optional[int] = None  # None = permanent until cleared


class ConditionUpdateRequest(BaseModel):
    """Request to update conditions."""

    conditions: Dict[str, Any]  # path -> value


class SlotEvaluateRequest(BaseModel):
    """Request to evaluate local adaptive selection for one slot."""

    apply: bool = True


class ConditionUpdateResponse(BaseModel):
    """Response from condition update."""

    updated: List[str]
    timestamp: str


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    timestamp: str


class DeviceEnrollRequest(BaseModel):
    """Hub Lite device enrollment request."""

    device_id: Optional[str] = None
    profile: Optional[str] = None
    labels: Dict[str, str] = Field(default_factory=dict)
    inventory: Dict[str, Any] = Field(default_factory=dict)


class HeartbeatRequest(BaseModel):
    """Hub Lite heartbeat request."""

    status: str = "online"
    inventory: Dict[str, Any] = Field(default_factory=dict)
    deployment_status: Dict[str, Any] = Field(default_factory=dict)


class HubPackageRequest(BaseModel):
    """Hub Lite package catalog request."""

    package_id: str
    name: str
    version: str
    path: Optional[str] = None
    sha256: Optional[str] = None
    device_profiles: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    actor: Optional[str] = None


class HubPackageRegisterRequest(BaseModel):
    """Register a package artifact in the Hub Lite catalog."""

    package_path: str
    require_signature: bool = False
    signing_key: Optional[str] = None
    device_profiles: Optional[List[str]] = None
    sign: Optional[bool] = None
    signer: str = "temms-hub-lite"
    strict_metadata: bool = True
    actor: Optional[str] = None


class HubPackagePromotionRequest(BaseModel):
    """Promote a Hub Lite package through the deployment lifecycle."""

    state: str
    reason: Optional[str] = None
    actor: Optional[str] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)


class HubPackageFromMLflowRequest(BaseModel):
    """Build and register a Hub Lite package from an MLflow model URI."""

    model_uri: str
    slot: str
    policy_path: Optional[str] = None
    output_dir: Optional[str] = None
    tracking_uri: Optional[str] = None
    model_format: Optional[str] = None
    require_schema: bool = True
    require_runtime_constraints: bool = True
    device_profile: Optional[str] = None
    runtime_constraints: Dict[str, Any] = Field(default_factory=dict)
    runtime_options: Dict[str, Any] = Field(default_factory=dict)
    model_artifact_path: Optional[str] = None
    require_signature: bool = False
    signing_key: Optional[str] = None
    sign: Optional[bool] = None
    signer: str = "temms-hub-lite"
    strict_metadata: bool = True
    archive: bool = True
    overwrite: bool = False
    actor: Optional[str] = None


class RuntimeTargetRequest(BaseModel):
    """Register a Hub Lite container runtime target."""

    runtime_target_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    image: str
    registry: Optional[str] = None
    os: str = "linux"
    arch: Optional[str] = None
    device_profiles: List[str] = Field(default_factory=list)
    runtimes: Dict[str, Any] = Field(default_factory=dict)
    accelerators: Dict[str, Any] = Field(default_factory=dict)
    runtime_constraints: Dict[str, Any] = Field(default_factory=dict)
    labels: Dict[str, str] = Field(default_factory=dict)
    source: str = "byo"
    actor: Optional[str] = None


class HubCompatibilityPreviewRequest(BaseModel):
    """Preview whether a package can be deployed to a device/runtime target."""

    device_id: str
    package_id: str
    model_id: Optional[str] = None
    runtime_target_id: Optional[str] = None


class HubCompatibilityMatrixRequest(BaseModel):
    """Build a package/device/runtime compatibility matrix."""

    package_ids: Optional[List[str]] = None
    model_ids: Optional[List[str]] = None
    device_ids: Optional[List[str]] = None
    runtime_target_ids: Optional[List[str]] = None
    include_device_inventory: bool = False


class MissionPackagePlanRequest(BaseModel):
    """Plan the mission-to-edge package bundle for a selected runtime path."""

    package_id: Optional[str] = None
    model_id: Optional[str] = None
    device_id: Optional[str] = None
    runtime_target_id: Optional[str] = None
    slot: Optional[str] = None
    goal: Optional[str] = None
    mission_yaml: Optional[str] = None
    sensor: Optional[str] = None
    latency_budget_ms: Optional[float] = None
    min_throughput_ips: Optional[float] = None
    switch_policy: Optional[str] = None
    confidence_threshold: Optional[float] = None
    fallback_model_id: Optional[str] = None
    ddil_mode: Optional[str] = None
    require_go: bool = True
    min_runtime_fit: Optional[float] = 95
    require_best_runtime: bool = True
    require_capability_lock: bool = True
    require_proof_signature: bool = True


class MissionPackageStageRequest(BaseModel):
    """Stage the deployment intent embedded in a mission package artifact."""

    mission_package: Dict[str, Any]
    rollout_id: Optional[str] = None
    reason: Optional[str] = None
    actor: Optional[str] = None


class RuntimeValidationRecordRequest(BaseModel):
    """Record runtime target validation evidence in Hub Lite."""

    runtime_target_id: str
    result: Dict[str, Any]
    package_id: Optional[str] = None
    package_path: Optional[str] = None
    actor: Optional[str] = None


class BenchmarkRecordRequest(BaseModel):
    """Record hardware-aware benchmark evidence in Hub Lite."""

    result: Dict[str, Any]
    device_id: Optional[str] = None
    package_id: Optional[str] = None
    runtime_target_id: Optional[str] = None
    actor: Optional[str] = None


class RolloutAssignRequest(BaseModel):
    """Hub Lite rollout assignment request."""

    rollout_id: Optional[str] = None
    device_id: str
    package_id: str
    model_id: Optional[str] = None
    slot: Optional[str] = None
    runtime_target_id: Optional[str] = None
    require_runtime_validation: bool = False
    require_approval: bool = False
    reason: Optional[str] = None
    actor: Optional[str] = None


class RolloutPlanCreateRequest(BaseModel):
    """Create a coordinated Hub Lite rollout plan."""

    plan_id: Optional[str] = None
    package_id: str
    model_id: Optional[str] = None
    device_ids: List[str]
    slot: Optional[str] = None
    runtime_target_id: Optional[str] = None
    batch_size: int = 1
    require_runtime_validation: bool = False
    require_approval: bool = False
    reason: Optional[str] = None
    actor: Optional[str] = None


class RolloutPlanAdvanceRequest(BaseModel):
    """Advance a coordinated rollout plan by one batch."""

    limit: Optional[int] = None
    actor: Optional[str] = None


class RolloutPlanStateRequest(BaseModel):
    """Pause or resume a coordinated rollout plan."""

    reason: Optional[str] = None
    actor: Optional[str] = None


class RolloutApprovalRequest(BaseModel):
    """Approve a rollout before edge apply."""

    reason: Optional[str] = None
    actor: Optional[str] = None


class RolloutStatusRequest(BaseModel):
    """Hub Lite rollout status update request."""

    state: str
    detail: Optional[str] = None
    actor: Optional[str] = None


class RolloutApplyRequest(BaseModel):
    """Apply a Hub Lite rollout on this edge agent."""

    model_id: Optional[str] = None
    require_signature: bool = False
    signing_key: Optional[str] = None
    actor: Optional[str] = None
    actor: Optional[str] = None


class RolloutRollbackRequest(BaseModel):
    """Rollback a Hub Lite rollout on this edge agent."""

    reason: Optional[str] = None
    actor: Optional[str] = None


class TelemetryExportRequest(BaseModel):
    """Telemetry export request."""

    limit: Optional[int] = None


class TelemetryReplayRequest(BaseModel):
    """Telemetry replay request."""

    clear: bool = False


class HubTelemetryReplayRequest(BaseModel):
    """Hub Lite telemetry replay ingestion request."""

    bundle: Dict[str, Any]
    device_id: Optional[str] = None
    actor: Optional[str] = None


class HubEvidenceIngestRequest(BaseModel):
    """Hub Lite full evidence bundle ingestion request."""

    bundle: Dict[str, Any]
    device_id: Optional[str] = None
    actor: Optional[str] = None


class EvidenceExportRequest(BaseModel):
    """Evidence bundle export request."""

    telemetry_limit: Optional[int] = None
    decision_limit: int = 100
    include_benchmarks: bool = True
    summary: bool = False
    summary_limit: int = 20
    replay: bool = False
    replay_limit: int = 50


class AirgapExportRequest(BaseModel):
    """Hub Lite air-gap export request."""

    include_packages: bool = False


CONNECTIVITY_CONDITIONS = (
    "operational.connectivity.offline",
    "operational.connectivity.mode",
    "operational.connectivity.network_available",
)
CONNECTIVITY_CONDITION_PRIORITY = 900
CONNECTIVITY_CONDITION_SOURCE = "runtime_control"


# ----- Application State -----


class AppState:
    """Shared application state for dependency injection."""

    def __init__(
        self,
        slot_manager: SlotManager,
        condition_store: ConditionStore,
        policy_engine: PolicyEngine,
        model_cache: ModelCache,
        model_storage: ModelStorage,
        inference_runtime: "InferenceRuntime",
    ):
        self.slot_manager = slot_manager
        self.condition_store = condition_store
        self.policy_engine = policy_engine
        self.model_cache = model_cache
        self.model_storage = model_storage
        self.inference_runtime = inference_runtime
        self.start_time = time.time()
        self.offline_mode = False
        self.pending_operations = None
        self.deployment_state = None
        self.daemon_config = None
        self.api_token = None
        self.rbac_token_roles: dict[str, set[str]] = {}
        self.hub_lite = None
        self.telemetry = None
        self.controller = AdaptiveInferenceController(
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            inference_runtime=inference_runtime,
        )


# Global state - set during app creation
_app_state: Optional[AppState] = None


def get_state(request: Request) -> AppState:
    """Dependency to get app-scoped state."""
    app_state = getattr(request.app.state, "temms_state", None)
    if app_state is not None:
        return app_state
    if _app_state is None:
        raise RuntimeError("Application state not initialized")
    return _app_state


# ----- Application Factory -----


def create_app(
    slot_manager: SlotManager,
    condition_store: ConditionStore,
    policy_engine: PolicyEngine,
    model_cache: ModelCache,
    model_storage: ModelStorage,
    inference_runtime: "InferenceRuntime",
    offline_mode: bool = False,
    pending_operations: Any = None,
    deployment_state: Any = None,
    daemon_config: Any = None,
    api_token: Optional[str] = None,
    rbac_token_roles: Optional[dict[str, set[str]]] = None,
    hub_lite: Any = None,
    telemetry: Any = None,
) -> FastAPI:
    """
    Create FastAPI application with injected dependencies.

    Args:
        slot_manager: SlotManager instance
        condition_store: ConditionStore instance
        policy_engine: PolicyEngine instance
        model_cache: ModelCache instance
        model_storage: ModelStorage instance
        inference_runtime: InferenceRuntime instance

    Returns:
        Configured FastAPI application
    """
    global _app_state

    _app_state = AppState(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
        model_storage=model_storage,
        inference_runtime=inference_runtime,
    )
    _app_state.offline_mode = offline_mode
    _app_state.pending_operations = pending_operations
    _app_state.deployment_state = deployment_state
    _app_state.daemon_config = daemon_config
    _app_state.api_token = api_token or getattr(daemon_config, "api_token", None)
    _app_state.rbac_token_roles = (
        rbac_token_roles
        if rbac_token_roles is not None
        else (
            getattr(daemon_config, "rbac_token_roles", None)
            or parse_rbac_token_roles(os.environ.get("TEMMS_RBAC_TOKENS"))
        )
    )
    _app_state.hub_lite = hub_lite
    _app_state.telemetry = telemetry
    if hub_lite is not None:
        _app_state.controller.activation_preflight = (
            lambda **kwargs: _control_activation_preflight(_app_state, **kwargs)
        )
    if offline_mode:
        _record_connectivity_conditions(
            _app_state,
            offline=True,
            source="startup_offline",
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Application lifespan management."""
        logger.info("TEMMS inference server starting")
        yield
        logger.info("TEMMS inference server shutting down")

    application = FastAPI(
        title="TEMMS",
        description="Tactical Edge Model Management System - Inference API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.temms_state = _app_state

    metrics_app = make_asgi_app()
    application.mount("/metrics", metrics_app)

    # Register API routes
    application.include_router(inference_router)
    application.include_router(control_router)
    application.include_router(hub_router)
    application.include_router(status_router)

    # Register Web UI routes
    try:
        from temms.ui.routes import create_ui_router

        ui_router = create_ui_router(get_state, control_auth_dependency=require_control_auth)
        application.include_router(ui_router)
        logger.info("Web UI registered at /ui/")
    except Exception as e:
        logger.warning(f"Could not load Web UI: {e}")

    return application


# ----- Routers -----

from fastapi import APIRouter

inference_router = APIRouter(prefix="/v1", tags=["inference"])
status_router = APIRouter(prefix="/v1", tags=["status"])


def require_control_auth(request: Request, state: AppState = Depends(get_state)) -> None:
    """Require a configured bearer or X-TEMMS-Token for control-plane writes."""
    expected = state.api_token
    rbac_tokens = state.rbac_token_roles or {}
    if not expected and not rbac_tokens:
        return

    supplied = _control_token_from_request(request)

    if not (expected and supplied == expected) and supplied not in rbac_tokens:
        raise HTTPException(status_code=401, detail="Invalid or missing control token")


def _control_token_from_request(request: Request) -> str | None:
    supplied = request.headers.get("x-temms-token")
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        supplied = auth_header.split(" ", 1)[1]
    return supplied


def parse_rbac_token_roles(raw: str | None) -> dict[str, set[str]]:
    """Parse optional role-scoped API tokens from env/config."""
    if raw is None or not raw.strip():
        return {}

    value = raw.strip()
    if value.startswith("{"):
        parsed = json.loads(value)
        token_roles: dict[str, set[str]] = {}
        for token, roles in parsed.items():
            normalized = _normalize_roles(roles)
            if token and normalized:
                token_roles[str(token)] = normalized
        return token_roles

    token_roles: dict[str, set[str]] = {}
    for entry in value.replace(";", ",").split(","):
        if not entry.strip():
            continue
        if "=" in entry:
            role_text, token = entry.split("=", 1)
        elif ":" in entry:
            role_text, token = entry.split(":", 1)
        else:
            raise ValueError("TEMMS_RBAC_TOKENS entries must use role=token, role:token, or JSON")
        token = token.strip()
        roles = _normalize_roles(role_text.replace("+", ","))
        if token and roles:
            token_roles.setdefault(token, set()).update(roles)
    return token_roles


def _normalize_roles(value: Any) -> set[str]:
    if isinstance(value, str):
        items = value.replace("|", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        return set()
    return {item.strip().lower() for item in items if item and item.strip()}


def request_control_roles(request: Request, state: AppState) -> set[str]:
    """Return the roles attached to the submitted control token."""
    token = _control_token_from_request(request)
    roles: set[str] = set()
    if state.api_token and token == state.api_token:
        roles.add("admin")
    roles.update((state.rbac_token_roles or {}).get(token or "", set()))
    return roles


def require_rbac_role(request: Request, state: AppState, *allowed_roles: str) -> None:
    """Require one of the supplied roles when opt-in RBAC tokens are configured."""
    if not state.rbac_token_roles:
        return
    roles = request_control_roles(request, state)
    allowed = {role.lower() for role in allowed_roles}
    if "admin" in roles or roles & allowed:
        return
    required = ", ".join(sorted(allowed))
    raise HTTPException(status_code=403, detail=f"Requires one of roles: {required}")


def request_actor(request: Request, explicit: Optional[str] = None, default: str = "api") -> str:
    """Return a non-secret actor label for audit history."""
    actor = (
        explicit or request.headers.get("x-temms-actor") or request.headers.get("x-forwarded-user")
    )
    if not actor:
        return default
    return actor.strip()[:128] or default


def rollout_signature_policy(
    state: AppState,
    require_signature: bool = False,
    signing_key: Optional[str] = None,
    resolve_key: bool = True,
) -> tuple[bool, Optional[str]]:
    """Resolve package signature policy from the request and daemon config."""
    from temms.core.signing import read_signing_key

    daemon_config = state.daemon_config
    daemon_requires_signature = bool(getattr(daemon_config, "rollout_require_signature", False))
    effective_require_signature = bool(require_signature or daemon_requires_signature)

    if signing_key:
        return effective_require_signature, signing_key

    if daemon_config is None or not resolve_key:
        return effective_require_signature, None

    effective_key = read_signing_key(
        getattr(daemon_config, "rollout_signing_key", None),
        getattr(daemon_config, "rollout_signing_key_file", None),
    )
    return effective_require_signature, effective_key


def _pending_operation_signing_key(state: AppState) -> Optional[str]:
    """Return the daemon signing key for DDIL pending-operation signatures."""
    _require_signature, signing_key = rollout_signature_policy(state)
    return signing_key


def _enqueue_pending_operation(
    state: AppState,
    operation: str,
    payload: Dict[str, Any],
    *,
    signer: str = "temms-ddil",
) -> None:
    if state.pending_operations is None:
        return
    state.pending_operations.enqueue(
        operation,
        payload,
        signing_key=_pending_operation_signing_key(state),
        signer=signer,
    )


def _normalize_payload_sha256(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("sha256:"):
        return text.removeprefix("sha256:").strip()
    return text


def _runtime_retarget_audit(payload: Dict[str, Any]) -> Dict[str, Any]:
    records = [
        record
        for record in payload.get("_temms_runtime_retarget", [])
        if isinstance(record, dict)
    ]
    if not records:
        return {}
    latest = records[-1]
    return {
        "schema_version": "temms-ddil-runtime-retarget-audit/v1",
        "latest": latest,
        "records": records,
        "retargeted_at": latest.get("retargeted_at"),
        "actor": latest.get("actor"),
        "reason": latest.get("reason"),
        "previous_runtime_target_id": latest.get("previous_runtime_target_id"),
        "runtime_target_id": latest.get("runtime_target_id"),
        "previous_payload_sha256": latest.get("previous_payload_sha256"),
    }


def package_signature_verified(package: Optional[Dict[str, Any]]) -> bool:
    """Return whether a Hub Lite catalog entry carries verified signature metadata."""
    if not package:
        return False
    metadata = package.get("metadata", {})
    validation = metadata.get("validation", {})
    if isinstance(validation, dict) and validation.get("signature_verified") is True:
        return True
    return metadata.get("signature_verified") is True


def package_strict_metadata_verified(package: Optional[Dict[str, Any]]) -> bool:
    """Return whether a catalog entry was validated with production metadata checks."""
    if not package:
        return False
    metadata = package.get("metadata", {})
    validation = metadata.get("validation", {})
    if isinstance(validation, dict):
        return validation.get("strict_metadata") is True and validation.get("valid", True) is True
    return metadata.get("strict_metadata") is True


def _enrich_readiness_with_evidence(
    readiness: Dict[str, Any],
    summary: Dict[str, Any],
    mission_replay: Dict[str, Any],
) -> Dict[str, Any]:
    """Append runtime evidence gates and recompute the deployment verdict."""
    gates = list(readiness.get("gates") or [])
    gates.extend(
        [
            _ddil_readiness_gate(summary),
            _evidence_chain_readiness_gate(summary, mission_replay),
        ]
    )
    enriched = _finalize_readiness_response({**readiness, "gates": gates})
    return _enrich_edge_runtime_mission(enriched, summary, mission_replay)


def _enrich_edge_runtime_mission(
    readiness: Dict[str, Any],
    summary: Dict[str, Any],
    mission_replay: Dict[str, Any],
) -> Dict[str, Any]:
    mission = _dict_of(readiness.get("edge_runtime_mission"))
    if not mission:
        return readiness
    metrics = dict(_dict_of(mission.get("metrics")))
    ddil_repair = _edge_runtime_ddil_repair_metric(summary, mission_replay)
    metrics["ddil_repair"] = ddil_repair
    operator_focus = list(mission.get("operator_focus") or [])
    if ddil_repair.get("status") != "go":
        repair_focus = f"{ddil_repair.get('state')}: {ddil_repair.get('detail')}"
        if repair_focus not in operator_focus:
            operator_focus.append(repair_focus)
    updated_mission = {
        **mission,
        "status": readiness.get("status"),
        "headline": _edge_runtime_mission_headline(str(readiness.get("status") or "")),
        "detail": _edge_runtime_mission_detail(readiness, mission),
        "next_action": readiness.get("next_action"),
        "metrics": metrics,
        "operator_focus": operator_focus[:4],
    }
    return {**readiness, "edge_runtime_mission": updated_mission}


def _edge_runtime_ddil_repair_metric(
    summary: Dict[str, Any],
    mission_replay: Dict[str, Any],
) -> Dict[str, Any]:
    runtime = _dict_of(summary.get("runtime"))
    preflight = _dict_of(runtime.get("pending_operation_preflight"))
    pending = _int_of(runtime.get("pending_operations_count"))
    replay_blocked = _int_of(preflight.get("blocked"))
    pending_records = [
        record for record in runtime.get("pending_operations") or [] if isinstance(record, dict)
    ]
    repair_candidate = next(
        (
            record
            for record in pending_records
            if record.get("runtime_remediation_runtime_target_id")
        ),
        None,
    )
    if repair_candidate:
        previous = repair_candidate.get("runtime_remediation_previous_runtime_target_id")
        target = repair_candidate.get("runtime_remediation_runtime_target_id")
        delta = repair_candidate.get("runtime_remediation_score_delta")
        return _readiness_refs(
            {
                "status": "attention",
                "state": "repair available",
                "detail": _runtime_repair_detail(previous, target, delta),
                "previous_runtime_target_id": previous,
                "runtime_target_id": target,
                "score_delta": delta,
                "payload_sha256": repair_candidate.get("payload_sha256"),
            }
        )
    if replay_blocked:
        return {
            "status": "blocked",
            "state": "blocked replay",
            "detail": (
                f"{replay_blocked} queued runtime intent"
                f"{'' if replay_blocked == 1 else 's'} blocked by preflight"
            ),
        }
    retarget_proof = _latest_runtime_retarget_replay_proof(mission_replay)
    if retarget_proof:
        return {
            "status": "go",
            "state": "retarget proved",
            "detail": retarget_proof,
        }
    if pending:
        return {
            "status": "attention",
            "state": "queued",
            "detail": f"{pending} signed intent{'' if pending == 1 else 's'} awaiting sync",
        }
    return {
        "status": "go",
        "state": "clear",
        "detail": "No runtime repair pending",
    }


def _runtime_repair_detail(previous: Any, target: Any, delta: Any) -> str:
    path = f"{previous} -> {target}" if previous else str(target or "runtime target")
    return f"{path}{f' (+{delta} fit)' if delta is not None else ''}"


def _latest_runtime_retarget_replay_proof(mission_replay: Dict[str, Any]) -> str:
    for event in mission_replay.get("events") or []:
        if not isinstance(event, dict):
            continue
        summary = str(event.get("summary") or "")
        detail = str(event.get("detail") or "")
        if event.get("runtime_retargeted") is True or "DDIL replay retargeted" in summary:
            return _runtime_retarget_proof_text(summary, detail)
        if detail.startswith("retargeted "):
            return _runtime_retarget_proof_text(summary, detail)
    for phase in mission_replay.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        summary = str(phase.get("summary") or "")
        if phase.get("phase") == "offline_operation" and "retargeted" in summary:
            return summary
    return ""


def _runtime_retarget_proof_text(summary: str, detail: str) -> str:
    if not summary:
        return detail
    if not detail:
        return summary
    detail_path = detail.removeprefix("retargeted ").strip()
    if detail_path and detail_path in summary:
        return summary
    return f"{summary}; {detail}"


def _edge_runtime_mission_headline(status: str) -> str:
    if status == "go":
        return "Selected model is proven for the edge path"
    if status == "blocked":
        return "Selected edge path is blocked"
    if status == "attention":
        return "Selected edge path needs operator proof"
    return "Selected edge path needs review"


def _edge_runtime_mission_detail(
    readiness: Dict[str, Any],
    mission: Dict[str, Any],
) -> str:
    path = _dict_of(mission.get("path"))
    label = str(path.get("label") or "selected edge path")
    if readiness.get("status") == "go":
        return f"{label} satisfies runtime, artifact, SLO, resource, DDIL, and evidence gates"
    return f"{readiness.get('headline')}: {label}"


def _ddil_readiness_gate(summary: Dict[str, Any]) -> Dict[str, Any]:
    runtime = _dict_of(summary.get("runtime"))
    verification = _dict_of(runtime.get("pending_operation_verification"))
    preflight = _dict_of(runtime.get("pending_operation_preflight"))
    pending = _int_of(runtime.get("pending_operations_count"))
    invalid = _int_of(verification.get("invalid"))
    replay_blocked = _int_of(preflight.get("blocked"))
    optimization_advisories = _int_of(preflight.get("optimization_advisories"))
    unresolved_dead_letters = _int_of(
        runtime.get("pending_operation_dead_letters_unresolved_count")
    )
    unsafe = invalid + replay_blocked
    refs = _ddil_readiness_refs(runtime)
    if unsafe:
        return _readiness_gate(
            "ddil_queue",
            "DDIL queue",
            "blocked",
            "blocked",
            f"{unsafe} unsafe intent{'s' if unsafe != 1 else ''} need quarantine or review",
            refs=refs,
            actions=[
                _readiness_action(
                    "quarantine_blocked_ddil",
                    "Quarantine blocked intents",
                    "quarantine_blocked",
                    refs=refs,
                )
            ],
        )
    if pending:
        offline = bool(runtime.get("offline_mode"))
        detail = f"{pending} signed intent{'s' if pending != 1 else ''} waiting for reconciliation"
        if optimization_advisories:
            detail += (
                f"; {optimization_advisories} runtime optimization "
                f"advisor{'ies' if optimization_advisories != 1 else 'y'}"
            )
        return _readiness_gate(
            "ddil_queue",
            "DDIL queue",
            "attention",
            (
                "runtime advisory"
                if optimization_advisories
                else ("offline queued" if offline else "pending replay")
            ),
            detail,
            refs=refs,
            actions=[
                _readiness_action(
                    "sync_pending_ddil",
                    "Sync pending intents",
                    "sync_pending",
                    refs=refs,
                )
            ],
        )
    if unresolved_dead_letters:
        return _readiness_gate(
            "ddil_queue",
            "DDIL queue",
            "attention",
            "quarantined",
            (
                f"{unresolved_dead_letters} unresolved quarantined intent"
                f"{'s' if unresolved_dead_letters != 1 else ''}"
            ),
            refs=refs,
            actions=[
                _readiness_action(
                    "requeue_quarantined_ddil",
                    "Requeue quarantine",
                    "requeue_dead_letters",
                    refs=refs,
                ),
                _readiness_action(
                    "acknowledge_quarantined_ddil",
                    "Acknowledge quarantine",
                    "acknowledge_dead_letters",
                    refs=refs,
                )
            ],
        )
    if runtime.get("offline_mode"):
        return _readiness_gate(
            "ddil_queue",
            "DDIL queue",
            "attention",
            "offline",
            "Link is intentionally offline with no queued intents",
            refs=refs,
            actions=[
                _readiness_action(
                    "restore_connectivity",
                    "Restore link",
                    "restore_online",
                    refs=refs,
                )
            ],
        )
    return _readiness_gate(
        "ddil_queue",
        "DDIL queue",
        "go",
        "clear",
        "No pending or blocked DDIL intents",
    )


def _evidence_chain_readiness_gate(
    summary: Dict[str, Any],
    mission_replay: Dict[str, Any],
) -> Dict[str, Any]:
    outcome = _dict_of(mission_replay.get("outcome"))
    phases = mission_replay.get("phases") if isinstance(mission_replay.get("phases"), list) else []
    incomplete = (
        outcome.get("incomplete_phases")
        if isinstance(outcome.get("incomplete_phases"), list)
        else []
    )
    completed = _int_of(outcome.get("completed_phases"))
    counts = _dict_of(summary.get("counts"))
    trust = _dict_of(summary.get("trust"))
    proof_events = _int_of(counts.get("timeline_entries"))
    signed_imports = _int_of(trust.get("signed_package_imports"))
    refs = _evidence_readiness_refs(
        proof_events=proof_events,
        signed_imports=signed_imports,
        completed=completed,
        total_phases=len(phases),
        incomplete=incomplete,
    )
    if phases and not incomplete and completed == len(phases):
        return _readiness_gate(
            "evidence_chain",
            "Evidence chain",
            "go",
            "complete",
            f"{completed} replay phases complete",
            refs=refs,
        )
    if proof_events or signed_imports:
        return _readiness_gate(
            "evidence_chain",
            "Evidence chain",
            "attention",
            "partial",
            f"{proof_events or signed_imports} proof events with signed package evidence",
            refs=refs,
            actions=[
                _readiness_action(
                    "export_mission_replay",
                    "Export mission replay",
                    "export_replay",
                    refs=refs,
                )
            ],
        )
    return _readiness_gate(
        "evidence_chain",
        "Evidence chain",
        "attention",
        "missing",
        "Generate mission proof after rollout or DDIL activity",
        refs=refs,
        actions=[
            _readiness_action(
                "export_mission_replay",
                "Export mission replay",
                "export_replay",
                refs=refs,
            )
        ],
    )


def _ddil_readiness_refs(runtime: Dict[str, Any]) -> Dict[str, Any]:
    verification = _dict_of(runtime.get("pending_operation_verification"))
    preflight = _dict_of(runtime.get("pending_operation_preflight"))
    pending_records = runtime.get("pending_operations")
    dead_letter_records = runtime.get("pending_operation_dead_letters")
    pending_operations = [
        _dict_of(operation)
        for operation in (pending_records if isinstance(pending_records, list) else [])
        if isinstance(operation, dict)
    ]
    dead_letters = [
        _dict_of(operation)
        for operation in (dead_letter_records if isinstance(dead_letter_records, list) else [])
        if isinstance(operation, dict)
    ]
    unresolved_dead_letters = [
        operation for operation in dead_letters if operation.get("acknowledged") is not True
    ]
    return _readiness_refs(
        {
            "offline_mode": bool(runtime.get("offline_mode")),
            "pending_operations": _int_of(runtime.get("pending_operations_count")),
            "pending_operation_types": runtime.get("pending_operation_types")
            if isinstance(runtime.get("pending_operation_types"), list)
            else [],
            "verified_intents": _int_of(verification.get("verified")),
            "invalid_intents": _int_of(verification.get("invalid")),
            "replay_ready_intents": _int_of(preflight.get("ready")),
            "replay_blocked_intents": _int_of(preflight.get("blocked")),
            "superseded_intents": _int_of(preflight.get("superseded")),
            "runtime_optimization_advisories": _int_of(
                preflight.get("optimization_advisories")
            ),
            "unresolved_dead_letters": len(unresolved_dead_letters),
            "pending_operation_hashes": _readiness_payload_hashes(pending_operations),
            "dead_letter_hashes": _readiness_payload_hashes(unresolved_dead_letters),
        }
    )


def _evidence_readiness_refs(
    *,
    proof_events: int,
    signed_imports: int,
    completed: int,
    total_phases: int,
    incomplete: list[Any],
) -> Dict[str, Any]:
    return _readiness_refs(
        {
            "proof_events": proof_events,
            "signed_package_imports": signed_imports,
            "completed_phases": completed,
            "total_phases": total_phases,
            "incomplete_phases": [
                str(phase) for phase in incomplete if phase is not None and phase != ""
            ],
            "export_mode": "replay",
        }
    )


def _readiness_payload_hashes(records: list[Dict[str, Any]], limit: int = 5) -> list[str]:
    hashes: list[str] = []
    for record in records:
        digest = record.get("payload_sha256")
        if digest:
            hashes.append(str(digest))
        if len(hashes) >= limit:
            break
    return hashes


def _readiness_refs(refs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in refs.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _readiness_action(
    action_id: str,
    label: str,
    kind: str,
    *,
    refs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    action_refs = _readiness_refs(refs or {})
    action = {
        "action_id": action_id,
        "label": label,
        "kind": kind,
        "refs": action_refs,
    }
    command = _readiness_action_command(kind, action_refs)
    if command:
        action["command"] = command
    return action


def _readiness_action_command(
    kind: str,
    refs: Dict[str, Any],
) -> Dict[str, Any] | None:
    if kind == "restore_online":
        return _readiness_command("POST", "/v1/control/online")
    if kind == "sync_pending":
        return _readiness_command("POST", "/v1/control/sync")
    if kind == "quarantine_blocked":
        return _readiness_command(
            "POST",
            "/v1/control/sync/quarantine-blocked",
            {
                "actor": READINESS_REMEDIATION_ACTOR,
                "reason": "readiness gate quarantine",
            },
        )
    if kind == "acknowledge_dead_letters":
        return _readiness_command(
            "POST",
            "/v1/control/sync/acknowledge-dead-letters",
            {
                "actor": READINESS_REMEDIATION_ACTOR,
                "reason": "readiness gate acknowledgement",
            },
        )
    if kind == "requeue_dead_letters":
        return _readiness_command(
            "POST",
            "/v1/control/sync/requeue-dead-letters",
            {
                "actor": READINESS_REMEDIATION_ACTOR,
                "reason": "readiness gate requeue",
                "require_ready": True,
            },
        )
    if kind == "export_replay":
        return _readiness_command(
            "POST",
            "/v1/hub/evidence/export",
            {
                "replay": True,
                "replay_limit": max(_int_of(refs.get("total_phases")), 50),
            },
        )
    return None


def _readiness_command(
    method: str,
    path: str,
    body: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    command: Dict[str, Any] = {"method": method, "path": path}
    if body is not None:
        command["body"] = _readiness_refs(body)
    return command


def _readiness_gate(
    gate_id: str,
    label: str,
    status: str,
    state: str,
    detail: str,
    *,
    refs: Dict[str, Any] | None = None,
    actions: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "gate_id": gate_id,
        "label": label,
        "status": status,
        "state": state,
        "detail": detail,
        "refs": _readiness_refs(refs or {}),
        "actions": actions or [],
    }


def _finalize_readiness_response(readiness: Dict[str, Any]) -> Dict[str, Any]:
    gates = [gate for gate in readiness.get("gates", []) if isinstance(gate, dict)]
    summary = {
        "go": sum(1 for gate in gates if gate.get("status") == "go"),
        "attention": sum(1 for gate in gates if gate.get("status") == "attention"),
        "blocked": sum(1 for gate in gates if gate.get("status") == "blocked"),
    }
    blocker = _first_gate_with_status(gates, "blocked")
    warning = _first_gate_with_status(gates, "attention")
    if blocker is not None:
        status = "blocked"
        headline = "Deployment is blocked"
        detail = "One or more safety gates prevent field rollout for the selected model."
        next_action = str(blocker.get("detail") or "Resolve the blocked gate")
    elif warning is not None:
        status = "attention"
        headline = "Deployment is stageable with operator action"
        detail = (
            "The selected model has no hard blockers, but one runtime, resource, "
            "performance, or proof gate still needs review."
        )
        next_action = str(warning.get("detail") or "Review the attention gate")
    else:
        status = "go"
        headline = "Deployment loop is ready"
        detail = (
            "Model package, runtime target, performance SLO, resource envelope, "
            "edge target, rollout, DDIL queue, and evidence chain are aligned."
        )
        next_action = "Export mission replay or stage the next rollout batch"
    return {
        **readiness,
        "status": status,
        "headline": headline,
        "detail": detail,
        "next_action": next_action,
        "summary": summary,
        "gates": gates,
        "actions": _readiness_actions(gates),
    }


def _readiness_actions(gates: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    actions: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for gate in gates:
        if gate.get("status") == "go":
            continue
        for action in gate.get("actions") or []:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("action_id") or action.get("kind") or "")
            if not action_id or action_id in seen:
                continue
            seen.add(action_id)
            actions.append({**action, "gate_id": gate.get("gate_id")})
    return actions


def _first_gate_with_status(
    gates: list[Dict[str, Any]],
    status: str,
) -> Dict[str, Any] | None:
    return next((gate for gate in gates if gate.get("status") == status), None)


def _dict_of(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_of(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def emit_telemetry(
    state: AppState,
    event_type: str,
    payload: Dict[str, Any],
    source: str = "api",
) -> None:
    """Best-effort telemetry append."""
    if state.telemetry is None:
        return
    try:
        state.telemetry.append(event_type, payload, source=source)
    except Exception as exc:
        logger.warning("Failed to append telemetry event %s: %s", event_type, exc)


def _rollout_apply_preflight(
    state: AppState,
    hub: Any,
    rollout: Dict[str, Any],
    *,
    package_id: str,
    model_id: str | None,
    actor: str,
) -> Dict[str, Any]:
    """Fail closed before edge activation when runtime or edge evidence is stale."""
    runtime_target_id = rollout.get("runtime_target_id")
    try:
        readiness = hub.deployment_readiness(
            package_id=package_id,
            model_id=model_id,
            device_id=rollout.get("device_id"),
            runtime_target_id=runtime_target_id,
            slot=rollout.get("slot"),
        )
    except ValueError as exc:
        detail = {
            "message": "Rollout apply preflight failed",
            "rollout_id": rollout.get("rollout_id"),
            "package_id": package_id,
            "model_id": model_id,
            "errors": [str(exc)],
        }
        emit_telemetry(
            state,
            "rollout.apply_preflight_blocked",
            {**detail, "actor": actor},
        )
        raise HTTPException(status_code=409, detail=detail) from exc

    blocking_gates = deployment_readiness_apply_blocking_gates(
        readiness,
        runtime_target_id=str(runtime_target_id) if runtime_target_id else None,
    )

    if blocking_gates:
        detail = {
            "message": "Rollout apply preflight failed",
            "rollout_id": rollout.get("rollout_id"),
            "package_id": package_id,
            "model_id": model_id,
            "runtime_target_id": runtime_target_id,
            "blocking_gates": blocking_gates,
            "readiness": readiness,
        }
        emit_telemetry(
            state,
            "rollout.apply_preflight_blocked",
            {
                "rollout_id": rollout.get("rollout_id"),
                "package_id": package_id,
                "model_id": model_id,
                "runtime_target_id": runtime_target_id,
                "blocking_gates": blocking_gates,
                "actor": actor,
            },
        )
        raise HTTPException(status_code=409, detail=detail)

    return readiness


def _control_activation_preflight(
    state: AppState,
    *,
    slot_name: str,
    model_id: str,
    trigger_type: str,
    trigger_detail: str,
    conditions: Dict[str, Any],
) -> Dict[str, Any] | None:
    """Admission-control adaptive activations when Hub Lite has edge context."""
    hub = state.hub_lite
    if hub is None:
        return None
    model = state.model_cache.get_model(model_id)
    if model is None or not model.package_id:
        return None
    if hub.get_package(model.package_id) is None:
        return None

    device_id = getattr(state.daemon_config, "hub_device_id", None) or socket.gethostname()
    if hub.get_device(device_id) is None:
        devices = hub.list_devices()
        if len(devices) != 1:
            return None
        device_id = str(devices[0].get("device_id") or "")
    if not device_id:
        return None

    try:
        readiness = hub.deployment_readiness(
            package_id=model.package_id,
            model_id=model.id,
            device_id=device_id,
            slot=slot_name,
        )
    except ValueError as exc:
        raise ActivationPreflightBlocked(str(exc)) from exc

    blocking_gates = _control_activation_blocking_gates(readiness)
    summary = {
        "schema_version": "temms-activation-preflight/v1",
        "status": readiness.get("status"),
        "selection": readiness.get("selection"),
        "checked_at": readiness.get("checked_at"),
    }
    if not blocking_gates:
        return summary

    emit_telemetry(
        state,
        "slot.activation_preflight_blocked",
        {
            "slot": slot_name,
            "model_id": model.id,
            "package_id": model.package_id,
            "device_id": device_id,
            "trigger_type": trigger_type,
            "trigger_detail": trigger_detail,
            "conditions": conditions,
            "blocking_gates": blocking_gates,
            "blocking_gate_count": len(blocking_gates),
            "readiness_status": readiness.get("status"),
            "readiness_selection": readiness.get("selection"),
        },
    )
    raise ActivationPreflightBlocked(
        "activation preflight blocked: " + _control_gate_summary(blocking_gates),
        readiness=readiness,
        blocking_gates=blocking_gates,
    )


def _control_activation_blocking_gates(readiness: Dict[str, Any]) -> list[Dict[str, Any]]:
    blocked_gate_ids = {
        "model_package",
        "runtime_target",
        "performance_fit",
        "resource_envelope",
        "edge_target",
    }
    attention_gate_ids = {"performance_fit", "resource_envelope", "edge_target"}
    blocking: list[Dict[str, Any]] = []
    for gate in readiness.get("gates") or []:
        if not isinstance(gate, dict):
            continue
        gate_id = str(gate.get("gate_id") or "")
        status = str(gate.get("status") or "")
        should_block = (
            status == "blocked" and gate_id in blocked_gate_ids
        ) or (
            status == "attention" and gate_id in attention_gate_ids
        )
        if not should_block:
            continue
        blocking.append(
            {
                "gate_id": gate_id,
                "label": gate.get("label"),
                "status": gate.get("status"),
                "state": gate.get("state"),
                "detail": gate.get("detail"),
                "refs": gate.get("refs") if isinstance(gate.get("refs"), dict) else {},
            }
        )
    return blocking


def _control_gate_summary(gates: list[Dict[str, Any]]) -> str:
    parts = [
        f"{gate.get('label') or gate.get('gate_id')} {gate.get('state')}: {gate.get('detail')}"
        for gate in gates[:3]
    ]
    remaining = len(gates) - len(parts)
    if remaining > 0:
        parts.append(f"{remaining} more gate{'s' if remaining != 1 else ''}")
    return "; ".join(parts)


def _control_activation_preflight_or_409(
    state: AppState,
    *,
    slot_name: str,
    model_id: str,
    trigger_type: str,
    trigger_detail: str,
    conditions: Dict[str, Any],
) -> Dict[str, Any] | None:
    """Run activation preflight for control-plane activations or raise HTTP 409."""
    try:
        return _control_activation_preflight(
            state,
            slot_name=slot_name,
            model_id=model_id,
            trigger_type=trigger_type,
            trigger_detail=trigger_detail,
            conditions=conditions,
        )
    except ActivationPreflightBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Activation preflight failed",
                "slot": slot_name,
                "model_id": model_id,
                "trigger_type": trigger_type,
                "trigger_detail": trigger_detail,
                "blocking_gates": exc.blocking_gates,
                "readiness": exc.readiness,
            },
        ) from exc


def _preserve_pending_replay_remainder(
    state: AppState,
    entries: list[Dict[str, Any]],
    failed_index: int,
    *,
    replayed: int,
    skipped: int,
    error: Exception,
) -> None:
    """Drop consumed DDIL entries after a partial sync failure."""
    store = state.pending_operations
    replace_all = getattr(store, "replace_all", None)
    if not callable(replace_all):
        return
    remaining = entries[failed_index:]
    try:
        replace_all(remaining)
    except Exception as cleanup_error:
        logger.warning(
            "Failed to preserve pending replay remainder after index %s: %s",
            failed_index,
            cleanup_error,
        )
        return
    emit_telemetry(
        state,
        "pending_operations.partial_replay_failed",
        {
            "failed_index": failed_index,
            "replayed": replayed,
            "skipped": skipped,
            "consumed": failed_index,
            "remaining": len(remaining),
            "error": str(error),
        },
    )


def _record_connectivity_conditions(
    state: AppState,
    offline: bool,
    source: str = CONNECTIVITY_CONDITION_SOURCE,
) -> list[str]:
    """Publish offline/connectivity control state as policy-visible conditions."""
    values = {
        "operational.connectivity.offline": offline,
        "operational.connectivity.mode": "offline" if offline else "online",
        "operational.connectivity.network_available": not offline,
    }
    updated: list[str] = []
    for path, value in values.items():
        state.condition_store.set(
            path=path,
            value=value,
            source=source,
            priority=CONNECTIVITY_CONDITION_PRIORITY,
            confidence=1.0,
        )
        updated.append(path)
        condition_update_count.inc()

    emit_telemetry(
        state,
        "connectivity.updated",
        {
            "offline": offline,
            "mode": values["operational.connectivity.mode"],
            "network_available": values["operational.connectivity.network_available"],
            "updated": updated,
            "source": source,
        },
    )
    return updated


def _deployment_state_name(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", str(value))


def _condition_path_segment(value: str) -> str:
    """Normalize a free-form name into a condition path segment."""
    segment = "".join(
        char.lower() if char.isalnum() or char == "_" else "_" for char in str(value)
    ).strip("_")
    return segment or "value"


def _record_inference_runtime_health(
    state: AppState,
    slot_name: str,
    *,
    healthy: bool,
    error: str | None = None,
    failed_model_id: str | None = None,
) -> None:
    """Publish per-slot inference health as policy-visible runtime conditions."""
    slot_segment = _condition_path_segment(slot_name)
    values = {
        f"runtime.inference.{slot_segment}.healthy": healthy,
        f"runtime.inference.{slot_segment}.last_error": error,
        f"runtime.inference.{slot_segment}.failed_model": failed_model_id,
    }
    for path, value in values.items():
        state.condition_store.set(
            path=path,
            value=value,
            source="inference_runtime",
            priority=900,
            confidence=1.0,
        )
        condition_update_count.inc()


async def _activate_fallback_after_inference_failure(
    state: AppState,
    slot_name: str,
    failed_model: Any,
    error: str,
) -> Any | None:
    """Try policy fallback chain after the active model fails during inference."""
    fallback_chain = state.policy_engine.get_fallback_chain(slot_name)
    if not fallback_chain:
        return None

    attempted: list[str] = []
    failures: list[str] = [f"{failed_model.id}: {error}"]
    conditions = state.condition_store.get_snapshot()
    trigger_detail = "runtime inference failure"

    for model_name in fallback_chain:
        fallback_model = state.model_cache.find_model(model_name)
        if fallback_model is None:
            attempted.append(model_name)
            failures.append(f"{model_name}: not found")
            continue
        if fallback_model.id == failed_model.id:
            attempted.append(fallback_model.id)
            failures.append(f"{fallback_model.id}: already failed")
            continue
        attempted.append(fallback_model.id)
        try:
            await state.inference_runtime.load_model(slot_name, fallback_model.id)
        except Exception as exc:
            failures.append(f"{fallback_model.id}: {exc}")
            continue

        fallback_metadata = {
            "selected_model": failed_model.id,
            "attempted": attempted,
            "failures": failures,
        }
        audit_metadata = model_audit_metadata(state, fallback_model.id)
        audit_metadata["fallback"] = fallback_metadata
        state.slot_manager.activate_model(
            slot_name=slot_name,
            model_id=fallback_model.id,
            trigger_type="fallback",
            trigger_detail=f"fallback after {trigger_detail}",
            conditions=conditions,
            audit_metadata=audit_metadata,
        )
        emit_telemetry(
            state,
            "inference.fallback",
            {
                "slot": slot_name,
                "failed_model_id": failed_model.id,
                "model_id": fallback_model.id,
                "reason": trigger_detail,
                "fallback": fallback_metadata,
                "model": audit_metadata,
            },
        )
        return fallback_model

    emit_telemetry(
        state,
        "inference.fallback_failed",
        {
            "slot": slot_name,
            "failed_model_id": failed_model.id,
            "fallback_chain": fallback_chain,
            "attempted": attempted,
            "failures": failures,
        },
    )
    state.slot_manager.update_slot_state(slot_name, SlotState.ERROR)
    return None


def model_audit_metadata(state: AppState, model_id: str | None) -> Dict[str, Any]:
    """Return compact model/package context for decision logs and telemetry."""
    if not model_id:
        return {}
    model = state.model_cache.get_model(model_id)
    if model is None:
        return {"model_id": model_id}
    return {
        "model_id": model.id,
        "model_name": model.name,
        "model_version": model.version,
        "model_format": model.format.value,
        "model_sha256": model.sha256,
        "package_id": model.package_id,
        "package": package_audit_metadata(state, model.package_id),
        "provenance": model.metadata.get("provenance", {}),
        "runtime_constraints": model.metadata.get("runtime_constraints", {}),
        "benchmark": model.metadata.get("benchmark", {}),
    }


def package_audit_metadata(state: AppState, package_id: str | None) -> Dict[str, Any]:
    """Return package provenance and verification context for audit logs."""
    if not package_id:
        return {}
    get_package = getattr(state.model_cache, "get_package", None)
    package = get_package(package_id) if callable(get_package) else None
    if package is None:
        return {"package_id": package_id}

    manifest = package.manifest or {}
    import_audit = manifest.get("_temms_import", {})
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

    return {
        "package_id": package.id,
        "name": package.name,
        "version": package.version,
        "source": package.source,
        "imported_at": package.imported_at.isoformat(),
        "source_registry": manifest.get("source_registry"),
        "mlflow_run_id": manifest.get("mlflow_run_id"),
        "provenance": manifest.get("provenance", {}),
        "compatibility": manifest.get("compatibility", {}),
        "policies": manifest.get("policies", []),
        "import": {
            "schema_version": (
                import_audit.get("schema_version") if isinstance(import_audit, dict) else None
            ),
            "source": import_audit.get("source") if isinstance(import_audit, dict) else None,
            "source_type": (
                import_audit.get("source_type") if isinstance(import_audit, dict) else None
            ),
            "source_sha256": (
                import_audit.get("source_sha256") if isinstance(import_audit, dict) else None
            ),
            "hashes_verified": (
                import_audit.get("hashes_verified") if isinstance(import_audit, dict) else None
            ),
            "signature_required": (
                import_audit.get("signature_required") if isinstance(import_audit, dict) else None
            ),
            "signature_verified": (
                import_audit.get("signature_verified") if isinstance(import_audit, dict) else None
            ),
            "signature": signature_summary,
            "device_profile": (
                import_audit.get("device_profile") if isinstance(import_audit, dict) else None
            ),
            "warnings": import_audit.get("warnings", []) if isinstance(import_audit, dict) else [],
        },
    }


control_router = APIRouter(
    prefix="/v1/control",
    tags=["control"],
    dependencies=[Depends(require_control_auth)],
)

hub_router = APIRouter(
    prefix="/v1/hub",
    tags=["hub-lite"],
    dependencies=[Depends(require_control_auth)],
)


# ----- Inference Endpoints -----


@inference_router.post("/slots/{slot_name}/infer", response_model=InferenceResponse)
async def infer(
    slot_name: str,
    file: UploadFile = File(...),
    state: AppState = Depends(get_state),
) -> InferenceResponse:
    """
    Run inference on the slot's currently active model.

    Args:
        slot_name: Name of the slot to run inference on
        file: Input file (image, etc.)

    Returns:
        InferenceResponse with predictions and metadata
    """
    start_time = time.time()

    # 1. Get slot
    slot = state.slot_manager.get_slot(slot_name)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Slot not found: {slot_name}")

    if slot.state != SlotState.RUNNING:
        raise HTTPException(
            status_code=503, detail=f"Slot '{slot_name}' is not running (state: {slot.state.value})"
        )

    if slot.active_model_id is None:
        raise HTTPException(status_code=503, detail=f"Slot '{slot_name}' has no active model")

    # 2. Get model info
    model = state.model_cache.get_model(slot.active_model_id)
    if model is None:
        raise HTTPException(
            status_code=503, detail=f"Active model not found in cache: {slot.active_model_id}"
        )

    # 3. Read input data
    input_data = await file.read()
    content_type = file.content_type or "application/octet-stream"

    # 4. Run inference
    try:
        predictions = await state.inference_runtime.infer(
            slot_name=slot_name,
            model_id=slot.active_model_id,
            input_data=input_data,
            content_type=content_type,
        )
    except Exception as e:
        logger.error(f"Inference failed for slot {slot_name}: {e}")
        error = str(e)
        _record_inference_runtime_health(
            state,
            slot_name,
            healthy=False,
            error=error,
            failed_model_id=model.id,
        )
        emit_telemetry(
            state,
            "inference.failed",
            {
                "slot": slot_name,
                "model_id": model.id,
                "model_name": model.name,
                "error": error,
            },
        )
        fallback_model = await _activate_fallback_after_inference_failure(
            state,
            slot_name,
            model,
            error,
        )
        if fallback_model is None:
            raise HTTPException(status_code=500, detail=f"Inference failed: {error}")
        try:
            predictions = await state.inference_runtime.infer(
                slot_name=slot_name,
                model_id=fallback_model.id,
                input_data=input_data,
                content_type=content_type,
            )
            model = fallback_model
        except Exception as retry_error:
            retry_detail = str(retry_error)
            _record_inference_runtime_health(
                state,
                slot_name,
                healthy=False,
                error=retry_detail,
                failed_model_id=fallback_model.id,
            )
            emit_telemetry(
                state,
                "inference.failed_after_fallback",
                {
                    "slot": slot_name,
                    "model_id": fallback_model.id,
                    "model_name": fallback_model.name,
                    "error": retry_detail,
                },
            )
            state.slot_manager.update_slot_state(slot_name, SlotState.ERROR)
            raise HTTPException(
                status_code=500,
                detail=f"Inference fallback failed: {retry_detail}",
            )

    _record_inference_runtime_health(state, slot_name, healthy=True)

    # 5. Return response
    latency_ms = (time.time() - start_time) * 1000
    inference_request_count.inc()
    inference_latency_ms.observe(latency_ms)
    emit_telemetry(
        state,
        "inference.served",
        {
            "slot": slot_name,
            "model_id": model.id,
            "model_name": model.name,
            "model_version": model.version,
            "latency_ms": latency_ms,
            "content_type": content_type,
            "bytes": len(input_data),
        },
    )

    return InferenceResponse(
        slot=slot_name,
        model=model.name,
        model_version=model.version,
        predictions=predictions,
        latency_ms=latency_ms,
    )


@inference_router.get("/slots/{slot_name}/status", response_model=SlotStatusResponse)
async def slot_status(
    slot_name: str,
    state: AppState = Depends(get_state),
) -> SlotStatusResponse:
    """Get detailed slot status."""
    slot = state.slot_manager.get_slot(slot_name)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Slot not found: {slot_name}")

    return SlotStatusResponse(
        name=slot.name,
        description=slot.description,
        state=slot.state.value,
        required=slot.required,
        active_model=slot.active_model_id,
        default_model=slot.default_model,
        candidates=slot.candidates,
        updated_at=slot.updated_at.isoformat(),
    )


# ----- Status Endpoints -----


@status_router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe."""
    return HealthResponse(
        status="ok",
        timestamp=datetime.now().isoformat(),
    )


@status_router.get("/status", response_model=SystemStatusResponse)
async def system_status(state: AppState = Depends(get_state)) -> SystemStatusResponse:
    """Full system status including all slots and conditions."""
    # Get all slots
    slots = state.slot_manager.list_slots()
    slots_dict = {}

    all_healthy = True
    has_error = False

    for slot in slots:
        slots_dict[slot.name] = {
            "state": slot.state.value,
            "active_model": slot.active_model_id,
            "required": slot.required,
        }

        if slot.state == SlotState.ERROR:
            has_error = True
        elif slot.state != SlotState.RUNNING and slot.required:
            all_healthy = False

    # Determine overall status
    if has_error:
        status = "error"
    elif not all_healthy:
        status = "degraded"
    else:
        status = "healthy"

    # Count conditions and policies
    conditions = state.condition_store.get_all()
    policies = state.policy_engine.list_policies()

    uptime = time.time() - state.start_time

    return SystemStatusResponse(
        status=status,
        slots=slots_dict,
        conditions_count=len(conditions),
        policies_count=len(policies),
        uptime_seconds=uptime,
    )


@status_router.get("/evidence")
async def export_edge_evidence(
    limit: int = 100,
    summary: bool = False,
    summary_limit: int = 20,
    replay: bool = False,
    replay_limit: int = 50,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Export local decision evidence from the edge runtime state."""
    from temms.evidence import (
        build_evidence_bundle,
        build_mission_replay,
        summarize_evidence_bundle,
    )

    bundle = build_evidence_bundle(
        state,
        telemetry_limit=limit,
        decision_limit=limit,
        include_benchmarks=True,
    )
    if replay:
        return build_mission_replay(bundle, limit=replay_limit)
    if summary:
        return summarize_evidence_bundle(bundle, limit=summary_limit)
    return bundle


# ----- Control Endpoints -----


@control_router.post("/slots/{slot_name}/model")
async def override_model(
    slot_name: str,
    request: SlotOverrideRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """
    Operator override - force specific model on a slot.

    This bypasses policy evaluation until cleared.
    """
    require_rbac_role(http_request, state, "operator")

    # Validate slot exists
    slot = state.slot_manager.get_slot(slot_name)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Slot not found: {slot_name}")

    # Validate model exists in cache
    model = state.model_cache.find_model(request.model)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model not found in cache: {request.model}")

    trigger_detail = request.reason or "manual override"
    conditions = state.condition_store.get_snapshot()
    activation_preflight = _control_activation_preflight_or_409(
        state,
        slot_name=slot_name,
        model_id=model.id,
        trigger_type="operator",
        trigger_detail=trigger_detail,
        conditions=conditions,
    )

    # Load model if not already loaded
    try:
        await state.inference_runtime.load_model(slot_name, model.id)
    except Exception as e:
        logger.error(f"Failed to load model {model.id} for slot {slot_name}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to load model: {str(e)}")

    # Set operator override so policy loop respects it
    state.slot_manager.set_operator_override(
        slot_name=slot_name,
        model_id=model.id,
        reason=request.reason or "manual override",
        source="api",
        duration_s=request.duration_s,
    )

    # Activate model with operator trigger
    audit_metadata = model_audit_metadata(state, model.id)
    if activation_preflight:
        audit_metadata["activation_preflight"] = activation_preflight
    state.slot_manager.activate_model(
        slot_name=slot_name,
        model_id=model.id,
        trigger_type="operator",
        trigger_detail=trigger_detail,
        conditions=conditions,
        audit_metadata=audit_metadata,
    )
    emit_telemetry(
        state,
        "slot.override",
        {
            "slot": slot_name,
            "model_id": model.id,
            "reason": request.reason,
            "duration_s": request.duration_s,
            "model": audit_metadata,
        },
    )

    logger.info(f"Operator override: slot={slot_name}, model={model.id}, reason={request.reason}")

    if state.offline_mode and state.pending_operations is not None:
        _enqueue_pending_operation(
            state,
            "override_model",
            {
                "slot_name": slot_name,
                "request": request.model_dump(),
                "applied_locally": True,
            },
        )

    return {
        "status": "buffered" if state.offline_mode else "success",
        "slot": slot_name,
        "model": model.id,
        "reason": request.reason,
        "offline": state.offline_mode,
        "applied_locally": True,
        "timestamp": datetime.now().isoformat(),
    }


@control_router.post("/slots/{slot_name}/evaluate")
async def evaluate_slot_control(
    slot_name: str,
    request: SlotEvaluateRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Evaluate local adaptive model selection for a slot."""
    if request.apply:
        require_rbac_role(http_request, state, "operator", "edge")
    decision = await state.controller.evaluate_slot(slot_name, apply=request.apply)
    if decision.status == "slot_not_found":
        raise HTTPException(status_code=404, detail=decision.reason)
    return decision.to_dict()


@control_router.post("/slots/{slot_name}/rollback")
async def rollback_slot(
    slot_name: str,
    request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Rollback a slot to the previous active model in the decision log."""
    require_rbac_role(request, state, "operator")
    actor = request_actor(request, default="operator")
    return await _rollback_slot_to_previous_model(
        state=state,
        slot_name=slot_name,
        trigger_detail="api",
        telemetry_payload={"actor": actor},
        update_rollout_for_slot=True,
        actor=actor,
    )


async def _rollback_slot_to_previous_model(
    *,
    state: AppState,
    slot_name: str,
    trigger_detail: str,
    telemetry_payload: Dict[str, Any],
    rollout_id: Optional[str] = None,
    update_rollout_for_slot: bool = False,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Rollback a slot to its previous model and optionally update Hub Lite state."""
    slot = state.slot_manager.get_slot(slot_name)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Slot not found: {slot_name}")

    decisions = state.slot_manager.get_decision_log(slot_name=slot_name, limit=25)
    previous_model_id = None
    for decision in decisions:
        candidate = decision.get("from_model")
        if candidate and candidate != slot.active_model_id:
            previous_model_id = candidate
            break

    if previous_model_id is None:
        raise HTTPException(status_code=409, detail=f"No rollback target for slot: {slot_name}")

    previous_model = state.model_cache.get_model(previous_model_id)
    if previous_model is None:
        raise HTTPException(
            status_code=404,
            detail=f"Rollback model not found in cache: {previous_model_id}",
        )

    conditions = state.condition_store.get_snapshot()
    activation_preflight = _control_activation_preflight_or_409(
        state,
        slot_name=slot_name,
        model_id=previous_model.id,
        trigger_type="rollback",
        trigger_detail=trigger_detail,
        conditions=conditions,
    )

    try:
        await state.inference_runtime.load_model(slot_name, previous_model.id)
    except Exception as e:
        logger.error(f"Rollback failed for slot {slot_name}: {e}")
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")

    audit_metadata = model_audit_metadata(state, previous_model.id)
    if activation_preflight:
        audit_metadata["activation_preflight"] = activation_preflight
    state.slot_manager.activate_model(
        slot_name=slot_name,
        model_id=previous_model.id,
        trigger_type="rollback",
        trigger_detail=trigger_detail,
        conditions=conditions,
        audit_metadata=audit_metadata,
    )
    emit_telemetry(
        state,
        "slot.rollback",
        {
            "slot": slot_name,
            "model_id": previous_model.id,
            "from_model": slot.active_model_id,
            "model": audit_metadata,
            **telemetry_payload,
        },
    )

    if state.hub_lite is not None and rollout_id is not None:
        state.hub_lite.update_rollout_status(
            rollout_id,
            "rolled_back",
            detail=f"slot {slot_name} rolled back to {previous_model.id}",
            actor=actor,
        )
    elif state.hub_lite is not None and update_rollout_for_slot:
        for rollout in state.hub_lite.list_rollouts():
            if rollout.get("slot") == slot_name:
                try:
                    state.hub_lite.update_rollout_status(
                        rollout["rollout_id"],
                        "rolled_back",
                        detail=f"slot {slot_name} rolled back to {previous_model.id}",
                        actor=actor,
                    )
                except Exception:
                    pass

    return {
        "status": "success",
        "slot": slot_name,
        "model": previous_model.id,
        "rollout_id": rollout_id,
        "timestamp": datetime.now().isoformat(),
    }


@control_router.post("/conditions", response_model=ConditionUpdateResponse)
async def update_conditions(
    request: ConditionUpdateRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> ConditionUpdateResponse:
    """
    Inject conditions from operator or external source.

    These are set with operator priority (1000) to override sensor data.
    """
    require_rbac_role(http_request, state, "operator")

    updated = []
    source = "operator_api_offline" if state.offline_mode else "operator_api"

    for path, value in request.conditions.items():
        state.condition_store.set(
            path=path,
            value=value,
            source=source,
            priority=1000,  # Operator override priority
            confidence=1.0,
        )
        updated.append(path)
        condition_update_count.inc()
        logger.info(f"Condition updated via API: {path}={value}")

    emit_telemetry(
        state,
        "conditions.updated",
        {
            "updated": updated,
            "count": len(updated),
            "source": source,
        },
    )

    if state.offline_mode and state.pending_operations is not None:
        _enqueue_pending_operation(
            state,
            "update_conditions",
            {
                "conditions": request.conditions,
                "applied_locally": True,
            },
        )

    return ConditionUpdateResponse(
        updated=updated,
        timestamp=datetime.now().isoformat(),
    )


@control_router.delete("/conditions/overrides")
async def clear_condition_overrides(
    request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Clear all operator condition overrides."""
    require_rbac_role(request, state, "operator")
    count = state.condition_store.clear_operator_overrides()
    logger.info(f"Cleared {count} operator condition overrides")

    return {
        "status": "success",
        "cleared_count": count,
        "timestamp": datetime.now().isoformat(),
    }


@control_router.post("/offline")
async def set_offline(request: Request, state: AppState = Depends(get_state)) -> Dict[str, Any]:
    require_rbac_role(request, state, "operator")
    state.offline_mode = True
    if state.daemon_config is not None:
        state.daemon_config.offline_mode = True
    updated = _record_connectivity_conditions(state, offline=True)
    if state.deployment_state:
        state.deployment_state.set_state("OFFLINE", "api_offline")
    return {
        "status": "success",
        "offline_mode": True,
        "conditions": updated,
    }


@control_router.post("/online")
async def set_online(request: Request, state: AppState = Depends(get_state)) -> Dict[str, Any]:
    require_rbac_role(request, state, "operator")
    state.offline_mode = False
    if state.daemon_config is not None:
        state.daemon_config.offline_mode = False
    updated = _record_connectivity_conditions(state, offline=False)
    if state.deployment_state:
        current_state = _deployment_state_name(state.deployment_state.get_state())
        if current_state == "OFFLINE":
            state.deployment_state.set_state("PENDING", "api_online")
    return {
        "status": "success",
        "offline_mode": False,
        "conditions": updated,
    }


async def _replay_deploy_operation(
    state: AppState,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Replay a queued deploy intent when it names a concrete slot/model pair."""
    context = deploy_intent_context(payload)
    slot_name = context.get("slot")
    model_id = context.get("model_id")
    if not slot_name or not model_id:
        raise HTTPException(
            status_code=409,
            detail="Queued deploy request did not include slot and model_id",
        )

    slot_name = str(slot_name)
    model_id = str(model_id)
    slot = state.slot_manager.get_slot(slot_name)
    if slot is None:
        raise HTTPException(status_code=409, detail=f"Queued deploy slot not found: {slot_name}")

    model = state.model_cache.get_model(model_id) or state.model_cache.find_model(model_id)
    if model is None:
        raise HTTPException(status_code=409, detail=f"Queued deploy model not found: {model_id}")
    context = deploy_intent_context(payload, model=model)
    trigger_detail = context.get("source") or "offline replay"
    conditions = state.condition_store.get_snapshot()
    activation_preflight = _control_activation_preflight_or_409(
        state,
        slot_name=slot_name,
        model_id=model.id,
        trigger_type="deploy",
        trigger_detail=trigger_detail,
        conditions=conditions,
    )

    try:
        await state.inference_runtime.load_model(slot_name, model.id)
    except Exception as e:
        logger.error(f"Failed to replay queued deploy for {model.id} on {slot_name}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to replay queued deploy for {model.id}: {str(e)}",
        ) from e

    audit_metadata = {
        **model_audit_metadata(state, model.id),
        "actor": context.get("actor") or "operator_api_sync",
        "source": context.get("source") or "ddil_sync",
        "package_id": context.get("package_id"),
        "device_id": context.get("device_id"),
        "runtime_target_id": context.get("runtime_target_id"),
    }
    retarget_audit = _runtime_retarget_audit(payload)
    if retarget_audit:
        audit_metadata["ddil_runtime_retarget"] = retarget_audit
    if activation_preflight:
        audit_metadata["activation_preflight"] = activation_preflight
    state.slot_manager.set_operator_override(
        slot_name=slot_name,
        model_id=model.id,
        reason=context.get("reason") or context.get("source") or "queued deploy intent",
        source="deploy_sync",
        duration_s=context.get("duration_s"),
    )
    state.slot_manager.activate_model(
        slot_name=slot_name,
        model_id=model.id,
        trigger_type="deploy",
        trigger_detail=trigger_detail,
        conditions=conditions,
        audit_metadata=audit_metadata,
    )
    emit_telemetry(
        state,
        "deploy.replayed",
        {
            **payload,
            "slot": slot_name,
            "model_id": model.id,
            "model": audit_metadata,
        },
    )
    if state.deployment_state:
        state.deployment_state.set_state("READY", f"activated {model.id}")
    return {"activated": True, "slot": slot_name, "model_id": model.id}


async def _replay_override_model_operation(
    state: AppState,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Replay a queued operator override intent with the same refs as preflight."""
    context = deploy_intent_context(payload)
    slot_name = context.get("slot")
    model_ref = context.get("model_id")
    if not slot_name or not model_ref:
        raise HTTPException(
            status_code=409,
            detail="Queued override_model request did not include slot_name and request.model",
        )

    slot_name = str(slot_name)
    model_ref = str(model_ref)
    slot = state.slot_manager.get_slot(slot_name)
    if slot is None:
        raise HTTPException(
            status_code=409,
            detail=f"Queued override_model slot not found: {slot_name}",
        )

    model = state.model_cache.get_model(model_ref) or state.model_cache.find_model(model_ref)
    if model is None:
        raise HTTPException(
            status_code=409,
            detail=f"Queued override_model model not found: {model_ref}",
        )

    context = deploy_intent_context(payload, model=model)
    reason = context.get("reason") or "offline replay"
    conditions = state.condition_store.get_snapshot()
    activation_preflight = _control_activation_preflight_or_409(
        state,
        slot_name=slot_name,
        model_id=model.id,
        trigger_type="operator",
        trigger_detail=reason,
        conditions=conditions,
    )
    try:
        await state.inference_runtime.load_model(slot_name, model.id)
    except Exception as e:
        logger.error(
            f"Failed to replay queued override_model for {model.id} on {slot_name}: {e}"
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to replay queued override_model for {model.id}: {str(e)}",
        ) from e

    state.slot_manager.set_operator_override(
        slot_name=slot_name,
        model_id=model.id,
        reason=reason,
        source="api_sync",
        duration_s=context.get("duration_s"),
    )
    audit_metadata = model_audit_metadata(state, model.id)
    if activation_preflight:
        audit_metadata["activation_preflight"] = activation_preflight
    state.slot_manager.activate_model(
        slot_name=slot_name,
        model_id=model.id,
        trigger_type="operator",
        trigger_detail=reason,
        conditions=conditions,
        audit_metadata=audit_metadata,
    )
    return {"activated": True, "slot": slot_name, "model_id": model.id}


@control_router.post("/sync")
async def sync_pending(request: Request, state: AppState = Depends(get_state)) -> Dict[str, Any]:
    require_rbac_role(request, state, "operator")
    if state.pending_operations is None:
        return {"status": "success", "replayed": 0}
    entries = state.pending_operations.read_all()
    replayed = 0
    skipped = 0
    preflight = pending_sync_preflight(state, entries)
    if preflight["status"] == "blocked":
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Pending operation preflight failed",
                "preflight": preflight,
            },
        )
    preflight_by_index = {
        int(entry["index"]): entry
        for entry in preflight.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("index"), int)
    }

    for index, entry in enumerate(entries):
        operation = entry.get("operation")
        payload = entry.get("payload", {})
        preflight_entry = preflight_by_index.get(index, {})
        try:
            if preflight_entry.get("superseded") is True:
                skipped += 1
                emit_telemetry(
                    state,
                    "pending_operations.superseded_skipped",
                    {
                        "index": index,
                        "operation": operation,
                        "slot": preflight_entry.get("slot"),
                        "model_id": preflight_entry.get("model_id"),
                        "resolved_model_id": preflight_entry.get("resolved_model_id"),
                        "superseded_by_index": preflight_entry.get(
                            "superseded_by_index"
                        ),
                        "superseded_by_model_id": preflight_entry.get(
                            "superseded_by_model_id"
                        ),
                        "payload_sha256": preflight_entry.get("payload_sha256"),
                    },
                )
                continue

            if operation == "update_conditions":
                conditions = payload.get("conditions", {})
                for path, value in conditions.items():
                    state.condition_store.set(
                        path=path,
                        value=value,
                        source="operator_api_sync",
                        priority=1000,
                        confidence=1.0,
                    )
                    condition_update_count.inc()
                replayed += 1
            elif operation == "override_model":
                replay_result = await _replay_override_model_operation(state, payload)
                if replay_result.get("activated") is not True:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "Queued override_model did not activate a model",
                            "result": replay_result,
                        },
                    )
                replayed += 1
            elif operation == "deploy":
                deployment_count.inc()
                replay_result = await _replay_deploy_operation(state, payload)
                if replay_result.get("activated") is not True:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "Queued deploy did not activate a model",
                            "result": replay_result,
                        },
                    )
                replayed += 1
        except Exception as exc:
            _preserve_pending_replay_remainder(
                state,
                entries,
                index,
                replayed=replayed,
                skipped=skipped,
                error=exc,
            )
            raise

    state.pending_operations.clear()
    return {
        "status": "success",
        "replayed": replayed,
        "skipped": skipped,
        "superseded_skipped": skipped,
        "pending_cleared": len(entries),
        "preflight": preflight,
    }


@control_router.get("/sync/preview")
async def sync_pending_preview(
    request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(request, state, "operator")
    if state.pending_operations is None:
        return pending_sync_preflight(state, [])
    return pending_sync_preflight(state, state.pending_operations.read_all())


@control_router.post("/sync/quarantine-blocked")
async def quarantine_blocked_pending(
    request: Request,
    body: Dict[str, Any] = Body(default_factory=dict),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(request, state, "operator")
    if state.pending_operations is None:
        return {"status": "success", "quarantined": 0, "remaining": 0}
    entries = state.pending_operations.read_all()
    preflight = pending_sync_preflight(state, entries)
    blocked_entries = [
        entry
        for entry in preflight.get("entries", [])
        if isinstance(entry, dict) and not entry.get("ready")
    ]
    if not blocked_entries:
        return {
            "status": "success",
            "quarantined": 0,
            "remaining": len(entries),
            "preflight": preflight,
        }

    actor = request_actor(
        request,
        explicit=str(body.get("actor") or "") if isinstance(body, dict) else None,
        default="operator_api",
    )
    reason = str(body.get("reason") or "blocked DDIL preflight") if isinstance(body, dict) else (
        "blocked DDIL preflight"
    )
    preflight_by_index = {
        int(entry["index"]): entry
        for entry in blocked_entries
        if isinstance(entry.get("index"), int)
    }
    result = state.pending_operations.quarantine(
        indexes=set(preflight_by_index),
        preflight_entries=preflight_by_index,
        actor=actor,
        reason=reason,
    )
    emit_telemetry(
        state,
        "pending_operations.quarantined",
        {
            "actor": actor,
            "reason": reason,
            "quarantined": result["quarantined"],
            "remaining": result["remaining"],
        },
    )
    return {
        "status": "success",
        "preflight": preflight,
        **result,
    }


def _pending_preflight_entry_for_payload(
    preflight: Dict[str, Any],
    payload_sha256: str,
) -> Optional[Dict[str, Any]]:
    digest = _normalize_payload_sha256(payload_sha256)
    for entry in preflight.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if _normalize_payload_sha256(entry.get("payload_sha256")) == digest:
            return entry
    return None


def _runtime_retarget_candidate(entry: Dict[str, Any]) -> Optional[str]:
    gate_lists = [
        entry.get("hub_optimization_gates"),
        entry.get("hub_blocking_gates"),
    ]
    for gates in gate_lists:
        if not isinstance(gates, list):
            continue
        for gate in gates:
            if not isinstance(gate, dict) or gate.get("gate_id") != "runtime_optimizer":
                continue
            for action in gate.get("actions", []):
                if not isinstance(action, dict):
                    continue
                if action.get("kind") != "select_runtime_target":
                    continue
                refs = action.get("refs") if isinstance(action.get("refs"), dict) else {}
                runtime_target_id = str(refs.get("runtime_target_id") or "").strip()
                if runtime_target_id:
                    return runtime_target_id
    best_runtime_target_id = str(entry.get("hub_best_runtime_target_id") or "").strip()
    return best_runtime_target_id or None


def _runtime_retarget_target_proof(
    entry: Dict[str, Any],
    runtime_target_id: str,
) -> Dict[str, Any]:
    assessments = entry.get("hub_target_assessments")
    if not isinstance(assessments, list) or not assessments:
        raise HTTPException(
            status_code=409,
            detail=(
                "Runtime retarget requires Hub target assessments with measured "
                "edge/runtime proof"
            ),
        )

    target = str(runtime_target_id or "").strip()
    assessment = next(
        (
            candidate
            for candidate in assessments
            if isinstance(candidate, dict)
            and str(candidate.get("runtime_target_id") or "").strip() == target
        ),
        None,
    )
    if assessment is None:
        raise HTTPException(
            status_code=409,
            detail=f"Runtime target {target} is not present in Hub target assessments",
        )

    if assessment.get("best") is not True:
        best = str(entry.get("hub_best_runtime_target_id") or "").strip()
        suffix = f"; best measured target is {best}" if best else ""
        raise HTTPException(
            status_code=409,
            detail=f"Runtime target {target} is not the best measured target{suffix}",
        )
    if assessment.get("eligible") is not True or assessment.get("blocked") is True:
        raise HTTPException(
            status_code=409,
            detail=f"Runtime target {target} is not eligible for this edge/model path",
        )
    status = str(assessment.get("status") or "").lower()
    if status and status != "eligible":
        raise HTTPException(
            status_code=409,
            detail=f"Runtime target {target} assessment status is {status}, expected eligible",
        )

    score = _optional_float(assessment.get("score"))
    if score is None:
        raise HTTPException(
            status_code=409,
            detail=f"Runtime target {target} is missing measured runtime-fit score",
        )
    validation = (
        assessment.get("component_states")
        if isinstance(assessment.get("component_states"), dict)
        else {}
    )
    runtime_validation = (
        validation.get("runtime_validation")
        if isinstance(validation.get("runtime_validation"), dict)
        else {}
    )
    validation_id = str(runtime_validation.get("validation_id") or "").strip()
    if not validation_id:
        raise HTTPException(
            status_code=409,
            detail=f"Runtime target {target} is missing non-dry-run validation proof",
        )
    benchmark_id = str(assessment.get("benchmark_id") or "").strip()
    if not benchmark_id:
        raise HTTPException(
            status_code=409,
            detail=f"Runtime target {target} is missing benchmark proof",
        )

    lock = (
        assessment.get("runtime_capability_lock")
        if isinstance(assessment.get("runtime_capability_lock"), dict)
        else {}
    )
    lock_status = str(lock.get("status") or "").lower()
    if lock_status != "locked":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Runtime target {target} capability lock is {lock_status or 'missing'}, "
                "expected locked"
            ),
        )
    capability_sha256 = str(lock.get("capability_sha256") or "").strip()
    if len(capability_sha256) != 64:
        raise HTTPException(
            status_code=409,
            detail=f"Runtime target {target} capability hash is missing",
        )
    locked_target = str(lock.get("runtime_target_id") or "").strip()
    if locked_target and locked_target != target:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Runtime target {target} capability lock belongs to {locked_target}"
            ),
        )

    edge_inventory = (
        lock.get("edge_inventory")
        if isinstance(lock.get("edge_inventory"), dict)
        else {}
    )
    telemetry_freshness = (
        edge_inventory.get("telemetry_freshness")
        if isinstance(edge_inventory.get("telemetry_freshness"), dict)
        else {}
    )
    workbench_best_runtime_target_id = str(
        entry.get("hub_runtime_workbench_best_runtime_target_id") or ""
    ).strip()
    return _compact_runtime_retarget_proof(
        {
            "schema_version": "temms-ddil-runtime-retarget-proof/v1",
            "status": "proved",
            "runtime_target_id": target,
            "target_assessment_schema_version": (
                RUNTIME_TARGET_ASSESSMENT_DIGEST_SCHEMA_VERSION
            ),
            "target_assessment_sha256": runtime_target_assessment_sha256(assessment),
            "best": True,
            "eligible": True,
            "runtime_fit_score": score,
            "rank": assessment.get("rank"),
            "tier": assessment.get("tier"),
            "detail": assessment.get("detail"),
            "runtime_lane": assessment.get("runtime_lane"),
            "artifact_lane": assessment.get("artifact_lane"),
            "runtime_capability_lock": lock,
            "capability_sha256": capability_sha256,
            "telemetry_freshness": telemetry_freshness,
            "runtime_validation_id": validation_id,
            "benchmark_id": benchmark_id,
            "latency_ms_p95": assessment.get("latency_ms_p95"),
            "throughput_ips": assessment.get("throughput_ips"),
            "component_states": assessment.get("component_states"),
            "runtime_workbench_schema_version": entry.get(
                "hub_runtime_workbench_schema_version"
            ),
            "runtime_workbench_status": entry.get("hub_runtime_workbench_status"),
            "runtime_workbench_target_selection_status": entry.get(
                "hub_runtime_workbench_target_selection_status"
            ),
            "runtime_workbench_previous_selected_runtime_target_id": entry.get(
                "hub_runtime_workbench_selected_runtime_target_id"
            ),
            "runtime_workbench_selected_runtime_target_id": target,
            "runtime_workbench_best_runtime_target_id": workbench_best_runtime_target_id,
            "runtime_workbench_target_count": entry.get(
                "hub_runtime_workbench_target_count"
            ),
            "runtime_workbench_eligible_target_count": entry.get(
                "hub_runtime_workbench_eligible_target_count"
            ),
            "runtime_workbench_blocked_target_count": entry.get(
                "hub_runtime_workbench_blocked_target_count"
            ),
            "runtime_workbench_selected_is_best": (
                workbench_best_runtime_target_id == target
            ),
        }
    )


def _compact_runtime_retarget_proof(proof: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in proof.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _optional_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@control_router.post("/sync/retarget-runtime")
async def retarget_pending_runtime(
    request: Request,
    body: Dict[str, Any] = Body(default_factory=dict),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(request, state, "operator")
    if state.pending_operations is None:
        return {"status": "success", "retargeted": 0, "remaining": 0}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")

    payload_sha256 = _normalize_payload_sha256(body.get("payload_sha256"))
    if not payload_sha256:
        raise HTTPException(status_code=400, detail="payload_sha256 is required")

    entries = state.pending_operations.read_all()
    preflight_before = pending_sync_preflight(state, entries)
    preflight_entry = _pending_preflight_entry_for_payload(preflight_before, payload_sha256)
    if preflight_entry is None:
        raise HTTPException(status_code=404, detail="Pending operation payload not found")

    requested_target = str(body.get("runtime_target_id") or "").strip()
    runtime_target_id = requested_target or _runtime_retarget_candidate(preflight_entry)
    if not runtime_target_id:
        raise HTTPException(
            status_code=409,
            detail="No runtime retarget candidate is available for this pending operation",
        )
    previous_runtime_target_id = str(preflight_entry.get("runtime_target_id") or "").strip()
    if previous_runtime_target_id == runtime_target_id:
        raise HTTPException(
            status_code=409,
            detail="Pending operation already targets the requested runtime",
        )
    runtime_target_proof = _runtime_retarget_target_proof(
        preflight_entry,
        runtime_target_id,
    )

    signature_required, signing_key = rollout_signature_policy(state)
    matching_entry = entries[int(preflight_entry["index"])]
    entry_has_signature = isinstance(matching_entry.get("signature"), dict)
    if (signature_required or entry_has_signature) and not signing_key:
        raise HTTPException(
            status_code=409,
            detail="Retargeting this pending operation requires a configured signing key",
        )

    actor = request_actor(
        request,
        explicit=str(body.get("actor") or ""),
        default="operator_api",
    )
    reason = str(body.get("reason") or "operator selected best runtime target")
    try:
        result = state.pending_operations.retarget_runtime(
            payload_sha256=payload_sha256,
            runtime_target_id=runtime_target_id,
            actor=actor,
            reason=reason,
            runtime_target_proof=runtime_target_proof,
            signing_key=signing_key,
            signer=actor,
            require_signature=signature_required,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    updated_entries = state.pending_operations.read_all()
    preflight_after = pending_sync_preflight(state, updated_entries)
    emit_telemetry(
        state,
        "pending_operations.runtime_retargeted",
        {
            "actor": actor,
            "reason": reason,
            "payload_sha256": result.get("payload_sha256"),
            "updated_payload_sha256": result.get("updated_payload_sha256"),
            "previous_runtime_target_id": result.get("previous_runtime_target_id"),
            "runtime_target_id": result.get("runtime_target_id"),
            "runtime_target_proof": runtime_target_proof,
        },
    )
    return {
        "status": "success",
        "preflight_before": preflight_before,
        "preflight_after": preflight_after,
        **result,
    }


@control_router.post("/sync/acknowledge-dead-letters")
async def acknowledge_pending_dead_letters(
    request: Request,
    body: Dict[str, Any] = Body(default_factory=dict),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(request, state, "operator")
    if state.pending_operations is None:
        return {"status": "success", "acknowledged": 0, "dead_letters": 0}
    actor = request_actor(
        request,
        explicit=str(body.get("actor") or "") if isinstance(body, dict) else None,
        default="operator_api",
    )
    reason = (
        str(body.get("reason") or "quarantined DDIL intent reviewed")
        if isinstance(body, dict)
        else "quarantined DDIL intent reviewed"
    )
    payload_sha256s: set[str] | None = None
    if isinstance(body, dict) and isinstance(body.get("payload_sha256s"), list):
        payload_sha256s = {
            digest
            for value in body["payload_sha256s"]
            if isinstance(value, (str, int, float)) and str(value)
            for digest in [_normalize_payload_sha256(value)]
            if digest
        }
    result = state.pending_operations.acknowledge_dead_letters(
        actor=actor,
        reason=reason,
        payload_sha256s=payload_sha256s,
    )
    emit_telemetry(
        state,
        "pending_operations.dead_letters_acknowledged",
        {
            "actor": actor,
            "reason": reason,
            "acknowledged": result["acknowledged"],
            "dead_letters": result["dead_letters"],
        },
    )
    return {
        "status": "success",
        **result,
    }


def _body_bool(body: Dict[str, Any], key: str, *, default: bool = False) -> bool:
    value = body.get(key) if isinstance(body, dict) else None
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _requeue_ready_payload_sha256s(
    state: AppState,
    payload_sha256s: set[str] | None,
) -> tuple[set[str], list[dict[str, Any]]]:
    store = state.pending_operations
    if store is None:
        return set(), []
    ready: set[str] = set()
    blocked: list[dict[str, Any]] = []
    for record in store.read_dead_letter():
        if not isinstance(record, dict):
            continue
        if record.get("acknowledged") or record.get("requeued"):
            continue
        digest = _normalize_payload_sha256(record.get("payload_sha256"))
        if payload_sha256s is not None and digest not in payload_sha256s:
            continue
        entry = record.get("entry")
        if not isinstance(entry, dict):
            blocked.append(
                {
                    "payload_sha256": digest,
                    "reason": "dead-letter record is missing original pending entry",
                    "replay_status": "blocked",
                }
            )
            continue
        preflight = pending_sync_preflight(state, [entry])
        preflight_entries = preflight.get("entries")
        preflight_entry = (
            preflight_entries[0]
            if isinstance(preflight_entries, list)
            and preflight_entries
            and isinstance(preflight_entries[0], dict)
            else {}
        )
        if preflight.get("status") == "ready" and preflight_entry.get("ready") is True:
            if digest:
                ready.add(digest)
            continue
        blocked.append(
            {
                "payload_sha256": digest,
                "operation": preflight_entry.get("operation") or entry.get("operation"),
                "reason": preflight_entry.get("reason")
                or "dead-letter requeue preflight blocked",
                "replay_status": preflight_entry.get("replay_status") or "blocked",
                "signature_status": preflight_entry.get("signature_status"),
                "slot": preflight_entry.get("slot"),
                "model_id": preflight_entry.get("model_id"),
                "device_id": preflight_entry.get("device_id"),
                "package_id": preflight_entry.get("package_id"),
                "runtime_target_id": preflight_entry.get("runtime_target_id"),
                "hub_readiness_status": preflight_entry.get("hub_readiness_status"),
                "hub_capability_lock_status": preflight_entry.get(
                    "hub_capability_lock_status"
                ),
                "hub_capability_sha256": preflight_entry.get("hub_capability_sha256"),
            }
        )
    return ready, blocked


@control_router.post("/sync/requeue-dead-letters")
async def requeue_pending_dead_letters(
    request: Request,
    body: Dict[str, Any] = Body(default_factory=dict),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(request, state, "operator")
    if state.pending_operations is None:
        return {"status": "success", "requeued": 0, "pending": 0, "dead_letters": 0}
    actor = request_actor(
        request,
        explicit=str(body.get("actor") or "") if isinstance(body, dict) else None,
        default="operator_api",
    )
    reason = (
        str(body.get("reason") or "operator requeued remediated DDIL intent")
        if isinstance(body, dict)
        else "operator requeued remediated DDIL intent"
    )
    payload_sha256s: set[str] | None = None
    if isinstance(body, dict) and isinstance(body.get("payload_sha256s"), list):
        payload_sha256s = {
            digest
            for value in body["payload_sha256s"]
            if isinstance(value, (str, int, float)) and str(value)
            for digest in [_normalize_payload_sha256(value)]
            if digest
        }
    force = _body_bool(body, "force", default=False)
    require_ready = _body_bool(body, "require_ready", default=True) and not force
    blocked_entries: list[dict[str, Any]] = []
    requeue_filter = payload_sha256s
    if require_ready:
        requeue_filter, blocked_entries = _requeue_ready_payload_sha256s(
            state,
            payload_sha256s,
        )
    result = state.pending_operations.requeue_dead_letters(
        actor=actor,
        reason=reason,
        payload_sha256s=requeue_filter,
    )
    preflight = pending_sync_preflight(state, state.pending_operations.read_all())
    emit_telemetry(
        state,
        "pending_operations.dead_letters_requeued",
        {
            "actor": actor,
            "reason": reason,
            "requeued": result["requeued"],
            "pending": result["pending"],
            "dead_letters": result["dead_letters"],
            "require_ready": require_ready,
            "blocked": len(blocked_entries),
        },
    )
    return {
        "status": "success",
        "preflight": preflight,
        "require_ready": require_ready,
        "blocked": len(blocked_entries),
        "blocked_entries": blocked_entries,
        **result,
    }


@control_router.post("/deploy")
async def request_deploy(
    request: Dict[str, Any],
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(http_request, state, "operator")
    deployment_count.inc()
    emit_telemetry(state, "deploy.requested", request)
    if state.offline_mode and state.pending_operations is not None:
        _enqueue_pending_operation(state, "deploy", request)
        return {"status": "buffered", "offline": True}
    return {"status": "accepted", "offline": False}


@control_router.post("/telemetry/export")
async def export_telemetry(
    request: TelemetryExportRequest,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Export buffered telemetry for air-gapped replay."""
    if state.telemetry is None:
        raise HTTPException(status_code=503, detail="Telemetry buffer is not configured")
    return state.telemetry.export_bundle(limit=request.limit)


@control_router.post("/telemetry/replay")
async def replay_telemetry(
    request: TelemetryReplayRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Replay buffered telemetry locally, optionally clearing it."""
    require_rbac_role(http_request, state, "operator")
    if state.telemetry is None:
        raise HTTPException(status_code=503, detail="Telemetry buffer is not configured")
    return state.telemetry.replay(clear=request.clear)


@control_router.delete("/telemetry")
async def clear_telemetry(request: Request, state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """Clear buffered telemetry after successful off-box transfer."""
    require_rbac_role(request, state, "operator")
    if state.telemetry is None:
        raise HTTPException(status_code=503, detail="Telemetry buffer is not configured")
    cleared = state.telemetry.clear()
    return {
        "status": "success",
        "cleared": cleared,
        "timestamp": datetime.now().isoformat(),
    }


@control_router.get("/audit/timeline")
async def audit_timeline(
    slot: Optional[str] = None,
    limit: int = 100,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Return a merged decision and telemetry timeline for operator audit."""
    from temms.evidence import (
        combined_timeline,
        decision_timeline,
        package_import_timeline,
        rollout_timeline,
    )

    decisions = decision_timeline(state, limit=limit)
    if slot:
        decisions = [decision for decision in decisions if decision.get("slot") == slot]
    rollout_events = rollout_timeline(state, limit=limit)
    if slot:
        rollout_events = [event for event in rollout_events if event.get("slot") == slot]
    telemetry_events = state.telemetry.read(limit=limit) if state.telemetry is not None else []
    if slot:
        telemetry_events = [
            event for event in telemetry_events if event.get("payload", {}).get("slot") == slot
        ]
    package_imports = package_import_timeline(state, limit=limit)
    if slot:
        package_imports = [
            event
            for event in package_imports
            if event.get("slot") == slot or slot in (event.get("slots") or [])
        ]
    timeline = combined_timeline(
        decisions,
        telemetry_events,
        rollout_events,
        package_imports=package_imports,
    )[:limit]
    return {
        "schema_version": "temms-audit-timeline/v1",
        "slot": slot,
        "count": len(timeline),
        "timeline": timeline,
    }


# ----- Hub Lite Endpoints -----


def get_hub_store(state: AppState):
    """Return Hub Lite store or fail clearly if unavailable."""
    if state.hub_lite is None:
        raise HTTPException(status_code=503, detail="Hub Lite store is not configured")
    return state.hub_lite


def reload_active_policy_store(state: AppState) -> int:
    """Reload in-memory policies from the daemon's active policy directory."""
    policy_dir = state.daemon_config.policy_dir if state.daemon_config is not None else None
    if policy_dir is None:
        return 0
    state.policy_engine.clear_policies()
    if not policy_dir.exists():
        return 0
    loaded = 0
    for policy_file in sorted([*policy_dir.glob("*.yaml"), *policy_dir.glob("*.yml")]):
        state.policy_engine.load_policy_from_file(policy_file)
        loaded += 1
    return loaded


@hub_router.post("/devices/enroll")
async def enroll_device(
    request: DeviceEnrollRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(http_request, state, "operator", "edge")
    hub = get_hub_store(state)
    return hub.enroll_device(
        device_id=request.device_id,
        profile=request.profile,
        labels=request.labels,
        inventory=request.inventory,
    )


@hub_router.post("/devices/{device_id}/heartbeat")
async def heartbeat(
    device_id: str,
    request: HeartbeatRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(http_request, state, "operator", "edge")
    hub = get_hub_store(state)
    return hub.heartbeat(
        device_id=device_id,
        status=request.status,
        inventory=request.inventory,
        deployment_status=request.deployment_status,
    )


@hub_router.get("/devices")
async def list_devices(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    hub = get_hub_store(state)
    return {"devices": hub.list_devices()}


@hub_router.post("/packages")
async def upsert_hub_package(
    request: HubPackageRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    package = request.model_dump(exclude={"actor"})
    require_signature, _ = rollout_signature_policy(state, resolve_key=False)
    if require_signature and not package_signature_verified(package):
        raise HTTPException(
            status_code=400,
            detail=(
                "Package catalog entries must include verified signature metadata when "
                "daemon signature policy is enabled; use /v1/hub/packages/register "
                "for artifact-backed package cataloging"
            ),
        )
    if require_signature and not package_strict_metadata_verified(package):
        raise HTTPException(
            status_code=400,
            detail=(
                "Package catalog entries must include strict production metadata validation "
                "when daemon signature policy is enabled; use /v1/hub/packages/register "
                "for artifact-backed package cataloging"
            ),
        )
    return hub.upsert_package(package, actor=actor)


@hub_router.post("/packages/register")
async def register_hub_package(
    request: HubPackageRegisterRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        require_signature, signing_key = rollout_signature_policy(
            state,
            require_signature=request.require_signature,
            signing_key=request.signing_key,
        )
        package_path = Path(request.package_path)
        should_sign = (
            request.sign if request.sign is not None else bool(require_signature and signing_key)
        )
        if should_sign:
            if not signing_key:
                raise ValueError("Package signing requires a signing key")
            from temms.core.package_archive import sign_package_artifact

            sign_package_artifact(package_path, signing_key, signer=request.signer)
        return hub.upsert_package_from_source(
            package_path,
            require_signature=require_signature,
            signing_key=signing_key,
            device_profiles=request.device_profiles,
            strict_metadata=request.strict_metadata,
            actor=actor,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.post("/packages/from-mlflow")
async def package_hub_mlflow_model(
    request: HubPackageFromMLflowRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Build, sign, and register a package from an MLflow registry model."""
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        require_signature, signing_key = rollout_signature_policy(
            state,
            require_signature=request.require_signature,
            signing_key=request.signing_key,
        )
        should_sign = (
            request.sign if request.sign is not None else bool(require_signature and signing_key)
        )
        if should_sign and not signing_key:
            raise ValueError("Package signing requires a signing key")

        from temms.core.package_builder import build_package_from_mlflow

        output_dir = (
            Path(request.output_dir) if request.output_dir else Path(hub.path).parent / "packages"
        )
        package_path = build_package_from_mlflow(
            model_uri=request.model_uri,
            slot=request.slot,
            policy_path=Path(request.policy_path) if request.policy_path else None,
            output_dir=output_dir,
            tracking_uri=request.tracking_uri,
            model_format=request.model_format,
            device_profile=request.device_profile,
            runtime_constraints_override=request.runtime_constraints,
            runtime_options_override=request.runtime_options,
            model_artifact_path=request.model_artifact_path,
            require_schema=request.require_schema,
            require_runtime_constraints=request.require_runtime_constraints,
            signing_key=signing_key if should_sign else None,
            signer=request.signer,
            strict_metadata=request.strict_metadata,
            archive=request.archive,
            overwrite=request.overwrite,
        )
        package = hub.upsert_package_from_source(
            package_path,
            require_signature=require_signature,
            signing_key=signing_key,
            device_profiles=[request.device_profile] if request.device_profile else None,
            strict_metadata=request.strict_metadata,
            actor=actor,
        )
        return {
            "package": package,
            "package_path": str(package_path),
            "signed": bool(should_sign),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.get("/packages")
async def list_hub_packages(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    hub = get_hub_store(state)
    return {"packages": hub.list_packages()}


@hub_router.post("/packages/{package_id}/promote")
async def promote_hub_package(
    package_id: str,
    request: HubPackagePromotionRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Promote a package candidate toward release or retirement."""
    requested_state = request.state.lower().strip()
    if requested_state == "approved":
        require_rbac_role(http_request, state, "approver")
    else:
        require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        return hub.promote_package(
            package_id,
            requested_state,
            actor=actor,
            reason=request.reason,
            evidence=request.evidence,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.get("/runtime-targets")
async def list_runtime_targets(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    hub = get_hub_store(state)
    return {"runtime_targets": hub.list_runtime_targets()}


@hub_router.post("/runtime-targets")
async def upsert_runtime_target(
    request: RuntimeTargetRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        return hub.upsert_runtime_target(
            request.model_dump(exclude={"actor"}),
            actor=actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _deployment_readiness_with_evidence(
    state: AppState,
    hub,
    *,
    package_id: Optional[str],
    model_id: Optional[str],
    device_id: Optional[str],
    runtime_target_id: Optional[str],
    slot: Optional[str],
) -> Dict[str, Any]:
    readiness = hub.deployment_readiness(
        package_id=package_id,
        model_id=model_id,
        device_id=device_id,
        runtime_target_id=runtime_target_id,
        slot=slot,
    )

    from temms.evidence import (
        build_evidence_bundle,
        build_mission_replay,
        summarize_evidence_bundle,
    )

    bundle = build_evidence_bundle(
        state,
        decision_limit=50,
        include_benchmarks=True,
    )
    summary = summarize_evidence_bundle(bundle, limit=20)
    mission_replay = build_mission_replay(bundle, limit=50)
    return _enrich_readiness_with_evidence(readiness, summary, mission_replay)


@hub_router.get("/readiness")
async def deployment_readiness(
    http_request: Request,
    package_id: Optional[str] = None,
    model_id: Optional[str] = None,
    device_id: Optional[str] = None,
    runtime_target_id: Optional[str] = None,
    slot: Optional[str] = None,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Return the authoritative Hub Lite deployment readiness verdict."""
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    try:
        return _deployment_readiness_with_evidence(
            state,
            hub,
            package_id=package_id,
            model_id=model_id,
            device_id=device_id,
            runtime_target_id=runtime_target_id,
            slot=slot,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.post("/mission-package/plan")
async def plan_mission_package(
    request: MissionPackagePlanRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Return the authoritative mission spec -> package -> edge plan."""
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    try:
        return _mission_package_plan_for_request(state, hub, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.post("/mission-package/download")
async def download_mission_package(
    request: MissionPackagePlanRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Response:
    """Download the selected mission package plan as a JSON handoff artifact."""
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    try:
        package_plan = _mission_package_plan_for_request(state, hub, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    filename = _mission_package_filename(package_plan)
    payload = (
        json.dumps(package_plan, indent=2, sort_keys=True, default=str).encode("utf-8")
        + b"\n"
    )
    integrity = (
        package_plan.get("integrity")
        if isinstance(package_plan.get("integrity"), dict)
        else {}
    )
    component_digests = (
        package_plan.get("component_digests")
        if isinstance(package_plan.get("component_digests"), dict)
        else {}
    )
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-TEMMS-Mission-Package-Filename": filename,
        "X-TEMMS-Mission-Package-Identity-SHA256": str(
            integrity.get("package_identity_sha256", "")
        ),
        "X-TEMMS-Mission-Package-SHA256": str(
            integrity.get("payload_sha256", "")
        ),
    }
    component_digest_headers = {
        "mission_sha256": "X-TEMMS-Mission-Package-Mission-SHA256",
        "runtime_plan_sha256": "X-TEMMS-Mission-Package-Runtime-Plan-SHA256",
        "deployment_intent_sha256": (
            "X-TEMMS-Mission-Package-Deployment-Intent-SHA256"
        ),
    }
    for digest_key, header_name in component_digest_headers.items():
        digest = component_digests.get(digest_key)
        if digest:
            headers[header_name] = str(digest)
    return Response(
        content=payload,
        media_type="application/json",
        headers=headers,
    )


@hub_router.post("/mission-package/stage")
async def stage_mission_package(
    request: MissionPackageStageRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Stage the rollout embedded in a mission package handoff artifact."""
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    try:
        body, stage_proof = _mission_package_stage_request_body(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    actor = request_actor(
        http_request,
        request.actor or str(body.get("actor") or ""),
        default="operator:mission-package-workbench",
    )
    body["actor"] = actor

    require_signature, _ = rollout_signature_policy(state, resolve_key=False)
    package = hub.get_package(str(body.get("package_id") or ""))
    if require_signature and package is not None and not package_signature_verified(package):
        raise HTTPException(
            status_code=400,
            detail=f"Package {body.get('package_id')} does not have a verified signature",
        )
    if require_signature and package is not None and not package_strict_metadata_verified(package):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Package {body.get('package_id')} does not have strict production "
                "metadata validation"
            ),
        )

    try:
        rollout = hub.assign_rollout(
            device_id=str(body.get("device_id") or ""),
            package_id=str(body.get("package_id") or ""),
            model_id=str(body["model_id"]) if body.get("model_id") else None,
            slot=str(body["slot"]) if body.get("slot") else None,
            rollout_id=str(body["rollout_id"]) if body.get("rollout_id") else None,
            runtime_target_id=str(body["runtime_target_id"])
            if body.get("runtime_target_id")
            else None,
            require_runtime_validation=bool(body.get("require_runtime_validation")),
            require_approval=bool(body.get("require_approval")),
            actor=actor,
            reason=str(body["reason"]) if body.get("reason") else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        **stage_proof,
        "status": "staged",
        "rollout_id": rollout.get("rollout_id"),
        "rollout_state": rollout.get("state"),
        "rollout": rollout,
    }


def _mission_package_plan_for_request(
    state: AppState,
    hub,
    request: MissionPackagePlanRequest,
) -> Dict[str, Any]:
    request_payload = _mission_package_request_payload(request)
    readiness = _deployment_readiness_with_evidence(
        state,
        hub,
        package_id=request_payload.get("package_id"),
        model_id=request_payload.get("model_id"),
        device_id=request_payload.get("device_id"),
        runtime_target_id=request_payload.get("runtime_target_id"),
        slot=request_payload.get("slot"),
    )
    return build_edge_mission_package_plan(
        readiness,
        request_payload,
        require_go=request.require_go,
        min_runtime_fit=request.min_runtime_fit,
        require_best_runtime=request.require_best_runtime,
        require_capability_lock=request.require_capability_lock,
        require_proof_signature=request.require_proof_signature,
    )


def _mission_package_stage_request_body(
    request: MissionPackageStageRequest,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    package_plan = request.mission_package
    if not isinstance(package_plan, dict):
        raise ValueError("mission_package must be an object")
    if package_plan.get("schema_version") != EDGE_MISSION_PACKAGE_SCHEMA_VERSION:
        raise ValueError(
            f"mission_package must use schema {EDGE_MISSION_PACKAGE_SCHEMA_VERSION}"
        )

    deployment_intent = (
        package_plan.get("deployment_intent")
        if isinstance(package_plan.get("deployment_intent"), dict)
        else {}
    )
    command = (
        deployment_intent.get("command")
        if isinstance(deployment_intent.get("command"), dict)
        else {}
    )
    command_method = str(command.get("method") or "POST").upper()
    command_path = _normalize_mission_package_stage_path(str(command.get("path") or ""))
    command_body = command.get("body") if isinstance(command.get("body"), dict) else {}
    if not deployment_intent or not command_body:
        raise ValueError("mission package does not contain a deployment intent command")
    if command_method != "POST" or command_path != "/rollouts":
        raise ValueError(
            f"unsupported mission package deployment command: "
            f"{command_method} {command.get('path')}"
        )

    stage_gate = _mission_package_stage_gate(package_plan)
    _verify_mission_package_stage_integrity(package_plan, deployment_intent)
    deployment_rollout_id = str(deployment_intent.get("rollout_id") or "")
    command_rollout_id = str(command_body.get("rollout_id") or "")
    if not deployment_rollout_id or not command_rollout_id:
        raise ValueError("mission package deployment intent requires rollout_id")
    if deployment_rollout_id != command_rollout_id:
        raise ValueError("mission package deployment intent rollout_id mismatch")
    if request.rollout_id and request.rollout_id != deployment_rollout_id:
        raise ValueError("mission package stage rollout_id must match deployment intent")
    intent_requires = (
        deployment_intent.get("requires")
        if isinstance(deployment_intent.get("requires"), dict)
        else {}
    )
    required_gate_flags = (
        ("approval", "require_approval"),
        ("runtime_validation", "require_runtime_validation"),
    )
    for intent_key, command_key in required_gate_flags:
        if intent_requires.get(intent_key) is not True:
            raise ValueError(
                f"mission package deployment intent must require {intent_key}"
            )
        if command_body.get(command_key) is not True:
            raise ValueError(
                f"mission package deployment command must require {intent_key}"
            )
    if intent_requires.get("edge_readiness") is not True:
        raise ValueError("mission package deployment intent must require edge_readiness")

    selection = (
        package_plan.get("selection")
        if isinstance(package_plan.get("selection"), dict)
        else {}
    )
    for field_name in (
        "package_id",
        "model_id",
        "device_id",
        "runtime_target_id",
        "slot",
    ):
        command_value = command_body.get(field_name)
        selection_value = selection.get(field_name)
        if command_value in (None, "") or selection_value in (None, ""):
            continue
        if str(command_value) != str(selection_value):
            raise ValueError(
                f"mission package deployment intent {field_name} does not match selection"
            )
    body: Dict[str, Any] = dict(command_body)
    for field_name in (
        "package_id",
        "model_id",
        "device_id",
        "runtime_target_id",
        "slot",
    ):
        if body.get(field_name) in (None, "") and selection.get(field_name) not in (
            None,
            "",
        ):
            body[field_name] = selection[field_name]
    if request.rollout_id:
        body["rollout_id"] = deployment_rollout_id
    if request.reason:
        body["reason"] = request.reason
    body.setdefault("reason", "mission package deployment handoff")
    body.setdefault("require_approval", True)
    body.setdefault("require_runtime_validation", True)

    if not body.get("package_id") or not body.get("device_id"):
        raise ValueError("mission package deployment intent requires package_id and device_id")

    integrity = (
        package_plan.get("integrity")
        if isinstance(package_plan.get("integrity"), dict)
        else {}
    )
    component_digests = (
        package_plan.get("component_digests")
        if isinstance(package_plan.get("component_digests"), dict)
        else {}
    )
    edge_handoff = (
        package_plan.get("edge_handoff")
        if isinstance(package_plan.get("edge_handoff"), dict)
        else {}
    )
    package_identity_sha256 = str(
        integrity.get("package_identity_sha256")
        or deployment_intent.get("package_identity_sha256")
        or ""
    )
    deployment_intent_sha256 = str(
        component_digests.get("deployment_intent_sha256")
        or canonical_json_hash(deployment_intent)
    )
    stage_proof = {
        "schema_version": "temms-edge-mission-package-stage/v1",
        "stage_gate": stage_gate,
        "package_identity_sha256": package_identity_sha256,
        "deployment_intent_sha256": deployment_intent_sha256,
        "command": {
            "method": "POST",
            "path": "/v1/hub/rollouts",
            "body": body,
        },
    }
    if edge_handoff:
        stage_proof["edge_handoff"] = edge_handoff
    return body, stage_proof


def _mission_package_stage_gate(package_plan: Dict[str, Any]) -> Dict[str, Any]:
    proof_gate = (
        package_plan.get("proof_gate")
        if isinstance(package_plan.get("proof_gate"), dict)
        else {}
    )
    status = str(proof_gate.get("status") or "").lower()
    failures = proof_gate.get("failures") if isinstance(proof_gate.get("failures"), list) else []
    if status != "passed":
        detail = (
            "; ".join(str(failure) for failure in failures if failure)
            or "mission package proof gate is not passed"
        )
        raise ValueError(f"mission package proof gate must pass before staging: {detail}")
    return {
        "status": "passed",
        "proof_gate_status": status,
        "requires": {
            "proof_gate": "passed",
            "package_identity": "verified",
            "deployment_intent": "verified",
        },
    }


def _normalize_mission_package_stage_path(path: str) -> str:
    normalized = path.strip()
    if normalized.startswith("/v1/hub/"):
        normalized = normalized.removeprefix("/v1/hub")
    if normalized == "v1/hub/rollouts":
        normalized = "/rollouts"
    if normalized == "rollouts":
        normalized = "/rollouts"
    return normalized


def _verify_mission_package_stage_integrity(
    package_plan: Dict[str, Any],
    deployment_intent: Dict[str, Any],
) -> None:
    integrity = (
        package_plan.get("integrity")
        if isinstance(package_plan.get("integrity"), dict)
        else {}
    )
    package_identity = (
        package_plan.get("package_identity")
        if isinstance(package_plan.get("package_identity"), dict)
        else {}
    )
    component_digests = (
        package_plan.get("component_digests")
        if isinstance(package_plan.get("component_digests"), dict)
        else {}
    )

    payload_sha256 = integrity.get("payload_sha256")
    if payload_sha256:
        unsigned_package = dict(package_plan)
        unsigned_package.pop("integrity", None)
        if canonical_json_hash(unsigned_package) != str(payload_sha256):
            raise ValueError("mission package payload digest does not match artifact body")

    expected_deployment_intent_sha256 = component_digests.get(
        "deployment_intent_sha256"
    )
    if expected_deployment_intent_sha256 and canonical_json_hash(
        deployment_intent
    ) != str(expected_deployment_intent_sha256):
        raise ValueError("mission package deployment intent digest does not match body")

    identity_values = [
        str(value)
        for value in (
            integrity.get("package_identity_sha256"),
            package_identity.get("package_identity_sha256"),
            deployment_intent.get("package_identity_sha256"),
            deployment_intent.get("mission_package_core_sha256"),
        )
        if value
    ]
    if len(set(identity_values)) > 1:
        raise ValueError("mission package identity digests do not agree")
    if identity_values:
        computed_identity = edge_mission_package_identity_hash(package_plan)
        if computed_identity != identity_values[0]:
            raise ValueError("mission package identity digest does not match artifact body")


def _mission_package_request_payload(request: MissionPackagePlanRequest) -> Dict[str, Any]:
    """Return request fields with missing package-planning values filled from mission YAML."""
    return hydrate_mission_spec_from_yaml(request.model_dump(exclude_none=True))


@hub_router.get("/edge-runtime-proof")
async def edge_runtime_proof(
    http_request: Request,
    package_id: Optional[str] = None,
    model_id: Optional[str] = None,
    device_id: Optional[str] = None,
    runtime_target_id: Optional[str] = None,
    slot: Optional[str] = None,
    source_action: str = "edge-runtime-mission",
    require_go: bool = False,
    min_runtime_fit: Optional[float] = None,
    require_best_runtime: bool = False,
    require_capability_lock: bool = False,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Return a hash-verifiable proof envelope for the selected edge runtime path."""
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    try:
        readiness = _deployment_readiness_with_evidence(
            state,
            hub,
            package_id=package_id,
            model_id=model_id,
            device_id=device_id,
            runtime_target_id=runtime_target_id,
            slot=slot,
        )
        return build_edge_runtime_proof(
            readiness,
            source_action=source_action,
            require_go=require_go,
            min_runtime_fit=min_runtime_fit,
            require_best_runtime=require_best_runtime,
            require_capability_lock=require_capability_lock,
            signing_key=_edge_runtime_proof_signing_key(state),
            signer="temms-hub-lite",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.get("/edge-runtime-proof/download")
async def download_edge_runtime_proof(
    http_request: Request,
    package_id: Optional[str] = None,
    model_id: Optional[str] = None,
    device_id: Optional[str] = None,
    runtime_target_id: Optional[str] = None,
    slot: Optional[str] = None,
    source_action: str = "edge-runtime-mission",
    require_go: bool = False,
    min_runtime_fit: Optional[float] = None,
    require_best_runtime: bool = False,
    require_capability_lock: bool = False,
    state: AppState = Depends(get_state),
) -> Response:
    """Download the selected edge runtime proof as a JSON artifact."""
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    try:
        readiness = _deployment_readiness_with_evidence(
            state,
            hub,
            package_id=package_id,
            model_id=model_id,
            device_id=device_id,
            runtime_target_id=runtime_target_id,
            slot=slot,
        )
        proof = build_edge_runtime_proof(
            readiness,
            source_action=source_action,
            require_go=require_go,
            min_runtime_fit=min_runtime_fit,
            require_best_runtime=require_best_runtime,
            require_capability_lock=require_capability_lock,
            signing_key=_edge_runtime_proof_signing_key(state),
            signer="temms-hub-lite",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    filename = _edge_runtime_proof_filename(proof)
    payload = (
        json.dumps(proof, indent=2, sort_keys=True, default=str).encode("utf-8")
        + b"\n"
    )
    integrity = proof.get("integrity") if isinstance(proof.get("integrity"), dict) else {}
    attestation = (
        integrity.get("attestation")
        if isinstance(integrity.get("attestation"), dict)
        else {}
    )
    proof_hash = str(integrity.get("payload_sha256", ""))
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-TEMMS-Edge-Proof-Filename": filename,
        "X-TEMMS-Edge-Proof-Gate-Status": str(proof.get("gate_status", "")),
        "X-TEMMS-Edge-Proof-SHA256": proof_hash,
        "X-TEMMS-Edge-Proof-Attestation": "signed" if attestation else "unsigned",
    }
    component_digests = (
        proof.get("component_digests")
        if isinstance(proof.get("component_digests"), dict)
        else {}
    )
    component_digest_headers = {
        "runtime_workbench_sha256": "X-TEMMS-Edge-Proof-Runtime-Workbench-SHA256",
        "runtime_decision_trace_sha256": (
            "X-TEMMS-Edge-Proof-Runtime-Decision-Trace-SHA256"
        ),
        "edge_execution_manifest_sha256": (
            "X-TEMMS-Edge-Proof-Execution-Manifest-SHA256"
        ),
    }
    for digest_key, header_name in component_digest_headers.items():
        digest = component_digests.get(digest_key)
        if digest:
            headers[header_name] = str(digest)
    if attestation.get("key_fingerprint"):
        headers["X-TEMMS-Edge-Proof-Key-Fingerprint"] = str(
            attestation["key_fingerprint"]
        )
    return Response(
        content=payload,
        media_type="application/json",
        headers=headers,
    )


def _edge_runtime_proof_signing_key(state: AppState) -> Optional[str]:
    """Return the daemon signing key used to attest edge-runtime proof artifacts."""
    _require_signature, signing_key = rollout_signature_policy(state)
    return signing_key


def _edge_runtime_proof_filename(proof: Dict[str, Any]) -> str:
    selection = proof.get("selection") if isinstance(proof.get("selection"), dict) else {}
    parts = [
        str(selection.get("model_id") or ""),
        str(selection.get("runtime_target_id") or ""),
        str(selection.get("device_id") or ""),
    ]
    slug = "-".join(part for part in (_edge_runtime_proof_slug(part) for part in parts) if part)
    slug = slug[:140].strip("-")
    return f"temms-edge-runtime-proof{f'-{slug}' if slug else ''}.json"


def _mission_package_filename(package_plan: Dict[str, Any]) -> str:
    selection = (
        package_plan.get("selection")
        if isinstance(package_plan.get("selection"), dict)
        else {}
    )
    parts = [
        str(selection.get("model_id") or ""),
        str(selection.get("runtime_target_id") or ""),
        str(selection.get("device_id") or ""),
    ]
    slug = "-".join(part for part in (_edge_runtime_proof_slug(part) for part in parts) if part)
    slug = slug[:140].strip("-")
    return f"temms-edge-mission-package{f'-{slug}' if slug else ''}.json"


def _edge_runtime_proof_slug(value: str) -> str:
    output: list[str] = []
    previous_dash = False
    for char in value.lower():
        if char.isascii() and char.isalnum():
            output.append(char)
            previous_dash = False
        elif not previous_dash:
            output.append("-")
            previous_dash = True
    return "".join(output).strip("-")


@hub_router.post("/compatibility/preview")
async def preview_rollout_compatibility(
    request: HubCompatibilityPreviewRequest,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Preview package/device/runtime compatibility without assigning a rollout."""
    hub = get_hub_store(state)
    try:
        return hub.preview_rollout_compatibility(
            device_id=request.device_id,
            package_id=request.package_id,
            runtime_target_id=request.runtime_target_id,
            model_id=request.model_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.post("/compatibility/matrix")
async def rollout_compatibility_matrix(
    request: HubCompatibilityMatrixRequest,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Return a package/device/runtime compatibility matrix."""
    hub = get_hub_store(state)
    try:
        return hub.compatibility_matrix(
            package_ids=request.package_ids,
            model_ids=request.model_ids,
            device_ids=request.device_ids,
            runtime_target_ids=request.runtime_target_ids,
            include_device_inventory=request.include_device_inventory,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.get("/runtime-targets/validations")
async def list_runtime_validations(
    http_request: Request,
    package_id: Optional[str] = None,
    runtime_target_id: Optional[str] = None,
    limit: Optional[int] = None,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """List recorded runtime target validation evidence."""
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    validations = hub.list_runtime_validations(
        package_id=package_id,
        runtime_target_id=runtime_target_id,
        limit=limit,
    )
    return {"runtime_validations": validations, "count": len(validations)}


@hub_router.post("/runtime-targets/validations")
async def record_runtime_validation(
    request: RuntimeValidationRecordRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Record runtime target validation evidence."""
    require_rbac_role(http_request, state, "operator", "edge")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        validation = hub.record_runtime_validation(
            request.runtime_target_id,
            request.result,
            package_id=request.package_id,
            package_path=request.package_path,
            actor=actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return validation


@hub_router.get("/benchmarks")
async def list_hub_benchmarks(
    http_request: Request,
    device_id: Optional[str] = None,
    package_id: Optional[str] = None,
    runtime_target_id: Optional[str] = None,
    model_id: Optional[str] = None,
    limit: Optional[int] = None,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """List hardware-aware benchmark evidence recorded by edge devices."""
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    benchmarks = hub.list_benchmarks(
        device_id=device_id,
        package_id=package_id,
        runtime_target_id=runtime_target_id,
        model_id=model_id,
        limit=limit,
    )
    return {"benchmarks": benchmarks, "count": len(benchmarks)}


@hub_router.post("/benchmarks")
async def record_hub_benchmark(
    request: BenchmarkRecordRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Record one hardware-aware benchmark result from an edge device."""
    require_rbac_role(http_request, state, "operator", "edge")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="edge-agent")
    try:
        benchmark = hub.record_benchmark(
            request.result,
            device_id=request.device_id,
            package_id=request.package_id,
            runtime_target_id=request.runtime_target_id,
            actor=actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return benchmark


@hub_router.get("/packages/{package_id}/artifact")
async def download_hub_package_artifact(
    package_id: str,
    request: Request,
    state: AppState = Depends(get_state),
) -> Response:
    """Download a package archive for online edge sync."""
    require_rbac_role(request, state, "operator", "edge")
    hub = get_hub_store(state)
    try:
        artifact = hub.package_artifact(package_id)
    except PackageArtifactIntegrityError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    filename = Path(artifact["filename"]).name
    return Response(
        content=artifact["content"],
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-TEMMS-Package-Filename": filename,
            "X-TEMMS-Package-SHA256": artifact["sha256"],
            "X-TEMMS-Package-Source-SHA256": artifact.get("source_sha256", ""),
            "X-TEMMS-Package-Artifact-SHA256": artifact["sha256"],
        },
    )


@hub_router.post("/rollouts")
async def assign_rollout(
    request: RolloutAssignRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    require_signature, _ = rollout_signature_policy(state, resolve_key=False)
    package = hub.get_package(request.package_id)
    if require_signature and package is not None and not package_signature_verified(package):
        raise HTTPException(
            status_code=400,
            detail=f"Package {request.package_id} does not have a verified signature",
        )
    if require_signature and package is not None and not package_strict_metadata_verified(package):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Package {request.package_id} does not have strict production "
                "metadata validation"
            ),
        )
    try:
        rollout = hub.assign_rollout(
            device_id=request.device_id,
            package_id=request.package_id,
            model_id=request.model_id,
            slot=request.slot,
            rollout_id=request.rollout_id,
            runtime_target_id=request.runtime_target_id,
            require_runtime_validation=request.require_runtime_validation,
            require_approval=request.require_approval,
            actor=actor,
            reason=request.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return rollout


@hub_router.get("/rollouts")
async def list_rollouts(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    hub = get_hub_store(state)
    return {"rollouts": hub.list_rollouts()}


@hub_router.post("/rollout-plans")
async def create_rollout_plan(
    request: RolloutPlanCreateRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Create a coordinated rollout plan across multiple devices."""
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    require_signature, _ = rollout_signature_policy(state, resolve_key=False)
    package = hub.get_package(request.package_id)
    if require_signature and package is not None and not package_signature_verified(package):
        raise HTTPException(
            status_code=400,
            detail=f"Package {request.package_id} does not have a verified signature",
        )
    if require_signature and package is not None and not package_strict_metadata_verified(package):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Package {request.package_id} does not have strict production "
                "metadata validation"
            ),
        )
    try:
        return hub.create_rollout_plan(
            plan_id=request.plan_id,
            package_id=request.package_id,
            model_id=request.model_id,
            device_ids=request.device_ids,
            slot=request.slot,
            runtime_target_id=request.runtime_target_id,
            batch_size=request.batch_size,
            require_runtime_validation=request.require_runtime_validation,
            require_approval=request.require_approval,
            actor=actor,
            reason=request.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.get("/rollout-plans")
async def list_rollout_plans(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """List coordinated rollout plans."""
    hub = get_hub_store(state)
    plans = hub.list_rollout_plans()
    return {"rollout_plans": plans, "count": len(plans)}


@hub_router.post("/rollout-plans/{plan_id}/advance")
async def advance_rollout_plan(
    plan_id: str,
    request: RolloutPlanAdvanceRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Assign the next rollout-plan batch."""
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        return hub.advance_rollout_plan(plan_id, limit=request.limit, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.post("/rollout-plans/{plan_id}/pause")
async def pause_rollout_plan(
    plan_id: str,
    request: RolloutPlanStateRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Pause a rollout plan before assigning more batches."""
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        return hub.pause_rollout_plan(plan_id, actor=actor, reason=request.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.post("/rollout-plans/{plan_id}/resume")
async def resume_rollout_plan(
    plan_id: str,
    request: RolloutPlanStateRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Resume a paused rollout plan."""
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        return hub.resume_rollout_plan(plan_id, actor=actor, reason=request.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.post("/rollouts/{rollout_id}/status")
async def update_rollout_status(
    rollout_id: str,
    request: RolloutStatusRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(http_request, state, "operator", "edge")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="api")
    try:
        return hub.update_rollout_status(
            rollout_id,
            request.state,
            detail=request.detail,
            actor=actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.post("/rollouts/{rollout_id}/approve")
async def approve_rollout(
    rollout_id: str,
    request: RolloutApprovalRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Approve a Hub Lite rollout before it is applied on the edge."""
    require_rbac_role(http_request, state, "approver")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        return hub.approve_rollout(
            rollout_id,
            actor=actor,
            reason=request.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.post("/rollouts/{rollout_id}/apply")
async def apply_rollout(
    rollout_id: str,
    request: RolloutApplyRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Apply a local rollout by importing its package and activating the target slot."""
    require_rbac_role(http_request, state, "operator", "edge")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="edge-agent")
    rollout = hub.get_rollout(rollout_id)
    if rollout is None:
        raise HTTPException(status_code=404, detail=f"Unknown rollout: {rollout_id}")
    approval = rollout.get("approval") if isinstance(rollout.get("approval"), dict) else {}
    if rollout.get("approval_required") and approval.get("approved") is not True:
        raise HTTPException(
            status_code=409,
            detail=f"Rollout {rollout_id} requires approval before apply",
        )

    package_id = rollout.get("package_id")
    package = hub.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"Unknown package: {package_id}")

    package_path = package.get("path")
    if not package_path:
        raise HTTPException(status_code=400, detail=f"Package {package_id} has no path")

    try:
        from temms.core.package_archive import package_directory
        from temms.core.package import PackageImporter
        from temms.core.runtime_profiles import (
            detect_runtime_capabilities,
            package_runtime_constraints,
            runtime_constraints_satisfied,
        )

        package_source = hub.verified_package_path(package_id)
        with package_directory(package_source) as package_dir:
            manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
        requested_model_id = request.model_id or rollout.get("model_id") or (
            manifest.get("models", [{}])[0].get("id") if manifest.get("models") else None
        )
        _rollout_apply_preflight(
            state,
            hub,
            rollout,
            package_id=str(package_id),
            model_id=requested_model_id,
            actor=actor,
        )
        hub.update_rollout_status(
            rollout_id,
            "downloading",
            detail="using local package path",
            actor=actor,
        )
        device = hub.get_device(rollout.get("device_id")) if rollout.get("device_id") else None
        capabilities = detect_runtime_capabilities().to_dict()
        if device:
            inventory = device.get("inventory") if isinstance(device.get("inventory"), dict) else {}
            for key in ("os", "machine", "python", "board_model"):
                if inventory.get(key):
                    capabilities[key] = inventory[key]
            for key in ("runtimes", "accelerators"):
                inventory_values = inventory.get(key)
                if not isinstance(inventory_values, dict):
                    continue
                merged_values = dict(capabilities.get(key) or {})
                for name, status in inventory_values.items():
                    if isinstance(status, dict) and isinstance(merged_values.get(name), dict):
                        merged_status = dict(merged_values[name])
                        merged_status.update(status)
                        merged_values[name] = merged_status
                    else:
                        merged_values[name] = status
                capabilities[key] = merged_values
            if device.get("profile") and device.get("profile") != "unknown":
                capabilities["device_profile"] = device["profile"]
            elif inventory.get("device_profile") and inventory.get("device_profile") != "unknown":
                capabilities["device_profile"] = inventory["device_profile"]
        failed_constraints: list[str] = []
        for constrained_model_id, constraints in package_runtime_constraints(
            manifest,
            model_id=requested_model_id,
        ):
            satisfied, reasons = runtime_constraints_satisfied(constraints, capabilities)
            if not satisfied:
                failed_constraints.extend(f"{constrained_model_id}: {reason}" for reason in reasons)
        if failed_constraints:
            raise ValueError(
                "Runtime constraints are not satisfied: " + "; ".join(failed_constraints)
            )

        cache_dir = (
            state.daemon_config.model_dir.parent / "cache"
            if state.daemon_config is not None
            else state.model_cache.db_path.parent / "cache"
        )
        active_policy_dir = (
            state.daemon_config.policy_dir if state.daemon_config is not None else None
        )
        require_signature, signing_key = rollout_signature_policy(
            state,
            require_signature=request.require_signature,
            signing_key=request.signing_key,
        )
        importer = PackageImporter(
            cache_dir=cache_dir,
            model_cache=state.model_cache,
            storage=state.model_storage,
            active_policy_dir=active_policy_dir,
            require_signature=require_signature,
            signing_key=signing_key,
            device_profile=capabilities.get("device_profile"),
            check_runtime_constraints=False,
        )
        result = importer.import_package(package_source, verify=True)
        reloaded_policies = reload_active_policy_store(state)
        hub.update_rollout_status(
            rollout_id,
            "imported",
            detail=(
                f"package imported; active policies reloaded: {reloaded_policies}"
                if active_policy_dir is not None
                else "package imported"
            ),
            actor=actor,
        )

        slot_name = rollout.get("slot")
        if not slot_name:
            return {
                "status": "imported",
                "rollout": hub.get_rollout(rollout_id),
                "models": [model.id for model in result.models],
            }

        slot = state.slot_manager.get_slot(slot_name)
        if slot is None:
            raise ValueError(f"Slot not found: {slot_name}")

        model_id = requested_model_id or (result.models[0].id if result.models else None)
        if model_id is None:
            raise ValueError("Package imported no models to activate")
        if state.model_cache.get_model(model_id) is None:
            raise ValueError(f"Model not found after import: {model_id}")

        await state.inference_runtime.load_model(slot_name, model_id)
        audit_metadata = {
            **model_audit_metadata(state, model_id),
            "rollout_id": rollout_id,
            "actor": actor,
        }
        state.slot_manager.activate_model(
            slot_name=slot_name,
            model_id=model_id,
            trigger_type="rollout",
            trigger_detail=rollout_id,
            conditions=state.condition_store.get_snapshot(),
            audit_metadata=audit_metadata,
        )
        hub.update_rollout_status(
            rollout_id,
            "activated",
            detail=f"activated {model_id}",
            actor=actor,
        )
        emit_telemetry(
            state,
            "rollout.activated",
            {
                "rollout_id": rollout_id,
                "device_id": rollout.get("device_id"),
                "package_id": package_id,
                "slot": slot_name,
                "model_id": model_id,
                "model": audit_metadata,
                "actor": actor,
            },
        )
        device_id = rollout.get("device_id")
        if device_id:
            hub.heartbeat(
                device_id,
                status="online",
                deployment_status={
                    "rollout_id": rollout_id,
                    "package_id": package_id,
                    "slot": slot_name,
                    "model_id": model_id,
                    "state": "activated",
                },
            )

        return {
            "status": "activated",
            "rollout": hub.get_rollout(rollout_id),
            "slot": slot_name,
            "model": model_id,
        }

    except HTTPException:
        raise

    except PackageArtifactIntegrityError as e:
        hub.update_rollout_status(rollout_id, "failed", detail=str(e), actor=actor)
        emit_telemetry(
            state,
            "rollout.failed",
            {
                "rollout_id": rollout_id,
                "package_id": package_id,
                "detail": str(e),
                "actor": actor,
            },
        )
        raise HTTPException(status_code=409, detail=f"Rollout apply failed: {e}")

    except Exception as e:
        hub.update_rollout_status(rollout_id, "failed", detail=str(e), actor=actor)
        emit_telemetry(
            state,
            "rollout.failed",
            {
                "rollout_id": rollout_id,
                "package_id": package_id,
                "detail": str(e),
                "actor": actor,
            },
        )
        raise HTTPException(status_code=500, detail=f"Rollout apply failed: {e}")


@hub_router.post("/rollouts/{rollout_id}/rollback")
async def rollback_rollout(
    rollout_id: str,
    http_request: Request,
    request: RolloutRollbackRequest = Body(default_factory=RolloutRollbackRequest),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Rollback the slot targeted by a Hub Lite rollout."""
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    rollout = hub.get_rollout(rollout_id)
    if rollout is None:
        raise HTTPException(status_code=404, detail=f"Unknown rollout: {rollout_id}")

    slot_name = rollout.get("slot")
    if not slot_name:
        raise HTTPException(status_code=400, detail=f"Rollout {rollout_id} has no slot")

    try:
        result = await _rollback_slot_to_previous_model(
            state=state,
            slot_name=slot_name,
            trigger_detail=rollout_id,
            telemetry_payload={
                "rollout_id": rollout_id,
                "device_id": rollout.get("device_id"),
                "package_id": rollout.get("package_id"),
                "reason": request.reason,
                "actor": actor,
            },
            rollout_id=rollout_id,
            actor=actor,
        )
    except HTTPException:
        raise
    except Exception as e:
        hub.update_rollout_status(
            rollout_id,
            "failed",
            detail=f"rollback failed: {e}",
            actor=actor,
        )
        raise HTTPException(status_code=500, detail=f"Rollout rollback failed: {e}")

    emit_telemetry(
        state,
        "rollout.rolled_back",
        {
            "rollout_id": rollout_id,
            "slot": slot_name,
            "model_id": result["model"],
            "reason": request.reason,
            "actor": actor,
        },
    )
    return {
        "status": "rolled_back",
        "rollout": hub.get_rollout(rollout_id),
        "slot": slot_name,
        "model": result["model"],
        "timestamp": result["timestamp"],
    }


@hub_router.get("/deployment-status")
async def deployment_status(
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    return hub.deployment_status()


@hub_router.post("/telemetry/replay")
async def replay_hub_telemetry(
    request: HubTelemetryReplayRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Ingest exported edge telemetry into Hub Lite after an offline mission."""
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        replay = hub.replay_telemetry_bundle(
            request.bundle,
            device_id=request.device_id,
            actor=actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "replay": replay}


@hub_router.get("/telemetry")
async def list_hub_telemetry(
    http_request: Request,
    limit: Optional[int] = None,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Return telemetry events replayed into Hub Lite."""
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    events = hub.telemetry_events(limit=limit)
    return {"events": events, "count": len(events)}


@hub_router.post("/evidence/ingest")
async def ingest_hub_evidence(
    request: HubEvidenceIngestRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Ingest a full exported edge evidence bundle into Hub Lite."""
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        evidence = hub.ingest_evidence_bundle(
            request.bundle,
            device_id=request.device_id,
            actor=actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "evidence": evidence}


@hub_router.get("/evidence")
async def list_hub_evidence(
    http_request: Request,
    limit: Optional[int] = None,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Return full evidence bundles aggregated into Hub Lite."""
    require_rbac_role(http_request, state, "operator", "auditor")
    hub = get_hub_store(state)
    evidence = hub.list_evidence_bundles(limit=limit)
    return {"evidence_bundles": evidence, "count": len(evidence)}


@hub_router.post("/airgap/export")
async def export_airgap_bundle(
    http_request: Request,
    request: AirgapExportRequest = Body(default_factory=AirgapExportRequest),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(http_request, state, "operator")
    hub = get_hub_store(state)
    try:
        return hub.export_bundle(include_packages=request.include_packages)
    except PackageArtifactIntegrityError as e:
        raise HTTPException(status_code=409, detail=str(e))


@hub_router.post("/evidence/export")
async def export_evidence_bundle(
    request: EvidenceExportRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Export Hub Lite plus edge audit evidence for post-mission review."""
    require_rbac_role(http_request, state, "operator", "auditor")
    get_hub_store(state)
    from temms.evidence import (
        build_evidence_bundle,
        build_mission_replay,
        summarize_evidence_bundle,
    )

    bundle = build_evidence_bundle(
        state,
        telemetry_limit=request.telemetry_limit,
        decision_limit=request.decision_limit,
        include_benchmarks=request.include_benchmarks,
    )
    if request.replay:
        return build_mission_replay(bundle, limit=request.replay_limit)
    if request.summary:
        return summarize_evidence_bundle(bundle, limit=request.summary_limit)
    return bundle


@hub_router.post("/airgap/import")
async def import_airgap_bundle(
    bundle: Dict[str, Any],
    request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    require_rbac_role(request, state, "operator")
    hub = get_hub_store(state)
    try:
        counts = hub.import_bundle(bundle)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Air-gap bundle import failed")
        raise HTTPException(status_code=500, detail=f"Air-gap bundle import failed: {e}")
    return {"status": "success", "imported": counts}


# Default app instance (for direct uvicorn usage with default config)
# In production, use create_app() with proper dependencies
app = FastAPI(
    title="TEMMS",
    description="Tactical Edge Model Management System - Inference API",
    version="0.1.0",
)


@app.get("/v1/health")
async def default_health():
    """Health endpoint for default app."""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}
