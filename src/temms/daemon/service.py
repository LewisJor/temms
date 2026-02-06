"""
Main TEMMS daemon - runs policy evaluation loop.

Responsibilities:
- Periodic condition collection (configurable interval)
- Policy evaluation per slot
- Model switching execution
- Inference server hosting
- Telemetry buffering
"""

import asyncio
import signal
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime

from temms.slots.manager import SlotManager, SlotState
from temms.conditions.store import ConditionStore
from temms.conditions.collectors import ConditionCollector
from temms.policy.engine import PolicyEngine
from temms.core.cache import ModelCache
from temms.core.storage import ModelStorage
from temms.inference.runtime import InferenceRuntime
from temms.inference.server import create_app

logger = logging.getLogger(__name__)


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
    db_path: Optional[Path] = None
    model_dir: Optional[Path] = None
    policy_dir: Optional[Path] = None

    # Behavior
    auto_start_slots: bool = True          # Start slots with default models
    max_inference_workers: int = 4

    def __post_init__(self):
        """Set default paths if not provided."""
        base_dir = Path("/var/lib/temms")

        if self.db_path is None:
            self.db_path = base_dir / "temms.db"
        if self.model_dir is None:
            self.model_dir = base_dir / "models"
        if self.policy_dir is None:
            self.policy_dir = Path("/etc/temms/policies")


class TEMMSDaemon:
    """
    Main TEMMS daemon process.

    Orchestrates:
    - Inference server (FastAPI/Uvicorn)
    - Condition collection loop
    - Policy evaluation loop
    - Model switching
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
        """
        Initialize daemon.

        Args:
            config: Daemon configuration
            slot_manager: SlotManager instance
            condition_store: ConditionStore instance
            policy_engine: PolicyEngine instance
            model_cache: ModelCache instance
            model_storage: ModelStorage instance
            collectors: List of condition collectors
        """
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
        self._server = None
        self._tasks: List[asyncio.Task] = []

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
        loop = asyncio.get_event_loop()
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
        """Periodically collect conditions from all collectors."""
        logger.info(
            f"Condition collection loop started (interval: {self.config.condition_interval_s}s)"
        )

        while self._running:
            try:
                for collector in self.collectors:
                    try:
                        # Collectors are sync, run in executor
                        loop = asyncio.get_event_loop()
                        conditions = await loop.run_in_executor(
                            None, collector.collect
                        )

                        for path, value in conditions.items():
                            self.condition_store.set(
                                path=path,
                                value=value,
                                source=collector.source_name,
                                priority=collector.source_priority,
                            )

                    except Exception as e:
                        logger.error(f"Collector {collector.source_name} failed: {e}")

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
        """Periodically evaluate policies for all slots."""
        logger.info(
            f"Policy evaluation loop started (interval: {self.config.policy_interval_s}s)"
        )

        while self._running:
            try:
                await self._evaluate_all_slots()

            except Exception as e:
                logger.error(f"Policy loop error: {e}")

            # Wait for next interval or shutdown
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.config.policy_interval_s,
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Continue loop

        logger.info("Policy evaluation loop stopped")

    async def _evaluate_all_slots(self) -> None:
        """Evaluate policies for all running slots."""
        slots = self.slot_manager.list_slots()
        conditions = self.condition_store.get_snapshot()

        for slot in slots:
            if slot.state != SlotState.RUNNING:
                continue

            try:
                # Evaluate policies for this slot
                new_model_name = self.policy_engine.evaluate_slot(slot.name)

                if new_model_name is None:
                    continue  # No change needed

                # Find model in cache
                new_model = self.model_cache.find_model(new_model_name)
                if new_model is None:
                    logger.warning(
                        f"Policy selected model not found: {new_model_name}"
                    )
                    continue

                # Check if already active
                if new_model.id == slot.active_model_id:
                    continue  # Already running correct model

                # Execute switch
                await self._execute_switch(
                    slot_name=slot.name,
                    new_model_id=new_model.id,
                    trigger_type="policy",
                    trigger_detail=f"policy_evaluation",
                    conditions=conditions,
                )

            except Exception as e:
                logger.error(f"Policy evaluation failed for slot {slot.name}: {e}")

    async def _execute_switch(
        self,
        slot_name: str,
        new_model_id: str,
        trigger_type: str,
        trigger_detail: str,
        conditions: Dict[str, Any],
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
        fallback_chain: List[str],
        conditions: Dict[str, Any],
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
