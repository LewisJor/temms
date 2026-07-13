import json
import importlib.util
from pathlib import Path

from temms.hub_lite import HubLiteStore


def _load_seed_hub_demo():
    script_path = Path(__file__).parents[2] / "scripts" / "seed_docker_hub_demo.py"
    spec = importlib.util.spec_from_file_location("seed_docker_hub_demo", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.seed_hub_demo


def test_seed_hub_demo_records_performance_evidence_idempotently(tmp_path):
    seed_hub_demo = _load_seed_hub_demo()
    package_source = Path(__file__).parents[2] / "examples" / "package-example"
    hub_state_path = tmp_path / "hub_lite.json"

    first = seed_hub_demo(
        package_source=package_source,
        data_dir=tmp_path,
        hub_state_path=hub_state_path,
        signing_key="demo-secret",
        device_id="edge-sim",
        device_profile="x86_64-cpu",
        site="local-lab",
    )
    first_hub = HubLiteStore(hub_state_path)
    first_benchmarks = first_hub.list_benchmarks(
        device_id="edge-sim",
        package_id="pkg-vision-models-20240115",
        runtime_target_id="temms-x86_64-cpu",
    )
    first_benchmark_ids = {record["benchmark_id"] for record in first_benchmarks}
    stale_state = json.loads(hub_state_path.read_text(encoding="utf-8"))
    for record in stale_state["benchmarks"].values():
        record["created_at"] = "2024-01-01T00:00:00Z"
        record["result"]["latency_ms"]["p95"] = 999.0
    hub_state_path.write_text(json.dumps(stale_state), encoding="utf-8")

    second = seed_hub_demo(
        package_source=package_source,
        data_dir=tmp_path,
        hub_state_path=hub_state_path,
        signing_key="demo-secret",
        device_id="edge-sim",
        device_profile="x86_64-cpu",
        site="local-lab",
    )

    hub = HubLiteStore(hub_state_path)
    validations = hub.list_runtime_validations(
        package_id="pkg-vision-models-20240115",
        runtime_target_id="temms-x86_64-cpu",
    )
    benchmarks = hub.list_benchmarks(
        device_id="edge-sim",
        package_id="pkg-vision-models-20240115",
        runtime_target_id="temms-x86_64-cpu",
    )
    package = hub.get_package("pkg-vision-models-20240115")
    assert package is not None
    current_source_sha = package["source_sha256"]
    models = {
        model["id"]: model
        for model in package["metadata"]["models"]
    }

    assert first["benchmarks"] == 3
    assert second["benchmarks"] == 3
    assert second["runtime_validation_id"] == validations[0]["validation_id"]
    assert validations[0]["source_sha256"] == current_source_sha
    assert len(validations) >= 1
    assert validations[0]["result"]["ok"] is True
    assert validations[0]["result"]["dry_run"] is False
    assert len(benchmarks) == 3
    assert first_benchmark_ids.isdisjoint(
        {record["benchmark_id"] for record in benchmarks}
    )
    assert {record["model_id"] for record in benchmarks} == {
        "model-yolov8-daylight-001",
        "model-yolov8-lowlight-001",
        "model-mobilenet-tiny-001",
    }
    assert all(record["created_at"] != "2024-01-01T00:00:00Z" for record in benchmarks)
    assert all(record["result"]["demo_seed"] is True for record in benchmarks)
    assert all(record["result"]["latency_ms"]["p95"] != 999.0 for record in benchmarks)
    assert any(record["result"]["latency_ms"]["p95"] == 9.4 for record in benchmarks)
    assert models["model-yolov8-lowlight-001"]["performance_slo"] == {
        "max_latency_ms_p95": 12.0,
        "min_throughput_ips": 85.0,
    }
    assert models["model-yolov8-lowlight-001"]["resource_requirements"] == {
        "min_memory_available_mb": 512.0,
        "min_storage_available_mb": 64.0,
    }

    readiness = hub.deployment_readiness(
        package_id="pkg-vision-models-20240115",
        model_id="model-yolov8-lowlight-001",
        device_id="edge-sim",
        runtime_target_id="temms-x86_64-cpu",
        slot="vision",
    )
    performance_gate = {
        gate["gate_id"]: gate for gate in readiness["gates"]
    }["performance_fit"]
    assert performance_gate["status"] == "go"
    assert performance_gate["state"] == "slo met"
    resource_gate = {
        gate["gate_id"]: gate for gate in readiness["gates"]
    }["resource_envelope"]
    assert resource_gate["status"] == "go"
    assert resource_gate["state"] == "met"
    assert resource_gate["refs"]["memory_available_mb"] == 4096.0
