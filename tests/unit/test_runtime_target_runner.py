from pathlib import Path
from types import SimpleNamespace

import pytest

from temms.core.runtime_target_runner import (
    CONTAINER_PACKAGE_PATH,
    CONTAINER_SIGNING_KEY_PATH,
    build_runtime_target_validation_command,
    validate_runtime_target_package,
)


def test_build_runtime_target_validation_command_mounts_package_and_key(temp_dir):
    package_dir = temp_dir / "package.temms"
    package_dir.mkdir()
    signing_key_file = temp_dir / "signing.key"
    signing_key_file.write_text("secret", encoding="utf-8")

    command = build_runtime_target_validation_command(
        {
            "runtime_target_id": "customer-orin",
            "image": "registry.example.com/customer/orin-runtime:2026.06",
            "os": "linux",
            "arch": "arm64",
            "device_profiles": ["orin-tensorrt"],
        },
        package_dir,
        signing_key_file=signing_key_file,
        pull_image=True,
    )

    assert command[:6] == [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/arm64",
        "--pull",
    ]
    assert "always" in command
    assert f"TEMMS_RUNTIME_TARGET_ID=customer-orin" in command
    assert f"TEMMS_DEVICE_PROFILE=orin-tensorrt" in command
    assert f"{package_dir.resolve()}:{CONTAINER_PACKAGE_PATH}:ro" in command
    assert f"{signing_key_file.resolve()}:{CONTAINER_SIGNING_KEY_PATH}:ro" in command
    image_index = command.index("registry.example.com/customer/orin-runtime:2026.06")
    assert command[image_index + 1 :] == [
        "temms",
        "package",
        "validate",
        CONTAINER_PACKAGE_PATH,
        "--require-signature",
        "--signing-key-file",
        CONTAINER_SIGNING_KEY_PATH,
        "--device-profile",
        "orin-tensorrt",
        "--check-runtime",
        "--strict-metadata",
    ]


def test_build_runtime_target_validation_command_passes_inline_signing_key(temp_dir):
    package_dir = temp_dir / "package.temms"
    package_dir.mkdir()

    command = build_runtime_target_validation_command(
        {
            "runtime_target_id": "x86",
            "image": "temms/agent:inference-amd64",
            "arch": "x86_64",
            "runtime_constraints": {"device_profiles": ["x86_64-cpu"]},
        },
        package_dir,
        signing_key="secret",
    )

    assert "--platform" in command
    assert "linux/amd64" in command
    assert "TEMMS_PACKAGE_SIGNING_KEY=secret" in command
    assert "--signing-key" in command
    assert "secret" in command
    assert "--device-profile" in command
    assert "x86_64-cpu" in command
    assert "--strict-metadata" in command


def test_build_runtime_target_validation_command_can_disable_strict_metadata(temp_dir):
    package_dir = temp_dir / "package.temms"
    package_dir.mkdir()

    command = build_runtime_target_validation_command(
        {"runtime_target_id": "x86", "image": "temms/agent:inference-amd64"},
        package_dir,
        strict_metadata=False,
    )

    assert "--check-runtime" in command
    assert "--strict-metadata" not in command


def test_validate_runtime_target_package_dry_run_does_not_run_docker(temp_dir, monkeypatch):
    package_dir = temp_dir / "package.temms"
    package_dir.mkdir()

    def fail_run(*args, **kwargs):
        raise AssertionError("docker should not run during dry-run")

    monkeypatch.setattr("subprocess.run", fail_run)

    result = validate_runtime_target_package(
        {"runtime_target_id": "target-1", "image": "temms/agent:test", "arch": "amd64"},
        package_dir,
        dry_run=True,
    )

    assert result.ok is True
    assert result.dry_run is True
    assert result.exit_code is None
    assert result.command[0:3] == ["docker", "run", "--rm"]


def test_validate_runtime_target_package_dry_run_allows_missing_package(temp_dir, monkeypatch):
    missing_package = temp_dir / "missing.temms.tar.zst"

    def fail_run(*args, **kwargs):
        raise AssertionError("docker should not run during dry-run")

    monkeypatch.setattr("subprocess.run", fail_run)

    result = validate_runtime_target_package(
        {"runtime_target_id": "target-1", "image": "temms/agent:test", "arch": "amd64"},
        missing_package,
        dry_run=True,
        require_signature=False,
    )

    assert result.ok is True
    assert result.dry_run is True
    assert str(missing_package.resolve()) in result.command_text
    assert "--allow-unsigned-package" in result.command


def test_validate_runtime_target_package_executes_docker(temp_dir, monkeypatch):
    package_dir = temp_dir / "package.temms"
    package_dir.mkdir()
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=7, stdout="out", stderr="err")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = validate_runtime_target_package(
        {"runtime_target_id": "target-1", "image": "temms/agent:test"},
        package_dir,
        dry_run=False,
        timeout_s=12,
    )

    assert result.ok is False
    assert result.exit_code == 7
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert seen["command"] == result.command
    assert seen["kwargs"]["timeout"] == 12
    assert seen["kwargs"]["capture_output"] is True


def test_build_runtime_target_validation_command_requires_existing_package(temp_dir):
    with pytest.raises(FileNotFoundError):
        build_runtime_target_validation_command(
            {"runtime_target_id": "target-1", "image": "temms/agent:test"},
            Path(temp_dir / "missing.temms"),
        )
