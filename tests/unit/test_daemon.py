"""
Unit tests for the TEMMS daemon service.
"""

import hashlib
import json
import pytest
import httpx
from unittest.mock import Mock, patch, AsyncMock
from pathlib import Path

from temms.core.cache import ModelFormat
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
        monkeypatch.setenv("TEMMS_HUB_AUTO_APPLY", "true")
        monkeypatch.setenv("TEMMS_ROLLOUT_REQUIRE_SIGNATURE", "false")
        monkeypatch.setenv("TEMMS_PACKAGE_SIGNING_KEY", "package-secret")

        config = DaemonConfig()

        assert config.hub_url == "http://hub.example:8080"
        assert config.hub_token == "hub-token"
        assert config.hub_device_id == "edge-1"
        assert config.hub_device_profile == "x86_64-cpu"
        assert config.hub_sync_interval_s == 2.5
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
                }

        monkeypatch.setattr(
            "temms.core.runtime_profiles.detect_runtime_capabilities",
            lambda: FakeCapabilities(),
        )
        config = DaemonConfig(
            db_path=temp_dir / "temms.db",
            model_dir=temp_dir / "models",
            policy_dir=temp_dir / "policies",
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
                }
            ],
            rollouts=[
                {
                    "rollout_id": "rollout-1",
                    "device_id": "edge-1",
                    "package_id": "pkg-vision-1",
                    "slot": "vision",
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
        assert daemon.hub_lite.get_rollout("rollout-1")["state"] == "assigned"
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
