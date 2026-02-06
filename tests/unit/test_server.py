"""
Unit tests for the inference server.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from fastapi.testclient import TestClient

from temms.inference.server import (
    create_app,
    HealthResponse,
    SlotStatusResponse,
    SystemStatusResponse,
    InferenceResponse,
)
from temms.inference.runtime import InferenceRuntime
from temms.slots.manager import SlotState


@pytest.fixture
def inference_runtime(model_cache, model_storage):
    """Create InferenceRuntime instance."""
    return InferenceRuntime(model_cache, model_storage)


@pytest.fixture
def app(
    slot_manager,
    condition_store,
    policy_engine,
    model_cache,
    model_storage,
    inference_runtime,
):
    """Create FastAPI test app."""
    return create_app(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
        model_storage=model_storage,
        inference_runtime=inference_runtime,
    )


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


class TestHealthEndpoint:
    """Tests for health endpoint."""

    def test_health_returns_ok(self, client):
        """Test health endpoint returns OK."""
        response = client.get("/v1/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "timestamp" in data


class TestStatusEndpoint:
    """Tests for system status endpoint."""

    def test_status_no_slots(self, client):
        """Test status with no slots."""
        response = client.get("/v1/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["slots"] == {}
        assert data["conditions_count"] == 0
        assert data["policies_count"] == 0
        assert data["uptime_seconds"] >= 0

    def test_status_with_slot(self, client, sample_slot):
        """Test status with a slot."""
        response = client.get("/v1/status")

        assert response.status_code == 200
        data = response.json()
        assert "vision" in data["slots"]


class TestSlotStatusEndpoint:
    """Tests for slot status endpoint."""

    def test_slot_status_not_found(self, client):
        """Test slot status for non-existent slot."""
        response = client.get("/v1/slots/nonexistent/status")

        assert response.status_code == 404

    def test_slot_status_exists(self, client, sample_slot):
        """Test slot status for existing slot."""
        response = client.get("/v1/slots/vision/status")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "vision"
        assert data["description"] == "Vision processing slot"
        assert data["required"] is True
        assert data["state"] == "stopped"


class TestInferenceEndpoint:
    """Tests for inference endpoint."""

    def test_infer_slot_not_found(self, client):
        """Test inference on non-existent slot."""
        response = client.post(
            "/v1/slots/nonexistent/infer",
            files={"file": ("test.jpg", b"fake image data", "image/jpeg")},
        )

        assert response.status_code == 404

    def test_infer_slot_not_running(self, client, sample_slot):
        """Test inference on slot that's not running."""
        response = client.post(
            "/v1/slots/vision/infer",
            files={"file": ("test.jpg", b"fake image data", "image/jpeg")},
        )

        assert response.status_code == 503
        assert "not running" in response.json()["detail"]


class TestControlEndpoints:
    """Tests for control endpoints."""

    def test_update_conditions(self, client):
        """Test updating conditions via API."""
        response = client.post(
            "/v1/control/conditions",
            json={
                "conditions": {
                    "platform.compute.cpu_temp_c": 75.5,
                    "environmental.atmospheric.visibility_m": 500,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["updated"]) == 2
        assert "platform.compute.cpu_temp_c" in data["updated"]

    def test_clear_condition_overrides(self, client, condition_store):
        """Test clearing condition overrides."""
        # First set an override
        condition_store.set(
            path="test.condition",
            value=100,
            source="test",
            priority=1000,  # Operator priority
        )

        response = client.delete("/v1/control/conditions/overrides")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["cleared_count"] >= 0

    def test_override_model_slot_not_found(self, client):
        """Test model override on non-existent slot."""
        response = client.post(
            "/v1/control/slots/nonexistent/model",
            json={"model": "test-model", "reason": "testing"},
        )

        assert response.status_code == 404

    def test_override_model_model_not_found(self, client, sample_slot):
        """Test model override with non-existent model."""
        response = client.post(
            "/v1/control/slots/vision/model",
            json={"model": "nonexistent-model", "reason": "testing"},
        )

        assert response.status_code == 404
        assert "Model not found" in response.json()["detail"]
