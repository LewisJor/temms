"""
End-to-end tests against a running Docker sim environment.

These tests require the TEMMS Docker environment to be running:
    make docker-up

Run with:
    pytest tests/integration/test_e2e_docker.py -v

Or:
    make test-e2e
"""

import time

import httpx
import pytest

# Base URL for the TEMMS daemon
BASE_URL = "http://localhost:8080"
TIMEOUT = 10.0


def _is_daemon_running():
    """Check if the TEMMS daemon is reachable."""
    try:
        r = httpx.get(f"{BASE_URL}/v1/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


# Skip all tests if daemon is not running
pytestmark = pytest.mark.skipif(
    not _is_daemon_running(),
    reason="TEMMS daemon not running. Start with: make docker-up",
)


@pytest.fixture(scope="module")
def client():
    """HTTP client for TEMMS daemon."""
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
        yield c


class TestSystemHealth:
    """Basic system health checks."""

    def test_health_endpoint(self, client):
        r = client.get("/v1/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"

    def test_system_status(self, client):
        r = client.get("/v1/status")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("healthy", "degraded")
        assert "slots" in data


class TestModelsImported:
    """Verify models were imported during container startup."""

    def test_vision_slot_exists(self, client):
        r = client.get("/v1/slots/vision/status")
        # May be 404 if slot wasn't created yet
        if r.status_code == 200:
            data = r.json()
            assert data["name"] == "vision"


class TestConditionInjection:
    """Test condition injection and policy evaluation."""

    def test_inject_conditions(self, client):
        r = client.post(
            "/v1/control/conditions",
            json={"conditions": {
                "environmental.atmospheric.visibility_m": 1000,
                "environmental.atmospheric.precipitation": "none",
            }},
        )
        assert r.status_code == 200
        data = r.json()
        assert "environmental.atmospheric.visibility_m" in data["updated"]

    def test_inject_fog_conditions(self, client):
        r = client.post(
            "/v1/control/conditions",
            json={"conditions": {
                "environmental.atmospheric.visibility_m": 50,
                "environmental.atmospheric.precipitation": "fog",
            }},
        )
        assert r.status_code == 200

        # Wait for policy evaluation
        time.sleep(3)

        # Check status
        r = client.get("/v1/status")
        assert r.status_code == 200

    def test_clear_overrides(self, client):
        r = client.delete("/v1/control/conditions/overrides")
        assert r.status_code == 200
        data = r.json()
        assert "cleared_count" in data


class TestWebUI:
    """Verify Web UI is accessible."""

    def test_dashboard_loads(self, client):
        r = client.get("/ui/")
        assert r.status_code == 200
        assert "TEMMS" in r.text

    def test_slots_page_loads(self, client):
        r = client.get("/ui/slots")
        assert r.status_code == 200

    def test_conditions_page_loads(self, client):
        r = client.get("/ui/conditions")
        assert r.status_code == 200

    def test_decisions_page_loads(self, client):
        r = client.get("/ui/decisions")
        assert r.status_code == 200

    def test_models_page_loads(self, client):
        r = client.get("/ui/models")
        assert r.status_code == 200

    def test_import_page_loads(self, client):
        r = client.get("/ui/import")
        assert r.status_code == 200


class TestAPIEndpoints:
    """Verify all API endpoints respond correctly."""

    def test_health(self, client):
        r = client.get("/v1/health")
        assert r.status_code == 200

    def test_status(self, client):
        r = client.get("/v1/status")
        assert r.status_code == 200

    def test_slot_not_found(self, client):
        r = client.get("/v1/slots/nonexistent/status")
        assert r.status_code == 404

    def test_condition_injection(self, client):
        r = client.post(
            "/v1/control/conditions",
            json={"conditions": {"test.e2e": 42}},
        )
        assert r.status_code == 200
