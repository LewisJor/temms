"""
Tests for Hub Lite state merge behavior.
"""

import base64
import hashlib
import json
from pathlib import Path

import pytest

from temms.core.signing import sign_package
from temms.hub_lite import HubLiteStore


def test_runtime_target_catalog_includes_defaults_and_byo_targets(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")

    defaults = store.list_runtime_targets()

    assert {target["runtime_target_id"] for target in defaults} >= {
        "temms-x86_64-cpu",
        "temms-arm64-jetson",
        "temms-rpi5-tflite",
        "temms-orin-tensorrt",
    }

    target = store.upsert_runtime_target(
        {
            "runtime_target_id": "customer-orin",
            "name": "Customer Orin",
            "image": "registry.example.com/edge/orin-runtime:2026.06",
            "os": "linux",
            "arch": "arm64",
            "device_profiles": ["jetson-orin"],
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CUDAExecutionProvider"],
                }
            },
            "accelerators": {"nvidia": {"available": True}},
        },
        actor="operator:alice",
    )

    assert target["device_profiles"] == ["orin-tensorrt"]
    assert target["metadata"]["audit"]["catalog_actor"] == "operator:alice"
    assert store.get_runtime_target("customer-orin")["image"].startswith("registry.example.com/")


def test_package_source_registration_requires_signature_by_default(temp_dir):
    """Hub source registration should be signing-first unless a lab opts out."""
    store = HubLiteStore(temp_dir / "hub.json")
    package_dir = _minimal_package(temp_dir / "pkg-source-default.temms", b"source-default")

    with pytest.raises(ValueError, match="Signature verification requires a signing key"):
        store.upsert_package_from_source(package_dir)

    assert store.list_packages() == []

    sign_package(package_dir, "hub-key", signer="unit-test-hub")

    package = store.upsert_package_from_source(
        package_dir,
        signing_key="hub-key",
        actor="operator:test",
    )

    assert package["package_id"] == "pkg-source-default"
    assert package["updated_by"] == "operator:test"
    assert package["metadata"]["validation"]["signature_verified"] is True
    assert store.get_package("pkg-source-default")["metadata"]["validation"][
        "signature"
    ]["signer"] == "unit-test-hub"


def test_rollout_assignment_records_runtime_target_and_blocks_mismatch(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "metadata": {
                "models": [
                    {
                        "id": "model-vision",
                        "runtime_constraints": {
                            "runtimes": ["onnxruntime"],
                            "providers": ["CPUExecutionProvider"],
                        },
                    }
                ]
            },
        }
    )

    rollout = store.assign_rollout(
        "edge-1",
        "pkg-vision",
        slot="vision",
        rollout_id="rollout-runtime",
        runtime_target_id="temms-x86_64-cpu",
    )

    assert rollout["runtime_target_id"] == "temms-x86_64-cpu"
    assert rollout["runtime_target"]["image"] == "temms/agent:inference-amd64"

    store.upsert_runtime_target(
        {
            "runtime_target_id": "bad-arm-target",
            "image": "registry.example.com/edge/arm64:latest",
            "device_profiles": ["arm64-jetson"],
            "runtimes": {"onnxruntime": {"available": True}},
        }
    )

    with pytest.raises(ValueError, match="Runtime target bad-arm-target"):
        store.assign_rollout(
            "edge-1",
            "pkg-vision",
            slot="vision",
            rollout_id="rollout-bad-runtime",
            runtime_target_id="bad-arm-target",
        )


def test_deployment_draft_persists_active_mission_and_airgap_import(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
        }
    )

    draft = store.upsert_deployment_draft(
        package_id="pkg-vision",
        runtime_target_id="temms-x86_64-cpu",
        device_id="edge-1",
        slot="vision",
        actor="operator:test",
    )

    assert draft["schema_version"] == "temms-deployment-draft/v1"
    assert draft["draft_id"] == "active"
    assert draft["package_id"] == "pkg-vision"
    assert draft["device_id"] == "edge-1"
    assert draft["runtime_target_id"] == "temms-x86_64-cpu"
    assert draft["runtime_target"]["image"] == "temms/agent:inference-amd64"
    assert draft["actor"] == "operator:test"
    assert store.get_deployment_draft()["slot"] == "vision"

    imported = HubLiteStore(temp_dir / "imported-hub.json")
    counts = imported.import_bundle(store.export_bundle())

    assert counts["deployment_drafts"] == 1
    imported_draft = imported.get_deployment_draft()
    assert imported_draft["package_id"] == "pkg-vision"
    assert imported_draft["runtime_target_id"] == "temms-x86_64-cpu"


