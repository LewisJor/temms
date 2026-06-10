"""
CLI test suite (#12).

Uses typer.testing.CliRunner to test CLI commands without
actually running as a subprocess.

Tests:
- temms version
- temms init
- temms status
- temms slot (create, list, status, set, decisions)
- temms condition (set, get, list, snapshot, clear-overrides)
- temms policy (load, list)
"""

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from temms.cli.main import app
from temms.core.config import Config, DatabaseConfig, PolicyConfig, StorageConfig

runner = CliRunner()


@pytest.fixture
def temms_env(temp_dir):
    """
    Create a minimal TEMMS environment:
    - config file
    - data directory with subdirs
    - empty DB
    """
    data_dir = temp_dir / "data"
    data_dir.mkdir()
    (data_dir / "models").mkdir()
    (data_dir / "cache").mkdir()
    (data_dir / "packages").mkdir()

    config_dir = temp_dir / "etc"
    config_dir.mkdir()
    (config_dir / "policies").mkdir()

    config_path = config_dir / "temms.yaml"
    config = Config(
        database=DatabaseConfig(path=data_dir / "temms.db"),
        storage=StorageConfig(
            model_dir=data_dir / "models",
            cache_dir=data_dir / "cache",
        ),
        policy=PolicyConfig(policy_dir=config_dir / "policies"),
    )
    config.save(config_path)

    return {
        "config_path": config_path,
        "data_dir": data_dir,
        "config_dir": config_dir,
    }


# ── version ──────────────────────────────────────────────────────────


class TestVersionCommand:
    """Test 'temms version' command."""

    def test_version_output(self):
        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0
        assert "TEMMS" in result.output


# ── init ─────────────────────────────────────────────────────────────


class TestInitCommand:
    """Test 'temms init' command."""

    def test_init_creates_directories(self, temp_dir):
        data_dir = temp_dir / "init_data"
        config_path = temp_dir / "init_config" / "temms.yaml"

        result = runner.invoke(
            app,
            [
                "init",
                "--config", str(config_path),
                "--data-dir", str(data_dir),
            ],
        )

        assert result.exit_code == 0
        assert data_dir.exists()
        assert (data_dir / "models").exists()
        assert (data_dir / "cache").exists()
        assert config_path.exists()


# ── status ───────────────────────────────────────────────────────────


class TestStatusCommand:
    """Test 'temms status' command."""

    def test_status_not_initialized(self, temp_dir):
        result = runner.invoke(
            app,
            ["status", "--config", str(temp_dir / "nonexistent.yaml")],
        )

        assert result.exit_code == 1
        assert "not initialized" in result.output.lower()

    def test_status_initialized(self, temms_env):
        result = runner.invoke(
            app,
            ["status", "--config", str(temms_env["config_path"])],
        )

        assert result.exit_code == 0
        assert "Cached models" in result.output


# ── package signing ─────────────────────────────────────────────────


class TestPackageSigningCommands:
    """Test package signing helper commands."""

    def test_keygen_sign_and_import_verified_package(
        self,
        temp_dir,
        model_cache,
        model_storage,
    ):
        pytest.importorskip("cryptography")

        package_dir = temp_dir / "signed-package"
        models_dir = package_dir / "models"
        models_dir.mkdir(parents=True)
        model_file = models_dir / "detector.onnx"
        model_file.write_bytes(b"fake model bytes")

        import hashlib
        sha256 = hashlib.sha256(model_file.read_bytes()).hexdigest()
        manifest = {
            "schema_version": "v1",
            "package_id": "signed-package",
            "name": "detector",
            "version": "1.0.0",
            "created_at": "2026-06-09T00:00:00Z",
            "models": [{
                "id": "detector-v1",
                "name": "detector",
                "version": "1.0.0",
                "format": "onnx",
                "filename": model_file.name,
                "sha256": sha256,
                "size_bytes": model_file.stat().st_size,
                "metadata": {},
            }],
            "policies": [],
            "validation": {
                "sim_passed": True,
                "tests_passed": True,
            },
        }
        (package_dir / "manifest.json").write_text(json.dumps(manifest))

        private_key = temp_dir / "builder.key"
        trusted_keys = temp_dir / "trusted-keys.json"
        keygen_result = runner.invoke(
            app,
            [
                "package", "keygen",
                "--key-id", "builder-key",
                "--private-key", str(private_key),
                "--trusted-keys", str(trusted_keys),
            ],
        )

        assert keygen_result.exit_code == 0
        assert private_key.exists()
        assert json.loads(trusted_keys.read_text())["builder-key"]

        sign_result = runner.invoke(
            app,
            [
                "package", "sign",
                str(package_dir),
                "--key-id", "builder-key",
                "--private-key", str(private_key),
            ],
        )

        assert sign_result.exit_code == 0
        signed_manifest = json.loads((package_dir / "manifest.json").read_text())
        assert signed_manifest["signature"]["key_id"] == "builder-key"

        from temms.core.package import PackageImporter
        importer = PackageImporter(temp_dir / "cache", model_cache, model_storage)
        with patch.dict(
            "os.environ",
            {"TEMMS_TRUSTED_SIGNATURE_KEYS_FILE": str(trusted_keys)},
            clear=False,
        ):
            result = importer.import_package(package_dir, verify=True)

        validation = result.manifest.validation
        assert validation["signature_verified"] is True
        assert validation["sim_passed"] is True
        assert validation["tests_passed"] is True
        assert validation["hash_verified"] is True


# ── slot create ──────────────────────────────────────────────────────


