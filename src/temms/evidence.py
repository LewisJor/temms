"""
Post-mission evidence bundle construction.
"""

from __future__ import annotations

import json
import socket
import tempfile
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any


def build_evidence_bundle(
    state: Any,
    telemetry_limit: int | None = None,
    decision_limit: int = 100,
    include_benchmarks: bool = True,
) -> dict[str, Any]:
    """Build a portable evidence bundle from the current edge agent state."""
    decisions = decision_timeline(state, limit=decision_limit)
    telemetry_events = (
        state.telemetry.read(limit=telemetry_limit)
        if getattr(state, "telemetry", None) is not None
        else []
    )
    rollout_events = rollout_timeline(state, limit=decision_limit)
    runtime_validations = runtime_validation_timeline(state, limit=decision_limit)
    hub_benchmarks = hub_benchmark_timeline(state, limit=decision_limit)
    package_imports = package_import_timeline(state, limit=decision_limit)

    payload = {
        "schema_version": "temms-evidence-bundle/v1",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "hub_lite": (
            state.hub_lite.export_bundle().get("hub_lite")
            if getattr(state, "hub_lite", None) is not None
            else None
        ),
        "deployment_state": _deployment_state(state),
        "diagnostics": _diagnostics(state),
        "slots": [_slot_to_dict(slot) for slot in state.slot_manager.list_slots()],
        "runtime_slots": state.inference_runtime.get_all_slots_info(),
        "conditions": {
            path: condition.to_dict() for path, condition in state.condition_store.get_all().items()
        },
        "condition_snapshot": state.condition_store.get_snapshot(),
        "models": [model.to_dict() for model in state.model_cache.list_models()],
        "packages": [_package_to_dict(package) for package in state.model_cache.list_packages()],
        "decisions": decisions,
        "telemetry": {
            "count": len(telemetry_events),
            "events": telemetry_events,
        },
        "rollout_events": rollout_events,
        "runtime_validations": runtime_validations,
        "hub_benchmarks": hub_benchmarks,
        "package_imports": package_imports,
        "benchmarks": _benchmark_results(state) if include_benchmarks else [],
        "timeline": combined_timeline(
            decisions,
            telemetry_events,
            rollout_events,
            runtime_validations,
            hub_benchmarks,
            package_imports,
        ),
    }
    payload["integrity"] = {
        "payload_sha256": _canonical_hash(payload),
        "algorithm": "sha256/json-canonical-v1",
    }
    return payload


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def decision_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return recent decision log entries across all slots."""
    decisions: list[dict[str, Any]] = []
    slots = state.slot_manager.list_slots()
    if slots:
        per_slot_limit = max(limit, 1)
        for slot in slots:
            decisions.extend(
                _normalize_decision(entry)
                for entry in state.slot_manager.get_decision_log(
                    slot_name=slot.name,
                    limit=per_slot_limit,
                )
            )
    else:
        decisions.extend(
            _normalize_decision(entry) for entry in state.slot_manager.get_decision_log(limit=limit)
        )
    decisions.sort(key=lambda entry: entry.get("created_at", ""), reverse=True)
    return decisions[:limit]


def rollout_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return rollout history entries across local Hub Lite state."""
    hub_lite = getattr(state, "hub_lite", None)
    if hub_lite is None:
        return []

    events: list[dict[str, Any]] = []
    for rollout in hub_lite.list_rollouts():
        rollout_id = rollout.get("rollout_id")
        for history in rollout.get("history", []) or []:
            event = {
                "rollout_id": rollout_id,
                "device_id": rollout.get("device_id"),
                "package_id": rollout.get("package_id"),
                "slot": rollout.get("slot"),
                "state": history.get("state"),
                "detail": history.get("detail"),
                "actor": history.get("actor"),
                "updated_at": history.get("updated_at"),
            }
            events.append(event)
    events.sort(key=lambda entry: entry.get("updated_at") or "", reverse=True)
    return events[:limit]


