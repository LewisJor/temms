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

import pytest
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from temms.cli.main import app
from temms.core.cache import ModelCache, ModelFormat
from temms.core.config import Config, DatabaseConfig, StorageConfig, PolicyConfig

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

        assert result.exit_code == 0, result.output
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
                "--config",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
        )

        assert result.exit_code == 0, result.output
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


# ── evidence ────────────────────────────────────────────────────────


class TestEvidenceCommand:
    """Test 'temms evidence' command."""

    def test_evidence_exports_local_bundle(self, temms_env):
        from temms.conditions.store import ConditionStore
        from temms.slots.manager import SlotManager

        slot_manager = SlotManager(temms_env["data_dir"] / "temms.db")
        condition_store = ConditionStore(temms_env["data_dir"] / "temms.db")
        slot_manager.create_slot(
            name="vision",
            description="Vision",
            required=True,
        )
        condition_store.set(
            path="platform.battery.percent",
            value=18,
            source="operator",
            priority=1000,
        )
        slot_manager.activate_model(
            slot_name="vision",
            model_id="tiny-v1",
            trigger_type="policy",
            trigger_detail="battery-adaptive/low-power",
            conditions=condition_store.get_snapshot(),
        )

        output = temms_env["data_dir"] / "evidence.json"
        result = runner.invoke(
            app,
            [
                "evidence",
                "--slot", "vision",
                "--output", str(output),
                "--config", str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        bundle = json.loads(output.read_text())
        assert bundle["schema_version"] == "temms-evidence-bundle/v1"
        assert bundle["decisions"][0]["to_model"] == "tiny-v1"
        assert (
            bundle["decisions"][0]["conditions_snapshot"]["platform"]["battery"]["percent"]
            == 18
        )


# ── daemon ───────────────────────────────────────────────────────────


class TestDaemonCommand:
    """Test 'temms daemon' command."""

    def test_daemon_start_foreground_uses_env_host_port(self, temms_env, monkeypatch):
        """Test omitted daemon bind flags defer to environment defaults."""
        monkeypatch.setenv("TEMMS_HOST", "127.0.0.1")
        monkeypatch.setenv("TEMMS_PORT", "18080")

        class FakeDaemon:
            async def start(self):
                return None

        with patch("temms.daemon.service.TEMMSDaemon.from_config") as from_config:
            from_config.return_value = FakeDaemon()

            result = runner.invoke(
                app,
                [
                    "daemon",
                    "start",
                    "--foreground",
                    "--config",
                    str(temms_env["config_path"]),
                ],
            )

        assert result.exit_code == 0, result.output
        daemon_config = from_config.call_args.args[0]
        assert daemon_config.inference_host == "127.0.0.1"
        assert daemon_config.inference_port == 18080
        assert "Host: 127.0.0.1" in result.output
        assert "Port: 18080" in result.output


class TestDoctorCommand:
    """Test 'temms doctor' command."""

    def test_doctor_reports_system_and_cache(self, temms_env):
        result = runner.invoke(
            app,
            ["doctor", "--config", str(temms_env["config_path"])],
        )

        assert result.exit_code == 0
        assert "TEMMS Doctor" in result.output
        assert "Known MVP Device Profiles" in result.output
        assert "rpi5-tflite" in result.output
        assert "Runtimes and Accelerators" in result.output
        assert "Model cache" in result.output

    def test_doctor_json_reports_machine_readable_health(self, temms_env):
        result = runner.invoke(
            app,
            ["doctor", "--config", str(temms_env["config_path"]), "--json"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["schema_version"] == "temms-doctor/v1"
        assert payload["config"]["found"] is True
        assert payload["path_strategy"] == {
            "source": "config",
            "non_root_fallback": False,
            "data_dir": str(temms_env["data_dir"]),
        }
        assert payload["system"]["machine"]
        assert payload["system"]["arch"]
        assert payload["system"]["python"]
        assert "onnxruntime" in payload["runtimes"]
        assert "tflite" in payload["runtimes"]
        assert "tflite_runtime" in payload["runtimes"]
        assert "tensorrt" in payload["runtimes"]
        assert "nvidia" in payload["accelerators"]
        assert "rpi5-tflite" in payload["known_device_profiles"]
        assert {path["name"] for path in payload["paths"]} == {
            "database_dir",
            "model_dir",
            "cache_dir",
            "package_dir",
            "policy_dir",
        }
        assert all(path["writable"] is True for path in payload["paths"])
        assert all(path["write_probe"]["ok"] is True for path in payload["paths"])
        assert all(path["write_probe"]["attempted"] is True for path in payload["paths"])
        assert payload["port"]["status"] in {"free", "in use"}
        assert payload["port"]["name"] == "api"
        assert {port["name"] for port in payload["ports"]} == {"api", "grpc"}
        assert {port["status"] for port in payload["ports"]} <= {"free", "in use"}
        assert payload["ports"][0] == payload["port"]
        assert payload["security"]["rollout_require_signature"] is True
        assert "api_token_configured" in payload["security"]
        assert "signing_key_configured" in payload["security"]
        assert payload["model_cache"]["models"] == 0
        assert payload["model_cache"]["packages"] == 0
        assert payload["model_cache"]["health"] == {
            "status": "healthy",
            "checked_models": 0,
            "issues": [],
        }

    def test_doctor_json_reports_model_cache_integrity_issues(self, temms_env):
        model_path = temms_env["data_dir"] / "models" / "corrupt.onnx"
        model_path.write_bytes(b"actual-model-bytes")
        cache = ModelCache(temms_env["data_dir"] / "temms.db")
        cache.add_cached_model(
            model_id="model-corrupt",
            name="corrupt",
            version="1",
            format=ModelFormat.ONNX,
            path=model_path,
            sha256="0" * 64,
            size_bytes=999,
            package_id="pkg-corrupt",
        )
        cache.add_cached_model(
            model_id="model-missing",
            name="missing",
            version="1",
            format=ModelFormat.ONNX,
            path=temms_env["data_dir"] / "models" / "missing.onnx",
            sha256="1" * 64,
            size_bytes=12,
            package_id="pkg-missing",
        )

        result = runner.invoke(
            app,
            ["doctor", "--config", str(temms_env["config_path"]), "--json"],
        )

        assert result.exit_code == 0
        health = json.loads(result.output)["model_cache"]["health"]
        assert health["status"] == "degraded"
        assert health["checked_models"] == 2
        assert {issue["type"] for issue in health["issues"]} == {
            "missing_file",
            "size_mismatch",
            "sha256_mismatch",
        }

    def test_doctor_json_reports_missing_config_without_rich_warning(self, temp_dir):
        missing_config = temp_dir / "missing.yaml"

        result = runner.invoke(app, ["doctor", "--config", str(missing_config), "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["config"] == {"path": str(missing_config), "found": False}
        assert payload["model_cache"] is None
        assert "path_strategy" in payload
        assert {path["name"] for path in payload["paths"]} == {
            "data_dir",
            "package_dir",
            "policy_dir",
        }
        assert {port["name"] for port in payload["ports"]} == {"api", "grpc"}

    def test_doctor_json_reports_non_root_fallback_paths_without_config(
        self,
        temp_dir,
        monkeypatch,
    ):
        missing_config = temp_dir / "missing.yaml"
        xdg_state_home = temp_dir / "state"
        fallback_dir = xdg_state_home / "temms"
        monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))
        monkeypatch.delenv("TEMMS_DATA_DIR", raising=False)
        monkeypatch.setattr("temms.daemon.service._path_can_be_created", lambda path: False)

        result = runner.invoke(app, ["doctor", "--config", str(missing_config), "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["path_strategy"]["source"] == "user_state_fallback"
        assert payload["path_strategy"]["non_root_fallback"] is True
        assert payload["path_strategy"]["data_dir"] == str(fallback_dir)
        paths = {path["name"]: path for path in payload["paths"]}
        assert paths["data_dir"]["path"] == str(fallback_dir)
        assert paths["package_dir"]["path"] == str(fallback_dir / "packages")
        assert paths["policy_dir"]["path"] == str(fallback_dir / "policies")

    def test_doctor_human_reports_non_root_fallback_paths_without_config(
        self,
        temp_dir,
        monkeypatch,
    ):
        missing_config = temp_dir / "missing.yaml"
        xdg_state_home = temp_dir / "state"
        fallback_dir = xdg_state_home / "temms"
        monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))
        monkeypatch.delenv("TEMMS_DATA_DIR", raising=False)
        monkeypatch.setattr("temms.daemon.service._path_can_be_created", lambda path: False)

        result = runner.invoke(app, ["doctor", "--config", str(missing_config)])

        assert result.exit_code == 0
        assert "Config not found" in result.output
        assert "Path strategy" in result.output
        assert "using non-root fallback" in result.output
        assert str(fallback_dir) in result.output

    def test_doctor_json_reports_actual_write_probe_failure(self, temms_env, monkeypatch):
        def fake_probe(path):
            if path == temms_env["config_dir"] / "policies":
                return {
                    "ok": False,
                    "path": str(path),
                    "attempted": True,
                    "error": "read-only file system",
                }
            return {
                "ok": True,
                "path": str(path),
                "attempted": True,
                "error": None,
            }

        monkeypatch.setattr("temms.cli.main._probe_path_writable", fake_probe)

        result = runner.invoke(
            app,
            ["doctor", "--config", str(temms_env["config_path"]), "--json"],
        )

        assert result.exit_code == 0
        paths = {path["name"]: path for path in json.loads(result.output)["paths"]}
        assert paths["policy_dir"]["writable"] is False
        assert paths["policy_dir"]["write_probe"]["attempted"] is True
        assert paths["policy_dir"]["write_probe"]["error"] == "read-only file system"

    def test_doctor_json_reports_security_readiness(self, temms_env, monkeypatch):
        signing_key_file = temms_env["data_dir"] / "signing.key"
        signing_key_file.write_text("secret", encoding="utf-8")
        monkeypatch.setenv("TEMMS_API_TOKEN", "api-token")
        monkeypatch.delenv("TEMMS_HUB_TOKEN", raising=False)
        monkeypatch.setenv("TEMMS_PACKAGE_SIGNING_KEY_FILE", str(signing_key_file))
        monkeypatch.delenv("TEMMS_PACKAGE_SIGNING_KEY", raising=False)
        monkeypatch.setenv("TEMMS_ROLLOUT_REQUIRE_SIGNATURE", "false")

        result = runner.invoke(
            app,
            ["doctor", "--config", str(temms_env["config_path"]), "--json"],
        )

        assert result.exit_code == 0
        security = json.loads(result.output)["security"]
        assert security == {
            "api_token_configured": True,
            "control_auth_enabled": True,
            "hub_token_configured": False,
            "hub_token_source": "TEMMS_API_TOKEN fallback",
            "rollout_require_signature": False,
            "signing_key_configured": True,
            "signing_key_source": "TEMMS_PACKAGE_SIGNING_KEY_FILE",
            "signing_key_file": str(signing_key_file),
            "signing_key_file_exists": True,
        }

    def test_doctor_human_output_reports_security_readiness(self, temms_env, monkeypatch):
        monkeypatch.setenv("TEMMS_API_TOKEN", "api-token")

        result = runner.invoke(
            app,
            ["doctor", "--config", str(temms_env["config_path"])],
        )

        assert result.exit_code == 0
        assert "Security Readiness" in result.output
        assert "Control API token" in result.output
        assert "Rollout signature enforcement" in result.output
        assert "Ports" in result.output
        assert "grpc" in result.output


class TestBenchmarkCommand:
    """Test 'temms benchmark' command."""

    def test_benchmark_writes_json(self, temms_env, monkeypatch):
        def fake_run_benchmark_sync(*args, **kwargs):
            return {
                "schema_version": "temms-benchmark/v1",
                "model_id": "model-1",
                "latency_ms": {"p95": 1.2},
            }

        monkeypatch.setattr("temms.benchmark.run_benchmark_sync", fake_run_benchmark_sync)
        output = temms_env["data_dir"] / "benchmark.json"

        result = runner.invoke(
            app,
            [
                "benchmark",
                "model-1",
                "--config",
                str(temms_env["config_path"]),
                "--output",
                str(output),
            ],
        )

        assert result.exit_code == 0
        assert json.loads(output.read_text())["schema_version"] == "temms-benchmark/v1"

    def test_benchmark_can_publish_to_hub(self, temms_env, monkeypatch):
        calls = []

        def fake_run_benchmark_sync(*args, **kwargs):
            return {
                "schema_version": "temms-benchmark/v1",
                "model_id": "model-1",
                "latency_ms": {"p95": 1.2},
            }

        class FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {"benchmark_id": "benchmark-1"}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                calls.append(("client", kwargs))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                calls.append((path, json))
                return FakeResponse()

        monkeypatch.setattr("temms.benchmark.run_benchmark_sync", fake_run_benchmark_sync)
        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "benchmark",
                "model-1",
                "--config",
                str(temms_env["config_path"]),
                "--hub-url",
                "http://hub-vm:8080",
                "--token",
                "hub-token",
                "--device-id",
                "edge-1",
                "--package-id",
                "pkg-vision",
                "--runtime-target-id",
                "temms-x86_64-cpu",
            ],
        )

        assert result.exit_code == 0
        assert "Benchmark published: benchmark-1" in result.output
        assert calls[0][1]["base_url"] == "http://hub-vm:8080/v1/hub"
        assert calls[0][1]["headers"] == {"X-TEMMS-Token": "hub-token"}
        assert calls[1] == (
            "/benchmarks",
            {
                "device_id": "edge-1",
                "package_id": "pkg-vision",
                "runtime_target_id": "temms-x86_64-cpu",
                "result": {
                    "schema_version": "temms-benchmark/v1",
                    "model_id": "model-1",
                    "latency_ms": {"p95": 1.2},
                },
                "actor": "operator:cli",
            },
        )


class TestPackageCommand:
    """Test 'temms package' commands."""

    def test_package_sign_and_validate(self, temp_dir):
        pkg = temp_dir / "pkg"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_file.write_bytes(b"fake-onnx")

        import hashlib

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-test",
            "name": "pkg-test",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-1",
                    "name": "model",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    "size_bytes": len(b"fake-onnx"),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        sign_result = runner.invoke(app, ["package", "sign", str(pkg), "--signing-key", "secret"])
        assert sign_result.exit_code == 0

        validate_result = runner.invoke(
            app,
            [
                "package",
                "validate",
                str(pkg),
                "--signing-key",
                "secret",
            ],
        )
        assert validate_result.exit_code == 0
        assert "Signature verified" in validate_result.output
        assert "Signer: temms" in validate_result.output
        assert "Key fingerprint: sha256:" in validate_result.output
        signature = json.loads((pkg / "signature.json").read_text(encoding="utf-8"))
        assert signature["key_fingerprint"].startswith("sha256:")

    def test_package_validate_requires_signature_by_default(self, temp_dir):
        pkg = temp_dir / "pkg-unsigned-validate"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_file.write_bytes(b"fake-onnx")

        import hashlib

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-unsigned-validate",
            "name": "pkg-unsigned-validate",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-unsigned-validate-1",
                    "name": "model-unsigned-validate",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    "size_bytes": len(b"fake-onnx"),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = runner.invoke(app, ["package", "validate", str(pkg)])

        assert result.exit_code == 1
        assert "Signature verification requires a signing key" in " ".join(result.output.split())

    def test_package_validate_can_check_runtime_constraints(self, temp_dir):
        pkg = temp_dir / "pkg-runtime-validate.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_file.write_bytes(b"fake-onnx")

        import hashlib

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-runtime-validate",
            "name": "pkg-runtime-validate",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "compatibility": {
                "device_profiles": ["x86_64-cpu"],
                "runtime_constraints": {"runtimes": ["missing-runtime"]},
            },
            "models": [
                {
                    "id": "model-runtime-validate-1",
                    "name": "model-runtime-validate",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    "size_bytes": len(b"fake-onnx"),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        sign_result = runner.invoke(app, ["package", "sign", str(pkg), "--signing-key", "secret"])
        assert sign_result.exit_code == 0

        structure_only = runner.invoke(
            app,
            ["package", "validate", str(pkg), "--signing-key", "secret"],
        )
        assert structure_only.exit_code == 0
        structure_json = runner.invoke(
            app,
            [
                "package",
                "validate",
                str(pkg),
                "--signing-key",
                "secret",
                "--json",
            ],
        )
        assert structure_json.exit_code == 0
        structure_payload = json.loads(structure_json.output)
        assert structure_payload["schema_version"] == "temms-package-validation/v1"
        assert structure_payload["valid"] is True
        assert structure_payload["signature_verified"] is True
        assert structure_payload["package"]["package_id"] == "pkg-runtime-validate"
        assert structure_payload["package"]["models"] == 1

        runtime_check = runner.invoke(
            app,
            [
                "package",
                "validate",
                str(pkg),
                "--signing-key",
                "secret",
                "--device-profile",
                "x86_64-cpu",
                "--check-runtime",
            ],
        )
        assert runtime_check.exit_code == 1
        assert "Runtime constraints are not satisfied" in runtime_check.output
        assert "missing runtimes: missing-runtime" in runtime_check.output
        runtime_json = runner.invoke(
            app,
            [
                "package",
                "validate",
                str(pkg),
                "--signing-key",
                "secret",
                "--device-profile",
                "x86_64-cpu",
                "--check-runtime",
                "--json",
            ],
        )
        assert runtime_json.exit_code == 1
        runtime_payload = json.loads(runtime_json.output)
        assert runtime_payload["valid"] is False
        assert runtime_payload["runtime_checked"] is True
        assert runtime_payload["device_profile"] == "x86_64-cpu"
        assert any(
            "missing runtimes: missing-runtime" in error for error in runtime_payload["errors"]
        )

    def test_import_rejects_unsatisfied_runtime_constraints_by_default(self, temp_dir, temms_env):
        pkg = temp_dir / "pkg-runtime-import.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_file.write_bytes(b"fake-onnx")

        import hashlib

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-runtime-import",
            "name": "pkg-runtime-import",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "compatibility": {
                "device_profiles": ["x86_64-cpu"],
                "runtime_constraints": {"runtimes": ["missing-runtime"]},
            },
            "models": [
                {
                    "id": "model-runtime-import-1",
                    "name": "model-runtime-import",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    "size_bytes": len(b"fake-onnx"),
                    "input_schema": {"shape": [1, 3, 224, 224]},
                    "output_schema": {"shape": [1, 1000]},
                    "runtime_constraints": {"runtimes": ["missing-runtime"]},
                    "benchmark": {"available": False},
                    "provenance": {
                        "source": "unit-test",
                        "run_id": "run-runtime-import",
                        "artifact_sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    },
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        sign_result = runner.invoke(app, ["package", "sign", str(pkg), "--signing-key", "secret"])
        assert sign_result.exit_code == 0

        result = runner.invoke(
            app,
            [
                "import",
                str(pkg),
                "--config",
                str(temms_env["config_path"]),
                "--signing-key",
                "secret",
                "--device-profile",
                "x86_64-cpu",
            ],
        )

        assert result.exit_code == 1
        output = " ".join(result.output.split())
        assert "Runtime constraints are not satisfied" in output
        assert "missing runtimes: missing-runtime" in output
        cache = ModelCache(temms_env["data_dir"] / "temms.db")
        assert cache.list_models() == []
        assert cache.list_packages() == []

    def test_package_archive_validate_sign_and_import(self, temp_dir, temms_env):
        pkg = temp_dir / "pkg-archive.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_file.write_bytes(b"fake-onnx")

        import hashlib

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-archive",
            "name": "pkg-archive",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-archive-1",
                    "name": "model-archive",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    "size_bytes": len(b"fake-onnx"),
                    "input_schema": {"shape": [1, 3, 224, 224]},
                    "output_schema": {"shape": [1, 1000]},
                    "runtime_constraints": {"device_profiles": ["x86_64-cpu"]},
                    "benchmark": {"available": False},
                    "provenance": {
                        "source": "unit-test",
                        "run_id": "run-archive",
                        "artifact_sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    },
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        archive_result = runner.invoke(app, ["package", "archive", str(pkg)])
        assert archive_result.exit_code == 0
        archive_path = temp_dir / "pkg-archive.temms.tar.zst"
        assert archive_path.exists()

        sign_result = runner.invoke(
            app,
            ["package", "sign", str(archive_path), "--signing-key", "secret"],
        )
        assert sign_result.exit_code == 0

        validate_result = runner.invoke(
            app,
            [
                "package",
                "validate",
                str(archive_path),
                "--signing-key",
                "secret",
            ],
        )
        assert validate_result.exit_code == 0
        assert "Signature verified" in validate_result.output

        import_result = runner.invoke(
            app,
            [
                "import",
                str(archive_path),
                "--config",
                str(temms_env["config_path"]),
                "--require-signature",
                "--signing-key",
                "secret",
                "--device-profile",
                "x86_64-cpu",
            ],
        )
        assert import_result.exit_code == 0
        assert "Package imported successfully" in import_result.output
        cache = ModelCache(temms_env["data_dir"] / "temms.db")
        imported = cache.list_packages()[0]
        import_audit = imported.manifest["_temms_import"]
        assert import_audit["source_type"] == "archive"
        assert import_audit["source_sha256"]
        assert import_audit["archive_sha256"] == import_audit["source_sha256"]
        assert import_audit["directory_sha256"] is None
        assert import_audit["signature_verified"] is True
        assert import_audit["validation"]["signature_verified"] is True
        assert import_audit["signature"]["signer"] == "temms"
        assert import_audit["signature"]["key_fingerprint"].startswith("sha256:")

    def test_import_requires_verified_signature_by_default(self, temp_dir, temms_env):
        pkg = temp_dir / "pkg-unsigned-import.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_file.write_bytes(b"fake-onnx")

        import hashlib

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-unsigned-import",
            "name": "pkg-unsigned-import",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-unsigned-import-1",
                    "name": "model-unsigned-import",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    "size_bytes": len(b"fake-onnx"),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = runner.invoke(
            app,
            ["import", str(pkg), "--config", str(temms_env["config_path"])],
        )

        assert result.exit_code == 1
        assert "Signature verification requires a signing key" in " ".join(result.output.split())

    def test_package_inspect_outputs_hub_catalog_entry(self, temp_dir):
        pkg = temp_dir / "pkg-inspect.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_file.write_bytes(b"fake-onnx")

        import hashlib

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-inspect",
            "name": "pkg-inspect",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
            "models": [
                {
                    "id": "model-inspect-1",
                    "name": "model-inspect",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    "size_bytes": len(b"fake-onnx"),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        sign_result = runner.invoke(app, ["package", "sign", str(pkg), "--signing-key", "secret"])
        assert sign_result.exit_code == 0

        inspect_result = runner.invoke(
            app,
            [
                "package",
                "inspect",
                str(pkg),
                "--signing-key",
                "secret",
                "--json",
            ],
        )

        assert inspect_result.exit_code == 0
        entry = json.loads(inspect_result.output)
        assert entry["package_id"] == "pkg-inspect"
        assert entry["sha256"]
        assert entry["source_sha256"] == entry["sha256"]
        assert entry["metadata"]["source"]["type"] == "directory"
        assert entry["metadata"]["source"]["sha256"] == entry["sha256"]
        assert entry["device_profiles"] == ["x86_64-cpu"]
        assert entry["metadata"]["validation"]["signature_verified"] is True
        assert entry["metadata"]["models"][0]["id"] == "model-inspect-1"

    def test_package_inspect_honors_strict_metadata(self, temp_dir):
        pkg = temp_dir / "pkg-inspect-lab.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_file.write_bytes(b"fake-onnx")

        import hashlib

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-inspect-lab",
            "name": "pkg-inspect-lab",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-inspect-lab-1",
                    "name": "model-inspect-lab",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    "size_bytes": len(b"fake-onnx"),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        sign_result = runner.invoke(app, ["package", "sign", str(pkg), "--signing-key", "secret"])
        assert sign_result.exit_code == 0

        loose_inspect = runner.invoke(
            app,
            [
                "package",
                "inspect",
                str(pkg),
                "--signing-key",
                "secret",
                "--json",
            ],
        )
        assert loose_inspect.exit_code == 0
        loose_entry = json.loads(loose_inspect.output)
        assert loose_entry["metadata"]["validation"]["strict_metadata"] is False
        assert any(
            "Model metadata incomplete" in warning
            for warning in loose_entry["metadata"]["validation"]["warnings"]
        )

        strict_inspect = runner.invoke(
            app,
            [
                "package",
                "inspect",
                str(pkg),
                "--signing-key",
                "secret",
                "--strict-metadata",
            ],
        )

        assert strict_inspect.exit_code == 1
        assert "Model metadata incomplete" in strict_inspect.output

    def test_package_from_mlflow_builds_signed_package(self, temp_dir, monkeypatch):
        artifact_dir = temp_dir / "artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "model.onnx").write_bytes(b"mlflow-onnx")

        policy = temp_dir / "policy.yaml"
        policy.write_text("""
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: test-policy
spec:
  slot: vision
  rules: []
""")

        class FakeClient:
            def get_model_version(self, name, version):
                return SimpleNamespace(
                    version=version,
                    run_id="run-123",
                    source="s3://mlflow-artifacts/detector/7",
                    status="READY",
                    current_stage="Production",
                    aliases=["champion"],
                    creation_timestamp=1710000000000,
                    last_updated_timestamp=1710000100000,
                )

            def get_run(self, run_id):
                return SimpleNamespace(
                    info=SimpleNamespace(
                        run_id=run_id,
                        artifact_uri="s3://mlflow-artifacts/run-123/artifacts",
                        user_id="ml-engineer",
                        status="FINISHED",
                        start_time=1709999900000,
                        end_time=1710000000000,
                    ),
                    data=SimpleNamespace(
                        params={
                            "input_schema": '{"shape":[1,3,224,224]}',
                            "output_schema": '{"classes":["vehicle","person"]}',
                            "runtime_constraints": '{"runtimes":["onnx"]}',
                            "runtime_options": '{"providers":["CPUExecutionProvider"]}',
                        },
                        metrics={
                            "avg_latency_ms": 6.25,
                            "p95_latency_ms": 12.5,
                            "fps": 31.0,
                            "peak_memory_mb": 128.0,
                            "accuracy": 0.98,
                        },
                        tags={"mlflow.runName": "test-run"},
                    ),
                )

            def download_artifacts(self, run_id, path, dst_path):
                import shutil

                dest = Path(dst_path) / "model"
                shutil.copytree(artifact_dir, dest)
                return str(dest)

        fake_mlflow = SimpleNamespace(
            set_tracking_uri=lambda uri: None,
            tracking=SimpleNamespace(MlflowClient=FakeClient),
        )
        monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

        result = runner.invoke(
            app,
            [
                "package",
                "from-mlflow",
                "models:/detector/7",
                "--slot",
                "vision",
                "--policy",
                str(policy),
                "--output",
                str(temp_dir),
                "--tracking-uri",
                "http://mlflow.example",
                "--device-profile",
                "x86_64-cpu",
                "--runtime-constraint",
                'accelerators=["nvidia"]',
                "--runtime-constraint",
                'provider_order=["CUDAExecutionProvider","CPUExecutionProvider"]',
                "--runtime-option",
                'providers=["CUDAExecutionProvider","CPUExecutionProvider"]',
                "--signing-key",
                "secret",
                "--json",
            ],
        )

        assert result.exit_code == 0
        build_payload = json.loads(result.output)
        assert build_payload["schema_version"] == "temms-package-build/v1"
        assert build_payload["action"] == "from-mlflow"
        assert build_payload["package"]["package_id"] == "mlflow-detector-7"
        assert build_payload["package"]["sha256"]
        assert build_payload["package"]["metadata"]["validation"]["signature_verified"] is True
        assert build_payload["package"]["metadata"]["source"]["type"] == "directory"
        package_dir = temp_dir / "mlflow-detector-7.temms"
        manifest = json.loads((package_dir / "manifest.json").read_text())
        assert manifest["mlflow_run_id"] == "run-123"
        assert manifest["metadata"]["build"]["schema_version"] == "temms-package-build/v1"
        assert manifest["metadata"]["build"]["workflow"] == "temms package from-mlflow"
        assert manifest["metadata"]["build"]["tracking_uri"] == "http://mlflow.example"
        assert manifest["metadata"]["build"]["requested_model_uri"] == "models:/detector/7"
        assert manifest["metadata"]["build"]["resolved_model_uri"] == "models:/detector/7"
        assert manifest["metadata"]["build"]["signed"] is True
        assert manifest["provenance"]["resolved_model_uri"] == "models:/detector/7"
        assert manifest["provenance"]["model_source"] == "s3://mlflow-artifacts/detector/7"
        assert manifest["provenance"]["model_status"] == "READY"
        assert manifest["provenance"]["model_current_stage"] == "Production"
        assert manifest["provenance"]["model_aliases"] == ["champion"]
        assert manifest["provenance"]["run_artifact_uri"] == (
            "s3://mlflow-artifacts/run-123/artifacts"
        )
        assert manifest["provenance"]["run_params_sha256"]
        assert manifest["provenance"]["run_tags_sha256"]
        artifact_metadata = manifest["provenance"]["artifact_metadata"]
        assert artifact_metadata == {
            "path": "model.onnx",
            "format": "onnx",
            "size_bytes": len(b"mlflow-onnx"),
            "sha256": manifest["models"][0]["sha256"],
        }
        assert manifest["provenance"]["artifact_metadata_sha256"]
        assert (
            manifest["metadata"]["build"]["artifact_metadata_sha256"]
            == manifest["provenance"]["artifact_metadata_sha256"]
        )
        assert manifest["models"][0]["input_schema"]["shape"] == [1, 3, 224, 224]
        assert manifest["models"][0]["output_schema"]["classes"] == ["vehicle", "person"]
        assert manifest["models"][0]["provenance"]["artifact_sha256"] == (
            manifest["models"][0]["sha256"]
        )
        assert manifest["models"][0]["provenance"]["artifact_size_bytes"] == len(b"mlflow-onnx")
        assert (
            manifest["models"][0]["provenance"]["artifact_metadata_sha256"]
            == manifest["provenance"]["artifact_metadata_sha256"]
        )
        assert manifest["models"][0]["runtime_options"]["providers"] == [
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
        assert manifest["models"][0]["runtime_constraints"]["runtimes"] == ["onnx"]
        assert manifest["models"][0]["runtime_constraints"]["accelerators"] == ["nvidia"]
        assert manifest["models"][0]["runtime_constraints"]["provider_order"] == [
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
        benchmark = manifest["models"][0]["benchmark"]
        assert benchmark["latency_ms"] == 6.25
        assert benchmark["p95_latency_ms"] == 12.5
        assert benchmark["throughput_fps"] == 31.0
        assert benchmark["memory_mb"] == 128.0
        assert benchmark["accuracy"] == 0.98
        assert benchmark["available"] is True
        assert benchmark["_source"]["type"] == "mlflow_run_metrics"
        assert benchmark["_source"]["metric_keys"] == {
            "latency_ms": "avg_latency_ms",
            "p95_latency_ms": "p95_latency_ms",
            "throughput_fps": "fps",
            "memory_mb": "peak_memory_mb",
            "accuracy": "accuracy",
        }
        assert benchmark["_source"]["metrics_sha256"]
        assert manifest["compatibility"]["device_profiles"] == ["x86_64-cpu"]
        assert (package_dir / "signature.json").exists()

        compatible = runner.invoke(
            app,
            [
                "package",
                "validate",
                str(package_dir),
                "--signing-key",
                "secret",
                "--device-profile",
                "x86_64-cpu",
                "--strict-metadata",
            ],
        )
        assert compatible.exit_code == 0

        incompatible = runner.invoke(
            app,
            [
                "package",
                "validate",
                str(package_dir),
                "--signing-key",
                "secret",
                "--device-profile",
                "rpi5-tflite",
            ],
        )
        assert incompatible.exit_code == 1
        assert "not compatible" in incompatible.output

    def test_package_from_mlflow_uses_mlmodel_signature_fallback(self, temp_dir, monkeypatch):
        artifact_dir = temp_dir / "signature_artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "model.onnx").write_bytes(b"mlflow-signature-onnx")
        (artifact_dir / "MLmodel").write_text("""
artifact_path: model
flavors:
  onnx:
    data: model.onnx
signature:
  inputs: '[{"name":"image","type":"tensor","tensor-spec":{"dtype":"float32","shape":[-1,3,224,224]}}]'
  outputs: '[{"name":"detections","type":"tensor","tensor-spec":{"dtype":"float32","shape":[-1,6]}}]'
""")

        class FakeClient:
            def get_model_version(self, name, version):
                return SimpleNamespace(version=version, run_id="run-signature")

            def get_run(self, run_id):
                return SimpleNamespace(
                    info=SimpleNamespace(run_id=run_id),
                    data=SimpleNamespace(
                        params={"runtime_constraints": '{"runtimes":["onnx"]}'},
                        metrics={},
                        tags={"mlflow.runName": "signature-run"},
                    ),
                )

            def download_artifacts(self, run_id, path, dst_path):
                import shutil

                dest = Path(dst_path) / "model"
                shutil.copytree(artifact_dir, dest)
                return str(dest)

        fake_mlflow = SimpleNamespace(
            set_tracking_uri=lambda uri: None,
            tracking=SimpleNamespace(MlflowClient=FakeClient),
        )
        monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

        result = runner.invoke(
            app,
            [
                "package",
                "from-mlflow",
                "models:/detector/8",
                "--slot",
                "vision",
                "--output",
                str(temp_dir),
                "--tracking-uri",
                "http://mlflow.example",
            ],
        )

        assert result.exit_code == 0
        manifest = json.loads((temp_dir / "mlflow-detector-8.temms" / "manifest.json").read_text())
        model = manifest["models"][0]
        assert model["input_schema"]["source"] == "MLmodel"
        assert model["input_schema"]["schema"][0]["name"] == "image"
        assert model["output_schema"]["schema"][0]["name"] == "detections"
        assert model["provenance"]["signature_path"] == "MLmodel"

    def test_package_from_mlflow_records_alias_and_resolved_version(self, temp_dir, monkeypatch):
        artifact_dir = temp_dir / "alias_artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "model.onnx").write_bytes(b"alias-onnx")

        class FakeClient:
            def get_model_version_by_alias(self, name, alias):
                assert name == "detector"
                assert alias == "champion"
                return SimpleNamespace(
                    version="9",
                    run_id="run-alias",
                    source="s3://mlflow-artifacts/detector/9",
                    aliases=["champion", "stable"],
                )

            def get_run(self, run_id):
                return SimpleNamespace(
                    info=SimpleNamespace(
                        run_id=run_id,
                        artifact_uri="s3://mlflow-artifacts/run-alias/artifacts",
                    ),
                    data=SimpleNamespace(
                        params={
                            "input_schema": '{"shape":[1,3,224,224]}',
                            "output_schema": '{"shape":[1,1000]}',
                            "runtime_constraints": '{"runtimes":["onnx"]}',
                        },
                        metrics={},
                        tags={"mlflow.runName": "alias-run"},
                    ),
                )

            def download_artifacts(self, run_id, path, dst_path):
                import shutil

                dest = Path(dst_path) / "model"
                shutil.copytree(artifact_dir, dest)
                return str(dest)

        fake_mlflow = SimpleNamespace(
            set_tracking_uri=lambda uri: None,
            tracking=SimpleNamespace(MlflowClient=FakeClient),
        )
        monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

        result = runner.invoke(
            app,
            [
                "package",
                "from-mlflow",
                "models:/detector@champion",
                "--slot",
                "vision",
                "--output",
                str(temp_dir),
            ],
        )

        assert result.exit_code == 0
        manifest = json.loads((temp_dir / "mlflow-detector-9.temms" / "manifest.json").read_text())
        assert manifest["provenance"]["model_uri"] == "models:/detector@champion"
        assert manifest["provenance"]["resolved_model_uri"] == "models:/detector/9"
        assert manifest["provenance"]["model_alias"] == "champion"
        assert manifest["provenance"]["model_aliases"] == ["champion", "stable"]
        assert manifest["provenance"]["run_name"] == "alias-run"
        model_provenance = manifest["models"][0]["provenance"]
        assert model_provenance["model_uri"] == "models:/detector@champion"
        assert model_provenance["resolved_model_uri"] == "models:/detector/9"
        assert model_provenance["artifact_sha256"] == manifest["models"][0]["sha256"]

    def test_package_from_mlflow_rejects_ambiguous_model_artifacts(self, temp_dir, monkeypatch):
        artifact_dir = temp_dir / "ambiguous_artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "candidate-a.onnx").write_bytes(b"model-a")
        (artifact_dir / "candidate-b.onnx").write_bytes(b"model-b")

        class FakeClient:
            def get_model_version(self, name, version):
                return SimpleNamespace(version=version, run_id="run-ambiguous")

            def get_run(self, run_id):
                return SimpleNamespace(
                    info=SimpleNamespace(run_id=run_id),
                    data=SimpleNamespace(
                        params={
                            "input_schema": '{"shape":[1,3,224,224]}',
                            "output_schema": '{"shape":[1,1000]}',
                            "runtime_constraints": '{"runtimes":["onnx"]}',
                        },
                        metrics={},
                        tags={},
                    ),
                )

            def download_artifacts(self, run_id, path, dst_path):
                import shutil

                dest = Path(dst_path) / "model"
                shutil.copytree(artifact_dir, dest)
                return str(dest)

        fake_mlflow = SimpleNamespace(
            set_tracking_uri=lambda uri: None,
            tracking=SimpleNamespace(MlflowClient=FakeClient),
        )
        monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

        result = runner.invoke(
            app,
            [
                "package",
                "from-mlflow",
                "models:/detector/10",
                "--slot",
                "vision",
                "--output",
                str(temp_dir),
            ],
        )

        assert result.exit_code == 1
        assert "Multiple model artifacts found" in result.output
        assert "--model-artifact" in result.output

    def test_package_from_mlflow_uses_explicit_model_artifact(self, temp_dir, monkeypatch):
        import hashlib

        artifact_dir = temp_dir / "explicit_artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "candidate-a.onnx").write_bytes(b"model-a")
        chosen = artifact_dir / "candidate-b.onnx"
        chosen.write_bytes(b"model-b")

        class FakeClient:
            def get_model_version(self, name, version):
                return SimpleNamespace(version=version, run_id="run-explicit")

            def get_run(self, run_id):
                return SimpleNamespace(
                    info=SimpleNamespace(run_id=run_id),
                    data=SimpleNamespace(
                        params={
                            "input_schema": '{"shape":[1,3,224,224]}',
                            "output_schema": '{"shape":[1,1000]}',
                            "runtime_constraints": '{"runtimes":["onnx"]}',
                        },
                        metrics={},
                        tags={},
                    ),
                )

            def download_artifacts(self, run_id, path, dst_path):
                import shutil

                dest = Path(dst_path) / "model"
                shutil.copytree(artifact_dir, dest)
                return str(dest)

        fake_mlflow = SimpleNamespace(
            set_tracking_uri=lambda uri: None,
            tracking=SimpleNamespace(MlflowClient=FakeClient),
        )
        monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

        result = runner.invoke(
            app,
            [
                "package",
                "from-mlflow",
                "models:/detector/11",
                "--slot",
                "vision",
                "--output",
                str(temp_dir),
                "--model-artifact",
                "candidate-b.onnx",
            ],
        )

        assert result.exit_code == 0, result.output
        manifest = json.loads((temp_dir / "mlflow-detector-11.temms" / "manifest.json").read_text())
        model = manifest["models"][0]
        assert model["filename"] == "candidate-b.onnx"
        assert model["sha256"] == hashlib.sha256(b"model-b").hexdigest()
        assert model["provenance"]["artifact_path"] == "candidate-b.onnx"

        repeat = runner.invoke(
            app,
            [
                "package",
                "from-mlflow",
                "models:/detector/11",
                "--slot",
                "vision",
                "--output",
                str(temp_dir),
                "--model-artifact",
                "candidate-b.onnx",
            ],
        )
        assert repeat.exit_code == 1
        assert "TEMMS package output already exists" in repeat.output

        overwrite = runner.invoke(
            app,
            [
                "package",
                "from-mlflow",
                "models:/detector/11",
                "--slot",
                "vision",
                "--output",
                str(temp_dir),
                "--model-artifact",
                "candidate-b.onnx",
                "--overwrite",
            ],
        )
        assert overwrite.exit_code == 0, overwrite.output

    def test_package_from_mlflow_requires_schema_by_default(self, temp_dir, monkeypatch):
        artifact_dir = temp_dir / "missing_schema_artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "model.onnx").write_bytes(b"missing-schema-onnx")

        class FakeClient:
            def get_model_version(self, name, version):
                return SimpleNamespace(version=version, run_id="run-missing-schema")

            def get_run(self, run_id):
                return SimpleNamespace(
                    info=SimpleNamespace(run_id=run_id),
                    data=SimpleNamespace(params={}, metrics={}, tags={}),
                )

            def download_artifacts(self, run_id, path, dst_path):
                import shutil

                dest = Path(dst_path) / "model"
                shutil.copytree(artifact_dir, dest)
                return str(dest)

        fake_mlflow = SimpleNamespace(
            set_tracking_uri=lambda uri: None,
            tracking=SimpleNamespace(MlflowClient=FakeClient),
        )
        monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

        result = runner.invoke(
            app,
            [
                "package",
                "from-mlflow",
                "models:/detector/10",
                "--slot",
                "vision",
                "--output",
                str(temp_dir),
            ],
        )

        assert result.exit_code == 1
        assert "requires input_schema and output_schema" in result.output
        assert not (temp_dir / "mlflow-detector-10.temms").exists()

        lab_result = runner.invoke(
            app,
            [
                "package",
                "from-mlflow",
                "models:/detector/10",
                "--slot",
                "vision",
                "--output",
                str(temp_dir),
                "--allow-missing-schema",
                "--allow-missing-runtime-constraints",
            ],
        )

        assert lab_result.exit_code == 0


class TestMLflowCommand:
    """Test legacy local-development MLflow bridge commands."""

    def test_mlflow_pull_requires_explicit_dev_opt_in(self, monkeypatch):
        class FakeBridge:
            def __init__(self, tracking_uri=None):
                raise AssertionError("MLflowBridge should not be constructed")

        monkeypatch.setitem(
            sys.modules,
            "temms.mlflow_bridge",
            SimpleNamespace(MLflowBridge=FakeBridge),
        )

        result = runner.invoke(
            app,
            ["mlflow", "pull", "detector", "--version", "7"],
        )

        assert result.exit_code == 1
        assert "local development only" in result.output
        assert "temms package from-mlflow models:/detector/7" in result.output
        assert "Refusing direct MLflow pull without --allow-dev-pull" in result.output

    def test_mlflow_pull_warns_to_use_signed_package_flow(self, temp_dir, monkeypatch):
        pulled_package = temp_dir / "pulled"

        class FakeBridge:
            available = True

            def __init__(self, tracking_uri=None):
                self.tracking_uri = tracking_uri

            def pull_model(self, model_name, version=None):
                assert model_name == "detector"
                assert version == "7"
                return pulled_package

        monkeypatch.setitem(
            sys.modules,
            "temms.mlflow_bridge",
            SimpleNamespace(MLflowBridge=FakeBridge),
        )

        result = runner.invoke(
            app,
            ["mlflow", "pull", "detector", "--version", "7", "--allow-dev-pull"],
        )

        assert result.exit_code == 0
        assert "local development only" in result.output
        assert "temms package from-mlflow models:/detector/7" in result.output
        assert "Model pulled to:" in result.output
        assert str(pulled_package) in result.output
        assert (
            f"temms import {pulled_package} --allow-unsigned-package --allow-lab-metadata"
        ) in " ".join(
            result.output.split()
        )


class TestHubCommand:
    """Test 'temms hub' commands."""

    def test_hub_enroll_posts_device_profile_labels_and_inventory(self, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                calls.append((path, json))
                return FakeResponse(json)

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "enroll",
                "--device-id",
                "edge-1",
                "--device-profile",
                "x86_64-cpu",
                "--label",
                "site=lab",
                "--inventory",
                "runtime=onnx",
                "--json",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["device_id"] == "edge-1"
        assert calls[0] == (
            "/devices/enroll",
            {
                "device_id": "edge-1",
                "profile": "x86_64-cpu",
                "labels": {"site": "lab"},
                "inventory": {"runtime": "onnx"},
            },
        )

    def test_hub_register_package_posts_catalog_entry(self, temp_dir, monkeypatch):
        pkg = temp_dir / "pkg-hub.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_file.write_bytes(b"fake-onnx")

        import hashlib

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-hub",
            "name": "pkg-hub",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "compatibility": {"device_profiles": ["x86_64-cpu"]},
            "models": [
                {
                    "id": "model-hub-1",
                    "name": "model-hub",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    "size_bytes": len(b"fake-onnx"),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        sign_result = runner.invoke(app, ["package", "sign", str(pkg), "--signing-key", "secret"])
        assert sign_result.exit_code == 0

        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.base_url = kwargs.get("base_url")
                self.headers = kwargs.get("headers")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                calls.append((path, json, self.base_url, self.headers))
                return FakeResponse(
                    {
                        "package_id": "pkg-hub",
                        "metadata": {
                            "validation": {
                                "signature_verified": True,
                            }
                        },
                    }
                )

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "register-package",
                str(pkg),
                "--hub-url",
                "http://hub:8080",
                "--token",
                "hub-token",
                "--signing-key",
                "secret",
                "--actor",
                "operator:alice",
                "--json",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["package_id"] == "pkg-hub"
        assert calls[0][0] == "/packages/register"
        assert calls[0][2] == "http://hub:8080/v1/hub"
        assert calls[0][3] == {"X-TEMMS-Token": "hub-token"}
        assert calls[0][1] == {
            "package_path": str(pkg),
            "require_signature": True,
            "signing_key": "secret",
            "device_profiles": None,
            "strict_metadata": True,
            "actor": "operator:alice",
        }
        assert payload["metadata"]["validation"]["signature_verified"] is True

    def test_hub_register_package_requires_verified_signature_by_default(
        self, temp_dir, monkeypatch
    ):
        pkg = temp_dir / "pkg-unsigned.temms"
        models = pkg / "models"
        models.mkdir(parents=True)
        model_file = models / "model.onnx"
        model_file.write_bytes(b"fake-onnx")

        import hashlib

        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-unsigned",
            "name": "pkg-unsigned",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model-unsigned-1",
                    "name": "model-unsigned",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(b"fake-onnx").hexdigest(),
                    "size_bytes": len(b"fake-onnx"),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        class FakeResponse:
            status_code = 400
            text = "Signature verification requires a signing key"

            def json(self):
                return {"detail": self.text}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                return FakeResponse()

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(app, ["hub", "register-package", str(pkg)])

        assert result.exit_code == 1
        assert "Signature verification requires a signing key" in result.output

    def test_hub_package_from_mlflow_posts_build_request(self, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.base_url = kwargs.get("base_url")
                self.headers = kwargs.get("headers")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                calls.append((path, json, self.base_url, self.headers))
                return FakeResponse(
                    {
                        "package": {
                            "package_id": "mlflow-detector-7",
                            "metadata": {
                                "validation": {
                                    "signature_verified": True,
                                }
                            },
                        },
                        "package_path": "/var/lib/temms/packages/mlflow-detector-7.temms.tar.zst",
                        "signed": True,
                    }
                )

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "package-from-mlflow",
                "models:/detector/7",
                "--hub-url",
                "http://hub:8080",
                "--token",
                "hub-token",
                "--slot",
                "vision",
                "--tracking-uri",
                "http://mlflow.example:5000",
                "--device-profile",
                "orin",
                "--runtime",
                "onnxruntime",
                "--provider",
                "CUDAExecutionProvider",
                "--accelerator",
                "nvidia",
                "--model-artifact",
                "model/model.onnx",
                "--allow-missing-schema",
                "--signing-key",
                "secret",
                "--overwrite",
                "--actor",
                "operator:alice",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Hub package built from MLflow" in result.output
        assert "mlflow-detector-7" in result.output
        assert calls == [
            (
                "/packages/from-mlflow",
                {
                    "model_uri": "models:/detector/7",
                    "slot": "vision",
                    "tracking_uri": "http://mlflow.example:5000",
                    "device_profile": "orin",
                    "runtime_constraints": {
                        "device_profiles": ["orin"],
                        "runtimes": ["onnxruntime"],
                        "preferred_providers": ["CUDAExecutionProvider"],
                        "accelerators": ["nvidia"],
                    },
                    "runtime_options": {
                        "providers": ["CUDAExecutionProvider"],
                    },
                    "model_artifact_path": "model/model.onnx",
                    "require_schema": False,
                    "require_signature": True,
                    "signing_key": "secret",
                    "archive": True,
                    "overwrite": True,
                    "strict_metadata": True,
                    "actor": "operator:alice",
                },
                "http://hub:8080/v1/hub",
                {"X-TEMMS-Token": "hub-token"},
            )
        ]

    def test_hub_package_from_mlflow_requires_slot(self, monkeypatch):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(app, ["hub", "package-from-mlflow", "models:/detector/7"])

        assert result.exit_code == 1
        assert "--slot is required" in result.output

    def test_hub_assign_and_export_bundle(self, temp_dir, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                calls.append((path, json))
                if path == "/airgap/export":
                    return FakeResponse({"schema_version": "temms-hub-lite-bundle/v1"})
                return FakeResponse({"rollout_id": "rollout-1", "state": "assigned"})

        monkeypatch.setattr("httpx.Client", FakeClient)

        assign = runner.invoke(
            app,
            [
                "hub",
                "assign",
                "--device-id",
                "edge-1",
                "--package-id",
                "pkg-vision",
                "--slot",
                "vision",
                "--rollout-id",
                "rollout-1",
                "--runtime-target-id",
                "temms-x86_64-cpu",
                "--require-runtime-validation",
                "--actor",
                "operator:alice",
            ],
        )
        assert assign.exit_code == 0
        assert calls[0] == (
            "/rollouts",
            {
                "device_id": "edge-1",
                "package_id": "pkg-vision",
                "slot": "vision",
                "rollout_id": "rollout-1",
                "runtime_target_id": "temms-x86_64-cpu",
                "require_runtime_validation": True,
                "actor": "operator:alice",
            },
        )

        output = temp_dir / "bundle.json"
        export = runner.invoke(
            app,
            [
                "hub",
                "export",
                "--include-packages",
                "--output",
                str(output),
            ],
        )
        assert export.exit_code == 0
        assert calls[1] == ("/airgap/export", {"include_packages": True})
        assert json.loads(output.read_text())["schema_version"] == "temms-hub-lite-bundle/v1"

    def test_hub_runtime_target_cli_lists_and_registers_images(self, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get(self, path):
                calls.append((path, None))
                return FakeResponse(
                    {
                        "runtime_targets": [
                            {
                                "runtime_target_id": "temms-x86_64-cpu",
                                "image": "temms/agent:inference-amd64",
                                "os": "linux",
                                "arch": "amd64",
                                "device_profiles": ["x86_64-cpu"],
                                "source": "default",
                            }
                        ]
                    }
                )

            def post(self, path, json=None):
                calls.append((path, json))
                return FakeResponse(json)

        monkeypatch.setattr("httpx.Client", FakeClient)

        listed = runner.invoke(app, ["hub", "runtime-targets"])

        assert listed.exit_code == 0
        assert "temms-x86_64-cpu" in listed.output
        assert calls[0] == ("/runtime-targets", None)

        registered = runner.invoke(
            app,
            [
                "hub",
                "register-runtime",
                "--runtime-target-id",
                "customer-orin",
                "--image",
                "registry.example.com/customer/orin:1.0.0",
                "--device-profile",
                "orin",
                "--runtime",
                "onnxruntime",
                "--runtime",
                "tensorrt",
                "--provider",
                "CUDAExecutionProvider",
                "--accelerator",
                "nvidia",
                "--actor",
                "operator:alice",
                "--json",
            ],
        )

        assert registered.exit_code == 0
        payload = json.loads(registered.output)
        assert payload["runtime_target_id"] == "customer-orin"
        assert calls[1] == (
            "/runtime-targets",
            {
                "runtime_target_id": "customer-orin",
                "name": "customer-orin",
                "image": "registry.example.com/customer/orin:1.0.0",
                "os": "linux",
                "arch": None,
                "device_profiles": ["orin"],
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CUDAExecutionProvider"],
                    },
                    "tensorrt": {"available": True},
                },
                "accelerators": {"nvidia": {"available": True}},
                "runtime_constraints": {
                    "device_profiles": ["orin"],
                    "runtimes": ["onnxruntime", "tensorrt"],
                    "preferred_providers": ["CUDAExecutionProvider"],
                    "accelerators": ["nvidia"],
                },
                "labels": {},
                "actor": "operator:alice",
            },
        )

    def test_hub_validate_runtime_builds_targeted_docker_command(self, temp_dir, monkeypatch):
        package_dir = temp_dir / "package.temms"
        package_dir.mkdir()
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def __init__(self, payload=None):
                self._payload = payload or {
                    "runtime_targets": [
                        {
                            "runtime_target_id": "customer-orin",
                            "image": "registry.example.com/customer/orin-runtime:2026.06",
                            "os": "linux",
                            "arch": "arm64",
                            "device_profiles": ["orin-tensorrt"],
                        }
                    ]
                }

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get(self, path):
                calls.append((path, None))
                return FakeResponse()

            def post(self, path, json=None):
                calls.append((path, json))
                return FakeResponse(
                    {
                        "validation_id": "runtime-validation-1",
                        "runtime_target_id": json["runtime_target_id"],
                        "package_id": json.get("package_id"),
                        "result": json["result"],
                    }
                )

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "validate-runtime",
                str(package_dir),
                "--runtime-target-id",
                "customer-orin",
                "--allow-unsigned-package",
                "--pull-image",
                "--dry-run",
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert calls[0] == ("/runtime-targets", None)
        assert calls[1][0] == "/runtime-targets/validations"
        assert calls[1][1]["runtime_target_id"] == "customer-orin"
        assert calls[1][1]["package_id"] is None
        assert calls[1][1]["result"]["runtime_target_id"] == "customer-orin"
        assert payload["schema_version"] == "temms-runtime-target-validation/v1"
        assert payload["runtime_target_id"] == "customer-orin"
        assert payload["validation_record"]["validation_id"] == "runtime-validation-1"
        assert payload["image"] == "registry.example.com/customer/orin-runtime:2026.06"
        assert payload["dry_run"] is True
        assert payload["ok"] is True
        assert payload["exit_code"] is None
        assert "--pull" in payload["command"]
        assert "always" in payload["command"]
        assert "--allow-unsigned-package" in payload["command"]
        assert "--device-profile" in payload["command"]
        assert "orin-tensorrt" in payload["command"]
        assert "--strict-metadata" in payload["command"]

    def test_hub_runtime_validations_lists_recorded_evidence(self, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {
                    "runtime_validations": [
                        {
                            "validation_id": "runtime-validation-1",
                            "package_id": "pkg-vision",
                            "runtime_target_id": "temms-x86_64-cpu",
                            "actor": "operator:alice",
                            "created_at": "2024-01-01T00:00:00Z",
                            "result": {"ok": True, "dry_run": False},
                        }
                    ],
                    "count": 1,
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get(self, path, params=None):
                calls.append((path, params))
                return FakeResponse()

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "runtime-validations",
                "--package-id",
                "pkg-vision",
                "--runtime-target-id",
                "temms-x86_64-cpu",
                "--json",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["runtime_validations"][0]["validation_id"] == "runtime-validation-1"
        assert payload["runtime_validations"][0]["package_id"] == "pkg-vision"
        assert calls == [
            (
                "/runtime-targets/validations",
                {
                    "package_id": "pkg-vision",
                    "runtime_target_id": "temms-x86_64-cpu",
                },
            )
        ]

    def test_hub_benchmarks_lists_recorded_evidence(self, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {
                    "benchmarks": [
                        {
                            "benchmark_id": "benchmark-1",
                            "device_id": "edge-1",
                            "package_id": "pkg-vision",
                            "runtime_target_id": "temms-x86_64-cpu",
                            "model_id": "model-1",
                            "result": {"latency_ms": {"p95": 1.2}},
                        }
                    ],
                    "count": 1,
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get(self, path, params=None):
                calls.append((path, params))
                return FakeResponse()

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "benchmarks",
                "--device-id",
                "edge-1",
                "--package-id",
                "pkg-vision",
                "--runtime-target-id",
                "temms-x86_64-cpu",
                "--json",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["benchmarks"][0]["benchmark_id"] == "benchmark-1"
        assert payload["benchmarks"][0]["device_id"] == "edge-1"
        assert calls == [
            (
                "/benchmarks",
                {
                    "device_id": "edge-1",
                    "package_id": "pkg-vision",
                    "runtime_target_id": "temms-x86_64-cpu",
                },
            )
        ]

    def test_hub_preview_compatibility_posts_preflight_request(self, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                calls.append((path, json))
                return FakeResponse(
                    {
                        "schema_version": "temms-rollout-compatibility/v1",
                        "compatible": True,
                        "failures": [],
                        "device": {
                            "device_id": "edge-rpi",
                            "profile": "rpi5-tflite",
                        },
                        "package": {
                            "package_id": "pkg-tflite",
                            "version": "1.0.0",
                        },
                        "runtime_target_id": "temms-rpi5-tflite",
                        "runtime_target": {
                            "runtime_target_id": "temms-rpi5-tflite",
                            "image": "temms/agent:runtime-rpi5-tflite",
                        },
                    }
                )

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "preview-compatibility",
                "--device-id",
                "edge-rpi",
                "--package-id",
                "pkg-tflite",
                "--runtime-target-id",
                "temms-rpi5-tflite",
            ],
        )

        assert result.exit_code == 0
        assert "Rollout compatibility compatible" in result.output
        assert "temms-rpi5-tflite" in result.output
        assert calls == [
            (
                "/compatibility/preview",
                {
                    "device_id": "edge-rpi",
                    "package_id": "pkg-tflite",
                    "runtime_target_id": "temms-rpi5-tflite",
                },
            )
        ]

    def test_hub_preview_compatibility_exits_nonzero_when_blocked(self, monkeypatch):
        class FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {
                    "schema_version": "temms-rollout-compatibility/v1",
                    "compatible": False,
                    "failures": ["model-tflite: missing runtimes: tflite_runtime"],
                    "device": {
                        "device_id": "edge-rpi",
                        "profile": "rpi5-tflite",
                    },
                    "package": {
                        "package_id": "pkg-tflite",
                        "version": "1.0.0",
                    },
                    "runtime_target": None,
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                return FakeResponse()

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "preview-compatibility",
                "--device-id",
                "edge-rpi",
                "--package-id",
                "pkg-tflite",
            ],
        )

        assert result.exit_code == 1
        assert "Rollout compatibility blocked" in result.output
        assert "missing runtimes: tflite_runtime" in result.output

    def test_hub_status_gets_deployment_status(self, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {
                    "devices": {"edge-1": {}},
                    "deployment_status": {"edge-1": {"state": "READY"}},
                    "rollouts": {},
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get(self, path):
                calls.append(path)
                return FakeResponse()

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(app, ["hub", "status", "--json"])

        assert result.exit_code == 0
        assert calls == ["/deployment-status"]
        assert json.loads(result.output)["deployment_status"]["edge-1"]["state"] == "READY"

    def test_hub_replay_telemetry_posts_bundle(self, temp_dir, monkeypatch):
        calls = []
        bundle_path = temp_dir / "telemetry-bundle.json"
        bundle_path.write_text(
            json.dumps(
                {
                    "schema_version": "temms-telemetry-bundle/v1",
                    "events": [{"event_id": "evt-1", "event_type": "rollout.activated"}],
                    "count": 1,
                }
            )
        )

        class FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {
                    "status": "success",
                    "replay": {"ingested": 1, "duplicates": 0},
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                calls.append((path, json))
                return FakeResponse()

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "replay-telemetry",
                str(bundle_path),
                "--device-id",
                "edge-1",
                "--actor",
                "operator:alice",
                "--json",
            ],
        )

        assert result.exit_code == 0
        assert calls == [
            (
                "/telemetry/replay",
                {
                    "bundle": {
                        "schema_version": "temms-telemetry-bundle/v1",
                        "events": [{"event_id": "evt-1", "event_type": "rollout.activated"}],
                        "count": 1,
                    },
                    "device_id": "edge-1",
                    "actor": "operator:alice",
                },
            )
        ]
        assert json.loads(result.output)["replay"]["ingested"] == 1

    def test_hub_rollback_posts_rollout_rollback(self, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {
                    "status": "rolled_back",
                    "rollout": {"rollout_id": "rollout-1", "state": "rolled_back"},
                    "model": "model-v1",
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                calls.append((path, json))
                return FakeResponse()

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "rollback",
                "rollout-1",
                "--actor",
                "operator:bob",
                "--reason",
                "bad latency",
                "--json",
            ],
        )

        assert result.exit_code == 0
        assert calls == [
            (
                "/rollouts/rollout-1/rollback",
                {"reason": "bad latency", "actor": "operator:bob"},
            )
        ]
        assert json.loads(result.output)["status"] == "rolled_back"

    def test_hub_apply_posts_actor(self, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {"status": "activated", "model": "model-v1"}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, path, json=None):
                calls.append((path, json))
                return FakeResponse()

        monkeypatch.setattr("httpx.Client", FakeClient)

        result = runner.invoke(
            app,
            [
                "hub",
                "apply",
                "rollout-1",
                "--require-signature",
                "--signing-key",
                "secret",
                "--actor",
                "edge:edge-1",
                "--json",
            ],
        )

        assert result.exit_code == 0
        assert calls == [
            (
                "/rollouts/rollout-1/apply",
                {
                    "require_signature": True,
                    "signing_key": "secret",
                    "actor": "edge:edge-1",
                },
            )
        ]
        assert json.loads(result.output)["status"] == "activated"


# ── slot create ──────────────────────────────────────────────────────


class TestSlotCreateCommand:
    """Test 'temms slot create' command."""

    def test_create_slot(self, temms_env):
        result = runner.invoke(
            app,
            [
                "slot",
                "create",
                "vision",
                "--description",
                "Vision slot",
                "--required",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "vision" in result.output.lower()

    def test_create_slot_with_candidates(self, temms_env):
        result = runner.invoke(
            app,
            [
                "slot",
                "create",
                "nav",
                "--description",
                "Navigation",
                "--candidates",
                "model-a,model-b",
                "--config",
                str(temms_env["config_path"]),
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
                "slot",
                "create",
                "vision",
                "--description",
                "Vision",
                "--config",
                str(temms_env["config_path"]),
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
                "slot",
                "status",
                "nonexistent",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_status_found(self, temms_env):
        runner.invoke(
            app,
            [
                "slot",
                "create",
                "vision",
                "--description",
                "Vision processing",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        result = runner.invoke(
            app,
            [
                "slot",
                "status",
                "vision",
                "--config",
                str(temms_env["config_path"]),
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
                "slot",
                "decisions",
                "--config",
                str(temms_env["config_path"]),
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
                "condition",
                "set",
                "platform.compute.cpu_temp_c",
                "72.5",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "condition set" in result.output.lower()

    def test_set_condition_string_value(self, temms_env):
        result = runner.invoke(
            app,
            [
                "condition",
                "set",
                "weather.precipitation",
                "fog",
                "--config",
                str(temms_env["config_path"]),
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
                "condition",
                "get",
                "nonexistent",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_get_existing(self, temms_env):
        # Set condition first
        runner.invoke(
            app,
            [
                "condition",
                "set",
                "temp",
                "72.5",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        result = runner.invoke(
            app,
            [
                "condition",
                "get",
                "temp",
                "--config",
                str(temms_env["config_path"]),
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
                "condition",
                "list",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "no conditions" in result.output.lower()

    def test_list_with_conditions(self, temms_env):
        runner.invoke(
            app,
            [
                "condition",
                "set",
                "temp",
                "72",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        result = runner.invoke(
            app,
            [
                "condition",
                "list",
                "--config",
                str(temms_env["config_path"]),
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
                "condition",
                "snapshot",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "Snapshot" in result.output

    def test_snapshot_with_data(self, temms_env):
        runner.invoke(
            app,
            [
                "condition",
                "set",
                "platform.cpu.temp",
                "60",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        result = runner.invoke(
            app,
            [
                "condition",
                "snapshot",
                "--config",
                str(temms_env["config_path"]),
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
                "condition",
                "clear-overrides",
                "--config",
                str(temms_env["config_path"]),
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
                "policy",
                "load",
                str(sample_policy_yaml),
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "thermal-adaptive" in result.output

    def test_load_policy_uses_configured_policy_dir(self, temms_env, sample_policy_yaml, temp_dir):
        active_policy_dir = temp_dir / "active-policies"
        config = Config.load(temms_env["config_path"])
        config.policy.policy_dir = active_policy_dir
        config.save(temms_env["config_path"])

        result = runner.invoke(
            app,
            [
                "policy",
                "load",
                str(sample_policy_yaml),
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0, result.output
        assert active_policy_dir.joinpath(sample_policy_yaml.name).exists()
        assert not temms_env["config_dir"].joinpath("policies", sample_policy_yaml.name).exists()
        assert str(active_policy_dir) in result.output
        assert "Copied to:" in result.output

    def test_load_policy_file_not_found(self, temms_env):
        result = runner.invoke(
            app,
            [
                "policy",
                "load",
                "/tmp/nonexistent-policy.yaml",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_load_policy_no_file_arg(self, temms_env):
        result = runner.invoke(
            app,
            [
                "policy",
                "load",
                "--config",
                str(temms_env["config_path"]),
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
                "policy",
                "list",
                "--config",
                str(temms_env["config_path"]),
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
                "policy",
                "list",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "thermal-adaptive" in result.output

    def test_list_uses_configured_policy_dir(self, temms_env, sample_policy_yaml, temp_dir):
        import shutil

        active_policy_dir = temp_dir / "active-policies"
        active_policy_dir.mkdir()
        config = Config.load(temms_env["config_path"])
        config.policy.policy_dir = active_policy_dir
        config.save(temms_env["config_path"])
        shutil.copy(sample_policy_yaml, active_policy_dir / "active.yaml")

        result = runner.invoke(
            app,
            [
                "policy",
                "list",
                "--config",
                str(temms_env["config_path"]),
            ],
        )

        assert result.exit_code == 0
        assert "thermal-adaptive" in result.output
