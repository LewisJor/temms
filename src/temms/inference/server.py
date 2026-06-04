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

import time
import logging
import json
from typing import Any, Dict, List, Optional
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Request, Body, Response
from prometheus_client import make_asgi_app
from pydantic import BaseModel, Field

from temms.slots.manager import SlotManager, SlotState
from temms.conditions.store import ConditionStore
from temms.policy.engine import PolicyEngine
from temms.core.cache import ModelCache
from temms.core.storage import ModelStorage
from temms.inference.runtime import InferenceRuntime
from temms.hub_lite import PackageArtifactIntegrityError
from temms.observability import (
    inference_request_count,
    inference_latency_ms,
    condition_update_count,
    deployment_count,
)

logger = logging.getLogger(__name__)


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
    runtime_target_id: Optional[str] = None


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
    slot: Optional[str] = None
    runtime_target_id: Optional[str] = None
    require_runtime_validation: bool = False
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


class EvidenceExportRequest(BaseModel):
    """Evidence bundle export request."""

    telemetry_limit: Optional[int] = None
    decision_limit: int = 100
    include_benchmarks: bool = True


class AirgapExportRequest(BaseModel):
    """Hub Lite air-gap export request."""

    include_packages: bool = False


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
        self.hub_lite = None
        self.telemetry = None


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
    _app_state.hub_lite = hub_lite
    _app_state.telemetry = telemetry

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
    if not expected:
        return

    supplied = request.headers.get("x-temms-token")
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        supplied = auth_header.split(" ", 1)[1]

    if supplied != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing control token")


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

    # 4. Run inference
    try:
        predictions = await state.inference_runtime.infer(
            slot_name=slot_name,
            model_id=slot.active_model_id,
            input_data=input_data,
            content_type=file.content_type or "application/octet-stream",
        )
    except Exception as e:
        logger.error(f"Inference failed for slot {slot_name}: {e}")
        raise HTTPException(status_code=500, detail=f"Inference failed: {str(e)}")

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
            "content_type": file.content_type or "application/octet-stream",
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


# ----- Control Endpoints -----


