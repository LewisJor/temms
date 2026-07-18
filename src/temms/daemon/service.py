"""
Main TEMMS daemon - runs policy evaluation loop.

Responsibilities:
- Periodic condition collection (configurable interval)
- Policy evaluation per slot (event-driven + periodic)
- Operator override enforcement
- Model switching and preloading execution
- Inference server hosting
- Telemetry buffering
"""

import asyncio
import hashlib
import signal
import logging
import os
import socket
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime

from temms.slots.manager import SlotManager, SlotState
from temms.conditions.store import ConditionStore
from temms.conditions.collectors import AsyncConditionCollector, ConditionCollector
from temms.policy.engine import PolicyEngine
from temms.core.cache import ModelCache
from temms.core.storage import ModelStorage
from temms.inference.runtime import InferenceRuntime
from temms.inference.server import create_app
from temms.daemon.deployment_state import DeploymentStateStore, DeploymentState
from temms.daemon.pending_ops import PendingOperationsStore
from temms.hub_lite import HubLiteStore
from temms.telemetry import TelemetryBuffer
from temms.observability import (
    condition_update_count,
    policy_decision_count,
    runtime_health_gauge,
    set_ddil_gauges,
    set_deployment_state,
    swap_latency_ms,
    uptime_gauge,
)

logger = logging.getLogger(__name__)

SYSTEM_DATA_DIR = Path("/var/lib/temms")
SYSTEM_POLICY_DIR = Path("/etc/temms/policies")
COLLECTOR_HEALTH_PRIORITY = 900
DEMO_EDGE_MEMORY_TOTAL_MB = 8192.0
DEMO_EDGE_MEMORY_AVAILABLE_MB = 4096.0
DEMO_EDGE_STORAGE_TOTAL_MB = 32768.0
DEMO_EDGE_STORAGE_AVAILABLE_MB = 24576.0


class ActivationPreflightBlocked(RuntimeError):
    """Raised when local edge readiness refuses a model activation."""

    def __init__(self, message: str, *, readiness: Dict[str, Any], blocking_gates: List[Dict[str, Any]]):
        super().__init__(message)
        self.readiness = readiness
        self.blocking_gates = blocking_gates


def _condition_path_segment(value: str) -> str:
    """Normalize a free-form source name into a condition path segment."""
    segment = "".join(
        char.lower() if char.isalnum() or char == "_" else "_" for char in str(value)
    ).strip("_")
    return segment or "collector"


def _hub_base_url(url: str) -> str:
    """Normalize TEMMS_HUB_URL to the Hub Lite API prefix."""
    base = url.rstrip("/")
    if base.endswith("/v1/hub"):
        return base
    return f"{base}/v1/hub"


def _hub_headers(token: Optional[str]) -> Dict[str, str]:
    """Return auth headers for Hub Lite sync requests."""
    if not token:
        return {}
    return {"X-TEMMS-Token": token}


def _hub_error_payload(response: Any) -> Dict[str, Any]:
    """Return structured telemetry for a failed Hub Lite HTTP response."""
    payload: Dict[str, Any] = {
        "status_code": getattr(response, "status_code", None),
        "failure_kind": "http_error",
    }
    try:
        body = response.json()
    except Exception:
        payload["detail"] = getattr(response, "text", "")
        return payload

    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, dict):
        message = detail.get("message")
        blocking_gates = detail.get("blocking_gates")
        readiness = detail.get("readiness") if isinstance(detail.get("readiness"), dict) else {}
        payload.update(
            {
                "detail": detail,
                "message": message,
                "failure_kind": (
                    "readiness_preflight"
                    if message == "Rollout apply preflight failed" or blocking_gates
                    else "http_error"
                ),
                "blocking_gates": blocking_gates if isinstance(blocking_gates, list) else [],
                "blocking_gate_count": (
                    len(blocking_gates) if isinstance(blocking_gates, list) else 0
                ),
                "readiness_status": readiness.get("status"),
                "readiness_selection": readiness.get("selection"),
            }
        )
        return payload

    payload["detail"] = detail
    return payload


