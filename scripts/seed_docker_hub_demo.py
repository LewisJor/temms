#!/usr/bin/env python3
"""Seed the local Docker Hub with a signed demo package.

The normal Docker stack should open directly into a product-ready Hub. This
script prepares that state without requiring the daemon HTTP server to already
be running.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from temms.core.package_archive import sign_package_artifact
from temms.hub_lite import HubLiteStore


PROMOTION_PATH = ("validated", "approved", "released")
DEMO_RUNTIME_TARGET_ID = "temms-x86_64-cpu"
DEMO_BENCHMARKS = {
    "model-yolov8-daylight-001": {
        "latency_ms": {"p50": 6.8, "p95": 9.4, "p99": 13.1},
        "throughput": {"inferences_per_second": 106.4},
    },
    "model-yolov8-lowlight-001": {
        "latency_ms": {"p50": 7.9, "p95": 11.2, "p99": 15.6},
        "throughput": {"inferences_per_second": 89.3},
    },
    "model-mobilenet-tiny-001": {
        "latency_ms": {"p50": 2.1, "p95": 3.8, "p99": 5.4},
        "throughput": {"inferences_per_second": 263.1},
    },
}


def seed_hub_demo(
    *,
    package_source: Path,
    data_dir: Path,
    hub_state_path: Path | None,
    signing_key: str,
    device_id: str,
    device_profile: str,
    site: str,
) -> dict[str, Any]:
    """Copy, sign, catalog, and release the demo package."""
    if not signing_key:
        raise ValueError("A signing key is required to seed the signed Docker demo")
    if not package_source.exists():
        raise ValueError(f"Demo package source does not exist: {package_source}")

    hub_state = hub_state_path or data_dir / "hub_lite.json"
    package_dir = data_dir / "packages" / "package-example-signed"
    package_dir.parent.mkdir(parents=True, exist_ok=True)
    if package_dir.exists():
        shutil.rmtree(package_dir)
    shutil.copytree(package_source, package_dir)

    sign_package_artifact(package_dir, signing_key, signer="temms-docker-demo")

    hub = HubLiteStore(hub_state)
    package = hub.upsert_package_from_source(
        package_dir,
        require_signature=True,
        signing_key=signing_key,
        device_profiles=[device_profile],
        strict_metadata=True,
        actor="operator:docker-seed",
    )
    package_id = str(package["package_id"])

    for state in PROMOTION_PATH:
        package = _promote_until(hub, package_id, state)

    device = hub.enroll_device(
        device_id,
        profile=device_profile,
        labels={"site": site, "source": "docker-demo", "simulated": "true"},
        inventory={
            "schema_version": "temms-device-inventory/v1",
            "simulated": True,
            "os": "linux",
            "arch": "amd64",
            "device_profile": device_profile,
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                }
            },
            "accelerators": {},
            "memory": {"total_mb": 8192.0, "available_mb": 4096.0},
            "storage": {"total_mb": 32768.0, "available_mb": 24576.0, "path": "/var/lib/temms"},
            "thermal": {"temperature_c": 42.0, "max_observed_c": 42.0},
            "power": {"source": "mains", "battery_percent": 100.0},
        },
    )
    device = hub.heartbeat(
        device_id,
        status="online",
        inventory=device["inventory"],
        deployment_status={
            "state": "READY",
            "source": "docker-demo-seed",
        },
    )
    validation = _ensure_runtime_validation(
        hub,
        package_id=package_id,
        package_path=package_dir,
    )
    benchmarks = _ensure_model_benchmarks(
        hub,
        package=package,
        package_id=package_id,
        device_id=device_id,
    )

    return {
        "hub_state_path": str(hub_state),
        "package_path": str(package_dir),
        "package_id": package_id,
        "package_state": package.get("promotion", {}).get("state"),
        "device_id": device["device_id"],
        "device_status": device.get("status"),
        "models": len(package.get("metadata", {}).get("models", [])),
        "runtime_validation_id": validation.get("validation_id"),
        "benchmarks": len(benchmarks),
    }


def _promote_until(hub: HubLiteStore, package_id: str, target_state: str) -> dict[str, Any]:
    package = hub.get_package(package_id)
    if package is None:
        raise ValueError(f"Unknown package after cataloging: {package_id}")

    current = package.get("promotion", {}).get("state") or "candidate"
    states = ("candidate", *PROMOTION_PATH)
    if current not in states:
        return package
    if states.index(current) >= states.index(target_state):
        return package

    return hub.promote_package(
        package_id,
        target_state,
        actor="operator:docker-seed",
        reason=f"docker demo seed promoted package to {target_state}",
    )


def _ensure_runtime_validation(
    hub: HubLiteStore,
    *,
    package_id: str,
    package_path: Path,
) -> dict[str, Any]:
    package = hub.get_package(package_id)
    expected_source_sha = (
        package.get("source_sha256") or package.get("sha256") if package else None
    )
    for record in hub.list_runtime_validations(
        package_id=package_id,
        runtime_target_id=DEMO_RUNTIME_TARGET_ID,
    ):
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        if (
            record.get("actor") == "operator:docker-seed"
            and result.get("ok") is True
            and result.get("dry_run") is False
            and (
                expected_source_sha is None
                or record.get("source_sha256") == expected_source_sha
            )
        ):
            return record

    return hub.record_runtime_validation(
        DEMO_RUNTIME_TARGET_ID,
        {
            "runtime_target_id": DEMO_RUNTIME_TARGET_ID,
            "image": "temms/agent:inference-amd64",
            "package_path": str(package_path),
            "command": [
                "temms",
                "package",
                "validate",
                str(package_path),
                "--check-runtime",
                "--strict-metadata",
            ],
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
            "stdout": "Docker demo runtime validation passed for ONNX Runtime CPU.",
            "stderr": "",
        },
        package_id=package_id,
        package_path=str(package_path),
        actor="operator:docker-seed",
    )


def _ensure_model_benchmarks(
    hub: HubLiteStore,
    *,
    package: dict[str, Any],
    package_id: str,
    device_id: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    models = package.get("metadata", {}).get("models", [])
    if not isinstance(models, list):
        return records

    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or "")
        if not model_id:
            continue
        for seeded in _seeded_benchmarks(
            hub,
            device_id=device_id,
            package_id=package_id,
            model_id=model_id,
        ):
            hub.delete_benchmark(str(seeded.get("benchmark_id") or ""))
        records.append(
            hub.record_benchmark(
                _benchmark_result(model),
                device_id=device_id,
                package_id=package_id,
                runtime_target_id=DEMO_RUNTIME_TARGET_ID,
                actor=f"edge:{device_id}",
            )
        )
    return records


def _seeded_benchmarks(
    hub: HubLiteStore,
    *,
    device_id: str,
    package_id: str,
    model_id: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in hub.list_benchmarks(
        device_id=device_id,
        package_id=package_id,
        runtime_target_id=DEMO_RUNTIME_TARGET_ID,
        model_id=model_id,
    ):
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        if record.get("actor") == f"edge:{device_id}" and result.get("demo_seed") is True:
            records.append(record)
    return records


def _benchmark_result(model: dict[str, Any]) -> dict[str, Any]:
    model_id = str(model.get("id") or "")
    profile = DEMO_BENCHMARKS.get(
        model_id,
        {
            "latency_ms": {"p50": 8.0, "p95": 12.0, "p99": 16.0},
            "throughput": {"inferences_per_second": 83.0},
        },
    )
    return {
        "schema_version": "temms-benchmark/v1",
        "demo_seed": True,
        "model_id": model_id,
        "model_name": model.get("name"),
        "slot": "vision",
        "runtime": "onnxruntime",
        "provider": "CPUExecutionProvider",
        "input_shape": [1, 3, 640, 640],
        "warmup_runs": 5,
        "sample_count": 100,
        "latency_ms": profile["latency_ms"],
        "throughput": profile["throughput"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-source", type=Path, default=Path("/app/examples/package-example"))
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/temms"))
    parser.add_argument("--hub-state-path", type=Path)
    parser.add_argument("--signing-key", default=os.environ.get("TEMMS_PACKAGE_SIGNING_KEY", ""))
    parser.add_argument("--device-id", default=os.environ.get("TEMMS_DEMO_DEVICE_ID", "edge-sim"))
    parser.add_argument("--device-profile", default=os.environ.get("TEMMS_DEVICE_PROFILE", "x86_64-cpu"))
    parser.add_argument("--site", default=os.environ.get("TEMMS_DEMO_SITE", "local-lab"))
    args = parser.parse_args()

    summary = seed_hub_demo(
        package_source=args.package_source,
        data_dir=args.data_dir,
        hub_state_path=args.hub_state_path,
        signing_key=args.signing_key,
        device_id=args.device_id,
        device_profile=args.device_profile,
        site=args.site,
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
