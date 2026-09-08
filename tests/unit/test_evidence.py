"""
Evidence bundle tests.
"""

import json
from types import SimpleNamespace

from temms.core.cache import ModelFormat
from temms.evidence import (
    EvidenceBundleBuilder,
    build_mission_replay,
    combined_timeline,
    runtime_fit_evidence_timeline,
    summarize_evidence_bundle,
)
from temms.hub_lite import HubLiteStore


def test_evidence_bundle_enriches_decision_with_model_and_package(
    model_cache,
    model_storage,
    slot_manager,
    condition_store,
    policy_engine,
    sample_model_file,
):
    package = model_cache.add_package(
        package_id="pkg-vision",
        name="vision-package",
        version="1.0.0",
        source="/mnt/usb/pkg-vision",
        manifest={"package_id": "pkg-vision", "signature_verified": True},
    )
    dest_path, sha256, size = model_storage.store_model(
        sample_model_file,
        "model-lowlight-v1",
        verify=True,
    )
    model_cache.add_cached_model(
        model_id="model-lowlight-v1",
        name="lowlight",
        version="1.0.0",
        format=ModelFormat.ONNX,
        path=dest_path,
        sha256=sha256,
        size_bytes=size,
        package_id=package.id,
        metadata={"runtime_constraints": {"runtimes": ["onnxruntime"]}},
    )
    slot_manager.create_slot(
        name="vision",
        description="Vision",
        required=True,
        default_model="daylight",
    )
    condition_store.set(
        path="environmental.visibility_m",
        value=40,
        source="operator",
        priority=1000,
    )
    slot_manager.activate_model(
        slot_name="vision",
        model_id="model-lowlight-v1",
        trigger_type="policy",
        trigger_detail="weather-adaptive/fog",
        conditions=condition_store.get_snapshot(),
    )

    bundle = EvidenceBundleBuilder(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
    ).build(slot_name="vision")

    assert bundle["schema_version"] == "temms-evidence-bundle/v1"
    assert bundle["integrity"]["payload_sha256"]
    assert len(bundle["decisions"]) == 1
    decision = bundle["decisions"][0]
    assert decision["to_model"] == "model-lowlight-v1"
    assert decision["conditions_snapshot"]["environmental"]["visibility_m"] == 40
    assert decision["model_evidence"]["to_model"]["sha256"] == sha256
    assert decision["model_evidence"]["to_package"]["manifest"]["signature_verified"] is True

    # The bundle is portable JSON, not a Python-only object graph.
    json.dumps(bundle)
















def test_mission_replay_flags_runtime_fit_upgrade_as_preview_only():
    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "exported_at": "2026-06-11T12:00:00Z",
        "runtime": {},
        "runtime_fit_evidence": [
            {
                "schema_version": "temms-runtime-fit-evidence/v1",
                "checked_at": "2026-06-11T12:00:35Z",
                "selection": {
                    "model_id": "lowlight",
                    "device_id": "edge-1",
                    "runtime_target_id": "cpu-fit",
                },
                "runtime_fit": {
                    "score": 82,
                    "tier": "ready",
                    "target_selection": {
                        "status": "upgrade_available",
                        "best_runtime_target_id": "gpu-fit",
                        "score_delta": 14,
                    },
                },
            }
        ],
    }

    summary = summarize_evidence_bundle(bundle)
    replay = build_mission_replay(bundle)
    phases = {phase["phase"]: phase for phase in replay["phases"]}

    assert summary["runtime"]["runtime_fit_evidence"][0]["score_delta"] == 14
    assert phases["runtime_fit"]["status"] == "preview_only"
    assert "better target gpu-fit" in phases["runtime_fit"]["summary"]
    assert "runtime_fit" in replay["outcome"]["incomplete_phases"]


