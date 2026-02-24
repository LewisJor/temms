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
from typing import Any, Dict, List, Optional
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from pydantic import BaseModel, Field

from temms.slots.manager import SlotManager, SlotState
from temms.conditions.store import ConditionStore
from temms.policy.engine import PolicyEngine
from temms.core.cache import ModelCache
from temms.core.storage import ModelStorage
from temms.inference.runtime import InferenceRuntime

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


# Global state - set during app creation
_app_state: Optional[AppState] = None


def get_state() -> AppState:
    """Dependency to get app state."""
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

    # Register routes
    application.include_router(inference_router)
    application.include_router(control_router)
    application.include_router(status_router)

    return application


# ----- Routers -----

from fastapi import APIRouter

inference_router = APIRouter(prefix="/v1", tags=["inference"])
control_router = APIRouter(prefix="/v1/control", tags=["control"])
status_router = APIRouter(prefix="/v1", tags=["status"])


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
            status_code=503,
            detail=f"Slot '{slot_name}' is not running (state: {slot.state.value})"
        )

    if slot.active_model_id is None:
        raise HTTPException(
            status_code=503,
            detail=f"Slot '{slot_name}' has no active model"
        )

    # 2. Get model info
    model = state.model_cache.get_model(slot.active_model_id)
    if model is None:
        raise HTTPException(
            status_code=503,
            detail=f"Active model not found in cache: {slot.active_model_id}"
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
    # Validate slot exists
    slot = state.slot_manager.get_slot(slot_name)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Slot not found: {slot_name}")

    # Validate model exists in cache
    model = state.model_cache.find_model(request.model)
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"Model not found in cache: {request.model}"
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
    state.slot_manager.activate_model(
        slot_name=slot_name,
        model_id=model.id,
        trigger_type="operator",
        trigger_detail=request.reason or "manual override",
        conditions=state.condition_store.get_snapshot(),
    )

    logger.info(
        f"Operator override: slot={slot_name}, model={model.id}, reason={request.reason}"
    )

    return {
        "status": "success",
        "slot": slot_name,
        "model": model.id,
        "reason": request.reason,
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
        logger.info(f"Condition updated via API: {path}={value}")

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
