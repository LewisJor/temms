"""
Docker execution helpers for Hub Lite runtime targets.

Runtime targets describe container images that simulate an edge runtime stack.
The runner mounts a TEMMS package into the target image and asks the image to
validate that package against its declared device/runtime capabilities.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTAINER_PACKAGE_PATH = "/temms-input/package"
CONTAINER_SIGNING_KEY_PATH = "/temms-input/signing.key"


@dataclass(frozen=True)
class RuntimeTargetValidationResult:
    """Result from validating a package in a runtime target container."""

    runtime_target_id: str
    image: str
    package_path: str
    command: list[str]
    command_text: str
    dry_run: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.dry_run or self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable result metadata."""
        return {
            "runtime_target_id": self.runtime_target_id,
            "image": self.image,
            "package_path": self.package_path,
            "command": self.command,
            "command_text": self.command_text,
            "dry_run": self.dry_run,
            "exit_code": self.exit_code,
            "ok": self.ok,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def build_runtime_target_validation_command(
    runtime_target: dict[str, Any],
    package_path: Path,
    *,
    require_signature: bool = True,
    strict_metadata: bool = True,
    signing_key: str | None = None,
    signing_key_file: Path | None = None,
    pull_image: bool = False,
    validate_package_exists: bool = True,
) -> list[str]:
    """Build a docker command that validates a package inside a runtime target."""
    image = runtime_target.get("image")
    if not image:
        raise ValueError("Runtime target image is required")

    package_path = package_path.expanduser().resolve()
    if validate_package_exists and not package_path.exists():
        raise FileNotFoundError(f"Package not found: {package_path}")

    command = ["docker", "run", "--rm"]
    platform_value = _docker_platform(runtime_target)
    if platform_value:
        command.extend(["--platform", platform_value])
    if pull_image:
        command.extend(["--pull", "always"])

    target_id = runtime_target.get("runtime_target_id") or runtime_target.get("target_id")
    if target_id:
        command.extend(["-e", f"TEMMS_RUNTIME_TARGET_ID={target_id}"])

    device_profile = _first_device_profile(runtime_target)
    if device_profile:
        command.extend(["-e", f"TEMMS_DEVICE_PROFILE={device_profile}"])

    if signing_key:
        command.extend(["-e", f"TEMMS_PACKAGE_SIGNING_KEY={signing_key}"])
    if signing_key_file:
        host_key = signing_key_file.expanduser().resolve()
        if not host_key.exists():
            raise FileNotFoundError(f"Signing key file not found: {host_key}")
        command.extend(["-v", f"{host_key}:{CONTAINER_SIGNING_KEY_PATH}:ro"])

    command.extend(["-v", f"{package_path}:{CONTAINER_PACKAGE_PATH}:ro"])

    inner_command = [
        "temms",
        "package",
        "validate",
        CONTAINER_PACKAGE_PATH,
    ]
    if require_signature:
        inner_command.append("--require-signature")
        if signing_key:
            inner_command.extend(["--signing-key", signing_key])
        if signing_key_file:
            inner_command.extend(["--signing-key-file", CONTAINER_SIGNING_KEY_PATH])
    else:
        inner_command.append("--allow-unsigned-package")
    if device_profile:
        inner_command.extend(["--device-profile", device_profile])
    inner_command.append("--check-runtime")
    if strict_metadata:
        inner_command.append("--strict-metadata")

    command.append(str(image))
    command.extend(inner_command)
    return command


def validate_runtime_target_package(
    runtime_target: dict[str, Any],
    package_path: Path,
    *,
    require_signature: bool = True,
    strict_metadata: bool = True,
    signing_key: str | None = None,
    signing_key_file: Path | None = None,
    pull_image: bool = False,
    dry_run: bool = False,
    timeout_s: int = 300,
) -> RuntimeTargetValidationResult:
    """Validate a package by running the selected runtime target container."""
    command = build_runtime_target_validation_command(
        runtime_target,
        package_path,
        require_signature=require_signature,
        strict_metadata=strict_metadata,
        signing_key=signing_key,
        signing_key_file=signing_key_file,
        pull_image=pull_image,
        validate_package_exists=not dry_run,
    )
    command_text = shlex.join(command)
    target_id = runtime_target.get("runtime_target_id") or runtime_target.get("target_id") or ""
    image = str(runtime_target.get("image") or "")

    if dry_run:
        return RuntimeTargetValidationResult(
            runtime_target_id=target_id,
            image=image,
            package_path=str(package_path),
            command=command,
            command_text=command_text,
            dry_run=True,
        )

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return RuntimeTargetValidationResult(
        runtime_target_id=target_id,
        image=image,
        package_path=str(package_path),
        command=command,
        command_text=command_text,
        dry_run=False,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _docker_platform(runtime_target: dict[str, Any]) -> str | None:
    os_name = str(runtime_target.get("os") or "linux").lower()
    arch = str(runtime_target.get("arch") or "").lower()
    arch_aliases = {
        "x86_64": "amd64",
        "x64": "amd64",
        "aarch64": "arm64",
    }
    arch = arch_aliases.get(arch, arch)
    if not arch:
        return None
    return f"{os_name}/{arch}"


def _first_device_profile(runtime_target: dict[str, Any]) -> str | None:
    profiles = runtime_target.get("device_profiles") or []
    if not profiles:
        constraints = runtime_target.get("runtime_constraints") or {}
        profiles = constraints.get("device_profiles") or []
    if not profiles:
        return None
    return str(profiles[0])