def _activation_blocking_gates(readiness: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return readiness gates that should block local policy/fallback activation."""
    blocked_gate_ids = {
        "model_package",
        "runtime_target",
        "performance_fit",
        "resource_envelope",
        "edge_target",
    }
    attention_gate_ids = {"performance_fit", "resource_envelope", "edge_target"}
    blocking: List[Dict[str, Any]] = []
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


def _gate_summary(gates: List[Dict[str, Any]]) -> str:
    parts = [
        f"{gate.get('label') or gate.get('gate_id')} {gate.get('state')}: {gate.get('detail')}"
        for gate in gates[:3]
    ]
    remaining = len(gates) - len(parts)
    if remaining > 0:
        parts.append(f"{remaining} more gate{'s' if remaining != 1 else ''}")
    return "; ".join(parts)


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    """Parse a floating point environment variable."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _float_or_none(value: Any) -> Optional[float]:
    """Return a finite float for numeric telemetry values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _apply_resource_floor(
    resource: Dict[str, Any],
    *,
    available_mb: float,
    total_mb: float,
) -> Dict[str, Any]:
    """Raise demo resource telemetry to a deterministic healthy floor."""
    current_available = _float_or_none(resource.get("available_mb"))
    current_total = _float_or_none(resource.get("total_mb"))
    floored_available = max(current_available or 0.0, available_mb)
    floored_total = max(current_total or 0.0, total_mb, floored_available)
    resource["available_mb"] = round(floored_available, 1)
    resource["total_mb"] = round(floored_total, 1)
    return resource


def _apply_demo_edge_inventory_floor(inventory: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the local Docker demo from starting in accidental resource drift."""
    memory = inventory.get("memory") if isinstance(inventory.get("memory"), dict) else {}
    storage = inventory.get("storage") if isinstance(inventory.get("storage"), dict) else {}
    inventory["memory"] = _apply_resource_floor(
        memory,
        available_mb=_env_float(
            "TEMMS_DEMO_EDGE_MEMORY_AVAILABLE_MB",
            DEMO_EDGE_MEMORY_AVAILABLE_MB,
        ),
        total_mb=_env_float("TEMMS_DEMO_EDGE_MEMORY_TOTAL_MB", DEMO_EDGE_MEMORY_TOTAL_MB),
    )
    inventory["storage"] = _apply_resource_floor(
        storage,
        available_mb=_env_float(
            "TEMMS_DEMO_EDGE_STORAGE_AVAILABLE_MB",
            DEMO_EDGE_STORAGE_AVAILABLE_MB,
        ),
        total_mb=_env_float("TEMMS_DEMO_EDGE_STORAGE_TOTAL_MB", DEMO_EDGE_STORAGE_TOTAL_MB),
    )
    inventory["simulated"] = True
    inventory["source"] = "docker-demo-heartbeat"
    inventory["demo_inventory"] = {
        "resource_floor": True,
        "memory_available_mb": inventory["memory"]["available_mb"],
        "storage_available_mb": inventory["storage"]["available_mb"],
    }
    return inventory


def _default_data_dir() -> Path:
    """Return the configured daemon data directory default."""
    return Path(os.environ.get("TEMMS_DATA_DIR", str(SYSTEM_DATA_DIR)))


def _user_state_dir() -> Path:
    """Return the non-root fallback state directory."""
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "temms"
    return Path.home() / ".local" / "state" / "temms"


def _path_can_be_created(path: Path) -> bool:
    """Return whether the current process should be able to create/use a path."""
    if path.exists():
        return os.access(path, os.W_OK)

    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return os.access(parent, os.W_OK)


def _resolve_non_root_path_defaults(config: "DaemonConfig") -> None:
    """Move system defaults to a user-writable state dir when running non-root."""
    default_data_dir = _default_data_dir()
    fallback_data_dir = _user_state_dir()

    if not _path_can_be_created(default_data_dir):
        default_paths = {
            "db_path": default_data_dir / "temms.db",
            "model_dir": default_data_dir / "models",
            "deployment_state_path": default_data_dir / "deployment_state.json",
            "pending_operations_path": default_data_dir / "pending_operations.json",
            "hub_state_path": default_data_dir / "hub_lite.json",
            "telemetry_path": default_data_dir / "telemetry.jsonl",
        }
        fallback_paths = {
            "db_path": fallback_data_dir / "temms.db",
            "model_dir": fallback_data_dir / "models",
            "deployment_state_path": fallback_data_dir / "deployment_state.json",
            "pending_operations_path": fallback_data_dir / "pending_operations.json",
            "hub_state_path": fallback_data_dir / "hub_lite.json",
            "telemetry_path": fallback_data_dir / "telemetry.jsonl",
        }
        for field_name, default_path in default_paths.items():
            if getattr(config, field_name) == default_path:
                setattr(config, field_name, fallback_paths[field_name])

    if config.policy_dir == SYSTEM_POLICY_DIR and not _path_can_be_created(SYSTEM_POLICY_DIR):
        config.policy_dir = fallback_data_dir / "policies"


def _read_optional_file(path: Optional[Path]) -> Optional[str]:
    """Read a small optional text file."""
    if path is None:
        return None
    return path.read_text(encoding="utf-8").strip()


def _rollout_history_entries_match(
    local_entry: Dict[str, Any],
    central_entry: Dict[str, Any],
) -> bool:
    """Return whether two rollout history entries represent the same transition."""
    return (
        local_entry.get("state") == central_entry.get("state")
        and local_entry.get("detail") == central_entry.get("detail")
        and local_entry.get("actor") == central_entry.get("actor")
    )


@dataclass
class DaemonConfig:
    """Daemon configuration."""

    # Intervals
    condition_interval_s: float = 5.0  # Collect conditions every 5s
    policy_interval_s: float = 1.0  # Evaluate policies every 1s

    # Inference server
    inference_host: str = "0.0.0.0"
    inference_port: int = 8080

    # Data paths
    db_path: Optional[Path] = None
    model_dir: Optional[Path] = None
    policy_dir: Optional[Path] = None

    # Behavior
    auto_start_slots: bool = True  # Start slots with default models
    max_inference_workers: int = 4
    deployment_state_path: Optional[Path] = None
    pending_operations_path: Optional[Path] = None
    hub_state_path: Optional[Path] = None
    telemetry_path: Optional[Path] = None
    offline_mode: bool = False
    api_token: Optional[str] = None
    hub_url: Optional[str] = None
    hub_token: Optional[str] = None
    hub_device_id: Optional[str] = None
    hub_device_profile: Optional[str] = None
    hub_sync_interval_s: float = 30.0
    edge_heartbeat_interval_s: float = 60.0
    hub_auto_apply: bool = False
    rollout_require_signature: bool = True
    rollout_signing_key: Optional[str] = None
    rollout_signing_key_file: Optional[Path] = None

    def __post_init__(self):
        """Set default paths if not provided."""
        if self.inference_host == "0.0.0.0":
            self.inference_host = (
                os.environ.get("TEMMS_INFERENCE_HOST")
                or os.environ.get("TEMMS_HOST")
                or self.inference_host
            )
        if self.inference_port == 8080:
            self.inference_port = _env_int(
                "TEMMS_INFERENCE_PORT",
                _env_int("TEMMS_PORT", self.inference_port),
            )

        if self.db_path is not None:
            base_dir = self.db_path.parent
        elif self.model_dir is not None:
            base_dir = self.model_dir.parent
        else:
            base_dir = _default_data_dir()

        if self.db_path is None:
            self.db_path = base_dir / "temms.db"
        if self.model_dir is None:
            self.model_dir = base_dir / "models"
        if self.policy_dir is None:
            self.policy_dir = Path("/etc/temms/policies")
        if self.deployment_state_path is None:
            self.deployment_state_path = base_dir / "deployment_state.json"
        if self.pending_operations_path is None:
            self.pending_operations_path = base_dir / "pending_operations.json"
        if self.hub_state_path is None:
            self.hub_state_path = base_dir / "hub_lite.json"
        if self.telemetry_path is None:
            self.telemetry_path = base_dir / "telemetry.jsonl"
        if self.api_token is None:
            self.api_token = os.environ.get("TEMMS_API_TOKEN")
        if self.hub_url is None:
            self.hub_url = os.environ.get("TEMMS_HUB_URL")
        if self.hub_token is None:
            self.hub_token = os.environ.get("TEMMS_HUB_TOKEN") or self.api_token
        if self.hub_device_id is None:
            self.hub_device_id = os.environ.get("TEMMS_DEVICE_ID") or socket.gethostname()
        if self.hub_device_profile is None:
            self.hub_device_profile = os.environ.get("TEMMS_DEVICE_PROFILE")
        env_sync_interval = os.environ.get("TEMMS_HUB_SYNC_INTERVAL_S")
        if env_sync_interval:
            self.hub_sync_interval_s = float(env_sync_interval)
        env_edge_heartbeat_interval = os.environ.get("TEMMS_EDGE_HEARTBEAT_INTERVAL_S")
        if env_edge_heartbeat_interval:
            self.edge_heartbeat_interval_s = float(env_edge_heartbeat_interval)
        self.hub_auto_apply = self.hub_auto_apply or _env_bool("TEMMS_HUB_AUTO_APPLY")
        self.rollout_require_signature = _env_bool(
            "TEMMS_ROLLOUT_REQUIRE_SIGNATURE",
            default=self.rollout_require_signature,
        )
        if self.rollout_signing_key is None:
            self.rollout_signing_key = os.environ.get("TEMMS_PACKAGE_SIGNING_KEY")
        if self.rollout_signing_key_file is None:
            signing_key_file = os.environ.get("TEMMS_PACKAGE_SIGNING_KEY_FILE")
            if signing_key_file:
                self.rollout_signing_key_file = Path(signing_key_file)


class TEMMSDaemon:
    """
    Main TEMMS daemon process.

    Orchestrates:
    - Inference server (FastAPI/Uvicorn)
    - Condition collection loop (concurrent collectors)
    - Policy evaluation loop (event-driven + periodic)
    - Model switching and preloading
    - Operator override enforcement
    """

    def __init__(
        self,
        config: DaemonConfig,
        slot_manager: SlotManager,
        condition_store: ConditionStore,
        policy_engine: PolicyEngine,
        model_cache: ModelCache,
        model_storage: ModelStorage,
        collectors: Optional[List[ConditionCollector]] = None,
    ):
        self.config = config
        self.slot_manager = slot_manager
        self.condition_store = condition_store
        self.policy_engine = policy_engine
        self.model_cache = model_cache
        self.model_storage = model_storage
        self.collectors = collectors or []

        # Create inference runtime
        self.inference_runtime = InferenceRuntime(
            model_cache=model_cache,
            model_storage=model_storage,
            max_workers=config.max_inference_workers,
        )

        default_data_dir = _default_data_dir()
        if (
            config.deployment_state_path == default_data_dir / "deployment_state.json"
            and not os.access(default_data_dir.parent, os.W_OK)
        ):
            config.deployment_state_path = model_cache.db_path.parent / "deployment_state.json"
        if (
            config.pending_operations_path == default_data_dir / "pending_operations.json"
            and not os.access(default_data_dir.parent, os.W_OK)
        ):
            config.pending_operations_path = model_cache.db_path.parent / "pending_operations.json"
        if config.hub_state_path == default_data_dir / "hub_lite.json" and not os.access(
            default_data_dir.parent, os.W_OK
        ):
            config.hub_state_path = model_cache.db_path.parent / "hub_lite.json"
        if config.telemetry_path == default_data_dir / "telemetry.jsonl" and not os.access(
            default_data_dir.parent, os.W_OK
        ):
            config.telemetry_path = model_cache.db_path.parent / "telemetry.jsonl"

        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._conditions_changed = asyncio.Event()  # Issue #3: event coordination
        self._server = None
        self._tasks: List[asyncio.Task] = []
        self._started_at = time.time()
        self._last_hub_sync_at: Optional[float] = None

        self.deployment_state = DeploymentStateStore(config.deployment_state_path)
        self.pending_operations = PendingOperationsStore(config.pending_operations_path)
        self.hub_lite = HubLiteStore(config.hub_state_path)
        self.telemetry = TelemetryBuffer(config.telemetry_path)

    def _emit_telemetry(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Best-effort daemon telemetry append."""
        try:
            self.telemetry.append(event_type, payload, source="daemon")
        except Exception as e:
            logger.warning("Failed to append telemetry event %s: %s", event_type, e)

    @classmethod
    def from_config(cls, config: DaemonConfig) -> "TEMMSDaemon":
        """
        Create daemon from configuration.

        Factory method that instantiates all components.
        """
        _resolve_non_root_path_defaults(config)

        # Ensure directories exist
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        config.model_dir.mkdir(parents=True, exist_ok=True)
        config.policy_dir.mkdir(parents=True, exist_ok=True)

        # Create components
        slot_manager = SlotManager(db_path=config.db_path)
        condition_store = ConditionStore(db_path=config.db_path)
        policy_engine = PolicyEngine(condition_store=condition_store)
        model_cache = ModelCache(db_path=config.db_path)
        model_storage = ModelStorage(model_dir=config.model_dir)

        # Create default collectors
        from temms.conditions.collectors import (
            SystemMetricsCollector,
            TimeBasedCollector,
        )

        collectors = [
            SystemMetricsCollector(),
            TimeBasedCollector(),
        ]

        return cls(
            config=config,
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            collectors=collectors,
        )

    async def start(self) -> None:
        """
        Start all daemon tasks.

        Blocks until shutdown signal received.
        """
        self._running = True
        self._shutdown_event.clear()

        logger.info("TEMMS daemon starting")
        logger.info(f"Config: host={self.config.inference_host}, port={self.config.inference_port}")

        # Setup signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal_handler)

        try:
            # Load policies from directory
            await self._load_policies()

            # Auto-start slots with default models
            if self.config.auto_start_slots:
                await self._auto_start_slots()

            # Start background tasks
            self._tasks = [
                asyncio.create_task(self._condition_loop(), name="condition_loop"),
                asyncio.create_task(self._policy_loop(), name="policy_loop"),
                asyncio.create_task(self._reconciliation_loop(), name="reconciliation_loop"),
                asyncio.create_task(
                    self._edge_heartbeat_loop(),
                    name="edge_heartbeat_loop",
                ),
            ]
            if self.config.hub_url:
                self._tasks.append(asyncio.create_task(self._hub_sync_loop(), name="hub_sync_loop"))

            # Start inference server (blocking)
            await self._run_inference_server()

        except asyncio.CancelledError:
            logger.info("Daemon cancelled")
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Graceful shutdown."""
        if not self._running:
            return

        logger.info("TEMMS daemon stopping")
        self._running = False
        self._shutdown_event.set()

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Shutdown inference runtime
        self.inference_runtime.shutdown()

        logger.info("TEMMS daemon stopped")

    def _signal_handler(self) -> None:
        """Handle shutdown signals."""
        logger.info("Shutdown signal received")
        self._shutdown_event.set()

    async def _load_policies(self) -> None:
        """Load policies from policy directory."""
        self.policy_engine.clear_policies()
        if not self.config.policy_dir.exists():
            logger.warning(f"Policy directory not found: {self.config.policy_dir}")
            return

        policy_files = list(self.config.policy_dir.glob("*.yaml")) + list(
            self.config.policy_dir.glob("*.yml")
        )

        for policy_file in policy_files:
            try:
                self.policy_engine.load_policy_from_file(policy_file)
                logger.info(f"Loaded policy: {policy_file.name}")
            except Exception as e:
                logger.error(f"Failed to load policy {policy_file}: {e}")

        logger.info(f"Loaded {len(self.policy_engine.list_policies())} policies")

    async def _auto_start_slots(self) -> None:
        """Load each slot's startup model into the (empty) runtime.

        Crash recovery: prefer the persisted ``active_model_id`` over the
        configured ``default_model``. ``active_model_id`` is committed only after
        a swap fully succeeds (load + warm + atomic DB commit), so it is always a
        fully-activated model, never a half-swap — it is the deterministic
        recovery anchor. On a fresh process the runtime holds nothing, so a slot
        that the store records as running is re-hydrated rather than skipped.
        """
        slots = self.slot_manager.list_slots()

        for slot in slots:
            # Skip only if this slot is already serving in the current process.
            if self.inference_runtime.get_slot_info(slot.name).get("has_model"):
                continue

            # Choose the startup model: last fully-activated first, else default.
            model = None
            trigger_detail = "default_model"
            if slot.active_model_id:
                model = self.model_cache.get_model(slot.active_model_id)
                if model is not None:
                    trigger_detail = "restore_active_model"
            if model is None and slot.default_model is not None:
                model = self.model_cache.find_model(slot.default_model)
            if model is None:
                if slot.active_model_id or slot.default_model:
                    logger.warning(
                        f"Startup model for slot {slot.name} not found "
                        f"(active={slot.active_model_id}, default={slot.default_model})"
                    )
                continue

            try:
                conditions = self.condition_store.get_snapshot()
                audit_metadata = await self._load_and_activate(
                    slot_name=slot.name,
                    model_id=model.id,
                    trigger_type="startup",
                    trigger_detail=trigger_detail,
                    conditions=conditions,
                )

                logger.info(
                    f"Auto-started slot {slot.name} with model {model.id} ({trigger_detail})"
                )
                self._emit_telemetry(
                    "slot.startup",
                    {
                        "slot": slot.name,
                        "model_id": model.id,
                        "trigger": trigger_detail,
                        "model": audit_metadata,
                    },
                )

            except Exception as e:
                preflight_blocked = isinstance(e, ActivationPreflightBlocked)
                blocking_gates = e.blocking_gates if preflight_blocked else []
                logger.error(f"Failed to auto-start slot {slot.name}: {e}")
                self.slot_manager.update_slot_state(
                    slot.name,
                    SlotState.STOPPED if preflight_blocked else SlotState.ERROR,
                )
                self._emit_telemetry(
                    "slot.startup_failed",
                    {
                        "slot": slot.name,
                        "model": slot.default_model,
                        "detail": str(e),
                        "failure_kind": (
                            "readiness_preflight" if preflight_blocked else "load_error"
                        ),
                        "blocking_gates": blocking_gates,
                    },
                )

    async def _condition_loop(self) -> None:
        """
        Periodically collect conditions from all collectors concurrently.

        Preserves each collector's source and priority for decision evidence.
        Signals the policy loop via _conditions_changed event (#3).
        """
        logger.info(
            f"Condition collection loop started (interval: {self.config.condition_interval_s}s)"
        )

        while self._running:
            try:
                collected_conditions = await self._collect_conditions_with_sources()

                for path, value, source, priority in collected_conditions:
                    self.condition_store.set(
                        path=path,
                        value=value,
                        source=source,
                        priority=priority,
                    )
                    condition_update_count.inc()

                # Signal the policy loop that conditions changed
                if collected_conditions:
                    self._conditions_changed.set()

            except Exception as e:
                logger.error(f"Condition loop error: {e}")

            # Wait for next interval or shutdown
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.config.condition_interval_s,
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Continue loop

        logger.info("Condition collection loop stopped")

    async def _collect_conditions_with_sources(self) -> List[tuple[str, Any, str, int]]:
        """Collect condition values while retaining each collector's source metadata."""
        loop = asyncio.get_running_loop()

        async def collect_one(collector: ConditionCollector) -> List[tuple[str, Any, str, int]]:
            source = str(getattr(collector, "source_name", None) or "collector")
            priority = int(getattr(collector, "source_priority", 100))
            health_prefix = f"runtime.collectors.{_condition_path_segment(source)}"
            health_source = f"{source}:health"
            try:
                if isinstance(collector, AsyncConditionCollector):
                    result = await collector.collect_async()
                else:
                    result = await loop.run_in_executor(None, collector.collect)
            except Exception as e:
                logger.error(f"Collector {source} failed: {e}")
                return [
                    (
                        f"{health_prefix}.healthy",
                        False,
                        health_source,
                        COLLECTOR_HEALTH_PRIORITY,
                    ),
                    (
                        f"{health_prefix}.last_error",
                        str(e),
                        health_source,
                        COLLECTOR_HEALTH_PRIORITY,
                    ),
                    (
                        f"{health_prefix}.reported_count",
                        0,
                        health_source,
                        COLLECTOR_HEALTH_PRIORITY,
                    ),
                ]
            if not isinstance(result, dict):
                result = {}
            collected = [(path, value, source, priority) for path, value in result.items()]
            collected.extend(
                [
                    (
                        f"{health_prefix}.healthy",
                        True,
                        health_source,
                        COLLECTOR_HEALTH_PRIORITY,
                    ),
                    (
                        f"{health_prefix}.last_error",
                        None,
                        health_source,
                        COLLECTOR_HEALTH_PRIORITY,
                    ),
                    (
                        f"{health_prefix}.reported_count",
                        len(result),
                        health_source,
                        COLLECTOR_HEALTH_PRIORITY,
                    ),
                ]
            )
            return collected

        results = await asyncio.gather(
            *(collect_one(collector) for collector in self.collectors),
            return_exceptions=True,
        )
        collected: List[tuple[str, Any, str, int]] = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Condition collection task failed: {result}")
            else:
                collected.extend(result)
        return collected

    async def _policy_loop(self) -> None:
        """
        Evaluate policies for all slots.

        Triggers on:
        1. Condition change event (sub-second reaction, #3)
        2. Periodic timer (fallback, ensures nothing is missed)
        """
        logger.info(f"Policy evaluation loop started (interval: {self.config.policy_interval_s}s)")

        while self._running:
            try:
                await self._evaluate_all_slots()

            except Exception as e:
                logger.error(f"Policy loop error: {e}")

            # Wait for conditions_changed event, periodic timer, or shutdown
            # Whichever fires first triggers the next evaluation
            try:
                shutdown_task = asyncio.create_task(self._shutdown_event.wait())
                conditions_task = asyncio.create_task(self._conditions_changed.wait())
                timer_task = asyncio.create_task(asyncio.sleep(self.config.policy_interval_s))

                done, pending = await asyncio.wait(
                    [shutdown_task, conditions_task, timer_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Cancel pending tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # Check if shutdown was the trigger
                if shutdown_task in done:
                    break

                # Clear the conditions_changed event for next cycle
                self._conditions_changed.clear()

            except asyncio.CancelledError:
                break

        logger.info("Policy evaluation loop stopped")

    async def _reconciliation_loop(self) -> None:
        """Simple desired-vs-actual reconciliation loop for deployment lifecycle."""
        while self._running:
            try:
                current = self.deployment_state.get_state()
                slots = self.slot_manager.list_slots()

                if self.config.offline_mode:
                    target = DeploymentState.OFFLINE
                elif not slots:
                    target = DeploymentState.PENDING
                elif any(slot.state == SlotState.ERROR for slot in slots):
                    target = DeploymentState.DEGRADED
                elif all(slot.state == SlotState.RUNNING for slot in slots):
                    target = DeploymentState.READY
                elif any(slot.state == SlotState.LOADING for slot in slots):
                    target = DeploymentState.DOWNLOADING
                else:
                    target = DeploymentState.PENDING

                if target != current:
                    self.deployment_state.set_state(target, "reconciliation")
                    set_deployment_state(target.value)

                runtime_health_gauge.set(
                    1
                    if target
                    in {DeploymentState.READY, DeploymentState.DOWNLOADING, DeploymentState.OFFLINE}
                    else 0
                )
                uptime_gauge.set(time.time() - self._started_at)

                # DDIL-specific gauges (issue #30).
                try:
                    pending = len(self.pending_operations.read_all())
                except Exception:
                    pending = 0
                last_sync = getattr(self, "_last_hub_sync_at", None)
                set_ddil_gauges(
                    offline=bool(self.config.offline_mode),
                    pending_intents=pending,
                    decision_chain_length=self.slot_manager.decision_count(),
                    seconds_since_sync=(time.time() - last_sync) if last_sync else None,
                )
            except Exception as e:
                logger.error(f"Reconciliation loop error: {e}")
                self.deployment_state.set_state(DeploymentState.FAILED, "reconciliation_error")

            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=1.0)
                break
            except asyncio.TimeoutError:
                pass

    async def _hub_sync_loop(self) -> None:
        """Enroll with Hub Lite and keep heartbeat/deployment state current."""
        logger.info(
            "Hub Lite sync loop started (url=%s, interval=%ss)",
            self.config.hub_url,
            self.config.hub_sync_interval_s,
        )

        while self._running:
            try:
                await self._hub_sync_once()
                if self.config.hub_url:
                    self._last_hub_sync_at = time.time()
            except Exception as e:
                logger.warning("Hub Lite sync failed: %s", e)
                self._emit_telemetry("hub.sync_failed", {"detail": str(e)})

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.config.hub_sync_interval_s,
                )
                break
            except asyncio.TimeoutError:
                pass

        logger.info("Hub Lite sync loop stopped")

    async def _edge_heartbeat_loop(self) -> None:
        """Keep local Hub Lite edge inventory fresh for readiness gates."""
        logger.info(
            "Local edge heartbeat loop started (device=%s, interval=%ss)",
            self.config.hub_device_id or socket.gethostname(),
            self.config.edge_heartbeat_interval_s,
        )

        while self._running:
            try:
                await asyncio.to_thread(self._edge_heartbeat_once)
            except Exception as e:
                logger.warning("Local edge heartbeat failed: %s", e)

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.config.edge_heartbeat_interval_s,
                )
                break
            except asyncio.TimeoutError:
                pass

        logger.info("Local edge heartbeat loop stopped")

    def _edge_heartbeat_once(self) -> Dict[str, Any]:
        """Refresh this daemon's local Hub Lite heartbeat and capability inventory."""
        device_id = self.config.hub_device_id or socket.gethostname()
        inventory = self._hub_inventory()
        profile = self.config.hub_device_profile or inventory.get("device_profile")
        self.hub_lite.enroll_device(device_id, profile=profile, inventory=inventory)
        return self.hub_lite.heartbeat(
            device_id,
            status="online",
            inventory=inventory,
            deployment_status=self._hub_deployment_status(),
        )

    async def _hub_sync_once(self) -> None:
        """Run one online sync pass against a central Hub Lite endpoint."""
        if not self.config.hub_url:
            return

        import httpx

        hub_base = _hub_base_url(self.config.hub_url)
        device_id = self.config.hub_device_id or socket.gethostname()
        inventory = self._hub_inventory()
        profile = self.config.hub_device_profile or inventory.get("device_profile")
        headers = _hub_headers(self.config.hub_token)

        async with httpx.AsyncClient(
            base_url=hub_base,
            headers=headers,
            timeout=5.0,
        ) as client:
            enroll_response = await client.post(
                "/devices/enroll",
                json={
                    "device_id": device_id,
                    "profile": profile,
                    "inventory": inventory,
                },
            )
            enroll_response.raise_for_status()
            heartbeat_response = await client.post(
                f"/devices/{device_id}/heartbeat",
                json={
                    "status": "online",
                    "inventory": inventory,
                    "deployment_status": self._hub_deployment_status(),
                },
            )
            heartbeat_response.raise_for_status()
            packages_response = await client.get("/packages")
            packages_response.raise_for_status()
            rollouts_response = await client.get("/rollouts")
            rollouts_response.raise_for_status()

            packages = packages_response.json().get("packages", [])
            rollouts = rollouts_response.json().get("rollouts", [])
            mirrored = self._mirror_hub_snapshot(
                packages=packages,
                rollouts=rollouts,
                device_id=device_id,
                profile=profile,
                inventory=inventory,
            )
            downloaded = await self._fetch_assigned_package_artifacts(
                client,
                rollouts=rollouts,
                device_id=device_id,
            )
            mirrored["package_artifacts"] = downloaded
            if self.config.hub_auto_apply:
                await self._auto_apply_assigned_rollouts()
            await self._push_local_rollout_states(
                client,
                central_rollouts=rollouts,
                device_id=device_id,
            )

        self._emit_telemetry("hub.synced", {"mirrored": mirrored, "hub_url": hub_base})

    async def _fetch_assigned_package_artifacts(
        self,
        client: Any,
        *,
        rollouts: List[Dict[str, Any]],
        device_id: str,
    ) -> int:
        """Download package archives for assigned local rollouts."""
        from temms.core.package_catalog import package_source_sha256

        package_ids = {
            rollout.get("package_id")
            for rollout in rollouts
            if rollout.get("device_id") == device_id
            and rollout.get("state", "assigned") == "assigned"
            and rollout.get("package_id")
        }
        if not package_ids:
            return 0

        artifact_dir = self.config.hub_state_path.parent / "packages"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_root = artifact_dir.resolve()
        downloaded = 0

        for package_id in sorted(package_ids):
            package = self.hub_lite.get_package(package_id)
            if package is None:
                continue
            package_path = package.get("path")
            if package_path:
                resolved_path = Path(package_path).expanduser().resolve()
                if resolved_path.exists() and resolved_path.is_relative_to(artifact_root):
                    expected_cached_sha = package.get("sha256") or (
                        (package.get("metadata") or {}).get("online_artifact", {}).get("sha256")
                    )
                    actual_cached_sha = package_source_sha256(resolved_path)
                    if not expected_cached_sha or actual_cached_sha == expected_cached_sha:
                        continue
                    self._emit_telemetry(
                        "hub.package_cache_mismatch",
                        {
                            "package_id": package_id,
                            "path": str(resolved_path),
                            "expected_sha256": expected_cached_sha,
                            "actual_sha256": actual_cached_sha,
                        },
                    )

            response = await client.get(f"/packages/{package_id}/artifact")
            response.raise_for_status()
            content = response.content
            expected_sha = response.headers.get("x-temms-package-sha256")
            source_sha = response.headers.get("x-temms-package-source-sha256")
            actual_sha = hashlib.sha256(content).hexdigest()
            if expected_sha and actual_sha != expected_sha:
                raise ValueError(f"Package artifact hash mismatch: {package_id}")

            filename = Path(
                response.headers.get("x-temms-package-filename") or f"{package_id}.temms.tar.zst"
            ).name
            destination = artifact_dir / filename
            tmp_destination = destination.with_suffix(destination.suffix + ".tmp")
            tmp_destination.write_bytes(content)
            tmp_destination.replace(destination)

            package["path"] = str(destination)
            package["sha256"] = actual_sha
            if source_sha:
                package["source_sha256"] = source_sha
            package.setdefault("metadata", {})["online_artifact"] = {
                "filename": filename,
                "sha256": actual_sha,
                "source_sha256": source_sha,
                "downloaded_at": datetime.utcnow().isoformat() + "Z",
            }
            self.hub_lite.upsert_package(package)
            downloaded += 1

        return downloaded

    def _hub_inventory(self) -> Dict[str, Any]:
        """Return runtime inventory for hub enrollment and heartbeat."""
        from temms.core.runtime_profiles import (
            detect_runtime_capabilities,
            normalize_device_profile,
        )

        inventory = detect_runtime_capabilities().to_dict()
        if self.config.hub_device_profile:
            inventory["device_profile"] = normalize_device_profile(self.config.hub_device_profile)
        if _env_bool("TEMMS_DEMO_SEED_HUB"):
            inventory = _apply_demo_edge_inventory_floor(inventory)
        inventory["temms"] = {
            "offline_mode": self.config.offline_mode,
            "api_host": self.config.inference_host,
            "api_port": self.config.inference_port,
        }
        return inventory

    def _hub_deployment_status(self) -> Dict[str, Any]:
        """Return the local deployment snapshot sent on heartbeat."""
        state = self.deployment_state.get_state()
        return {
            "state": state.value,
            "slots": {
                slot.name: {
                    "state": slot.state.value,
                    "active_model_id": slot.active_model_id,
                    "default_model": slot.default_model,
                }
                for slot in self.slot_manager.list_slots()
            },
            "rollouts": {
                rollout.get("rollout_id"): {
                    "state": rollout.get("state"),
                    "slot": rollout.get("slot"),
                    "package_id": rollout.get("package_id"),
                    "updated_at": rollout.get("updated_at"),
                }
                for rollout in self.hub_lite.list_rollouts()
                if rollout.get("rollout_id")
            },
        }

    def _mirror_hub_snapshot(
        self,
        *,
        packages: List[Dict[str, Any]],
        rollouts: List[Dict[str, Any]],
        device_id: str,
        profile: Optional[str],
        inventory: Dict[str, Any],
    ) -> Dict[str, int]:
        """Mirror central Hub Lite package and rollout assignments locally."""
        self.hub_lite.enroll_device(device_id, profile=profile, inventory=inventory)
        packages_by_id = {}
        for package in packages:
            package_id = package.get("package_id")
            if not package_id:
                continue
            mirrored_package = self._merge_cached_package_artifact(package)
            packages_by_id[package_id] = mirrored_package
            self.hub_lite.upsert_package(mirrored_package)

        mirrored_rollouts = 0
        for rollout in rollouts:
            if rollout.get("device_id") != device_id:
                continue
            rollout_id = rollout.get("rollout_id")
            package_id = rollout.get("package_id")
            if not rollout_id or not package_id:
                continue
            if self.hub_lite.get_rollout(rollout_id) is not None:
                continue
            if package_id not in packages_by_id:
                continue
            self.hub_lite.assign_rollout(
                device_id=device_id,
                package_id=package_id,
                model_id=rollout.get("model_id"),
                slot=rollout.get("slot"),
                rollout_id=rollout_id,
                runtime_target_id=rollout.get("runtime_target_id"),
                actor=rollout.get("actor"),
            )
            mirrored_rollouts += 1

        return {"packages": len(packages_by_id), "rollouts": mirrored_rollouts}

    def _merge_cached_package_artifact(self, central_package: Dict[str, Any]) -> Dict[str, Any]:
        """Preserve a valid local artifact path when central source did not change."""
        package_id = central_package.get("package_id")
        if not package_id:
            return dict(central_package)

        local = self.hub_lite.get_package(package_id) or {}
        local_metadata = local.get("metadata") or {}
        local_artifact = local_metadata.get("online_artifact") or {}
        local_path = local.get("path")
        local_source_sha = local.get("source_sha256") or local_artifact.get("source_sha256")
        central_source_sha = central_package.get("source_sha256") or central_package.get("sha256")

        if not local_path or not local_artifact:
            return dict(central_package)
        if local_source_sha and central_source_sha and local_source_sha != central_source_sha:
            return dict(central_package)

        resolved_path = Path(local_path).expanduser().resolve()
        artifact_root = (self.config.hub_state_path.parent / "packages").resolve()
        if not resolved_path.exists() or not resolved_path.is_relative_to(artifact_root):
            return dict(central_package)

        merged = dict(central_package)
        merged["path"] = str(resolved_path)
        merged["sha256"] = local.get("sha256") or local_artifact.get("sha256")
        if local_source_sha or central_source_sha:
            merged["source_sha256"] = local_source_sha or central_source_sha
        metadata = dict(central_package.get("metadata") or {})
        metadata["online_artifact"] = local_artifact
        merged["metadata"] = metadata
        return merged

    async def _push_local_rollout_states(
        self,
        client: Any,
        *,
        central_rollouts: List[Dict[str, Any]],
        device_id: str,
    ) -> None:
        """Replay local rollout state transitions back to central Hub Lite."""
        central_by_id = {
            rollout.get("rollout_id"): rollout
            for rollout in central_rollouts
            if rollout.get("device_id") == device_id and rollout.get("rollout_id")
        }
        for local in self.hub_lite.list_rollouts():
            rollout_id = local.get("rollout_id")
            if not rollout_id or rollout_id not in central_by_id:
                continue
            entries = self._rollout_history_entries_to_push(
                local,
                central_by_id[rollout_id],
                device_id=device_id,
            )
            for entry in entries:
                response = await client.post(
                    f"/rollouts/{rollout_id}/status",
                    json={
                        "state": entry["state"],
                        "detail": entry.get("detail") or "edge sync",
                        "actor": entry.get("actor") or f"edge:{device_id}",
                    },
                )
                response.raise_for_status()

    def _rollout_history_entries_to_push(
        self,
        local: Dict[str, Any],
        central: Dict[str, Any],
        *,
        device_id: str,
    ) -> List[Dict[str, Any]]:
        """Return local rollout history entries missing from central state."""
        local_history = local.get("history") or []
        central_history = central.get("history") or []
        if not local_history:
            local_state = local.get("state")
            central_state = central.get("state")
            if local_state and local_state != central_state:
                return [
                    {
                        "state": local_state,
                        "detail": "edge sync",
                        "actor": f"edge:{device_id}",
                    }
                ]
            return []

        prefix_len = 0
        for local_entry, central_entry in zip(local_history, central_history):
            if not _rollout_history_entries_match(local_entry, central_entry):
                break
            prefix_len += 1

        entries = [dict(entry) for entry in local_history[prefix_len:]]
        if not entries and local.get("state") != central.get("state") and local.get("state"):
            entries.append(
                {
                    "state": local["state"],
                    "detail": "edge sync",
                    "actor": f"edge:{device_id}",
                }
            )
        return entries

    async def _auto_apply_assigned_rollouts(self) -> None:
        """Apply locally assigned rollouts when explicitly enabled."""
        import httpx

        headers = _hub_headers(self.config.api_token)
        signing_key = self.config.rollout_signing_key or _read_optional_file(
            self.config.rollout_signing_key_file
        )
        if self.config.rollout_require_signature and not signing_key:
            for rollout in self.hub_lite.list_rollouts():
                if rollout.get("state") == "assigned":
                    rollout_id = rollout.get("rollout_id")
                    if not rollout_id:
                        continue
                    self.hub_lite.update_rollout_status(
                        rollout_id,
                        "failed",
                        detail="auto-apply requires TEMMS_PACKAGE_SIGNING_KEY_FILE or TEMMS_PACKAGE_SIGNING_KEY",
                    )
                    self._emit_telemetry(
                        "rollout.auto_apply_failed",
                        {
                            "rollout_id": rollout_id,
                            "detail": "missing signing key",
                        },
                    )
            return

        base_url = f"http://127.0.0.1:{self.config.inference_port}/v1/hub"
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=30.0,
        ) as client:
            for rollout in self.hub_lite.list_rollouts():
                if rollout.get("state") != "assigned":
                    continue
                rollout_id = rollout.get("rollout_id")
                if not rollout_id:
                    continue
                response = await client.post(
                    f"/rollouts/{rollout_id}/apply",
                    json={
                        "require_signature": self.config.rollout_require_signature,
                        "signing_key": signing_key,
                        "actor": f"edge:{self.config.hub_device_id or 'local'}",
                    },
                )
                if response.status_code >= 400:
                    self._emit_telemetry(
                        "rollout.auto_apply_failed",
                        {
                            "rollout_id": rollout_id,
                            **_hub_error_payload(response),
                        },
                    )
                    continue
                self._emit_telemetry(
                    "rollout.auto_applied",
                    {
                        "rollout_id": rollout_id,
                        "response": response.json(),
                    },
                )

    async def _evaluate_all_slots(self) -> None:
        """Evaluate policies for all running slots, respecting operator overrides."""
        slots = self.slot_manager.list_slots()
        conditions = self.condition_store.get_snapshot()

        for slot in slots:
            if slot.state != SlotState.RUNNING:
                continue

            try:
                # Check for active operator override (#1)
                if self.slot_manager.has_active_override(slot.name):
                    logger.debug(f"POLICY_SKIP slot={slot.name} reason=operator_override_active")
                    continue

                # Evaluate policies for this slot
                policy_decision_count.inc()
                result = self.policy_engine.evaluate_slot(slot.name)

                if result.switch_to is None:
                    continue  # No change needed

                # Find model in cache (with optional version pin, #8)
                new_model = self.model_cache.find_model(result.switch_to, version=result.version)
                if new_model is None:
                    logger.warning(f"Policy selected model not found: {result.switch_to}")
                    continue

                # Check if already active
                if new_model.id == slot.active_model_id:
                    # Already running correct model, but handle preloads
                    await self._handle_preloads(slot.name, result.preload)
                    continue

                # Execute switch
                trigger_detail = result.triggered_by or "policy_evaluation"
                await self._execute_switch(
                    slot_name=slot.name,
                    new_model_id=new_model.id,
                    trigger_type="policy",
                    trigger_detail=trigger_detail,
                    conditions=conditions,
                    decision_metadata={
                        "policy_evaluation": result.explanation,
                    },
                )

                # Handle preloads from the policy result (#16)
                await self._handle_preloads(slot.name, result.preload)

            except Exception as e:
                logger.error(f"Policy evaluation failed for slot {slot.name}: {e}")

    async def _handle_preloads(self, slot_name: str, preload_list: List[str]) -> None:
        """Preload models specified in policy result."""
        for model_name in preload_list:
            model = self.model_cache.find_model(model_name)
            if model is None:
                logger.debug(f"Preload model not found: {model_name}")
                continue

            try:
                await self.inference_runtime.preload_model(slot_name, model.id)
            except Exception as e:
                logger.debug(f"Failed to preload {model_name}: {e}")

    async def _execute_switch(
        self,
        slot_name: str,
        new_model_id: str,
        trigger_type: str,
        trigger_detail: str,
        conditions: Dict[str, Any],
        decision_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Execute model switch with logging.

        Args:
            slot_name: Target slot
            new_model_id: Model to switch to
            trigger_type: policy, operator, fallback, startup
            trigger_detail: Details about trigger
            conditions: Current conditions snapshot
            decision_metadata: Structured trigger details for audit evidence
        """
        slot = self.slot_manager.get_slot(slot_name)
        if slot is None:
            logger.error(f"Slot not found: {slot_name}")
            return

        old_model_id = slot.active_model_id

        try:
            audit_metadata = await self._load_and_activate(
                slot_name=slot_name,
                model_id=new_model_id,
                trigger_type=trigger_type,
                trigger_detail=trigger_detail,
                conditions=conditions,
                extra_audit=decision_metadata,
            )

            logger.info(
                f"Model switch: slot={slot_name}, "
                f"{old_model_id} -> {new_model_id}, "
                f"trigger={trigger_type}/{trigger_detail}"
            )
            self._emit_telemetry(
                "slot.model_switched",
                {
                    "slot": slot_name,
                    "from_model": old_model_id,
                    "to_model": new_model_id,
                    "trigger_type": trigger_type,
                    "trigger_detail": trigger_detail,
                    "conditions": conditions,
                    "model": audit_metadata,
                    **(decision_metadata or {}),
                },
            )

        except Exception as e:
            preflight_blocked = isinstance(e, ActivationPreflightBlocked)
            logger.error(f"Model switch failed for {slot_name}: {e}")
            self._emit_telemetry(
                "slot.model_switch_failed",
                {
                    "slot": slot_name,
                    "from_model": old_model_id,
                    "to_model": new_model_id,
                    "trigger_type": trigger_type,
                    "trigger_detail": trigger_detail,
                    "conditions": conditions,
                    "detail": str(e),
                    **(decision_metadata or {}),
                },
            )

            # Try fallback chain
            fallback_chain = self.policy_engine.get_fallback_chain(slot_name)
            if fallback_chain:
                await self._execute_fallback(
                    slot_name,
                    fallback_chain,
                    conditions,
                    selected_model=new_model_id,
                    trigger_detail=trigger_detail,
                    load_error=str(e),
                    preserve_slot_state_on_failure=preflight_blocked,
                )
            elif preflight_blocked:
                self.slot_manager.update_slot_state(slot_name, SlotState.RUNNING)
            else:
                self.slot_manager.update_slot_state(slot_name, SlotState.ERROR)

    async def _execute_fallback(
        self,
        slot_name: str,
        fallback_chain: List[str],
        conditions: Dict[str, Any],
        selected_model: Optional[str] = None,
        trigger_detail: str = "primary_failed",
        load_error: Optional[str] = None,
        preserve_slot_state_on_failure: bool = False,
    ) -> None:
        """Execute fallback chain for slot."""
        logger.info(f"Executing fallback chain for {slot_name}: {fallback_chain}")

        attempted: List[str] = []
        failures: List[str] = []
        if selected_model and load_error:
            failures.append(f"{selected_model}: {load_error}")

        for model_name in fallback_chain:
            model = self.model_cache.find_model(model_name)
            if model is None:
                attempted.append(model_name)
                failures.append(f"{model_name}: not found")
                logger.warning(f"Fallback model not found: {model_name}")
                continue

            attempted.append(model.id)
            try:
                activation_preflight = self._activation_preflight(
                    slot_name=slot_name,
                    model_id=model.id,
                    trigger_type="fallback",
                    trigger_detail=trigger_detail,
                    conditions=conditions,
                )
                await self.inference_runtime.load_model(slot_name, model.id)
            except Exception as e:
                failures.append(f"{model.id}: {e}")
                logger.warning(f"Fallback model {model_name} failed: {e}")
                continue

            fallback_metadata = {
                "selected_model": selected_model,
                "attempted": attempted,
                "failures": failures,
            }
            audit_metadata = self._build_activation_audit(
                model.id, activation_preflight, extra={"fallback": fallback_metadata}
            )
            fallback_trigger = (
                f"fallback after {trigger_detail}" if trigger_detail else "primary_failed"
            )
            self.slot_manager.activate_model(
                slot_name=slot_name,
                model_id=model.id,
                trigger_type="fallback",
                trigger_detail=fallback_trigger,
                conditions=conditions,
                audit_metadata=audit_metadata,
            )
            logger.info(f"Fallback successful for {slot_name}: {model.id}")
            self._emit_telemetry(
                "slot.fallback",
                {
                    "slot": slot_name,
                    "model_id": model.id,
                    "reason": fallback_trigger,
                    "conditions": conditions,
                    "model": audit_metadata,
                    "fallback": fallback_metadata,
                },
            )
            return

        # All fallbacks failed
        self.slot_manager.update_slot_state(
            slot_name,
            SlotState.RUNNING if preserve_slot_state_on_failure else SlotState.ERROR,
        )
        logger.critical(f"All fallbacks failed for slot {slot_name}")
        self._emit_telemetry(
            "slot.fallback_failed",
            {
                "slot": slot_name,
                "fallback_chain": fallback_chain,
                "selected_model": selected_model,
                "attempted": attempted,
                "failures": failures,
                "conditions": conditions,
            },
        )

    def _build_activation_audit(
        self,
        model_id: str,
        activation_preflight: Optional[Dict[str, Any]],
        *,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Assemble the audit_metadata recorded alongside a model activation."""
        audit_metadata = self._model_audit_metadata(model_id)
        if activation_preflight:
            audit_metadata["activation_preflight"] = activation_preflight
        if extra:
            audit_metadata.update(extra)
        return audit_metadata

    async def _load_and_activate(
        self,
        *,
        slot_name: str,
        model_id: str,
        trigger_type: str,
        trigger_detail: str,
        conditions: Dict[str, Any],
        extra_audit: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Preflight, hot-load, and activate a model on a slot.

        Shared by startup auto-start and policy-driven switches: both mark the
        slot LOADING, load the model, then record the activation with its audit
        metadata. Raises ActivationPreflightBlocked or the underlying load error;
        callers own the failure and telemetry paths. Returns the recorded
        audit_metadata so callers can attach it to success telemetry.
        """
        activation_preflight = self._activation_preflight(
            slot_name=slot_name,
            model_id=model_id,
            trigger_type=trigger_type,
            trigger_detail=trigger_detail,
            conditions=conditions,
        )
        self.slot_manager.update_slot_state(slot_name, SlotState.LOADING)
        # Time the swap the operator actually feels: load + warm through activate.
        swap_started = time.monotonic()
        await self.inference_runtime.load_model(slot_name, model_id)
        audit_metadata = self._build_activation_audit(
            model_id, activation_preflight, extra=extra_audit
        )
        self.slot_manager.activate_model(
            slot_name=slot_name,
            model_id=model_id,
            trigger_type=trigger_type,
            trigger_detail=trigger_detail,
            conditions=conditions,
            audit_metadata=audit_metadata,
        )
        swap_latency_ms.observe((time.monotonic() - swap_started) * 1000.0)
        return audit_metadata

    def _activation_preflight(
        self,
        *,
        slot_name: str,
        model_id: str,
        trigger_type: str,
        trigger_detail: str,
        conditions: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Block local activation when Hub Lite proves this edge cannot host the model."""
        model = self.model_cache.get_model(model_id)
        if model is None or not model.package_id:
            return None
        package = self.hub_lite.get_package(model.package_id)
        if package is None:
            return None

        device_id = self.config.hub_device_id or socket.gethostname()
        if self.hub_lite.get_device(device_id) is None:
            return None

        readiness = self.hub_lite.deployment_readiness(
            package_id=model.package_id,
            model_id=model.id,
            device_id=device_id,
            slot=slot_name,
        )
        blocking_gates = _activation_blocking_gates(readiness)
        summary = {
            "schema_version": "temms-activation-preflight/v1",
            "status": readiness.get("status"),
            "selection": readiness.get("selection"),
            "checked_at": readiness.get("checked_at"),
        }
        if not blocking_gates:
            return summary

        message = "activation preflight blocked: " + _gate_summary(blocking_gates)
        self._emit_telemetry(
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
            message,
            readiness=readiness,
            blocking_gates=blocking_gates,
        )

    def _model_audit_metadata(self, model_id: str | None) -> Dict[str, Any]:
        """Return compact model/package context for decision logs and telemetry."""
        if not model_id:
            return {}
        model = self.model_cache.get_model(model_id)
        if model is None:
            return {"model_id": model_id}
        return {
            "model_id": model.id,
            "model_name": model.name,
            "model_version": model.version,
            "model_format": model.format.value,
            "model_sha256": model.sha256,
            "package_id": model.package_id,
            "provenance": model.metadata.get("provenance", {}),
            "runtime_constraints": model.metadata.get("runtime_constraints", {}),
            "benchmark": model.metadata.get("benchmark", {}),
        }

    async def _run_inference_server(self) -> None:
        """Run FastAPI inference server."""
        import uvicorn

        # Create FastAPI app with dependencies
        app = create_app(
            slot_manager=self.slot_manager,
            condition_store=self.condition_store,
            policy_engine=self.policy_engine,
            model_cache=self.model_cache,
            model_storage=self.model_storage,
            inference_runtime=self.inference_runtime,
            offline_mode=self.config.offline_mode,
            pending_operations=self.pending_operations,
            deployment_state=self.deployment_state,
            daemon_config=self.config,
            api_token=self.config.api_token,
            hub_lite=self.hub_lite,
            telemetry=self.telemetry,
        )

        # Configure uvicorn
        config = uvicorn.Config(
            app=app,
            host=self.config.inference_host,
            port=self.config.inference_port,
            log_level="info",
            access_log=True,
        )

        server = uvicorn.Server(config)

        # Run server with shutdown coordination
        self._server = server

        async def serve_with_shutdown():
            """Serve until shutdown event."""
            serve_task = asyncio.create_task(server.serve())
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())

            done, pending = await asyncio.wait(
                [serve_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel pending
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # Shutdown server
            server.should_exit = True

        await serve_with_shutdown()


async def start_daemon(config: Optional[DaemonConfig] = None) -> None:
    """
    Start TEMMS daemon.

    Convenience function for CLI.
    """
    if config is None:
        config = DaemonConfig()

    daemon = TEMMSDaemon.from_config(config)
    await daemon.start()