def test_runtime_fit_replay_prefers_active_slot_and_dedupes_rollout_proof():
    def runtime_fit_record(
        *,
        checked_at: str,
        model_id: str,
        score: int,
        rollout_id: str,
    ) -> dict[str, object]:
        return {
            "schema_version": "temms-runtime-fit-evidence/v1",
            "checked_at": checked_at,
            "selection": {
                "package_id": "pkg-vision",
                "model_id": model_id,
                "device_id": "edge-1",
                "runtime_target_id": "edge-cpu",
                "slot": "vision",
                "rollout_id": rollout_id,
            },
            "runtime_fit": {
                "score": score,
                "tier": "optimal",
                "runtime_lane": {
                    "lane_id": "cpu-onnx",
                    "label": "CPU portable",
                    "execution_engine": "onnxruntime",
                    "acceleration": "cpu",
                },
                "artifact_lane": {
                    "status": "go",
                    "state": "native artifact",
                    "detail": "onnx artifact is native for CPU portable",
                    "model_format": "onnx",
                },
                "target_selection": {
                    "status": "best",
                    "best_runtime_target_id": "edge-cpu",
                    "score_delta": 0,
                },
            },
        }

    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "runtime": {},
        "slots": [
            {
                "name": "vision",
                "state": "running",
                "active_model_id": "lowlight",
                "default_model": "daylight",
            }
        ],
        "runtime_fit_evidence": [
            runtime_fit_record(
                checked_at="2026-06-11T12:00:40Z",
                model_id="daylight",
                score=95,
                rollout_id="rollout-daylight",
            ),
            runtime_fit_record(
                checked_at="2026-06-11T12:00:35Z",
                model_id="lowlight",
                score=96,
                rollout_id="rollout-lowlight",
            ),
            runtime_fit_record(
                checked_at="2026-06-11T12:00:36Z",
                model_id="lowlight",
                score=98,
                rollout_id="rollout-lowlight",
            ),
        ],
    }

    summary = summarize_evidence_bundle(bundle, limit=10)
    runtime_fits = summary["runtime"]["runtime_fit_evidence"]

    assert summary["counts"]["runtime_fit_evidence"] == 2
    assert [fit["model_id"] for fit in runtime_fits] == ["lowlight", "daylight"]
    assert runtime_fits[0]["score"] == 98

    replay = build_mission_replay(bundle, limit=10)
    phases = {phase["phase"]: phase for phase in replay["phases"]}

    assert phases["runtime_fit"]["status"] == "complete"
    assert "98/100 optimal on edge-cpu" in phases["runtime_fit"]["summary"]
    assert "lane CPU portable / cpu" in phases["runtime_fit"]["summary"]
    assert "artifact native artifact" in phases["runtime_fit"]["summary"]
    assert "95/100" not in phases["runtime_fit"]["summary"]
    runtime_fit_events = [
        event for event in replay["events"] if event["kind"] == "runtime_fit"
    ]
    assert runtime_fit_events
    assert {event["phase"] for event in runtime_fit_events} == {"runtime_fit"}


def test_summary_timeline_marks_active_runtime_fit_before_same_second_inactive():
    def runtime_fit_record(model_id: str, score: int, checked_at: str) -> dict[str, object]:
        return {
            "schema_version": "temms-runtime-fit-evidence/v1",
            "checked_at": checked_at,
            "selection": {
                "package_id": "pkg-vision",
                "model_id": model_id,
                "device_id": "edge-1",
                "runtime_target_id": "edge-cpu",
                "slot": "vision",
                "rollout_id": f"rollout-{model_id}",
            },
            "runtime_fit": {
                "score": score,
                "tier": "optimal",
                "target_selection": {
                    "status": "best",
                    "best_runtime_target_id": "edge-cpu",
                    "score_delta": 0,
                },
            },
        }

    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "runtime": {},
        "slots": [
            {
                "name": "vision",
                "state": "running",
                "active_model_id": "lowlight",
            }
        ],
        "runtime_fit_evidence": [
            runtime_fit_record("daylight", 95, "2026-06-11T12:00:35.263419Z"),
            runtime_fit_record("lowlight", 98, "2026-06-11T12:00:35.261738Z"),
        ],
    }

    summary = summarize_evidence_bundle(bundle, limit=4)
    runtime_events = [
        event for event in summary["timeline"] if event["kind"] == "runtime_fit"
    ]

    assert runtime_events[0]["active_runtime_proof"] is True
    assert "lowlight runtime fit 98/100 optimal" in runtime_events[0]["summary"]
    assert "active_runtime_proof" not in runtime_events[1]
    assert "daylight runtime fit 95/100 optimal" in runtime_events[1]["summary"]

    raw_timeline = combined_timeline(
        [],
        [],
        runtime_fit_evidence=bundle["runtime_fit_evidence"],
        active_slots=summary["active_slots"],
    )
    assert raw_timeline[0]["active_runtime_proof"] is True
    assert "lowlight runtime fit 98/100 optimal" in raw_timeline[0]["summary"]

    replay = build_mission_replay(bundle, limit=4)
    replay_runtime_events = [
        event for event in replay["events"] if event["kind"] == "runtime_fit"
    ]
    assert replay_runtime_events[0]["active_runtime_proof"] is True
    assert replay_runtime_events[0]["summary"] == raw_timeline[0]["summary"]