class TestSlotCreateCommand:
    """Test 'temms slot create' command."""

    def test_create_slot(self, temms_env):
        result = runner.invoke(
            app,
            [
                "slot", "create", "vision",
                "--description", "Vision slot",
                "--required",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "vision" in result.output.lower()

    def test_create_slot_with_candidates(self, temms_env):
        result = runner.invoke(
            app,
            [
                "slot", "create", "nav",
                "--description", "Navigation",
                "--candidates", "model-a,model-b",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0


# ── slot list ────────────────────────────────────────────────────────


class TestSlotListCommand:
    """Test 'temms slot list' command."""

    def test_list_no_slots(self, temms_env):
        result = runner.invoke(
            app,
            ["slot", "list", "--config", str(temms_env["config_path"])],
        )

        assert result.exit_code == 0
        assert "no slots" in result.output.lower()

    def test_list_with_slots(self, temms_env):
        # Create a slot first
        runner.invoke(
            app,
            [
                "slot", "create", "vision",
                "--description", "Vision",
                "--config", str(temms_env["config_path"]),
            ],
        )

        result = runner.invoke(
            app,
            ["slot", "list", "--config", str(temms_env["config_path"])],
        )

        assert result.exit_code == 0
        assert "vision" in result.output.lower()


# ── slot status ──────────────────────────────────────────────────────


class TestSlotStatusCommand:
    """Test 'temms slot status' command."""

    def test_status_not_found(self, temms_env):
        result = runner.invoke(
            app,
            [
                "slot", "status", "nonexistent",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_status_found(self, temms_env):
        runner.invoke(
            app,
            [
                "slot", "create", "vision",
                "--description", "Vision processing",
                "--config", str(temms_env["config_path"]),
            ],
        )

        result = runner.invoke(
            app,
            [
                "slot", "status", "vision",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "vision" in result.output.lower()
        assert "stopped" in result.output.lower()


# ── slot decisions ───────────────────────────────────────────────────


class TestSlotDecisionsCommand:
    """Test 'temms slot decisions' command."""

    def test_no_decisions(self, temms_env):
        result = runner.invoke(
            app,
            [
                "slot", "decisions",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "no decisions" in result.output.lower()


# ── condition set ────────────────────────────────────────────────────


class TestConditionSetCommand:
    """Test 'temms condition set' command."""

    def test_set_condition_json_value(self, temms_env):
        result = runner.invoke(
            app,
            [
                "condition", "set",
                "platform.compute.cpu_temp_c", "72.5",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "condition set" in result.output.lower()

    def test_set_condition_string_value(self, temms_env):
        result = runner.invoke(
            app,
            [
                "condition", "set",
                "weather.precipitation", "fog",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0


# ── condition get ────────────────────────────────────────────────────


class TestConditionGetCommand:
    """Test 'temms condition get' command."""

    def test_get_missing(self, temms_env):
        result = runner.invoke(
            app,
            [
                "condition", "get", "nonexistent",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_get_existing(self, temms_env):
        # Set condition first
        runner.invoke(
            app,
            [
                "condition", "set",
                "temp", "72.5",
                "--config", str(temms_env["config_path"]),
            ],
        )

        result = runner.invoke(
            app,
            [
                "condition", "get", "temp",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "72.5" in result.output


# ── condition list ───────────────────────────────────────────────────


class TestConditionListCommand:
    """Test 'temms condition list' command."""

    def test_list_empty(self, temms_env):
        result = runner.invoke(
            app,
            [
                "condition", "list",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "no conditions" in result.output.lower()

    def test_list_with_conditions(self, temms_env):
        runner.invoke(
            app,
            [
                "condition", "set", "temp", "72",
                "--config", str(temms_env["config_path"]),
            ],
        )

        result = runner.invoke(
            app,
            [
                "condition", "list",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "temp" in result.output


# ── condition snapshot ───────────────────────────────────────────────


class TestConditionSnapshotCommand:
    """Test 'temms condition snapshot' command."""

    def test_snapshot_empty(self, temms_env):
        result = runner.invoke(
            app,
            [
                "condition", "snapshot",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "Snapshot" in result.output

    def test_snapshot_with_data(self, temms_env):
        runner.invoke(
            app,
            [
                "condition", "set", "platform.cpu.temp", "60",
                "--config", str(temms_env["config_path"]),
            ],
        )

        result = runner.invoke(
            app,
            [
                "condition", "snapshot",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "platform" in result.output


# ── condition clear-overrides ────────────────────────────────────────


class TestConditionClearOverridesCommand:
    """Test 'temms condition clear-overrides' command."""

    def test_clear_overrides(self, temms_env):
        result = runner.invoke(
            app,
            [
                "condition", "clear-overrides",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "cleared" in result.output.lower()


# ── policy load ──────────────────────────────────────────────────────


class TestPolicyLoadCommand:
    """Test 'temms policy load' command."""

    def test_load_policy(self, temms_env, sample_policy_yaml):
        result = runner.invoke(
            app,
            [
                "policy", "load", str(sample_policy_yaml),
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "thermal-adaptive" in result.output

    def test_load_policy_file_not_found(self, temms_env):
        result = runner.invoke(
            app,
            [
                "policy", "load", "/tmp/nonexistent-policy.yaml",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_load_policy_no_file_arg(self, temms_env):
        result = runner.invoke(
            app,
            [
                "policy", "load",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 1


# ── policy list ──────────────────────────────────────────────────────


class TestPolicyListCommand:
    """Test 'temms policy list' command."""

    def test_list_no_policies(self, temms_env):
        result = runner.invoke(
            app,
            [
                "policy", "list",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "no policies" in result.output.lower()

    def test_list_with_policy(self, temms_env, sample_policy_yaml):
        import shutil

        # Copy policy to the policies directory
        policies_dir = temms_env["config_dir"] / "policies"
        shutil.copy(sample_policy_yaml, policies_dir / "test.yaml")

        result = runner.invoke(
            app,
            [
                "policy", "list",
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "thermal-adaptive" in result.output
