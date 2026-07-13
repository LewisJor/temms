"""
Integration tests for the full inference flow.

Tests the complete pipeline from condition change to policy evaluation
to model switching and inference.
"""

import json

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from temms.core.cache import ModelCache, ModelFormat
from temms.core.storage import ModelStorage
from temms.slots.manager import SlotManager
from temms.conditions.store import ConditionStore
from temms.policy.engine import PolicyEngine
from temms.policy.schema import SlotPolicy
from temms.inference.runtime import InferenceRuntime
from temms.inference.server import create_app
from temms.daemon.service import TEMMSDaemon, DaemonConfig
from temms.daemon.pending_ops import PendingOperationsStore
from temms.daemon.deployment_state import DeploymentStateStore


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
            full_system["slot_manager"].get_slot("vision").active_model_id == "test-model-tiny-v1"
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
        assert bundle["decisions"][0]["conditions_snapshot"]["environmental"]["visibility_m"] == 50
        assert bundle["integrity"]["payload_sha256"]


class TestOfflineLocalControl:
    """Test offline control keeps applying locally while buffering for sync."""

    def test_offline_startup_records_connectivity_conditions(
        self,
        full_system,
        temp_dir,
    ):
        app = create_app(
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            inference_runtime=full_system["inference_runtime"],
            offline_mode=True,
        )
        TestClient(app)

        offline = full_system["condition_store"].get("operational.connectivity.offline")
        mode = full_system["condition_store"].get("operational.connectivity.mode")
        network = full_system["condition_store"].get("operational.connectivity.network_available")
        assert offline is not None
        assert offline.value is True
        assert offline.source == "startup_offline"
        assert offline.priority == 900
        assert mode is not None
        assert mode.value == "offline"
        assert network is not None
        assert network.value is False

    def test_offline_online_control_updates_connectivity_conditions(
        self,
        full_system,
        temp_dir,
    ):
        deployment_state = DeploymentStateStore(temp_dir / "deployment_state.json")
        app = create_app(
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            inference_runtime=full_system["inference_runtime"],
            deployment_state=deployment_state,
        )
        client = TestClient(app)

        offline_response = client.post("/v1/control/offline")

        assert offline_response.status_code == 200
        assert offline_response.json()["offline_mode"] is True
        assert "operational.connectivity.offline" in offline_response.json()["conditions"]
        assert deployment_state.get_state().value == "OFFLINE"
        offline = full_system["condition_store"].get("operational.connectivity.offline")
        mode = full_system["condition_store"].get("operational.connectivity.mode")
        network = full_system["condition_store"].get("operational.connectivity.network_available")
        assert offline is not None
        assert offline.value is True
        assert offline.source == "runtime_control"
        assert offline.priority == 900
        assert mode is not None
        assert mode.value == "offline"
        assert network is not None
        assert network.value is False

        online_response = client.post("/v1/control/online")

        assert online_response.status_code == 200
        assert online_response.json()["offline_mode"] is False
        assert deployment_state.get_state().value == "PENDING"
        offline = full_system["condition_store"].get("operational.connectivity.offline")
        mode = full_system["condition_store"].get("operational.connectivity.mode")
        network = full_system["condition_store"].get("operational.connectivity.network_available")
        assert offline is not None
        assert offline.value is False
        assert mode is not None
        assert mode.value == "online"
        assert network is not None
        assert network.value is True

    def test_offline_connectivity_condition_drives_policy_switch(
        self,
        full_system,
        sample_model_file,
    ):
        policy = SlotPolicy(
            metadata={"name": "connectivity-adaptive"},
            spec={
                "slot": "vision",
                "default_model": "daylight-model",
                "rules": [
                    {
                        "name": "offline-local-model",
                        "priority": 120,
                        "conditions": {
                            "all": [
                                {
                                    "metric": "operational.connectivity.offline",
                                    "operator": "eq",
                                    "value": True,
                                }
                            ]
                        },
                        "action": {"switch_to": "local-small"},
                    }
                ],
            },
        )
        full_system["policy_engine"].load_policy(policy)
        daylight_path, daylight_sha, daylight_size = full_system["model_storage"].store_model(
            sample_model_file, "daylight-model-v1", verify=True
        )
        local_path, local_sha, local_size = full_system["model_storage"].store_model(
            sample_model_file,
            "local-small-v1",
            verify=True,
        )
        full_system["model_cache"].add_cached_model(
            model_id="daylight-model-v1",
            name="daylight-model",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=daylight_path,
            sha256=daylight_sha,
            size_bytes=daylight_size,
            package_id="daylight-package",
        )
        full_system["model_cache"].add_cached_model(
            model_id="local-small-v1",
            name="local-small",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=local_path,
            sha256=local_sha,
            size_bytes=local_size,
            package_id="local-package",
        )
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision slot",
            required=True,
            candidates=["daylight-model", "local-small"],
        )
        full_system["slot_manager"].activate_model(
            slot_name="vision",
            model_id="daylight-model-v1",
            trigger_type="bootstrap",
            trigger_detail="startup",
        )
        full_system["inference_runtime"].load_model = AsyncMock(return_value=True)
        app = create_app(
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            inference_runtime=full_system["inference_runtime"],
        )
        client = TestClient(app)

        client.post("/v1/control/offline")
        response = client.post(
            "/v1/control/slots/vision/evaluate",
            json={"apply": True},
        )

        assert response.status_code == 200
        decision = response.json()
        assert decision["status"] == "activated"
        assert decision["trigger_detail"] == ("connectivity-adaptive/offline-local-model")
        assert decision["selected_model"] == "local-small-v1"
        assert decision["activated_model"] == "local-small-v1"
        assert decision["conditions"]["operational"]["connectivity"]["offline"] is True

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
            daemon_config=DaemonConfig(
                db_path=temp_dir / "temms-signed-ddil.db",
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                rollout_require_signature=True,
                rollout_signing_key="ddil-secret",
            ),
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

    def test_offline_deploy_sync_activates_buffered_model_intent(
        self,
        full_system,
        temp_dir,
        sample_model_file,
    ):
        pending = PendingOperationsStore(temp_dir / "pending_operations.json")
        daylight_path, daylight_sha, daylight_size = full_system["model_storage"].store_model(
            sample_model_file,
            "model-daylight",
            verify=True,
        )
        lowlight_path, lowlight_sha, lowlight_size = full_system["model_storage"].store_model(
            sample_model_file,
            "model-lowlight",
            verify=True,
        )
        full_system["model_cache"].add_cached_model(
            model_id="model-daylight",
            name="daylight",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=daylight_path,
            sha256=daylight_sha,
            size_bytes=daylight_size,
            package_id="pkg-vision",
        )
        full_system["model_cache"].add_cached_model(
            model_id="model-lowlight",
            name="lowlight",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=lowlight_path,
            sha256=lowlight_sha,
            size_bytes=lowlight_size,
            package_id="pkg-vision",
        )
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision slot",
            required=True,
        )
        full_system["slot_manager"].activate_model(
            slot_name="vision",
            model_id="model-daylight",
            trigger_type="bootstrap",
            trigger_detail="startup",
        )
        full_system["inference_runtime"].load_model = AsyncMock(return_value=True)
        app = create_app(
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            inference_runtime=full_system["inference_runtime"],
            offline_mode=True,
            pending_operations=pending,
            daemon_config=DaemonConfig(
                db_path=temp_dir / "temms-deploy-ddil.db",
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                rollout_require_signature=True,
                rollout_signing_key="ddil-secret",
            ),
        )
        client = TestClient(app)

        deploy_response = client.post(
            "/v1/control/deploy",
            json={
                "actor": "operator:test",
                "source": "ddil-unit-test",
                "package_id": "pkg-vision",
                "model_id": "model-lowlight",
                "device_id": "edge-1",
                "runtime_target_id": "temms-x86_64-cpu",
                "slot": "vision",
            },
        )

        assert deploy_response.status_code == 200
        assert deploy_response.json()["status"] == "buffered"
        signed_entries = pending.read_all()
        assert signed_entries[0]["signature"]["algorithm"] == "HMAC-SHA256"
        assert signed_entries[0]["signature"]["signer"] == "temms-ddil"
        preview_response = client.get("/v1/control/sync/preview")
        assert preview_response.status_code == 200
        assert preview_response.json()["status"] == "ready"
        assert preview_response.json()["ready"] == 1
        assert preview_response.json()["entries"][0]["replay_status"] == "ready"
        assert preview_response.json()["entries"][0]["resolved_model_id"] == "model-lowlight"
        sync_response = client.post("/v1/control/sync")

        assert sync_response.status_code == 200
        assert sync_response.json()["replayed"] == 1
        assert sync_response.json()["preflight"]["ready"] == 1
        assert pending.read_all() == []
        slot = full_system["slot_manager"].get_slot("vision")
        assert slot.active_model_id == "model-lowlight"
        assert slot.operator_override is not None
        assert slot.operator_override.model_id == "model-lowlight"
        assert slot.operator_override.source == "deploy_sync"
        full_system["inference_runtime"].load_model.assert_awaited_once_with(
            "vision",
            "model-lowlight",
        )

    def test_sync_replays_nested_deploy_request_context(
        self,
        full_system,
        temp_dir,
        sample_model_file,
    ):
        pending = PendingOperationsStore(temp_dir / "pending_operations.json")
        model_path, model_sha, model_size = full_system["model_storage"].store_model(
            sample_model_file,
            "model-lowlight",
            verify=True,
        )
        full_system["model_cache"].add_cached_model(
            model_id="model-lowlight",
            name="lowlight",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=model_path,
            sha256=model_sha,
            size_bytes=model_size,
            package_id="pkg-vision",
        )
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision slot",
            required=True,
        )
        full_system["slot_manager"].activate_model(
            slot_name="vision",
            model_id="model-daylight",
            trigger_type="bootstrap",
            trigger_detail="startup",
        )
        full_system["inference_runtime"].load_model = AsyncMock(return_value=True)
        pending.enqueue(
            "deploy",
            {
                "request": {
                    "actor": "operator:nested-test",
                    "source": "nested-ddil-request",
                    "reason": "field operator queued nested request",
                    "package_id": "pkg-vision",
                    "model_id": "model-lowlight",
                    "device_id": "edge-1",
                    "runtime_target_id": "temms-x86_64-cpu",
                    "slot": "vision",
                },
            },
            signing_key="ddil-secret",
        )
        app = create_app(
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            inference_runtime=full_system["inference_runtime"],
            pending_operations=pending,
            daemon_config=DaemonConfig(
                db_path=temp_dir / "temms-nested-deploy-ddil.db",
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                rollout_require_signature=True,
                rollout_signing_key="ddil-secret",
            ),
        )
        client = TestClient(app)

        preview_response = client.get("/v1/control/sync/preview")
        sync_response = client.post("/v1/control/sync")

        assert preview_response.status_code == 200
        preview_entry = preview_response.json()["entries"][0]
        assert preview_entry["replay_status"] == "ready"
        assert preview_entry["slot"] == "vision"
        assert preview_entry["model_id"] == "model-lowlight"
        assert preview_entry["package_id"] == "pkg-vision"
        assert preview_entry["device_id"] == "edge-1"
        assert preview_entry["runtime_target_id"] == "temms-x86_64-cpu"
        assert sync_response.status_code == 200
        assert sync_response.json()["replayed"] == 1
        assert pending.read_all() == []
        slot = full_system["slot_manager"].get_slot("vision")
        assert slot.active_model_id == "model-lowlight"
        assert slot.operator_override is not None
        assert slot.operator_override.reason == "field operator queued nested request"
        full_system["inference_runtime"].load_model.assert_awaited_once_with(
            "vision",
            "model-lowlight",
        )
        decision = full_system["slot_manager"].get_decision_log("vision", limit=1)[0]
        audit = json.loads(decision["audit_metadata"])
        assert audit["actor"] == "operator:nested-test"
        assert audit["source"] == "nested-ddil-request"
        assert audit["package_id"] == "pkg-vision"
        assert audit["device_id"] == "edge-1"
        assert audit["runtime_target_id"] == "temms-x86_64-cpu"

    def test_sync_preserves_only_failed_remainder_after_partial_replay(
        self,
        full_system,
        temp_dir,
        sample_model_file,
    ):
        pending = PendingOperationsStore(temp_dir / "pending_operations.json")
        model_path, model_sha, model_size = full_system["model_storage"].store_model(
            sample_model_file,
            "model-lowlight",
            verify=True,
        )
        full_system["model_cache"].add_cached_model(
            model_id="model-lowlight",
            name="lowlight",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=model_path,
            sha256=model_sha,
            size_bytes=model_size,
            package_id="pkg-vision",
        )
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision slot",
            required=True,
        )
        pending.enqueue(
            "update_conditions",
            {"conditions": {"operational.mission.phase": "egress"}},
            signing_key="ddil-secret",
        )
        pending.enqueue(
            "deploy",
            {
                "actor": "operator:test",
                "source": "ddil-partial-failure-test",
                "package_id": "pkg-vision",
                "model_id": "model-lowlight",
                "device_id": "edge-1",
                "runtime_target_id": "temms-x86_64-cpu",
                "slot": "vision",
            },
            signing_key="ddil-secret",
        )
        full_system["inference_runtime"].load_model = AsyncMock(
            side_effect=RuntimeError("runtime unavailable")
        )
        app = create_app(
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            inference_runtime=full_system["inference_runtime"],
            pending_operations=pending,
            daemon_config=DaemonConfig(
                db_path=temp_dir / "temms-partial-replay-ddil.db",
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                rollout_require_signature=True,
                rollout_signing_key="ddil-secret",
            ),
        )
        client = TestClient(app)

        failed_sync = client.post("/v1/control/sync")

        assert failed_sync.status_code == 500
        assert "runtime unavailable" in failed_sync.json()["detail"]
        condition = full_system["condition_store"].get("operational.mission.phase")
        assert condition is not None
        assert condition.value == "egress"
        remaining = pending.read_all()
        assert len(remaining) == 1
        assert remaining[0]["operation"] == "deploy"
        assert remaining[0]["payload"]["model_id"] == "model-lowlight"
        full_system["inference_runtime"].load_model.assert_awaited_once_with(
            "vision",
            "model-lowlight",
        )

        full_system["inference_runtime"].load_model = AsyncMock(return_value=True)
        resumed_sync = client.post("/v1/control/sync")

        assert resumed_sync.status_code == 200
        assert resumed_sync.json()["replayed"] == 1
        assert resumed_sync.json()["pending_cleared"] == 1
        assert pending.read_all() == []
        slot = full_system["slot_manager"].get_slot("vision")
        assert slot.active_model_id == "model-lowlight"
        full_system["inference_runtime"].load_model.assert_awaited_once_with(
            "vision",
            "model-lowlight",
        )

    def test_signed_offline_queue_rejects_tampered_pending_operation(
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
            daemon_config=DaemonConfig(
                db_path=temp_dir / "temms-tamper-ddil.db",
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                rollout_require_signature=True,
                rollout_signing_key="ddil-secret",
            ),
        )
        client = TestClient(app)

        response = client.post(
            "/v1/control/conditions",
            json={"conditions": {"operational.mission.phase": "egress"}},
        )
        entries = pending.read_all()
        entries[0]["payload"]["conditions"]["operational.mission.phase"] = "tampered"
        pending.path.write_text(json.dumps(entries), encoding="utf-8")

        sync_response = client.post("/v1/control/sync")

        assert response.status_code == 200
        assert sync_response.status_code == 409
        detail = sync_response.json()["detail"]
        assert detail["message"] == "Pending operation preflight failed"
        assert "signature" in detail["preflight"]["entries"][0]["reason"]
        assert detail["preflight"]["entries"][0]["signature_status"] == "invalid"
        assert pending.read_all()[0]["payload"]["conditions"]["operational.mission.phase"] == (
            "tampered"
        )

    def test_sync_preflight_blocks_unreplayable_pending_deploy(
        self,
        full_system,
        temp_dir,
    ):
        pending = PendingOperationsStore(temp_dir / "pending_operations.json")
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision slot",
            required=True,
        )
        pending.enqueue(
            "deploy",
            {
                "actor": "operator:test",
                "source": "ddil-unit-test",
                "package_id": "pkg-vision",
                "model_id": "missing-model",
                "device_id": "edge-1",
                "runtime_target_id": "temms-x86_64-cpu",
                "slot": "vision",
            },
            signing_key="ddil-secret",
        )
        app = create_app(
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            inference_runtime=full_system["inference_runtime"],
            pending_operations=pending,
            daemon_config=DaemonConfig(
                db_path=temp_dir / "temms-preflight-ddil.db",
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                rollout_require_signature=True,
                rollout_signing_key="ddil-secret",
            ),
        )
        client = TestClient(app)

        preview_response = client.get("/v1/control/sync/preview")
        sync_response = client.post("/v1/control/sync")

        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["status"] == "blocked"
        assert preview["blocked"] == 1
        assert preview["entries"][0]["signature_status"] == "verified"
        assert preview["entries"][0]["replay_status"] == "blocked"
        assert preview["entries"][0]["reason"] == "model not found: missing-model"
        assert sync_response.status_code == 409
        detail = sync_response.json()["detail"]
        assert detail["message"] == "Pending operation preflight failed"
        assert detail["preflight"]["blocked"] == 1
        assert len(pending.read_all()) == 1
        quarantine_response = client.post(
            "/v1/control/sync/quarantine-blocked",
            json={
                "actor": "operator:test",
                "reason": "test quarantine",
            },
        )

        assert quarantine_response.status_code == 200
        quarantine = quarantine_response.json()
        assert quarantine["quarantined"] == 1
        assert quarantine["remaining"] == 0
        assert pending.read_all() == []
        dead_letters = pending.read_dead_letter()
        assert len(dead_letters) == 1
        assert dead_letters[0]["actor"] == "operator:test"
        assert dead_letters[0]["reason"] == "test quarantine"
        assert dead_letters[0]["preflight"]["reason"] == "model not found: missing-model"

        requeue_response = client.post(
            "/v1/control/sync/requeue-dead-letters",
            json={
                "actor": "operator:test",
                "reason": "edge evidence remediated",
                "payload_sha256s": [dead_letters[0]["payload_sha256"]],
            },
        )

        assert requeue_response.status_code == 200
        requeued = requeue_response.json()
        assert requeued["require_ready"] is True
        assert requeued["requeued"] == 0
        assert requeued["blocked"] == 1
        assert requeued["pending"] == 0
        assert "model not found: missing-model" in requeued["blocked_entries"][0]["reason"]
        assert pending.read_all() == []
        dead_letters = pending.read_dead_letter()
        assert len(dead_letters) == 1
        assert "requeued" not in dead_letters[0]

        force_requeue_response = client.post(
            "/v1/control/sync/requeue-dead-letters",
            json={
                "actor": "operator:test",
                "reason": "edge evidence remediated",
                "payload_sha256s": [dead_letters[0]["payload_sha256"]],
                "force": True,
            },
        )

        assert force_requeue_response.status_code == 200
        requeued = force_requeue_response.json()
        assert requeued["require_ready"] is False
        assert requeued["requeued"] == 1
        assert requeued["pending"] == 1
        assert len(pending.read_all()) == 1
        dead_letters = pending.read_dead_letter()
        assert len(dead_letters) == 1
        assert dead_letters[0]["requeued"] is True
        assert dead_letters[0]["requeued_by"] == "operator:test"
        assert dead_letters[0]["requeue_reason"] == "edge evidence remediated"

        second_quarantine_response = client.post(
            "/v1/control/sync/quarantine-blocked",
            json={
                "actor": "operator:test",
                "reason": "still blocked after requeue",
            },
        )
        assert second_quarantine_response.status_code == 200
        assert second_quarantine_response.json()["quarantined"] == 1
        assert pending.read_all() == []

        acknowledge_response = client.post(
            "/v1/control/sync/acknowledge-dead-letters",
            json={
                "actor": "operator:test",
                "reason": "reviewed blocked DDIL intent",
            },
        )

        assert acknowledge_response.status_code == 200
        acknowledged = acknowledge_response.json()
        assert acknowledged["acknowledged"] == 1
        assert acknowledged["dead_letters"] == 2
        dead_letters = pending.read_dead_letter()
        assert len(dead_letters) == 2
        assert dead_letters[0]["requeued"] is True
        assert "acknowledged" not in dead_letters[0]
        assert dead_letters[1]["acknowledged"] is True
        assert dead_letters[1]["acknowledged_by"] == "operator:test"
        assert dead_letters[1]["acknowledgement_reason"] == "reviewed blocked DDIL intent"

    def test_sync_preflight_marks_superseded_deploy_intents(
        self,
        full_system,
        temp_dir,
        sample_model_file,
    ):
        pending = PendingOperationsStore(temp_dir / "pending_operations.json")
        daylight_path, daylight_sha, daylight_size = full_system["model_storage"].store_model(
            sample_model_file,
            "model-daylight",
            verify=True,
        )
        lowlight_path, lowlight_sha, lowlight_size = full_system["model_storage"].store_model(
            sample_model_file,
            "model-lowlight",
            verify=True,
        )
        full_system["model_cache"].add_cached_model(
            model_id="model-daylight",
            name="daylight",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=daylight_path,
            sha256=daylight_sha,
            size_bytes=daylight_size,
            package_id="pkg-vision",
        )
        full_system["model_cache"].add_cached_model(
            model_id="model-lowlight",
            name="lowlight",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=lowlight_path,
            sha256=lowlight_sha,
            size_bytes=lowlight_size,
            package_id="pkg-vision",
        )
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision slot",
            required=True,
        )
        full_system["slot_manager"].activate_model(
            slot_name="vision",
            model_id="model-daylight",
            trigger_type="bootstrap",
            trigger_detail="startup",
        )
        full_system["inference_runtime"].load_model = AsyncMock(return_value=True)
        app = create_app(
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            inference_runtime=full_system["inference_runtime"],
            offline_mode=True,
            pending_operations=pending,
            daemon_config=DaemonConfig(
                db_path=temp_dir / "temms-superseded-ddil.db",
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                rollout_require_signature=True,
                rollout_signing_key="ddil-secret",
            ),
        )
        client = TestClient(app)

        for model_id in ("model-daylight", "model-lowlight"):
            response = client.post(
                "/v1/control/deploy",
                json={
                    "actor": "operator:test",
                    "source": "ddil-stack-test",
                    "package_id": "pkg-vision",
                    "model_id": model_id,
                    "device_id": "edge-1",
                    "runtime_target_id": "temms-x86_64-cpu",
                    "slot": "vision",
                },
            )
            assert response.status_code == 200
            assert response.json()["status"] == "buffered"

        preview_response = client.get("/v1/control/sync/preview")

        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["status"] == "ready"
        assert preview["ready"] == 2
        assert preview["blocked"] == 0
        assert preview["superseded"] == 1
        assert preview["slot_outcomes"][0]["slot"] == "vision"
        assert preview["slot_outcomes"][0]["index"] == 1
        assert preview["slot_outcomes"][0]["model_id"] == "model-lowlight"
        assert preview["entries"][0]["replay_status"] == "superseded"
        assert preview["entries"][0]["ready"] is True
        assert preview["entries"][0]["superseded_by_index"] == 1
        assert preview["entries"][0]["superseded_by_model_id"] == "model-lowlight"
        assert preview["entries"][0]["final_for_slot"] is False
        assert preview["entries"][1]["replay_status"] == "ready"
        assert preview["entries"][1]["final_for_slot"] is True

        sync_response = client.post("/v1/control/sync")

        assert sync_response.status_code == 200
        sync_payload = sync_response.json()
        assert sync_payload["replayed"] == 1
        assert sync_payload["skipped"] == 1
        assert sync_payload["superseded_skipped"] == 1
        assert sync_payload["pending_cleared"] == 2
        assert sync_payload["preflight"]["superseded"] == 1
        assert pending.read_all() == []
        slot = full_system["slot_manager"].get_slot("vision")
        assert slot.active_model_id == "model-lowlight"
        full_system["inference_runtime"].load_model.assert_awaited_once_with(
            "vision",
            "model-lowlight",
        )

    def test_sync_skips_superseded_operator_override_intents(
        self,
        full_system,
        temp_dir,
        sample_model_file,
    ):
        pending = PendingOperationsStore(temp_dir / "pending_operations.json")
        daylight_path, daylight_sha, daylight_size = full_system["model_storage"].store_model(
            sample_model_file,
            "model-daylight",
            verify=True,
        )
        lowlight_path, lowlight_sha, lowlight_size = full_system["model_storage"].store_model(
            sample_model_file,
            "model-lowlight",
            verify=True,
        )
        full_system["model_cache"].add_cached_model(
            model_id="model-daylight",
            name="daylight",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=daylight_path,
            sha256=daylight_sha,
            size_bytes=daylight_size,
            package_id="pkg-vision",
        )
        full_system["model_cache"].add_cached_model(
            model_id="model-lowlight",
            name="lowlight",
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=lowlight_path,
            sha256=lowlight_sha,
            size_bytes=lowlight_size,
            package_id="pkg-vision",
        )
        full_system["slot_manager"].create_slot(
            name="vision",
            description="Vision slot",
            required=True,
        )
        for model_id, reason in (
            ("model-daylight", "operator queued daylight"),
            ("model-lowlight", "operator queued lowlight"),
        ):
            pending.enqueue(
                "override_model",
                {
                    "slot_name": "vision",
                    "request": {
                        "model": model_id,
                        "reason": reason,
                        "duration_s": 300,
                    },
                },
                signing_key="ddil-secret",
            )
        full_system["inference_runtime"].load_model = AsyncMock(return_value=True)
        app = create_app(
            slot_manager=full_system["slot_manager"],
            condition_store=full_system["condition_store"],
            policy_engine=full_system["policy_engine"],
            model_cache=full_system["model_cache"],
            model_storage=full_system["model_storage"],
            inference_runtime=full_system["inference_runtime"],
            offline_mode=True,
            pending_operations=pending,
            daemon_config=DaemonConfig(
                db_path=temp_dir / "temms-superseded-override-ddil.db",
                model_dir=temp_dir / "models",
                policy_dir=temp_dir / "policies",
                rollout_require_signature=True,
                rollout_signing_key="ddil-secret",
            ),
        )
        client = TestClient(app)

        preview_response = client.get("/v1/control/sync/preview")
        sync_response = client.post("/v1/control/sync")

        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["status"] == "ready"
        assert preview["superseded"] == 1
        assert preview["slot_outcomes"][0]["operation"] == "override_model"
        assert preview["slot_outcomes"][0]["model_id"] == "model-lowlight"
        assert preview["entries"][0]["replay_status"] == "superseded"
        assert preview["entries"][0]["superseded_by_model_id"] == "model-lowlight"
        assert sync_response.status_code == 200
        sync_payload = sync_response.json()
        assert sync_payload["replayed"] == 1
        assert sync_payload["skipped"] == 1
        assert sync_payload["superseded_skipped"] == 1
        assert pending.read_all() == []
        slot = full_system["slot_manager"].get_slot("vision")
        assert slot.active_model_id == "model-lowlight"
        assert slot.operator_override is not None
        assert slot.operator_override.model_id == "model-lowlight"
        assert slot.operator_override.reason == "operator queued lowlight"
        assert slot.operator_override.source == "api_sync"
        full_system["inference_runtime"].load_model.assert_awaited_once_with(
            "vision",
            "model-lowlight",
        )


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