def test_runtime_fit_evidence_timeline_exports_hub_readiness_fit(tmp_path):
    hub = HubLiteStore(tmp_path / "hub.json")
    hub.enroll_device(
        "edge-1",
        profile="x86_64-cpu",
        inventory={
            "runtimes": {
                "onnxruntime": {
                    "available": True,
                    "providers": ["CPUExecutionProvider"],
                }
            },
            "memory": {"available_mb": 2048.0},
            "storage": {"available_mb": 4096.0},
        },
    )
    hub.upsert_package(
        {
            "package_id": "pkg-fit",
            "name": "fit-package",
            "version": "1.0.0",
            "device_profiles": ["x86_64-cpu"],
            "metadata": {
                "validation": {
                    "valid": True,
                    "signature_verified": True,
                    "strict_metadata": True,
                },
                "models": [
                    {
                        "id": "model-fit",
                        "runtime_constraints": {"runtimes": ["onnxruntime"]},
                        "performance_slo": {
                            "max_latency_ms_p95": 12.0,
                            "min_throughput_ips": 80.0,
                        },
                        "resource_requirements": {
                            "min_memory_available_mb": 512.0,
                            "min_storage_available_mb": 64.0,
                        },
                    }
                ],
            },
        }
    )
    hub.promote_package("pkg-fit", "validated", actor="operator:test")
    hub.promote_package("pkg-fit", "approved", actor="operator:test")
    hub.promote_package("pkg-fit", "released", actor="operator:test")
    hub.record_runtime_validation(
        "temms-x86_64-cpu",
        {
            "runtime_target_id": "temms-x86_64-cpu",
            "image": "temms/agent:inference-amd64",
            "dry_run": False,
            "exit_code": 0,
            "ok": True,
        },
        package_id="pkg-fit",
        actor="operator:test",
    )
    hub.record_benchmark(
        {
            "schema_version": "temms-benchmark/v1",
            "model_id": "model-fit",
            "latency_ms": {"p95": 8.0},
            "throughput": {"inferences_per_second": 120.0},
        },
        device_id="edge-1",
        package_id="pkg-fit",
        runtime_target_id="temms-x86_64-cpu",
        actor="edge:edge-1",
    )
    hub.assign_rollout(
        "edge-1",
        "pkg-fit",
        slot="vision",
        rollout_id="rollout-fit",
        runtime_target_id="temms-x86_64-cpu",
        model_id="model-fit",
        actor="operator:test",
    )

    records = runtime_fit_evidence_timeline(SimpleNamespace(hub_lite=hub))

    assert records[0]["schema_version"] == "temms-runtime-fit-evidence/v1"
    assert records[0]["selection"]["rollout_id"] == "rollout-fit"
    assert records[0]["runtime_fit"]["schema_version"] == "temms-runtime-fit/v1"
    assert records[0]["runtime_fit"]["score"] >= 85
    assert records[0]["runtime_fit"]["components"]["performance"]["state"] == "slo met"
    assert records[0]["runtime_optimizer_gate"]["gate_id"] == "runtime_optimizer"
    mission = records[0]["edge_runtime_mission"]
    assert mission["schema_version"] == "temms-edge-runtime-mission/v1"
    assert mission["path"]["label"] == "model-fit -> temms-x86_64-cpu -> edge-1"
    assert mission["metrics"]["runtime_fit"]["score"] >= 85
    assert mission["metrics"]["runtime_lane"]["lane_id"] == "cpu-onnx"
    assert mission["metrics"]["artifact_fit"]["status"] == "attention"
    contract = records[0]["edge_execution_contract"]
    assert contract["schema_version"] == "temms-edge-execution-contract/v1"
    assert contract["path"]["label"] == "model-fit -> temms-x86_64-cpu -> edge-1"
    assert contract["runtime_fit"]["score"] >= 85
    workbench = records[0]["runtime_workbench"]
    assert workbench["schema_version"] == "temms-runtime-workbench/v1"
    assert workbench["selected_runtime_target_id"] == "temms-x86_64-cpu"
    assert workbench["summary"]["target_count"] >= 1

    bundle = {"runtime_fit_evidence": records}
    summary = summarize_evidence_bundle(bundle, limit=5)
    runtime_summary = summary["runtime"]["runtime_fit_evidence"][0]
    assert runtime_summary["edge_runtime_mission_status"] == "attention"
    assert runtime_summary["edge_runtime_mission_path"] == (
        "model-fit -> temms-x86_64-cpu -> edge-1"
    )
    assert runtime_summary["edge_execution_contract_status"] == "attention"
    assert runtime_summary["edge_execution_contract_path"] == (
        "model-fit -> temms-x86_64-cpu -> edge-1"
    )
    assert runtime_summary["runtime_workbench_schema_version"] == (
        "temms-runtime-workbench/v1"
    )
    assert runtime_summary["runtime_workbench_selected_runtime_target_id"] == (
        "temms-x86_64-cpu"
    )
    assert runtime_summary["runtime_workbench_target_count"] >= 1


def test_summary_counts_signed_hub_package_when_cache_import_is_unsigned():
    bundle = {
        "schema_version": "temms-evidence-bundle/v1",
        "packages": [
            {
                "id": "pkg-vision",
                "manifest": {
                    "_temms_import": {
                        "signature_verified": False,
                    }
                },
            }
        ],
        "package_imports": [
            {
                "package_id": "pkg-vision",
                "signature_verified": False,
                "imported_at": "2026-06-25T16:17:16Z",
            }
        ],
        "hub_lite": {
            "packages": {
                "pkg-vision": {
                    "package_id": "pkg-vision",
                    "metadata": {
                        "validation": {
                            "signature_verified": True,
                            "strict_metadata": True,
                        }
                    },
                    "promotion": {"state": "released"},
                }
            }
        },
        "package_promotions": [
            {
                "package_id": "pkg-vision",
                "state": "released",
                "updated_at": "2026-06-25T16:17:16Z",
            }
        ],
    }

    summary = summarize_evidence_bundle(bundle)

    assert summary["trust"]["signed_package_imports"] == 1
    assert summary["trust"]["signed_package_ids"] == ["pkg-vision"]
    assert summary["trust"]["released_packages"] == 1