def test_rollout_compatibility_preview_is_side_effect_free(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device(
        "edge-rpi",
        profile="rpi5-tflite",
        inventory={"runtimes": {"tflite_runtime": {"available": True}}},
    )
    store.upsert_package(
        {
            "package_id": "pkg-tflite",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["rpi5-tflite"],
            "metadata": {
                "models": [
                    {
                        "id": "model-tflite",
                        "runtime_constraints": {"runtimes": ["tflite_runtime"]},
                    }
                ]
            },
        }
    )

    compatible = store.preview_rollout_compatibility("edge-rpi", "pkg-tflite")

    assert compatible["schema_version"] == "temms-rollout-compatibility/v1"
    assert compatible["compatible"] is True
    assert compatible["failures"] == []
    assert compatible["device"]["profile"] == "rpi5-tflite"
    assert compatible["runtime_target"] is None
    assert compatible["package"]["runtime_constraints"] == [
        {
            "model_id": "model-tflite",
            "constraints": {"runtimes": ["tflite_runtime"]},
        }
    ]
    assert store.list_rollouts() == []

    store.upsert_runtime_target(
        {
            "runtime_target_id": "x86-only",
            "image": "registry.example.com/x86:latest",
            "device_profiles": ["x86_64-cpu"],
            "runtimes": {"onnxruntime": {"available": True}},
        }
    )

    blocked = store.preview_rollout_compatibility(
        "edge-rpi",
        "pkg-tflite",
        runtime_target_id="x86-only",
    )

    assert blocked["compatible"] is False
    assert any("device profile rpi5-tflite" in failure for failure in blocked["failures"])
    assert any("missing runtimes: tflite_runtime" in failure for failure in blocked["failures"])
    assert store.list_rollouts() == []


def test_runtime_validation_records_redact_signing_key_and_export(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "a" * 64,
        }
    )

    record = store.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "package_path": "/tmp/pkg.temms.tar.zst",
            "command": [
                "docker",
                "run",
                "-e",
                "TEMMS_PACKAGE_SIGNING_KEY=secret",
                "temms/agent:inference-amd64",
                "temms",
                "package",
                "validate",
                "/temms-input/package",
                "--signing-key",
                "secret",
            ],
            "dry_run": True,
            "exit_code": None,
            "ok": True,
        },
        package_id="pkg-vision",
        actor="operator:alice",
    )

    assert record["validation_id"].startswith("runtime-validation-")
    assert record["actor"] == "operator:alice"
    assert record["source_sha256"] == "a" * 64
    assert "secret" not in " ".join(record["result"]["command"])
    assert "secret" not in record["result"]["command_text"]
    assert "TEMMS_PACKAGE_SIGNING_KEY=********" in record["result"]["command"]
    assert record["result"]["command"][-1] == "********"
    assert store.list_runtime_validations(package_id="pkg-vision")[0]["validation_id"] == (
        record["validation_id"]
    )
    exported = store.export_bundle()
    assert record["validation_id"] in exported["hub_lite"]["runtime_validations"]


def test_benchmark_records_filter_and_airgap_export(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "c" * 64,
        }
    )

    record = store.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-vision",
            "slot": "vision",
            "latency_ms": {"p50": 4.0, "p95": 8.0},
            "throughput": {"inferences_per_second": 125.0},
        },
        device_id="edge-1",
        package_id="pkg-vision",
        runtime_target_id="temms-x86_64-cpu",
        actor="edge:edge-1",
    )

    assert record["benchmark_id"].startswith("benchmark-")
    assert record["device"]["profile"] == "x86_64-cpu"
    assert record["package"]["package_id"] == "pkg-vision"
    assert record["source_sha256"] == "c" * 64
    assert record["runtime_target"]["image"] == "temms/agent:inference-amd64"
    assert record["result"]["latency_ms"]["p95"] == 8.0
    assert store.list_benchmarks(device_id="edge-1")[0]["benchmark_id"] == (record["benchmark_id"])
    assert store.list_benchmarks(package_id="missing") == []
    exported = store.export_bundle()
    assert record["benchmark_id"] in exported["hub_lite"]["benchmarks"]

    imported = HubLiteStore(temp_dir / "imported-hub.json")
    counts = imported.import_bundle(exported)
    assert counts["benchmarks"] == 1
    assert imported.list_benchmarks(model_id="model-vision")[0]["benchmark_id"] == (
        record["benchmark_id"]
    )


