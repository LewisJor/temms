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
from dataclasses import dataclass, field
from datetime import datetime

from temms.slots.manager import SlotManager, SlotState
from temms.conditions.store import ConditionStore
from temms.conditions.collectors import ConditionCollector, collect_all_async
from temms.policy.engine import PolicyEngine, PolicyEvalResult
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
    set_deployment_state,
    uptime_gauge,
)

logger = logging.getLogger(__name__)

SYSTEM_DATA_DIR = Path("/var/lib/temms")
SYSTEM_POLICY_DIR = Path("/etc/temms/policies")


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
        """Start slots that have default models."""
        slots = self.slot_manager.list_slots()

        for slot in slots:
            if slot.state != SlotState.STOPPED:
                continue

            if slot.default_model is None:
                continue

            # Find model in cache
            model = self.model_cache.find_model(slot.default_model)
            if model is None:
                logger.warning(
                    f"Default model for slot {slot.name} not found: {slot.default_model}"
                )
                continue

            try:
                # Load model
                self.slot_manager.update_slot_state(slot.name, SlotState.LOADING)
                await self.inference_runtime.load_model(slot.name, model.id)

                # Activate
                self.slot_manager.activate_model(
                    slot_name=slot.name,
                    model_id=model.id,
                    trigger_type="startup",
                    trigger_detail="default_model",
                    conditions=self.condition_store.get_snapshot(),
                    audit_metadata=self._model_audit_metadata(model.id),
                )

                logger.info(f"Auto-started slot {slot.name} with model {model.id}")
                self._emit_telemetry(
                    "slot.startup",
                    {
                        "slot": slot.name,
                        "model_id": model.id,
                        "trigger": "default_model",
                        "model": self._model_audit_metadata(model.id),
                    },
                )

            except Exception as e:
                logger.error(f"Failed to auto-start slot {slot.name}: {e}")
                self.slot_manager.update_slot_state(slot.name, SlotState.ERROR)
                self._emit_telemetry(
                    "slot.startup_failed",
                    {
                        "slot": slot.name,
                        "model": slot.default_model,
                        "detail": str(e),
                    },
                )

    async def _condition_loop(self) -> None:
        """
        Periodically collect conditions from all collectors concurrently.

        Uses collect_all_async for concurrent collection (#15).
        Signals the policy loop via _conditions_changed event (#3).
        """
        logger.info(
            f"Condition collection loop started (interval: {self.config.condition_interval_s}s)"
        )

        while self._running:
            try:
                # Collect from all collectors concurrently
                all_conditions = await collect_all_async(self.collectors)

                # Store collected conditions
                for path, value in all_conditions.items():
                    # Determine source and priority from the collector that produced this
                    # Since collect_all_async merges results, use a default
                    # The individual collector's priority is embedded in the result
                    self.condition_store.set(
                        path=path,
                        value=value,
                        source="collector",
                        priority=100,  # Sensor priority
                    )
                    condition_update_count.inc()

                # Signal the policy loop that conditions changed
                if all_conditions:
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
                slot=rollout.get("slot"),
                rollout_id=rollout_id,
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
                            "status_code": response.status_code,
                            "detail": response.text,
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
            # Set loading state
            self.slot_manager.update_slot_state(slot_name, SlotState.LOADING)

            # Load new model (hot-swap)
            await self.inference_runtime.load_model(slot_name, new_model_id)

            # Activate model
            audit_metadata = self._model_audit_metadata(new_model_id)
            if decision_metadata:
                audit_metadata.update(decision_metadata)
            self.slot_manager.activate_model(
                slot_name=slot_name,
                model_id=new_model_id,
                trigger_type=trigger_type,
                trigger_detail=trigger_detail,
                conditions=conditions,
                audit_metadata=audit_metadata,
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
                await self._execute_fallback(slot_name, fallback_chain, conditions)
            else:
                self.slot_manager.update_slot_state(slot_name, SlotState.ERROR)

    async def _execute_fallback(
        self,
        slot_name: str,
        fallback_chain: List[str],
        conditions: Dict[str, Any],
    ) -> None:
        """Execute fallback chain for slot."""
        logger.info(f"Executing fallback chain for {slot_name}: {fallback_chain}")

        loaded_model_id = await self.inference_runtime.try_fallback_chain(slot_name, fallback_chain)

        if loaded_model_id:
            # Activate fallback model
            self.slot_manager.activate_model(
                slot_name=slot_name,
                model_id=loaded_model_id,
                trigger_type="fallback",
                trigger_detail="primary_failed",
                conditions=conditions,
                audit_metadata=self._model_audit_metadata(loaded_model_id),
            )
            logger.info(f"Fallback successful for {slot_name}: {loaded_model_id}")
            self._emit_telemetry(
                "slot.fallback",
                {
                    "slot": slot_name,
                    "model_id": loaded_model_id,
                    "reason": "primary_failed",
                    "conditions": conditions,
                    "model": self._model_audit_metadata(loaded_model_id),
                },
            )
        else:
            # All fallbacks failed
            self.slot_manager.update_slot_state(slot_name, SlotState.ERROR)
            logger.critical(f"All fallbacks failed for slot {slot_name}")
            self._emit_telemetry(
                "slot.fallback_failed",
                {
                    "slot": slot_name,
                    "fallback_chain": fallback_chain,
                    "conditions": conditions,
                },
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
