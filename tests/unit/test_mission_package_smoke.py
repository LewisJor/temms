import importlib.util
from pathlib import Path


def _load_mission_package_smoke():
    script_path = Path(__file__).parents[2] / "scripts" / "mission_package_smoke.py"
    spec = importlib.util.spec_from_file_location("mission_package_smoke", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_yaml_only_body_carries_mission_selection_and_policy():
    smoke = _load_mission_package_smoke()

    body = dict(smoke.DEFAULT_BODY)
    yaml_only = smoke.yaml_only_body_for(body)

    assert set(yaml_only) == {
        "mission_yaml",
        "require_go",
        "min_runtime_fit",
        "require_best_runtime",
        "require_capability_lock",
        "require_proof_signature",
    }
    mission_yaml = yaml_only["mission_yaml"]
    assert "schema_version: temms-edge-mission/v1" in mission_yaml
    assert f"  package_id: {body['package_id']}" in mission_yaml
    assert f"  model_id: {body['model_id']}" in mission_yaml
    assert f"  device_id: {body['device_id']}" in mission_yaml
    assert f"  runtime_target_id: {body['runtime_target_id']}" in mission_yaml
    assert f"  latency_budget_ms: {body['latency_budget_ms']}" in mission_yaml
    assert f"  min_throughput_ips: {body['min_throughput_ips']}" in mission_yaml
    assert f"  switch_policy: {body['switch_policy']}" in mission_yaml
    assert f"  mode: {body['ddil_mode']}" in mission_yaml


def test_deployment_body_stable_fields_ignore_hash_reason():
    smoke = _load_mission_package_smoke()

    first = {
        "rollout_id": "readiness-rollout-1",
        "package_id": "pkg",
        "model_id": "model",
        "device_id": "edge",
        "runtime_target_id": "runtime",
        "slot": "vision",
        "require_approval": True,
        "require_runtime_validation": True,
        "actor": "operator:readiness-remediation",
        "reason": "mission package deployment handoff aaa111",
    }
    second = {**first, "reason": "mission package deployment handoff bbb222"}

    assert smoke.deployment_body_stable_fields(first) == smoke.deployment_body_stable_fields(second)
