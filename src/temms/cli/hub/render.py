"""Rendering for Hub Lite payloads.

Presentation only. These moved out of ``cli/main.py`` so the Hub sub-commands
can import them without a cycle: the command layer must not depend on the
module that composes the whole CLI.

``HUB_PRINTERS`` stays a dispatch table rather than a chain of ``if action ==``
branches -- adding an action means adding a row, not editing a function.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.table import Table

console = Console()


def _render_enroll(action: str, payload: dict) -> None:
    """Render `temms hub enroll`."""
    console.print("[green]Hub device enrolled[/green]")
    console.print(f"Device: {payload.get('device_id', '')}")
    console.print(f"Profile: {payload.get('profile', '')}")


def _render_devices(action: str, payload: dict) -> None:
    """Render `temms hub devices`."""
    table = Table(title="Hub Devices")
    table.add_column("Device")
    table.add_column("Profile")
    table.add_column("Status")
    table.add_column("Last Seen")
    for device in payload.get("devices", []):
        table.add_row(
            device.get("device_id", ""),
            device.get("profile", ""),
            device.get("status", ""),
            device.get("last_seen_at", ""),
        )
    console.print(table)


def _render_packages(action: str, payload: dict) -> None:
    """Render `temms hub packages`."""
    table = Table(title="Hub Packages")
    table.add_column("Package")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Promotion")
    table.add_column("Profiles")
    for package in payload.get("packages", []):
        promotion = (
            package.get("promotion") if isinstance(package.get("promotion"), dict) else {}
        )
        table.add_row(
            package.get("package_id", ""),
            package.get("name", ""),
            package.get("version", ""),
            promotion.get("state", "candidate"),
            ", ".join(package.get("device_profiles", []) or []),
        )
    console.print(table)


def _render_promote_package(action: str, payload: dict) -> None:
    """Render `temms hub promote-package`."""
    promotion = payload.get("promotion") if isinstance(payload.get("promotion"), dict) else {}
    console.print("[green]Hub package promoted[/green]")
    console.print(f"Package: {payload.get('package_id', '')}")
    console.print(f"State: {promotion.get('state', '')}")
    console.print(f"Actor: {promotion.get('actor') or ''}")


def _render_runtime_targets(action: str, payload: dict) -> None:
    """Render `temms hub runtime-targets`."""
    table = Table(title="Hub Runtime Targets")
    table.add_column("Target")
    table.add_column("Image")
    table.add_column("OS/Arch")
    table.add_column("Profiles")
    table.add_column("Source")
    for target in payload.get("runtime_targets", []):
        table.add_row(
            target.get("runtime_target_id", ""),
            target.get("image", ""),
            f"{target.get('os', 'linux')}/{target.get('arch') or ''}",
            ", ".join(target.get("device_profiles", []) or []),
            target.get("source", ""),
        )
    console.print(table)


def _render_readiness(action: str, payload: dict) -> None:
    """Render `temms hub readiness`."""
    _print_hub_readiness(payload)


def _render_edge_runtime_mission(action: str, payload: dict) -> None:
    """Render `temms hub edge-runtime-mission`."""
    _print_edge_runtime_mission(payload)


def _render_mission_package_plan(action: str, payload: dict) -> None:
    """Render `temms hub` mission-package-plan / mission-package-download."""
    _print_mission_package_plan(payload, downloaded=action == "mission-package-download")


def _render_mission_package_stage(action: str, payload: dict) -> None:
    """Render `temms hub mission-package-stage`."""
    rollout = payload.get("rollout") if isinstance(payload.get("rollout"), dict) else payload
    console.print("[green]Mission package deployment intent staged[/green]")
    console.print(
        f"Rollout: {payload.get('rollout_id') or rollout.get('rollout_id', '')} "
        f"({payload.get('rollout_state') or rollout.get('state', 'unknown')})"
    )
    if payload.get("package_identity_sha256"):
        console.print(
            f"Package identity: {payload.get('package_identity_sha256')}"
        )
    if rollout.get("device_id"):
        console.print(f"Device: {rollout.get('device_id')}")
    if rollout.get("package_id"):
        console.print(f"Package: {rollout.get('package_id')}")


def _render_register_runtime(action: str, payload: dict) -> None:
    """Render `temms hub register-runtime`."""
    console.print("[green]Runtime target registered[/green]")
    console.print(f"Target: {payload.get('runtime_target_id', '')}")
    console.print(f"Image: {payload.get('image', '')}")


def _render_package_from_mlflow(action: str, payload: dict) -> None:
    """Render `temms hub package-from-mlflow`."""
    package = payload.get("package", {})
    console.print("[green]Hub package built from MLflow[/green]")
    console.print(f"Package: {package.get('package_id', '')}")
    console.print(f"Path: {payload.get('package_path', '')}")
    console.print(f"Signed: {payload.get('signed', False)}")


def _render_validate_runtime(action: str, payload: dict) -> None:
    """Render `temms hub validate-runtime`."""
    status = "ready" if payload.get("dry_run") else "passed" if payload.get("ok") else "failed"
    color = "green" if payload.get("ok") else "red"
    console.print(f"[{color}]Runtime target validation {status}[/{color}]")
    console.print(f"Target: {payload.get('runtime_target_id', '')}")
    console.print(f"Image: {payload.get('image', '')}")
    validation_record = payload.get("validation_record") or {}
    if validation_record.get("validation_id"):
        console.print(f"Evidence: {validation_record.get('validation_id')}")
    console.print(f"Command: {payload.get('command_text', '')}")
    if payload.get("exit_code") is not None:
        console.print(f"Exit code: {payload.get('exit_code')}")
    stdout = (payload.get("stdout") or "").strip()
    stderr = (payload.get("stderr") or "").strip()
    if stdout:
        console.print(f"stdout:\n{stdout}")
    if stderr:
        console.print(f"stderr:\n{stderr}")


def _render_runtime_validations(action: str, payload: dict) -> None:
    """Render `temms hub runtime-validations`."""
    table = Table(title="Hub Runtime Validations")
    table.add_column("Validation")
    table.add_column("Package")
    table.add_column("Runtime")
    table.add_column("Result")
    table.add_column("Actor")
    table.add_column("Created")
    for validation in payload.get("runtime_validations", []):
        result = validation.get("result") or {}
        status = "preview" if result.get("dry_run") else "pass" if result.get("ok") else "fail"
        table.add_row(
            validation.get("validation_id", ""),
            validation.get("package_id") or validation.get("package_path") or "",
            validation.get("runtime_target_id", ""),
            status,
            validation.get("actor") or "",
            validation.get("created_at") or "",
        )
    console.print(table)


def _render_benchmarks(action: str, payload: dict) -> None:
    """Render `temms hub benchmarks`."""
    table = Table(title="Hub Benchmarks")
    table.add_column("Benchmark")
    table.add_column("Device")
    table.add_column("Package")
    table.add_column("Runtime")
    table.add_column("Model")
    table.add_column("p95 ms")
    table.add_column("Created")
    for benchmark in payload.get("benchmarks", []):
        result = benchmark.get("result") or {}
        latency = result.get("latency_ms") if isinstance(result.get("latency_ms"), dict) else {}
        p95 = latency.get("p95")
        table.add_row(
            benchmark.get("benchmark_id", ""),
            benchmark.get("device_id") or "",
            benchmark.get("package_id") or "",
            benchmark.get("runtime_target_id") or "",
            benchmark.get("model_id") or result.get("model_id") or "",
            "" if p95 is None else str(p95),
            benchmark.get("created_at") or "",
        )
    console.print(table)


def _render_preview_compatibility(action: str, payload: dict) -> None:
    """Render `temms hub preview-compatibility`."""
    color = "green" if payload.get("compatible") else "red"
    status = "compatible" if payload.get("compatible") else "blocked"
    device = payload.get("device") or {}
    package = payload.get("package") or {}
    runtime_target = payload.get("runtime_target") or {}
    console.print(f"[{color}]Rollout compatibility {status}[/{color}]")
    console.print(f"Device: {device.get('device_id', '')} ({device.get('profile', 'unknown')})")
    console.print(f"Package: {package.get('package_id', '')} v{package.get('version', '')}")
    console.print(
        "Runtime: "
        + (
            f"{runtime_target.get('runtime_target_id')} ({runtime_target.get('image')})"
            if runtime_target
            else "auto / device inventory"
        )
    )
    for failure in payload.get("failures", []):
        console.print(f"[red]Failure:[/red] {failure}")


def _render_compatibility_matrix(action: str, payload: dict) -> None:
    """Render `temms hub compatibility-matrix`."""
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    table = Table(title="Hub Compatibility Matrix")
    table.add_column("Package")
    table.add_column("Model")
    table.add_column("Device")
    table.add_column("Runtime")
    table.add_column("Compatible")
    table.add_column("Ready")
    table.add_column("Validation")
    table.add_column("Blockers")
    for cell in payload.get("cells", []):
        runtime = cell.get("runtime_target_id") or "device inventory"
        validation = (
            "pass"
            if cell.get("runtime_validation_ready")
            else "missing" if cell.get("runtime_target_id") else "inventory"
        )
        blockers = list(cell.get("assignment_blockers") or cell.get("failures") or [])
        blockers_text = "; ".join(str(blocker) for blocker in blockers[:2])
        if len(blockers) > 2:
            blockers_text += f"; +{len(blockers) - 2} more"
        table.add_row(
            cell.get("package_id", ""),
            cell.get("model_id") or "package",
            cell.get("device_id", ""),
            runtime,
            "yes" if cell.get("compatible") else "no",
            "yes" if cell.get("assignment_ready") else "no",
            validation,
            blockers_text or "ready",
        )
    console.print(table)
    console.print(
        "Ready: " f"{counts.get('assignment_ready', 0)}/{counts.get('cells', 0)} " "cells"
    )






def _render_rollouts(action: str, payload: dict) -> None:
    """Render `temms hub rollouts`."""
    table = Table(title="Hub Rollouts")
    table.add_column("Rollout")
    table.add_column("Device")
    table.add_column("Package")
    table.add_column("Slot")
    table.add_column("Runtime")
    table.add_column("State")
    table.add_column("Approval")
    for rollout in payload.get("rollouts", []):
        approval = rollout.get("approval") if isinstance(rollout.get("approval"), dict) else {}
        table.add_row(
            rollout.get("rollout_id", ""),
            rollout.get("device_id", ""),
            rollout.get("package_id", ""),
            rollout.get("slot", "") or "",
            rollout.get("runtime_target_id", "") or "auto",
            rollout.get("state", ""),
            approval.get("state", "not_required"),
        )
    console.print(table)


def _render_status(action: str, payload: dict) -> None:
    """Render `temms hub status`."""
    devices = payload.get("devices", {})
    deployments = payload.get("deployment_status", {})
    rollouts = payload.get("rollouts", {})
    telemetry = payload.get("telemetry_events", {})
    console.print("[bold]Hub Deployment Status[/bold]")
    console.print(f"Devices: {len(devices)}")
    console.print(f"Deployment snapshots: {len(deployments)}")
    console.print(f"Rollouts: {len(rollouts)}")
    console.print(f"Replayed telemetry events: {len(telemetry)}")


def _render_telemetry(action: str, payload: dict) -> None:
    """Render `temms hub telemetry`."""
    table = Table(title="Hub Replayed Telemetry")
    table.add_column("Event")
    table.add_column("Type")
    table.add_column("Device")
    table.add_column("Timestamp")
    for event in payload.get("events", []):
        table.add_row(
            event.get("event_id", ""),
            event.get("event_type", ""),
            event.get("device_id", "") or "",
            event.get("timestamp", ""),
        )
    console.print(table)


def _render_evidence(action: str, payload: dict) -> None:
    """Render `temms hub evidence`."""
    table = Table(title="Hub Evidence Bundles")
    table.add_column("Evidence")
    table.add_column("Device")
    table.add_column("Exported")
    table.add_column("Ingested")
    table.add_column("Headline")
    for record in payload.get("evidence_bundles", []):
        table.add_row(
            record.get("evidence_id", ""),
            record.get("device_id", "") or "",
            record.get("exported_at", "") or "",
            record.get("ingested_at", "") or "",
            record.get("headline", "") or "",
        )
    console.print(table)


def _render_ingest_evidence(action: str, payload: dict) -> None:
    """Render `temms hub ingest-evidence`."""
    record = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    duplicate = " duplicate" if record.get("duplicate") else ""
    console.print(f"[green]Evidence ingested{duplicate}[/green]")
    console.print(f"Evidence: {record.get('evidence_id', '')}")
    console.print(f"Device: {record.get('device_id', '') or 'unknown'}")
    if record.get("headline"):
        console.print(f"Headline: {record.get('headline')}")


def _render_generic_hub_payload(action: str, payload: dict) -> None:
    """Fallback for actions without a dedicated renderer."""
    console.print("[green]Hub command succeeded[/green]")
    if "rollout_id" in payload:
        console.print(f"Rollout: {payload['rollout_id']} ({payload.get('state', 'unknown')})")
    elif "package_id" in payload:
        console.print(f"Package: {payload['package_id']} v{payload.get('version', '')}")
    elif "status" in payload:
        console.print(f"Status: {payload['status']}")


HUB_PRINTERS: dict[str, Callable[[str, dict], None]] = {
    "enroll": _render_enroll,
    "devices": _render_devices,
    "packages": _render_packages,
    "promote-package": _render_promote_package,
    "runtime-targets": _render_runtime_targets,
    "readiness": _render_readiness,
    "edge-runtime-mission": _render_edge_runtime_mission,
    "mission-package-plan": _render_mission_package_plan,
    "mission-package-download": _render_mission_package_plan,
    "mission-package-stage": _render_mission_package_stage,
    "register-runtime": _render_register_runtime,
    "package-from-mlflow": _render_package_from_mlflow,
    "validate-runtime": _render_validate_runtime,
    "runtime-validations": _render_runtime_validations,
    "benchmarks": _render_benchmarks,
    "preview-compatibility": _render_preview_compatibility,
    "compatibility-matrix": _render_compatibility_matrix,
    "rollouts": _render_rollouts,
    "status": _render_status,
    "telemetry": _render_telemetry,
    "evidence": _render_evidence,
    "ingest-evidence": _render_ingest_evidence,
}


def _print_hub_payload(action: str, payload: dict) -> None:
    """Print a Hub Lite payload using the renderer registered for its action."""
    HUB_PRINTERS.get(action, _render_generic_hub_payload)(action, payload)


def _print_hub_readiness(payload: dict[str, Any]) -> None:
    """Print the full deployment readiness verdict in an operator-readable form."""
    status = str(payload.get("status") or "unknown")
    color = _hub_status_color(status)
    console.print(f"[bold {color}]Hub readiness: {status}[/bold {color}]")
    if payload.get("headline"):
        console.print(str(payload["headline"]))
    if payload.get("next_action"):
        console.print(f"Next action: {payload['next_action']}")

    selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
    if selection:
        console.print(
            "Path: "
            f"{selection.get('model_id') or 'package'} -> "
            f"{selection.get('runtime_target_id') or 'auto'} -> "
            f"{selection.get('device_id') or 'edge'}"
        )

    table = Table(title="Readiness Gates")
    table.add_column("Gate")
    table.add_column("Status")
    table.add_column("State")
    table.add_column("Detail")
    for gate in payload.get("gates", []):
        if not isinstance(gate, dict):
            continue
        table.add_row(
            str(gate.get("label") or gate.get("gate_id") or ""),
            str(gate.get("status") or ""),
            str(gate.get("state") or ""),
            str(gate.get("detail") or ""),
        )
    console.print(table)

    actions = [action for action in payload.get("actions", []) if isinstance(action, dict)]
    if actions:
        action_table = Table(title="Readiness Actions")
        action_table.add_column("Action")
        action_table.add_column("Kind")
        action_table.add_column("Gate")
        action_table.add_column("Command")
        for action in actions:
            command = action.get("command") if isinstance(action.get("command"), dict) else {}
            command_text = ""
            if command:
                command_text = f"{command.get('method', '')} {command.get('path', '')}".strip()
            action_table.add_row(
                str(action.get("label") or action.get("action_id") or ""),
                str(action.get("kind") or ""),
                str(action.get("gate_id") or ""),
                command_text,
            )
        console.print(action_table)


def _print_mission_package_plan(payload: dict[str, Any], *, downloaded: bool) -> None:  # noqa: C901  (tracked in #54)
    """Print a compact mission package handoff summary."""
    selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
    mission = payload.get("mission") if isinstance(payload.get("mission"), dict) else {}
    proof_gate = payload.get("proof_gate") if isinstance(payload.get("proof_gate"), dict) else {}
    integrity = payload.get("integrity") if isinstance(payload.get("integrity"), dict) else {}
    deployment_intent = (
        payload.get("deployment_intent")
        if isinstance(payload.get("deployment_intent"), dict)
        else {}
    )
    edge_handoff = (
        payload.get("edge_handoff")
        if isinstance(payload.get("edge_handoff"), dict)
        else {}
    )
    command = (
        deployment_intent.get("command")
        if isinstance(deployment_intent.get("command"), dict)
        else {}
    )
    handoff_commands = (
        edge_handoff.get("commands")
        if isinstance(edge_handoff.get("commands"), dict)
        else {}
    )
    status = str(proof_gate.get("status") or "planned")
    color = _hub_status_color(status)
    label = "downloaded" if downloaded else "planned"
    console.print(f"[bold {color}]Mission package {label}: {status}[/bold {color}]")
    if mission.get("goal"):
        console.print(f"Goal: {mission.get('goal')}")
    console.print(
        "Path: "
        f"{selection.get('model_id') or 'model'} -> "
        f"{selection.get('runtime_target_id') or 'runtime'} -> "
        f"{selection.get('device_id') or 'edge'}"
    )
    if selection.get("package_id"):
        console.print(f"Package: {selection.get('package_id')}")
    sensor_value = mission.get("sensor")
    slot_value = mission.get("slot") or selection.get("slot")
    if sensor_value and slot_value:
        console.print(f"Sensor: {sensor_value} / {slot_value}")
    elif sensor_value:
        console.print(f"Sensor: {sensor_value}")
    elif slot_value:
        console.print(f"Slot: {slot_value}")
    if integrity.get("package_identity_sha256"):
        console.print(f"Package identity: {integrity.get('package_identity_sha256')}")
    if deployment_intent.get("rollout_id"):
        console.print(f"Deploy intent: {deployment_intent.get('rollout_id')}")
    if command.get("path"):
        console.print(f"Command: {command.get('method', 'POST')} {command.get('path')}")
    stage_command = (
        handoff_commands.get("stage_package")
        if isinstance(handoff_commands.get("stage_package"), dict)
        else {}
    )
    apply_command = (
        handoff_commands.get("apply_rollout")
        if isinstance(handoff_commands.get("apply_rollout"), dict)
        else {}
    )
    if stage_command.get("path"):
        console.print(
            f"Stage package: {stage_command.get('method', 'POST')} {stage_command.get('path')}"
        )
    if apply_command.get("path"):
        console.print(
            f"Apply rollout: {apply_command.get('method', 'POST')} {apply_command.get('path')}"
        )


def _print_edge_runtime_mission(payload: dict[str, Any]) -> None:
    """Print the compact selected model/runtime/edge proof."""
    if not payload:
        console.print("[red]Edge runtime mission is not available[/red]")
        return

    status = str(payload.get("status") or "unknown")
    color = _hub_status_color(status)
    console.print(f"[bold {color}]Edge Runtime Mission: {status}[/bold {color}]")
    if payload.get("headline"):
        console.print(str(payload["headline"]))
    if payload.get("detail"):
        console.print(str(payload["detail"]))

    path = payload.get("path") if isinstance(payload.get("path"), dict) else {}
    if path:
        console.print(
            "Path: "
            f"{path.get('model_id') or 'model'} -> "
            f"{path.get('runtime_target_id') or 'runtime'} -> "
            f"{path.get('device_id') or 'edge'}"
        )
    if payload.get("next_action"):
        console.print(f"Next action: {payload['next_action']}")

    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    table = Table(title="On-Device Proof")
    table.add_column("Metric")
    table.add_column("Status")
    table.add_column("Detail")
    for key in (
        "runtime_fit",
        "target_selection",
        "runtime_lane",
        "artifact_fit",
        "live_inventory",
        "performance",
        "resources",
        "runtime_validation",
        "production_admission",
        "ddil_repair",
    ):
        metric = metrics.get(key)
        if not isinstance(metric, dict):
            continue
        table.add_row(
            _mission_metric_label(key),
            _mission_metric_status(metric),
            _mission_metric_detail(metric),
        )
    console.print(table)

    operator_focus = [str(item) for item in payload.get("operator_focus", []) if item]
    if operator_focus:
        focus_table = Table(title="Operator Focus")
        focus_table.add_column("Item")
        for item in operator_focus:
            focus_table.add_row(item)
        console.print(focus_table)


def _mission_metric_label(key: str) -> str:
    labels = {
        "runtime_fit": "Runtime fit",
        "target_selection": "Target selection",
        "runtime_lane": "Runtime lane",
        "artifact_fit": "Artifact fit",
        "live_inventory": "Live inventory",
        "performance": "Performance SLO",
        "resources": "Resource envelope",
        "runtime_validation": "Runtime validation",
        "production_admission": "Production admission",
        "ddil_repair": "DDIL repair",
    }
    return labels.get(key, key.replace("_", " ").title())


def _mission_metric_status(metric: dict[str, Any]) -> str:
    status = metric.get("status") or metric.get("state") or ""
    score = metric.get("score")
    tier = metric.get("tier")
    if score is not None:
        suffix = f"score {score}"
        if tier:
            suffix += f", {tier}"
        return f"{status} ({suffix})" if status else suffix
    if metric.get("apply_allowed") is not None:
        allowed = "allowed" if metric.get("apply_allowed") else "blocked"
        return f"{status} ({allowed})" if status else allowed
    return str(status)


def _mission_metric_detail(metric: dict[str, Any]) -> str:
    if metric.get("detail"):
        return str(metric["detail"])
    if metric.get("label"):
        lane = str(metric["label"])
        engine = metric.get("execution_engine")
        acceleration = metric.get("acceleration")
        parts = [lane]
        if engine:
            parts.append(str(engine))
        if acceleration:
            parts.append(str(acceleration))
        return " / ".join(parts)
    if metric.get("state"):
        return str(metric["state"])
    if metric.get("best_runtime_target_id"):
        return f"best target {metric['best_runtime_target_id']}"
    return ""


def _hub_status_color(status: str) -> str:
    if status == "go":
        return "green"
    if status == "blocked":
        return "red"
    if status == "attention":
        return "yellow"
    return "white"