@control_router.post("/slots/{slot_name}/model")
async def override_model(
    slot_name: str,
    request: SlotOverrideRequest,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """
    Operator override - force specific model on a slot.

    This bypasses policy evaluation until cleared.
    """
    if state.offline_mode and state.pending_operations is not None:
        state.pending_operations.enqueue(
            "override_model",
            {
                "slot_name": slot_name,
                "request": request.model_dump(),
            },
        )
        return {
            "status": "buffered",
            "slot": slot_name,
            "offline": True,
            "timestamp": datetime.now().isoformat(),
        }

    # Validate slot exists
    slot = state.slot_manager.get_slot(slot_name)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Slot not found: {slot_name}")

    # Validate model exists in cache
    model = state.model_cache.find_model(request.model)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model not found in cache: {request.model}")

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
    state.slot_manager.activate_model(
        slot_name=slot_name,
        model_id=model.id,
        trigger_type="operator",
        trigger_detail=request.reason or "manual override",
        conditions=state.condition_store.get_snapshot(),
        audit_metadata=model_audit_metadata(state, model.id),
    )
    emit_telemetry(
        state,
        "slot.override",
        {
            "slot": slot_name,
            "model_id": model.id,
            "reason": request.reason,
            "duration_s": request.duration_s,
            "model": model_audit_metadata(state, model.id),
        },
    )

    logger.info(f"Operator override: slot={slot_name}, model={model.id}, reason={request.reason}")

    return {
        "status": "success",
        "slot": slot_name,
        "model": model.id,
        "reason": request.reason,
        "timestamp": datetime.now().isoformat(),
    }


@control_router.post("/slots/{slot_name}/rollback")
async def rollback_slot(
    slot_name: str,
    request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Rollback a slot to the previous active model in the decision log."""
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

    if state.model_cache.get_model(previous_model_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"Rollback model not found in cache: {previous_model_id}",
        )

    try:
        await state.inference_runtime.load_model(slot_name, previous_model_id)
    except Exception as e:
        logger.error(f"Rollback failed for slot {slot_name}: {e}")
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")

    state.slot_manager.activate_model(
        slot_name=slot_name,
        model_id=previous_model_id,
        trigger_type="rollback",
        trigger_detail="api",
        conditions=state.condition_store.get_snapshot(),
        audit_metadata=model_audit_metadata(state, previous_model_id),
    )
    emit_telemetry(
        state,
        "slot.rollback",
        {
            "slot": slot_name,
            "model_id": previous_model_id,
            "from_model": slot.active_model_id,
            "model": model_audit_metadata(state, previous_model_id),
            **telemetry_payload,
        },
    )

    if state.hub_lite is not None and rollout_id is not None:
        state.hub_lite.update_rollout_status(
            rollout_id,
            "rolled_back",
            detail=f"slot {slot_name} rolled back to {previous_model_id}",
            actor=actor,
        )
    elif state.hub_lite is not None and update_rollout_for_slot:
        for rollout in state.hub_lite.list_rollouts():
            if rollout.get("slot") == slot_name:
                try:
                    state.hub_lite.update_rollout_status(
                        rollout["rollout_id"],
                        "rolled_back",
                        detail=f"slot {slot_name} rolled back to {previous_model_id}",
                        actor=actor,
                    )
                except Exception:
                    pass

    return {
        "status": "success",
        "slot": slot_name,
        "model": previous_model_id,
        "rollout_id": rollout_id,
        "timestamp": datetime.now().isoformat(),
    }


@control_router.post("/conditions", response_model=ConditionUpdateResponse)
async def update_conditions(
    request: ConditionUpdateRequest,
    state: AppState = Depends(get_state),
) -> ConditionUpdateResponse:
    """
    Inject conditions from operator or external source.

    These are set with operator priority (1000) to override sensor data.
    """
    if state.offline_mode and state.pending_operations is not None:
        state.pending_operations.enqueue(
            "update_conditions",
            {"conditions": request.conditions},
        )
        return ConditionUpdateResponse(
            updated=list(request.conditions.keys()),
            timestamp=datetime.now().isoformat(),
        )

    updated = []

    for path, value in request.conditions.items():
        state.condition_store.set(
            path=path,
            value=value,
            source="operator_api",
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
            "source": "operator_api",
        },
    )

    return ConditionUpdateResponse(
        updated=updated,
        timestamp=datetime.now().isoformat(),
    )


@control_router.delete("/conditions/overrides")
async def clear_condition_overrides(
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Clear all operator condition overrides."""
    count = state.condition_store.clear_operator_overrides()
    logger.info(f"Cleared {count} operator condition overrides")

    return {
        "status": "success",
        "cleared_count": count,
        "timestamp": datetime.now().isoformat(),
    }


@control_router.post("/offline")
async def set_offline(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    state.offline_mode = True
    if state.daemon_config is not None:
        state.daemon_config.offline_mode = True
    if state.deployment_state:
        state.deployment_state.set_state("OFFLINE", "api_offline")
    return {"status": "success", "offline_mode": True}


@control_router.post("/online")
async def set_online(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    state.offline_mode = False
    if state.daemon_config is not None:
        state.daemon_config.offline_mode = False
    return {"status": "success", "offline_mode": False}


@control_router.post("/sync")
async def sync_pending(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    if state.pending_operations is None:
        return {"status": "success", "replayed": 0}
    entries = state.pending_operations.read_all()
    replayed = 0

    for entry in entries:
        operation = entry.get("operation")
        payload = entry.get("payload", {})

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
            slot_name = payload.get("slot_name")
            req = payload.get("request", {})
            model_name = req.get("model")
            if slot_name and model_name:
                model = state.model_cache.find_model(model_name)
                if model is not None:
                    await state.inference_runtime.load_model(slot_name, model.id)
                    state.slot_manager.set_operator_override(
                        slot_name=slot_name,
                        model_id=model.id,
                        reason=req.get("reason") or "offline replay",
                        source="api_sync",
                        duration_s=req.get("duration_s"),
                    )
                    state.slot_manager.activate_model(
                        slot_name=slot_name,
                        model_id=model.id,
                        trigger_type="operator",
                        trigger_detail=req.get("reason") or "offline replay",
                        conditions=state.condition_store.get_snapshot(),
                        audit_metadata=model_audit_metadata(state, model.id),
                    )
                    replayed += 1
        elif operation == "deploy":
            deployment_count.inc()
            replayed += 1

    state.pending_operations.clear()
    return {"status": "success", "replayed": replayed, "pending_cleared": len(entries)}


@control_router.post("/deploy")
async def request_deploy(
    request: Dict[str, Any],
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    deployment_count.inc()
    emit_telemetry(state, "deploy.requested", request)
    if state.offline_mode and state.pending_operations is not None:
        state.pending_operations.enqueue("deploy", request)
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
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Replay buffered telemetry locally, optionally clearing it."""
    if state.telemetry is None:
        raise HTTPException(status_code=503, detail="Telemetry buffer is not configured")
    return state.telemetry.replay(clear=request.clear)


@control_router.delete("/telemetry")
async def clear_telemetry(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """Clear buffered telemetry after successful off-box transfer."""
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
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
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
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
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
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="operator")
    try:
        return hub.upsert_runtime_target(
            request.model_dump(exclude={"actor"}),
            actor=actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@hub_router.get("/runtime-targets/validations")
async def list_runtime_validations(
    package_id: Optional[str] = None,
    runtime_target_id: Optional[str] = None,
    limit: Optional[int] = None,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """List recorded runtime target validation evidence."""
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
    device_id: Optional[str] = None,
    package_id: Optional[str] = None,
    runtime_target_id: Optional[str] = None,
    model_id: Optional[str] = None,
    limit: Optional[int] = None,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """List hardware-aware benchmark evidence recorded by edge devices."""
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
    state: AppState = Depends(get_state),
) -> Response:
    """Download a package archive for online edge sync."""
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
            detail=f"Package {request.package_id} does not have strict production metadata validation",
        )
    try:
        rollout = hub.assign_rollout(
            device_id=request.device_id,
            package_id=request.package_id,
            slot=request.slot,
            rollout_id=request.rollout_id,
            runtime_target_id=request.runtime_target_id,
            require_runtime_validation=request.require_runtime_validation,
            actor=actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return rollout


@hub_router.get("/rollouts")
async def list_rollouts(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    hub = get_hub_store(state)
    return {"rollouts": hub.list_rollouts()}


@hub_router.post("/rollouts/{rollout_id}/status")
async def update_rollout_status(
    rollout_id: str,
    request: RolloutStatusRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
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


@hub_router.post("/rollouts/{rollout_id}/apply")
async def apply_rollout(
    rollout_id: str,
    request: RolloutApplyRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Apply a local rollout by importing its package and activating the target slot."""
    hub = get_hub_store(state)
    actor = request_actor(http_request, request.actor, default="edge-agent")
    rollout = hub.get_rollout(rollout_id)
    if rollout is None:
        raise HTTPException(status_code=404, detail=f"Unknown rollout: {rollout_id}")

    package_id = rollout.get("package_id")
    package = hub.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"Unknown package: {package_id}")

    package_path = package.get("path")
    if not package_path:
        raise HTTPException(status_code=400, detail=f"Package {package_id} has no path")

    hub.update_rollout_status(
        rollout_id,
        "downloading",
        detail="using local package path",
        actor=actor,
    )

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
        requested_model_id = request.model_id or (
            manifest.get("models", [{}])[0].get("id") if manifest.get("models") else None
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
async def deployment_status(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    hub = get_hub_store(state)
    return hub.deployment_status()


@hub_router.post("/telemetry/replay")
async def replay_hub_telemetry(
    request: HubTelemetryReplayRequest,
    http_request: Request,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Ingest exported edge telemetry into Hub Lite after an offline mission."""
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
    limit: Optional[int] = None,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Return telemetry events replayed into Hub Lite."""
    hub = get_hub_store(state)
    events = hub.telemetry_events(limit=limit)
    return {"events": events, "count": len(events)}


@hub_router.post("/airgap/export")
async def export_airgap_bundle(
    request: AirgapExportRequest = Body(default_factory=AirgapExportRequest),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    hub = get_hub_store(state)
    try:
        return hub.export_bundle(include_packages=request.include_packages)
    except PackageArtifactIntegrityError as e:
        raise HTTPException(status_code=409, detail=str(e))


@hub_router.post("/evidence/export")
async def export_evidence_bundle(
    request: EvidenceExportRequest,
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """Export Hub Lite plus edge audit evidence for post-mission review."""
    get_hub_store(state)
    from temms.evidence import build_evidence_bundle

    return build_evidence_bundle(
        state,
        telemetry_limit=request.telemetry_limit,
        decision_limit=request.decision_limit,
        include_benchmarks=request.include_benchmarks,
    )


@hub_router.post("/airgap/import")
async def import_airgap_bundle(
    bundle: Dict[str, Any],
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
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
