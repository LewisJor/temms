"""
Unit tests for the TEMMS daemon service.
"""

import asyncio
import hashlib
import json
import pytest
import httpx
from unittest.mock import AsyncMock
from pathlib import Path
from types import SimpleNamespace

from temms.core.cache import ModelFormat
from temms.daemon.pending_preflight import (
    pending_sync_preflight,
    runtime_target_assessment_sha256,
)
from temms.daemon.service import TEMMSDaemon, DaemonConfig, _hub_base_url, _hub_headers
from temms.policy.schema import (
    Condition,
    ConditionGroup,
    PolicyAction,
    PolicyRule,
    SlotPolicy,
    SlotPolicyMetadata,
    SlotPolicySpec,
)
from temms.slots.manager import SlotState


def _release_package(hub, package_id: str) -> dict:
    hub.promote_package(
        package_id,
        "validated",
        actor="operator:validator",
        reason="runtime validation passed",
    )
    hub.promote_package(
        package_id,
        "approved",
        actor="operator:approver",
        reason="package approved for release",
    )
    return hub.promote_package(
        package_id,
        "released",
        actor="operator:release",
        reason="package released for rollout",
    )


def test_pending_sync_preflight_blocks_stale_runtime_retarget_assessment_hash():
    """DDIL replay fails closed when signed retarget proof no longer matches target facts."""
    capability_hash = "b" * 64
    runtime_target = {
        "runtime_target_id": "gpu-fit",
        "image": "registry.example.com/gpu-fit:latest",
        "registry": "registry.example.com",
        "arch": "amd64",
        "device_profiles": ["x86_64-cpu"],
    }
    runtime_lane = {
        "lane_id": "gpu-cuda",
        "label": "CUDA",
        "execution_engine": "onnxruntime",
        "acceleration": "cuda",
    }
    artifact_lane = {
        "status": "go",
        "state": "native artifact",
        "model_format": "onnx",
        "lane_id": "onnx-cuda",
    }
    capability_lock = {
        "schema_version": "temms-runtime-capability-lock/v1",
        "status": "locked",
        "capability_sha256": capability_hash,
        "runtime_target_id": "gpu-fit",
        "runtime_mode": "onnxruntime",
        "runtime_target": runtime_target,
        "artifact_lane": artifact_lane,
        "failures": [],
    }
    signed_assessment = {
        "runtime_target_id": "gpu-fit",
        "rank": 1,
        "selected": True,
        "best": True,
        "status": "eligible",
        "eligible": True,
        "score": 99,
        "tier": "optimal",
        "detail": "CUDA target satisfies the model path",
        "runtime_target": runtime_target,
        "runtime_lane": runtime_lane,
        "artifact_lane": artifact_lane,
        "runtime_capability_lock": capability_lock,
        "benchmark_id": "benchmark-gpu",
        "latency_ms_p95": 4.0,
        "throughput_ips": 230.0,
        "component_states": {
            "runtime_validation": {
                "status": "go",
                "state": "validated",
                "validation_id": "validation-gpu",
            }
        },
    }
    signed_assessment_hash = runtime_target_assessment_sha256(signed_assessment)
    current_assessment = {
        **signed_assessment,
        "runtime_lane": {
            **runtime_lane,
            "optimization_goal": "throughput",
        },
    }
    current_assessment_hash = runtime_target_assessment_sha256(current_assessment)
    assert signed_assessment_hash != current_assessment_hash

    proof = {
        "schema_version": "temms-ddil-runtime-retarget-proof/v1",
        "status": "proved",
        "runtime_target_id": "gpu-fit",
        "best": True,
        "eligible": True,
        "runtime_fit_score": 99,
        "runtime_capability_lock": capability_lock,
        "capability_sha256": capability_hash,
        "runtime_validation_id": "validation-gpu",
        "benchmark_id": "benchmark-gpu",
        "target_assessment_sha256": signed_assessment_hash,
    }
    payload = {
        "slot": "vision",
        "model_id": "model-vision",
        "package_id": "pkg-vision",
        "device_id": "edge-1",
        "runtime_target_id": "gpu-fit",
        "_temms_runtime_retarget": [
            {
                "schema_version": "temms-runtime-retarget/v1",
                "runtime_target_id": "gpu-fit",
                "runtime_target_proof": proof,
            }
        ],
    }

    class FakeHub:
        def deployment_readiness(self, **kwargs):
            return {
                "schema_version": "temms-deployment-readiness/v1",
                "status": "go",
                "selection": {
                    "package_id": "pkg-vision",
                    "model_id": "model-vision",
                    "device_id": "edge-1",
                    "runtime_target_id": "gpu-fit",
                    "slot": "vision",
                },
                "gates": [],
                "runtime_fit": {
                    "score": 99,
                    "tier": "optimal",
                    "runtime_lane": current_assessment["runtime_lane"],
                    "artifact_lane": artifact_lane,
                    "runtime_capability_lock": capability_lock,
                    "target_selection": {
                        "status": "best",
                        "best_runtime_target_id": "gpu-fit",
                    },
                },
                "production_admission": {"status": "go", "apply_allowed": True},
                "edge_execution_contract": {
                    "status": "go",
                    "recommended_action": "apply_or_stage",
                    "runtime_capability_lock": capability_lock,
                    "target_selection": {
                        "status": "best",
                        "best_runtime_target_id": "gpu-fit",
                    },
                    "target_assessments": [current_assessment],
                },
                "runtime_workbench": {
                    "schema_version": "temms-runtime-workbench/v1",
                    "status": "go",
                    "selected_runtime_target_id": "gpu-fit",
                    "best_runtime_target_id": "gpu-fit",
                    "target_selection": {"status": "best"},
                    "summary": {
                        "target_count": 1,
                        "eligible_target_count": 1,
                        "blocked_target_count": 0,
                        "selected_is_best": True,
                        "production_apply_allowed": True,
                    },
                    "targets": [current_assessment],
                },
            }

    state = SimpleNamespace(
        daemon_config=SimpleNamespace(
            rollout_require_signature=False,
            rollout_signing_key=None,
            rollout_signing_key_file=None,
        ),
        hub_lite=FakeHub(),
        model_cache=SimpleNamespace(
            get_model=lambda model_id: SimpleNamespace(id=model_id, package_id="pkg-vision"),
            find_model=lambda model_id: None,
        ),
        pending_operations=[
            {
                "operation": "deploy",
                "payload": payload,
            }
        ],
        slot_manager=SimpleNamespace(get_slot=lambda slot: SimpleNamespace(name=slot)),
    )

    preflight = pending_sync_preflight(state)
    entry = preflight["entries"][0]

    assert preflight["status"] == "blocked"
    assert entry["ready"] is False
    assert entry["reason"] == "runtime retarget proof is stale: target assessment changed"
    assert entry["hub_runtime_retarget_proof_status"] == "stale_target_assessment"
    assert (
        entry["hub_runtime_retarget_proof_signed_target_assessment_sha256"]
        == signed_assessment_hash
    )
    assert (
        entry["hub_runtime_retarget_proof_current_target_assessment_sha256"]
        == current_assessment_hash
    )


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

    def test_inference_env_defaults(self, monkeypatch):
        """Test inference bind settings can be provided by environment."""
        monkeypatch.setenv("TEMMS_HOST", "127.0.0.1")
        monkeypatch.setenv("TEMMS_PORT", "18080")

        config = DaemonConfig()

        assert config.inference_host == "127.0.0.1"
        assert config.inference_port == 18080

    def test_explicit_inference_settings_override_env(self, monkeypatch):
        """Test explicit inference bind settings override environment defaults."""
        monkeypatch.setenv("TEMMS_HOST", "127.0.0.1")
        monkeypatch.setenv("TEMMS_PORT", "18080")

        config = DaemonConfig(inference_host="192.0.2.10", inference_port=9000)

        assert config.inference_host == "192.0.2.10"
        assert config.inference_port == 9000

    def test_inference_env_aliases_override_deploy_env(self, monkeypatch):
        """Test explicit inference env aliases take precedence over deploy env names."""
        monkeypatch.setenv("TEMMS_HOST", "127.0.0.1")
        monkeypatch.setenv("TEMMS_PORT", "18080")
        monkeypatch.setenv("TEMMS_INFERENCE_HOST", "0.0.0.0")
        monkeypatch.setenv("TEMMS_INFERENCE_PORT", "28080")

        config = DaemonConfig()

        assert config.inference_host == "0.0.0.0"
        assert config.inference_port == 28080

    def test_invalid_inference_env_port_is_rejected(self, monkeypatch):
        """Test invalid inference port env values fail fast."""
        monkeypatch.setenv("TEMMS_PORT", "not-a-port")

        with pytest.raises(ValueError, match="TEMMS_PORT must be an integer"):
            DaemonConfig()

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

    def test_hub_sync_env_defaults(self, monkeypatch):
        """Test Hub Lite sync settings can be provided by environment."""
        monkeypatch.setenv("TEMMS_HUB_URL", "http://hub.example:8080")
        monkeypatch.setenv("TEMMS_HUB_TOKEN", "hub-token")
        monkeypatch.setenv("TEMMS_DEVICE_ID", "edge-1")
        monkeypatch.setenv("TEMMS_DEVICE_PROFILE", "x86_64-cpu")
        monkeypatch.setenv("TEMMS_HUB_SYNC_INTERVAL_S", "2.5")
        monkeypatch.setenv("TEMMS_EDGE_HEARTBEAT_INTERVAL_S", "7.5")
        monkeypatch.setenv("TEMMS_HUB_AUTO_APPLY", "true")
        monkeypatch.setenv("TEMMS_ROLLOUT_REQUIRE_SIGNATURE", "false")
        monkeypatch.setenv("TEMMS_PACKAGE_SIGNING_KEY", "package-secret")

        config = DaemonConfig()

        assert config.hub_url == "http://hub.example:8080"
        assert config.hub_token == "hub-token"
        assert config.hub_device_id == "edge-1"
        assert config.hub_device_profile == "x86_64-cpu"
        assert config.hub_sync_interval_s == 2.5
        assert config.edge_heartbeat_interval_s == 7.5
        assert config.hub_auto_apply is True
        assert config.rollout_require_signature is False
        assert config.rollout_signing_key == "package-secret"

    def test_hub_url_and_headers_helpers(self):
        """Test Hub Lite URL normalization and token headers."""
        assert _hub_base_url("http://hub:8080") == "http://hub:8080/v1/hub"
        assert _hub_base_url("http://hub:8080/v1/hub") == "http://hub:8080/v1/hub"
        assert _hub_headers("secret") == {"X-TEMMS-Token": "secret"}
        assert _hub_headers(None) == {}


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

    def test_from_config_default_paths_fall_back_for_non_root(self, temp_dir, monkeypatch):
        """Test default system paths move to user state when unwritable."""
        monkeypatch.delenv("TEMMS_DATA_DIR", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(temp_dir / "state"))

        def fake_access(path, mode):
            path = Path(path)
            if str(path).startswith("/var/lib") or str(path).startswith("/etc"):
                return False
            return True

        monkeypatch.setattr("temms.daemon.service.os.access", fake_access)
        config = DaemonConfig()

        daemon = TEMMSDaemon.from_config(config)

        fallback_dir = temp_dir / "state" / "temms"
        assert daemon.config == config
        assert config.db_path == fallback_dir / "temms.db"
        assert config.model_dir == fallback_dir / "models"
        assert config.deployment_state_path == fallback_dir / "deployment_state.json"
        assert config.pending_operations_path == fallback_dir / "pending_operations.json"
        assert config.hub_state_path == fallback_dir / "hub_lite.json"
        assert config.telemetry_path == fallback_dir / "telemetry.jsonl"
        assert config.policy_dir == fallback_dir / "policies"
        assert config.model_dir.exists()
        assert config.policy_dir.exists()

    def test_hub_inventory_reports_structured_runtime_capabilities(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
        monkeypatch,
    ):
        """Test heartbeat inventory is compatible with Hub runtime checks."""

        class FakeCapabilities:
            def to_dict(self):
                return {
                    "os": "Linux",
                    "machine": "aarch64",
                    "python": "3.11",
                    "device_profile": "arm64-jetson",
                    "runtimes": {
                        "onnxruntime": {
                            "available": True,
                            "providers": ["CUDAExecutionProvider"],
                        }
                    },
                    "accelerators": {"nvidia": {"available": True}},
                    "memory": {"available_mb": 4096.0, "total_mb": 8192.0},
                    "storage": {"available_mb": 65536.0, "total_mb": 131072.0},
                    "thermal": {"temperature_c": 42.0},
                    "power": {"battery_percent": 96.0, "source": "mains"},
                }

        monkeypatch.setattr(
            "temms.core.runtime_profiles.detect_runtime_capabilities",
            lambda: FakeCapabilities(),
        )
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            hub_state_path=temp_dir / "hub_lite.json",
            deployment_state_path=temp_dir / "deployment_state.json",
            hub_device_id="edge-local",
            hub_device_profile="orin",
            inference_host="127.0.0.1",
            inference_port=18080,
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

        inventory = daemon._hub_inventory()

        assert inventory["device_profile"] == "orin-tensorrt"
        assert inventory["runtimes"]["onnxruntime"]["available"] is True
        assert inventory["runtimes"]["onnxruntime"]["providers"] == ["CUDAExecutionProvider"]
        assert inventory["accelerators"]["nvidia"]["available"] is True
        assert inventory["temms"] == {
            "offline_mode": False,
            "api_host": "127.0.0.1",
            "api_port": 18080,
        }

        heartbeat = daemon._edge_heartbeat_once()
        stored = daemon.hub_lite.get_device("edge-local")
        deployment_status = daemon.hub_lite.deployment_status()["deployment_status"][
            "edge-local"
        ]

        assert heartbeat["device_id"] == "edge-local"
        assert heartbeat["status"] == "online"
        assert heartbeat["last_seen_at"]
        assert stored["profile"] == "orin-tensorrt"
        assert stored["inventory"]["memory"]["available_mb"] == 4096.0
        assert stored["inventory"]["storage"]["available_mb"] == 65536.0
        assert stored["inventory"]["thermal"]["temperature_c"] == 42.0
        assert deployment_status["state"] == daemon.deployment_state.get_state().value
        assert deployment_status["slots"] == {}

    def test_demo_seed_inventory_uses_healthy_resource_floor(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
        monkeypatch,
    ):
        """Test Docker demo heartbeat does not inherit accidental host pressure."""

        class FakeCapabilities:
            def to_dict(self):
                return {
                    "os": "Linux",
                    "machine": "x86_64",
                    "python": "3.11",
                    "device_profile": "x86_64-cpu",
                    "runtimes": {
                        "onnxruntime": {
                            "available": True,
                            "providers": ["CPUExecutionProvider"],
                        }
                    },
                    "accelerators": {},
                    "memory": {"available_mb": 64.0, "total_mb": 2048.0},
                    "storage": {"available_mb": 128.0, "total_mb": 4096.0},
                }

        monkeypatch.setenv("TEMMS_DEMO_SEED_HUB", "1")
        monkeypatch.setattr(
            "temms.core.runtime_profiles.detect_runtime_capabilities",
            lambda: FakeCapabilities(),
        )
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            hub_state_path=temp_dir / "hub_lite.json",
            deployment_state_path=temp_dir / "deployment_state.json",
            hub_device_id="edge-sim",
            hub_device_profile="x86_64-cpu",
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

        inventory = daemon._hub_inventory()

        assert inventory["device_profile"] == "x86_64-cpu"
        assert inventory["runtimes"]["onnxruntime"]["available"] is True
        assert inventory["memory"]["available_mb"] == 4096.0
        assert inventory["memory"]["total_mb"] == 8192.0
        assert inventory["storage"]["available_mb"] == 24576.0
        assert inventory["storage"]["total_mb"] == 32768.0
        assert inventory["simulated"] is True
        assert inventory["source"] == "docker-demo-heartbeat"
        assert inventory["demo_inventory"]["resource_floor"] is True

    def test_emit_telemetry_writes_daemon_event(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test daemon audit telemetry writes to the local buffer."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            telemetry_path=temp_dir / "telemetry.jsonl",
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

        daemon._emit_telemetry("slot.model_switched", {"slot": "vision"})

        events = daemon.telemetry.read()
        assert events[0]["event_type"] == "slot.model_switched"
        assert events[0]["source"] == "daemon"

    def test_mirror_hub_snapshot_creates_local_assignments(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test online sync mirrors assigned central rollouts into local Hub Lite."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            hub_state_path=temp_dir / "hub_lite.json",
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

        mirrored = daemon._mirror_hub_snapshot(
            packages=[
                {
                    "package_id": "pkg-vision-1",
                    "name": "vision",
                    "version": "1",
                    "path": "/packages/pkg-vision-1.temms.tar.zst",
                    "device_profiles": ["x86_64-cpu"],
                    "metadata": {
                        "models": [
                            {
                                "id": "model-lowlight-1",
                                "runtime_constraints": {
                                    "device_profiles": ["x86_64-cpu"],
                                    "runtimes": ["onnxruntime"],
                                },
                            }
                        ]
                    },
                    "promotion": {
                        "schema_version": "temms-package-promotion/v1",
                        "package_id": "pkg-vision-1",
                        "state": "released",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "actor": "operator:release",
                        "reason": "released for rollout",
                        "history": [
                            {
                                "state": "released",
                                "from_state": "approved",
                                "updated_at": "2026-01-01T00:00:00Z",
                                "actor": "operator:release",
                                "reason": "released for rollout",
                                "evidence": {},
                            }
                        ],
                    },
                }
            ],
            rollouts=[
                {
                    "rollout_id": "rollout-1",
                    "device_id": "edge-1",
                    "package_id": "pkg-vision-1",
                    "model_id": "model-lowlight-1",
                    "slot": "vision",
                    "runtime_target_id": "temms-x86_64-cpu",
                },
                {
                    "rollout_id": "rollout-other",
                    "device_id": "edge-2",
                    "package_id": "pkg-vision-1",
                    "slot": "vision",
                },
            ],
            device_id="edge-1",
            profile="x86_64-cpu",
            inventory={"python": "3.11"},
        )

        assert mirrored == {"packages": 1, "rollouts": 1}
        assert daemon.hub_lite.get_device("edge-1")["profile"] == "x86_64-cpu"
        assert daemon.hub_lite.get_package("pkg-vision-1")["name"] == "vision"
        rollout = daemon.hub_lite.get_rollout("rollout-1")
        assert rollout["state"] == "assigned"
        assert rollout["model_id"] == "model-lowlight-1"
        assert rollout["runtime_target_id"] == "temms-x86_64-cpu"
        assert daemon.hub_lite.get_rollout("rollout-other") is None

    def test_mirror_hub_snapshot_preserves_valid_local_artifact(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test repeated online sync preserves a verified local package artifact."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            hub_state_path=temp_dir / "hub_lite.json",
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
        cache_dir = temp_dir / "packages"
        cache_dir.mkdir()
        cached_path = cache_dir / "pkg-vision-1.temms.tar.zst"
        cached_path.write_bytes(b"cached-package")

        daemon.hub_lite.upsert_package(
            {
                "package_id": "pkg-vision-1",
                "name": "vision",
                "version": "1",
                "path": str(cached_path),
                "sha256": hashlib.sha256(b"cached-package").hexdigest(),
                "source_sha256": "source-v1",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "online_artifact": {
                        "filename": cached_path.name,
                        "sha256": hashlib.sha256(b"cached-package").hexdigest(),
                        "source_sha256": "source-v1",
                    }
                },
            }
        )

        daemon._mirror_hub_snapshot(
            packages=[
                {
                    "package_id": "pkg-vision-1",
                    "name": "vision",
                    "version": "1",
                    "path": "/central/pkg-vision-1.temms",
                    "sha256": "source-v1",
                    "source_sha256": "source-v1",
                    "device_profiles": ["x86_64-cpu"],
                    "metadata": {"source": {"sha256": "source-v1"}},
                }
            ],
            rollouts=[],
            device_id="edge-1",
            profile="x86_64-cpu",
            inventory={},
        )

        package = daemon.hub_lite.get_package("pkg-vision-1")
        assert Path(package["path"]) == cached_path.resolve()
        assert package["sha256"] == hashlib.sha256(b"cached-package").hexdigest()
        assert package["source_sha256"] == "source-v1"
        assert package["metadata"]["online_artifact"]["source_sha256"] == "source-v1"

    def test_mirror_hub_snapshot_drops_cache_when_source_changes(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test changed central source digest replaces stale cached artifact metadata."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            hub_state_path=temp_dir / "hub_lite.json",
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
        cache_dir = temp_dir / "packages"
        cache_dir.mkdir()
        cached_path = cache_dir / "pkg-vision-1.temms.tar.zst"
        cached_path.write_bytes(b"cached-package")

        daemon.hub_lite.upsert_package(
            {
                "package_id": "pkg-vision-1",
                "name": "vision",
                "version": "1",
                "path": str(cached_path),
                "sha256": hashlib.sha256(b"cached-package").hexdigest(),
                "source_sha256": "source-v1",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "online_artifact": {
                        "filename": cached_path.name,
                        "sha256": hashlib.sha256(b"cached-package").hexdigest(),
                        "source_sha256": "source-v1",
                    }
                },
            }
        )

        daemon._mirror_hub_snapshot(
            packages=[
                {
                    "package_id": "pkg-vision-1",
                    "name": "vision",
                    "version": "2",
                    "path": "/central/pkg-vision-2.temms",
                    "sha256": "source-v2",
                    "source_sha256": "source-v2",
                    "device_profiles": ["x86_64-cpu"],
                    "metadata": {"source": {"sha256": "source-v2"}},
                }
            ],
            rollouts=[],
            device_id="edge-1",
            profile="x86_64-cpu",
            inventory={},
        )

        package = daemon.hub_lite.get_package("pkg-vision-1")
        assert package["path"] == "/central/pkg-vision-2.temms"
        assert package["sha256"] == "source-v2"
        assert package["source_sha256"] == "source-v2"
        assert "online_artifact" not in package["metadata"]


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

    async def test_load_policies_clears_removed_active_policies(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
        sample_policy_yaml,
    ):
        """Test policy reload treats the active policy directory as source of truth."""
        policy_dir = temp_dir / "policies"
        policy_dir.mkdir()

        import shutil

        active_policy = policy_dir / "test-policy.yaml"
        shutil.copy(sample_policy_yaml, active_policy)

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

        active_policy.unlink()
        await daemon._load_policies()

        assert len(policy_engine.list_policies()) == 0

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

    async def test_push_local_rollout_states_replays_missing_history(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test online sync pushes downloading/imported/activated transitions."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            hub_state_path=temp_dir / "hub_lite.json",
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
        daemon.hub_lite.enroll_device("edge-1", profile="x86_64-cpu")
        daemon.hub_lite.upsert_package(
            {
                "package_id": "pkg-vision-1",
                "name": "vision",
                "version": "1",
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(daemon.hub_lite, "pkg-vision-1")
        daemon.hub_lite.assign_rollout(
            "edge-1",
            "pkg-vision-1",
            slot="vision",
            rollout_id="rollout-1",
        )
        daemon.hub_lite.update_rollout_status(
            "rollout-1",
            "downloading",
            detail="using local package path",
            actor="edge:edge-1",
        )
        daemon.hub_lite.update_rollout_status(
            "rollout-1",
            "imported",
            detail="package imported",
            actor="edge:edge-1",
        )
        daemon.hub_lite.update_rollout_status(
            "rollout-1",
            "activated",
            detail="activated model-v1",
            actor="edge:edge-1",
        )
        calls = []

        class FakeResponse:
            def raise_for_status(self):
                return None

        class FakeClient:
            async def post(self, path, json=None):
                calls.append((path, json))
                return FakeResponse()

        await daemon._push_local_rollout_states(
            FakeClient(),
            central_rollouts=[
                {
                    "rollout_id": "rollout-1",
                    "device_id": "edge-1",
                    "package_id": "pkg-vision-1",
                    "slot": "vision",
                    "state": "assigned",
                    "history": [
                        {
                            "state": "assigned",
                            "detail": "assigned",
                            "actor": None,
                        }
                    ],
                }
            ],
            device_id="edge-1",
        )

        assert calls == [
            (
                "/rollouts/rollout-1/status",
                {
                    "state": "downloading",
                    "detail": "using local package path",
                    "actor": "edge:edge-1",
                },
            ),
            (
                "/rollouts/rollout-1/status",
                {
                    "state": "imported",
                    "detail": "package imported",
                    "actor": "edge:edge-1",
                },
            ),
            (
                "/rollouts/rollout-1/status",
                {
                    "state": "activated",
                    "detail": "activated model-v1",
                    "actor": "edge:edge-1",
                },
            ),
        ]

    async def test_policy_switch_audit_includes_policy_explanation(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test policy-driven switches carry rule evidence into audit logs."""
        old_bytes = b"old-model"
        new_bytes = b"new-model"
        old_path = temp_dir / "old.onnx"
        new_path = temp_dir / "new.onnx"
        old_path.write_bytes(old_bytes)
        new_path.write_bytes(new_bytes)
        model_cache.add_cached_model(
            model_id="old-model-v1",
            name="old-model",
            version="1",
            format=ModelFormat.ONNX,
            path=old_path,
            sha256=hashlib.sha256(old_bytes).hexdigest(),
            size_bytes=len(old_bytes),
            package_id="pkg-old",
        )
        model_cache.add_cached_model(
            model_id="fog-model-v1",
            name="fog-model",
            version="1",
            format=ModelFormat.ONNX,
            path=new_path,
            sha256=hashlib.sha256(new_bytes).hexdigest(),
            size_bytes=len(new_bytes),
            package_id="pkg-fog",
            metadata={"provenance": {"run_id": "run-fog"}},
        )
        slot_manager.create_slot(
            "vision",
            "Vision slot",
            candidates=["old-model", "fog-model"],
        )
        slot_manager.activate_model("vision", "old-model-v1", "startup", "seed")
        condition_store.set(
            "environmental.atmospheric.visibility_m",
            40,
            "sensor",
            100,
            confidence=0.99,
        )
        policy_engine.load_policy(
            SlotPolicy(
                metadata=SlotPolicyMetadata(name="weather-policy"),
                spec=SlotPolicySpec(
                    slot="vision",
                    rules=[
                        PolicyRule(
                            name="fog-rule",
                            priority=90,
                            conditions=ConditionGroup(
                                all=[
                                    Condition(
                                        metric="environmental.atmospheric.visibility_m",
                                        operator="lt",
                                        value=100,
                                        min_confidence=0.9,
                                    )
                                ]
                            ),
                            action=PolicyAction(switch_to="fog-model"),
                        )
                    ],
                ),
            )
        )
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            telemetry_path=temp_dir / "telemetry.jsonl",
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
        daemon.inference_runtime.load_model = AsyncMock(return_value=True)

        await daemon._evaluate_all_slots()

        daemon.inference_runtime.load_model.assert_awaited_once_with("vision", "fog-model-v1")
        decision = slot_manager.get_decision_log("vision", limit=1)[0]
        audit = json.loads(decision["audit_metadata"])
        explanation = audit["policy_evaluation"]
        assert explanation["reason"] == "rule_matched"
        assert explanation["matched_rule"]["policy"] == "weather-policy"
        assert explanation["matched_rule"]["rule"] == "fog-rule"
        condition = explanation["matched_rule"]["conditions"]["items"][0]
        assert condition["actual"] == 40
        assert condition["source"] == "sensor"
        assert condition["matched"] is True
        event = daemon.telemetry.read()[0]
        assert event["payload"]["policy_evaluation"]["matched_rule"]["rule"] == "fog-rule"

    async def test_policy_switch_preflight_blocks_resource_unsafe_model_and_uses_fallback(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test policy hot-swap refuses resource-unsafe models before load."""
        for model_id, model_name in [
            ("daylight-model-v1", "daylight-model"),
            ("heavy-model-v1", "heavy-model"),
            ("safe-model-v1", "safe-model"),
        ]:
            model_bytes = f"{model_id}-bytes".encode()
            model_path = temp_dir / f"{model_id}.onnx"
            model_path.write_bytes(model_bytes)
            model_cache.add_cached_model(
                model_id=model_id,
                name=model_name,
                version="1",
                format=ModelFormat.ONNX,
                path=model_path,
                sha256=hashlib.sha256(model_bytes).hexdigest(),
                size_bytes=len(model_bytes),
                package_id="pkg-adaptive",
            )

        slot_manager.create_slot(
            "vision",
            "Vision slot",
            candidates=["daylight-model", "heavy-model", "safe-model"],
        )
        slot_manager.activate_model("vision", "daylight-model-v1", "startup", "seed")
        condition_store.set("mission.mode", "survey", "operator", 100)
        policy_engine.load_policy(
            SlotPolicy(
                metadata=SlotPolicyMetadata(name="adaptive-resource-policy"),
                spec=SlotPolicySpec(
                    slot="vision",
                    rules=[
                        PolicyRule(
                            name="survey-heavy-rule",
                            priority=100,
                            conditions=ConditionGroup(
                                all=[
                                    Condition(
                                        metric="mission.mode",
                                        operator="eq",
                                        value="survey",
                                    )
                                ]
                            ),
                            action=PolicyAction(switch_to="heavy-model"),
                        )
                    ],
                    fallback_chain=["safe-model", "daylight-model"],
                ),
            )
        )
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            telemetry_path=temp_dir / "telemetry.jsonl",
            hub_state_path=temp_dir / "hub_lite.json",
            hub_device_id="edge-1",
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
        daemon.hub_lite.enroll_device(
            "edge-1",
            profile="x86_64-cpu",
            inventory={
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CPUExecutionProvider"],
                    }
                },
                "memory": {"available_mb": 256.0},
                "storage": {"available_mb": 2048.0},
            },
        )
        daemon.hub_lite.upsert_package(
            {
                "package_id": "pkg-adaptive",
                "name": "adaptive-models",
                "version": "1",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "validation": {
                        "signature_verified": True,
                        "strict_metadata": True,
                    },
                    "models": [
                        {
                            "id": "heavy-model-v1",
                            "runtime_constraints": {"runtimes": ["onnxruntime"]},
                            "resource_requirements": {
                                "min_memory_available_mb": 1024.0,
                                "min_storage_available_mb": 128.0,
                            },
                        },
                        {
                            "id": "safe-model-v1",
                            "runtime_constraints": {"runtimes": ["onnxruntime"]},
                            "resource_requirements": {
                                "min_memory_available_mb": 128.0,
                                "min_storage_available_mb": 64.0,
                            },
                        },
                        {
                            "id": "daylight-model-v1",
                            "runtime_constraints": {"runtimes": ["onnxruntime"]},
                            "resource_requirements": {
                                "min_memory_available_mb": 128.0,
                                "min_storage_available_mb": 64.0,
                            },
                        },
                    ],
                },
            }
        )
        _release_package(daemon.hub_lite, "pkg-adaptive")
        daemon.inference_runtime.load_model = AsyncMock(return_value=True)

        await daemon._evaluate_all_slots()

        daemon.inference_runtime.load_model.assert_awaited_once_with(
            "vision",
            "safe-model-v1",
        )
        assert slot_manager.get_slot("vision").active_model_id == "safe-model-v1"
        events = daemon.telemetry.read()
        preflight_event = next(
            event for event in events if event["event_type"] == "slot.activation_preflight_blocked"
        )
        assert preflight_event["payload"]["model_id"] == "heavy-model-v1"
        assert preflight_event["payload"]["blocking_gates"][0]["gate_id"] == (
            "resource_envelope"
        )
        fallback_event = next(event for event in events if event["event_type"] == "slot.fallback")
        assert fallback_event["payload"]["model_id"] == "safe-model-v1"
        decision = slot_manager.get_decision_log("vision", limit=1)[0]
        audit = json.loads(decision["audit_metadata"])
        assert audit["activation_preflight"]["status"] in {"go", "attention"}
        assert audit["activation_preflight"]["selection"]["device_id"] == "edge-1"
        assert audit["fallback"]["selected_model"] == "heavy-model-v1"

    async def test_auto_start_preflight_blocks_resource_unsafe_default_model(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test daemon startup refuses unsafe default models before loading."""
        model_bytes = b"heavy-default-model"
        model_path = temp_dir / "heavy-default.onnx"
        model_path.write_bytes(model_bytes)
        model_cache.add_cached_model(
            model_id="heavy-default-v1",
            name="heavy-default",
            version="1",
            format=ModelFormat.ONNX,
            path=model_path,
            sha256=hashlib.sha256(model_bytes).hexdigest(),
            size_bytes=len(model_bytes),
            package_id="pkg-startup",
        )
        slot_manager.create_slot(
            "vision",
            "Vision slot",
            default_model="heavy-default",
            candidates=["heavy-default"],
        )
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            telemetry_path=temp_dir / "telemetry.jsonl",
            hub_state_path=temp_dir / "hub_lite_startup.json",
            hub_device_id="edge-1",
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
        daemon.hub_lite.enroll_device(
            "edge-1",
            profile="x86_64-cpu",
            inventory={
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CPUExecutionProvider"],
                    }
                },
                "memory": {"available_mb": 256.0},
                "storage": {"available_mb": 2048.0},
            },
        )
        daemon.hub_lite.upsert_package(
            {
                "package_id": "pkg-startup",
                "name": "startup-models",
                "version": "1",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "validation": {
                        "signature_verified": True,
                        "strict_metadata": True,
                    },
                    "models": [
                        {
                            "id": "heavy-default-v1",
                            "runtime_constraints": {"runtimes": ["onnxruntime"]},
                            "resource_requirements": {
                                "min_memory_available_mb": 1024.0,
                                "min_storage_available_mb": 128.0,
                            },
                        }
                    ],
                },
            }
        )
        _release_package(daemon.hub_lite, "pkg-startup")
        daemon.inference_runtime.load_model = AsyncMock(return_value=True)

        await daemon._auto_start_slots()

        daemon.inference_runtime.load_model.assert_not_awaited()
        slot = slot_manager.get_slot("vision")
        assert slot is not None
        assert slot.state == SlotState.STOPPED
        assert slot.active_model_id is None
        assert slot_manager.get_decision_log("vision", limit=1) == []
        events = daemon.telemetry.read()
        preflight_event = next(
            event for event in events if event["event_type"] == "slot.activation_preflight_blocked"
        )
        assert preflight_event["payload"]["trigger_type"] == "startup"
        assert preflight_event["payload"]["model_id"] == "heavy-default-v1"
        assert preflight_event["payload"]["blocking_gates"][0]["gate_id"] == (
            "resource_envelope"
        )
        startup_failed = next(
            event for event in events if event["event_type"] == "slot.startup_failed"
        )
        assert startup_failed["payload"]["failure_kind"] == "readiness_preflight"
        assert startup_failed["payload"]["blocking_gates"][0]["gate_id"] == (
            "resource_envelope"
        )

    async def test_auto_start_records_activation_preflight_for_safe_default_model(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test startup activation evidence includes local edge admission proof."""
        model_bytes = b"safe-default-model"
        model_path = temp_dir / "safe-default.onnx"
        model_path.write_bytes(model_bytes)
        model_cache.add_cached_model(
            model_id="safe-default-v1",
            name="safe-default",
            version="1",
            format=ModelFormat.ONNX,
            path=model_path,
            sha256=hashlib.sha256(model_bytes).hexdigest(),
            size_bytes=len(model_bytes),
            package_id="pkg-startup-safe",
        )
        slot_manager.create_slot(
            "vision",
            "Vision slot",
            default_model="safe-default",
            candidates=["safe-default"],
        )
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            telemetry_path=temp_dir / "telemetry.jsonl",
            hub_state_path=temp_dir / "hub_lite_startup_safe.json",
            hub_device_id="edge-1",
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
        daemon.hub_lite.enroll_device(
            "edge-1",
            profile="x86_64-cpu",
            inventory={
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CPUExecutionProvider"],
                    }
                },
                "memory": {"available_mb": 256.0},
                "storage": {"available_mb": 2048.0},
            },
        )
        daemon.hub_lite.upsert_package(
            {
                "package_id": "pkg-startup-safe",
                "name": "startup-safe-models",
                "version": "1",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "validation": {
                        "signature_verified": True,
                        "strict_metadata": True,
                    },
                    "models": [
                        {
                            "id": "safe-default-v1",
                            "runtime_constraints": {"runtimes": ["onnxruntime"]},
                            "resource_requirements": {
                                "min_memory_available_mb": 128.0,
                                "min_storage_available_mb": 64.0,
                            },
                        }
                    ],
                },
            }
        )
        _release_package(daemon.hub_lite, "pkg-startup-safe")
        daemon.inference_runtime.load_model = AsyncMock(return_value=True)

        await daemon._auto_start_slots()

        daemon.inference_runtime.load_model.assert_awaited_once_with(
            "vision",
            "safe-default-v1",
        )
        slot = slot_manager.get_slot("vision")
        assert slot is not None
        assert slot.state == SlotState.RUNNING
        assert slot.active_model_id == "safe-default-v1"
        decision = slot_manager.get_decision_log("vision", limit=1)[0]
        audit = json.loads(decision["audit_metadata"])
        assert audit["activation_preflight"]["selection"]["device_id"] == "edge-1"
        assert audit["activation_preflight"]["selection"]["model_id"] == "safe-default-v1"
        startup_event = next(
            event for event in daemon.telemetry.read() if event["event_type"] == "slot.startup"
        )
        assert startup_event["payload"]["model"]["activation_preflight"]["selection"][
            "model_id"
        ] == "safe-default-v1"

    async def test_condition_loop_triggers_policy_hot_swap_with_source_metadata(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test local collector input can drive policy selection and hot-swap."""

        class VisibilityCollector:
            source_name = "visibility_sensor"
            source_priority = 150

            def collect(self):
                return {"environmental.atmospheric.visibility_m": 35}

        for model_id, model_name in [
            ("daylight-model-v1", "daylight-model"),
            ("fog-model-v1", "fog-model"),
        ]:
            model_bytes = f"{model_id}-bytes".encode()
            model_path = temp_dir / f"{model_id}.onnx"
            model_path.write_bytes(model_bytes)
            model_cache.add_cached_model(
                model_id=model_id,
                name=model_name,
                version="1",
                format=ModelFormat.ONNX,
                path=model_path,
                sha256=hashlib.sha256(model_bytes).hexdigest(),
                size_bytes=len(model_bytes),
                package_id="pkg-collector-loop",
            )

        slot_manager.create_slot(
            "vision",
            "Vision slot",
            candidates=["daylight-model", "fog-model"],
        )
        slot_manager.activate_model("vision", "daylight-model-v1", "startup", "seed")
        policy_engine.load_policy(
            SlotPolicy(
                metadata=SlotPolicyMetadata(name="collector-weather-policy"),
                spec=SlotPolicySpec(
                    slot="vision",
                    rules=[
                        PolicyRule(
                            name="collector-fog-rule",
                            priority=80,
                            conditions=ConditionGroup(
                                all=[
                                    Condition(
                                        metric="environmental.atmospheric.visibility_m",
                                        operator="lt",
                                        value=100,
                                    )
                                ]
                            ),
                            action=PolicyAction(switch_to="fog-model"),
                        )
                    ],
                ),
            )
        )
        config = DaemonConfig(
            condition_interval_s=30,
            policy_interval_s=30,
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            telemetry_path=temp_dir / "telemetry.jsonl",
        )
        daemon = TEMMSDaemon(
            config=config,
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            collectors=[VisibilityCollector()],
        )
        daemon.inference_runtime.load_model = AsyncMock(return_value=True)
        daemon._running = True
        condition_task = asyncio.create_task(daemon._condition_loop())
        policy_task = asyncio.create_task(daemon._policy_loop())

        try:
            for _ in range(50):
                if slot_manager.get_slot("vision").active_model_id == "fog-model-v1":
                    break
                await asyncio.sleep(0.02)

            condition = condition_store.get("environmental.atmospheric.visibility_m")
            assert condition is not None
            assert condition.value == 35
            assert condition.source == "visibility_sensor"
            assert condition.priority == 150
            assert slot_manager.get_slot("vision").active_model_id == "fog-model-v1"
            daemon.inference_runtime.load_model.assert_awaited_with("vision", "fog-model-v1")
            decision = slot_manager.get_decision_log("vision", limit=1)[0]
            assert decision["trigger_type"] == "policy"
            assert decision["to_model"] == "fog-model-v1"
        finally:
            daemon._running = False
            daemon._shutdown_event.set()
            await asyncio.gather(condition_task, policy_task)

    async def test_policy_loop_fallback_audit_names_failed_selected_model(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test daemon-loop fallback evidence names the failed policy-selected model."""
        for model_id, model_name in [
            ("daylight-model-v1", "daylight-model"),
            ("faulty-model-v1", "faulty-model"),
            ("lowlight-model-v1", "lowlight-model"),
        ]:
            model_bytes = f"{model_id}-bytes".encode()
            model_path = temp_dir / f"{model_id}.onnx"
            model_path.write_bytes(model_bytes)
            model_cache.add_cached_model(
                model_id=model_id,
                name=model_name,
                version="1",
                format=ModelFormat.ONNX,
                path=model_path,
                sha256=hashlib.sha256(model_bytes).hexdigest(),
                size_bytes=len(model_bytes),
                package_id="pkg-adaptive",
            )

        slot_manager.create_slot(
            "vision",
            "Vision slot",
            candidates=["daylight-model", "faulty-model", "lowlight-model"],
        )
        slot_manager.activate_model("vision", "daylight-model-v1", "startup", "seed")
        condition_store.set("simulation.force_model_load_failure", True, "sensor", 100)
        policy_engine.load_policy(
            SlotPolicy(
                metadata=SlotPolicyMetadata(name="failure-policy"),
                spec=SlotPolicySpec(
                    slot="vision",
                    rules=[
                        PolicyRule(
                            name="load-failure-rule",
                            priority=100,
                            conditions=ConditionGroup(
                                all=[
                                    Condition(
                                        metric="simulation.force_model_load_failure",
                                        operator="eq",
                                        value=True,
                                    )
                                ]
                            ),
                            action=PolicyAction(switch_to="faulty-model"),
                        )
                    ],
                    fallback_chain=["lowlight-model", "daylight-model"],
                ),
            )
        )
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            telemetry_path=temp_dir / "telemetry.jsonl",
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

        async def load_model(slot_name, model_id):
            if model_id == "faulty-model-v1":
                raise RuntimeError("simulated load failure")
            return True

        daemon.inference_runtime.load_model = AsyncMock(side_effect=load_model)

        await daemon._evaluate_all_slots()

        assert slot_manager.get_slot("vision").active_model_id == "lowlight-model-v1"
        assert [call.args for call in daemon.inference_runtime.load_model.await_args_list] == [
            ("vision", "faulty-model-v1"),
            ("vision", "lowlight-model-v1"),
        ]
        decision = slot_manager.get_decision_log("vision", limit=1)[0]
        assert decision["trigger_type"] == "fallback"
        assert decision["to_model"] == "lowlight-model-v1"
        audit = json.loads(decision["audit_metadata"])
        assert audit["fallback"]["selected_model"] == "faulty-model-v1"
        assert audit["fallback"]["attempted"] == ["lowlight-model-v1"]
        assert audit["fallback"]["failures"][0].startswith("faulty-model-v1:")
        fallback_event = daemon.telemetry.read()[-1]
        assert fallback_event["event_type"] == "slot.fallback"
        assert fallback_event["payload"]["fallback"]["selected_model"] == "faulty-model-v1"

    async def test_collector_failure_drives_degraded_sensor_policy_switch(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test degraded collector health is policy-visible and can drive a switch."""

        class BrokenVisibilityCollector:
            source_name = "visibility_sensor"
            source_priority = 150

            def collect(self):
                raise RuntimeError("visibility sensor offline")

        for model_id, model_name in [
            ("daylight-model-v1", "daylight-model"),
            ("safe-model-v1", "safe-model"),
        ]:
            model_bytes = f"{model_id}-bytes".encode()
            model_path = temp_dir / f"{model_id}.onnx"
            model_path.write_bytes(model_bytes)
            model_cache.add_cached_model(
                model_id=model_id,
                name=model_name,
                version="1",
                format=ModelFormat.ONNX,
                path=model_path,
                sha256=hashlib.sha256(model_bytes).hexdigest(),
                size_bytes=len(model_bytes),
                package_id="pkg-degraded-sensor",
            )

        slot_manager.create_slot(
            "vision",
            "Vision slot",
            candidates=["daylight-model", "safe-model"],
        )
        slot_manager.activate_model("vision", "daylight-model-v1", "startup", "seed")
        policy_engine.load_policy(
            SlotPolicy(
                metadata=SlotPolicyMetadata(name="sensor-health-policy"),
                spec=SlotPolicySpec(
                    slot="vision",
                    rules=[
                        PolicyRule(
                            name="visibility-sensor-degraded",
                            priority=130,
                            conditions=ConditionGroup(
                                all=[
                                    Condition(
                                        metric="runtime.collectors.visibility_sensor.healthy",
                                        operator="eq",
                                        value=False,
                                    )
                                ]
                            ),
                            action=PolicyAction(switch_to="safe-model"),
                        )
                    ],
                ),
            )
        )
        config = DaemonConfig(
            condition_interval_s=30,
            policy_interval_s=30,
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            telemetry_path=temp_dir / "telemetry.jsonl",
        )
        daemon = TEMMSDaemon(
            config=config,
            slot_manager=slot_manager,
            condition_store=condition_store,
            policy_engine=policy_engine,
            model_cache=model_cache,
            model_storage=model_storage,
            collectors=[BrokenVisibilityCollector()],
        )
        daemon.inference_runtime.load_model = AsyncMock(return_value=True)
        daemon._running = True
        condition_task = asyncio.create_task(daemon._condition_loop())
        policy_task = asyncio.create_task(daemon._policy_loop())

        try:
            for _ in range(50):
                if slot_manager.get_slot("vision").active_model_id == "safe-model-v1":
                    break
                await asyncio.sleep(0.02)

            health = condition_store.get("runtime.collectors.visibility_sensor.healthy")
            error = condition_store.get("runtime.collectors.visibility_sensor.last_error")
            reported_count = condition_store.get(
                "runtime.collectors.visibility_sensor.reported_count"
            )
            assert health is not None
            assert health.value is False
            assert health.source == "visibility_sensor:health"
            assert health.priority == 900
            assert error is not None
            assert error.value == "visibility sensor offline"
            assert reported_count is not None
            assert reported_count.value == 0
            assert slot_manager.get_slot("vision").active_model_id == "safe-model-v1"
            daemon.inference_runtime.load_model.assert_awaited_with("vision", "safe-model-v1")
            decision = slot_manager.get_decision_log("vision", limit=1)[0]
            assert decision["trigger_type"] == "policy"
            assert decision["trigger_detail"] == ("sensor-health-policy/visibility-sensor-degraded")
            conditions_snapshot = json.loads(decision["conditions_snapshot"])
            assert (
                conditions_snapshot["runtime"]["collectors"]["visibility_sensor"]["healthy"]
                is False
            )
        finally:
            daemon._running = False
            daemon._shutdown_event.set()
            await asyncio.gather(condition_task, policy_task)

    async def test_auto_apply_requires_signing_key(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test auto-apply fails assigned rollout clearly without a signing key."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            hub_state_path=temp_dir / "hub_lite.json",
            telemetry_path=temp_dir / "telemetry.jsonl",
            hub_auto_apply=True,
            rollout_require_signature=True,
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
        daemon.hub_lite.enroll_device("edge-1", profile="x86_64-cpu")
        daemon.hub_lite.upsert_package(
            {
                "package_id": "pkg-vision-1",
                "name": "vision",
                "version": "1",
                "path": "/packages/pkg-vision-1.temms.tar.zst",
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(daemon.hub_lite, "pkg-vision-1")
        daemon.hub_lite.assign_rollout(
            "edge-1",
            "pkg-vision-1",
            slot="vision",
            rollout_id="rollout-1",
        )

        await daemon._auto_apply_assigned_rollouts()

        rollout = daemon.hub_lite.get_rollout("rollout-1")
        assert rollout["state"] == "failed"
        assert "requires TEMMS_PACKAGE_SIGNING_KEY" in rollout["history"][-1]["detail"]
        assert daemon.telemetry.read()[0]["event_type"] == "rollout.auto_apply_failed"

    async def test_auto_apply_records_structured_preflight_block(
        self,
        monkeypatch,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test auto-apply preserves assigned state and emits actionable preflight telemetry."""
        requests = []

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                self.base_url = kwargs.get("base_url")
                self.headers = kwargs.get("headers")

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, path, json):
                requests.append({"path": path, "json": json, "base_url": str(self.base_url)})
                return httpx.Response(
                    409,
                    json={
                        "detail": {
                            "message": "Rollout apply preflight failed",
                            "rollout_id": "rollout-1",
                            "package_id": "pkg-vision-1",
                            "model_id": "model-vision-1",
                            "runtime_target_id": "temms-x86_64-cpu",
                            "blocking_gates": [
                                {
                                    "gate_id": "performance_fit",
                                    "status": "attention",
                                    "state": "benchmark missing",
                                    "detail": "No benchmark evidence for declared performance SLO",
                                }
                            ],
                            "readiness": {
                                "status": "attention",
                                "selection": {
                                    "package_id": "pkg-vision-1",
                                    "model_id": "model-vision-1",
                                    "device_id": "edge-1",
                                    "runtime_target_id": "temms-x86_64-cpu",
                                    "slot": "vision",
                                },
                            },
                        }
                    },
                )

        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            hub_state_path=temp_dir / "hub_lite.json",
            telemetry_path=temp_dir / "telemetry.jsonl",
            hub_auto_apply=True,
            hub_device_id="edge-1",
            rollout_require_signature=False,
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
        daemon.hub_lite.enroll_device("edge-1", profile="x86_64-cpu")
        daemon.hub_lite.upsert_package(
            {
                "package_id": "pkg-vision-1",
                "name": "vision",
                "version": "1",
                "path": "/packages/pkg-vision-1.temms.tar.zst",
                "device_profiles": ["x86_64-cpu"],
                "metadata": {
                    "validation": {
                        "signature_verified": True,
                        "strict_metadata": True,
                    },
                    "models": [{"id": "model-vision-1"}],
                },
            }
        )
        _release_package(daemon.hub_lite, "pkg-vision-1")
        daemon.hub_lite.assign_rollout(
            "edge-1",
            "pkg-vision-1",
            slot="vision",
            rollout_id="rollout-1",
            runtime_target_id="temms-x86_64-cpu",
            model_id="model-vision-1",
        )

        await daemon._auto_apply_assigned_rollouts()

        assert requests == [
            {
                "path": "/rollouts/rollout-1/apply",
                "json": {
                    "require_signature": False,
                    "signing_key": None,
                    "actor": "edge:edge-1",
                },
                "base_url": "http://127.0.0.1:8080/v1/hub",
            }
        ]
        assert daemon.hub_lite.get_rollout("rollout-1")["state"] == "assigned"
        event = daemon.telemetry.read()[0]
        assert event["event_type"] == "rollout.auto_apply_failed"
        assert event["payload"]["failure_kind"] == "readiness_preflight"
        assert event["payload"]["message"] == "Rollout apply preflight failed"
        assert event["payload"]["blocking_gate_count"] == 1
        assert event["payload"]["blocking_gates"][0]["gate_id"] == "performance_fit"
        assert event["payload"]["readiness_selection"]["device_id"] == "edge-1"

    async def test_fetch_assigned_package_artifact_rewrites_local_path(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test online sync caches package artifacts from Hub before apply."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            hub_state_path=temp_dir / "hub_lite.json",
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
        artifact_bytes = b"online-package-archive"
        artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
        source_sha = "central-source-sha"
        daemon.hub_lite.enroll_device("edge-1", profile="x86_64-cpu")
        daemon.hub_lite.upsert_package(
            {
                "package_id": "pkg-vision-1",
                "name": "vision",
                "version": "1",
                "path": "/central-only/pkg-vision-1.temms.tar.zst",
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(daemon.hub_lite, "pkg-vision-1")
        daemon.hub_lite.assign_rollout(
            "edge-1",
            "pkg-vision-1",
            slot="vision",
            rollout_id="rollout-1",
        )

        class FakeClient:
            async def get(self, path):
                assert path == "/packages/pkg-vision-1/artifact"
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", "http://hub/packages/pkg-vision-1/artifact"),
                    content=artifact_bytes,
                    headers={
                        "x-temms-package-filename": "pkg-vision-1.temms.tar.zst",
                        "x-temms-package-sha256": artifact_sha,
                        "x-temms-package-source-sha256": source_sha,
                    },
                )

        downloaded = await daemon._fetch_assigned_package_artifacts(
            FakeClient(),
            rollouts=[
                {
                    "rollout_id": "rollout-1",
                    "device_id": "edge-1",
                    "package_id": "pkg-vision-1",
                    "slot": "vision",
                    "state": "assigned",
                }
            ],
            device_id="edge-1",
        )

        package = daemon.hub_lite.get_package("pkg-vision-1")
        assert downloaded == 1
        assert Path(package["path"]).exists()
        assert Path(package["path"]).read_bytes() == artifact_bytes
        assert package["sha256"] == artifact_sha
        assert package["source_sha256"] == source_sha
        assert package["metadata"]["online_artifact"]["sha256"] == artifact_sha
        assert package["metadata"]["online_artifact"]["source_sha256"] == source_sha

    async def test_fetch_assigned_package_artifact_refreshes_corrupt_cache(
        self,
        slot_manager,
        condition_store,
        policy_engine,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test online sync re-downloads a cached artifact with the wrong digest."""
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
            hub_state_path=temp_dir / "hub_lite.json",
            telemetry_path=temp_dir / "telemetry.jsonl",
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
        artifact_bytes = b"fresh-package-archive"
        artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
        cache_dir = temp_dir / "packages"
        cache_dir.mkdir()
        cached_path = cache_dir / "pkg-vision-1.temms.tar.zst"
        cached_path.write_bytes(b"corrupt-package-archive")

        daemon.hub_lite.enroll_device("edge-1", profile="x86_64-cpu")
        daemon.hub_lite.upsert_package(
            {
                "package_id": "pkg-vision-1",
                "name": "vision",
                "version": "1",
                "path": str(cached_path),
                "sha256": artifact_sha,
                "device_profiles": ["x86_64-cpu"],
            }
        )
        _release_package(daemon.hub_lite, "pkg-vision-1")
        daemon.hub_lite.assign_rollout(
            "edge-1",
            "pkg-vision-1",
            slot="vision",
            rollout_id="rollout-1",
        )

        class FakeClient:
            async def get(self, path):
                assert path == "/packages/pkg-vision-1/artifact"
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", "http://hub/packages/pkg-vision-1/artifact"),
                    content=artifact_bytes,
                    headers={
                        "x-temms-package-filename": "pkg-vision-1.temms.tar.zst",
                        "x-temms-package-sha256": artifact_sha,
                    },
                )

        downloaded = await daemon._fetch_assigned_package_artifacts(
            FakeClient(),
            rollouts=[
                {
                    "rollout_id": "rollout-1",
                    "device_id": "edge-1",
                    "package_id": "pkg-vision-1",
                    "slot": "vision",
                    "state": "assigned",
                }
            ],
            device_id="edge-1",
        )

        package = daemon.hub_lite.get_package("pkg-vision-1")
        assert downloaded == 1
        assert cached_path.read_bytes() == artifact_bytes
        assert package["sha256"] == artifact_sha
        assert daemon.telemetry.read()[0]["event_type"] == "hub.package_cache_mismatch"

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
