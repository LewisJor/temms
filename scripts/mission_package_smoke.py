"""Smoke-test the live Mission Package Workbench contract.

This checks the demo failure mode that is easy to miss: a fresh React bundle
served by a stale daemon image. It intentionally uses only the stdlib so it can
run inside a local Docker demo without extra setup.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_BODY: dict[str, Any] = {
    "package_id": "pkg-vision-models-20240115",
    "model_id": "model-yolov8-lowlight-001",
    "device_id": "edge-sim",
    "runtime_target_id": "temms-x86_64-cpu",
    "slot": "vision",
    "goal": (
        "Detect vehicles in changing light, keep inference local during link "
        "loss, and preserve proof for every model/runtime switch."
    ),
    "sensor": "camera.rgb",
    "latency_budget_ms": 12,
    "min_throughput_ips": 85,
    "switch_policy": "condition_and_confidence",
    "confidence_threshold": 0.72,
    "fallback_model_id": "auto",
    "ddil_mode": "queue_signed_intents",
    "require_go": True,
    "min_runtime_fit": 95,
    "require_best_runtime": True,
    "require_capability_lock": True,
    "require_proof_signature": True,
}


def mission_yaml_for_body(body: dict[str, Any]) -> str:
    """Return a mission spec that should be sufficient for package planning."""
    return "\n".join(
        [
            "schema_version: temms-edge-mission/v1",
            "mission:",
            f"  goal: {body['goal']}",
            f"  sensor: {body['sensor']}",
            f"  slot: {body['slot']}",
            "selection:",
            f"  package_id: {body['package_id']}",
            f"  model_id: {body['model_id']}",
            f"  device_id: {body['device_id']}",
            f"  runtime_target_id: {body['runtime_target_id']}",
            "slo:",
            f"  latency_budget_ms: {body['latency_budget_ms']}",
            f"  min_throughput_ips: {body['min_throughput_ips']}",
            "model_handling:",
            f"  switch_policy: {body['switch_policy']}",
            f"  confidence_threshold: {body['confidence_threshold']}",
            f"  fallback_model_id: {body['fallback_model_id']}",
            "ddil:",
            f"  mode: {body['ddil_mode']}",
            "",
        ]
    )


def yaml_only_body_for(body: dict[str, Any]) -> dict[str, Any]:
    """Return a mission package plan body that depends on server-side YAML derivation."""
    return {
        "mission_yaml": mission_yaml_for_body(body),
        "require_go": body["require_go"],
        "min_runtime_fit": body["min_runtime_fit"],
        "require_best_runtime": body["require_best_runtime"],
        "require_capability_lock": body["require_capability_lock"],
        "require_proof_signature": body["require_proof_signature"],
    }


class SmokeFailure(RuntimeError):
    """Raised when the live demo contract is not satisfied."""


def request_json(
    method: str,
    url: str,
    token: str = "",
    body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], urllib.response.addinfourl]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=20)
    except urllib.error.HTTPError as error:
        detail = error.read(700).decode("utf-8", errors="replace")
        raise SmokeFailure(f"{method} {url} returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise SmokeFailure(f"{method} {url} failed: {error}") from error
    raw = response.read().decode("utf-8")
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as error:
        raise SmokeFailure(f"{method} {url} did not return JSON: {raw[:700]}") from error
    if not isinstance(parsed, dict):
        raise SmokeFailure(f"{method} {url} returned non-object JSON")
    return parsed, response


def request_text(url: str, token: str = "") -> str:
    headers = {"Accept": "text/html"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        response = urllib.request.urlopen(request, timeout=20)
    except urllib.error.HTTPError as error:
        detail = error.read(700).decode("utf-8", errors="replace")
        raise SmokeFailure(f"GET {url} returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise SmokeFailure(f"GET {url} failed: {error}") from error
    return response.read().decode("utf-8", errors="replace")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def header(response: urllib.response.addinfourl, name: str) -> str:
    return response.headers.get(name, "")


def deployment_body_stable_fields(body: dict[str, Any]) -> dict[str, Any]:
    """Fields that should match when the mission source changes but selection does not."""
    return {
        key: body.get(key)
        for key in (
            "rollout_id",
            "package_id",
            "model_id",
            "device_id",
            "runtime_target_id",
            "slot",
            "require_approval",
            "require_runtime_validation",
            "actor",
        )
    }


def approve_rollout(hub_url: str, token: str, rollout_id: str) -> dict[str, Any]:
    approved, _ = request_json(
        "POST",
        f"{hub_url}/v1/hub/rollouts/{rollout_id}/approve",
        token,
        {
            "actor": "operator:mission-package-smoke",
            "reason": "mission package smoke approval",
        },
    )
    approval = approved.get("approval") if isinstance(approved.get("approval"), dict) else {}
    require(approval.get("state") == "approved", "mission package smoke approval failed")
    return approved


def apply_rollout(hub_url: str, token: str, rollout_id: str, model_id: str) -> dict[str, Any]:
    applied, _ = request_json(
        "POST",
        f"{hub_url}/v1/hub/rollouts/{rollout_id}/apply",
        token,
        {
            "actor": "operator:mission-package-smoke",
            "model_id": model_id,
        },
    )
    applied_rollout = applied.get("rollout") if isinstance(applied.get("rollout"), dict) else applied
    require(
        applied_rollout.get("state") in {"activated", "imported"},
        "mission package smoke apply failed",
    )
    return applied


def advance_pending_smoke_rollout_gate(hub_url: str, token: str, body: dict[str, Any]) -> str:
    """Advance a previous smoke-created rollout if it is the only readiness blocker."""
    params = urllib.parse.urlencode(
        {
            "package_id": body["package_id"],
            "model_id": body["model_id"],
            "device_id": body["device_id"],
            "runtime_target_id": body["runtime_target_id"],
            "slot": body["slot"],
        }
    )
    readiness, _ = request_json("GET", f"{hub_url}/v1/hub/readiness?{params}", token)
    gates = readiness.get("gates") if isinstance(readiness.get("gates"), list) else []
    blockers = [
        gate
        for gate in gates
        if isinstance(gate, dict) and gate.get("status") != "go"
    ]
    if len(blockers) != 1:
        return ""
    blocker = blockers[0]
    if blocker.get("gate_id") != "rollout_gate":
        return ""
    selection = readiness.get("selection") if isinstance(readiness.get("selection"), dict) else {}
    rollout_id = str(selection.get("rollout_id") or "")
    if not rollout_id:
        return ""
    if blocker.get("state") == "approval pending":
        approve_rollout(hub_url, token, rollout_id)
        apply_rollout(hub_url, token, rollout_id, body["model_id"])
    elif blocker.get("state") == "assigned":
        apply_rollout(hub_url, token, rollout_id, body["model_id"])
    else:
        return ""
    return rollout_id


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    hub_url = args.hub_url.rstrip("/")
    token = args.token or os.environ.get("TEMMS_HUB_TOKEN", "")
    body = dict(DEFAULT_BODY)
    for key in (
        "package_id",
        "model_id",
        "device_id",
        "runtime_target_id",
        "slot",
    ):
        value = getattr(args, key)
        if value:
            body[key] = value

    html = request_text(f"{hub_url}/ui/hub", token)
    require("TEMMS - Mission Package Workbench" in html, "Hub shell is not the Mission Package Workbench")
    require("Edge Runtime Control" not in html, "Hub shell still contains retired product name")

    preadvanced_rollout_id = advance_pending_smoke_rollout_gate(hub_url, token, body)

    plan, _ = request_json("POST", f"{hub_url}/v1/hub/mission-package/plan", token, body)
    require(
        plan.get("schema_version") == "temms-edge-mission-package/v1",
        "mission package plan returned the wrong schema",
    )
    runtime_plan = plan.get("runtime_plan") if isinstance(plan.get("runtime_plan"), dict) else {}
    deployment_intent = (
        plan.get("deployment_intent")
        if isinstance(plan.get("deployment_intent"), dict)
        else {}
    )
    command = (
        deployment_intent.get("command")
        if isinstance(deployment_intent.get("command"), dict)
        else {}
    )
    component_digests = (
        plan.get("component_digests")
        if isinstance(plan.get("component_digests"), dict)
        else {}
    )
    edge_handoff = (
        plan.get("edge_handoff")
        if isinstance(plan.get("edge_handoff"), dict)
        else {}
    )
    handoff_commands = (
        edge_handoff.get("commands")
        if isinstance(edge_handoff.get("commands"), dict)
        else {}
    )
    integrity = plan.get("integrity") if isinstance(plan.get("integrity"), dict) else {}
    package_identity = (
        plan.get("package_identity")
        if isinstance(plan.get("package_identity"), dict)
        else {}
    )

    require(runtime_plan.get("status") == "go", "runtime plan is not go")
    require(command.get("path") == "/v1/hub/rollouts", "deployment intent does not target rollout creation")
    require(bool(command.get("body")), "deployment intent is missing rollout body")
    require(
        bool(integrity.get("package_identity_sha256")),
        "mission package identity digest is missing",
    )
    require(
        integrity.get("package_identity_sha256")
        == package_identity.get("package_identity_sha256"),
        "mission package identity digest does not match package identity block",
    )
    require(
        deployment_intent.get("package_identity_sha256")
        == integrity.get("package_identity_sha256"),
        "deployment intent does not reference package identity",
    )
    require(
        edge_handoff.get("schema_version") == "temms-edge-mission-package-handoff/v1",
        "edge handoff schema is missing",
    )
    require(
        handoff_commands.get("stage_package", {}).get("path")
        == "/v1/hub/mission-package/stage",
        "edge handoff does not include package stage command",
    )
    require(
        str(handoff_commands.get("apply_rollout", {}).get("path", "")).endswith("/apply"),
        "edge handoff does not include rollout apply command",
    )
    require(bool(component_digests.get("runtime_plan_sha256")), "runtime plan digest is missing")
    require(bool(component_digests.get("deployment_intent_sha256")), "deployment intent digest is missing")
    require(bool(component_digests.get("edge_handoff_sha256")), "edge handoff digest is missing")
    require(bool(integrity.get("payload_sha256")), "mission package payload digest is missing")

    yaml_plan, _ = request_json(
        "POST",
        f"{hub_url}/v1/hub/mission-package/plan",
        token,
        yaml_only_body_for(body),
    )
    yaml_runtime_plan = (
        yaml_plan.get("runtime_plan")
        if isinstance(yaml_plan.get("runtime_plan"), dict)
        else {}
    )
    yaml_deployment_intent = (
        yaml_plan.get("deployment_intent")
        if isinstance(yaml_plan.get("deployment_intent"), dict)
        else {}
    )
    yaml_command = (
        yaml_deployment_intent.get("command")
        if isinstance(yaml_deployment_intent.get("command"), dict)
        else {}
    )
    require(
        yaml_plan.get("schema_version") == "temms-edge-mission-package/v1",
        "YAML-only mission package plan returned the wrong schema",
    )
    require(
        yaml_plan.get("selection") == plan.get("selection"),
        "YAML-only mission package plan did not derive the selected edge path",
    )
    require(
        yaml_runtime_plan.get("status") == "go",
        "YAML-only mission package runtime plan is not go",
    )
    require(
        yaml_command.get("path") == "/v1/hub/rollouts",
        "YAML-only deployment intent does not target rollout creation",
    )
    require(
        deployment_body_stable_fields(yaml_command.get("body") or {})
        == deployment_body_stable_fields(command.get("body") or {}),
        "YAML-only deployment intent body does not match the explicit mission path",
    )

    downloaded, download_response = request_json(
        "POST", f"{hub_url}/v1/hub/mission-package/download", token, body
    )
    downloaded_runtime_plan = (
        downloaded.get("runtime_plan")
        if isinstance(downloaded.get("runtime_plan"), dict)
        else {}
    )
    downloaded_deployment_intent = (
        downloaded.get("deployment_intent")
        if isinstance(downloaded.get("deployment_intent"), dict)
        else {}
    )
    downloaded_command = (
        downloaded_deployment_intent.get("command")
        if isinstance(downloaded_deployment_intent.get("command"), dict)
        else {}
    )
    downloaded_component_digests = (
        downloaded.get("component_digests")
        if isinstance(downloaded.get("component_digests"), dict)
        else {}
    )
    downloaded_edge_handoff = (
        downloaded.get("edge_handoff")
        if isinstance(downloaded.get("edge_handoff"), dict)
        else {}
    )
    downloaded_handoff_commands = (
        downloaded_edge_handoff.get("commands")
        if isinstance(downloaded_edge_handoff.get("commands"), dict)
        else {}
    )
    downloaded_integrity = (
        downloaded.get("integrity") if isinstance(downloaded.get("integrity"), dict) else {}
    )
    require(
        downloaded.get("schema_version") == "temms-edge-mission-package/v1",
        "downloaded mission package returned the wrong schema",
    )
    require(downloaded_runtime_plan.get("status") == "go", "downloaded runtime plan is not go")
    require(
        downloaded_command.get("path") == "/v1/hub/rollouts",
        "downloaded deployment intent does not target rollout creation",
    )
    require(
        bool(downloaded_integrity.get("payload_sha256")),
        "downloaded package payload digest is missing",
    )
    require(
        header(download_response, "X-TEMMS-Mission-Package-SHA256")
        == downloaded_integrity.get("payload_sha256"),
        "download response payload digest header does not match package body",
    )
    require(
        header(download_response, "X-TEMMS-Mission-Package-Identity-SHA256")
        == downloaded_integrity.get("package_identity_sha256"),
        "download response identity digest header does not match package body",
    )
    require(
        downloaded_integrity.get("package_identity_sha256")
        == integrity.get("package_identity_sha256"),
        "plan and download package identity digests differ",
    )
    require(
        header(download_response, "X-TEMMS-Mission-Package-Deployment-Intent-SHA256")
        == downloaded_component_digests.get("deployment_intent_sha256"),
        "download response deployment-intent digest header does not match package body",
    )
    require(
        downloaded_edge_handoff.get("schema_version")
        == "temms-edge-mission-package-handoff/v1",
        "downloaded package is missing edge handoff runbook",
    )
    require(
        downloaded_handoff_commands.get("stage_package", {}).get("path")
        == "/v1/hub/mission-package/stage",
        "downloaded edge handoff does not include package stage command",
    )
    require(
        str(downloaded_handoff_commands.get("apply_rollout", {}).get("path", "")).endswith(
            "/apply"
        ),
        "downloaded edge handoff does not include rollout apply command",
    )

    staged, _ = request_json(
        "POST",
        f"{hub_url}/v1/hub/mission-package/stage",
        token,
        {
            "actor": "operator:mission-package-smoke",
            "mission_package": downloaded,
            "reason": "mission package smoke staged from artifact",
        },
    )
    rollout = staged.get("rollout") if isinstance(staged.get("rollout"), dict) else {}
    staged_edge_handoff = (
        staged.get("edge_handoff")
        if isinstance(staged.get("edge_handoff"), dict)
        else {}
    )
    staged_handoff_commands = (
        staged_edge_handoff.get("commands")
        if isinstance(staged_edge_handoff.get("commands"), dict)
        else {}
    )
    require(
        staged.get("schema_version") == "temms-edge-mission-package-stage/v1",
        "mission package stage returned the wrong schema",
    )
    require(staged.get("status") == "staged", "mission package stage did not stage rollout")
    stage_gate = staged.get("stage_gate") if isinstance(staged.get("stage_gate"), dict) else {}
    require(
        stage_gate.get("status") == "passed",
        "mission package stage gate did not pass",
    )
    require(
        staged.get("package_identity_sha256")
        == downloaded_integrity.get("package_identity_sha256"),
        "stage response does not reference downloaded package identity",
    )
    require(
        staged.get("deployment_intent_sha256")
        == downloaded_component_digests.get("deployment_intent_sha256"),
        "stage response does not reference downloaded deployment intent",
    )
    require(
        rollout.get("rollout_id") == downloaded_deployment_intent.get("rollout_id"),
        "staged rollout ID does not match deployment intent",
    )
    require(
        str(staged_handoff_commands.get("apply_rollout", {}).get("path", "")).endswith(
            "/apply"
        ),
        "staged package did not preserve edge handoff apply command",
    )
    require(rollout.get("state") in {"assigned", "activated"}, "staged rollout has unexpected state")
    approved = approve_rollout(hub_url, token, str(rollout.get("rollout_id") or ""))
    approval = approved.get("approval") if isinstance(approved.get("approval"), dict) else {}
    applied = apply_rollout(
        hub_url,
        token,
        str(rollout.get("rollout_id") or ""),
        body["model_id"],
    )

    return {
        "hub_url": hub_url,
        "schema_version": plan.get("schema_version"),
        "runtime_status": runtime_plan.get("status"),
        "runtime_fit_score": runtime_plan.get("runtime_fit_score"),
        "runtime_target_id": runtime_plan.get("runtime_target_id"),
        "yaml_only_runtime_status": yaml_runtime_plan.get("status"),
        "yaml_only_runtime_target_id": yaml_runtime_plan.get("runtime_target_id"),
        "rollout_id": deployment_intent.get("rollout_id"),
        "deployment_path": command.get("path"),
        "stage_status": staged.get("status"),
        "stage_gate_status": stage_gate.get("status"),
        "approval_state": approval.get("state"),
        "apply_state": (
            applied.get("state")
            or (
                applied.get("rollout", {}).get("state")
                if isinstance(applied.get("rollout"), dict)
                else None
            )
        ),
        "preadvanced_rollout_id": preadvanced_rollout_id,
        "package_identity_sha256": integrity.get("package_identity_sha256"),
        "plan_payload_sha256": integrity.get("payload_sha256"),
        "download_payload_sha256": downloaded_integrity.get("payload_sha256"),
        "download_filename": header(download_response, "X-TEMMS-Mission-Package-Filename"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub-url", default=os.environ.get("TEMMS_SMOKE_TEMMS_URL", "http://localhost:8080"))
    parser.add_argument("--token", default=os.environ.get("TEMMS_API_TOKEN", ""))
    parser.add_argument("--package-id")
    parser.add_argument("--model-id")
    parser.add_argument("--device-id")
    parser.add_argument("--runtime-target-id")
    parser.add_argument("--slot")
    return parser.parse_args()


def main() -> int:
    try:
        summary = run_smoke(parse_args())
    except SmokeFailure as error:
        print(f"Mission package smoke FAILED: {error}", file=sys.stderr)
        return 1
    print("Mission package smoke OK")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