def runtime_validation_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return runtime target validation evidence records."""
    hub_lite = getattr(state, "hub_lite", None)
    if hub_lite is None:
        return []
    list_validations = getattr(hub_lite, "list_runtime_validations", None)
    if not callable(list_validations):
        return []
    return list_validations(limit=limit)


def hub_benchmark_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return hardware-aware benchmark evidence records from Hub Lite."""
    hub_lite = getattr(state, "hub_lite", None)
    if hub_lite is None:
        return []
    list_benchmarks = getattr(hub_lite, "list_benchmarks", None)
    if not callable(list_benchmarks):
        return []
    return list_benchmarks(limit=limit)


def package_import_timeline(state: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Return package import audit records from the local model cache."""
    model_cache = getattr(state, "model_cache", None)
    if model_cache is None:
        return []

    events: list[dict[str, Any]] = []
    for package in model_cache.list_packages():
        manifest = package.manifest if isinstance(package.manifest, dict) else {}
        import_audit = manifest.get("_temms_import")
        if not isinstance(import_audit, dict):
            import_audit = {}
        signature = import_audit.get("signature") if isinstance(import_audit, dict) else None
        signature_summary = None
        if isinstance(signature, dict):
            signature_summary = {
                "schema_version": signature.get("schema_version"),
                "algorithm": signature.get("algorithm"),
                "signer": signature.get("signer"),
                "key_fingerprint": signature.get("key_fingerprint"),
                "signed_at": signature.get("signed_at"),
                "manifest_sha256": signature.get("manifest_sha256"),
            }
        events.append(
            {
                "schema_version": "temms-package-import-event/v1",
                "package_id": package.id,
                "name": package.name,
                "version": package.version,
                "source": package.source,
                "slot": _package_import_slot(manifest),
                "slots": _package_import_slots(manifest),
                "imported_at": import_audit.get("imported_at") or package.imported_at.isoformat(),
                "source_sha256": import_audit.get("source_sha256"),
                "source_type": import_audit.get("source_type"),
                "hashes_verified": import_audit.get("hashes_verified"),
                "signature_required": import_audit.get("signature_required"),
                "signature_verified": import_audit.get("signature_verified"),
                "signature": signature_summary,
                "device_profile": import_audit.get("device_profile"),
                "warnings": import_audit.get("warnings", []),
                "import": import_audit,
            }
        )
    events.sort(key=lambda entry: entry.get("imported_at") or "", reverse=True)
    return events[:limit]


def combined_timeline(
    decisions: list[dict[str, Any]],
    telemetry_events: list[dict[str, Any]],
    rollout_events: list[dict[str, Any]] | None = None,
    runtime_validations: list[dict[str, Any]] | None = None,
    hub_benchmarks: list[dict[str, Any]] | None = None,
    package_imports: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Merge decisions, rollouts, telemetry, validation, benchmark, and import evidence."""
    timeline: list[dict[str, Any]] = []
    for decision in decisions:
        timeline.append(
            {
                "kind": "decision",
                "timestamp": decision.get("created_at"),
                "slot": decision.get("slot"),
                "summary": (
                    f"{decision.get('from_model') or 'none'} -> "
                    f"{decision.get('to_model')} "
                    f"({decision.get('trigger_type')})"
                ),
                "record": decision,
            }
        )
    for event in telemetry_events:
        timeline.append(
            {
                "kind": "telemetry",
                "timestamp": event.get("timestamp"),
                "slot": event.get("payload", {}).get("slot"),
                "summary": event.get("event_type"),
                "record": event,
            }
        )
    for event in rollout_events or []:
        actor = event.get("actor") or "unknown"
        timeline.append(
            {
                "kind": "rollout",
                "timestamp": event.get("updated_at"),
                "slot": event.get("slot"),
                "summary": (f"{event.get('rollout_id')} {event.get('state')} " f"by {actor}"),
                "record": event,
            }
        )
    for validation in runtime_validations or []:
        result = validation.get("result") if isinstance(validation.get("result"), dict) else {}
        status = "passed" if result.get("ok") else "failed"
        if result.get("dry_run"):
            status = "previewed"
        runtime_target = validation.get("runtime_target_id") or "runtime"
        timeline.append(
            {
                "kind": "runtime_validation",
                "timestamp": validation.get("created_at"),
                "slot": None,
                "summary": (
                    f"{validation.get('package_id') or validation.get('package_path')} "
                    f"{status} on {runtime_target}"
                ),
                "record": validation,
            }
        )
    for benchmark in hub_benchmarks or []:
        result = benchmark.get("result") if isinstance(benchmark.get("result"), dict) else {}
        latency = result.get("latency_ms") if isinstance(result.get("latency_ms"), dict) else {}
        p95 = latency.get("p95")
        summary = f"{benchmark.get('model_id') or 'model'} benchmarked"
        if p95 is not None:
            summary += f" p95={p95}ms"
        if benchmark.get("device_id"):
            summary += f" on {benchmark['device_id']}"
        timeline.append(
            {
                "kind": "benchmark",
                "timestamp": benchmark.get("created_at"),
                "slot": result.get("slot"),
                "summary": summary,
                "record": benchmark,
            }
        )
    for package_import in package_imports or []:
        status = "verified" if package_import.get("signature_verified") else "imported"
        signer = (package_import.get("signature") or {}).get("signer")
        summary = f"{package_import.get('package_id')} {status}"
        if signer:
            summary += f" by {signer}"
        timeline.append(
            {
                "kind": "package_import",
                "timestamp": package_import.get("imported_at"),
                "slot": package_import.get("slot"),
                "summary": summary,
                "record": package_import,
            }
        )
    timeline.sort(key=lambda entry: entry.get("timestamp") or "", reverse=True)
    return timeline


def _normalize_decision(entry: dict[str, Any]) -> dict[str, Any]:
    decision = dict(entry)
    snapshot = decision.get("conditions_snapshot")
    if isinstance(snapshot, str):
        try:
            decision["conditions_snapshot"] = json.loads(snapshot)
        except Exception:
            pass
    metadata = decision.get("audit_metadata")
    if isinstance(metadata, str):
        try:
            decision["audit_metadata"] = json.loads(metadata)
        except Exception:
            pass
    for key, value in list(decision.items()):
        if hasattr(value, "isoformat"):
            decision[key] = value.isoformat()
    return decision


def _slot_to_dict(slot: Any) -> dict[str, Any]:
    data = {
        "name": slot.name,
        "description": slot.description,
        "required": slot.required,
        "default_model": slot.default_model,
        "active_model_id": slot.active_model_id,
        "state": slot.state.value,
        "candidates": slot.candidates,
        "metadata": slot.metadata,
        "updated_at": slot.updated_at.isoformat(),
    }
    if slot.operator_override is not None:
        data["operator_override"] = {
            "model_id": slot.operator_override.model_id,
            "reason": slot.operator_override.reason,
            "source": slot.operator_override.source,
            "set_at": slot.operator_override.set_at.isoformat(),
            "expires_at": (
                slot.operator_override.expires_at.isoformat()
                if slot.operator_override.expires_at
                else None
            ),
        }
    return data


def _package_to_dict(package: Any) -> dict[str, Any]:
    return {
        "id": package.id,
        "name": package.name,
        "version": package.version,
        "source": package.source,
        "imported_at": package.imported_at.isoformat(),
        "manifest": package.manifest,
    }


def _safe_json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _model_dict(model: Any | None) -> dict[str, Any] | None:
    return model.to_dict() if model is not None else None


class EvidenceBundleBuilder:
    """Build portable evidence directly from local stores."""

    def __init__(
        self,
        slot_manager: Any,
        condition_store: Any,
        policy_engine: Any,
        model_cache: Any,
    ):
        self.slot_manager = slot_manager
        self.condition_store = condition_store
        self.policy_engine = policy_engine
        self.model_cache = model_cache

    def build(
        self,
        slot_name: str | None = None,
        limit: int = 100,
        runtime_slots: dict[str, dict[str, Any]] | None = None,
        offline_mode: bool = False,
        pending_operations: list[dict[str, Any]] | None = None,
        deployment_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        models = {model.id: model for model in self.model_cache.list_models()}
        packages = {package.id: package for package in self.model_cache.list_packages()}
        decisions = self.slot_manager.get_decision_log(slot_name=slot_name, limit=limit)
        enriched_decisions = [
            self._decision_evidence(decision, models, packages)
            for decision in decisions
        ]
        policies = [
            policy.model_dump(mode="json", exclude_none=True)
            for policy in self.policy_engine.list_policies()
        ]
        conditions = self.condition_store.get_all()

        payload: dict[str, Any] = {
            "schema_version": "temms-evidence-bundle/v1",
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "scope": {
                "slot": slot_name,
                "decision_limit": limit,
            },
            "runtime": {
                "offline_mode": offline_mode,
                "deployment_state": deployment_state,
                "pending_operations": pending_operations or [],
                "runtime_slots": runtime_slots or {},
            },
            "slots": [_slot_to_dict(slot) for slot in self.slot_manager.list_slots()],
            "conditions": {
                "snapshot": self.condition_store.get_snapshot(),
                "values": {
                    path: value.to_dict()
                    for path, value in sorted(conditions.items())
                },
            },
            "policies": policies,
            "models": [_model_dict(model) for model in models.values()],
            "packages": [_package_to_dict(package) for package in packages.values()],
            "decisions": enriched_decisions,
        }
        payload["integrity"] = {
            "payload_sha256": _canonical_hash(payload),
            "algorithm": "sha256/json-canonical-v1",
        }
        return payload

    def _decision_evidence(
        self,
        decision: dict[str, Any],
        models: dict[str, Any],
        packages: dict[str, Any],
    ) -> dict[str, Any]:
        to_model = models.get(decision.get("to_model"))
        from_model = models.get(decision.get("from_model"))
        package = packages.get(to_model.package_id) if to_model is not None else None

        return {
            "id": decision.get("id"),
            "slot": decision.get("slot"),
            "from_model": decision.get("from_model"),
            "to_model": decision.get("to_model"),
            "trigger_type": decision.get("trigger_type"),
            "trigger_detail": decision.get("trigger_detail"),
            "created_at": decision.get("created_at"),
            "conditions_snapshot": _safe_json_loads(
                decision.get("conditions_snapshot"),
                {},
            ),
            "model_evidence": {
                "from_model": _model_dict(from_model),
                "to_model": _model_dict(to_model),
                "to_package": _package_to_dict(package) if package is not None else None,
            },
        }


def _package_import_slot(manifest: dict[str, Any]) -> str | None:
    slots = _package_import_slots(manifest)
    return slots[0] if slots else None


def _package_import_slots(manifest: dict[str, Any]) -> list[str]:
    slots: set[str] = set()
    for policy in manifest.get("policies", []) or []:
        if isinstance(policy, dict) and policy.get("slot"):
            slots.add(str(policy["slot"]))
    compatibility = manifest.get("compatibility") if isinstance(manifest, dict) else {}
    if isinstance(compatibility, dict):
        declared = compatibility.get("slots")
        if isinstance(declared, list):
            slots.update(str(slot) for slot in declared if slot)
    return sorted(slots)


def _deployment_state(state: Any) -> dict[str, Any] | None:
    store = getattr(state, "deployment_state", None)
    if store is None:
        return None
    payload = store._read()
    return {
        "state": payload.get("state"),
        "reason": payload.get("reason"),
        "updated_at": payload.get("updated_at"),
    }


def _diagnostics(state: Any) -> dict[str, Any]:
    """Return a doctor-like diagnostic snapshot for evidence bundles."""
    from temms import __version__
    from temms.core.cache_health import model_cache_health
    from temms.core.runtime_profiles import detect_runtime_capabilities, known_device_profiles

    capabilities = detect_runtime_capabilities()
    daemon_config = getattr(state, "daemon_config", None)
    model_cache = getattr(state, "model_cache", None)
    model_storage = getattr(state, "model_storage", None)

    path_candidates: list[tuple[str, Path]] = []
    if daemon_config is not None:
        if getattr(daemon_config, "db_path", None) is not None:
            path_candidates.append(("database_dir", daemon_config.db_path.parent))
        if getattr(daemon_config, "model_dir", None) is not None:
            path_candidates.append(("model_dir", daemon_config.model_dir))
            path_candidates.append(("cache_dir", daemon_config.model_dir.parent / "cache"))
            path_candidates.append(("package_dir", daemon_config.model_dir.parent / "packages"))
        if getattr(daemon_config, "policy_dir", None) is not None:
            path_candidates.append(("policy_dir", daemon_config.policy_dir))
    else:
        if model_cache is not None and getattr(model_cache, "db_path", None) is not None:
            path_candidates.append(("database_dir", model_cache.db_path.parent))
        if model_storage is not None and getattr(model_storage, "model_dir", None) is not None:
            path_candidates.append(("model_dir", model_storage.model_dir))

    paths = [_path_report(name, path) for name, path in path_candidates]

    cache_report = None
    if model_cache is not None:
        models = model_cache.list_models()
        storage_stats = (
            model_storage.get_storage_stats()
            if model_storage is not None
            else {"model_count": None, "total_size_bytes": None, "storage_path": None}
        )
        cache_report = {
            "database": str(getattr(model_cache, "db_path", "")),
            "models": len(models),
            "packages": len(model_cache.list_packages()),
            "model_count_on_disk": storage_stats.get("model_count"),
            "total_size_bytes": storage_stats.get("total_size_bytes"),
            "storage_path": storage_stats.get("storage_path"),
            "health": model_cache_health(models),
        }

    port = None
    ports = []
    if daemon_config is not None:
        host = getattr(daemon_config, "inference_host", "0.0.0.0")
        port_number = getattr(daemon_config, "inference_port", None)
        if port_number is not None:
            port = {
                "name": "api",
                "host": host,
                "check_host": "127.0.0.1",
                "port": port_number,
                "status": _port_status("127.0.0.1", int(port_number)),
            }
            ports.append(port)

    return {
        "schema_version": "temms-diagnostics/v1",
        "temms_version": __version__,
        "system": capabilities.to_dict(),
        "known_device_profiles": known_device_profiles(),
        "paths": paths,
        "port": port,
        "ports": ports,
        "model_cache": cache_report,
    }


def _path_report(name: str, path: Path) -> dict[str, Any]:
    exists = path.exists()
    writable_target = path if exists else _nearest_existing_parent(path)
    write_probe = _probe_path_writable(writable_target)
    return {
        "name": name,
        "path": str(path),
        "exists": exists,
        "writable_target": str(writable_target),
        "writable": write_probe["ok"],
        "write_probe": write_probe,
    }


def _nearest_existing_parent(path: Path) -> Path:
    """Return the closest existing parent for a path that may not exist yet."""
    current = path.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _probe_path_writable(path: Path) -> dict[str, Any]:
    """Create and delete a tiny probe file to prove directory writability."""
    if not path.exists():
        return {
            "ok": False,
            "path": str(path),
            "attempted": False,
            "error": "target does not exist",
        }
    if not path.is_dir():
        return {
            "ok": False,
            "path": str(path),
            "attempted": False,
            "error": "target is not a directory",
        }

    try:
        with tempfile.NamedTemporaryFile(
            prefix=".temms-evidence-",
            dir=path,
            delete=True,
        ) as probe:
            probe.write(b"ok")
            probe.flush()
        return {"ok": True, "path": str(path), "attempted": True, "error": None}
    except Exception as e:
        return {
            "ok": False,
            "path": str(path),
            "attempted": True,
            "error": str(e),
        }


def _port_status(host: str, port: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex((host, port)) == 0:
            return "in use"
    return "free"


def _benchmark_results(state: Any) -> list[dict[str, Any]]:
    benchmark_dir = _benchmark_dir(state)
    if benchmark_dir is None or not benchmark_dir.exists():
        return []

    results: list[dict[str, Any]] = []
    for path in sorted(benchmark_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.setdefault("path", str(path))
            results.append(payload)
        except Exception:
            results.append({"path": str(path), "error": "invalid benchmark JSON"})
    return results


def _benchmark_dir(state: Any) -> Path | None:
    daemon_config = getattr(state, "daemon_config", None)
    if daemon_config is not None and getattr(daemon_config, "model_dir", None) is not None:
        return daemon_config.model_dir.parent / "benchmarks"
    model_cache = getattr(state, "model_cache", None)
    if model_cache is not None and getattr(model_cache, "db_path", None) is not None:
        return model_cache.db_path.parent / "benchmarks"
    return None