def test_rollout_assignment_can_require_passing_runtime_validation(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    store.upsert_package(
        {
            "package_id": "pkg-vision",
            "name": "vision",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "sha256": "b" * 64,
            "metadata": {
                "models": [
                    {
                        "id": "model-vision",
                        "runtime_constraints": {
                            "runtimes": ["onnxruntime"],
                            "providers": ["CPUExecutionProvider"],
                        },
                    }
                ]
            },
        }
    )

    with pytest.raises(ValueError, match="No passing runtime validation"):
        store.assign_rollout(
            "edge-1",
            "pkg-vision",
            slot="vision",
            runtime_target_id="temms-x86_64-cpu",
            require_runtime_validation=True,
        )

    store.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "package_path": "/tmp/pkg-vision.temms.tar.zst",
            "command": ["docker", "run", "temms/agent:inference-amd64"],
            "dry_run": True,
            "ok": True,
        },
        package_id="pkg-vision",
        actor="operator:preview",
    )

    with pytest.raises(ValueError, match="No passing runtime validation"):
        store.assign_rollout(
            "edge-1",
            "pkg-vision",
            slot="vision",
            runtime_target_id="temms-x86_64-cpu",
            require_runtime_validation=True,
        )

    validation = store.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "package_path": "/tmp/pkg-vision.temms.tar.zst",
            "command": ["docker", "run", "temms/agent:inference-amd64"],
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-vision",
        actor="operator:alice",
    )

    rollout = store.assign_rollout(
        "edge-1",
        "pkg-vision",
        slot="vision",
        rollout_id="rollout-gated",
        runtime_target_id="temms-x86_64-cpu",
        require_runtime_validation=True,
    )

    assert rollout["runtime_validation_required"] is True
    assert rollout["runtime_validation"]["validation_id"] == validation["validation_id"]
    assert rollout["runtime_validation"]["dry_run"] is False
    assert rollout["runtime_validation"]["ok"] is True


def test_state_write_failure_preserves_previous_hub_lite_state(temp_dir, monkeypatch):
    store = HubLiteStore(temp_dir / "hub.json")
    store.enroll_device("edge-1", profile="x86_64-cpu")
    previous_payload = store.path.read_text(encoding="utf-8")
    original_replace = Path.replace

    def fail_state_replace(path, target):
        if Path(target) == store.path:
            raise OSError("simulated replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_state_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        store.heartbeat("edge-1", status="online")

    assert store.path.read_text(encoding="utf-8") == previous_payload
    device = store.get_device("edge-1")
    assert device["profile"] == "x86_64-cpu"
    assert "status" not in device
    assert not list(temp_dir.glob(".hub.json-*"))


def test_airgap_import_preserves_newer_local_rollout_state(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    stale_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {},
            "rollouts": {
                "rollout-1": {
                    "rollout_id": "rollout-1",
                    "device_id": "edge-1",
                    "package_id": "pkg-1",
                    "slot": "vision",
                    "state": "assigned",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "history": [
                        {
                            "state": "assigned",
                            "updated_at": "2026-01-01T00:00:00Z",
                            "detail": "assigned centrally",
                            "actor": "operator:alice",
                        }
                    ],
                }
            },
            "deployment_status": {
                "edge-1": {
                    "device_id": "edge-1",
                    "state": "ASSIGNED",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            },
        },
    }
    newer_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {},
            "rollouts": {
                "rollout-1": {
                    "rollout_id": "rollout-1",
                    "device_id": "edge-1",
                    "package_id": "pkg-1",
                    "slot": "vision",
                    "state": "activated",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:05:00Z",
                    "history": [
                        {
                            "state": "activated",
                            "updated_at": "2026-01-01T00:05:00Z",
                            "detail": "loaded locally",
                            "actor": "edge:edge-1",
                        }
                    ],
                }
            },
            "deployment_status": {
                "edge-1": {
                    "device_id": "edge-1",
                    "state": "READY",
                    "updated_at": "2026-01-01T00:05:00Z",
                }
            },
        },
    }

    store.import_bundle(newer_bundle)
    counts = store.import_bundle(stale_bundle)

    rollout = store.get_rollout("rollout-1")
    assert counts["rollouts"] == 1
    assert rollout["state"] == "activated"
    assert [event["state"] for event in rollout["history"]] == [
        "assigned",
        "activated",
    ]
    deployment = store.deployment_status()["deployment_status"]["edge-1"]
    assert deployment["state"] == "READY"


def test_airgap_import_accepts_newer_rollout_state(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    old_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {},
            "rollouts": {
                "rollout-1": {
                    "rollout_id": "rollout-1",
                    "state": "assigned",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "history": [
                        {
                            "state": "assigned",
                            "updated_at": "2026-01-01T00:00:00Z",
                        }
                    ],
                }
            },
        },
    }
    new_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {},
            "rollouts": {
                "rollout-1": {
                    "rollout_id": "rollout-1",
                    "state": "rolled_back",
                    "updated_at": "2026-01-01T00:10:00Z",
                    "history": [
                        {
                            "state": "rolled_back",
                            "updated_at": "2026-01-01T00:10:00Z",
                        }
                    ],
                }
            },
        },
    }

    store.import_bundle(old_bundle)
    store.import_bundle(new_bundle)

    rollout = store.get_rollout("rollout-1")
    assert rollout["state"] == "rolled_back"
    assert [event["state"] for event in rollout["history"]] == [
        "assigned",
        "rolled_back",
    ]


