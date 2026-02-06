"""
Unit tests for the TEMMS daemon service.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from pathlib import Path

from temms.daemon.service import TEMMSDaemon, DaemonConfig


class TestDaemonConfig:
    """Tests for DaemonConfig."""

    def test_defaults(self):
        """Test default configuration values."""
        config = DaemonConfig()

        assert config.condition_interval_s == 5.0
        assert config.policy_interval_s == 1.0
        assert config.inference_host == "0.0.0.0"
        assert config.inference_port == 8080
        assert config.auto_start_slots is True
        assert config.max_inference_workers == 4

    def test_default_paths(self):
        """Test default path initialization."""
        config = DaemonConfig()

        assert config.db_path == Path("/var/lib/temms/temms.db")
        assert config.model_dir == Path("/var/lib/temms/models")
        assert config.policy_dir == Path("/etc/temms/policies")

    def test_custom_paths(self, temp_dir):
        """Test custom path configuration."""
        config = DaemonConfig(
            db_path=temp_dir / "custom.db",
            model_dir=temp_dir / "custom_models",
            policy_dir=temp_dir / "custom_policies",
        )

        assert config.db_path == temp_dir / "custom.db"
        assert config.model_dir == temp_dir / "custom_models"
        assert config.policy_dir == temp_dir / "custom_policies"


class TestTEMMSDaemon:
    """Tests for TEMMSDaemon class."""

    def test_init(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
    ):
        """Test daemon initialization."""
        config = DaemonConfig()

        daemon = TEMMSDaemon(
            config=config,
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            collectors=[],
        )

        assert daemon.config == config
        assert daemon.slot_manager == slot_manager
        assert daemon.condition_store == condition_store
        assert daemon.policy_engine == policy_engine
        assert daemon._running is False

    def test_from_config(self, temp_dir):
        """Test creating daemon from configuration."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
        )

        daemon = TEMMSDaemon.from_config(config)

        assert daemon is not None
        assert daemon.config == config
        assert len(daemon.collectors) > 0  # Should have default collectors


@pytest.mark.asyncio
class TestTEMMSDaemonAsync:
    """Async tests for TEMMSDaemon."""

    async def test_load_policies_no_dir(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test policy loading when directory doesn't exist."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "nonexistent_policies",
        )

        daemon = TEMMSDaemon(
            config=config,
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            collectors=[],
        )

        # Should not raise, just log warning
        await daemon._load_policies()

        assert len(policy_engine.list_policies()) == 0

    async def test_load_policies_with_file(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
        sample_policy_yaml,
    ):
        """Test policy loading with actual policy file."""
        policy_dir = temp_dir / "policies"
        policy_dir.mkdir()

        # Copy sample policy to policy dir
        import shutil
        shutil.copy(sample_policy_yaml, policy_dir / "test-policy.yaml")

        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=policy_dir,
        )

        daemon = TEMMSDaemon(
            config=config,
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            collectors=[],
        )

        await daemon._load_policies()

        assert len(policy_engine.list_policies()) == 1

    async def test_evaluate_all_slots_no_slots(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test evaluating policies with no slots."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
        )

        daemon = TEMMSDaemon(
            config=config,
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            collectors=[],
        )

        # Should not raise
        await daemon._evaluate_all_slots()

    async def test_stop_not_running(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test stopping daemon that's not running."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
        )

        daemon = TEMMSDaemon(
            config=config,
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            collectors=[],
        )

        # Should not raise
        await daemon.stop()

        assert daemon._running is False
