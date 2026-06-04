"""
Integration tests for the full inference flow.

Tests the complete pipeline from condition change to policy evaluation
to model switching and inference.
"""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from temms.core.cache import ModelCache, ModelFormat
from temms.core.storage import ModelStorage
from temms.slots.manager import SlotManager, SlotState
from temms.conditions.store import ConditionStore
from temms.policy.engine import PolicyEngine
from temms.inference.runtime import InferenceRuntime
from temms.inference.server import create_app
from temms.daemon.service import TEMMSDaemon, DaemonConfig
from temms.daemon.pending_ops import PendingOperationsStore


@pytest.fixture
def full_system(temp_dir):
    """Create full TEMMS system for integration testing."""
    db_path = temp_dir / "temms.db"
    model_dir = temp_dir / "models"
    model_dir.mkdir()

    # Create components
    model_cache = ModelCache(db_path)
    model_storage = ModelStorage(model_dir)
    slot_manager = SlotManager(db_path)
    condition_store = ConditionStore(db_path)
    policy_engine = PolicyEngine(condition_store)
    inference_runtime = InferenceRuntime(model_cache, model_storage)

    return {
        "db_path": db_path,
        "model_dir": model_dir,
        "model_cache": model_cache,
        "model_storage": model_storage,
        "slot_manager": slot_manager,
        "condition_store": condition_store,
        "policy_engine": policy_engine,
        "inference_runtime": inference_runtime,
    }


@pytest.fixture
def full_app(full_system):
    """Create FastAPI app with full system."""
    app = create_app(
        slot_manager=full_system["slot_manager"],
        condition_store=full_system["condition_store"],
        policy_engine=full_system["policy_engine"],
        model_cache=full_system["model_cache"],
        model_storage=full_system["model_storage"],
        inference_runtime=full_system["inference_runtime"],
    )
    return app


@pytest.fixture
def full_client(full_app):
    """Create test client with full system."""
    return TestClient(full_app)


class TestConditionToPolicyFlow:
    """Test flow from condition injection to policy evaluation."""

    def test_condition_injection_updates_store(self, full_client, full_system):
        """Test that injected conditions are stored correctly."""
        # Inject conditions via API
        response = full_client.post(
            "/v1/control/conditions",
            json={
                "conditions": {
                    "platform.compute.cpu_temp_c": 80.0,
                }
            },
        )

        assert response.status_code == 200

        # Verify condition is in store
        condition = full_system["condition_store"].get("platform.compute.cpu_temp_c")
        assert condition is not None
        assert condition.value == 80.0
        assert condition.priority == 1000  # Operator priority

    def test_explicit_slot_evaluate_applies_local_decision(
        self,
        full_client,
        full_system,
        sample_model_file,
        sample_policy_yaml,
    ):
        """Test the explicit local adaptive decision API."""
        full_system["policy_engine"].load_policy_from_file(sample_policy_yaml)
        dest_path, sha256, size = full_system["model_storage"].store_model(
            sample_model_file,
            "test-model-tiny-v1",
            verify=True,
        )
        full_system["model_cache"].add_cached_model(
            model_id="test-model-tiny-v1",
            name="test-model-tiny",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=dest_path,
            sha256=sha256,
            size_bytes=size,
            package_id="test-package",
        )
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision slot",
            required=True,
        )
        full_system["condition_store"].set(
            path="platform.compute.cpu_temp_c",
            value=82.0,
            source="sensor",
            priority=100,
        )
        full_system["inference_runtime"].load_model = AsyncMock(return_value=True)

        response = full_client.post(
            "/v1/control/slots/vision/evaluate",
            json={"apply": True},
        )

        assert response.status_code == 200
        decision = response.json()
        assert decision["status"] == "activated"
        assert decision["activated_model"] == "test-model-tiny-v1"
        assert (
            full_system["slot_manager"].get_slot("vision").active_model_id
            == "test-model-tiny-v1"
        )

    def test_policy_evaluation_with_matching_condition(
        self, full_client, full_system, sample_policy_yaml
    ):
        """Test that policy evaluates correctly when conditions match."""
        # Load policy
        full_system["policy_engine"].load_policy_from_file(sample_policy_yaml)

        # Create slot
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision slot",
            required=True,
            default_model="test-model",
            candidates=["test-model", "test-model-tiny"],
        )

        # Set condition that triggers policy (CPU temp >= 75)
        full_system["condition_store"].set(
            path="platform.compute.cpu_temp_c",
            value=80.0,
            source="test",
            priority=100,
        )

        # Evaluate policy
        result = full_system["policy_engine"].evaluate_slot("vision")

        # Should want to switch to tiny model
        assert result.switch_to == "test-model-tiny"
        assert result.is_default is False

    def test_policy_evaluation_with_non_matching_condition(
        self, full_client, full_system, sample_policy_yaml
    ):
        """Test that policy returns None when conditions don't match."""
        # Load policy
        full_system["policy_engine"].load_policy_from_file(sample_policy_yaml)

        # Create slot
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision slot",
            required=True,
            default_model="test-model",
        )

        # Set condition that doesn't trigger policy (CPU temp < 75)
        full_system["condition_store"].set(
            path="platform.compute.cpu_temp_c",
            value=50.0,
            source="test",
            priority=100,
        )

        # Evaluate policy
        result = full_system["policy_engine"].evaluate_slot("vision")

        # Should return no switch needed (no rules matched, no default_model in policy)
        assert result.switch_to is None


