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
import logging
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temms.conditions.collectors import ConditionCollector, collect_all_async
from temms.conditions.store import ConditionStore
from temms.core.cache import ModelCache
from temms.core.storage import ModelStorage
from temms.daemon.deployment_state import DeploymentState, DeploymentStateStore
from temms.daemon.pending_ops import PendingOperationsStore
from temms.inference.runtime import InferenceRuntime
from temms.inference.server import create_app
from temms.observability import (
    condition_update_count,
    policy_decision_count,
    runtime_health_gauge,
    set_deployment_state,
    uptime_gauge,
)
from temms.policy.engine import PolicyEngine
from temms.slots.manager import SlotManager, SlotState

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = Path("/var/lib/temms")
_DEFAULT_DEPLOYMENT_STATE_PATH = _DEFAULT_DATA_DIR / "deployment_state.json"
_DEFAULT_PENDING_OPERATIONS_PATH = _DEFAULT_DATA_DIR / "pending_operations.json"


@dataclass
class DaemonConfig:
    """Daemon configuration."""
    # Intervals
    condition_interval_s: float = 5.0      # Collect conditions every 5s
    policy_interval_s: float = 1.0         # Evaluate policies every 1s

    # Inference server
    inference_host: str = "0.0.0.0"
    inference_port: int = 8080

    # Data paths
    db_path: Path | None = None
    model_dir: Path | None = None
    policy_dir: Path | None = None

    # Behavior
    auto_start_slots: bool = True          # Start slots with default models
    max_inference_workers: int = 4
    deployment_state_path: Path | None = None
    pending_operations_path: Path | None = None
    offline_mode: bool = False

    def __post_init__(self):
        """Set default paths if not provided."""
        explicit_db_path = self.db_path is not None
        base_dir = _DEFAULT_DATA_DIR

        if self.db_path is None:
            self.db_path = base_dir / "temms.db"
        if self.model_dir is None:
            self.model_dir = base_dir / "models"
        if self.policy_dir is None:
            self.policy_dir = Path("/etc/temms/policies")
        state_dir = self.db_path.parent if explicit_db_path else base_dir
        if self.deployment_state_path is None:
            self.deployment_state_path = state_dir / "deployment_state.json"
        if self.pending_operations_path is None:
            self.pending_operations_path = state_dir / "pending_operations.json"


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
        collectors: list[ConditionCollector] | None = None,
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

        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._conditions_changed = asyncio.Event()  # Issue #3: event coordination
        self._server = None
        self._tasks: list[asyncio.Task] = []
        self._started_at = time.time()

        self.deployment_state = self._init_deployment_state_store()
        self.pending_operations = self._init_pending_operations_store()

    def _local_state_dir(self) -> Path:
        """Fallback state directory for embedded/local daemon construction."""
        return Path(self.model_storage.model_dir).parent

    def _uses_embedded_state_root(self) -> bool:
        """Whether injected components point at a non-service data root."""
        return self._local_state_dir() != _DEFAULT_DATA_DIR

    def _init_deployment_state_store(self) -> DeploymentStateStore:
        """Initialize deployment state, falling back for non-root local runs."""
        if (
            self.config.deployment_state_path == _DEFAULT_DEPLOYMENT_STATE_PATH
            and self._uses_embedded_state_root()
        ):
            fallback = self._local_state_dir() / "deployment_state.json"
            self.config.deployment_state_path = fallback
            return DeploymentStateStore(fallback)

        try:
            return DeploymentStateStore(self.config.deployment_state_path)
        except PermissionError:
            if self.config.deployment_state_path != _DEFAULT_DEPLOYMENT_STATE_PATH:
                raise
            fallback = self._local_state_dir() / "deployment_state.json"
            logger.warning(
                "Deployment state path %s is not writable; using %s",
                self.config.deployment_state_path,
                fallback,
            )
            self.config.deployment_state_path = fallback
            return DeploymentStateStore(fallback)

    def _init_pending_operations_store(self) -> PendingOperationsStore:
        """Initialize pending operations, falling back for non-root local runs."""
        if (
            self.config.pending_operations_path == _DEFAULT_PENDING_OPERATIONS_PATH
            and self._uses_embedded_state_root()
        ):
            fallback = self._local_state_dir() / "pending_operations.json"
            self.config.pending_operations_path = fallback
            return PendingOperationsStore(fallback)

        try:
            return PendingOperationsStore(self.config.pending_operations_path)
        except PermissionError:
            if self.config.pending_operations_path != _DEFAULT_PENDING_OPERATIONS_PATH:
                raise
            fallback = self._local_state_dir() / "pending_operations.json"
            logger.warning(
                "Pending operations path %s is not writable; using %s",
                self.config.pending_operations_path,
                fallback,
            )
            self.config.pending_operations_path = fallback
            return PendingOperationsStore(fallback)

    @classmethod
    def from_config(cls, config: DaemonConfig) -> "TEMMSDaemon":
        """
        Create daemon from configuration.

        Factory method that instantiates all components.
        """
        # Ensure directories exist
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        config.model_dir.mkdir(parents=True, exist_ok=True)

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
        if not self.config.policy_dir.exists():
            logger.warning(f"Policy directory not found: {self.config.policy_dir}")
            return

        policy_files = list(self.config.policy_dir.glob("*.yaml")) + \
                       list(self.config.policy_dir.glob("*.yml"))

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
                )

                logger.info(f"Auto-started slot {slot.name} with model {model.id}")

            except Exception as e:
                logger.error(f"Failed to auto-start slot {slot.name}: {e}")
                self.slot_manager.update_slot_state(slot.name, SlotState.ERROR)

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
        logger.info(
            f"Policy evaluation loop started (interval: {self.config.policy_interval_s}s)"
        )

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
                timer_task = asyncio.create_task(
                    asyncio.sleep(self.config.policy_interval_s)
                )

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

                healthy_states = {
                    DeploymentState.READY,
                    DeploymentState.DOWNLOADING,
                    DeploymentState.OFFLINE,
                }
                runtime_health_gauge.set(1 if target in healthy_states else 0)
                uptime_gauge.set(time.time() - self._started_at)
            except Exception as e:
                logger.error(f"Reconciliation loop error: {e}")
                self.deployment_state.set_state(DeploymentState.FAILED, "reconciliation_error")

            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=1.0)
                break
            except asyncio.TimeoutError:
                pass

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
                    logger.debug(
                        f"POLICY_SKIP slot={slot.name} reason=operator_override_active"
                    )
                    continue

                # Evaluate policies for this slot
                policy_decision_count.inc()
                result = self.policy_engine.evaluate_slot(slot.name)

                if result.switch_to is None:
                    continue  # No change needed

                # Find model in cache (with optional version pin, #8)
                new_model = self.model_cache.find_model(
                    result.switch_to, version=result.version
                )
                if new_model is None:
                    logger.warning(
                        f"Policy selected model not found: {result.switch_to}"
                    )
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
                )

                # Handle preloads from the policy result (#16)
                await self._handle_preloads(slot.name, result.preload)

            except Exception as e:
                logger.error(f"Policy evaluation failed for slot {slot.name}: {e}")

    async def _handle_preloads(self, slot_name: str, preload_list: list[str]) -> None:
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
        conditions: dict[str, Any],
    ) -> None:
        """
        Execute model switch with logging.

        Args:
            slot_name: Target slot
            new_model_id: Model to switch to
            trigger_type: policy, operator, fallback, startup
            trigger_detail: Details about trigger
            conditions: Current conditions snapshot
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
            self.slot_manager.activate_model(
                slot_name=slot_name,
                model_id=new_model_id,
                trigger_type=trigger_type,
                trigger_detail=trigger_detail,
                conditions=conditions,
            )

            logger.info(
                f"Model switch: slot={slot_name}, "
                f"{old_model_id} -> {new_model_id}, "
                f"trigger={trigger_type}/{trigger_detail}"
            )

        except Exception as e:
            logger.error(f"Model switch failed for {slot_name}: {e}")

            # Try fallback chain
            fallback_chain = self.policy_engine.get_fallback_chain(slot_name)
            if fallback_chain:
                await self._execute_fallback(slot_name, fallback_chain, conditions)
            else:
                self.slot_manager.update_slot_state(slot_name, SlotState.ERROR)

    async def _execute_fallback(
        self,
        slot_name: str,
        fallback_chain: list[str],
        conditions: dict[str, Any],
    ) -> None:
        """Execute fallback chain for slot."""
        logger.info(f"Executing fallback chain for {slot_name}: {fallback_chain}")

        loaded_model_id = await self.inference_runtime.try_fallback_chain(
            slot_name, fallback_chain
        )

        if loaded_model_id:
            # Activate fallback model
            self.slot_manager.activate_model(
                slot_name=slot_name,
                model_id=loaded_model_id,
                trigger_type="fallback",
                trigger_detail="primary_failed",
                conditions=conditions,
            )
            logger.info(f"Fallback successful for {slot_name}: {loaded_model_id}")
        else:
            # All fallbacks failed
            self.slot_manager.update_slot_state(slot_name, SlotState.ERROR)
            logger.critical(f"All fallbacks failed for slot {slot_name}")

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


async def start_daemon(config: DaemonConfig | None = None) -> None:
    """
    Start TEMMS daemon.

    Convenience function for CLI.
    """
    if config is None:
        config = DaemonConfig()

    daemon = TEMMSDaemon.from_config(config)
    await daemon.start()
