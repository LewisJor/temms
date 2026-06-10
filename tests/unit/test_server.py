"""
Unit tests for the inference server.
"""

import hashlib
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from temms.inference.runtime import InferenceRuntime
from temms.inference.server import (
    create_app,
)


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


def _trusted_package_manifest(package_id: str, name: str = "trusted-package"):
    """Package manifest with the full default Hub deployment gate satisfied."""
    return {
        "package_id": package_id,
        "name": name,
        "version": "1.0.0",
        "signature": {"key_id": "builder-key"},
        "validation": {
            "hash_verified": True,
            "signature_verified": True,
            "sim_passed": True,
            "sim_evidence": {
                "passed": True,
                "source": "temms-sim",
                "detail": "fog-regression",
                "run_id": "sim-42",
                "protected_by_signature": True,
            },
            "tests_passed": True,
            "test_evidence": {
                "passed": True,
                "source": "pytest",
                "detail": "unit-readiness",
                "run_id": "ci-99",
                "protected_by_signature": True,
            },
        },
    }


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


class TestHubUI:
    """Tests for the Hub model workflow."""

    def test_dashboard_and_conditions_render_condition_map(
        self,
        client,
        condition_store,
    ):
        """UI pages should handle ConditionStore's path-to-condition mapping."""
        condition_store.set(
            path="environmental.atmospheric.visibility_m",
            value=500,
            source="test",
            priority=100,
        )

        dashboard = client.get("/ui/")
        conditions = client.get("/ui/conditions")

        assert dashboard.status_code == 200
        assert conditions.status_code == 200
        assert "environmental.atmospheric.visibility_m" in conditions.text

    def test_dashboard_marks_inactive_targets_as_needing_attention(
        self,
        client,
        sample_slot,
    ):
        """Dashboard readiness should not claim healthy when targets are idle."""
        response = client.get("/ui/")

        assert response.status_code == 200
        assert "Mission Readiness" in response.text
        assert "Needs attention" in response.text
        assert "No active model" in response.text
        assert sample_slot.name in response.text

    def test_models_page_shows_registry_candidates(self, client):
        """Hub page should surface MLflow registry versions in TEMMS."""
        with patch("temms.mlflow_bridge.MLflowBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.available = True
            bridge.tracking_uri = "http://mlflow:5000"
            bridge.list_models.return_value = [{
                "name": "detector",
                "description": "",
                "tags": {},
                "versions": [{
                    "version": "3",
                    "status": "READY",
                    "stage": "Production",
                    "run_id": "abc123def456",
                    "experiment_id": "7",
                    "format": "onnx",
                }],
            }]

            response = client.get("/ui/models")

        assert response.status_code == 200
        assert "Model -> Package -> Target -> Deploy Edge/SIM" in response.text
        assert "Model List" in response.text
        assert "1 Model" in response.text
        assert "2 Package" in response.text
        assert "3 Target" in response.text
        assert "4 Deploy Edge/SIM" in response.text
        assert "detector" in response.text
        assert "Production" in response.text
        assert "Registry only" in response.text
        assert "Package" in response.text
        assert "Next: import to validate hash" in response.text
        assert "Signed" in response.text
        assert "Sim" in response.text
        assert "Test" in response.text
        assert "Val" in response.text
        assert "Registry" in response.text
        assert "Run" in response.text
        assert 'href="http://mlflow:5000/#/models/detector/versions/3"' in response.text
        assert (
            'href="http://mlflow:5000/#/experiments/7/runs/abc123def456"'
            in response.text
        )

    def test_models_page_links_imported_package_to_registry_source(
        self,
        client,
        model_cache,
        sample_cached_model,
    ):
        """Imported packages should still link back to their registry provenance."""
        model_cache.add_package(
            package_id=sample_cached_model.package_id,
            name="validated-package",
            version="1.0.0",
            source="fixture",
            manifest={
                "package_id": sample_cached_model.package_id,
                "name": "validated-package",
                "version": "1.0.0",
                "source_registry": "https://mlflow.example",
                "mlflow_run_id": "run-789",
                "mlflow_experiment_id": "12",
                "validation": {"hash_verified": True},
            },
        )

        with patch("temms.mlflow_bridge.MLflowBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.available = True
            bridge.tracking_uri = "https://mlflow.example"
            bridge.list_models.return_value = []

            response = client.get("/ui/models")

        assert response.status_code == 200
        assert sample_cached_model.name in response.text
        assert (
            'href="https://mlflow.example/#/models/test-model/versions/1.0.0"'
            in response.text
        )
        assert (
            'href="https://mlflow.example/#/experiments/12/runs/run-789"'
            in response.text
        )

    def test_models_page_marks_registry_list_errors_unavailable(self, client):
        """Hub should not show a false online state when MLflow listing fails."""
        with patch("temms.mlflow_bridge.MLflowBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.available = True
            bridge.tracking_uri = "http://localhost:5000"
            bridge.list_models.return_value = []
            bridge.last_error = "403 Forbidden"

            response = client.get("/ui/models")

        assert response.status_code == 200
        assert "Registry unavailable" in response.text
        assert "403 Forbidden" in response.text
        assert "Registry online" not in response.text

    def test_models_page_labels_hash_only_import_as_needs_evidence(
        self,
        client,
        model_cache,
        sample_cached_model,
        sample_slot,
    ):
        """Imported models missing the default gate should not look deploy-ready."""
        model_cache.add_package(
            package_id=sample_cached_model.package_id,
            name="validated-package",
            version="1.0.0",
            source="fixture",
            manifest={
                "package_id": sample_cached_model.package_id,
                "name": "validated-package",
                "version": "1.0.0",
                "validation": {"hash_verified": True},
            },
        )

        with patch("temms.mlflow_bridge.MLflowBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.available = True
            bridge.tracking_uri = "http://mlflow:5000"
            bridge.list_models.return_value = []

            response = client.get("/ui/models")

        assert response.status_code == 200
        assert sample_cached_model.name in response.text
        assert sample_slot.name in response.text
        assert "Needs evidence" in response.text
        assert "Missing required evidence: Signed, Sim, Test" in response.text
        assert "Evidence needed" in response.text

    def test_models_page_shows_ready_model_deploy_action(
        self,
        client,
        model_cache,
        sample_cached_model,
        sample_slot,
    ):
        """Fully evidenced Hub models should show as deploy-ready."""
        model_cache.add_package(
            package_id=sample_cached_model.package_id,
            name="trusted-package",
            version="1.0.0",
            source="fixture",
            manifest=_trusted_package_manifest(sample_cached_model.package_id),
        )

        with patch("temms.mlflow_bridge.MLflowBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.available = True
            bridge.tracking_uri = "http://mlflow:5000"
            bridge.list_models.return_value = []

            response = client.get("/ui/models")

        assert response.status_code == 200
        assert sample_cached_model.name in response.text
        assert sample_cached_model.id in response.text
        assert "Deploy ready" in response.text
        assert sample_slot.name in response.text
        assert "Deploy" in response.text

    def test_import_mlflow_model_imports_package(
        self,
        client,
        temp_dir,
        model_cache,
    ):
        """Selecting a registry version should import it into the Hub cache."""
        package_dir = temp_dir / "mlflow-detector-3"
        models_dir = package_dir / "models"
        models_dir.mkdir(parents=True)

        model_file = models_dir / "detector.onnx"
        model_file.write_bytes(b"fake onnx bytes")
        sha256 = hashlib.sha256(model_file.read_bytes()).hexdigest()

        manifest = {
            "schema_version": "v1",
            "package_id": "mlflow-pull-detector-3",
            "name": "detector",
            "version": "3",
            "created_at": "2026-06-09T00:00:00Z",
            "created_by": "mlflow-bridge",
            "models": [{
                "id": "detector-3",
                "name": "detector",
                "version": "3",
                "format": "onnx",
                "filename": "detector.onnx",
                "sha256": sha256,
                "size_bytes": model_file.stat().st_size,
                "metadata": {
                    "validation": {
                        "signature_verified": True,
                    }
                },
            }],
            "policies": [],
            "source_registry": "http://mlflow:5000",
            "mlflow_run_id": "abc123",
            "validation": {
                "signature_verified": True,
                "sim_passed": True,
                "tests_passed": True,
            },
        }
        (package_dir / "manifest.json").write_text(json.dumps(manifest))

        with patch("temms.mlflow_bridge.MLflowBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.available = True
            bridge.pull_model.return_value = package_dir

            response = client.post(
                "/ui/models/import-mlflow",
                data={"model_name": "detector", "model_version": "3"},
            )

        assert response.status_code == 200
        assert "Imported detector" in response.text
        assert "Refresh" not in response.text
        assert response.headers["HX-Redirect"] == "/ui/models"
        imported = model_cache.find_model("detector", "3")
        assert imported is not None
        assert imported.metadata["validation"]["hash_verified"] is True
        assert imported.metadata["validation"]["signature_verified"] is False

        package = model_cache.list_packages()[0]
        assert package.manifest["validation"]["hash_verified"] is True
        assert package.manifest["validation"]["signature_present"] is False
        assert package.manifest["validation"]["signature_verified"] is False
        assert package.manifest["validation"]["signature_detail"] == "No signature"

    def test_import_mlflow_model_handles_already_imported_model(
        self,
        client,
        model_cache,
        sample_cached_model,
    ):
        """Duplicate import should not imply the model is deploy-ready."""
        response = client.post(
            "/ui/models/import-mlflow",
            data={
                "model_name": sample_cached_model.name,
                "model_version": sample_cached_model.version,
            },
        )

        assert response.status_code == 200
        assert "already in Hub" in response.text
        assert "ready for edge deployment" not in response.text
        assert response.headers["HX-Redirect"] == "/ui/models"

    def test_package_import_refreshes_hub_catalog(
        self,
        client,
        temp_dir,
        model_cache,
    ):
        """Advanced package import should return operators to the updated catalog."""
        package_dir = temp_dir / "local-detector"
        models_dir = package_dir / "models"
        models_dir.mkdir(parents=True)
        model_file = models_dir / "detector.onnx"
        model_file.write_bytes(b"fake onnx bytes")
        sha256 = hashlib.sha256(model_file.read_bytes()).hexdigest()

        manifest = {
            "schema_version": "v1",
            "package_id": "local-detector",
            "name": "local-detector",
            "version": "1.0.0",
            "created_at": "2026-06-09T00:00:00Z",
            "models": [{
                "id": "local-detector-1",
                "name": "local-detector",
                "version": "1.0.0",
                "format": "onnx",
                "filename": model_file.name,
                "sha256": sha256,
                "size_bytes": model_file.stat().st_size,
                "metadata": {},
            }],
            "policies": [],
        }
        (package_dir / "manifest.json").write_text(json.dumps(manifest))

        response = client.post(
            "/ui/import",
            data={"package_path": str(package_dir)},
        )

        assert response.status_code == 200
        assert "Imported 1 models" in response.text
        assert response.headers["HX-Redirect"] == "/ui/models"
        assert model_cache.find_model("local-detector", "1.0.0") is not None

    def test_deploy_cached_model_to_slot(
        self,
        client,
        inference_runtime,
        model_cache,
        sample_cached_model,
        sample_slot,
        slot_manager,
    ):
        """Deploying from Hub should activate the model in the selected slot."""
        model_cache.add_package(
            package_id=sample_cached_model.package_id,
            name="trusted-package",
            version="1.0.0",
            source="fixture",
            manifest=_trusted_package_manifest(sample_cached_model.package_id),
        )

        with patch.object(
            inference_runtime,
            "load_model",
            new=AsyncMock(return_value=True),
        ):
            response = client.post(
                "/ui/models/deploy",
                data={
                    "model_id": sample_cached_model.id,
                    "slot_name": sample_slot.name,
                    "reason": "test deploy",
                },
            )

        assert response.status_code == 200
        assert "Deployed test-model" in response.text
        assert response.headers["HX-Redirect"] == "/ui/models"
        slot = slot_manager.get_slot(sample_slot.name)
        assert slot.active_model_id == sample_cached_model.id
        assert slot_manager.has_active_override(sample_slot.name) is True

    def test_deploy_requires_default_readiness_evidence(
        self,
        client,
        inference_runtime,
        sample_cached_model,
        sample_slot,
        slot_manager,
    ):
        """Hub deploy should block models without the full default readiness gate."""
        with patch.object(
            inference_runtime,
            "load_model",
            new=AsyncMock(return_value=True),
        ):
            response = client.post(
                "/ui/models/deploy",
                data={
                    "model_id": sample_cached_model.id,
                    "slot_name": sample_slot.name,
                    "reason": "test deploy",
                },
            )

        assert response.status_code == 200
        assert "Cannot deploy test-model" in response.text
        assert "Missing required evidence: Signed, Sim, Test, Val" in response.text
        slot = slot_manager.get_slot(sample_slot.name)
        assert slot.active_model_id is None

    def test_deploy_requires_signature_sim_and_test_by_default(
        self,
        client,
        inference_runtime,
        model_cache,
        sample_cached_model,
        sample_slot,
    ):
        """Hash validation alone should not satisfy the default deployment gate."""
        model_cache.add_package(
            package_id=sample_cached_model.package_id,
            name="validated-package",
            version="1.0.0",
            source="fixture",
            manifest={
                "package_id": sample_cached_model.package_id,
                "name": "validated-package",
                "version": "1.0.0",
                "validation": {"hash_verified": True},
            },
        )

        with patch.object(
            inference_runtime,
            "load_model",
            new=AsyncMock(return_value=True),
        ):
            response = client.post(
                "/ui/models/deploy",
                data={
                    "model_id": sample_cached_model.id,
                    "slot_name": sample_slot.name,
                    "reason": "test deploy",
                },
            )

        assert response.status_code == 200
        assert "Missing required evidence: Signed, Sim, Test" in response.text

    def test_target_override_requires_default_readiness_evidence(
        self,
        client,
        inference_runtime,
        sample_cached_model,
        sample_slot,
        slot_manager,
    ):
        """Manual target overrides should use the same gate as Models deploy."""
        with patch.object(
            inference_runtime,
            "load_model",
            new=AsyncMock(return_value=True),
        ) as load_model:
            response = client.post(
                f"/ui/slots/{sample_slot.name}/override",
                data={
                    "model_name": sample_cached_model.name,
                    "reason": "manual test",
                },
            )

        assert response.status_code == 200
        assert "Cannot override vision with test-model" in response.text
        assert "Missing required evidence: Signed, Sim, Test, Val" in response.text
        load_model.assert_not_called()
        slot = slot_manager.get_slot(sample_slot.name)
        assert slot.active_model_id is None

    def test_target_override_allows_all_readiness_evidence(
        self,
        client,
        inference_runtime,
        model_cache,
        sample_cached_model,
        sample_slot,
        slot_manager,
    ):
        """Manual override remains available once the package gate passes."""
        model_cache.add_package(
            package_id=sample_cached_model.package_id,
            name="trusted-package",
            version="1.0.0",
            source="fixture",
            manifest=_trusted_package_manifest(sample_cached_model.package_id),
        )

        with patch.object(
            inference_runtime,
            "load_model",
            new=AsyncMock(return_value=True),
        ):
            response = client.post(
                f"/ui/slots/{sample_slot.name}/override",
                data={
                    "model_name": sample_cached_model.name,
                    "reason": "manual test",
                },
            )

        assert response.status_code == 200
        assert "Override set: test-model" in response.text
        slot = slot_manager.get_slot(sample_slot.name)
        assert slot.active_model_id == sample_cached_model.id
        assert slot_manager.has_active_override(sample_slot.name) is True

    def test_deploy_allows_all_readiness_evidence(
        self,
        client,
        inference_runtime,
        model_cache,
        sample_cached_model,
        sample_slot,
        slot_manager,
    ):
        """The default gate should allow deploy after every readiness check passes."""
        model_cache.add_package(
            package_id=sample_cached_model.package_id,
            name="trusted-package",
            version="1.0.0",
            source="fixture",
            manifest=_trusted_package_manifest(sample_cached_model.package_id),
        )

        with patch.object(
            inference_runtime,
            "load_model",
            new=AsyncMock(return_value=True),
        ):
            response = client.post(
                "/ui/models/deploy",
                data={
                    "model_id": sample_cached_model.id,
                    "slot_name": sample_slot.name,
                    "reason": "test deploy",
                },
            )

        assert response.status_code == 200
        assert "Deployed test-model" in response.text
        assert response.headers["HX-Redirect"] == "/ui/models"
        slot = slot_manager.get_slot(sample_slot.name)
        assert slot.active_model_id == sample_cached_model.id

    def test_models_page_uses_package_readiness_evidence(
        self,
        client,
        model_cache,
        sample_cached_model,
    ):
        """Trust chips should reflect persisted package evidence."""
        model_cache.add_package(
            package_id=sample_cached_model.package_id,
            name="trusted-package",
            version="1.0.0",
            source="fixture",
            manifest={
                "package_id": sample_cached_model.package_id,
                "name": "trusted-package",
                "version": "1.0.0",
                "signature": {"key_id": "builder-key"},
                "validation": {
                    "hash_verified": True,
                    "signature_verified": True,
                    "sim_passed": True,
                    "sim_evidence": {
                        "passed": True,
                        "source": "temms-sim",
                        "detail": "fog-regression",
                        "run_id": "sim-42",
                        "protected_by_signature": True,
                    },
                    "tests_passed": True,
                    "test_evidence": {
                        "passed": True,
                        "source": "pytest",
                        "detail": "unit-readiness",
                        "run_id": "ci-99",
                        "protected_by_signature": True,
                    },
                },
            },
        )

        with patch("temms.mlflow_bridge.MLflowBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.available = True
            bridge.tracking_uri = "http://mlflow:5000"
            bridge.list_models.return_value = []

            response = client.get("/ui/models")

        assert response.status_code == 200
        assert "Signature verified" in response.text
        assert "fog-regression; source: temms-sim; run: sim-42; signed" in response.text
        assert "unit-readiness; source: pytest; run: ci-99; signed" in response.text
        assert "Hash verified" in response.text