class TestSlotManagement:
    """Test slot creation and management."""

    def test_create_slot_and_check_status(self, full_client, full_system):
        """Test creating a slot and checking its status via API."""
        # Create slot directly
        full_system["slot_manager"].create_slot(
            name="targeting",
            description="Target tracking slot",
            required=False,
            default_model="tracker-v1",
        )

        # Check status via API
        response = full_client.get("/v1/slots/targeting/status")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "targeting"
        assert data["state"] == "stopped"
        assert data["required"] is False

    def test_system_status_shows_all_slots(self, full_client, full_system):
        """Test that system status shows all slots."""
        # Create multiple slots
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision",
            required=True,
        )
        full_system["slot_manager"].create_slot(
            name="targeting",
            description="Targeting",
            required=False,
        )

        # Check system status
        response = full_client.get("/v1/status")

        assert response.status_code == 200
        data = response.json()
        assert "vision" in data["slots"]
        assert "targeting" in data["slots"]


class TestDecisionLogging:
    """Test that model switches are properly logged."""

    def test_model_activation_logs_decision(self, full_system):
        """Test that activating a model creates a decision log entry."""
        # Create slot
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision",
            required=True,
            default_model="model-a",
        )

        # Set some conditions
        full_system["condition_store"].set(
            path="platform.compute.cpu_temp_c",
            value=75.0,
            source="test",
            priority=100,
        )

        # Activate model
        full_system["slot_manager"].activate_model(
            slot_name="vision",
            model_id="model-b",
            trigger_type="policy",
            trigger_detail="thermal-adaptive",
            conditions=full_system["condition_store"].get_snapshot(),
        )

        # Check decision log
        decisions = full_system["slot_manager"].get_decision_log("vision", limit=1)

        assert len(decisions) == 1
        assert decisions[0]["from_model"] is None  # First activation
        assert decisions[0]["to_model"] == "model-b"
        assert decisions[0]["trigger_type"] == "policy"
        assert decisions[0]["trigger_detail"] == "thermal-adaptive"

    def test_evidence_endpoint_exports_decision_bundle(self, full_client, full_system):
        """Test that the API exports portable decision evidence."""
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision",
            required=True,
        )
        full_system["condition_store"].set(
            path="environmental.visibility_m",
            value=50,
            source="operator",
            priority=1000,
        )
        full_system["slot_manager"].activate_model(
            slot_name="vision",
            model_id="model-lowlight",
            trigger_type="policy",
            trigger_detail="weather-adaptive/fog",
            conditions=full_system["condition_store"].get_snapshot(),
        )

        response = full_client.get("/v1/evidence?slot=vision")

        assert response.status_code == 200
        bundle = response.json()
        assert bundle["schema_version"] == "temms-evidence-bundle/v1"
        assert bundle["decisions"][0]["to_model"] == "model-lowlight"
        assert (
            bundle["decisions"][0]["conditions_snapshot"]["environmental"]["visibility_m"]
            == 50
        )
        assert bundle["integrity"]["payload_sha256"]


class TestOfflineLocalControl:
    """Test offline control keeps applying locally while buffering for sync."""

    def test_offline_condition_update_applies_locally_and_buffers(
        self,
        full_system,
        temp_dir,
    ):
        pending = PendingOperationsStore(temp_dir / "pending_operations.json")
        app = create_app(
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            inference_runtime=full_system["inference_runtime"],
            offline_mode=True,
            pending_operations=pending,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/control/conditions",
            json={"conditions": {"operational.mission.phase": "egress"}},
        )

        assert response.status_code == 200
        condition = full_system["condition_store"].get("operational.mission.phase")
        assert condition is not None
        assert condition.value == "egress"
        assert condition.source == "operator_api_offline"
        entries = pending.read_all()
        assert len(entries) == 1
        assert entries[0]["operation"] == "update_conditions"
        assert entries[0]["payload"]["applied_locally"] is True


class TestConditionPriority:
    """Test condition priority resolution."""

    def test_higher_priority_overrides_lower(self, full_system):
        """Test that higher priority conditions override lower."""
        store = full_system["condition_store"]

        # Set low priority condition
        store.set(
            path="test.value",
            value=100,
            source="sensor",
            priority=50,
        )

        # Set high priority condition
        store.set(
            path="test.value",
            value=200,
            source="operator",
            priority=1000,
        )

        # Check value
        condition = store.get("test.value")
        assert condition.value == 200
        assert condition.priority == 1000

    def test_lower_priority_does_not_override(self, full_system):
        """Test that lower priority conditions don't override higher."""
        store = full_system["condition_store"]

        # Set high priority condition
        store.set(
            path="test.value",
            value=200,
            source="operator",
            priority=1000,
        )

        # Try to set low priority condition
        store.set(
            path="test.value",
            value=100,
            source="sensor",
            priority=50,
        )

        # Check value - should still be high priority value
        condition = store.get("test.value")
        assert condition.value == 200
        assert condition.priority == 1000


@pytest.mark.asyncio
class TestAsyncDaemonOperations:
    """Async tests for daemon operations."""

    async def test_daemon_policy_loading(self, full_system, sample_policy_yaml, temp_dir):
        """Test that daemon loads policies from directory."""
        # Create policy directory
        policy_dir = temp_dir / "policies"
        policy_dir.mkdir()

        # Copy policy
        import shutil
        shutil.copy(sample_policy_yaml, policy_dir / "test.yaml")

        # Create daemon config
        config = DaemonConfig(
            db_path=full_system["db_path"],
            model_dir=full_system["model_dir"],
            policy_dir=policy_dir,
        )

        # Create daemon
        daemon = TEMMSDaemon(
            config=config,
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            collectors=[],
        )

        # Load policies
        await daemon._load_policies()

        # Check policies loaded
        policies = full_system["policy_engine"].list_policies()
        assert len(policies) == 1
        assert policies[0].metadata.name == "thermal-adaptive"