def test_airgap_import_skips_stale_package_artifact(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")

    def bundle(updated_at: str, content: bytes) -> dict:
        artifact_sha = hashlib.sha256(content).hexdigest()
        return {
            "schema_version": "temms-hub-lite-bundle/v1",
            "hub_lite": {
                "devices": {},
                "packages": {
                    "pkg-1": {
                        "package_id": "pkg-1",
                        "name": "vision",
                        "version": "1",
                        "updated_at": updated_at,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                },
                "rollouts": {},
                "deployment_status": {},
            },
            "package_artifacts": {
                "pkg-1": {
                    "filename": "pkg-1.temms.tar.zst",
                    "sha256": artifact_sha,
                    "source_sha256": artifact_sha,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
            },
        }

    imported = store.import_bundle(bundle("2026-01-01T00:05:00Z", b"new-package"))
    assert imported["package_artifacts"] == 1
    package = store.get_package("pkg-1")
    package_path = package["path"]
    assert (temp_dir / "packages" / "pkg-1.temms.tar.zst").read_bytes() == b"new-package"

    stale = store.import_bundle(bundle("2026-01-01T00:00:00Z", b"old-package"))

    package = store.get_package("pkg-1")
    assert stale["package_artifacts"] == 0
    assert stale["package_artifacts_skipped"] == 1
    assert package["updated_at"] == "2026-01-01T00:05:00Z"
    assert package["path"] == package_path
    assert (temp_dir / "packages" / "pkg-1.temms.tar.zst").read_bytes() == b"new-package"


def test_airgap_import_hash_mismatch_preserves_existing_package_file(temp_dir):
    store = HubLiteStore(temp_dir / "hub.json")
    existing_content = b"known-good-package"
    existing_sha = hashlib.sha256(existing_content).hexdigest()
    good_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {
                "pkg-1": {
                    "package_id": "pkg-1",
                    "name": "vision",
                    "version": "1",
                    "updated_at": "2026-01-01T00:05:00Z",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            },
            "rollouts": {},
            "deployment_status": {},
        },
        "package_artifacts": {
            "pkg-1": {
                "filename": "pkg-1.temms.tar.zst",
                "sha256": existing_sha,
                "source_sha256": existing_sha,
                "content_base64": base64.b64encode(existing_content).decode("ascii"),
            }
        },
    }
    store.import_bundle(good_bundle)
    package_path = temp_dir / "packages" / "pkg-1.temms.tar.zst"

    bad_content = b"corrupted-transfer"
    expected_new_sha = hashlib.sha256(b"different-new-package").hexdigest()
    bad_bundle = {
        "schema_version": "temms-hub-lite-bundle/v1",
        "hub_lite": {
            "devices": {},
            "packages": {
                "pkg-1": {
                    "package_id": "pkg-1",
                    "name": "vision",
                    "version": "2",
                    "updated_at": "2026-01-01T00:10:00Z",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            },
            "rollouts": {},
            "deployment_status": {},
        },
        "package_artifacts": {
            "pkg-1": {
                "filename": "pkg-1.temms.tar.zst",
                "sha256": expected_new_sha,
                "source_sha256": expected_new_sha,
                "content_base64": base64.b64encode(bad_content).decode("ascii"),
            }
        },
    }

    with pytest.raises(ValueError, match="Package artifact hash mismatch"):
        store.import_bundle(bad_bundle)

    package = store.get_package("pkg-1")
    assert package["version"] == "1"
    assert package["path"] == str(package_path)
    assert package_path.read_bytes() == existing_content


def _minimal_package(package_dir: Path, model_bytes: bytes) -> Path:
    """Create a small valid directory package for Hub Lite source-registration tests."""
    models_dir = package_dir / "models"
    models_dir.mkdir(parents=True)
    model_path = models_dir / "model.onnx"
    model_path.write_bytes(model_bytes)
    manifest = {
        "schema_version": "v1",
        "package_id": "pkg-source-default",
        "name": "source-default",
        "version": "1.0.0",
        "created_at": "2026-01-01T00:00:00Z",
        "models": [
            {
                "id": "model-source-default",
                "name": "source-default",
                "version": "1.0.0",
                "format": "onnx",
                "filename": "model.onnx",
                "sha256": hashlib.sha256(model_bytes).hexdigest(),
                "size_bytes": len(model_bytes),
            }
        ],
        "policies": [],
        "compatibility": {"device_profiles": ["x86_64-cpu"]},
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return package_dir
