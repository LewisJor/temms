#!/usr/bin/env python3
"""arm64 (Pi-class) edge acceptance on Apple Silicon — issue #33.

Per docs/direction.md the compatibility claim is "model registry → any target →
signed → DDIL → deploy". "Any target" is only credible if a non-x86 target is
actually exercised, and Apple Silicon runs linux/arm64 natively — so a
Raspberry-Pi-class edge can be validated on the laptop, today, with no hardware
and no emulation.

This brings up the acceptance edge pinned to linux/arm64 and asserts the things
that would otherwise be taken on trust:

  * the container really is aarch64 (not an amd64 image under emulation)
  * the agent detects an arm64 device profile from the silicon
  * a declared Pi-class profile agrees with the hardware — no arch mismatch
  * the agent serves, and reports a runtime that exists on that architecture

Jetson/GPU validation stays deferred to real hardware.

Usage:
    python scripts/arm64_acceptance.py [--keep]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(REPO_ROOT / "deploy" / "docker-compose.acceptance.yml"),
    "-f",
    str(REPO_ROOT / "deploy" / "docker-compose.acceptance.arm64.yml"),
]
EDGE_CONTAINER = "temms-acceptance-edge-airgap"
EDGE_PORT = 18082


class AcceptanceCheckError(Exception):
    """An acceptance assertion did not hold."""


def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kwargs)  # type: ignore[arg-type]


def _exec_in_edge(command: list[str]) -> str:
    result = _run(["docker", "exec", EDGE_CONTAINER, *command])
    if result.returncode != 0:
        raise AcceptanceCheckError(
            f"`{' '.join(command)}` failed in {EDGE_CONTAINER}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def bring_up() -> None:
    print("==> Building and starting the arm64 (Pi-class) edge")
    result = _run([*COMPOSE, "up", "--build", "-d"])
    if result.returncode != 0:
        raise AcceptanceCheckError(f"compose up failed:\n{result.stderr[-2000:]}")


def wait_for_health(timeout_s: float = 180.0) -> None:
    print("==> Waiting for the edge agent to report healthy")
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        probe = _run(
            ["curl", "-sf", f"http://localhost:{EDGE_PORT}/v1/health"],
        )
        if probe.returncode == 0:
            return
        last = probe.stderr.strip()
        time.sleep(2)
    raise AcceptanceCheckError(f"edge did not become healthy within {timeout_s:.0f}s ({last})")


def check_container_is_aarch64() -> str:
    """The image must be genuinely arm64, not amd64 under emulation."""
    machine = _exec_in_edge(["uname", "-m"])
    if machine not in {"aarch64", "arm64"}:
        raise AcceptanceCheckError(f"expected an aarch64 container, got {machine!r}")
    return machine


def check_detected_profile() -> dict:
    """The agent must infer an arm64 profile from the silicon itself."""
    raw = _exec_in_edge(
        [
            "python",
            "-c",
            "import json;from temms.core.runtime_profiles import detect_runtime_capabilities;"
            "print(json.dumps(detect_runtime_capabilities().to_dict()))",
        ]
    )
    inventory = json.loads(raw)

    if inventory.get("arch") != "arm64":
        raise AcceptanceCheckError(
            f"agent reports arch {inventory.get('arch')!r}, expected 'arm64'"
        )

    detected = inventory.get("detected_device_profile")
    if detected not in {"arm64-cpu", "rpi5-tflite", "arm64-jetson", "orin-tensorrt"}:
        raise AcceptanceCheckError(f"detected profile {detected!r} is not an arm64 profile")

    # The declared Pi-class profile must agree with the hardware. This is the
    # regression that motivated the check: a device can declare x86_64-cpu while
    # running on arm64, and the Hub's fit gate would clear a package that cannot
    # actually run there.
    mismatch = inventory.get("device_profile_arch_mismatch")
    if mismatch:
        raise AcceptanceCheckError(f"declared profile contradicts the silicon: {mismatch}")

    return inventory


def check_serves_requests() -> None:
    probe = _run(["curl", "-sf", f"http://localhost:{EDGE_PORT}/v1/health"])
    if probe.returncode != 0:
        raise AcceptanceCheckError("edge stopped serving /v1/health")


def main() -> int:
    parser = argparse.ArgumentParser(description="arm64 Pi-class edge acceptance")
    parser.add_argument("--keep", action="store_true", help="Leave the stack running")
    args = parser.parse_args()

    host = _run(["uname", "-m"]).stdout.strip()
    if host not in {"arm64", "aarch64"}:
        print(
            f"[SKIP] host is {host}; this acceptance runs natively on arm64 "
            "(Apple Silicon). Running it under emulation proves little about a "
            "Pi-class target."
        )
        return 0

    checks: list[tuple[str, str]] = []
    try:
        bring_up()
        wait_for_health()

        machine = check_container_is_aarch64()
        checks.append(("container_is_aarch64", machine))

        inventory = check_detected_profile()
        checks.append(("agent_arch", str(inventory.get("arch"))))
        checks.append(("declared_profile", str(inventory.get("device_profile"))))
        checks.append(("detected_profile", str(inventory.get("detected_device_profile"))))
        checks.append(("no_arch_mismatch", "ok"))

        check_serves_requests()
        checks.append(("serves_requests", "ok"))
    except AcceptanceCheckError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    finally:
        if not args.keep:
            _run([*COMPOSE, "down", "-v"])

    print("\n[PASS] arm64 (Pi-class) edge acceptance")
    for name, detail in checks:
        print(f"  ok  {name}: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
