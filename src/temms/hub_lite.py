"""
Hub Lite state store.

Hub Lite is intentionally small for the MVP: it tracks device enrollment,
heartbeats/inventory, package catalog entries, rollout assignments, rollout
state, and air-gap bundle exchange in a local JSON file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import shlex
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from temms.core.atomic import write_json_atomic
from temms.core.runtime_profiles import (
    default_runtime_targets,
    normalize_device_profile,
    runtime_constraints_satisfied,
    runtime_lane_summary,
)

ROLLOUT_STATES = {
    "assigned",
    "downloading",
    "imported",
    "activated",
    "failed",
    "rolled_back",
}

ROLLOUT_PLAN_STATES = {
    "ready",
    "advancing",
    "blocked",
    "paused",
    "completed",
    "failed",
}

PACKAGE_PROMOTION_STATES = {
    "candidate",
    "validated",
    "approved",
    "released",
    "retired",
}

PACKAGE_PROMOTION_TRANSITIONS = {
    "candidate": {"validated", "retired"},
    "validated": {"approved", "retired"},
    "approved": {"released", "retired"},
    "released": {"retired"},
    "retired": set(),
}

READINESS_REMEDIATION_ACTOR = "operator:readiness-remediation"
RUNTIME_REMEDIATION_ACTOR = "operator:runtime-remediation"
READINESS_REMEDIATION_ID_PREFIX = "readiness"
READINESS_HEARTBEAT_STALE_SECONDS = 300
READINESS_BENCHMARK_STALE_SECONDS = 86400
EDGE_RUNTIME_PROOF_SCHEMA_VERSION = "temms-edge-runtime-proof/v1"
EDGE_RUNTIME_PROOF_ATTESTATION_SCHEMA_VERSION = "temms-edge-runtime-proof-attestation/v1"
EDGE_MISSION_PACKAGE_SCHEMA_VERSION = "temms-edge-mission-package/v1"
EDGE_MISSION_PACKAGE_IDENTITY_SCHEMA_VERSION = (
    "temms-edge-mission-package-identity/v1"
)
EDGE_MISSION_PACKAGE_COMPONENT_DIGESTS_SCHEMA_VERSION = (
    "temms-edge-mission-package-component-digests/v1"
)
RUNTIME_DECISION_SCHEMA_VERSION = "temms-runtime-decision/v1"
EDGE_EXECUTION_CONTRACT_SCHEMA_VERSION = "temms-edge-execution-contract/v1"
EDGE_EXECUTION_MANIFEST_SCHEMA_VERSION = "temms-edge-execution-manifest/v1"
RUNTIME_WORKBENCH_SCHEMA_VERSION = "temms-runtime-workbench/v1"
RUNTIME_DECISION_TRACE_SCHEMA_VERSION = "temms-runtime-decision-trace/v1"
EDGE_RUNTIME_PROOF_COMPONENT_DIGESTS_SCHEMA_VERSION = (
    "temms-edge-runtime-proof-component-digests/v1"
)


class PackageArtifactNotFound(ValueError):
    """Raised when a package catalog entry has no readable artifact."""


class PackageArtifactIntegrityError(ValueError):
    """Raised when a cataloged package artifact changed after registration."""


@dataclass
class HubLiteStore:
    """Persistent JSON store for Hub Lite."""

    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._empty())

    def enroll_device(
        self,
        device_id: str | None = None,
        profile: str | None = None,
        labels: dict[str, str] | None = None,
        inventory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Enroll or update a device."""
        now = _now()
        data = self._read()
        device_id = device_id or f"dev-{uuid.uuid4().hex[:12]}"
        current = data["devices"].get(device_id, {})
        device = {
            **current,
            "device_id": device_id,
            "profile": normalize_device_profile(profile or current.get("profile")) or "unknown",
            "labels": labels or current.get("labels", {}),
            "inventory": inventory or current.get("inventory", {}),
            "enrolled_at": current.get("enrolled_at", now),
            "last_seen_at": now,
        }
        data["devices"][device_id] = device
        self._write(data)
        return device

    def heartbeat(
        self,
        device_id: str,
        status: str = "online",
        inventory: dict[str, Any] | None = None,
        deployment_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record heartbeat, inventory, and deployment status for a device."""
        now = _now()
        data = self._read()
        device = data["devices"].setdefault(
            device_id,
            {
                "device_id": device_id,
                "profile": "unknown",
                "labels": {},
                "inventory": {},
                "enrolled_at": now,
            },
        )
        device["status"] = status
        device["last_seen_at"] = now
        if inventory is not None:
            device["inventory"] = inventory
        if deployment_status is not None:
            data["deployment_status"][device_id] = {
                **deployment_status,
                "device_id": device_id,
                "updated_at": now,
            }
        self._write(data)
        return device

    def list_devices(self) -> list[dict[str, Any]]:
        """Return enrolled devices."""
        return list(self._read()["devices"].values())

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        """Return one enrolled device."""
        return self._read()["devices"].get(device_id)

    def upsert_package(
        self,
        package: dict[str, Any],
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Add or update a model package catalog entry."""
        data = self._read()
        package_id = package.get("package_id")
        if not package_id:
            raise ValueError("package_id is required")
        current = data["packages"].get(package_id, {})
        now = _now()
        entry = {
            **current,
            **package,
            "package_id": package_id,
            "updated_at": now,
        }
        entry.setdefault("created_at", current.get("created_at", entry["updated_at"]))
        entry.setdefault("device_profiles", [])
        entry["promotion"] = _normalize_package_promotion(
            current=current,
            incoming=package.get("promotion"),
            package_id=package_id,
            actor=actor,
            updated_at=now,
        )
        if actor:
            entry["updated_by"] = actor
            entry.setdefault("created_by", current.get("created_by") or actor)
            metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            entry["metadata"] = metadata
            metadata.setdefault("audit", {})["catalog_actor"] = actor
            metadata["audit"]["cataloged_at"] = now
        data["packages"][package_id] = entry
        self._write(data)
        return entry

    def promote_package(
        self,
        package_id: str,
        state: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a package promotion lifecycle transition."""
        state = state.lower().strip()
        if state not in PACKAGE_PROMOTION_STATES:
            raise ValueError(f"Invalid package promotion state: {state}")
        data = self._read()
        package = data["packages"].get(package_id)
        if package is None:
            raise ValueError(f"Unknown package: {package_id}")

        now = _now()
        promotion = _normalize_package_promotion(
            current=package,
            incoming=package.get("promotion"),
            package_id=package_id,
            actor=actor,
            updated_at=now,
        )
        current_state = promotion.get("state") or "candidate"
        if state != current_state and state not in PACKAGE_PROMOTION_TRANSITIONS[current_state]:
            raise ValueError(f"Invalid package promotion transition: {current_state} -> {state}")

        event = {
            "state": state,
            "from_state": current_state,
            "updated_at": now,
            "actor": actor,
            "reason": reason,
            "evidence": evidence or {},
        }
        history = promotion.setdefault("history", [])
        if state != current_state or not history:
            history.append(event)
        promotion.update(
            {
                "schema_version": "temms-package-promotion/v1",
                "state": state,
                "updated_at": now,
                "actor": actor,
                "reason": reason,
                "evidence": evidence or {},
            }
        )
        package["promotion"] = promotion
        package["updated_at"] = now
        if actor:
            package["updated_by"] = actor
        data["packages"][package_id] = package
        self._write(data)
        return package

    def upsert_package_from_source(
        self,
        package_path: Path,
        *,
        require_signature: bool = True,
        signing_key: str | None = None,
        device_profiles: list[str] | None = None,
        strict_metadata: bool = False,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Derive and upsert a catalog entry from a package artifact."""
        from temms.core.package_catalog import catalog_entry_from_package

        entry = catalog_entry_from_package(
            package_path,
            require_signature=require_signature,
            signing_key=signing_key,
            device_profiles=device_profiles,
            strict_metadata=strict_metadata,
        )
        return self.upsert_package(entry, actor=actor)

    def list_packages(self) -> list[dict[str, Any]]:
        """Return package catalog entries."""
        return list(self._read()["packages"].values())

    def get_package(self, package_id: str) -> dict[str, Any] | None:
        """Return one package catalog entry."""
        return self._read()["packages"].get(package_id)

    def upsert_deployment_draft(
        self,
        draft_id: str = "active",
        *,
        package_id: str,
        device_id: str,
        runtime_target_id: str | None = None,
        slot: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Persist an operator-selected deployment candidate."""
        data = self._read()
        if package_id not in data["packages"]:
            raise ValueError(f"Unknown package: {package_id}")
        if device_id not in data["devices"]:
            raise ValueError(f"Unknown device: {device_id}")
        runtime_target = None
        if runtime_target_id:
            runtime_target = _runtime_targets_with_defaults(data).get(runtime_target_id)
            if runtime_target is None:
                raise ValueError(f"Unknown runtime target: {runtime_target_id}")

        now = _now()
        current = data.setdefault("deployment_drafts", {}).get(draft_id, {})
        draft = {
            **current,
            "schema_version": "temms-deployment-draft/v1",
            "draft_id": draft_id,
            "package_id": package_id,
            "device_id": device_id,
            "runtime_target_id": runtime_target_id,
            "slot": slot,
            "runtime_target": _rollout_runtime_target_summary(runtime_target),
            "actor": actor,
            "updated_at": now,
        }
        draft.setdefault("created_at", current.get("created_at", now))
        data["deployment_drafts"][draft_id] = draft
        self._write(data)
        return draft

    def get_deployment_draft(self, draft_id: str = "active") -> dict[str, Any] | None:
        """Return a saved deployment candidate."""
        return self._read().get("deployment_drafts", {}).get(draft_id)

    def list_runtime_targets(self) -> list[dict[str, Any]]:
        """Return container runtime targets, including built-in defaults."""
        data = self._read()
        runtime_targets = _runtime_targets_with_defaults(data)
        return list(runtime_targets.values())

    def get_runtime_target(self, runtime_target_id: str) -> dict[str, Any] | None:
        """Return one runtime target from the container catalog."""
        data = self._read()
        return _runtime_targets_with_defaults(data).get(runtime_target_id)

    def record_runtime_validation(
        self,
        runtime_target_id: str,
        result: dict[str, Any],
        *,
        package_id: str | None = None,
        package_path: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Persist runtime target validation evidence without storing secrets."""
        data = self._read()
        runtime_target = _runtime_targets_with_defaults(data).get(runtime_target_id)
        if runtime_target is None:
            raise ValueError(f"Unknown runtime target: {runtime_target_id}")
        package = None
        if package_id:
            package = data["packages"].get(package_id)
            if package is None:
                raise ValueError(f"Unknown package: {package_id}")

        now = _now()
        validation_id = f"runtime-validation-{uuid.uuid4().hex[:12]}"
        record = {
            "schema_version": "temms-runtime-validation-record/v1",
            "validation_id": validation_id,
            "runtime_target_id": runtime_target_id,
            "runtime_target": _rollout_runtime_target_summary(runtime_target),
            "package_id": package_id,
            "package": _package_compatibility_summary(package) if package else None,
            "package_path": package_path or result.get("package_path"),
            "source_sha256": (
                package.get("source_sha256") or package.get("sha256") if package else None
            ),
            "result": _sanitize_runtime_validation_result(result),
            "actor": actor,
            "created_at": now,
        }
        data.setdefault("runtime_validations", {})[validation_id] = record
        self._write(data)
        return record

    def list_runtime_validations(
        self,
        *,
        package_id: str | None = None,
        runtime_target_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return recorded runtime target validation evidence."""
        validations = list(self._read().get("runtime_validations", {}).values())
        if package_id:
            validations = [
                validation
                for validation in validations
                if validation.get("package_id") == package_id
            ]
        if runtime_target_id:
            validations = [
                validation
                for validation in validations
                if validation.get("runtime_target_id") == runtime_target_id
            ]
        validations.sort(key=lambda validation: validation.get("created_at", ""), reverse=True)
        if limit is not None:
            return validations[:limit]
        return validations

    def record_benchmark(
        self,
        result: dict[str, Any],
        *,
        device_id: str | None = None,
        package_id: str | None = None,
        runtime_target_id: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Persist hardware-aware benchmark evidence for fleet comparison."""
        data = self._read()
        device = None
        if device_id:
            device = data["devices"].get(device_id)
            if device is None:
                raise ValueError(f"Unknown device: {device_id}")
        package = None
        if package_id:
            package = data["packages"].get(package_id)
            if package is None:
                raise ValueError(f"Unknown package: {package_id}")
        runtime_target = None
        if runtime_target_id:
            runtime_target = _runtime_targets_with_defaults(data).get(runtime_target_id)
            if runtime_target is None:
                raise ValueError(f"Unknown runtime target: {runtime_target_id}")

        now = _now()
        benchmark_id = f"benchmark-{uuid.uuid4().hex[:12]}"
        record = {
            "schema_version": "temms-hub-benchmark-record/v1",
            "benchmark_id": benchmark_id,
            "device_id": device_id,
            "device": _device_compatibility_summary(device) if device else None,
            "package_id": package_id,
            "package": _package_compatibility_summary(package) if package else None,
            "source_sha256": (
                package.get("source_sha256") or package.get("sha256") if package else None
            ),
            "runtime_target_id": runtime_target_id,
            "runtime_target": _rollout_runtime_target_summary(runtime_target),
            "model_id": result.get("model_id"),
            "result": _sanitize_benchmark_result(result),
            "actor": actor,
            "created_at": now,
        }
        data.setdefault("benchmarks", {})[benchmark_id] = record
        self._write(data)
        return record

    def delete_benchmark(self, benchmark_id: str) -> bool:
        """Remove one benchmark evidence record from the local store."""
        clean_id = str(benchmark_id or "").strip()
        if not clean_id:
            return False
        data = self._read()
        benchmarks = data.get("benchmarks")
        if not isinstance(benchmarks, dict) or clean_id not in benchmarks:
            return False
        del benchmarks[clean_id]
        self._write(data)
        return True

    def list_benchmarks(
        self,
        *,
        device_id: str | None = None,
        package_id: str | None = None,
        runtime_target_id: str | None = None,
        model_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return recorded hardware benchmark evidence."""
        benchmarks = list(self._read().get("benchmarks", {}).values())
        if device_id:
            benchmarks = [
                benchmark for benchmark in benchmarks if benchmark.get("device_id") == device_id
            ]
        if package_id:
            benchmarks = [
                benchmark for benchmark in benchmarks if benchmark.get("package_id") == package_id
            ]
        if runtime_target_id:
            benchmarks = [
                benchmark
                for benchmark in benchmarks
                if benchmark.get("runtime_target_id") == runtime_target_id
            ]
        if model_id:
            benchmarks = [
                benchmark for benchmark in benchmarks if benchmark.get("model_id") == model_id
            ]
        benchmarks.sort(key=lambda benchmark: benchmark.get("created_at", ""), reverse=True)
        if limit is not None:
            return benchmarks[:limit]
        return benchmarks

    def preview_rollout_compatibility(
        self,
        device_id: str,
        package_id: str,
        runtime_target_id: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a side-effect-free package/device/runtime compatibility preview."""
        data = self._read()
        return _rollout_compatibility_preview(
            data,
            device_id=device_id,
            package_id=package_id,
            runtime_target_id=runtime_target_id,
            model_id=model_id,
        )

    def compatibility_matrix(
        self,
        *,
        package_ids: list[str] | None = None,
        model_ids: list[str] | None = None,
        device_ids: list[str] | None = None,
        runtime_target_ids: list[str] | None = None,
        include_device_inventory: bool = False,
    ) -> dict[str, Any]:
        """Return a fleet/package/runtime compatibility matrix."""
        data = self._read()
        return _compatibility_matrix(
            data,
            package_ids=package_ids,
            model_ids=model_ids,
            device_ids=device_ids,
            runtime_target_ids=runtime_target_ids,
            include_device_inventory=include_device_inventory,
        )

    def deployment_readiness(
        self,
        *,
        package_id: str | None = None,
        model_id: str | None = None,
        device_id: str | None = None,
        runtime_target_id: str | None = None,
        slot: str | None = None,
    ) -> dict[str, Any]:
        """Return an operator-facing deployment readiness verdict."""
        data = self._read()
        return _deployment_readiness(
            data,
            package_id=package_id,
            model_id=model_id,
            device_id=device_id,
            runtime_target_id=runtime_target_id,
            slot=slot,
        )

    def edge_runtime_proof(
        self,
        *,
        package_id: str | None = None,
        model_id: str | None = None,
        device_id: str | None = None,
        runtime_target_id: str | None = None,
        slot: str | None = None,
        source_action: str = "edge-runtime-mission",
        require_go: bool = False,
        min_runtime_fit: float | None = None,
        require_best_runtime: bool = False,
        require_capability_lock: bool = False,
    ) -> dict[str, Any]:
        """Return a portable proof envelope for a selected edge runtime path."""
        readiness = self.deployment_readiness(
            package_id=package_id,
            model_id=model_id,
            device_id=device_id,
            runtime_target_id=runtime_target_id,
            slot=slot,
        )
        return build_edge_runtime_proof(
            readiness,
            source_action=source_action,
            require_go=require_go,
            min_runtime_fit=min_runtime_fit,
            require_best_runtime=require_best_runtime,
            require_capability_lock=require_capability_lock,
        )

    def upsert_runtime_target(
        self,
        runtime_target: dict[str, Any],
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Add or update a BYO container runtime target."""
        data = self._read()
        runtime_targets = data.setdefault("runtime_targets", default_runtime_targets())
        runtime_target_id = runtime_target.get("runtime_target_id") or runtime_target.get(
            "target_id"
        )
        if not runtime_target_id:
            raise ValueError("runtime_target_id is required")
        image = runtime_target.get("image")
        if not image:
            raise ValueError("runtime target image is required")

        current = runtime_targets.get(runtime_target_id, {})
        now = _now()
        target = {
            **current,
            **runtime_target,
            "runtime_target_id": runtime_target_id,
            "image": image,
            "updated_at": now,
        }
        target.setdefault("name", runtime_target_id)
        target.setdefault("source", current.get("source") or "byo")
        target.setdefault("default", False)
        target.setdefault("device_profiles", [])
        target["device_profiles"] = [
            normalized
            for normalized in (
                normalize_device_profile(profile) for profile in target.get("device_profiles", [])
            )
            if normalized
        ]
        target.setdefault("runtimes", {})
        target.setdefault("accelerators", {})
        target.setdefault("runtime_constraints", {})
        target.setdefault("labels", {})
        target.setdefault("created_at", current.get("created_at", target["updated_at"]))
        target["runtime_lane"] = runtime_lane_summary(target)
        if actor:
            target["updated_by"] = actor
            target.setdefault("created_by", current.get("created_by") or actor)
            target.setdefault("metadata", {}).setdefault("audit", {})["catalog_actor"] = actor
            target["metadata"]["audit"]["cataloged_at"] = now
        runtime_targets[runtime_target_id] = target
        self._write(data)
        return target

    def verified_package_path(self, package_id: str) -> Path:
        """Return the cataloged package path after checking its source digest."""
        package = self.get_package(package_id)
        if package is None:
            raise PackageArtifactNotFound(f"Unknown package: {package_id}")
        return _verified_package_path(package_id, package)

    def package_artifact(self, package_id: str) -> dict[str, Any]:
        """Return archive bytes for a package catalog entry."""
        package = self.get_package(package_id)
        if package is None:
            raise PackageArtifactNotFound(f"Unknown package: {package_id}")

        package_path = self.verified_package_path(package_id)
        filename, artifact_bytes = _package_artifact_payload(Path(package_path))
        artifact_sha256 = _sha256_bytes(artifact_bytes)
        return {
            "package_id": package_id,
            "filename": filename,
            "sha256": artifact_sha256,
            "artifact_sha256": artifact_sha256,
            "source_sha256": package.get("source_sha256") or package.get("sha256"),
            "content": artifact_bytes,
        }

    def assign_rollout(
        self,
        device_id: str,
        package_id: str,
        slot: str | None = None,
        rollout_id: str | None = None,
        runtime_target_id: str | None = None,
        model_id: str | None = None,
        require_runtime_validation: bool = False,
        require_approval: bool = False,
        actor: str | None = None,
        reason: str | None = None,
        rollout_plan_id: str | None = None,
        rollout_plan_batch: int | None = None,
    ) -> dict[str, Any]:
        """Assign a package rollout to one device."""
        data = self._read()
        if rollout_id and rollout_id in data["rollouts"]:
            existing = data["rollouts"][rollout_id]
            _ensure_rollout_request_matches(
                existing,
                device_id=device_id,
                package_id=package_id,
                slot=slot,
                runtime_target_id=runtime_target_id,
                model_id=model_id,
                require_runtime_validation=require_runtime_validation,
                require_approval=require_approval,
                rollout_plan_id=rollout_plan_id,
                rollout_plan_batch=rollout_plan_batch,
            )
            return existing
        if device_id not in data["devices"]:
            raise ValueError(f"Unknown device: {device_id}")
        if package_id not in data["packages"]:
            raise ValueError(f"Unknown package: {package_id}")
        package = data["packages"][package_id]
        promotion = _normalize_package_promotion(
            current=package,
            incoming=package.get("promotion"),
            package_id=package_id,
            actor=None,
            updated_at=_now(),
        )
        if promotion.get("state") != "released":
            raise ValueError(
                f"Package {package_id} is not released for rollout "
                f"(promotion state: {promotion.get('state')})"
            )
        if model_id:
            _validate_package_model(package, package_id=package_id, model_id=model_id)
        preview = _rollout_compatibility_preview(
            data,
            device_id=device_id,
            package_id=package_id,
            runtime_target_id=runtime_target_id,
            model_id=model_id,
        )
        if not preview["compatible"]:
            if runtime_target_id:
                raise ValueError(
                    f"Runtime target {runtime_target_id} is not compatible with "
                    f"package {package_id} on device {device_id}: " + "; ".join(preview["failures"])
                )
            raise ValueError(
                f"Package {package_id} runtime constraints are not compatible with "
                f"device {device_id}: " + "; ".join(preview["failures"])
            )
        runtime_target = (
            _runtime_targets_with_defaults(data).get(runtime_target_id)
            if runtime_target_id
            else None
        )
        runtime_validation = None
        if require_runtime_validation:
            if not runtime_target_id:
                raise ValueError("Runtime validation gate requires a runtime target")
            runtime_validation = _latest_passing_runtime_validation(
                data,
                package_id=package_id,
                runtime_target_id=runtime_target_id,
                package=package,
            )
            if runtime_validation is None:
                raise ValueError(
                    f"No passing runtime validation found for package {package_id} "
                    f"on runtime target {runtime_target_id}"
                )

        now = _now()
        rollout_id = rollout_id or f"rollout-{uuid.uuid4().hex[:12]}"
        rollout = {
            "rollout_id": rollout_id,
            "device_id": device_id,
            "package_id": package_id,
            "model_id": model_id,
            "package_promotion": _package_promotion_summary(package),
            "slot": slot,
            "runtime_target_id": runtime_target_id,
            "runtime_target": _rollout_runtime_target_summary(runtime_target),
            "runtime_validation_required": require_runtime_validation,
            "runtime_validation": _rollout_runtime_validation_summary(runtime_validation),
            "approval_required": require_approval,
            "approval": _rollout_approval(
                required=require_approval,
                actor=None,
                reason=None,
                updated_at=now,
            ),
            "state": "assigned",
            "created_at": now,
            "updated_at": now,
            "actor": actor,
            "reason": reason,
            "rollout_plan_id": rollout_plan_id,
            "rollout_plan_batch": rollout_plan_batch,
            "history": [
                {
                    "state": "assigned",
                    "updated_at": now,
                    "detail": reason or "assigned",
                    "actor": actor,
                }
            ],
        }
        data["rollouts"][rollout_id] = rollout
        self._write(data)
        return rollout

    def create_rollout_plan(
        self,
        *,
        package_id: str,
        device_ids: list[str],
        slot: str | None = None,
        plan_id: str | None = None,
        runtime_target_id: str | None = None,
        model_id: str | None = None,
        batch_size: int = 1,
        require_runtime_validation: bool = False,
        require_approval: bool = False,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Create a coordinated rollout plan across multiple devices."""
        data = self._read()
        if plan_id and plan_id in data.setdefault("rollout_plans", {}):
            existing = data["rollout_plans"][plan_id]
            _ensure_rollout_plan_request_matches(
                existing,
                package_id=package_id,
                device_ids=device_ids,
                slot=slot,
                runtime_target_id=runtime_target_id,
                model_id=model_id,
                batch_size=batch_size,
                require_runtime_validation=require_runtime_validation,
                require_approval=require_approval,
            )
            return existing
        if package_id not in data["packages"]:
            raise ValueError(f"Unknown package: {package_id}")
        if not device_ids:
            raise ValueError("At least one device_id is required")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if runtime_target_id and runtime_target_id not in _runtime_targets_with_defaults(data):
            raise ValueError(f"Unknown runtime target: {runtime_target_id}")

        package = data["packages"][package_id]
        if model_id:
            _validate_package_model(package, package_id=package_id, model_id=model_id)
        promotion = _normalize_package_promotion(
            current=package,
            incoming=package.get("promotion"),
            package_id=package_id,
            actor=None,
            updated_at=_now(),
        )
        now = _now()
        plan_id = plan_id or f"plan-{uuid.uuid4().hex[:12]}"
        targets = []
        for device_id in _dedupe_ids(device_ids):
            if device_id not in data["devices"]:
                raise ValueError(f"Unknown device: {device_id}")
            blockers: list[str] = []
            preview = _rollout_compatibility_preview(
                data,
                device_id=device_id,
                package_id=package_id,
                runtime_target_id=runtime_target_id,
                model_id=model_id,
            )
            blockers.extend(preview.get("failures") or [])
            validation = None
            if require_runtime_validation:
                if not runtime_target_id:
                    blockers.append("runtime validation gate requires a runtime target")
                else:
                    validation = _latest_passing_runtime_validation(
                        data,
                        package_id=package_id,
                        runtime_target_id=runtime_target_id,
                        package=package,
                    )
                    if validation is None:
                        blockers.append(
                            f"no passing runtime validation for package {package_id} "
                            f"on runtime target {runtime_target_id}"
                        )
            if promotion.get("state") != "released":
                blockers.append(
                    f"package promotion state is {promotion.get('state')}, not released"
                )
            targets.append(
                {
                    "device_id": device_id,
                    "state": "blocked" if blockers else "pending",
                    "rollout_id": None,
                    "assigned_at": None,
                    "blockers": blockers,
                    "compatible": not preview.get("failures"),
                    "runtime_validation_ready": validation is not None,
                    "runtime_validation": _rollout_runtime_validation_summary(validation),
                }
            )

        pending_targets = [target for target in targets if target.get("state") == "pending"]
        plan_state = "ready" if pending_targets else "blocked"
        plan = {
            "schema_version": "temms-rollout-plan/v1",
            "plan_id": plan_id,
            "package_id": package_id,
            "model_id": model_id,
            "package_promotion": _package_promotion_summary(package),
            "slot": slot,
            "runtime_target_id": runtime_target_id,
            "runtime_target": _rollout_runtime_target_summary(
                _runtime_targets_with_defaults(data).get(runtime_target_id)
                if runtime_target_id
                else None
            ),
            "batch_size": batch_size,
            "require_runtime_validation": require_runtime_validation,
            "require_approval": require_approval,
            "state": plan_state,
            "current_batch": 0,
            "targets": targets,
            "counts": _rollout_plan_counts(targets),
            "created_at": now,
            "updated_at": now,
            "actor": actor,
            "reason": reason,
            "history": [
                {
                    "state": "created",
                    "updated_at": now,
                    "detail": reason or f"created rollout plan with {len(targets)} targets",
                    "actor": actor,
                    "counts": _rollout_plan_counts(targets),
                }
            ],
        }
        data.setdefault("rollout_plans", {})[plan_id] = plan
        self._write(data)
        return plan

    def advance_rollout_plan(
        self,
        plan_id: str,
        *,
        limit: int | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Assign the next batch for a rollout plan."""
        data = self._read()
        plan = data.setdefault("rollout_plans", {}).get(plan_id)
        if plan is None:
            raise ValueError(f"Unknown rollout plan: {plan_id}")
        if plan.get("state") == "paused":
            raise ValueError(f"Rollout plan {plan_id} is paused")
        if plan.get("state") == "blocked":
            raise ValueError(f"Rollout plan {plan_id} has no assignable targets")
        if plan.get("state") == "completed":
            return plan

        batch_limit = limit if limit is not None else int(plan.get("batch_size") or 1)
        if batch_limit < 1:
            raise ValueError("limit must be at least 1")
        pending = [target for target in plan.get("targets", []) if target.get("state") == "pending"]
        if not pending:
            return self._complete_rollout_plan(plan_id, actor=actor, detail="no pending targets")

        batch_number = int(plan.get("current_batch") or 0) + 1
        assigned_rollout_ids: list[str] = []
        for index, target in enumerate(pending[:batch_limit], start=1):
            device_id = str(target.get("device_id") or "")
            rollout_id = f"{plan_id}-b{batch_number}-{index}"
            rollout = self.assign_rollout(
                device_id=device_id,
                package_id=str(plan.get("package_id") or ""),
                slot=plan.get("slot"),
                rollout_id=rollout_id,
                runtime_target_id=plan.get("runtime_target_id"),
                model_id=plan.get("model_id"),
                require_runtime_validation=bool(plan.get("require_runtime_validation")),
                require_approval=bool(plan.get("require_approval")),
                actor=actor,
                reason=f"assigned by rollout plan {plan_id} batch {batch_number}",
                rollout_plan_id=plan_id,
                rollout_plan_batch=batch_number,
            )
            target["state"] = "assigned"
            target["rollout_id"] = rollout["rollout_id"]
            target["assigned_at"] = rollout.get("created_at")
            assigned_rollout_ids.append(rollout["rollout_id"])

        data = self._read()
        plan = data.setdefault("rollout_plans", {}).get(plan_id)
        if plan is None:
            raise ValueError(f"Unknown rollout plan: {plan_id}")
        assigned_by_device = {target["device_id"]: target for target in pending[:batch_limit]}
        for target in plan.get("targets", []):
            assigned = assigned_by_device.get(target.get("device_id"))
            if assigned is not None:
                target.update(
                    {
                        "state": "assigned",
                        "rollout_id": assigned.get("rollout_id"),
                        "assigned_at": assigned.get("assigned_at"),
                    }
                )

        now = _now()
        remaining = [
            target for target in plan.get("targets", []) if target.get("state") == "pending"
        ]
        plan["current_batch"] = batch_number
        plan["state"] = "ready" if remaining else _rollout_plan_state(plan.get("targets", []))
        plan["updated_at"] = now
        plan["actor"] = actor or plan.get("actor")
        plan["counts"] = _rollout_plan_counts(plan.get("targets", []))
        plan.setdefault("history", []).append(
            {
                "state": "advanced",
                "updated_at": now,
                "detail": f"assigned batch {batch_number}",
                "actor": actor,
                "batch": batch_number,
                "rollout_ids": assigned_rollout_ids,
                "counts": plan["counts"],
            }
        )
        if plan["state"] == "completed":
            plan["history"].append(
                {
                    "state": "completed",
                    "updated_at": now,
                    "detail": "all assignable targets assigned",
                    "actor": actor,
                    "counts": plan["counts"],
                }
            )
        self._write(data)
        return plan

    def pause_rollout_plan(
        self,
        plan_id: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Pause a rollout plan before assigning more batches."""
        return self._set_rollout_plan_state(
            plan_id,
            "paused",
            actor=actor,
            reason=reason or "rollout plan paused",
        )

    def resume_rollout_plan(
        self,
        plan_id: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Resume a paused rollout plan."""
        return self._set_rollout_plan_state(
            plan_id,
            "ready",
            actor=actor,
            reason=reason or "rollout plan resumed",
        )

    def list_rollout_plans(self) -> list[dict[str, Any]]:
        """Return coordinated rollout plans."""
        plans = list(self._read().setdefault("rollout_plans", {}).values())
        plans.sort(key=lambda plan: plan.get("updated_at", ""), reverse=True)
        return plans

    def get_rollout_plan(self, plan_id: str) -> dict[str, Any] | None:
        """Return one coordinated rollout plan."""
        return self._read().setdefault("rollout_plans", {}).get(plan_id)

    def _complete_rollout_plan(
        self,
        plan_id: str,
        *,
        actor: str | None = None,
        detail: str = "completed",
    ) -> dict[str, Any]:
        return self._set_rollout_plan_state(plan_id, "completed", actor=actor, reason=detail)

    def _set_rollout_plan_state(
        self,
        plan_id: str,
        state: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if state not in ROLLOUT_PLAN_STATES:
            raise ValueError(f"Invalid rollout plan state: {state}")
        data = self._read()
        plan = data.setdefault("rollout_plans", {}).get(plan_id)
        if plan is None:
            raise ValueError(f"Unknown rollout plan: {plan_id}")
        if state == "ready":
            state = _rollout_plan_state(plan.get("targets", []))
        now = _now()
        plan["state"] = state
        plan["updated_at"] = now
        plan["actor"] = actor or plan.get("actor")
        plan["counts"] = _rollout_plan_counts(plan.get("targets", []))
        plan.setdefault("history", []).append(
            {
                "state": state,
                "updated_at": now,
                "detail": reason,
                "actor": actor,
                "counts": plan["counts"],
            }
        )
        self._write(data)
        return plan

    def approve_rollout(
        self,
        rollout_id: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Approve a rollout that requires policy/operator approval before apply."""
        data = self._read()
        rollout = data["rollouts"].get(rollout_id)
        if rollout is None:
            raise ValueError(f"Unknown rollout: {rollout_id}")

        now = _now()
        rollout["approval_required"] = True
        rollout["approval"] = _rollout_approval(
            required=True,
            actor=actor,
            reason=reason,
            updated_at=now,
        )
        rollout["updated_at"] = now
        if actor:
            rollout["actor"] = actor
        rollout.setdefault("history", []).append(
            {
                "state": "approved",
                "updated_at": now,
                "detail": reason or "rollout approved",
                "actor": actor,
            }
        )
        self._write(data)
        return rollout

    def update_rollout_status(
        self,
        rollout_id: str,
        state: str,
        detail: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Update rollout lifecycle state."""
        if state not in ROLLOUT_STATES:
            raise ValueError(f"Invalid rollout state: {state}")
        data = self._read()
        rollout = data["rollouts"].get(rollout_id)
        if rollout is None:
            raise ValueError(f"Unknown rollout: {rollout_id}")

        now = _now()
        rollout["state"] = state
        rollout["updated_at"] = now
        if actor:
            rollout["actor"] = actor
        rollout.setdefault("history", []).append(
            {"state": state, "updated_at": now, "detail": detail, "actor": actor}
        )
        _reconcile_rollout_plan_target(data, rollout, state=state, actor=actor, updated_at=now)
        self._write(data)
        return rollout

    def list_rollouts(self) -> list[dict[str, Any]]:
        """Return rollout assignments."""
        return list(self._read()["rollouts"].values())

    def get_rollout(self, rollout_id: str) -> dict[str, Any] | None:
        """Return one rollout assignment."""
        return self._read()["rollouts"].get(rollout_id)

    def deployment_status(self) -> dict[str, Any]:
        """Return deployment status by device and rollout."""
        data = self._read()
        return {
            "devices": data["devices"],
            "deployment_status": data["deployment_status"],
            "rollouts": data["rollouts"],
            "rollout_plans": data.setdefault("rollout_plans", {}),
            "telemetry_events": data.get("telemetry_events", {}),
            "telemetry_replays": data.get("telemetry_replays", {}),
            "evidence_bundles": data.get("evidence_bundles", {}),
            "evidence_ingests": data.get("evidence_ingests", {}),
            "runtime_validations": data.get("runtime_validations", {}),
            "benchmarks": data.get("benchmarks", {}),
        }

    def replay_telemetry_bundle(
        self,
        bundle: dict[str, Any],
        device_id: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Ingest an exported edge telemetry bundle into Hub Lite."""
        if bundle.get("schema_version") != "temms-telemetry-bundle/v1":
            raise ValueError("Unsupported telemetry bundle schema")
        events = bundle.get("events", [])
        if not isinstance(events, list):
            raise ValueError("Telemetry bundle events must be a list")

        data = self._read()
        telemetry_events = data.setdefault("telemetry_events", {})
        telemetry_replays = data.setdefault("telemetry_replays", {})
        replay_id = f"telemetry-replay-{uuid.uuid4().hex[:12]}"
        replayed_at = _now()
        ingested = 0
        duplicates = 0

        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or uuid.uuid4())
            if event_id in telemetry_events:
                duplicates += 1
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            telemetry_events[event_id] = {
                **event,
                "event_id": event_id,
                "device_id": device_id or event.get("device_id") or payload.get("device_id"),
                "replay_id": replay_id,
                "replayed_at": replayed_at,
            }
            ingested += 1

        telemetry_replays[replay_id] = {
            "replay_id": replay_id,
            "device_id": device_id,
            "actor": actor,
            "bundle_exported_at": bundle.get("exported_at"),
            "replayed_at": replayed_at,
            "ingested": ingested,
            "duplicates": duplicates,
            "source_event_count": len(events),
        }
        if device_id:
            self.heartbeat(
                device_id,
                status="replayed",
                deployment_status={
                    "state": "telemetry_replayed",
                    "replay_id": replay_id,
                    "ingested": ingested,
                    "duplicates": duplicates,
                },
            )
            data = self._read()
            data.setdefault("telemetry_events", {}).update(telemetry_events)
            data.setdefault("telemetry_replays", {}).update(telemetry_replays)

        self._write(data)
        return telemetry_replays[replay_id]

    def telemetry_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return centrally replayed telemetry events."""
        events = list(self._read().get("telemetry_events", {}).values())
        events.sort(key=lambda event: event.get("timestamp", ""))
        if limit is not None:
            return events[-limit:]
        return events

    def ingest_evidence_bundle(
        self,
        bundle: dict[str, Any],
        device_id: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Ingest a full edge evidence bundle for central Hub aggregation."""
        if bundle.get("schema_version") != "temms-evidence-bundle/v1":
            raise ValueError("Unsupported evidence bundle schema")

        from temms.evidence import summarize_evidence_bundle

        data = self._read()
        evidence_bundles = data.setdefault("evidence_bundles", {})
        evidence_ingests = data.setdefault("evidence_ingests", {})
        evidence_id = _evidence_bundle_id(bundle)
        ingest_id = f"evidence-ingest-{uuid.uuid4().hex[:12]}"
        ingested_at = _now()
        inferred_device_id = device_id or _infer_evidence_device_id(bundle)
        duplicate = evidence_id in evidence_bundles
        try:
            summary = summarize_evidence_bundle(bundle, limit=20)
        except Exception as exc:
            summary = {
                "schema_version": "temms-evidence-summary/v1",
                "headline": f"summary unavailable: {exc}",
                "counts": {},
            }

        record = {
            "schema_version": "temms-ingested-evidence/v1",
            "evidence_id": evidence_id,
            "device_id": inferred_device_id,
            "actor": actor,
            "ingested_at": ingested_at,
            "exported_at": bundle.get("exported_at"),
            "source_schema_version": bundle.get("schema_version"),
            "integrity": bundle.get("integrity", {}),
            "headline": summary.get("headline"),
            "counts": summary.get("counts", {}),
            "summary": summary,
            "bundle": bundle,
        }
        if duplicate:
            existing = evidence_bundles[evidence_id]
            existing["last_ingested_at"] = ingested_at
            existing["last_actor"] = actor
            existing["duplicate_ingests"] = int(existing.get("duplicate_ingests") or 0) + 1
            record = existing
        else:
            evidence_bundles[evidence_id] = record

        evidence_ingests[ingest_id] = {
            "schema_version": "temms-evidence-ingest/v1",
            "ingest_id": ingest_id,
            "evidence_id": evidence_id,
            "device_id": inferred_device_id,
            "actor": actor,
            "ingested_at": ingested_at,
            "exported_at": bundle.get("exported_at"),
            "duplicate": duplicate,
        }
        if inferred_device_id:
            self.heartbeat(
                inferred_device_id,
                status="evidence_ingested",
                deployment_status={
                    "state": "evidence_ingested",
                    "evidence_id": evidence_id,
                    "ingest_id": ingest_id,
                },
            )
            data = self._read()
            data.setdefault("evidence_bundles", {}).update(evidence_bundles)
            data.setdefault("evidence_ingests", {}).update(evidence_ingests)

        self._write(data)
        return {**record, "ingest_id": ingest_id, "duplicate": duplicate}

    def list_evidence_bundles(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return centrally ingested edge evidence bundles."""
        records = list(self._read().get("evidence_bundles", {}).values())
        records.sort(key=lambda record: record.get("ingested_at", ""), reverse=True)
        if limit is not None:
            return records[:limit]
        return records

    def export_bundle(self, include_packages: bool = False) -> dict[str, Any]:
        """Return a portable air-gap bundle."""
        data = self._read()
        data["runtime_targets"] = _runtime_targets_with_defaults(data)
        bundle = {
            "schema_version": "temms-hub-lite-bundle/v1",
            "exported_at": _now(),
            "hub_lite": data,
        }
        if include_packages:
            bundle["package_artifacts"] = self._export_package_artifacts(data)
        return bundle

    def import_bundle(
        self, bundle: dict[str, Any], package_dir: Path | None = None
    ) -> dict[str, int]:
        """Merge an air-gap bundle into local state."""
        if bundle.get("schema_version") != "temms-hub-lite-bundle/v1":
            raise ValueError("Unsupported air-gap bundle schema")
        incoming = bundle.get("hub_lite", {})
        data = self._read()
        counts = {}
        accepted_records: dict[str, set[str]] = {}
        for key in (
            "devices",
            "packages",
            "rollouts",
            "deployment_status",
            "telemetry_events",
            "telemetry_replays",
            "runtime_targets",
            "runtime_validations",
            "benchmarks",
            "deployment_drafts",
            "rollout_plans",
            "evidence_bundles",
            "evidence_ingests",
        ):
            records = incoming.get(key, {})
            collection = data.setdefault(key, {})
            accepted_records[key] = set()
            for record_id, incoming_record in records.items():
                current_record = collection.get(record_id)
                if _accept_incoming_record(current_record, incoming_record):
                    accepted_records[key].add(record_id)
                collection[record_id] = _merge_record(
                    current_record,
                    incoming_record,
                    merge_history=key in {"rollouts", "rollout_plans"},
                )
            counts[key] = len(records)

        artifacts = bundle.get("package_artifacts", {})
        if artifacts:
            package_dir = package_dir or self.path.parent / "packages"
            package_dir.mkdir(parents=True, exist_ok=True)
            imported_artifacts = 0
            skipped_artifacts = 0
            for package_id, artifact in artifacts.items():
                if package_id not in accepted_records.get("packages", set()):
                    skipped_artifacts += 1
                    continue
                filename = Path(artifact.get("filename") or f"{package_id}.temms.tar.zst").name
                content = artifact.get("content_base64")
                expected_sha = artifact.get("sha256")
                if not content:
                    raise ValueError(f"Package artifact {package_id} is missing content")
                artifact_bytes = base64.b64decode(content)
                actual_sha = _sha256_bytes(artifact_bytes)
                if expected_sha and actual_sha != expected_sha:
                    raise ValueError(f"Package artifact hash mismatch: {package_id}")
                destination = package_dir / filename
                tmp_destination = destination.with_suffix(destination.suffix + ".tmp")
                tmp_destination.write_bytes(artifact_bytes)
                tmp_destination.replace(destination)
                package = data["packages"].get(package_id)
                if package is not None:
                    package["path"] = str(destination)
                    if artifact.get("source_sha256"):
                        package["source_sha256"] = artifact["source_sha256"]
                    package["sha256"] = actual_sha
                    package.setdefault("metadata", {})["airgap_artifact"] = {
                        "filename": filename,
                        "sha256": actual_sha,
                        "source_sha256": artifact.get("source_sha256"),
                        "imported_at": _now(),
                    }
                    imported_artifacts += 1
            counts["package_artifacts"] = imported_artifacts
            counts["package_artifacts_skipped"] = skipped_artifacts

        self._write(data)
        return counts

    def _export_package_artifacts(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return package archive payloads for air-gap transfer."""
        artifacts: dict[str, Any] = {}
        for package_id, package in data.get("packages", {}).items():
            try:
                package_path = _verified_package_path(package_id, package)
                filename, artifact_bytes = _package_artifact_payload(Path(package_path))
            except PackageArtifactNotFound:
                continue
            artifacts[package_id] = {
                "filename": filename,
                "sha256": _sha256_bytes(artifact_bytes),
                "source_sha256": package.get("source_sha256") or package.get("sha256"),
                "content_base64": base64.b64encode(artifact_bytes).decode("ascii"),
            }
        return artifacts

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self._empty()

    def _write(self, data: dict[str, Any]) -> None:
        write_json_atomic(self.path, data, indent=2, sort_keys=True)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "devices": {},
            "packages": {},
            "rollouts": {},
            "deployment_status": {},
            "telemetry_events": {},
            "telemetry_replays": {},
            "runtime_targets": default_runtime_targets(),
            "runtime_validations": {},
            "benchmarks": {},
            "deployment_drafts": {},
            "rollout_plans": {},
            "evidence_bundles": {},
            "evidence_ingests": {},
        }


def _dedupe_ids(record_ids: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for record_id in record_ids:
        clean_id = str(record_id or "").strip()
        if not clean_id or clean_id in seen:
            continue
        seen.add(clean_id)
        deduped.append(clean_id)
    return deduped


def _ensure_rollout_request_matches(
    rollout: dict[str, Any],
    *,
    device_id: str,
    package_id: str,
    slot: str | None,
    runtime_target_id: str | None,
    model_id: str | None,
    require_runtime_validation: bool,
    require_approval: bool,
    rollout_plan_id: str | None,
    rollout_plan_batch: int | None,
) -> None:
    expected = {
        "device_id": device_id,
        "package_id": package_id,
        "slot": slot,
        "runtime_target_id": runtime_target_id,
        "model_id": model_id,
        "runtime_validation_required": require_runtime_validation,
        "approval_required": require_approval,
        "rollout_plan_id": rollout_plan_id,
        "rollout_plan_batch": rollout_plan_batch,
    }
    mismatches = [
        key
        for key, value in expected.items()
        if rollout.get(key) != value
    ]
    if mismatches:
        rollout_id = rollout.get("rollout_id", "existing rollout")
        raise ValueError(
            f"Rollout {rollout_id} already exists with different "
            + ", ".join(mismatches)
        )


def _ensure_rollout_plan_request_matches(
    plan: dict[str, Any],
    *,
    package_id: str,
    device_ids: list[str],
    slot: str | None,
    runtime_target_id: str | None,
    model_id: str | None,
    batch_size: int,
    require_runtime_validation: bool,
    require_approval: bool,
) -> None:
    expected = {
        "package_id": package_id,
        "model_id": model_id,
        "slot": slot,
        "runtime_target_id": runtime_target_id,
        "batch_size": batch_size,
        "require_runtime_validation": require_runtime_validation,
        "require_approval": require_approval,
    }
    mismatches = [
        key
        for key, value in expected.items()
        if plan.get(key) != value
    ]
    existing_devices = [
        str(target.get("device_id") or "")
        for target in plan.get("targets", [])
        if target.get("device_id")
    ]
    if existing_devices != _dedupe_ids(device_ids):
        mismatches.append("device_ids")
    if mismatches:
        plan_id = plan.get("plan_id", "existing rollout plan")
        raise ValueError(
            f"Rollout plan {plan_id} already exists with different "
            + ", ".join(mismatches)
        )


def _reconcile_rollout_plan_target(
    data: dict[str, Any],
    rollout: dict[str, Any],
    *,
    state: str,
    actor: str | None,
    updated_at: str,
) -> None:
    """Mirror a rollout lifecycle transition onto its owning rollout plan target."""
    plan_id = rollout.get("rollout_plan_id")
    rollout_id = rollout.get("rollout_id")
    if not plan_id or not rollout_id:
        return
    plan = data.setdefault("rollout_plans", {}).get(plan_id)
    if not isinstance(plan, dict):
        return

    changed = False
    for target in plan.get("targets", []):
        if target.get("rollout_id") != rollout_id:
            continue
        target["state"] = state
        target["updated_at"] = updated_at
        target["last_actor"] = actor
        changed = True
        break

    if not changed:
        return

    plan["counts"] = _rollout_plan_counts(plan.get("targets", []))
    plan["state"] = _rollout_plan_state(plan.get("targets", []))
    plan["updated_at"] = updated_at
    plan["actor"] = actor or plan.get("actor")
    plan.setdefault("history", []).append(
        {
            "state": "reconciled",
            "updated_at": updated_at,
            "detail": f"{rollout_id} moved to {state}",
            "actor": actor,
            "rollout_ids": [rollout_id],
            "counts": plan["counts"],
        }
    )


def _rollout_plan_state(targets: list[dict[str, Any]]) -> str:
    """Return the operator-facing state for a coordinated rollout plan."""
    states = {str(target.get("state") or "") for target in targets}
    if not targets:
        return "blocked"
    if states <= {"blocked"}:
        return "blocked"
    if "failed" in states:
        return "failed"
    if "pending" in states:
        return "ready"
    if states <= {"activated", "rolled_back"}:
        return "completed"
    if states & {"assigned", "downloading", "imported"}:
        return "advancing"
    return "ready"


def _rollout_plan_counts(targets: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "targets": len(targets),
        "pending": 0,
        "assigned": 0,
        "blocked": 0,
        "downloading": 0,
        "imported": 0,
        "activated": 0,
        "rolled_back": 0,
        "failed": 0,
    }
    for target in targets:
        state = target.get("state")
        if state in counts:
            counts[state] += 1
    return counts


def _normalize_package_promotion(
    *,
    current: dict[str, Any],
    incoming: Any,
    package_id: str,
    actor: str | None,
    updated_at: str,
) -> dict[str, Any]:
    """Return a valid package promotion record, preserving existing history."""
    current_promotion = (
        current.get("promotion") if isinstance(current.get("promotion"), dict) else {}
    )
    incoming_promotion = incoming if isinstance(incoming, dict) else {}
    promotion = dict(current_promotion)
    promotion.update(incoming_promotion)
    state = str(promotion.get("state") or "candidate").lower().strip()
    if state not in PACKAGE_PROMOTION_STATES:
        state = "candidate"

    history = [
        event
        for event in promotion.get("history", [])
        if isinstance(event, dict) and event.get("state")
    ]
    if not history:
        history = [
            {
                "state": state,
                "from_state": None,
                "updated_at": updated_at,
                "actor": actor,
                "reason": "package cataloged",
                "evidence": {},
            }
        ]
    return {
        "schema_version": "temms-package-promotion/v1",
        "package_id": package_id,
        "state": state,
        "updated_at": promotion.get("updated_at") or updated_at,
        "actor": promotion.get("actor") or actor,
        "reason": promotion.get("reason") or history[-1].get("reason"),
        "evidence": (
            promotion.get("evidence") if isinstance(promotion.get("evidence"), dict) else {}
        ),
        "history": history,
    }


def _package_promotion_summary(package: dict[str, Any]) -> dict[str, Any]:
    promotion = package.get("promotion") if isinstance(package.get("promotion"), dict) else {}
    return {
        "schema_version": "temms-package-promotion-summary/v1",
        "package_id": package.get("package_id"),
        "state": promotion.get("state") or "candidate",
        "updated_at": promotion.get("updated_at"),
        "actor": promotion.get("actor"),
        "reason": promotion.get("reason"),
    }


def _accept_incoming_record(current: Any, incoming: Any) -> bool:
    """Return whether an incoming air-gap record should replace local state."""
    if current is None:
        return True
    if not isinstance(current, dict) or not isinstance(incoming, dict):
        return False
    return _record_timestamp(incoming) > _record_timestamp(current)


def _merge_record(
    current: Any,
    incoming: Any,
    *,
    merge_history: bool = False,
) -> Any:
    """Merge air-gap records without replacing newer local state."""
    if current is None:
        return incoming
    if not isinstance(current, dict) or not isinstance(incoming, dict):
        return current

    if _record_timestamp(incoming) > _record_timestamp(current):
        merged = dict(incoming)
    else:
        merged = dict(current)

    if merge_history:
        merged["history"] = _merge_history(
            current.get("history", []),
            incoming.get("history", []),
        )
    return merged


def _record_timestamp(record: dict[str, Any]) -> str:
    for key in (
        "updated_at",
        "last_seen_at",
        "replayed_at",
        "imported_at",
        "exported_at",
        "timestamp",
        "created_at",
        "enrolled_at",
    ):
        value = record.get(key)
        if value is not None:
            return str(value)
    return ""


def _merge_history(
    current_history: Any,
    incoming_history: Any,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in list(current_history or []) + list(incoming_history or []):
        if not isinstance(event, dict):
            continue
        key = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        if key in seen:
            continue
        seen.add(key)
        merged.append(event)
    merged.sort(key=lambda event: str(event.get("updated_at", "")))
    return merged


def _runtime_targets_with_defaults(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return runtime targets with built-in defaults merged into stored state."""
    runtime_targets = default_runtime_targets()
    stored = data.setdefault("runtime_targets", {})
    for target_id, target in stored.items():
        if isinstance(target, dict):
            runtime_targets[target_id] = target
    return runtime_targets


def _rollout_compatibility_preview(
    data: dict[str, Any],
    *,
    device_id: str,
    package_id: str,
    runtime_target_id: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Return package/device/runtime compatibility without mutating rollout state."""
    devices = data.get("devices", {})
    packages = data.get("packages", {})
    if device_id not in devices:
        raise ValueError(f"Unknown device: {device_id}")
    if package_id not in packages:
        raise ValueError(f"Unknown package: {package_id}")

    device = devices[device_id]
    package = packages[package_id]
    if model_id:
        _validate_package_model(package, package_id=package_id, model_id=model_id)
    runtime_target = None
    failures: list[str] = []

    package_profiles = [
        normalized
        for normalized in (
            normalize_device_profile(profile) for profile in package.get("device_profiles", [])
        )
        if normalized
    ]
    device_profile = normalize_device_profile(device.get("profile"))
    if package_profiles and device_profile not in package_profiles:
        failures.append(
            f"package profile mismatch: device profile {device_profile} is not in "
            f"{package_profiles}"
        )

    if runtime_target_id:
        runtime_target = _runtime_targets_with_defaults(data).get(runtime_target_id)
        if runtime_target is None:
            raise ValueError(f"Unknown runtime target: {runtime_target_id}")
        failures.extend(_runtime_target_failures(runtime_target, package, device, model_id=model_id))
    else:
        failures.extend(_runtime_constraint_failures(package, device, model_id=model_id))

    return {
        "schema_version": "temms-rollout-compatibility/v1",
        "compatible": not failures,
        "failures": failures,
        "model_id": model_id,
        "device": _device_compatibility_summary(device),
        "package": _package_compatibility_summary(package),
        "runtime_target_id": runtime_target_id,
        "runtime_target": _rollout_runtime_target_summary(runtime_target),
        "checked_at": _now(),
    }


def _compatibility_matrix(
    data: dict[str, Any],
    *,
    package_ids: list[str] | None = None,
    model_ids: list[str] | None = None,
    device_ids: list[str] | None = None,
    runtime_target_ids: list[str] | None = None,
    include_device_inventory: bool = False,
) -> dict[str, Any]:
    packages = _select_hub_records(
        data.get("packages", {}),
        package_ids,
        id_label="package",
    )
    devices = _select_hub_records(
        data.get("devices", {}),
        device_ids,
        id_label="device",
    )
    runtime_targets = _select_hub_records(
        _runtime_targets_with_defaults(data),
        runtime_target_ids,
        id_label="runtime target",
    )
    runtime_options: list[tuple[str | None, dict[str, Any] | None]] = [
        (target.get("runtime_target_id"), target) for target in runtime_targets
    ]
    if include_device_inventory:
        runtime_options.insert(0, (None, None))

    cells: list[dict[str, Any]] = []
    for package in packages:
        package_id = str(package.get("package_id") or "")
        selected_model_ids = _matrix_model_ids(package, model_ids)
        promotion = _package_promotion_summary(package)
        package_released = promotion.get("state") == "released"
        for selected_model_id in selected_model_ids:
            for device in devices:
                device_id = str(device.get("device_id") or "")
                for runtime_target_id, _runtime_target in runtime_options:
                    preview = _rollout_compatibility_preview(
                        data,
                        device_id=device_id,
                        package_id=package_id,
                        runtime_target_id=runtime_target_id,
                        model_id=selected_model_id,
                    )
                    validation = (
                        _latest_passing_runtime_validation(
                            data,
                            package_id=package_id,
                            runtime_target_id=runtime_target_id,
                            package=package,
                        )
                        if runtime_target_id
                        else None
                    )
                    performance = (
                        _performance_fit_summary(
                            data,
                            package=package,
                            device=device,
                            runtime_target=_runtime_target,
                            model_id=selected_model_id,
                        )
                        if selected_model_id
                        else {
                            "status": "go",
                            "state": "not scoped",
                            "detail": "No declared model selected for performance evaluation",
                            "slo": {},
                            "benchmark": None,
                            "failures": [],
                        }
                    )
                    resource_envelope = (
                        _resource_envelope_summary(
                            package=package,
                            device=device,
                            model_id=selected_model_id,
                        )
                        if selected_model_id
                        else {
                            "status": "go",
                            "state": "not scoped",
                            "detail": "No declared model selected for resource evaluation",
                            "requirements": {},
                            "observed": {},
                            "failures": [],
                            "missing": [],
                        }
                    )
                    telemetry_freshness = _device_telemetry_freshness(device)
                    runtime_fit = (
                        _runtime_target_fit_summary(
                            data,
                            package=package,
                            device=device,
                            runtime_target=_runtime_target,
                            model_id=selected_model_id,
                            preview=preview,
                            validation=validation,
                            performance=performance,
                            resource_envelope=resource_envelope,
                            telemetry_freshness=telemetry_freshness,
                        )
                        if selected_model_id
                        else {
                            "schema_version": "temms-runtime-fit/v1",
                            "score": 0,
                            "tier": "not_scoped",
                            "detail": "Select a model to score runtime fit",
                        }
                    )
                    runtime_lane = (
                        runtime_fit.get("runtime_lane")
                        if isinstance(runtime_fit.get("runtime_lane"), dict)
                        else runtime_lane_summary(_runtime_target)
                    )
                    artifact_lane = (
                        runtime_fit.get("artifact_lane")
                        if isinstance(runtime_fit.get("artifact_lane"), dict)
                        else {}
                    )
                    assignment_blockers = list(preview.get("failures") or [])
                    if resource_envelope.get("status") == "blocked":
                        assignment_blockers.extend(
                            str(failure)
                            for failure in resource_envelope.get("failures") or []
                        )
                    if not package_released:
                        assignment_blockers.append(
                            "package promotion state is "
                            f"{promotion.get('state') or 'candidate'}, not released"
                        )
                    resource_blocked = resource_envelope.get("status") == "blocked"
                    cells.append(
                        {
                            "package_id": package_id,
                            "model_id": selected_model_id,
                            "device_id": device_id,
                            "runtime_target_id": runtime_target_id,
                            "runtime_mode": (
                                "runtime_target" if runtime_target_id else "device_inventory"
                            ),
                            "compatible": bool(preview.get("compatible")),
                            "package_released": package_released,
                            "assignment_ready": (
                                bool(preview.get("compatible"))
                                and package_released
                                and not resource_blocked
                            ),
                            "runtime_validation_ready": validation is not None,
                            "runtime_validation": _rollout_runtime_validation_summary(validation),
                            "performance_ready": performance.get("status") == "go",
                            "performance": performance,
                            "resource_ready": resource_envelope.get("status") == "go",
                            "resource_blocked": resource_blocked,
                            "resource_envelope": resource_envelope,
                            "telemetry_freshness": telemetry_freshness,
                            "runtime_lane": runtime_lane,
                            "artifact_lane": artifact_lane,
                            "runtime_fit": runtime_fit,
                            "package_promotion": promotion,
                            "failures": list(preview.get("failures") or []),
                            "assignment_blockers": assignment_blockers,
                            "device": preview.get("device"),
                            "package": preview.get("package"),
                            "runtime_target": preview.get("runtime_target"),
                            "checked_at": preview.get("checked_at"),
                        }
                    )

    counts = {
        "cells": len(cells),
        "compatible": sum(1 for cell in cells if cell["compatible"]),
        "blocked": sum(1 for cell in cells if not cell["compatible"]),
        "assignment_ready": sum(1 for cell in cells if cell["assignment_ready"]),
        "needs_release": sum(
            1 for cell in cells if cell["compatible"] and not cell["package_released"]
        ),
        "runtime_validation_ready": sum(1 for cell in cells if cell["runtime_validation_ready"]),
        "runtime_validation_missing": sum(
            1
            for cell in cells
            if cell["runtime_target_id"] and not cell["runtime_validation_ready"]
        ),
        "performance_ready": sum(1 for cell in cells if cell["performance_ready"]),
        "performance_attention": sum(1 for cell in cells if not cell["performance_ready"]),
        "resource_ready": sum(1 for cell in cells if cell["resource_ready"]),
        "resource_attention": sum(
            1
            for cell in cells
            if not cell["resource_ready"] and not cell["resource_blocked"]
        ),
        "resource_blocked": sum(1 for cell in cells if cell["resource_blocked"]),
        "runtime_fit_optimal": sum(
            1 for cell in cells if cell.get("runtime_fit", {}).get("tier") == "optimal"
        ),
        "runtime_fit_ready": sum(
            1 for cell in cells if cell.get("runtime_fit", {}).get("tier") == "ready"
        ),
        "runtime_fit_needs_evidence": sum(
            1 for cell in cells if cell.get("runtime_fit", {}).get("tier") == "needs_evidence"
        ),
        "runtime_fit_blocked": sum(
            1 for cell in cells if cell.get("runtime_fit", {}).get("tier") == "blocked"
        ),
    }
    recommendations = _compatibility_recommendations(cells)
    return {
        "schema_version": "temms-compatibility-matrix/v1",
        "generated_at": _now(),
        "filters": {
            "package_ids": package_ids,
            "model_ids": model_ids,
            "device_ids": device_ids,
            "runtime_target_ids": runtime_target_ids,
            "include_device_inventory": include_device_inventory,
        },
        "dimensions": {
            "packages": len(packages),
            "models": sum(len(_matrix_model_ids(package, model_ids)) for package in packages),
            "devices": len(devices),
            "runtime_targets": len(runtime_targets),
            "device_inventory": include_device_inventory,
            "cells": len(cells),
        },
        "counts": counts,
        "packages": [_matrix_package_summary(package) for package in packages],
        "devices": [_device_compatibility_summary(device) for device in devices],
        "runtime_targets": [
            _rollout_runtime_target_summary(runtime_target) for runtime_target in runtime_targets
        ],
        "recommendations": recommendations,
        "cells": cells,
    }


def _compatibility_recommendations(
    cells: list[dict[str, Any]],
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return ranked, operator-facing edge deployment recommendations."""
    recommendations = [_compatibility_recommendation(cell) for cell in cells]

    def sort_key(recommendation: dict[str, Any]) -> tuple[Any, ...]:
        optimization = (
            recommendation.get("optimization")
            if isinstance(recommendation.get("optimization"), dict)
            else {}
        )
        latency = _float_of(optimization.get("latency_ms_p95"))
        throughput = _float_of(optimization.get("throughput_ips"))
        return (
            -int(recommendation.get("score") or 0),
            _recommendation_decision_rank(str(recommendation.get("decision") or "")),
            latency if latency is not None else float("inf"),
            -(throughput or 0.0),
            str(recommendation.get("package_id") or ""),
            str(recommendation.get("model_id") or ""),
            str(recommendation.get("device_id") or ""),
            str(recommendation.get("runtime_target_id") or ""),
        )

    recommendations.sort(key=sort_key)
    ranked: list[dict[str, Any]] = []
    for rank, recommendation in enumerate(recommendations[:limit], start=1):
        ranked.append({**recommendation, "rank": rank})
    return ranked


def _compatibility_recommendation(cell: dict[str, Any]) -> dict[str, Any]:
    decision = _recommendation_decision(cell)
    score = _recommendation_score(cell)
    required_actions = _recommendation_required_actions(cell, decision)
    warnings = _recommendation_warnings(cell)
    runtime_fit = (
        cell.get("runtime_fit") if isinstance(cell.get("runtime_fit"), dict) else {}
    )
    return {
        "rank": None,
        "score": score,
        "decision": decision,
        "confidence": _recommendation_confidence(cell, decision),
        "package_id": cell.get("package_id"),
        "model_id": cell.get("model_id"),
        "device_id": cell.get("device_id"),
        "runtime_target_id": cell.get("runtime_target_id"),
        "runtime_mode": cell.get("runtime_mode"),
        "runtime_lane": (
            runtime_fit.get("runtime_lane")
            if isinstance(runtime_fit.get("runtime_lane"), dict)
            else cell.get("runtime_lane")
        ),
        "artifact_lane": runtime_fit.get("artifact_lane"),
        "primary_reason": _recommendation_primary_reason(cell, decision),
        "required_actions": required_actions,
        "warnings": warnings,
        "fit": {
            "compatible": bool(cell.get("compatible")),
            "assignment_ready": bool(cell.get("assignment_ready")),
            "runtime_validation_ready": bool(cell.get("runtime_validation_ready")),
            "performance_ready": bool(cell.get("performance_ready")),
            "resource_ready": bool(cell.get("resource_ready")),
            "resource_blocked": bool(cell.get("resource_blocked")),
            "package_released": bool(cell.get("package_released")),
            "runtime_fit_tier": runtime_fit.get("tier"),
            "runtime_fit_score": runtime_fit.get("score"),
        },
        "optimization": _recommendation_optimization(cell),
        "runtime_fit": runtime_fit,
    }


def _recommendation_score(cell: dict[str, Any]) -> int:
    score = 0
    if cell.get("compatible"):
        score += 34
    if cell.get("package_released"):
        score += 14
    if cell.get("assignment_ready"):
        score += 12
    if cell.get("resource_ready"):
        score += 12
    elif cell.get("resource_blocked"):
        score -= 18
    if cell.get("performance_ready"):
        score += 10
    else:
        score -= 6
    if cell.get("runtime_target_id"):
        score += 4
        if cell.get("runtime_validation_ready"):
            score += 8
        else:
            score -= 4
    else:
        score -= 4

    optimization = _recommendation_optimization(cell)
    latency_headroom = _float_of(optimization.get("latency_headroom_pct"))
    throughput_headroom = _float_of(optimization.get("throughput_headroom_pct"))
    memory_headroom = _float_of(optimization.get("memory_headroom_mb"))
    storage_headroom = _float_of(optimization.get("storage_headroom_mb"))
    if latency_headroom is not None:
        score += min(4, max(0, int(latency_headroom // 10)))
    if throughput_headroom is not None:
        score += min(4, max(0, int(throughput_headroom // 25)))
    if memory_headroom is not None and memory_headroom >= 0:
        score += 1
    if storage_headroom is not None and storage_headroom >= 0:
        score += 1
    runtime_fit = (
        cell.get("runtime_fit") if isinstance(cell.get("runtime_fit"), dict) else {}
    )
    runtime_fit_score = _float_of(runtime_fit.get("score"))
    if runtime_fit_score is not None:
        score = int(round((score * 0.4) + (runtime_fit_score * 0.6)))
    return max(0, min(100, score))


def _recommendation_decision(cell: dict[str, Any]) -> str:
    if not cell.get("compatible") or cell.get("resource_blocked"):
        return "blocked"
    if not cell.get("package_released"):
        return "release_required"
    if cell.get("runtime_target_id") and not cell.get("runtime_validation_ready"):
        return "validate_runtime"
    if not cell.get("performance_ready"):
        return "benchmark_or_tune"
    if not cell.get("resource_ready"):
        return "refresh_resource_telemetry"
    if cell.get("assignment_ready"):
        return "deploy"
    return "review"


def _recommendation_decision_rank(decision: str) -> int:
    order = {
        "deploy": 0,
        "validate_runtime": 1,
        "benchmark_or_tune": 2,
        "refresh_resource_telemetry": 3,
        "release_required": 4,
        "review": 5,
        "blocked": 6,
    }
    return order.get(decision, 9)


def _recommendation_confidence(cell: dict[str, Any], decision: str) -> str:
    if decision == "deploy" and cell.get("runtime_validation_ready") and cell.get("performance_ready"):
        return "high"
    if cell.get("assignment_ready") and cell.get("compatible"):
        return "medium"
    return "low"


def _recommendation_primary_reason(cell: dict[str, Any], decision: str) -> str:
    runtime_fit = (
        cell.get("runtime_fit") if isinstance(cell.get("runtime_fit"), dict) else {}
    )
    if decision == "deploy":
        return str(
            runtime_fit.get("detail")
            or "released package, compatible edge inventory, runtime validation, SLO, and resource gates align"
        )
    if decision == "validate_runtime":
        return "compatible target needs runtime validation evidence before confident rollout"
    if decision == "benchmark_or_tune":
        performance = cell.get("performance") if isinstance(cell.get("performance"), dict) else {}
        return str(performance.get("detail") or "performance evidence needs review")
    if decision == "refresh_resource_telemetry":
        resource = cell.get("resource_envelope") if isinstance(cell.get("resource_envelope"), dict) else {}
        return str(resource.get("detail") or "resource envelope needs fresh edge telemetry")
    if decision == "release_required":
        promotion = cell.get("package_promotion") if isinstance(cell.get("package_promotion"), dict) else {}
        return f"package is {promotion.get('state') or 'candidate'}, not released"
    blockers = cell.get("assignment_blockers")
    if isinstance(blockers, list) and blockers:
        return str(blockers[0])
    failures = cell.get("failures")
    if isinstance(failures, list) and failures:
        return str(failures[0])
    return "review this target before rollout"


def _recommendation_required_actions(cell: dict[str, Any], decision: str) -> list[str]:
    actions: list[str] = []
    if decision == "release_required":
        actions.append("promote package to released")
    if decision == "validate_runtime":
        actions.append("run runtime target validation")
    if decision == "benchmark_or_tune":
        actions.append("record fresh on-device benchmark")
    if decision == "refresh_resource_telemetry":
        actions.append("refresh edge resource telemetry")
    if decision == "blocked":
        blockers = cell.get("assignment_blockers")
        if isinstance(blockers, list):
            actions.extend(str(blocker) for blocker in blockers[:3])
        if not actions:
            actions.append("select a compatible model, device, or runtime target")
    return actions


def _recommendation_warnings(cell: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    runtime_fit = (
        cell.get("runtime_fit") if isinstance(cell.get("runtime_fit"), dict) else {}
    )
    if not cell.get("runtime_target_id"):
        warnings.append("no pinned runtime target; using device inventory compatibility")
    if cell.get("runtime_target_id") and not cell.get("runtime_validation_ready"):
        warnings.append("runtime validation evidence missing")
    if not cell.get("performance_ready"):
        performance = cell.get("performance") if isinstance(cell.get("performance"), dict) else {}
        warnings.append(str(performance.get("detail") or "performance evidence missing"))
    if not cell.get("resource_ready") and not cell.get("resource_blocked"):
        resource = cell.get("resource_envelope") if isinstance(cell.get("resource_envelope"), dict) else {}
        warnings.append(str(resource.get("detail") or "resource telemetry incomplete"))
    penalties = runtime_fit.get("penalties")
    if isinstance(penalties, list):
        warnings.extend(str(penalty) for penalty in penalties[:2])
    return warnings


def _recommendation_optimization(cell: dict[str, Any]) -> dict[str, Any]:
    performance = cell.get("performance") if isinstance(cell.get("performance"), dict) else {}
    resource = (
        cell.get("resource_envelope")
        if isinstance(cell.get("resource_envelope"), dict)
        else {}
    )
    benchmark = (
        performance.get("benchmark") if isinstance(performance.get("benchmark"), dict) else {}
    )
    slo = performance.get("slo") if isinstance(performance.get("slo"), dict) else {}
    requirements = (
        resource.get("requirements") if isinstance(resource.get("requirements"), dict) else {}
    )
    observed = resource.get("observed") if isinstance(resource.get("observed"), dict) else {}

    latency = _float_of(benchmark.get("latency_ms_p95"))
    max_latency = _float_of(slo.get("max_latency_ms_p95"))
    throughput = _float_of(benchmark.get("throughput_ips"))
    min_throughput = _float_of(slo.get("min_throughput_ips"))
    memory_available = _float_of(observed.get("memory_available_mb"))
    min_memory = _float_of(requirements.get("min_memory_available_mb"))
    storage_available = _float_of(observed.get("storage_available_mb"))
    min_storage = _float_of(requirements.get("min_storage_available_mb"))

    return _readiness_refs(
        {
            "performance_state": performance.get("state"),
            "runtime_fit_score": (
                cell.get("runtime_fit", {}).get("score")
                if isinstance(cell.get("runtime_fit"), dict)
                else None
            ),
            "runtime_fit_tier": (
                cell.get("runtime_fit", {}).get("tier")
                if isinstance(cell.get("runtime_fit"), dict)
                else None
            ),
            "benchmark_id": benchmark.get("benchmark_id"),
            "latency_ms_p95": latency,
            "throughput_ips": throughput,
            "latency_headroom_pct": _percent_headroom(max_latency, latency, lower_is_better=True),
            "throughput_headroom_pct": _percent_headroom(
                min_throughput,
                throughput,
                lower_is_better=False,
            ),
            "benchmark_age_seconds": (
                performance.get("benchmark_freshness", {})
                if isinstance(performance.get("benchmark_freshness"), dict)
                else {}
            ).get("benchmark_age_seconds"),
            "resource_state": resource.get("state"),
            "memory_available_mb": memory_available,
            "memory_headroom_mb": _numeric_headroom(memory_available, min_memory),
            "storage_available_mb": storage_available,
            "storage_headroom_mb": _numeric_headroom(storage_available, min_storage),
        }
    )


def _percent_headroom(
    target: float | None,
    observed: float | None,
    *,
    lower_is_better: bool,
) -> float | None:
    if target is None or observed is None or target <= 0:
        return None
    if lower_is_better:
        return round(((target - observed) / target) * 100, 2)
    return round(((observed - target) / target) * 100, 2)


def _numeric_headroom(observed: float | None, required: float | None) -> float | None:
    if observed is None or required is None:
        return None
    return round(observed - required, 3)


def _runtime_target_fit_summary(
    data: dict[str, Any],
    *,
    package: dict[str, Any],
    device: dict[str, Any],
    runtime_target: dict[str, Any] | None,
    model_id: str,
    preview: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    performance: dict[str, Any] | None = None,
    resource_envelope: dict[str, Any] | None = None,
    telemetry_freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a ranked runtime/device/model fit summary for operator decisions."""
    package_id = str(package.get("package_id") or "")
    device_id = str(device.get("device_id") or "")
    runtime_target_id = (
        str(runtime_target.get("runtime_target_id") or runtime_target.get("id") or "")
        if runtime_target
        else None
    )
    if preview is None:
        preview = _rollout_compatibility_preview(
            data,
            device_id=device_id,
            package_id=package_id,
            runtime_target_id=runtime_target_id,
            model_id=model_id,
        )
    compatible = preview.get("compatible") is True
    compatibility_failures = [str(failure) for failure in preview.get("failures") or []]
    if validation is None and runtime_target_id:
        validation = _latest_passing_runtime_validation(
            data,
            package_id=package_id,
            runtime_target_id=runtime_target_id,
            package=package,
        )
    if performance is None:
        performance = _performance_fit_summary(
            data,
            package=package,
            device=device,
            runtime_target=runtime_target,
            model_id=model_id,
        )
    if resource_envelope is None:
        resource_envelope = _resource_envelope_summary(
            package=package,
            device=device,
            model_id=model_id,
        )
    if telemetry_freshness is None:
        telemetry_freshness = _device_telemetry_freshness(device)

    optimization = _recommendation_optimization(
        {
            "performance": performance,
            "resource_envelope": resource_envelope,
        }
    )
    runtime_lane = runtime_lane_summary(runtime_target)
    artifact_lane = _model_artifact_lane_summary(
        package,
        model_id=model_id,
        runtime_target=runtime_target,
    )
    latency_headroom = _float_of(optimization.get("latency_headroom_pct"))
    throughput_headroom = _float_of(optimization.get("throughput_headroom_pct"))
    memory_headroom = _float_of(optimization.get("memory_headroom_mb"))
    storage_headroom = _float_of(optimization.get("storage_headroom_mb"))
    validation_ready = validation is not None
    performance_status = str(performance.get("status") or "")
    performance_state = str(performance.get("state") or "")
    resource_status = str(resource_envelope.get("status") or "")
    resource_state = str(resource_envelope.get("state") or "")
    telemetry_status = str(telemetry_freshness.get("status") or "")

    compatibility_score = 25 if compatible else 0
    if runtime_target_id:
        validation_score = 20 if validation_ready else (8 if compatible else 0)
        validation_state = "validated" if validation_ready else "validation missing"
    else:
        validation_score = 12 if compatible else 0
        validation_state = "device inventory"

    if performance_status == "go":
        margin_bonus = 0
        if latency_headroom is not None:
            margin_bonus += min(3, max(0, int(latency_headroom // 15)))
        if throughput_headroom is not None:
            margin_bonus += min(2, max(0, int(throughput_headroom // 40)))
        performance_score = min(25, 20 + margin_bonus)
    elif performance_state == "benchmark stale":
        performance_score = 10
    elif performance_state == "benchmark missing":
        performance_score = 8
    elif performance_state == "slo miss":
        performance_score = 3
    else:
        performance_score = 6

    if resource_status == "go":
        headroom_bonus = 0
        if memory_headroom is not None and memory_headroom >= 0:
            headroom_bonus += 2
        if storage_headroom is not None and storage_headroom >= 0:
            headroom_bonus += 2
        resource_score = min(20, 16 + headroom_bonus)
    elif resource_status == "blocked":
        resource_score = 0
    else:
        resource_score = 8

    telemetry_score = 10 if telemetry_status == "go" else 4
    score = max(
        0,
        min(
            100,
            compatibility_score
            + validation_score
            + performance_score
            + resource_score
            + telemetry_score,
        ),
    )
    hard_blocked = not compatible or resource_status == "blocked"
    if hard_blocked:
        tier = "blocked"
    elif score >= 85 and validation_score >= 20 and telemetry_status == "go":
        tier = "optimal"
    elif score >= 70:
        tier = "ready"
    else:
        tier = "needs_evidence"

    reasons: list[str] = []
    penalties: list[str] = []
    if compatible:
        reasons.append("runtime inventory satisfies model constraints")
    else:
        penalties.extend(compatibility_failures[:3])
    if validation_ready:
        reasons.append("non-dry-run runtime validation is recorded")
    elif runtime_target_id:
        penalties.append("runtime validation evidence missing")
    if artifact_lane.get("status") == "go":
        reasons.append(str(artifact_lane.get("detail") or "model artifact fits runtime lane"))
    elif artifact_lane.get("status") == "blocked":
        penalties.append(str(artifact_lane.get("detail") or "model artifact does not fit runtime lane"))
    elif artifact_lane.get("status") == "attention":
        penalties.append(str(artifact_lane.get("detail") or "model artifact lane needs review"))
    if performance_status == "go":
        reasons.append(str(performance.get("detail") or "performance evidence passes"))
    else:
        penalties.append(str(performance.get("detail") or "performance evidence needs review"))
    if resource_status == "go":
        reasons.append(str(resource_envelope.get("detail") or "resource envelope passes"))
    else:
        penalties.append(str(resource_envelope.get("detail") or "resource envelope needs review"))
    if telemetry_status == "go":
        reasons.append(str(telemetry_freshness.get("detail") or "edge telemetry is fresh"))
    else:
        penalties.append(str(telemetry_freshness.get("detail") or "edge telemetry is stale"))

    lane_label = str(runtime_lane.get("label") or runtime_lane.get("lane_id") or "runtime lane")
    runtime_context = (
        f"via {lane_label} ({runtime_target_id})"
        if runtime_target_id
        else f"using {lane_label}"
    )
    detail = (
        f"{score}/100 {tier.replace('_', ' ')} runtime fit"
        f" for {model_id} on {device_id} {runtime_context}"
    )
    runtime_capability_lock = _runtime_capability_lock(
        package=package,
        device=device,
        runtime_target=runtime_target,
        model_id=model_id,
        compatibility_failures=compatibility_failures,
        artifact_lane=artifact_lane,
        telemetry_freshness=telemetry_freshness,
    )
    return _readiness_refs(
        {
            "schema_version": "temms-runtime-fit/v1",
            "score": score,
            "tier": tier,
            "detail": detail,
            "package_id": package_id,
            "model_id": model_id,
            "device_id": device_id,
            "runtime_target_id": runtime_target_id,
            "runtime_mode": "runtime_target" if runtime_target_id else "device_inventory",
            "runtime_lane": runtime_lane,
            "artifact_lane": artifact_lane,
            "runtime_capability_lock": runtime_capability_lock,
            "components": {
                "compatibility": {
                    "score": compatibility_score,
                    "max_score": 25,
                    "state": "compatible" if compatible else "blocked",
                    "failures": compatibility_failures,
                },
                "runtime_validation": {
                    "score": validation_score,
                    "max_score": 20,
                    "state": validation_state,
                    "validation_id": (validation or {}).get("validation_id"),
                },
                "performance": {
                    "score": performance_score,
                    "max_score": 25,
                    "state": performance_state,
                    "status": performance_status,
                    "latency_headroom_pct": latency_headroom,
                    "throughput_headroom_pct": throughput_headroom,
                },
                "resource": {
                    "score": resource_score,
                    "max_score": 20,
                    "state": resource_state,
                    "status": resource_status,
                    "memory_headroom_mb": memory_headroom,
                    "storage_headroom_mb": storage_headroom,
                },
                "telemetry": {
                    "score": telemetry_score,
                    "max_score": 10,
                    "state": telemetry_freshness.get("state"),
                    "status": telemetry_status,
                },
            },
            "optimization": optimization,
            "reasons": reasons[:5],
            "penalties": penalties[:5],
        }
    )


def _runtime_target_selection_summary(
    data: dict[str, Any],
    *,
    package: dict[str, Any],
    device: dict[str, Any],
    model_id: str,
    slot: str | None = None,
    selected_runtime_target: dict[str, Any] | None,
    selected_runtime_fit: dict[str, Any],
) -> dict[str, Any]:
    """Rank the selected runtime target against compatible measured alternatives."""
    runtime_targets = list(_runtime_targets_with_defaults(data).values())
    if not runtime_targets:
        return {}

    selected_runtime_target_id = (
        str(
            (selected_runtime_target or {}).get("runtime_target_id")
            or (selected_runtime_target or {}).get("id")
            or ""
        )
        or None
    )
    candidates: list[dict[str, Any]] = []
    for runtime_target in runtime_targets:
        runtime_target_id = str(
            runtime_target.get("runtime_target_id") or runtime_target.get("id") or ""
        )
        if not runtime_target_id:
            continue
        fit = (
            selected_runtime_fit
            if runtime_target_id == selected_runtime_target_id
            else _runtime_target_fit_summary(
                data,
                package=package,
                device=device,
                runtime_target=runtime_target,
                model_id=model_id,
            )
        )
        optimization = (
            fit.get("optimization") if isinstance(fit.get("optimization"), dict) else {}
        )
        runtime_lane = (
            fit.get("runtime_lane")
            if isinstance(fit.get("runtime_lane"), dict)
            else runtime_lane_summary(runtime_target)
        )
        blocked = fit.get("tier") == "blocked"
        candidates.append(
            _readiness_refs(
                {
                    "runtime_target_id": runtime_target_id,
                    "package_id": package.get("package_id"),
                    "model_id": model_id,
                    "device_id": device.get("device_id"),
                    "slot": slot,
                    "runtime_lane": runtime_lane,
                    "runtime_target": _runtime_target_execution_ref(runtime_target),
                    "artifact_lane": fit.get("artifact_lane"),
                    "score": fit.get("score"),
                    "tier": fit.get("tier"),
                    "detail": fit.get("detail"),
                    "status": "blocked" if blocked else "eligible",
                    "eligible": not blocked,
                    "reasons": fit.get("reasons"),
                    "penalties": fit.get("penalties"),
                    "component_states": _runtime_target_component_states(fit),
                    "runtime_capability_lock": _runtime_capability_lock_summary(
                        fit.get("runtime_capability_lock")
                        if isinstance(fit.get("runtime_capability_lock"), dict)
                        else {}
                    ),
                    "latency_ms_p95": optimization.get("latency_ms_p95"),
                    "throughput_ips": optimization.get("throughput_ips"),
                    "benchmark_id": optimization.get("benchmark_id"),
                    "blocked": blocked,
                }
            )
        )

    eligible = [candidate for candidate in candidates if candidate.get("blocked") is not True]
    if not eligible:
        return {
            "schema_version": "temms-runtime-target-selection/v1",
            "status": "no_eligible_targets",
            "selected_runtime_target_id": selected_runtime_target_id,
            "candidate_count": len(candidates),
            "eligible_target_count": 0,
            "target_assessments": _runtime_target_assessments(
                candidates,
                ranked=[],
                selected_runtime_target_id=selected_runtime_target_id,
                best_runtime_target_id=None,
            ),
            "detail": "No compatible runtime target is currently eligible for this model and edge.",
        }

    def sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        latency = _float_of(candidate.get("latency_ms_p95"))
        throughput = _float_of(candidate.get("throughput_ips"))
        return (
            -int(candidate.get("score") or 0),
            latency if latency is not None else float("inf"),
            -(throughput or 0.0),
            str(candidate.get("runtime_target_id") or ""),
        )

    eligible.sort(key=sort_key)
    ranked = [{**candidate, "rank": rank} for rank, candidate in enumerate(eligible, start=1)]
    selected = next(
        (
            candidate
            for candidate in ranked
            if candidate.get("runtime_target_id") == selected_runtime_target_id
        ),
        None,
    )
    best = ranked[0]
    target_assessments = _runtime_target_assessments(
        candidates,
        ranked=ranked,
        selected_runtime_target_id=selected_runtime_target_id,
        best_runtime_target_id=str(best.get("runtime_target_id") or ""),
    )
    selected_score = _float_of((selected or selected_runtime_fit).get("score"))
    best_score = _float_of(best.get("score"))
    score_delta = (
        round(best_score - selected_score, 3)
        if best_score is not None and selected_score is not None
        else None
    )
    if selected is None:
        status = "selected_not_eligible"
        detail = (
            f"Selected runtime target {selected_runtime_target_id or 'unknown'} is not eligible; "
            f"use {best.get('runtime_target_id')} for the highest measured fit."
        )
    elif selected.get("rank") == 1:
        status = "best"
        detail = (
            f"Selected runtime target {selected_runtime_target_id} is the highest-scoring "
            f"eligible target at {selected.get('score')}/100."
        )
    else:
        status = "upgrade_available"
        delta_text = f"{score_delta:g} points above" if score_delta is not None else "above"
        detail = (
            f"{best.get('runtime_target_id')} scores {best.get('score')}/100, "
            f"{delta_text} selected {selected_runtime_target_id}."
        )
    return _readiness_refs(
        {
            "schema_version": "temms-runtime-target-selection/v1",
            "status": status,
            "selected_runtime_target_id": selected_runtime_target_id,
            "selected_rank": (selected or {}).get("rank"),
            "selected_score": (selected or selected_runtime_fit).get("score"),
            "selected_runtime_lane": (
                selected.get("runtime_lane")
                if isinstance(selected, dict) and isinstance(selected.get("runtime_lane"), dict)
                else selected_runtime_fit.get("runtime_lane")
            ),
            "best_runtime_target_id": best.get("runtime_target_id"),
            "best_score": best.get("score"),
            "best_runtime_lane": best.get("runtime_lane"),
            "score_delta": score_delta,
            "candidate_count": len(candidates),
            "eligible_target_count": len(eligible),
            "detail": detail,
            "alternatives": ranked[:3],
            "target_assessments": target_assessments,
        }
    )


def _runtime_target_component_states(fit: dict[str, Any]) -> dict[str, Any]:
    components = fit.get("components") if isinstance(fit.get("components"), dict) else {}

    def compact_component(name: str) -> dict[str, Any]:
        component = components.get(name) if isinstance(components.get(name), dict) else {}
        return _readiness_refs(
            {
                "state": component.get("state"),
                "status": component.get("status"),
                "score": component.get("score"),
                "max_score": component.get("max_score"),
                "validation_id": component.get("validation_id"),
                "failures": component.get("failures"),
            }
        )

    return _readiness_refs(
        {
            "compatibility": compact_component("compatibility"),
            "runtime_validation": compact_component("runtime_validation"),
            "performance": compact_component("performance"),
            "resource": compact_component("resource"),
            "telemetry": compact_component("telemetry"),
        }
    )


def _runtime_target_execution_ref(runtime_target: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(runtime_target, dict):
        return {}
    return _readiness_refs(
        {
            "runtime_target_id": runtime_target.get("runtime_target_id")
            or runtime_target.get("id"),
            "image": runtime_target.get("image"),
            "registry": runtime_target.get("registry"),
            "os": runtime_target.get("os"),
            "arch": runtime_target.get("arch"),
            "device_profiles": runtime_target.get("device_profiles"),
        }
    )


def _runtime_capability_lock(
    *,
    package: dict[str, Any],
    device: dict[str, Any],
    runtime_target: dict[str, Any] | None,
    model_id: str,
    compatibility_failures: list[str],
    artifact_lane: dict[str, Any],
    telemetry_freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the compact on-device capability basis for runtime admission."""
    runtime_target_id = (
        str(runtime_target.get("runtime_target_id") or runtime_target.get("id") or "")
        if runtime_target
        else None
    )
    model = _catalog_model(package, model_id)
    package_profiles = [
        normalized
        for normalized in (
            normalize_device_profile(profile) for profile in package.get("device_profiles", [])
        )
        if normalized
    ]
    model_constraints = [
        {"model_id": constrained_model_id, "constraints": constraints}
        for constrained_model_id, constraints in _catalog_runtime_constraints(
            package,
            model_id=model_id,
        )
    ]
    target_profiles = [
        normalized
        for normalized in (
            normalize_device_profile(profile)
            for profile in (runtime_target or {}).get("device_profiles", [])
        )
        if normalized
    ]
    inventory = device.get("inventory") if isinstance(device.get("inventory"), dict) else {}
    artifact_status = str(artifact_lane.get("status") or "")
    if telemetry_freshness is None:
        telemetry_freshness = _device_telemetry_freshness(device)
    telemetry_status = str(telemetry_freshness.get("status") or "")
    failures = list(compatibility_failures)
    if artifact_status == "blocked":
        failures.append(str(artifact_lane.get("detail") or "model artifact does not fit runtime lane"))
    if telemetry_status != "go":
        freshness_detail = str(
            telemetry_freshness.get("detail")
            or telemetry_freshness.get("state")
            or "edge heartbeat timestamp is not fresh"
        )
        failures.append(f"edge inventory freshness is not locked: {freshness_detail}")
    status = "locked" if not failures and artifact_status != "attention" else "blocked" if failures else "attention"
    payload = _readiness_refs(
        {
            "schema_version": "temms-runtime-capability-lock/v1",
            "status": status,
            "package_id": package.get("package_id"),
            "model_id": model_id,
            "device_id": device.get("device_id"),
            "runtime_target_id": runtime_target_id,
            "runtime_mode": "runtime_target" if runtime_target_id else "device_inventory",
            "model_requirements": _readiness_refs(
                {
                    "device_profiles": package_profiles,
                    "model_format": _model_artifact_format(model),
                    "filename": (model or {}).get("filename"),
                    "runtime_constraints": model_constraints,
                }
            ),
            "runtime_target": _runtime_target_capability_basis(runtime_target),
            "edge_inventory": _edge_inventory_capability_basis(
                device,
                inventory,
                telemetry_freshness=telemetry_freshness,
            ),
            "artifact_lane": _readiness_refs(
                {
                    "status": artifact_lane.get("status"),
                    "state": artifact_lane.get("state"),
                    "model_format": artifact_lane.get("model_format"),
                    "lane_id": artifact_lane.get("lane_id"),
                    "native_formats": artifact_lane.get("native_formats"),
                    "convertible_formats": artifact_lane.get("convertible_formats"),
                }
            ),
            "failures": failures[:8],
        }
    )
    return {
        **payload,
        "capability_sha256": canonical_json_hash(
            _runtime_capability_lock_digest_payload(payload)
        ),
    }


def _runtime_target_capability_basis(
    runtime_target: dict[str, Any] | None,
) -> dict[str, Any]:
    if runtime_target is None:
        return {}
    return _readiness_refs(
        {
            "runtime_target_id": runtime_target.get("runtime_target_id")
            or runtime_target.get("id"),
            "image": runtime_target.get("image"),
            "registry": runtime_target.get("registry"),
            "os": runtime_target.get("os"),
            "arch": runtime_target.get("arch"),
            "device_profiles": [
                normalized
                for normalized in (
                    normalize_device_profile(profile)
                    for profile in runtime_target.get("device_profiles", [])
                )
                if normalized
            ],
            "runtime_lane": runtime_lane_summary(runtime_target),
            "runtime_constraints": runtime_target.get("runtime_constraints"),
            "inventory_constraints": _runtime_target_inventory_constraints(runtime_target),
            "runtimes": _compact_runtime_surface(runtime_target.get("runtimes")),
            "accelerators": _compact_accelerator_surface(runtime_target.get("accelerators")),
        }
    )


def _edge_inventory_capability_basis(
    device: dict[str, Any],
    inventory: dict[str, Any],
    *,
    telemetry_freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if telemetry_freshness is None:
        telemetry_freshness = _device_telemetry_freshness(device)
    return _readiness_refs(
        {
            "device_profile": normalize_device_profile(
                inventory.get("device_profile") or device.get("profile")
            ),
            "status": device.get("status"),
            "last_seen_at": device.get("last_seen_at"),
            "telemetry_freshness": _readiness_refs(
                {
                    "status": telemetry_freshness.get("status"),
                    "state": telemetry_freshness.get("state"),
                    "detail": telemetry_freshness.get("detail"),
                    "last_seen_at": telemetry_freshness.get("last_seen_at"),
                    "heartbeat_age_seconds": telemetry_freshness.get(
                        "heartbeat_age_seconds"
                    ),
                    "heartbeat_stale_after_seconds": telemetry_freshness.get(
                        "heartbeat_stale_after_seconds"
                    ),
                }
            ),
            "runtimes": _compact_runtime_surface(inventory.get("runtimes")),
            "accelerators": _compact_accelerator_surface(inventory.get("accelerators")),
            "memory": inventory.get("memory") if isinstance(inventory.get("memory"), dict) else None,
            "storage": (
                inventory.get("storage") if isinstance(inventory.get("storage"), dict) else None
            ),
            "thermal": (
                inventory.get("thermal") if isinstance(inventory.get("thermal"), dict) else None
            ),
            "power": inventory.get("power") if isinstance(inventory.get("power"), dict) else None,
        }
    )


def _compact_runtime_surface(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, Any] = {}
    for runtime, status in sorted(value.items()):
        runtime_id = str(runtime)
        if isinstance(status, dict):
            compact[runtime_id] = _readiness_refs(
                {
                    "available": status.get("available"),
                    "providers": status.get("providers"),
                    "version": status.get("version"),
                    "options": status.get("options"),
                }
            )
        else:
            compact[runtime_id] = status
    return compact


def _compact_accelerator_surface(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, Any] = {}
    for accelerator, status in sorted(value.items()):
        accelerator_id = str(accelerator)
        if isinstance(status, dict):
            compact[accelerator_id] = _readiness_refs(
                {
                    "available": status.get("available"),
                    "vendor": status.get("vendor"),
                    "name": status.get("name"),
                    "driver": status.get("driver"),
                    "count": status.get("count"),
                }
            )
        else:
            compact[accelerator_id] = status
    return compact


def _runtime_capability_lock_summary(lock: dict[str, Any]) -> dict[str, Any]:
    if not lock:
        return {}
    return _readiness_refs(
        {
            "schema_version": lock.get("schema_version"),
            "status": lock.get("status"),
            "capability_sha256": lock.get("capability_sha256"),
            "runtime_target_id": lock.get("runtime_target_id"),
            "runtime_mode": lock.get("runtime_mode"),
            "model_requirements": lock.get("model_requirements"),
            "runtime_target": lock.get("runtime_target"),
            "edge_inventory": lock.get("edge_inventory"),
            "artifact_lane": lock.get("artifact_lane"),
            "failures": lock.get("failures"),
        }
    )


def _runtime_capability_lock_digest_payload(lock: dict[str, Any]) -> dict[str, Any]:
    """Return stable capability facts used for capability lock hashing."""
    edge_inventory = lock.get("edge_inventory") if isinstance(lock.get("edge_inventory"), dict) else {}
    telemetry_freshness = (
        edge_inventory.get("telemetry_freshness")
        if isinstance(edge_inventory.get("telemetry_freshness"), dict)
        else {}
    )
    return _readiness_refs(
        {
            "schema_version": lock.get("schema_version"),
            "status": lock.get("status"),
            "package_id": lock.get("package_id"),
            "model_id": lock.get("model_id"),
            "device_id": lock.get("device_id"),
            "runtime_target_id": lock.get("runtime_target_id"),
            "runtime_mode": lock.get("runtime_mode"),
            "model_requirements": lock.get("model_requirements"),
            "runtime_target": lock.get("runtime_target"),
            "edge_inventory": _readiness_refs(
                {
                    "device_profile": edge_inventory.get("device_profile"),
                    "telemetry_freshness": _readiness_refs(
                        {
                            "status": telemetry_freshness.get("status"),
                            "state": telemetry_freshness.get("state"),
                            "heartbeat_stale_after_seconds": telemetry_freshness.get(
                                "heartbeat_stale_after_seconds"
                            ),
                        }
                    ),
                    "runtimes": edge_inventory.get("runtimes"),
                    "accelerators": edge_inventory.get("accelerators"),
                }
            ),
            "artifact_lane": lock.get("artifact_lane"),
            "failures": lock.get("failures"),
        }
    )


def _runtime_target_assessments(
    candidates: list[dict[str, Any]],
    *,
    ranked: list[dict[str, Any]],
    selected_runtime_target_id: str | None,
    best_runtime_target_id: str | None,
) -> list[dict[str, Any]]:
    ranked_by_id = {
        str(candidate.get("runtime_target_id") or ""): candidate
        for candidate in ranked
        if candidate.get("runtime_target_id")
    }

    assessments: list[dict[str, Any]] = []
    for candidate in candidates:
        runtime_target_id = str(candidate.get("runtime_target_id") or "")
        if not runtime_target_id:
            continue
        ranked_candidate = ranked_by_id.get(runtime_target_id, {})
        blocked = candidate.get("blocked") is True
        assessments.append(
            _readiness_refs(
                {
                    **candidate,
                    "rank": ranked_candidate.get("rank"),
                    "selected": runtime_target_id == selected_runtime_target_id,
                    "best": runtime_target_id == best_runtime_target_id,
                    "status": "blocked" if blocked else "eligible",
                    "remediation": _runtime_target_assessment_remediation(
                        candidate,
                        runtime_target_id=runtime_target_id,
                        selected_runtime_target_id=selected_runtime_target_id,
                        best_runtime_target_id=best_runtime_target_id,
                    ),
                }
            )
        )

    def assessment_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        runtime_target_id = str(candidate.get("runtime_target_id") or "")
        rank = int(candidate.get("rank") or 999)
        score = _float_of(candidate.get("score")) or 0.0
        selected_rank = 0 if runtime_target_id == selected_runtime_target_id else 1
        best_rank = 0 if runtime_target_id == best_runtime_target_id else 1
        blocked_rank = 1 if candidate.get("blocked") is True else 0
        return (selected_rank, best_rank, blocked_rank, rank, -score, runtime_target_id)

    assessments.sort(key=assessment_sort_key)
    return assessments


def _runtime_target_assessment_remediation(
    candidate: dict[str, Any],
    *,
    runtime_target_id: str,
    selected_runtime_target_id: str | None,
    best_runtime_target_id: str | None,
) -> dict[str, Any]:
    """Return the next operator move for one runtime-target assessment."""
    component_states = (
        candidate.get("component_states")
        if isinstance(candidate.get("component_states"), dict)
        else {}
    )
    raw_penalties = candidate.get("penalties") if isinstance(candidate.get("penalties"), list) else []
    penalties = [
        str(penalty)
        for penalty in raw_penalties
        if penalty
    ]
    penalty_text = "; ".join(penalties).lower()
    selected = runtime_target_id == selected_runtime_target_id
    best = runtime_target_id == best_runtime_target_id
    refs = _readiness_refs(
        {
            "runtime_target_id": runtime_target_id,
            "package_id": candidate.get("package_id"),
            "model_id": candidate.get("model_id"),
            "device_id": candidate.get("device_id"),
            "slot": candidate.get("slot"),
            "selected_runtime_target_id": selected_runtime_target_id,
            "best_runtime_target_id": best_runtime_target_id,
        }
    )

    if candidate.get("blocked") is True:
        artifact_lane = (
            candidate.get("artifact_lane")
            if isinstance(candidate.get("artifact_lane"), dict)
            else {}
        )
        compatibility = (
            component_states.get("compatibility")
            if isinstance(component_states.get("compatibility"), dict)
            else {}
        )
        resource = (
            component_states.get("resource")
            if isinstance(component_states.get("resource"), dict)
            else {}
        )
        if "device profile" in penalty_text:
            action = "select_matching_edge_class"
            label = "Use matching edge class"
            detail = penalties[0] if penalties else "Runtime target requires a different edge profile."
        elif artifact_lane.get("status") == "blocked":
            action = "package_runtime_artifact"
            label = "Package runtime artifact"
            detail = str(
                artifact_lane.get("detail")
                or "Package an artifact format native to this runtime lane."
            )
        elif resource.get("status") == "blocked" or resource.get("state") == "blocked":
            action = "free_edge_resources"
            label = "Free edge resources"
            detail = penalties[0] if penalties else "Selected edge does not satisfy resource envelope."
        elif compatibility.get("state") == "blocked" or compatibility.get("failures"):
            action = "resolve_runtime_capability"
            label = "Resolve capability gap"
            detail = penalties[0] if penalties else "Runtime target capability requirements are not met."
        else:
            action = "resolve_target_blocker"
            label = "Resolve target blocker"
            detail = penalties[0] if penalties else "Runtime target is not eligible for this path."
        return _runtime_target_remediation(
            action,
            label,
            detail,
            requires_edge_execution=False,
            refs=refs,
            candidate=candidate,
        )

    validation = (
        component_states.get("runtime_validation")
        if isinstance(component_states.get("runtime_validation"), dict)
        else {}
    )
    performance = (
        component_states.get("performance")
        if isinstance(component_states.get("performance"), dict)
        else {}
    )
    telemetry = (
        component_states.get("telemetry")
        if isinstance(component_states.get("telemetry"), dict)
        else {}
    )
    if validation.get("state") == "validation missing":
        return _runtime_target_remediation(
            "validate_runtime",
            "Validate runtime",
            "Run non-dry-run package validation inside this runtime target.",
            requires_edge_execution=False,
            refs=refs,
            candidate=candidate,
        )
    if performance.get("status") != "go":
        return _runtime_target_remediation(
            "record_benchmark",
            "Record edge benchmark",
            "Run benchmark proof on the selected edge/runtime path.",
            requires_edge_execution=True,
            refs=refs,
            candidate=candidate,
        )
    if telemetry.get("status") not in {"", "go"}:
        return _runtime_target_remediation(
            "refresh_edge_inventory",
            "Refresh edge inventory",
            "Refresh heartbeat and runtime/provider inventory before admission.",
            requires_edge_execution=True,
            refs=refs,
            candidate=candidate,
        )
    if best and selected:
        return _runtime_target_remediation(
            "ready",
            "Use for field apply",
            "Selected runtime is the best measured eligible path.",
            requires_edge_execution=False,
            refs=refs,
            candidate=candidate,
        )
    if best:
        return _runtime_target_remediation(
            "use_best_runtime",
            "Use best runtime",
            f"Switch the selected path to {runtime_target_id} before rollout.",
            requires_edge_execution=False,
            refs=refs,
            candidate=candidate,
        )
    return _runtime_target_remediation(
        "fallback_candidate",
        "Keep as fallback",
        "Eligible measured fallback if the selected runtime becomes unavailable.",
        requires_edge_execution=False,
        refs=refs,
        candidate=candidate,
    )


def _runtime_target_remediation(
    action: str,
    label: str,
    detail: str,
    *,
    requires_edge_execution: bool,
    refs: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    remediation = _readiness_refs(
        {
            "action": action,
            "label": label,
            "detail": detail,
            "requires_edge_execution": requires_edge_execution,
            "refs": refs,
        }
    )
    remediation.update(_runtime_target_remediation_command(action, refs, candidate))
    return remediation


def _runtime_target_remediation_command(
    action: str,
    refs: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    if action == "record_benchmark":
        command = _readiness_action_command("record_benchmark", refs)
        return _runtime_target_command_fields(command)

    if action == "validate_runtime":
        command = [
            "uv",
            "run",
            "temms",
            "hub",
            "validate-runtime",
            "<package-path>",
            "--hub-url",
            "${TEMMS_HUB_URL}",
            "--package-id",
            str(refs.get("package_id") or "<package-id>"),
            "--runtime-target-id",
            str(refs.get("runtime_target_id") or "<runtime-target-id>"),
            "--actor",
            RUNTIME_REMEDIATION_ACTOR,
            "--require-signature",
        ]
        return _runtime_target_operator_command_fields(
            command,
            "Replace <package-path> with the signed TEMMS package artifact.",
        )

    if action == "refresh_edge_inventory":
        command = [
            "env",
            "TEMMS_HUB_URL=${TEMMS_HUB_URL}",
            f"TEMMS_DEVICE_ID={refs.get('device_id') or '<device-id>'}",
            "TEMMS_EDGE_HEARTBEAT_INTERVAL_S=10",
            "temms",
            "daemon",
            "start",
            "--foreground",
        ]
        return _runtime_target_edge_command_fields(
            command,
            "Run on the edge node to refresh runtime/provider inventory and heartbeat freshness.",
        )

    if action == "package_runtime_artifact":
        runtime_lane = (
            candidate.get("runtime_lane")
            if isinstance(candidate.get("runtime_lane"), dict)
            else {}
        )
        command = [
            "uv",
            "run",
            "temms",
            "hub",
            "package-from-mlflow",
            "<model-uri>",
            "--hub-url",
            "${TEMMS_HUB_URL}",
            "--slot",
            str(refs.get("slot") or "vision"),
            "--model-artifact",
            "<runtime-native-artifact-path>",
            "--actor",
            RUNTIME_REMEDIATION_ACTOR,
        ]
        execution_engine = str(runtime_lane.get("execution_engine") or "")
        if execution_engine:
            command.extend(["--runtime", execution_engine])
        for provider in _runtime_target_command_values(runtime_lane.get("providers")):
            command.extend(["--provider", provider])
        for accelerator in _runtime_target_command_values(runtime_lane.get("accelerators")):
            command.extend(["--accelerator", accelerator])
        return _runtime_target_operator_command_fields(
            command,
            "Package a runtime-native artifact, then re-run validation and strict proof.",
        )

    if action in {
        "select_matching_edge_class",
        "resolve_runtime_capability",
        "free_edge_resources",
        "resolve_target_blocker",
    }:
        command = [
            "uv",
            "run",
            "temms",
            "hub",
            "compatibility-matrix",
            "--hub-url",
            "${TEMMS_HUB_URL}",
            "--device-id",
            str(refs.get("device_id") or "<device-id>"),
            "--package-id",
            str(refs.get("package_id") or "<package-id>"),
            "--model-id",
            str(refs.get("model_id") or "<model-id>"),
            "--runtime-target-id",
            str(refs.get("runtime_target_id") or "<runtime-target-id>"),
            "--include-device-inventory",
            "--json",
        ]
        return _runtime_target_operator_command_fields(
            command,
            "Inspect live edge inventory against the model and runtime target constraints.",
        )

    command = [
        "uv",
        "run",
        "temms",
        "hub",
        "edge-runtime-mission",
        "--hub-url",
        "${TEMMS_HUB_URL}",
        "--package-id",
        str(refs.get("package_id") or "<package-id>"),
        "--model-id",
        str(refs.get("model_id") or "<model-id>"),
        "--device-id",
        str(refs.get("device_id") or "<device-id>"),
        "--runtime-target-id",
        str(refs.get("runtime_target_id") or "<runtime-target-id>"),
        "--slot",
        str(refs.get("slot") or "vision"),
        "--require-go",
        "--require-best-runtime",
        "--require-capability-lock",
        "--min-runtime-fit",
        "95",
        "--json",
    ]
    return _runtime_target_operator_command_fields(
        command,
        "Re-check this runtime path against the signed edge-runtime gate.",
    )


def _runtime_target_command_fields(command: dict[str, Any] | None) -> dict[str, Any]:
    if not command:
        return {}
    return _readiness_refs(
        {
            "command": command,
            "edge_command": command.get("edge_command"),
            "edge_command_text": command.get("edge_command_text"),
            "edge_command_note": command.get("edge_command_note"),
        }
    )


def _runtime_target_operator_command_fields(
    command: list[str],
    note: str,
) -> dict[str, Any]:
    return _readiness_refs(
        {
            "operator_command": command,
            "operator_command_text": shlex.join(command) if command else None,
            "operator_command_note": note,
        }
    )


def _runtime_target_edge_command_fields(command: list[str], note: str) -> dict[str, Any]:
    return _readiness_refs(
        {
            "edge_command": command,
            "edge_command_text": shlex.join(command) if command else None,
            "edge_command_note": note,
        }
    )


def _runtime_target_command_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, dict):
        values: list[str] = []
        for key, raw in value.items():
            if raw is True:
                values.append(str(key))
            elif isinstance(raw, dict) and raw.get("available") is not False:
                values.append(str(key))
        return values
    return []


def _deployment_readiness(
    data: dict[str, Any],
    *,
    package_id: str | None = None,
    model_id: str | None = None,
    device_id: str | None = None,
    runtime_target_id: str | None = None,
    slot: str | None = None,
) -> dict[str, Any]:
    seed_rollout = _select_readiness_rollout(
        data,
        package_id=package_id,
        model_id=model_id,
        device_id=device_id,
        runtime_target_id=runtime_target_id,
        slot=slot,
    )
    package = _select_readiness_package(
        data,
        package_id=package_id or (seed_rollout or {}).get("package_id"),
    )
    selected_package_id = package.get("package_id") if package else package_id
    selected_model_id = model_id or (seed_rollout or {}).get("model_id")
    if package is not None and selected_model_id:
        _validate_package_model(
            package,
            package_id=str(selected_package_id or ""),
            model_id=str(selected_model_id),
        )
    if package is not None and not selected_model_id:
        selected_model_id = _first_declared_model_id(package)

    device = _select_readiness_device(
        data,
        device_id=device_id or (seed_rollout or {}).get("device_id"),
    )
    selected_device_id = device.get("device_id") if device else device_id
    runtime_target = _select_readiness_runtime_target(
        data,
        runtime_target_id=runtime_target_id or (seed_rollout or {}).get("runtime_target_id"),
        package=package,
        device=device,
        model_id=selected_model_id,
    )
    runtime_target_is_pinned = bool(runtime_target_id or (seed_rollout or {}).get("runtime_target_id"))
    selected_runtime_target_id = (
        runtime_target.get("runtime_target_id") if runtime_target else runtime_target_id
    )
    latest_rollout = _select_readiness_rollout(
        data,
        package_id=selected_package_id,
        model_id=selected_model_id,
        device_id=selected_device_id,
        runtime_target_id=selected_runtime_target_id,
        slot=slot,
    )

    gates = [
        _model_package_readiness_gate(package, model_id=selected_model_id),
        _runtime_target_readiness_gate(
            data,
            package=package,
            device=device,
            runtime_target=runtime_target,
            model_id=selected_model_id,
            slot=slot or (latest_rollout or {}).get("slot"),
            rollout=latest_rollout,
        ),
        _performance_readiness_gate(
            data,
            package=package,
            device=device,
            runtime_target=runtime_target,
            model_id=selected_model_id,
            slot=slot or (latest_rollout or {}).get("slot"),
            rollout=latest_rollout,
        ),
        _resource_envelope_readiness_gate(
            data=data,
            package=package,
            device=device,
            runtime_target=runtime_target,
            model_id=selected_model_id,
            slot=slot or (latest_rollout or {}).get("slot"),
            rollout=latest_rollout,
        ),
        _edge_target_readiness_gate(device),
        _rollout_readiness_gate(
            latest_rollout,
            package_id=selected_package_id,
            model_id=selected_model_id,
            device_id=selected_device_id,
            runtime_target_id=selected_runtime_target_id,
            slot=slot or (latest_rollout or {}).get("slot"),
        ),
    ]
    selection = {
        "package_id": selected_package_id,
        "model_id": selected_model_id,
        "device_id": selected_device_id,
        "runtime_target_id": selected_runtime_target_id,
        "slot": slot or (latest_rollout or {}).get("slot"),
        "rollout_id": (latest_rollout or {}).get("rollout_id"),
    }
    runtime_fit = (
        _runtime_target_fit_summary(
            data,
            package=package,
            device=device,
            runtime_target=runtime_target,
            model_id=str(selected_model_id),
        )
        if package is not None
        and device is not None
        and runtime_target is not None
        and selected_model_id
        else None
    )
    if runtime_fit is not None:
        target_selection = _runtime_target_selection_summary(
            data,
            package=package,
            device=device,
            model_id=str(selected_model_id),
            slot=slot or (latest_rollout or {}).get("slot"),
            selected_runtime_target=runtime_target,
            selected_runtime_fit=runtime_fit,
        )
        if target_selection:
            runtime_fit = {**runtime_fit, "target_selection": target_selection}
            gates.insert(
                2,
                _runtime_optimization_readiness_gate(
                    target_selection,
                    package_id=selected_package_id,
                    model_id=selected_model_id,
                    device_id=selected_device_id,
                    slot=slot or (latest_rollout or {}).get("slot"),
                    ),
                )
    return _finalize_deployment_readiness(
        gates,
        checked_at=_now(),
        selection={key: value for key, value in selection.items() if value},
        runtime_fit=runtime_fit,
        production_admission=_production_admission_summary(
            gates,
            runtime_target_id=selected_runtime_target_id if runtime_target_is_pinned else None,
        ),
    )


def _model_package_readiness_gate(
    package: dict[str, Any] | None,
    *,
    model_id: str | None,
) -> dict[str, Any]:
    if package is None:
        return _readiness_gate(
            "model_package",
            "Model package",
            "blocked",
            "missing",
            "Register a signed TEMMS package with model metadata",
            actions=[
                _readiness_action(
                    "register_package",
                    "Register signed package",
                    "register_package",
                )
            ],
        )

    package_id = str(package.get("package_id") or package.get("id") or "")
    package_label = str(package.get("name") or model_id or package_id or "selected package")
    package_summary = _package_compatibility_summary(package)
    promotion = _package_promotion_summary(package)
    promotion_state = str(promotion.get("state") or "candidate")
    refs = {"package_id": package_id, "model_id": model_id}
    if package_summary.get("signature_verified") is not True:
        return _readiness_gate(
            "model_package",
            "Model package",
            "blocked",
            "unsigned",
            "Register a package with verified signature metadata",
            refs=refs,
            actions=[
                _readiness_action(
                    "register_signed_package",
                    "Register signed package",
                    "register_package",
                    refs=refs,
                )
            ],
        )
    if promotion_state == "released":
        return _readiness_gate(
            "model_package",
            "Model package",
            "go",
            "released",
            f"{package_label} is signed and released",
            refs=refs,
        )
    if promotion_state == "retired":
        return _readiness_gate(
            "model_package",
            "Model package",
            "blocked",
            "retired",
            f"{package_id} is retired and cannot be assigned",
            refs=refs,
            actions=[
                _readiness_action(
                    "select_released_package",
                    "Select released package",
                    "select_package",
                    refs=refs,
                )
            ],
        )
    return _readiness_gate(
        "model_package",
        "Model package",
        "attention",
        promotion_state,
        "Promote the signed package to released before field assignment",
        refs=refs,
        actions=[
            _readiness_action(
                "promote_package_release",
                "Promote package",
                "promote_package",
                refs={**refs, "target_state": "released"},
            )
        ],
    )


def _runtime_target_readiness_gate(
    data: dict[str, Any],
    *,
    package: dict[str, Any] | None,
    device: dict[str, Any] | None,
    runtime_target: dict[str, Any] | None,
    model_id: str | None,
    slot: str | None,
    rollout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if runtime_target is None:
        return _readiness_gate(
            "runtime_target",
            "Runtime target",
            "blocked",
            "missing",
            "Register or select a runtime target for this model",
            actions=[
                _readiness_action(
                    "register_runtime_target",
                    "Register runtime target",
                    "register_runtime_target",
                )
            ],
        )
    runtime_target_id = str(runtime_target.get("runtime_target_id") or runtime_target.get("id") or "")
    rollout_id = str((rollout or {}).get("rollout_id") or "")
    rollout_state = str((rollout or {}).get("state") or "")
    drift_sensitive = rollout_state in {"imported", "activated"}
    refs = {
        "package_id": (package or {}).get("package_id"),
        "device_id": (device or {}).get("device_id"),
        "runtime_target_id": runtime_target_id,
        "model_id": model_id,
        "slot": slot,
        "rollout_id": rollout_id,
        "rollout_state": rollout_state,
    }
    if package is None or device is None:
        return _readiness_gate(
            "runtime_target",
            "Runtime target",
            "attention",
            "selected",
            "Select a package and edge target to evaluate runtime compatibility",
            refs=refs,
            actions=[
                _readiness_action(
                    "select_deployment_context",
                    "Select package and edge target",
                    "select_context",
                    refs=refs,
                )
            ],
        )

    package_id = str(package.get("package_id") or "")
    device_id = str(device.get("device_id") or "")
    preview = _rollout_compatibility_preview(
        data,
        device_id=device_id,
        package_id=package_id,
        runtime_target_id=runtime_target_id,
        model_id=model_id,
    )
    if preview.get("compatible") is not True:
        failure_list = [str(failure) for failure in preview.get("failures") or []]
        failures = "; ".join(failure_list)
        drift_refs = _readiness_refs(
            {
                **refs,
                "runtime_failures": failure_list,
                "runtime_drift": True if drift_sensitive else None,
            }
        )
        if drift_sensitive:
            fallback_action = _fallback_model_readiness_action(
                data,
                package=package,
                device=device,
                runtime_target=runtime_target,
                current_model_id=str(model_id or ""),
                slot=slot,
                drift_kind="runtime",
                allow_runtime_switch=True,
            )
            actions = [
                action
                for action in [
                    fallback_action,
                    _readiness_action(
                        "rollback_runtime_drift",
                        "Rollback active rollout",
                        "rollback_rollout",
                        refs=drift_refs,
                    ),
                    _readiness_action(
                        "refresh_runtime_inventory",
                        "Refresh edge inventory",
                        "refresh_edge_inventory",
                        refs=drift_refs,
                    ),
                ]
                if action is not None
            ]
            return _readiness_gate(
                "runtime_target",
                "Runtime target",
                "blocked",
                "runtime drift",
                (
                    f"Active rollout {rollout_id or 'selected rollout'} can no longer run on "
                    f"{runtime_target_id}: {failures or 'runtime inventory no longer matches'}"
                ),
                refs=drift_refs,
                actions=actions,
            )
        return _readiness_gate(
            "runtime_target",
            "Runtime target",
            "blocked",
            "incompatible",
            failures or "Selected runtime target does not satisfy model constraints",
            refs=drift_refs,
            actions=[
                _readiness_action(
                    "select_compatible_runtime",
                    "Select compatible runtime",
                    "select_runtime_target",
                    refs=refs,
                )
            ],
        )

    freshness = _device_telemetry_freshness(device)
    refs = {**refs, **_device_telemetry_refs(freshness)}
    if freshness["status"] != "go":
        return _readiness_gate(
            "runtime_target",
            "Runtime target",
            "attention",
            "inventory stale",
            f"{device_id} runtime inventory is not fresh enough to trust: {freshness['detail']}",
            refs=refs,
            actions=[
                _readiness_action(
                    "refresh_runtime_inventory",
                    "Refresh edge inventory",
                    "refresh_edge_inventory",
                    refs=refs,
                )
            ],
        )

    validation = _latest_passing_runtime_validation(
        data,
        package_id=package_id,
        runtime_target_id=runtime_target_id,
        package=package,
    )
    if validation is not None:
        validation_id = str(validation.get("validation_id") or "latest validation")
        return _readiness_gate(
            "runtime_target",
            "Runtime target",
            "go",
            "validated",
            f"{runtime_target_id} has passing package validation",
            refs={**refs, "validation_id": validation_id},
        )
    return _readiness_gate(
        "runtime_target",
        "Runtime target",
        "attention",
        "compatible",
        "Run non-dry-run validation before requiring runtime evidence",
        refs=refs,
        actions=[
            _readiness_action(
                "validate_runtime_target",
                "Validate runtime target",
                "validate_runtime",
                refs=refs,
            )
        ],
    )


def _runtime_optimization_readiness_gate(
    target_selection: dict[str, Any],
    *,
    package_id: str | None,
    model_id: str | None,
    device_id: str | None,
    slot: str | None,
) -> dict[str, Any]:
    status = str(target_selection.get("status") or "")
    selected_runtime_target_id = target_selection.get("selected_runtime_target_id")
    best_runtime_target_id = target_selection.get("best_runtime_target_id")
    refs = _readiness_refs(
        {
            "package_id": package_id,
            "model_id": model_id,
            "device_id": device_id,
            "runtime_target_id": selected_runtime_target_id,
            "best_runtime_target_id": best_runtime_target_id,
            "slot": slot,
            "selected_rank": target_selection.get("selected_rank"),
            "best_score": target_selection.get("best_score"),
            "score_delta": target_selection.get("score_delta"),
        }
    )
    if status == "best":
        return _readiness_gate(
            "runtime_optimizer",
            "Runtime optimizer",
            "go",
            "best target",
            str(target_selection.get("detail") or "Selected runtime target is the best measured fit"),
            refs=refs,
        )
    if status == "upgrade_available":
        action_refs = {
            **refs,
            "runtime_target_id": best_runtime_target_id,
            "previous_runtime_target_id": selected_runtime_target_id,
        }
        return _readiness_gate(
            "runtime_optimizer",
            "Runtime optimizer",
            "attention",
            "better target available",
            str(target_selection.get("detail") or "A higher-scoring runtime target is available"),
            refs=refs,
            actions=[
                _readiness_action(
                    "select_best_runtime_target",
                    "Use best runtime",
                    "select_runtime_target",
                    refs=action_refs,
                )
            ],
        )
    if status in {"selected_not_eligible", "no_eligible_targets"}:
        actions = []
        if best_runtime_target_id:
            action_refs = {
                **refs,
                "runtime_target_id": best_runtime_target_id,
                "previous_runtime_target_id": selected_runtime_target_id,
            }
            actions.append(
                _readiness_action(
                    "select_best_runtime_target",
                    "Use best runtime",
                    "select_runtime_target",
                    refs=action_refs,
                )
            )
        return _readiness_gate(
            "runtime_optimizer",
            "Runtime optimizer",
            "blocked",
            status.replace("_", " "),
            str(target_selection.get("detail") or "No eligible runtime target is available"),
            refs=refs,
            actions=actions,
        )
    return _readiness_gate(
        "runtime_optimizer",
        "Runtime optimizer",
        "attention",
        "comparison pending",
        str(target_selection.get("detail") or "Runtime target comparison needs review"),
        refs=refs,
    )


def _performance_readiness_gate(
    data: dict[str, Any],
    *,
    package: dict[str, Any] | None,
    device: dict[str, Any] | None,
    runtime_target: dict[str, Any] | None,
    model_id: str | None,
    slot: str | None,
    rollout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    package_id = str((package or {}).get("package_id") or "")
    device_id = str((device or {}).get("device_id") or "")
    runtime_target_id = str(
        (runtime_target or {}).get("runtime_target_id") or (runtime_target or {}).get("id") or ""
    )
    rollout_id = str((rollout or {}).get("rollout_id") or "")
    rollout_state = str((rollout or {}).get("state") or "")
    drift_sensitive = rollout_state in {"imported", "activated"}
    refs = {
        "package_id": package_id,
        "model_id": model_id,
        "device_id": device_id,
        "runtime_target_id": runtime_target_id,
        "slot": slot,
        "rollout_id": rollout_id,
        "rollout_state": rollout_state,
    }
    if package is None or device is None or not model_id:
        return _readiness_gate(
            "performance_fit",
            "Performance fit",
            "go",
            "not scoped",
            "Performance evidence waits for a selected model and edge target",
            refs=refs,
        )

    fit = _performance_fit_summary(
        data,
        package=package,
        device=device,
        runtime_target=runtime_target,
        model_id=model_id,
    )
    fit_refs = {**refs, **_performance_fit_refs(fit)}
    if drift_sensitive and fit.get("state") == "slo miss":
        fit_refs["performance_drift"] = True
    if fit["status"] == "go":
        return _readiness_gate(
            "performance_fit",
            "Performance fit",
            "go",
            str(fit.get("state") or "benchmarked"),
            str(fit.get("detail") or "Benchmark evidence satisfies declared performance SLOs"),
            refs=fit_refs,
        )
    if drift_sensitive and fit.get("state") == "slo miss":
        fallback_action = _fallback_model_readiness_action(
            data,
            package=package,
            device=device,
            runtime_target=runtime_target,
            current_model_id=model_id,
            slot=slot,
            drift_kind="performance",
        )
        actions = [
            action
            for action in [
                fallback_action,
                _readiness_action(
                    "rollback_performance_drift",
                    "Rollback active rollout",
                    "rollback_rollout",
                    refs=fit_refs,
                ),
                _readiness_action(
                    "record_performance_benchmark",
                    "Record benchmark",
                    "record_benchmark",
                    refs=fit_refs,
                ),
            ]
            if action is not None
        ]
        return _readiness_gate(
            "performance_fit",
            "Performance fit",
            "blocked",
            "performance drift",
            (
                f"Active rollout {rollout_id or 'selected rollout'} no longer meets "
                f"the model performance SLO: {fit.get('detail')}"
            ),
            refs=fit_refs,
            actions=actions,
        )
    state = str(fit.get("state") or "needs benchmark")
    detail = str(fit.get("detail") or "Record benchmark evidence for this model/runtime/device")
    if drift_sensitive and state in {"benchmark missing", "benchmark stale"}:
        state = "drift unverified"
        detail = (
            f"Active rollout {rollout_id or 'selected rollout'} cannot prove its "
            f"performance SLO from current benchmark evidence: {detail}"
        )
    return _readiness_gate(
        "performance_fit",
        "Performance fit",
        "attention",
        state,
        detail,
        refs=fit_refs,
        actions=[
            _readiness_action(
                "record_performance_benchmark",
                "Record benchmark",
                "record_benchmark",
                refs=fit_refs,
            )
        ],
    )


def _resource_envelope_readiness_gate(
    *,
    data: dict[str, Any],
    package: dict[str, Any] | None,
    device: dict[str, Any] | None,
    runtime_target: dict[str, Any] | None,
    model_id: str | None,
    slot: str | None,
    rollout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    package_id = str((package or {}).get("package_id") or "")
    device_id = str((device or {}).get("device_id") or "")
    rollout_id = str((rollout or {}).get("rollout_id") or "")
    rollout_state = str((rollout or {}).get("state") or "")
    drift_sensitive = rollout_state in {"imported", "activated"}
    refs = {
        "package_id": package_id,
        "model_id": model_id,
        "device_id": device_id,
        "rollout_id": rollout_id,
        "rollout_state": rollout_state,
    }
    if package is None or device is None or not model_id:
        return _readiness_gate(
            "resource_envelope",
            "Resource envelope",
            "go",
            "not scoped",
            "Resource envelope waits for a selected model and edge target",
            refs=refs,
        )

    freshness = _device_telemetry_freshness(device)
    fit = _resource_envelope_summary(package=package, device=device, model_id=model_id)
    fit_refs = {
        **refs,
        **_resource_envelope_refs(fit),
        **_device_telemetry_refs(freshness),
    }
    if drift_sensitive and fit["status"] != "go":
        fit_refs["resource_drift"] = True
    if fit["status"] == "go":
        if freshness["status"] != "go":
            state = "drift unverified" if drift_sensitive else "telemetry stale"
            detail = (
                f"Active rollout {rollout_id or 'selected rollout'} cannot prove its "
                f"resource envelope from stale edge telemetry: {freshness['detail']}"
                if drift_sensitive
                else f"Resource envelope was last proven from stale edge telemetry: {freshness['detail']}"
            )
            return _readiness_gate(
                "resource_envelope",
                "Resource envelope",
                "attention",
                state,
                detail,
                refs=fit_refs,
                actions=[
                    _readiness_action(
                        "refresh_resource_inventory",
                        "Refresh edge inventory",
                        "refresh_edge_inventory",
                        refs=fit_refs,
                    )
                ],
            )
        return _readiness_gate(
            "resource_envelope",
            "Resource envelope",
            "go",
            str(fit.get("state") or "met"),
            str(fit.get("detail") or "Device resource telemetry satisfies model envelope"),
            refs=fit_refs,
        )
    if drift_sensitive and fit["status"] == "blocked":
        fallback_action = _fallback_model_readiness_action(
            data,
            package=package,
            device=device,
            runtime_target=runtime_target,
            current_model_id=model_id,
            slot=slot,
            drift_kind="resource",
        )
        actions = [
            action
            for action in [
                fallback_action,
                _readiness_action(
                    "rollback_resource_drift",
                    "Rollback active rollout",
                    "rollback_rollout",
                    refs=fit_refs,
                ),
                _readiness_action(
                    "refresh_resource_inventory",
                    "Refresh edge inventory",
                    "refresh_edge_inventory",
                    refs=fit_refs,
                ),
            ]
            if action is not None
        ]
        return _readiness_gate(
            "resource_envelope",
            "Resource envelope",
            "blocked",
            "resource drift",
            (
                f"Active rollout {rollout_id or 'selected rollout'} no longer satisfies "
                f"the model resource envelope: {fit.get('detail')}"
            ),
            refs=fit_refs,
            actions=actions,
        )
    status = "blocked" if fit["status"] == "blocked" else "attention"
    state = str(fit.get("state") or "needs telemetry")
    detail = str(fit.get("detail") or "Resource telemetry is incomplete for this model/device pair")
    if drift_sensitive and status == "attention":
        state = "drift unverified"
        detail = (
            f"Active rollout {rollout_id or 'selected rollout'} cannot prove its "
            f"resource envelope from current edge telemetry: {detail}"
        )
    return _readiness_gate(
        "resource_envelope",
        "Resource envelope",
        status,
        state,
        detail,
        refs=fit_refs,
        actions=[
            _readiness_action(
                "refresh_resource_inventory",
                "Refresh edge inventory",
                "refresh_edge_inventory",
                refs=fit_refs,
            )
        ] if status == "attention" else [],
    )


def _device_telemetry_freshness(device: dict[str, Any]) -> dict[str, Any]:
    last_seen_at = str(device.get("last_seen_at") or "")
    stale_after_seconds = READINESS_HEARTBEAT_STALE_SECONDS
    last_seen = _parse_hub_timestamp(last_seen_at)
    checked_at = _parse_hub_timestamp(_now())
    if last_seen is None or checked_at is None:
        return {
            "status": "attention",
            "state": "telemetry unknown",
            "detail": "edge heartbeat timestamp is missing or invalid",
            "last_seen_at": last_seen_at,
            "heartbeat_stale_after_seconds": stale_after_seconds,
        }

    age_seconds = max(0, int((checked_at - last_seen).total_seconds()))
    if age_seconds > stale_after_seconds:
        return {
            "status": "attention",
            "state": "telemetry stale",
            "detail": (
                f"last heartbeat was {_format_seconds(age_seconds)} ago; "
                f"freshness budget is {_format_seconds(stale_after_seconds)}"
            ),
            "last_seen_at": last_seen_at,
            "heartbeat_age_seconds": age_seconds,
            "heartbeat_stale_after_seconds": stale_after_seconds,
        }
    return {
        "status": "go",
        "state": "telemetry fresh",
        "detail": f"last heartbeat was {_format_seconds(age_seconds)} ago",
        "last_seen_at": last_seen_at,
        "heartbeat_age_seconds": age_seconds,
        "heartbeat_stale_after_seconds": stale_after_seconds,
    }


def _device_telemetry_refs(freshness: dict[str, Any]) -> dict[str, Any]:
    return _readiness_refs(
        {
            "telemetry_state": freshness.get("state"),
            "last_seen_at": freshness.get("last_seen_at"),
            "heartbeat_age_seconds": freshness.get("heartbeat_age_seconds"),
            "heartbeat_stale_after_seconds": freshness.get(
                "heartbeat_stale_after_seconds"
            ),
        }
    )


def _edge_target_readiness_gate(device: dict[str, Any] | None) -> dict[str, Any]:
    if device is None:
        return _readiness_gate(
            "edge_target",
            "Edge target",
            "blocked",
            "missing",
            "Enroll an edge node or connect a simulated device",
            actions=[
                _readiness_action(
                    "enroll_edge_target",
                    "Enroll edge target",
                    "enroll_device",
                )
            ],
        )
    device_id = str(device.get("device_id") or device.get("id") or "")
    profile = normalize_device_profile(device.get("profile")) or "unknown"
    state = str(device.get("status") or "registered")
    freshness = _device_telemetry_freshness(device)
    refs = {"device_id": device_id, **_device_telemetry_refs(freshness)}
    if state != "offline" and freshness["status"] != "go":
        return _readiness_gate(
            "edge_target",
            "Edge target",
            "attention",
            str(freshness.get("state") or "telemetry stale"),
            f"{device_id} reports profile {profile}, but {freshness['detail']}",
            refs=refs,
            actions=[
                _readiness_action(
                    "refresh_edge_inventory",
                    "Refresh edge inventory",
                    "refresh_edge_inventory",
                    refs=refs,
                )
            ],
        )
    status = "attention" if state == "offline" else "go"
    actions = (
        [
            _readiness_action(
                "restore_edge_connectivity",
                "Restore edge connectivity",
                "restore_connectivity",
                refs=refs,
            )
        ]
        if state == "offline"
        else None
    )
    return _readiness_gate(
        "edge_target",
        "Edge target",
        status,
        state,
        f"{device_id} reports profile {profile}",
        refs=refs,
        actions=actions,
    )


def _rollout_readiness_gate(
    rollout: dict[str, Any] | None,
    *,
    package_id: str | None,
    model_id: str | None,
    device_id: str | None,
    runtime_target_id: str | None,
    slot: str | None,
) -> dict[str, Any]:
    if rollout is None:
        rollout_refs = {
            "package_id": package_id,
            "model_id": model_id,
            "device_id": device_id,
            "runtime_target_id": runtime_target_id,
            "slot": slot,
            "require_approval": True,
        }
        plan_refs = {
            "package_id": package_id,
            "model_id": model_id,
            "device_ids": [device_id] if device_id else [],
            "runtime_target_id": runtime_target_id,
            "slot": slot,
            "batch_size": 1,
            "require_approval": True,
        }
        return _readiness_gate(
            "rollout_gate",
            "Rollout gate",
            "attention",
            "not assigned",
            "Create a rollout or staged rollout plan for the selected model",
            refs=rollout_refs,
            actions=[
                _readiness_action(
                    "create_rollout",
                    "Create rollout",
                    "create_rollout",
                    refs=rollout_refs,
                ),
                _readiness_action(
                    "create_rollout_plan",
                    "Create staged plan",
                    "create_rollout_plan",
                    refs=plan_refs,
                ),
            ],
        )
    rollout_id = str(rollout.get("rollout_id") or "")
    state = str(rollout.get("state") or "assigned")
    refs = {
        "rollout_id": rollout_id,
        "package_id": rollout.get("package_id"),
        "device_id": rollout.get("device_id"),
        "runtime_target_id": rollout.get("runtime_target_id"),
        "model_id": rollout.get("model_id"),
    }
    if state in {"failed", "blocked"}:
        return _readiness_gate(
            "rollout_gate",
            "Rollout gate",
            "blocked",
            state,
            "Inspect rollout failure before advancing this model",
            refs=refs,
            actions=[
                _readiness_action(
                    "inspect_rollout_failure",
                    "Inspect rollout failure",
                    "inspect_rollout",
                    refs=refs,
                )
            ],
        )
    approval = rollout.get("approval") if isinstance(rollout.get("approval"), dict) else {}
    if rollout.get("approval_required") and approval.get("approved") is not True:
        return _readiness_gate(
            "rollout_gate",
            "Rollout gate",
            "attention",
            "approval pending",
            "Approve the rollout policy before edge apply",
            refs=refs,
            actions=[
                _readiness_action(
                    "approve_rollout",
                    "Approve rollout",
                    "approve_rollout",
                    refs=refs,
                )
            ],
        )
    if state in {"activated", "rolled_back"}:
        return _readiness_gate(
            "rollout_gate",
            "Rollout gate",
            "go",
            state,
            "Latest rollout reached a terminal audited outcome",
            refs=refs,
        )
    return _readiness_gate(
        "rollout_gate",
        "Rollout gate",
        "attention",
        state,
        "Rollout can advance through apply",
        refs=refs,
        actions=[
            _readiness_action(
                "apply_rollout",
                "Apply rollout",
                "apply_rollout",
                refs=refs,
            )
        ],
    )


def _readiness_action(
    action_id: str,
    label: str,
    kind: str,
    *,
    refs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_refs = _readiness_refs(refs or {})
    action = {
        "action_id": action_id,
        "label": label,
        "kind": kind,
        "refs": action_refs,
    }
    command = _readiness_action_command(kind, action_refs)
    if command:
        action["command"] = command
    return action


def _readiness_gate(
    gate_id: str,
    label: str,
    status: str,
    state: str,
    detail: str,
    *,
    refs: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "label": label,
        "status": status,
        "state": state,
        "detail": detail,
        "refs": _readiness_refs(refs or {}),
        "actions": actions or [],
    }


def _fallback_model_readiness_action(
    data: dict[str, Any],
    *,
    package: dict[str, Any],
    device: dict[str, Any],
    runtime_target: dict[str, Any] | None,
    current_model_id: str,
    slot: str | None,
    drift_kind: str,
    allow_runtime_switch: bool = False,
) -> dict[str, Any] | None:
    candidate = _fallback_model_candidate(
        data,
        package=package,
        device=device,
        runtime_target=runtime_target,
        current_model_id=current_model_id,
        allow_runtime_switch=allow_runtime_switch,
    )
    if candidate is None:
        return None

    runtime_target_id = str(candidate.get("runtime_target_id") or "")
    runtime_validation = (
        _latest_passing_runtime_validation(
            data,
            package_id=str(package.get("package_id") or ""),
            runtime_target_id=runtime_target_id,
            package=package,
        )
        if runtime_target_id
        else None
    )

    refs = _readiness_refs(
        {
            "package_id": package.get("package_id"),
            "model_id": candidate["model_id"],
            "fallback_for_model_id": current_model_id,
            "device_id": device.get("device_id"),
            "runtime_target_id": runtime_target_id or None,
            "slot": slot,
            "require_approval": True,
            "require_runtime_validation": True if runtime_validation else None,
            "fallback_runtime_validation_id": (
                runtime_validation.get("validation_id") if runtime_validation else None
            ),
            "fallback_reason": f"{drift_kind} drift",
            "reason": f"readiness gate fallback for {drift_kind} drift",
            "fallback_performance_state": candidate.get("performance_state"),
            "fallback_resource_state": candidate.get("resource_state"),
            "fallback_benchmark_id": candidate.get("benchmark_id"),
            "fallback_latency_ms_p95": candidate.get("latency_ms_p95"),
            "fallback_throughput_ips": candidate.get("throughput_ips"),
            "fallback_runtime_target_id": candidate.get("runtime_target_id"),
        }
    )
    return _readiness_action(
        "stage_fallback_model",
        "Stage fallback model",
        "create_rollout",
        refs=refs,
    )


def _readiness_action_command(kind: str, refs: dict[str, Any]) -> dict[str, Any] | None:
    if kind == "promote_package" and refs.get("package_id"):
        return _readiness_command(
            "POST",
            f"/v1/hub/packages/{refs['package_id']}/promote",
            {
                "state": refs.get("target_state", "released"),
                "actor": READINESS_REMEDIATION_ACTOR,
                "reason": "readiness gate package promotion",
            },
        )
    if kind == "create_rollout":
        return _readiness_command(
            "POST",
            "/v1/hub/rollouts",
            _readiness_refs(
                {
                    "rollout_id": _readiness_command_id(
                        "rollout",
                        refs,
                        [
                            "package_id",
                            "model_id",
                            "device_id",
                            "runtime_target_id",
                            "slot",
                        ],
                    ),
                    "package_id": refs.get("package_id"),
                    "model_id": refs.get("model_id"),
                    "device_id": refs.get("device_id"),
                    "runtime_target_id": refs.get("runtime_target_id"),
                    "slot": refs.get("slot"),
                    "require_approval": refs.get("require_approval"),
                    "require_runtime_validation": refs.get("require_runtime_validation"),
                    "actor": READINESS_REMEDIATION_ACTOR,
                    "reason": refs.get("reason", "readiness gate rollout assignment"),
                }
            ),
        )
    if kind == "create_rollout_plan":
        return _readiness_command(
            "POST",
            "/v1/hub/rollout-plans",
            _readiness_refs(
                {
                    "plan_id": _readiness_command_id(
                        "plan",
                        refs,
                        [
                            "package_id",
                            "model_id",
                            "device_ids",
                            "runtime_target_id",
                            "slot",
                        ],
                    ),
                    "package_id": refs.get("package_id"),
                    "model_id": refs.get("model_id"),
                    "device_ids": refs.get("device_ids"),
                    "runtime_target_id": refs.get("runtime_target_id"),
                    "slot": refs.get("slot"),
                    "batch_size": refs.get("batch_size", 1),
                    "require_approval": refs.get("require_approval"),
                    "actor": READINESS_REMEDIATION_ACTOR,
                    "reason": "readiness gate staged rollout plan",
                }
            ),
        )
    if kind == "approve_rollout" and refs.get("rollout_id"):
        return _readiness_command(
            "POST",
            f"/v1/hub/rollouts/{refs['rollout_id']}/approve",
            {
                "actor": READINESS_REMEDIATION_ACTOR,
                "reason": "readiness gate approval",
            },
        )
    if kind == "apply_rollout" and refs.get("rollout_id"):
        return _readiness_command(
            "POST",
            f"/v1/hub/rollouts/{refs['rollout_id']}/apply",
            _readiness_refs(
                {
                    "model_id": refs.get("model_id"),
                    "actor": READINESS_REMEDIATION_ACTOR,
                }
            ),
        )
    if kind == "rollback_rollout" and refs.get("rollout_id"):
        if refs.get("performance_drift") is True:
            reason = "readiness gate performance drift"
        elif refs.get("runtime_drift") is True:
            reason = "readiness gate runtime drift"
        else:
            reason = "readiness gate resource drift"
        return _readiness_command(
            "POST",
            f"/v1/hub/rollouts/{refs['rollout_id']}/rollback",
            {
                "actor": READINESS_REMEDIATION_ACTOR,
                "reason": reason,
            },
        )
    if kind == "record_benchmark":
        edge_command = _readiness_benchmark_edge_command(refs)
        return _readiness_command(
            "POST",
            "/v1/hub/benchmarks",
            _readiness_refs(
                {
                    "device_id": refs.get("device_id"),
                    "package_id": refs.get("package_id"),
                    "runtime_target_id": refs.get("runtime_target_id"),
                    "actor": "edge-agent",
                    "result": _readiness_refs(
                        {
                            "schema_version": "temms-benchmark/v1",
                            "model_id": refs.get("model_id"),
                            "slot": refs.get("slot"),
                        }
                    ),
                }
            ),
            extra=_readiness_refs(
                {
                    "requires_edge_execution": True,
                    "edge_command": edge_command,
                    "edge_command_text": shlex.join(edge_command) if edge_command else None,
                    "edge_command_note": (
                        "Run on the selected edge after the model package is cached; "
                        "the central API body is only the target envelope for the published result."
                    ),
                }
            ),
        )
    if kind == "inspect_rollout":
        return _readiness_command("GET", "/v1/hub/rollouts")
    return None


def _readiness_benchmark_edge_command(refs: dict[str, Any]) -> list[str]:
    model_id = str(refs.get("model_id") or "")
    if not model_id:
        return []
    command = [
        "temms",
        "benchmark",
        model_id,
        "--slot",
        str(refs.get("slot") or "vision"),
        "--samples",
        str(refs.get("benchmark_samples") or 10),
        "--warmup",
        str(refs.get("benchmark_warmup") or 2),
        "--hub-url",
        "${TEMMS_HUB_URL}",
    ]
    if refs.get("device_id"):
        command.extend(["--device-id", str(refs["device_id"])])
    if refs.get("package_id"):
        command.extend(["--package-id", str(refs["package_id"])])
    if refs.get("runtime_target_id"):
        command.extend(["--runtime-target-id", str(refs["runtime_target_id"])])
    command.extend(["--actor", "edge-agent"])
    return command


def _readiness_command(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command: dict[str, Any] = {"method": method, "path": path}
    if body is not None:
        command["body"] = _readiness_refs(body)
    if extra is not None:
        command.update(_readiness_refs(extra))
    return command


def _readiness_command_id(
    kind: str,
    refs: dict[str, Any],
    keys: list[str],
) -> str:
    payload = _readiness_refs({key: refs.get(key) for key in keys})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    return f"{READINESS_REMEDIATION_ID_PREFIX}-{kind}-{digest}"


def _readiness_refs(refs: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in refs.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _finalize_deployment_readiness(
    gates: list[dict[str, Any]],
    *,
    checked_at: str,
    selection: dict[str, Any],
    runtime_fit: dict[str, Any] | None = None,
    production_admission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = {
        "go": sum(1 for gate in gates if gate.get("status") == "go"),
        "attention": sum(1 for gate in gates if gate.get("status") == "attention"),
        "blocked": sum(1 for gate in gates if gate.get("status") == "blocked"),
    }
    blocker = next((gate for gate in gates if gate.get("status") == "blocked"), None)
    warning = next((gate for gate in gates if gate.get("status") == "attention"), None)
    if blocker is not None:
        status = "blocked"
        headline = "Deployment is blocked"
        detail = "One or more safety gates prevent field rollout for the selected model."
        next_action = str(blocker.get("detail") or "Resolve the blocked gate")
    elif warning is not None:
        status = "attention"
        headline = "Deployment is stageable with operator action"
        detail = "The selected model has no hard blockers, but one runtime, resource, performance, or proof gate still needs review."
        next_action = str(warning.get("detail") or "Review the attention gate")
    else:
        status = "go"
        headline = "Deployment loop is ready"
        detail = "Model package, runtime target, performance SLO, resource envelope, edge target, rollout, DDIL queue, and evidence chain are aligned."
        next_action = "Export mission replay or stage the next rollout batch"
    readiness = {
        "schema_version": "temms-deployment-readiness/v1",
        "status": status,
        "headline": headline,
        "detail": detail,
        "next_action": next_action,
        "checked_at": checked_at,
        "selection": selection,
        "summary": summary,
        "gates": gates,
        "actions": _readiness_actions(gates),
    }
    if runtime_fit is not None:
        readiness["runtime_fit"] = runtime_fit
    if production_admission is not None:
        readiness["production_admission"] = production_admission
    runtime_decision = _runtime_decision_evidence(
        gates,
        checked_at=checked_at,
        selection=selection,
        readiness_status=status,
        runtime_fit=runtime_fit,
        production_admission=production_admission,
    )
    if runtime_decision:
        readiness["runtime_decision"] = runtime_decision
    edge_execution_contract = _edge_execution_contract(
        gates,
        checked_at=checked_at,
        selection=selection,
        readiness_status=status,
        next_action=next_action,
        runtime_fit=runtime_fit,
        production_admission=production_admission,
        runtime_decision=runtime_decision,
    )
    if edge_execution_contract:
        readiness["edge_execution_contract"] = edge_execution_contract
    runtime_workbench = _runtime_workbench_contract(
        checked_at=checked_at,
        selection=selection,
        readiness_status=status,
        runtime_fit=runtime_fit,
        production_admission=production_admission,
        runtime_decision=runtime_decision,
        edge_execution_contract=edge_execution_contract,
    )
    if runtime_workbench:
        readiness["runtime_workbench"] = runtime_workbench
    readiness["edge_runtime_mission"] = _edge_runtime_mission_summary(
        status=status,
        headline=headline,
        next_action=next_action,
        checked_at=checked_at,
        selection=selection,
        gates=gates,
        runtime_fit=runtime_fit,
        production_admission=production_admission,
        runtime_decision=runtime_decision,
    )
    return readiness


def _runtime_workbench_contract(
    *,
    checked_at: str,
    selection: dict[str, Any],
    readiness_status: str,
    runtime_fit: dict[str, Any] | None,
    production_admission: dict[str, Any] | None,
    runtime_decision: dict[str, Any] | None,
    edge_execution_contract: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the canonical ranked runtime workbench for one model/edge path."""
    runtime_fit = runtime_fit if isinstance(runtime_fit, dict) else {}
    runtime_decision = runtime_decision if isinstance(runtime_decision, dict) else {}
    edge_execution_contract = (
        edge_execution_contract if isinstance(edge_execution_contract, dict) else {}
    )
    if not runtime_fit:
        return {}

    production_admission = (
        production_admission if isinstance(production_admission, dict) else {}
    )
    target_selection = (
        runtime_decision.get("target_selection")
        if isinstance(runtime_decision.get("target_selection"), dict)
        else runtime_fit.get("target_selection")
        if isinstance(runtime_fit.get("target_selection"), dict)
        else {}
    )
    selected_runtime_target_id = (
        target_selection.get("selected_runtime_target_id")
        or selection.get("runtime_target_id")
        or runtime_fit.get("runtime_target_id")
    )
    best_runtime_target_id = target_selection.get("best_runtime_target_id")
    raw_assessments = (
        runtime_decision.get("target_assessments")
        if isinstance(runtime_decision.get("target_assessments"), list)
        else edge_execution_contract.get("target_assessments")
        if isinstance(edge_execution_contract.get("target_assessments"), list)
        else target_selection.get("target_assessments")
        if isinstance(target_selection.get("target_assessments"), list)
        else []
    )
    targets = [
        _runtime_workbench_target(
            assessment,
            selected_runtime_target_id=str(selected_runtime_target_id or ""),
            best_runtime_target_id=str(best_runtime_target_id or ""),
        )
        for assessment in raw_assessments
        if isinstance(assessment, dict)
    ]
    if not targets and selected_runtime_target_id:
        targets = [
            _runtime_workbench_target(
                {
                    "runtime_target_id": selected_runtime_target_id,
                    "score": runtime_fit.get("score"),
                    "tier": runtime_fit.get("tier"),
                    "detail": runtime_fit.get("detail"),
                    "status": (
                        "blocked"
                        if runtime_fit.get("tier") == "blocked"
                        else "eligible"
                    ),
                    "runtime_lane": runtime_fit.get("runtime_lane"),
                    "artifact_lane": runtime_fit.get("artifact_lane"),
                    "runtime_capability_lock": runtime_fit.get(
                        "runtime_capability_lock"
                    ),
                    "component_states": _runtime_target_component_states(
                        runtime_fit
                    ),
                },
                selected_runtime_target_id=str(selected_runtime_target_id),
                best_runtime_target_id=str(
                    best_runtime_target_id or selected_runtime_target_id
                ),
            )
        ]

    selected_target = next((target for target in targets if target.get("selected")), None)
    best_target = next((target for target in targets if target.get("best")), None)
    blocked_count = sum(1 for target in targets if target.get("status") == "blocked")
    eligible_count = sum(1 for target in targets if target.get("eligible") is True)
    return _readiness_refs(
        {
            "schema_version": RUNTIME_WORKBENCH_SCHEMA_VERSION,
            "checked_at": checked_at,
            "status": readiness_status,
            "recommended_action": runtime_decision.get("recommended_action"),
            "detail": target_selection.get("detail") or runtime_fit.get("detail"),
            "selection": _readiness_refs(selection),
            "selected_runtime_target_id": selected_runtime_target_id,
            "best_runtime_target_id": best_runtime_target_id,
            "target_selection": _readiness_refs(
                {
                    "schema_version": target_selection.get("schema_version"),
                    "status": target_selection.get("status"),
                    "selected_rank": target_selection.get("selected_rank"),
                    "selected_score": target_selection.get("selected_score"),
                    "best_score": target_selection.get("best_score"),
                    "score_delta": target_selection.get("score_delta"),
                    "candidate_count": target_selection.get("candidate_count"),
                    "eligible_target_count": target_selection.get(
                        "eligible_target_count"
                    ),
                }
            ),
            "summary": _readiness_refs(
                {
                    "target_count": len(targets),
                    "eligible_target_count": eligible_count,
                    "blocked_target_count": blocked_count,
                    "selected_is_best": (
                        bool(selected_runtime_target_id and best_runtime_target_id)
                        and selected_runtime_target_id == best_runtime_target_id
                    ),
                    "production_apply_allowed": production_admission.get(
                        "apply_allowed"
                    ),
                }
            ),
            "selected_target": selected_target,
            "best_target": best_target,
            "targets": targets,
            "proof_policy": {
                "require_go": True,
                "min_runtime_fit": 95,
                "require_best_runtime": True,
                "require_capability_lock": True,
                "require_proof_signature": True,
            },
            "production_admission": _readiness_refs(
                {
                    "status": production_admission.get("status"),
                    "apply_allowed": production_admission.get("apply_allowed"),
                    "detail": production_admission.get("detail"),
                    "blocking_gate_count": production_admission.get(
                        "blocking_gate_count"
                    ),
                }
            ),
        }
    )


def _runtime_workbench_target(
    assessment: dict[str, Any],
    *,
    selected_runtime_target_id: str,
    best_runtime_target_id: str,
) -> dict[str, Any]:
    runtime_target_id = str(assessment.get("runtime_target_id") or "")
    if not runtime_target_id:
        return {}
    runtime_lane = (
        assessment.get("runtime_lane")
        if isinstance(assessment.get("runtime_lane"), dict)
        else {}
    )
    runtime_target_ref = (
        assessment.get("runtime_target")
        if isinstance(assessment.get("runtime_target"), dict)
        else {}
    )
    artifact_lane = (
        assessment.get("artifact_lane")
        if isinstance(assessment.get("artifact_lane"), dict)
        else {}
    )
    component_states = (
        assessment.get("component_states")
        if isinstance(assessment.get("component_states"), dict)
        else {}
    )
    capability_lock = (
        assessment.get("runtime_capability_lock")
        if isinstance(assessment.get("runtime_capability_lock"), dict)
        else {}
    )
    remediation = (
        assessment.get("remediation")
        if isinstance(assessment.get("remediation"), dict)
        else {}
    )
    status = str(
        assessment.get("status")
        or ("blocked" if assessment.get("blocked") is True else "eligible")
    )
    selected = runtime_target_id == selected_runtime_target_id
    best = runtime_target_id == best_runtime_target_id
    return _readiness_refs(
        {
            "runtime_target_id": runtime_target_id,
            "rank": assessment.get("rank"),
            "status": status,
            "eligible": False if status == "blocked" else assessment.get("eligible", True),
            "selected": selected,
            "best": best,
            "score": assessment.get("score"),
            "tier": assessment.get("tier"),
            "detail": assessment.get("detail"),
            "runtime_target": _readiness_refs(runtime_target_ref),
            "runtime_lane": _runtime_decision_lane(runtime_lane),
            "artifact_lane": _readiness_refs(
                {
                    "status": artifact_lane.get("status"),
                    "state": artifact_lane.get("state"),
                    "detail": artifact_lane.get("detail"),
                    "model_format": artifact_lane.get("model_format"),
                    "lane_id": artifact_lane.get("lane_id"),
                }
            ),
            "proof": _runtime_workbench_target_proof(
                assessment,
                component_states=component_states,
                capability_lock=capability_lock,
            ),
            "component_states": component_states,
            "reasons": assessment.get("reasons"),
            "penalties": assessment.get("penalties"),
            "remediation": remediation,
            "action": _readiness_refs(
                {
                    "label": remediation.get("label"),
                    "kind": remediation.get("action"),
                    "requires_edge_execution": remediation.get(
                        "requires_edge_execution"
                    ),
                    "command": remediation.get("command"),
                }
            ),
        }
    )


def _runtime_workbench_target_proof(
    assessment: dict[str, Any],
    *,
    component_states: dict[str, Any],
    capability_lock: dict[str, Any],
) -> dict[str, Any]:
    validation = (
        component_states.get("runtime_validation")
        if isinstance(component_states.get("runtime_validation"), dict)
        else {}
    )
    performance = (
        component_states.get("performance")
        if isinstance(component_states.get("performance"), dict)
        else {}
    )
    resource = (
        component_states.get("resource")
        if isinstance(component_states.get("resource"), dict)
        else {}
    )
    telemetry = (
        component_states.get("telemetry")
        if isinstance(component_states.get("telemetry"), dict)
        else {}
    )
    return _readiness_refs(
        {
            "runtime_validation_state": validation.get("state"),
            "runtime_validation_status": validation.get("status"),
            "validation_id": validation.get("validation_id"),
            "performance_state": performance.get("state"),
            "performance_status": performance.get("status"),
            "resource_state": resource.get("state"),
            "resource_status": resource.get("status"),
            "telemetry_state": telemetry.get("state"),
            "telemetry_status": telemetry.get("status"),
            "capability_lock_status": capability_lock.get("status"),
            "capability_sha256": capability_lock.get("capability_sha256"),
            "benchmark_id": assessment.get("benchmark_id"),
            "latency_ms_p95": assessment.get("latency_ms_p95"),
            "throughput_ips": assessment.get("throughput_ips"),
        }
    )


def _runtime_decision_trace_contract(
    runtime_workbench: dict[str, Any],
) -> dict[str, Any]:
    """Return a portable operator trace derived from the runtime workbench."""
    if runtime_workbench.get("schema_version") != RUNTIME_WORKBENCH_SCHEMA_VERSION:
        return {}
    targets = runtime_workbench.get("targets")
    if not isinstance(targets, list):
        return {}

    rows = [
        _runtime_decision_trace_row(target)
        for target in targets
        if isinstance(target, dict)
    ]
    rows = [row for row in rows if row.get("runtime_target_id")]
    if not rows:
        return {}

    selected_runtime_target_id = str(
        runtime_workbench.get("selected_runtime_target_id") or ""
    )
    best_runtime_target_id = str(runtime_workbench.get("best_runtime_target_id") or "")
    summary = (
        runtime_workbench.get("summary")
        if isinstance(runtime_workbench.get("summary"), dict)
        else {}
    )
    target_selection = (
        runtime_workbench.get("target_selection")
        if isinstance(runtime_workbench.get("target_selection"), dict)
        else {}
    )
    return _readiness_refs(
        {
            "schema_version": RUNTIME_DECISION_TRACE_SCHEMA_VERSION,
            "source_schema_version": runtime_workbench.get("schema_version"),
            "checked_at": runtime_workbench.get("checked_at"),
            "status": runtime_workbench.get("status"),
            "recommended_action": runtime_workbench.get("recommended_action"),
            "detail": runtime_workbench.get("detail"),
            "selected_runtime_target_id": selected_runtime_target_id,
            "best_runtime_target_id": best_runtime_target_id,
            "selected_is_best": summary.get("selected_is_best"),
            "target_count": summary.get("target_count", len(rows)),
            "eligible_target_count": summary.get(
                "eligible_target_count",
                sum(1 for row in rows if row.get("eligible") is True),
            ),
            "blocked_target_count": summary.get(
                "blocked_target_count",
                sum(1 for row in rows if row.get("status") == "blocked"),
            ),
            "target_selection_status": target_selection.get("status"),
            "selected_rank": target_selection.get("selected_rank"),
            "selected_score": target_selection.get("selected_score"),
            "best_score": target_selection.get("best_score"),
            "score_delta": target_selection.get("score_delta"),
            "rows": rows,
            "commands": [
                row["remediation_command"]
                for row in rows
                if isinstance(row.get("remediation_command"), dict)
            ],
        }
    )


def _runtime_decision_trace_row(target: dict[str, Any]) -> dict[str, Any]:
    runtime_target_id = str(target.get("runtime_target_id") or "")
    if not runtime_target_id:
        return {}
    proof = target.get("proof") if isinstance(target.get("proof"), dict) else {}
    remediation = (
        target.get("remediation") if isinstance(target.get("remediation"), dict) else {}
    )
    runtime_lane = (
        target.get("runtime_lane") if isinstance(target.get("runtime_lane"), dict) else {}
    )
    artifact_lane = (
        target.get("artifact_lane")
        if isinstance(target.get("artifact_lane"), dict)
        else {}
    )
    return _readiness_refs(
        {
            "runtime_target_id": runtime_target_id,
            "rank": target.get("rank"),
            "status": target.get("status"),
            "eligible": target.get("eligible"),
            "selected": target.get("selected") is True,
            "best": target.get("best") is True,
            "score": target.get("score"),
            "tier": target.get("tier"),
            "detail": target.get("detail"),
            "runtime_lane": _runtime_decision_lane(runtime_lane),
            "artifact_lane": _readiness_refs(
                {
                    "status": artifact_lane.get("status"),
                    "state": artifact_lane.get("state"),
                    "detail": artifact_lane.get("detail"),
                    "model_format": artifact_lane.get("model_format"),
                    "lane_id": artifact_lane.get("lane_id"),
                }
            ),
            "proof_components": _runtime_decision_trace_proof_components(proof),
            "capability_lock": _readiness_refs(
                {
                    "status": proof.get("capability_lock_status"),
                    "capability_sha256": proof.get("capability_sha256"),
                    "telemetry_state": proof.get("telemetry_state"),
                    "telemetry_status": proof.get("telemetry_status"),
                }
            ),
            "validation_id": proof.get("validation_id"),
            "benchmark_id": proof.get("benchmark_id"),
            "latency_ms_p95": proof.get("latency_ms_p95"),
            "throughput_ips": proof.get("throughput_ips"),
            "reasons": target.get("reasons") if isinstance(target.get("reasons"), list) else [],
            "penalties": target.get("penalties") if isinstance(target.get("penalties"), list) else [],
            "remediation": _readiness_refs(
                {
                    "action": remediation.get("action"),
                    "label": remediation.get("label"),
                    "detail": remediation.get("detail"),
                    "requires_edge_execution": remediation.get(
                        "requires_edge_execution"
                    ),
                }
            ),
            "remediation_command": _runtime_decision_trace_command(
                runtime_target_id,
                remediation,
            ),
        }
    )


def _runtime_decision_trace_proof_components(
    proof: dict[str, Any],
) -> dict[str, Any]:
    return _readiness_refs(
        {
            "runtime_validation": _readiness_refs(
                {
                    "status": proof.get("runtime_validation_status"),
                    "state": proof.get("runtime_validation_state"),
                    "evidence_id": proof.get("validation_id"),
                }
            ),
            "benchmark": _readiness_refs(
                {
                    "status": proof.get("performance_status"),
                    "state": proof.get("performance_state"),
                    "evidence_id": proof.get("benchmark_id"),
                    "latency_ms_p95": proof.get("latency_ms_p95"),
                    "throughput_ips": proof.get("throughput_ips"),
                }
            ),
            "resource": _readiness_refs(
                {
                    "status": proof.get("resource_status"),
                    "state": proof.get("resource_state"),
                }
            ),
            "telemetry": _readiness_refs(
                {
                    "status": proof.get("telemetry_status"),
                    "state": proof.get("telemetry_state"),
                }
            ),
            "capability_lock": _readiness_refs(
                {
                    "status": proof.get("capability_lock_status"),
                    "capability_sha256": proof.get("capability_sha256"),
                }
            ),
        }
    )


def _runtime_decision_trace_command(
    runtime_target_id: str,
    remediation: dict[str, Any],
) -> dict[str, Any]:
    if not remediation:
        return {}
    command_record = (
        remediation.get("command") if isinstance(remediation.get("command"), dict) else {}
    )
    edge_command_text = str(
        remediation.get("edge_command_text")
        or command_record.get("edge_command_text")
        or ""
    ).strip()
    operator_command_text = str(
        remediation.get("operator_command_text")
        or command_record.get("operator_command_text")
        or ""
    ).strip()
    edge_command = _runtime_decision_trace_command_text(
        remediation.get("edge_command") or command_record.get("edge_command")
    )
    operator_command = _runtime_decision_trace_command_text(
        remediation.get("operator_command") or command_record.get("operator_command")
    )
    command_text = edge_command_text or operator_command_text or edge_command or operator_command
    if not command_text:
        return {}
    kind = "edge" if edge_command_text or edge_command else "operator"
    return _readiness_refs(
        {
            "runtime_target_id": runtime_target_id,
            "action": remediation.get("action"),
            "label": remediation.get("label") or remediation.get("action"),
            "kind": kind,
            "requires_edge_execution": remediation.get("requires_edge_execution") is True,
            "command_text": command_text,
            "note": remediation.get(f"{kind}_command_note")
            or command_record.get(f"{kind}_command_note"),
        }
    )


def _runtime_decision_trace_command_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    command = [str(part) for part in value if part not in (None, "")]
    return shlex.join(command) if command else ""


def _edge_execution_contract(
    gates: list[dict[str, Any]],
    *,
    checked_at: str,
    selection: dict[str, Any],
    readiness_status: str,
    next_action: str,
    runtime_fit: dict[str, Any] | None,
    production_admission: dict[str, Any] | None,
    runtime_decision: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the portable contract for the selected model/runtime/edge path."""
    runtime_fit = runtime_fit if isinstance(runtime_fit, dict) else {}
    runtime_decision = runtime_decision if isinstance(runtime_decision, dict) else {}
    if not runtime_fit or not runtime_decision:
        return {}

    production_admission = (
        production_admission if isinstance(production_admission, dict) else {}
    )
    target_selection = (
        runtime_decision.get("target_selection")
        if isinstance(runtime_decision.get("target_selection"), dict)
        else runtime_fit.get("target_selection")
        if isinstance(runtime_fit.get("target_selection"), dict)
        else {}
    )
    selected_runtime_target_id = (
        target_selection.get("selected_runtime_target_id")
        or selection.get("runtime_target_id")
        or runtime_fit.get("runtime_target_id")
    )
    path = _readiness_refs(
        {
            "package_id": selection.get("package_id"),
            "model_id": selection.get("model_id"),
            "device_id": selection.get("device_id"),
            "runtime_target_id": selected_runtime_target_id,
            "slot": selection.get("slot"),
            "rollout_id": selection.get("rollout_id"),
            "label": _edge_runtime_path_label(
                {**selection, "runtime_target_id": selected_runtime_target_id}
            ),
        }
    )
    blocking_gates = [
        _runtime_decision_gate(gate)
        for gate in gates
        if isinstance(gate, dict) and gate.get("status") == "blocked"
    ]
    attention_gates = [
        _runtime_decision_gate(gate)
        for gate in gates
        if isinstance(gate, dict) and gate.get("status") == "attention"
    ]
    selected_lane = (
        runtime_decision.get("selected_runtime_lane")
        if isinstance(runtime_decision.get("selected_runtime_lane"), dict)
        else runtime_fit.get("runtime_lane")
        if isinstance(runtime_fit.get("runtime_lane"), dict)
        else {}
    )
    best_lane = (
        runtime_decision.get("best_runtime_lane")
        if isinstance(runtime_decision.get("best_runtime_lane"), dict)
        else {}
    )
    artifact_lane = (
        runtime_decision.get("artifact_lane")
        if isinstance(runtime_decision.get("artifact_lane"), dict)
        else runtime_fit.get("artifact_lane")
        if isinstance(runtime_fit.get("artifact_lane"), dict)
        else {}
    )
    capability_lock = (
        runtime_decision.get("runtime_capability_lock")
        if isinstance(runtime_decision.get("runtime_capability_lock"), dict)
        else runtime_fit.get("runtime_capability_lock")
        if isinstance(runtime_fit.get("runtime_capability_lock"), dict)
        else {}
    )
    return _readiness_refs(
        {
            "schema_version": EDGE_EXECUTION_CONTRACT_SCHEMA_VERSION,
            "checked_at": checked_at,
            "status": readiness_status,
            "path": path,
            "recommended_action": runtime_decision.get("recommended_action"),
            "detail": runtime_decision.get("detail") or runtime_fit.get("detail"),
            "next_action": next_action,
            "runtime_fit": _readiness_refs(
                {
                    "score": runtime_fit.get("score"),
                    "tier": runtime_fit.get("tier"),
                    "detail": runtime_fit.get("detail"),
                    "runtime_target_id": runtime_fit.get("runtime_target_id")
                    or selected_runtime_target_id,
                    "reasons": runtime_fit.get("reasons"),
                    "penalties": runtime_fit.get("penalties"),
                }
            ),
            "target_selection": _readiness_refs(
                {
                    "status": target_selection.get("status"),
                    "selected_runtime_target_id": selected_runtime_target_id,
                    "selected_rank": target_selection.get("selected_rank"),
                    "selected_score": target_selection.get("selected_score"),
                    "best_runtime_target_id": target_selection.get("best_runtime_target_id"),
                    "best_score": target_selection.get("best_score"),
                    "score_delta": target_selection.get("score_delta"),
                    "candidate_count": target_selection.get("candidate_count"),
                    "eligible_target_count": target_selection.get("eligible_target_count"),
                }
            ),
            "selected_runtime_lane": _runtime_decision_lane(selected_lane),
            "best_runtime_lane": _runtime_decision_lane(best_lane),
            "artifact_lane": _readiness_refs(
                {
                    "status": artifact_lane.get("status"),
                    "state": artifact_lane.get("state"),
                    "detail": artifact_lane.get("detail"),
                    "model_format": artifact_lane.get("model_format"),
                    "lane_id": artifact_lane.get("lane_id"),
                }
            ),
            "runtime_capability_lock": _runtime_capability_lock_summary(capability_lock),
            "production_admission": _readiness_refs(
                {
                    "status": production_admission.get("status"),
                    "apply_allowed": production_admission.get("apply_allowed"),
                    "detail": production_admission.get("detail"),
                    "blocking_gate_count": production_admission.get("blocking_gate_count"),
                }
            ),
            "gate_counts": {
                "blocking": len(blocking_gates),
                "attention": len(attention_gates),
            },
            "blocking_gates": blocking_gates,
            "attention_gates": attention_gates,
            "top_candidates": runtime_decision.get("top_candidates"),
            "target_assessments": runtime_decision.get("target_assessments")
            or target_selection.get("target_assessments"),
            "proof_policy": {
                "require_go": True,
                "min_runtime_fit": 95,
                "require_best_runtime": True,
                "require_capability_lock": True,
                "require_proof_signature": True,
            },
        }
    )


def _runtime_decision_evidence(
    gates: list[dict[str, Any]],
    *,
    checked_at: str,
    selection: dict[str, Any],
    readiness_status: str,
    runtime_fit: dict[str, Any] | None,
    production_admission: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a compact, portable proof of the selected runtime decision."""
    runtime_fit = runtime_fit if isinstance(runtime_fit, dict) else {}
    if not runtime_fit:
        return {}

    production_admission = (
        production_admission if isinstance(production_admission, dict) else {}
    )
    target_selection = (
        runtime_fit.get("target_selection")
        if isinstance(runtime_fit.get("target_selection"), dict)
        else {}
    )
    runtime_lane = (
        runtime_fit.get("runtime_lane")
        if isinstance(runtime_fit.get("runtime_lane"), dict)
        else {}
    )
    artifact_lane = (
        runtime_fit.get("artifact_lane")
        if isinstance(runtime_fit.get("artifact_lane"), dict)
        else {}
    )
    capability_lock = (
        runtime_fit.get("runtime_capability_lock")
        if isinstance(runtime_fit.get("runtime_capability_lock"), dict)
        else {}
    )
    blocking_gates = [
        _runtime_decision_gate(gate)
        for gate in gates
        if isinstance(gate, dict) and gate.get("status") == "blocked"
    ]
    attention_gates = [
        _runtime_decision_gate(gate)
        for gate in gates
        if isinstance(gate, dict) and gate.get("status") == "attention"
    ]
    target_status = str(target_selection.get("status") or "")
    if target_status == "best" and production_admission.get("apply_allowed") is True:
        recommended_action = "apply_or_stage"
    elif target_status in {"upgrade_available", "selected_not_eligible"}:
        recommended_action = "use_best_runtime"
    elif blocking_gates:
        recommended_action = "resolve_blocking_gates"
    elif attention_gates:
        recommended_action = "collect_missing_evidence"
    else:
        recommended_action = "review"

    top_candidates = [
        _runtime_decision_candidate(candidate)
        for candidate in target_selection.get("alternatives", [])
        if isinstance(candidate, dict)
    ][:5]
    target_assessments = [
        _runtime_decision_target_assessment(candidate)
        for candidate in target_selection.get("target_assessments", [])
        if isinstance(candidate, dict)
    ]

    return _readiness_refs(
        {
            "schema_version": RUNTIME_DECISION_SCHEMA_VERSION,
            "checked_at": checked_at,
            "readiness_status": readiness_status,
            "recommended_action": recommended_action,
            "detail": target_selection.get("detail") or runtime_fit.get("detail"),
            "selected": _readiness_refs(selection),
            "runtime_fit": _readiness_refs(
                {
                    "score": runtime_fit.get("score"),
                    "tier": runtime_fit.get("tier"),
                    "detail": runtime_fit.get("detail"),
                    "reasons": runtime_fit.get("reasons"),
                    "penalties": runtime_fit.get("penalties"),
                }
            ),
            "target_selection": _readiness_refs(
                {
                    "status": target_selection.get("status"),
                    "selected_runtime_target_id": target_selection.get(
                        "selected_runtime_target_id"
                    )
                    or selection.get("runtime_target_id"),
                    "selected_rank": target_selection.get("selected_rank"),
                    "selected_score": target_selection.get("selected_score"),
                    "best_runtime_target_id": target_selection.get("best_runtime_target_id"),
                    "best_score": target_selection.get("best_score"),
                    "score_delta": target_selection.get("score_delta"),
                    "candidate_count": target_selection.get("candidate_count"),
                    "eligible_target_count": target_selection.get("eligible_target_count"),
                }
            ),
            "selected_runtime_lane": _runtime_decision_lane(runtime_lane),
            "best_runtime_lane": _runtime_decision_lane(
                target_selection.get("best_runtime_lane")
                if isinstance(target_selection.get("best_runtime_lane"), dict)
                else {}
            ),
            "artifact_lane": _readiness_refs(
                {
                    "status": artifact_lane.get("status"),
                    "state": artifact_lane.get("state"),
                    "detail": artifact_lane.get("detail"),
                    "model_format": artifact_lane.get("model_format"),
                    "lane_id": artifact_lane.get("lane_id"),
                }
            ),
            "runtime_capability_lock": _runtime_capability_lock_summary(capability_lock),
            "production_admission": _readiness_refs(
                {
                    "status": production_admission.get("status"),
                    "apply_allowed": production_admission.get("apply_allowed"),
                    "detail": production_admission.get("detail"),
                    "blocking_gate_count": production_admission.get("blocking_gate_count"),
                }
            ),
            "blocking_gates": blocking_gates,
            "attention_gates": attention_gates,
            "top_candidates": top_candidates,
            "target_assessments": target_assessments,
        }
    )


def _runtime_decision_gate(gate: dict[str, Any]) -> dict[str, Any]:
    return _readiness_refs(
        {
            "gate_id": gate.get("gate_id"),
            "label": gate.get("label"),
            "status": gate.get("status"),
            "state": gate.get("state"),
            "detail": gate.get("detail"),
            "refs": gate.get("refs") if isinstance(gate.get("refs"), dict) else None,
        }
    )


def _runtime_decision_lane(lane: dict[str, Any]) -> dict[str, Any]:
    return _readiness_refs(
        {
            "lane_id": lane.get("lane_id"),
            "label": lane.get("label"),
            "execution_engine": lane.get("execution_engine"),
            "acceleration": lane.get("acceleration"),
            "providers": lane.get("providers"),
            "accelerators": lane.get("accelerators"),
            "optimization_goal": lane.get("optimization_goal"),
        }
    )


def _runtime_decision_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    lane = candidate.get("runtime_lane") if isinstance(candidate.get("runtime_lane"), dict) else {}
    return _readiness_refs(
        {
            "rank": candidate.get("rank"),
            "runtime_target_id": candidate.get("runtime_target_id"),
            "runtime_target": candidate.get("runtime_target")
            if isinstance(candidate.get("runtime_target"), dict)
            else None,
            "score": candidate.get("score"),
            "tier": candidate.get("tier"),
            "blocked": candidate.get("blocked"),
            "latency_ms_p95": candidate.get("latency_ms_p95"),
            "throughput_ips": candidate.get("throughput_ips"),
            "benchmark_id": candidate.get("benchmark_id"),
            "runtime_lane": _runtime_decision_lane(lane),
        }
    )


def _runtime_decision_target_assessment(candidate: dict[str, Any]) -> dict[str, Any]:
    lane = candidate.get("runtime_lane") if isinstance(candidate.get("runtime_lane"), dict) else {}
    artifact_lane = (
        candidate.get("artifact_lane") if isinstance(candidate.get("artifact_lane"), dict) else {}
    )
    return _readiness_refs(
        {
            **_runtime_decision_candidate(candidate),
            "status": candidate.get("status"),
            "selected": True if candidate.get("selected") is True else None,
            "best": True if candidate.get("best") is True else None,
            "eligible": candidate.get("eligible"),
            "detail": candidate.get("detail"),
            "reasons": candidate.get("reasons"),
            "penalties": candidate.get("penalties"),
            "component_states": candidate.get("component_states"),
            "runtime_target": candidate.get("runtime_target")
            if isinstance(candidate.get("runtime_target"), dict)
            else None,
            "runtime_capability_lock": _runtime_capability_lock_summary(
                candidate.get("runtime_capability_lock")
                if isinstance(candidate.get("runtime_capability_lock"), dict)
                else {}
            ),
            "remediation": candidate.get("remediation")
            if isinstance(candidate.get("remediation"), dict)
            else None,
            "runtime_lane": _runtime_decision_lane(lane),
            "artifact_lane": _readiness_refs(
                {
                    "status": artifact_lane.get("status"),
                    "state": artifact_lane.get("state"),
                    "detail": artifact_lane.get("detail"),
                    "model_format": artifact_lane.get("model_format"),
                    "lane_id": artifact_lane.get("lane_id"),
                }
            ),
        }
    )


def _edge_execution_manifest(
    *,
    selection: dict[str, Any],
    edge_execution_contract: dict[str, Any],
    runtime_workbench: dict[str, Any],
    runtime_decision_trace: dict[str, Any],
    gate_policy: dict[str, Any],
    gate_status: str,
    gate_failures: list[str],
) -> dict[str, Any]:
    """Return the signed execution manifest for the selected edge runtime path."""
    selected_runtime_target_id = str(
        runtime_workbench.get("selected_runtime_target_id")
        or selection.get("runtime_target_id")
        or ""
    )
    selected_target = _runtime_workbench_selected_target(
        runtime_workbench,
        selected_runtime_target_id=selected_runtime_target_id,
    )
    if not selected_runtime_target_id and not selected_target:
        return {}

    contract_path = (
        edge_execution_contract.get("path")
        if isinstance(edge_execution_contract.get("path"), dict)
        else {}
    )
    path = _readiness_refs(
        {
            "package_id": selection.get("package_id") or contract_path.get("package_id"),
            "model_id": selection.get("model_id") or contract_path.get("model_id"),
            "device_id": selection.get("device_id") or contract_path.get("device_id"),
            "runtime_target_id": selected_runtime_target_id,
            "slot": selection.get("slot") or contract_path.get("slot"),
            "rollout_id": selection.get("rollout_id") or contract_path.get("rollout_id"),
            "label": _edge_runtime_path_label(
                {
                    **contract_path,
                    **selection,
                    "runtime_target_id": selected_runtime_target_id,
                }
            ),
        }
    )
    selected_proof = (
        selected_target.get("proof") if isinstance(selected_target.get("proof"), dict) else {}
    )
    selected_runtime_ref = (
        selected_target.get("runtime_target")
        if isinstance(selected_target.get("runtime_target"), dict)
        else {}
    )
    selected_runtime_lane = _manifest_source_dict(
        edge_execution_contract.get("selected_runtime_lane"),
        selected_target.get("runtime_lane"),
    )
    artifact_lane = _manifest_source_dict(
        edge_execution_contract.get("artifact_lane"),
        selected_target.get("artifact_lane"),
    )
    capability_lock = _manifest_source_dict(
        edge_execution_contract.get("runtime_capability_lock"),
        {
            "status": selected_proof.get("capability_lock_status"),
            "capability_sha256": selected_proof.get("capability_sha256"),
            "telemetry_state": selected_proof.get("telemetry_state"),
            "telemetry_status": selected_proof.get("telemetry_status"),
        },
    )
    target_selection = (
        edge_execution_contract.get("target_selection")
        if isinstance(edge_execution_contract.get("target_selection"), dict)
        else runtime_workbench.get("target_selection")
        if isinstance(runtime_workbench.get("target_selection"), dict)
        else {}
    )
    production_admission = (
        edge_execution_contract.get("production_admission")
        if isinstance(edge_execution_contract.get("production_admission"), dict)
        else runtime_workbench.get("production_admission")
        if isinstance(runtime_workbench.get("production_admission"), dict)
        else {}
    )
    selected_command = next(
        (
            command
            for command in runtime_decision_trace.get("commands", [])
            if isinstance(command, dict)
            and str(command.get("runtime_target_id") or "") == selected_runtime_target_id
        ),
        {},
    )
    workbench_summary = (
        runtime_workbench.get("summary")
        if isinstance(runtime_workbench.get("summary"), dict)
        else {}
    )
    return _readiness_refs(
        {
            "schema_version": EDGE_EXECUTION_MANIFEST_SCHEMA_VERSION,
            "checked_at": runtime_workbench.get("checked_at")
            or edge_execution_contract.get("checked_at"),
            "path": path,
            "model": _readiness_refs(
                {
                    "package_id": path.get("package_id"),
                    "model_id": path.get("model_id"),
                    "slot": path.get("slot"),
                    "artifact_format": artifact_lane.get("model_format"),
                    "artifact_state": artifact_lane.get("state"),
                    "artifact_lane_id": artifact_lane.get("lane_id"),
                    "artifact_detail": artifact_lane.get("detail"),
                }
            ),
            "execution": _readiness_refs(
                {
                    "runtime_target_id": selected_runtime_target_id,
                    "runtime_image": selected_runtime_ref.get("image"),
                    "runtime_registry": selected_runtime_ref.get("registry"),
                    "runtime_os": selected_runtime_ref.get("os"),
                    "runtime_arch": selected_runtime_ref.get("arch"),
                    "runtime_device_profiles": selected_runtime_ref.get("device_profiles"),
                    "runtime_lane": _runtime_decision_lane(selected_runtime_lane),
                    "target_status": selected_target.get("status"),
                    "target_score": selected_target.get("score"),
                    "target_tier": selected_target.get("tier"),
                    "selected_is_best": workbench_summary.get("selected_is_best"),
                    "best_runtime_target_id": runtime_workbench.get("best_runtime_target_id"),
                }
            ),
            "edge": _readiness_refs(
                {
                    "device_id": path.get("device_id"),
                    "capability_lock": _runtime_capability_lock_summary(capability_lock),
                    "telemetry": _readiness_refs(
                        {
                            "status": selected_proof.get("telemetry_status"),
                            "state": selected_proof.get("telemetry_state"),
                        }
                    ),
                }
            ),
            "evidence": _readiness_refs(
                {
                    "runtime_validation_id": selected_proof.get("validation_id"),
                    "benchmark_id": selected_proof.get("benchmark_id"),
                    "latency_ms_p95": selected_proof.get("latency_ms_p95"),
                    "throughput_ips": selected_proof.get("throughput_ips"),
                    "resource_status": selected_proof.get("resource_status"),
                    "resource_state": selected_proof.get("resource_state"),
                }
            ),
            "admission": _readiness_refs(
                {
                    "gate_status": gate_status,
                    "gate_policy": gate_policy,
                    "gate_failures": gate_failures,
                    "production_status": production_admission.get("status"),
                    "apply_allowed": production_admission.get("apply_allowed"),
                    "target_selection_status": target_selection.get("status"),
                    "recommended_action": edge_execution_contract.get(
                        "recommended_action"
                    )
                    or runtime_workbench.get("recommended_action"),
                }
            ),
            "selected_remediation_command": _readiness_refs(selected_command),
        }
    )


def _runtime_workbench_selected_target(
    runtime_workbench: dict[str, Any],
    *,
    selected_runtime_target_id: str,
) -> dict[str, Any]:
    selected_target = (
        runtime_workbench.get("selected_target")
        if isinstance(runtime_workbench.get("selected_target"), dict)
        else {}
    )
    if selected_target:
        return selected_target
    targets = runtime_workbench.get("targets")
    if not isinstance(targets, list):
        return {}
    for target in targets:
        if not isinstance(target, dict):
            continue
        if target.get("selected") is True:
            return target
        if selected_runtime_target_id and str(target.get("runtime_target_id") or "") == selected_runtime_target_id:
            return target
    return {}


def _manifest_source_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


EDGE_MISSION_PACKAGE_IDENTITY_COMPONENTS = (
    "mission",
    "selection",
    "slo",
    "model_handling",
    "ddil",
    "runtime_plan",
    "proof_gate",
)
EDGE_MISSION_PACKAGE_IDENTITY_TRANSIENT_KEYS = {
    "age_seconds",
    "checked_at",
    "created_at",
    "deployment_id",
    "heartbeat_age_seconds",
    "last_seen",
    "last_seen_at",
    "plan_id",
    "planned_at",
    "rollout_id",
    "rollout_plan_id",
    "updated_at",
}


def _edge_mission_package_identity_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _readiness_refs(
            {
                key: _edge_mission_package_identity_value(nested)
                for key, nested in value.items()
                if key not in EDGE_MISSION_PACKAGE_IDENTITY_TRANSIENT_KEYS
            }
        )
    if isinstance(value, list):
        return [
            _edge_mission_package_identity_value(item)
            for item in value
            if item not in (None, "", [], {})
        ]
    return value


def _edge_mission_package_identity_component(
    component_name: str,
    component: dict[str, Any],
) -> dict[str, Any]:
    if component_name != "runtime_plan":
        return _edge_mission_package_identity_value(component)

    target_selection = (
        component.get("target_selection")
        if isinstance(component.get("target_selection"), dict)
        else {}
    )
    capability_lock = (
        component.get("runtime_capability_lock")
        if isinstance(component.get("runtime_capability_lock"), dict)
        else {}
    )
    artifact_lane = (
        capability_lock.get("artifact_lane")
        if isinstance(capability_lock.get("artifact_lane"), dict)
        else {}
    )
    production_admission = (
        component.get("production_admission")
        if isinstance(component.get("production_admission"), dict)
        else {}
    )
    return _readiness_refs(
        {
            "status": component.get("status"),
            "runtime_target_id": component.get("runtime_target_id"),
            "runtime_fit_score": component.get("runtime_fit_score"),
            "runtime_fit_tier": component.get("runtime_fit_tier"),
            "target_selection": _readiness_refs(
                {
                    "schema_version": target_selection.get("schema_version"),
                    "status": target_selection.get("status"),
                    "selected_runtime_target_id": target_selection.get(
                        "selected_runtime_target_id"
                    ),
                    "best_runtime_target_id": target_selection.get(
                        "best_runtime_target_id"
                    ),
                    "selected_score": target_selection.get("selected_score"),
                    "best_score": target_selection.get("best_score"),
                    "score_delta": target_selection.get("score_delta"),
                    "selected_rank": target_selection.get("selected_rank"),
                    "eligible_target_count": target_selection.get(
                        "eligible_target_count"
                    ),
                    "candidate_count": target_selection.get("candidate_count"),
                }
            ),
            "runtime_capability_lock": _readiness_refs(
                {
                    "schema_version": capability_lock.get("schema_version"),
                    "status": capability_lock.get("status"),
                    "capability_sha256": capability_lock.get("capability_sha256"),
                    "runtime_target_id": capability_lock.get("runtime_target_id"),
                    "runtime_mode": capability_lock.get("runtime_mode"),
                    "artifact_lane": _edge_mission_package_identity_value(
                        artifact_lane
                    ),
                }
            ),
            "recommended_action": component.get("recommended_action"),
            "production_admission": _readiness_refs(
                {
                    "schema_version": production_admission.get("schema_version"),
                    "status": production_admission.get("status"),
                    "apply_allowed": production_admission.get("apply_allowed"),
                    "blocking_gate_count": production_admission.get(
                        "blocking_gate_count"
                    ),
                }
            ),
        }
    )


def edge_mission_package_identity_payload(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the stable mission/runtime package identity payload."""
    components: dict[str, Any] = {}
    for component_name in EDGE_MISSION_PACKAGE_IDENTITY_COMPONENTS:
        component = plan.get(component_name)
        if isinstance(component, dict) and component:
            components[component_name] = _edge_mission_package_identity_component(
                component_name,
                component,
            )
    return {
        "schema_version": EDGE_MISSION_PACKAGE_IDENTITY_SCHEMA_VERSION,
        "components": components,
    }


def edge_mission_package_identity_hash(plan: dict[str, Any]) -> str:
    """Return the stable identity hash shared by plan, download, and deploy intent."""
    return canonical_json_hash(edge_mission_package_identity_payload(plan))


def build_edge_mission_package_plan(
    readiness: dict[str, Any],
    mission_spec: dict[str, Any] | None = None,
    *,
    require_go: bool = True,
    min_runtime_fit: float | None = 95,
    require_best_runtime: bool = True,
    require_capability_lock: bool = True,
    require_proof_signature: bool = True,
) -> dict[str, Any]:
    """Build the mission-to-edge package plan from the readiness engine."""
    mission_spec = mission_spec or {}
    selection = (
        readiness.get("selection")
        if isinstance(readiness.get("selection"), dict)
        else {}
    )
    edge_runtime_mission = (
        readiness.get("edge_runtime_mission")
        if isinstance(readiness.get("edge_runtime_mission"), dict)
        else {}
    )
    runtime_fit = (
        readiness.get("runtime_fit")
        if isinstance(readiness.get("runtime_fit"), dict)
        else {}
    )
    runtime_decision = (
        readiness.get("runtime_decision")
        if isinstance(readiness.get("runtime_decision"), dict)
        else {}
    )
    edge_execution_contract = (
        readiness.get("edge_execution_contract")
        if isinstance(readiness.get("edge_execution_contract"), dict)
        else {}
    )
    runtime_workbench = (
        readiness.get("runtime_workbench")
        if isinstance(readiness.get("runtime_workbench"), dict)
        else {}
    )
    mission_payload = edge_runtime_mission or readiness
    gate_failures = edge_runtime_proof_gate_failures(
        "edge-runtime-mission",
        mission_payload,
        require_go=require_go,
        min_runtime_fit=min_runtime_fit,
        require_best_runtime=require_best_runtime,
        require_capability_lock=require_capability_lock,
        runtime_context=readiness,
    )
    gate_policy = _readiness_refs(
        {
            "require_go": require_go,
            "min_runtime_fit": min_runtime_fit,
            "require_best_runtime": require_best_runtime,
            "require_capability_lock": require_capability_lock,
            "require_proof_signature": require_proof_signature,
        }
    )
    yaml_source = str(
        mission_spec.get("mission_yaml")
        or mission_spec.get("yaml")
        or mission_spec.get("source_yaml")
        or ""
    )
    mission = _readiness_refs(
        {
            "goal": mission_spec.get("goal"),
            "sensor": mission_spec.get("sensor"),
            "slot": mission_spec.get("slot") or selection.get("slot"),
            "source": "yaml" if yaml_source else "operator_form",
            "source_yaml": yaml_source,
            "source_yaml_sha256": hashlib.sha256(yaml_source.encode("utf-8")).hexdigest()
            if yaml_source
            else None,
        }
    )
    target_selection = (
        runtime_fit.get("target_selection")
        if isinstance(runtime_fit.get("target_selection"), dict)
        else runtime_decision.get("target_selection")
        if isinstance(runtime_decision.get("target_selection"), dict)
        else edge_execution_contract.get("target_selection")
        if isinstance(edge_execution_contract.get("target_selection"), dict)
        else {}
    )
    capability_lock = _runtime_capability_lock_for_proof_gate(readiness)
    runtime_plan = _readiness_refs(
        {
            "status": readiness.get("status"),
            "runtime_target_id": selection.get("runtime_target_id"),
            "runtime_fit_score": runtime_fit.get("score"),
            "runtime_fit_tier": runtime_fit.get("tier"),
            "target_selection": target_selection,
            "runtime_capability_lock": _runtime_capability_lock_summary(capability_lock)
            if capability_lock
            else None,
            "recommended_action": edge_execution_contract.get("recommended_action")
            or runtime_decision.get("recommended_action"),
            "production_admission": readiness.get("production_admission"),
        }
    )
    selection_refs = _readiness_refs(selection)
    package_plan = {
        "schema_version": EDGE_MISSION_PACKAGE_SCHEMA_VERSION,
        "planned_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mission": mission,
        "selection": selection_refs,
        "slo": _readiness_refs(
            {
                "latency_budget_ms": _optional_float(
                    mission_spec.get("latency_budget_ms")
                ),
                "min_throughput_ips": _optional_float(
                    mission_spec.get("min_throughput_ips")
                    or mission_spec.get("throughput_min_ips")
                ),
            }
        ),
        "model_handling": _readiness_refs(
            {
                "switch_policy": mission_spec.get("switch_policy"),
                "confidence_threshold": _optional_float(
                    mission_spec.get("confidence_threshold")
                ),
                "fallback_model_id": mission_spec.get("fallback_model_id") or "auto",
            }
        ),
        "ddil": _readiness_refs(
            {
                "mode": mission_spec.get("ddil_mode") or "queue_signed_intents",
                "replay_requires_readiness": True,
                "proof_required": True,
            }
        ),
        "runtime_plan": runtime_plan,
        "proof_gate": {
            "status": "passed" if not gate_failures else "failed",
            "policy": gate_policy,
            "failures": gate_failures,
        },
        "readiness": _readiness_refs(
            {
                "schema_version": readiness.get("schema_version"),
                "status": readiness.get("status"),
                "headline": readiness.get("headline"),
                "next_action": readiness.get("next_action"),
                "checked_at": readiness.get("checked_at"),
            }
        ),
        "package": {
            "includes": [
                "mission_spec",
                "model_artifacts",
                "runtime_contract",
                "sensor_bindings",
                "model_switch_policy",
                "ddil_replay_policy",
                "edge_runtime_proof",
            ]
        },
    }
    package_identity_payload = edge_mission_package_identity_payload(package_plan)
    package_identity_components = sorted(
        package_identity_payload.get("components", {}).keys()
    )
    package_identity_sha256 = canonical_json_hash(package_identity_payload)
    package_plan["package_identity"] = {
        "schema_version": EDGE_MISSION_PACKAGE_IDENTITY_SCHEMA_VERSION,
        "package_identity_sha256": package_identity_sha256,
        "components": package_identity_components,
    }
    deployment_intent = _edge_mission_package_deployment_intent(
        selection_refs,
        mission_package_core_sha256=package_identity_sha256,
    )
    if deployment_intent:
        package_plan["deployment_intent"] = deployment_intent
        package_plan["edge_handoff"] = _edge_mission_package_edge_handoff(
            package_plan,
            deployment_intent,
            package_identity_sha256=package_identity_sha256,
        )
    if edge_execution_contract:
        package_plan["edge_execution_contract"] = edge_execution_contract
    if runtime_workbench:
        package_plan["runtime_workbench"] = runtime_workbench

    component_digests = edge_mission_package_component_digests(package_plan)
    if len(component_digests) > 1:
        package_plan["component_digests"] = component_digests
    package_plan["integrity"] = {
        "package_identity_sha256": package_identity_sha256,
        "payload_sha256": canonical_json_hash(package_plan),
    }
    return package_plan


def _edge_mission_package_deployment_intent(
    selection: dict[str, Any],
    *,
    mission_package_core_sha256: str,
) -> dict[str, Any]:
    refs = _readiness_refs(
        {
            "package_id": selection.get("package_id"),
            "model_id": selection.get("model_id"),
            "device_id": selection.get("device_id"),
            "runtime_target_id": selection.get("runtime_target_id"),
            "slot": selection.get("slot"),
        }
    )
    if not refs.get("package_id") or not refs.get("device_id"):
        return {}
    rollout_id = _readiness_command_id(
        "rollout",
        refs,
        ["package_id", "model_id", "device_id", "runtime_target_id", "slot"],
    )
    body = _readiness_refs(
        {
            "rollout_id": rollout_id,
            "package_id": refs.get("package_id"),
            "model_id": refs.get("model_id"),
            "device_id": refs.get("device_id"),
            "runtime_target_id": refs.get("runtime_target_id"),
            "slot": refs.get("slot"),
            "require_approval": True,
            "require_runtime_validation": True,
            "actor": READINESS_REMEDIATION_ACTOR,
            "reason": f"mission package deployment handoff {mission_package_core_sha256[:12]}",
        }
    )
    return {
        "schema_version": "temms-edge-deployment-intent/v1",
        "mode": "stage_rollout",
        "rollout_id": rollout_id,
        "package_identity_sha256": mission_package_core_sha256,
        "mission_package_core_sha256": mission_package_core_sha256,
        "requires": {
            "approval": True,
            "runtime_validation": True,
            "edge_readiness": True,
        },
        "command": {
            "method": "POST",
            "path": "/v1/hub/rollouts",
            "body": body,
        },
    }


def _edge_mission_package_edge_handoff(
    package_plan: dict[str, Any],
    deployment_intent: dict[str, Any],
    *,
    package_identity_sha256: str,
) -> dict[str, Any]:
    """Return the package-to-edge runbook embedded in the artifact."""
    selection = (
        package_plan.get("selection")
        if isinstance(package_plan.get("selection"), dict)
        else {}
    )
    proof_gate = (
        package_plan.get("proof_gate")
        if isinstance(package_plan.get("proof_gate"), dict)
        else {}
    )
    rollout_id = str(deployment_intent.get("rollout_id") or "")
    if not rollout_id:
        return {}
    return {
        "schema_version": "temms-edge-mission-package-handoff/v1",
        "mode": "stage_approve_apply",
        "package_identity_sha256": package_identity_sha256,
        "selection": _readiness_refs(selection),
        "stage_gate": {
            "proof_gate": "passed",
            "package_identity": "verified",
            "deployment_intent": "verified",
            "current_proof_gate_status": proof_gate.get("status"),
        },
        "artifact_integrity": {
            "package_identity_sha256": package_identity_sha256,
            "payload_digest_header": "X-TEMMS-Mission-Package-SHA256",
            "identity_digest_header": "X-TEMMS-Mission-Package-Identity-SHA256",
            "deployment_intent_digest_header": (
                "X-TEMMS-Mission-Package-Deployment-Intent-SHA256"
            ),
        },
        "commands": {
            "stage_package": {
                "method": "POST",
                "path": "/v1/hub/mission-package/stage",
                "body": {"mission_package": "<temms-edge-mission-package/v1>"},
            },
            "create_rollout_intent": deployment_intent.get("command"),
            "approve_rollout": {
                "method": "POST",
                "path": f"/v1/hub/rollouts/{rollout_id}/approve",
                "body": {"actor": READINESS_REMEDIATION_ACTOR},
            },
            "apply_rollout": {
                "method": "POST",
                "path": f"/v1/hub/rollouts/{rollout_id}/apply",
                "body": {"actor": READINESS_REMEDIATION_ACTOR},
            },
        },
        "sequence": [
            "verify package identity and payload digest",
            "stage package artifact through /v1/hub/mission-package/stage",
            "approve rollout policy gate when required",
            "apply rollout on the target edge runtime",
            "export evidence or replay DDIL queue after field operation",
        ],
    }


def build_edge_runtime_proof(
    readiness: dict[str, Any],
    *,
    source_action: str = "edge-runtime-mission",
    require_go: bool = False,
    min_runtime_fit: float | None = None,
    require_best_runtime: bool = False,
    require_capability_lock: bool = False,
    signing_key: str | None = None,
    signer: str = "temms",
) -> dict[str, Any]:
    """Build a portable, hash-verifiable proof for a selected edge runtime path."""
    if source_action not in {"readiness", "edge-runtime-mission"}:
        raise ValueError("source_action must be readiness or edge-runtime-mission")

    mission = (
        readiness.get("edge_runtime_mission")
        if isinstance(readiness.get("edge_runtime_mission"), dict)
        else {}
    )
    payload = readiness if source_action == "readiness" else mission
    selection = (
        readiness.get("selection")
        if isinstance(readiness.get("selection"), dict)
        else mission.get("path")
        if isinstance(mission.get("path"), dict)
        else {}
    )
    runtime_decision = (
        readiness.get("runtime_decision")
        if isinstance(readiness.get("runtime_decision"), dict)
        else {}
    )
    edge_execution_contract = (
        readiness.get("edge_execution_contract")
        if isinstance(readiness.get("edge_execution_contract"), dict)
        else {}
    )
    gate_failures = edge_runtime_proof_gate_failures(
        source_action,
        payload,
        require_go=require_go,
        min_runtime_fit=min_runtime_fit,
        require_best_runtime=require_best_runtime,
        require_capability_lock=require_capability_lock,
        runtime_context=readiness,
    )
    gate_policy: dict[str, Any] = {"require_go": require_go}
    if min_runtime_fit is not None:
        gate_policy["min_runtime_fit"] = min_runtime_fit
    if require_best_runtime:
        gate_policy["require_best_runtime"] = True
    if require_capability_lock:
        gate_policy["require_capability_lock"] = True
    proof = {
        "schema_version": EDGE_RUNTIME_PROOF_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_action": source_action,
        "gate_status": "passed" if not gate_failures else "failed",
        "gate_policy": gate_policy,
        "gate_failures": gate_failures,
        "status": str(payload.get("status") or "unknown"),
        "runtime_fit_score": edge_runtime_proof_runtime_fit_score(source_action, payload),
        "selection": selection,
        "edge_runtime_mission": mission,
        "readiness": readiness,
    }
    if runtime_decision:
        proof["runtime_decision"] = runtime_decision
    if edge_execution_contract:
        proof["edge_execution_contract"] = edge_execution_contract
    runtime_workbench = (
        readiness.get("runtime_workbench")
        if isinstance(readiness.get("runtime_workbench"), dict)
        else {}
    )
    if runtime_workbench:
        proof["runtime_workbench"] = runtime_workbench
        runtime_decision_trace = _runtime_decision_trace_contract(runtime_workbench)
        if runtime_decision_trace:
            proof["runtime_decision_trace"] = runtime_decision_trace
            edge_execution_manifest = _edge_execution_manifest(
                selection=selection,
                edge_execution_contract=edge_execution_contract,
                runtime_workbench=runtime_workbench,
                runtime_decision_trace=runtime_decision_trace,
                gate_policy=gate_policy,
                gate_status=str(proof["gate_status"]),
                gate_failures=gate_failures,
            )
            if edge_execution_manifest:
                proof["edge_execution_manifest"] = edge_execution_manifest
    component_digests = edge_runtime_proof_component_digests(proof)
    if len(component_digests) > 1:
        proof["component_digests"] = component_digests
    proof["integrity"] = {"payload_sha256": canonical_json_hash(proof)}
    if signing_key:
        sign_edge_runtime_proof(proof, signing_key, signer=signer)
    return proof


def edge_runtime_proof_gate_failures(
    action: str,
    payload: dict[str, Any],
    *,
    require_go: bool,
    min_runtime_fit: float | None,
    require_best_runtime: bool = False,
    require_capability_lock: bool = False,
    runtime_context: dict[str, Any] | None = None,
) -> list[str]:
    """Return gate failures used by edge-runtime proof generation/verification."""
    if action not in {"readiness", "edge-runtime-mission"}:
        return []

    failures: list[str] = []
    status = str(payload.get("status") or "unknown")
    if require_go and status != "go":
        failures.append(f"{action} status is {status}, expected go")

    if min_runtime_fit is not None:
        score = edge_runtime_proof_runtime_fit_score(action, payload)
        if score is None:
            failures.append("runtime fit score is missing")
        elif score < min_runtime_fit:
            failures.append(
                f"runtime fit score {score:g}/100 is below required {min_runtime_fit:g}/100"
            )
    if require_best_runtime:
        failures.extend(
            _runtime_target_best_gate_failures(runtime_context or payload or {})
        )
    if require_capability_lock:
        failures.extend(
            _runtime_capability_lock_gate_failures(runtime_context or payload or {})
        )
    return failures


def _runtime_target_best_gate_failures(runtime_context: dict[str, Any]) -> list[str]:
    target_selection = _runtime_target_selection_for_proof_gate(runtime_context)
    if not target_selection:
        return ["runtime target selection proof is missing"]

    status = str(target_selection.get("status") or "").lower()
    selected = str(target_selection.get("selected_runtime_target_id") or "")
    best = str(target_selection.get("best_runtime_target_id") or "")
    score_delta = _optional_float(target_selection.get("score_delta"))
    selected_is_best = bool(selected and best and selected == best)
    if (
        status == "best"
        and (not selected or not best or selected_is_best)
        and (score_delta is None or score_delta <= 0)
    ):
        return []
    if selected_is_best and (score_delta is None or score_delta <= 0):
        return []
    if selected and best and selected != best:
        return [
            f"selected runtime target {selected} is not best measured target {best}"
        ]
    if score_delta is not None and score_delta > 0:
        return [
            f"selected runtime target trails best measured target by {score_delta:g} points"
        ]
    if status:
        return [f"runtime target selection status is {status}, expected best"]
    return ["runtime target selection proof is missing best-runtime status"]


def _runtime_target_selection_for_proof_gate(
    runtime_context: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(runtime_context, dict):
        return {}
    readiness = (
        runtime_context.get("readiness")
        if isinstance(runtime_context.get("readiness"), dict)
        else {}
    )
    for source in (
        runtime_context,
        runtime_context.get("edge_execution_contract"),
        runtime_context.get("runtime_decision"),
        runtime_context.get("runtime_fit"),
        readiness.get("edge_execution_contract"),
        readiness.get("runtime_decision"),
        readiness.get("runtime_fit"),
    ):
        if not isinstance(source, dict):
            continue
        target_selection = source.get("target_selection")
        if isinstance(target_selection, dict) and target_selection:
            return target_selection
    return {}


def _runtime_capability_lock_gate_failures(runtime_context: dict[str, Any]) -> list[str]:
    lock = _runtime_capability_lock_for_proof_gate(runtime_context)
    if not lock:
        return ["runtime capability lock proof is missing"]

    status = str(lock.get("status") or "").lower()
    digest = str(lock.get("capability_sha256") or "")
    raw_failures = lock.get("failures") if isinstance(lock.get("failures"), list) else []
    lock_failures = [str(failure) for failure in raw_failures if failure]
    failures: list[str] = []
    if status != "locked":
        failures.append(f"runtime capability lock status is {status or 'missing'}, expected locked")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        failures.append("runtime capability lock capability_sha256 is missing or invalid")
    if lock_failures:
        failures.append(
            "runtime capability lock has failures: " + "; ".join(lock_failures[:3])
        )
    return failures


def _runtime_capability_lock_for_proof_gate(
    runtime_context: dict[str, Any],
) -> dict[str, Any]:
    sources = [
        runtime_context.get("edge_execution_contract"),
        runtime_context.get("runtime_decision"),
        runtime_context.get("runtime_fit"),
    ]
    readiness = runtime_context.get("readiness")
    if isinstance(readiness, dict):
        sources.extend(
            [
                readiness.get("edge_execution_contract"),
                readiness.get("runtime_decision"),
                readiness.get("runtime_fit"),
            ]
        )
    for source in sources:
        if not isinstance(source, dict):
            continue
        lock = source.get("runtime_capability_lock")
        if isinstance(lock, dict) and lock:
            return lock
    return {}


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def edge_runtime_proof_runtime_fit_score(
    action: str,
    payload: dict[str, Any],
) -> float | None:
    if action == "readiness":
        runtime_fit = payload.get("runtime_fit")
    else:
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        runtime_fit = metrics.get("runtime_fit")
    if not isinstance(runtime_fit, dict):
        return None
    try:
        return float(runtime_fit.get("score"))
    except (TypeError, ValueError):
        return None


def canonical_json_hash(payload: dict[str, Any]) -> str:
    """Return the canonical SHA256 used for portable proof envelopes."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def edge_runtime_proof_component_digests(proof: dict[str, Any]) -> dict[str, Any]:
    """Return stable digests for the independently auditable proof components."""
    digests: dict[str, Any] = {
        "schema_version": EDGE_RUNTIME_PROOF_COMPONENT_DIGESTS_SCHEMA_VERSION,
    }
    for component_name in (
        "runtime_workbench",
        "runtime_decision_trace",
        "edge_execution_manifest",
    ):
        component = proof.get(component_name)
        if isinstance(component, dict) and component:
            digests[f"{component_name}_sha256"] = canonical_json_hash(component)
    return digests


def edge_mission_package_component_digests(plan: dict[str, Any]) -> dict[str, Any]:
    """Return stable digests for package-plan components handed to the edge."""
    digests: dict[str, Any] = {
        "schema_version": EDGE_MISSION_PACKAGE_COMPONENT_DIGESTS_SCHEMA_VERSION,
    }
    for component_name in (
        "mission",
        "selection",
        "slo",
        "model_handling",
        "ddil",
        "runtime_plan",
        "proof_gate",
        "deployment_intent",
        "edge_handoff",
        "edge_execution_contract",
        "runtime_workbench",
    ):
        component = plan.get(component_name)
        if isinstance(component, dict) and component:
            digests[f"{component_name}_sha256"] = canonical_json_hash(component)
    return digests


def sign_edge_runtime_proof(
    proof: dict[str, Any],
    key: str,
    *,
    signer: str = "temms",
) -> dict[str, Any]:
    """Attach an HMAC attestation to an edge-runtime proof envelope."""
    from temms.core.signing import SIGNATURE_ALGORITHM, signing_key_fingerprint

    integrity = proof.setdefault("integrity", {})
    if not isinstance(integrity, dict):
        raise ValueError("proof integrity must be an object")
    payload_sha256 = str(integrity.get("payload_sha256") or "")
    if not payload_sha256:
        unsigned_proof = dict(proof)
        unsigned_proof.pop("integrity", None)
        payload_sha256 = canonical_json_hash(unsigned_proof)
        integrity["payload_sha256"] = payload_sha256
    attestation = {
        "schema_version": EDGE_RUNTIME_PROOF_ATTESTATION_SCHEMA_VERSION,
        "algorithm": SIGNATURE_ALGORITHM,
        "signed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "signer": signer,
        "key_fingerprint": signing_key_fingerprint(key),
        "payload_sha256": payload_sha256,
    }
    attestation["signature"] = _edge_runtime_proof_attestation_signature(
        attestation,
        key,
    )
    integrity["attestation"] = attestation
    return proof


def verify_edge_runtime_proof_attestation(
    proof: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    """Verify the optional HMAC attestation on an edge-runtime proof envelope."""
    from temms.core.signing import SIGNATURE_ALGORITHM, signing_key_fingerprint

    integrity = proof.get("integrity") if isinstance(proof.get("integrity"), dict) else {}
    attestation = (
        integrity.get("attestation")
        if isinstance(integrity.get("attestation"), dict)
        else {}
    )
    errors: list[str] = []
    if not attestation:
        errors.append("integrity.attestation is missing")
    elif attestation.get("schema_version") != EDGE_RUNTIME_PROOF_ATTESTATION_SCHEMA_VERSION:
        errors.append(
            "attestation schema_version is "
            f"{attestation.get('schema_version') or 'missing'}, expected "
            f"{EDGE_RUNTIME_PROOF_ATTESTATION_SCHEMA_VERSION}"
        )
    elif attestation.get("algorithm") != SIGNATURE_ALGORITHM:
        errors.append(f"unsupported attestation algorithm: {attestation.get('algorithm')}")
    else:
        payload_sha256 = str(integrity.get("payload_sha256") or "")
        if attestation.get("payload_sha256") != payload_sha256:
            errors.append("attestation payload_sha256 does not match proof integrity")
        expected_fingerprint = signing_key_fingerprint(key)
        if attestation.get("key_fingerprint") != expected_fingerprint:
            errors.append("attestation signing key fingerprint mismatch")
        expected_signature = _edge_runtime_proof_attestation_signature(
            attestation,
            key,
        )
        if not hmac.compare_digest(str(attestation.get("signature") or ""), expected_signature):
            errors.append("attestation signature mismatch")

    return {
        "schema_version": "temms-edge-runtime-proof-attestation-verification/v1",
        "verified": not errors,
        "errors": errors,
        "algorithm": attestation.get("algorithm"),
        "signer": attestation.get("signer"),
        "signed_at": attestation.get("signed_at"),
        "key_fingerprint": attestation.get("key_fingerprint"),
    }


def _edge_runtime_proof_attestation_signature(
    attestation: dict[str, Any],
    key: str,
) -> str:
    unsigned = {name: value for name, value in attestation.items() if name != "signature"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hmac.new(key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _edge_runtime_mission_summary(
    *,
    status: str,
    headline: str,
    next_action: str,
    checked_at: str,
    selection: dict[str, Any],
    gates: list[dict[str, Any]],
    runtime_fit: dict[str, Any] | None,
    production_admission: dict[str, Any] | None,
    runtime_decision: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the compact operator-facing selected edge runtime path."""
    gates_by_id = {
        str(gate.get("gate_id") or ""): gate
        for gate in gates
        if isinstance(gate, dict)
    }
    runtime_fit = runtime_fit if isinstance(runtime_fit, dict) else {}
    production_admission = (
        production_admission if isinstance(production_admission, dict) else {}
    )
    runtime_lane = (
        runtime_fit.get("runtime_lane")
        if isinstance(runtime_fit.get("runtime_lane"), dict)
        else {}
    )
    artifact_lane = (
        runtime_fit.get("artifact_lane")
        if isinstance(runtime_fit.get("artifact_lane"), dict)
        else {}
    )
    target_selection = (
        runtime_fit.get("target_selection")
        if isinstance(runtime_fit.get("target_selection"), dict)
        else {}
    )
    path = _readiness_refs(
        {
            "package_id": selection.get("package_id"),
            "model_id": selection.get("model_id"),
            "device_id": selection.get("device_id"),
            "runtime_target_id": selection.get("runtime_target_id"),
            "slot": selection.get("slot"),
            "rollout_id": selection.get("rollout_id"),
            "label": _edge_runtime_path_label(selection),
        }
    )
    metrics = {
        "runtime_fit": _readiness_refs(
            {
                "status": _runtime_fit_status(runtime_fit),
                "score": runtime_fit.get("score"),
                "tier": runtime_fit.get("tier"),
                "detail": runtime_fit.get("detail"),
            }
        ),
        "runtime_lane": _readiness_refs(
            {
                "status": "go" if runtime_lane else "attention",
                "lane_id": runtime_lane.get("lane_id"),
                "label": runtime_lane.get("label"),
                "execution_engine": runtime_lane.get("execution_engine"),
                "acceleration": runtime_lane.get("acceleration"),
                "providers": runtime_lane.get("providers"),
                "accelerators": runtime_lane.get("accelerators"),
                "optimization_goal": runtime_lane.get("optimization_goal"),
            }
        ),
        "artifact_fit": _readiness_refs(
            {
                "status": artifact_lane.get("status") or "attention",
                "state": artifact_lane.get("state"),
                "detail": artifact_lane.get("detail"),
                "model_format": artifact_lane.get("model_format"),
                "lane_id": artifact_lane.get("lane_id"),
            }
        ),
        "live_inventory": _edge_live_inventory_metric(gates_by_id),
        "performance": _edge_gate_metric(gates_by_id.get("performance_fit")),
        "resources": _edge_gate_metric(gates_by_id.get("resource_envelope")),
        "runtime_validation": _edge_gate_metric(gates_by_id.get("runtime_target")),
        "production_admission": _readiness_refs(
            {
                "status": production_admission.get("status") or "attention",
                "apply_allowed": production_admission.get("apply_allowed"),
                "detail": production_admission.get("detail"),
                "blocking_gate_count": production_admission.get("blocking_gate_count"),
            }
        ),
    }
    if target_selection:
        metrics["target_selection"] = _readiness_refs(
            {
                "status": target_selection.get("status"),
                "detail": target_selection.get("detail"),
                "selected_rank": target_selection.get("selected_rank"),
                "selected_score": target_selection.get("selected_score"),
                "best_runtime_target_id": target_selection.get("best_runtime_target_id"),
                "best_score": target_selection.get("best_score"),
                "score_delta": target_selection.get("score_delta"),
            }
        )
    if runtime_decision:
        decision_target = (
            runtime_decision.get("target_selection")
            if isinstance(runtime_decision.get("target_selection"), dict)
            else {}
        )
        metrics["runtime_decision"] = _readiness_refs(
            {
                "status": decision_target.get("status") or runtime_decision.get("readiness_status"),
                "recommended_action": runtime_decision.get("recommended_action"),
                "detail": runtime_decision.get("detail"),
                "best_runtime_target_id": decision_target.get("best_runtime_target_id"),
                "apply_allowed": (
                    runtime_decision.get("production_admission", {}).get("apply_allowed")
                    if isinstance(runtime_decision.get("production_admission"), dict)
                    else None
                ),
            }
        )
    return _readiness_refs(
        {
            "schema_version": "temms-edge-runtime-mission/v1",
            "status": status,
            "headline": _edge_runtime_mission_headline(status),
            "detail": _edge_runtime_mission_detail(status, headline, path),
            "next_action": next_action,
            "checked_at": checked_at,
            "path": path,
            "metrics": metrics,
            "operator_focus": _edge_runtime_operator_focus(
                gates,
                target_selection=target_selection,
                production_admission=production_admission,
            ),
        }
    )


def _edge_runtime_path_label(selection: dict[str, Any]) -> str:
    return " -> ".join(
        str(selection.get(key) or "missing")
        for key in ("model_id", "runtime_target_id", "device_id")
    )


def _edge_runtime_mission_headline(status: str) -> str:
    if status == "go":
        return "Selected model is proven for the edge path"
    if status == "blocked":
        return "Selected edge path is blocked"
    if status == "attention":
        return "Selected edge path needs operator proof"
    return "Selected edge path needs review"


def _edge_runtime_mission_detail(
    status: str,
    readiness_headline: str,
    path: dict[str, Any],
) -> str:
    label = str(path.get("label") or "selected edge path")
    if status == "go":
        return f"{label} satisfies runtime, artifact, SLO, resource, and admission gates"
    return f"{readiness_headline}: {label}"


def _runtime_fit_status(runtime_fit: dict[str, Any]) -> str:
    tier = str(runtime_fit.get("tier") or "")
    if tier in {"optimal", "ready"}:
        return "go"
    if tier == "blocked":
        return "blocked"
    if runtime_fit:
        return "attention"
    return "missing"


def _edge_gate_metric(gate: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(gate, dict):
        return {
            "status": "missing",
            "state": "missing",
            "detail": "Readiness gate is not available",
        }
    return _readiness_refs(
        {
            "status": gate.get("status"),
            "state": gate.get("state"),
            "detail": gate.get("detail"),
            "gate_id": gate.get("gate_id"),
        }
    )


def _edge_live_inventory_metric(gates_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    runtime_gate = gates_by_id.get("runtime_target")
    edge_gate = gates_by_id.get("edge_target")
    if isinstance(runtime_gate, dict) and runtime_gate.get("status") != "go":
        return _edge_gate_metric(runtime_gate)
    return _edge_gate_metric(edge_gate)


def _edge_runtime_operator_focus(
    gates: list[dict[str, Any]],
    *,
    target_selection: dict[str, Any],
    production_admission: dict[str, Any],
) -> list[str]:
    focus: list[str] = []
    if target_selection.get("status") not in {None, "", "best"}:
        detail = target_selection.get("detail")
        if detail:
            focus.append(str(detail))
    for gate in gates:
        if gate.get("status") == "go":
            continue
        detail = gate.get("detail")
        if detail:
            focus.append(str(detail))
    if production_admission.get("apply_allowed") is False:
        detail = production_admission.get("detail")
        if detail:
            focus.append(str(detail))
    unique: list[str] = []
    for item in focus:
        if item not in unique:
            unique.append(item)
    return unique[:4]


def deployment_readiness_apply_blocking_gates(
    readiness: dict[str, Any],
    *,
    runtime_target_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return gates that should stop production rollout apply on an edge agent."""
    return _production_apply_blocking_gates(
        [
            gate
            for gate in readiness.get("gates", [])
            if isinstance(gate, dict)
        ],
        runtime_target_id=runtime_target_id,
    )


def _production_admission_summary(
    gates: list[dict[str, Any]],
    *,
    runtime_target_id: str | None,
) -> dict[str, Any]:
    blocking_gates = _production_apply_blocking_gates(
        gates,
        runtime_target_id=runtime_target_id,
    )
    if blocking_gates:
        return {
            "schema_version": "temms-production-admission/v1",
            "status": "blocked",
            "apply_allowed": False,
            "detail": "Production apply is blocked until edge admission gates are resolved",
            "blocking_gate_count": len(blocking_gates),
            "blocking_gates": blocking_gates,
        }
    return {
        "schema_version": "temms-production-admission/v1",
        "status": "go",
        "apply_allowed": True,
        "detail": "Production apply is permitted for the selected edge runtime path",
        "blocking_gate_count": 0,
        "blocking_gates": [],
    }


def _production_apply_blocking_gates(
    gates: list[dict[str, Any]],
    *,
    runtime_target_id: str | None,
) -> list[dict[str, Any]]:
    blocking: list[dict[str, Any]] = []
    for gate in gates:
        if gate.get("status") == "go":
            continue
        gate_id = str(gate.get("gate_id") or "")
        gate_state = str(gate.get("state") or "")
        should_block = gate_id in {
            "performance_fit",
            "resource_envelope",
            "edge_target",
        }
        if gate_id in {"runtime_target", "runtime_optimizer"} and runtime_target_id:
            should_block = True
        if gate_id == "model_package" and gate_state in {"missing", "retired"}:
            should_block = True
        if should_block:
            blocking.append(_production_apply_gate_ref(gate))
    return blocking


def _production_apply_gate_ref(gate: dict[str, Any]) -> dict[str, Any]:
    return _readiness_refs(
        {
            "gate_id": gate.get("gate_id"),
            "label": gate.get("label"),
            "status": gate.get("status"),
            "state": gate.get("state"),
            "detail": gate.get("detail"),
            "refs": gate.get("refs") if isinstance(gate.get("refs"), dict) else None,
            "actions": gate.get("actions") if isinstance(gate.get("actions"), list) else None,
        }
    )


def _readiness_actions(gates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for gate in gates:
        if gate.get("status") == "go":
            continue
        for action in gate.get("actions") or []:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("action_id") or action.get("kind") or "")
            if not action_id or action_id in seen:
                continue
            seen.add(action_id)
            actions.append({**action, "gate_id": gate.get("gate_id")})
    return actions


def _select_readiness_package(
    data: dict[str, Any],
    *,
    package_id: str | None,
) -> dict[str, Any] | None:
    packages = data.get("packages", {})
    if package_id:
        package = packages.get(package_id)
        if package is None:
            raise ValueError(f"Unknown package: {package_id}")
        return package
    candidates = list(packages.values())
    if not candidates:
        return None
    candidates.sort(
        key=lambda package: (
            _package_promotion_summary(package).get("state") == "released",
            _package_compatibility_summary(package).get("signature_verified") is True,
            _record_timestamp(package),
        ),
        reverse=True,
    )
    return candidates[0]


def _select_readiness_device(
    data: dict[str, Any],
    *,
    device_id: str | None,
) -> dict[str, Any] | None:
    devices = data.get("devices", {})
    if device_id:
        device = devices.get(device_id)
        if device is None:
            raise ValueError(f"Unknown device: {device_id}")
        return device
    candidates = list(devices.values())
    if not candidates:
        return None
    candidates.sort(
        key=lambda device: (
            device.get("status") == "online",
            device.get("status") != "offline",
            _record_timestamp(device),
        ),
        reverse=True,
    )
    return candidates[0]


def _select_readiness_runtime_target(
    data: dict[str, Any],
    *,
    runtime_target_id: str | None,
    package: dict[str, Any] | None,
    device: dict[str, Any] | None,
    model_id: str | None,
) -> dict[str, Any] | None:
    runtime_targets = _runtime_targets_with_defaults(data)
    if runtime_target_id:
        runtime_target = runtime_targets.get(runtime_target_id)
        if runtime_target is None:
            raise ValueError(f"Unknown runtime target: {runtime_target_id}")
        return runtime_target
    if not runtime_targets:
        return None

    candidates = list(runtime_targets.values())
    if package is not None and device is not None and model_id:
        usable = []
        for target in candidates:
            fit = _runtime_target_fit_summary(
                data,
                package=package,
                device=device,
                runtime_target=target,
                model_id=model_id,
            )
            if fit.get("tier") != "blocked":
                usable.append((target, fit))
        if usable:
            usable.sort(
                key=lambda pair: (
                    -int(pair[1].get("score") or 0),
                    str(pair[0].get("runtime_target_id") or pair[0].get("id") or ""),
                )
            )
            return usable[0][0]

    package_id = package.get("package_id") if package else None
    if package_id:
        validations = [
            validation
            for validation in data.get("runtime_validations", {}).values()
            if validation.get("package_id") == package_id
            and validation.get("runtime_target_id") in runtime_targets
            and _runtime_validation_passed(validation)
        ]
        validations.sort(key=lambda validation: validation.get("created_at", ""), reverse=True)
        if validations:
            return runtime_targets.get(str(validations[0].get("runtime_target_id")))

    if package is not None and device is not None:
        compatible = [
            target
            for target in candidates
            if not _runtime_target_failures(target, package, device, model_id=model_id)
        ]
        if compatible:
            return compatible[0]
    return candidates[0]


def _select_readiness_rollout(
    data: dict[str, Any],
    *,
    package_id: str | None,
    model_id: str | None,
    device_id: str | None,
    runtime_target_id: str | None,
    slot: str | None,
) -> dict[str, Any] | None:
    rollouts = list(data.get("rollouts", {}).values())
    if package_id:
        rollouts = [
            rollout for rollout in rollouts if rollout.get("package_id") == package_id
        ]
    if model_id:
        rollouts = [
            rollout
            for rollout in rollouts
            if rollout.get("model_id") == model_id
        ]
    if device_id:
        rollouts = [rollout for rollout in rollouts if rollout.get("device_id") == device_id]
    if runtime_target_id:
        rollouts = [
            rollout
            for rollout in rollouts
            if rollout.get("runtime_target_id") == runtime_target_id
        ]
    if slot:
        rollouts = [rollout for rollout in rollouts if rollout.get("slot") == slot]
    rollouts.sort(key=_record_timestamp, reverse=True)
    return rollouts[0] if rollouts else None


def _record_timestamp(record: dict[str, Any]) -> str:
    return str(
        record.get("updated_at")
        or record.get("created_at")
        or record.get("last_seen_at")
        or record.get("enrolled_at")
        or ""
    )


def _first_declared_model_id(package: dict[str, Any]) -> str | None:
    metadata = package.get("metadata") if isinstance(package.get("metadata"), dict) else {}
    models = metadata.get("models") if isinstance(metadata.get("models"), list) else []
    for model in models:
        if isinstance(model, dict) and model.get("id"):
            return str(model["id"])
    return None


def _select_hub_records(
    records: dict[str, dict[str, Any]],
    requested_ids: list[str] | None,
    *,
    id_label: str,
) -> list[dict[str, Any]]:
    if requested_ids is None:
        return list(records.values())
    selected: list[dict[str, Any]] = []
    for record_id in requested_ids:
        if not record_id:
            continue
        record = records.get(record_id)
        if record is None:
            raise ValueError(f"Unknown {id_label}: {record_id}")
        selected.append(record)
    return selected


def _device_compatibility_summary(device: dict[str, Any]) -> dict[str, Any]:
    inventory = device.get("inventory") if isinstance(device.get("inventory"), dict) else {}
    return {
        "device_id": device.get("device_id"),
        "profile": normalize_device_profile(device.get("profile")),
        "status": device.get("status"),
        "last_seen_at": device.get("last_seen_at"),
        "runtimes": inventory.get("runtimes", {}),
        "accelerators": inventory.get("accelerators", {}),
    }


def _package_compatibility_summary(package: dict[str, Any]) -> dict[str, Any]:
    metadata = package.get("metadata") if isinstance(package.get("metadata"), dict) else {}
    return {
        "package_id": package.get("package_id"),
        "name": package.get("name"),
        "version": package.get("version"),
        "device_profiles": [
            normalized
            for normalized in (
                normalize_device_profile(profile) for profile in package.get("device_profiles", [])
            )
            if normalized
        ],
        "runtime_constraints": [
            {"model_id": model_id, "constraints": constraints}
            for model_id, constraints in _catalog_runtime_constraints(package)
        ],
        "signature_verified": (
            metadata.get("signature_verified") is True
            or (
                isinstance(metadata.get("validation"), dict)
                and metadata["validation"].get("signature_verified") is True
            )
        ),
    }


def _matrix_package_summary(package: dict[str, Any]) -> dict[str, Any]:
    summary = _package_compatibility_summary(package)
    summary["promotion"] = _package_promotion_summary(package)
    return summary


def _matrix_model_ids(package: dict[str, Any], requested_model_ids: list[str] | None) -> list[str | None]:
    """Return model ids represented by compatibility-matrix cells for a package."""
    declared_model_ids = sorted(_catalog_model_ids(package))
    if requested_model_ids is not None:
        requested = [str(model_id) for model_id in requested_model_ids if model_id]
        if declared_model_ids:
            return [model_id for model_id in requested if model_id in declared_model_ids]
        return [None] if not requested else []
    return declared_model_ids or [None]


def _performance_fit_summary(
    data: dict[str, Any],
    *,
    package: dict[str, Any],
    device: dict[str, Any],
    runtime_target: dict[str, Any] | None,
    model_id: str,
) -> dict[str, Any]:
    package_id = str(package.get("package_id") or "")
    device_id = str(device.get("device_id") or "")
    runtime_target_id = (
        str(runtime_target.get("runtime_target_id") or runtime_target.get("id") or "")
        if runtime_target
        else None
    )
    model = _catalog_model(package, model_id)
    slo = _model_performance_slo(model)
    benchmark = _latest_matching_benchmark(
        data,
        package_id=package_id,
        model_id=model_id,
        device_id=device_id,
        runtime_target_id=runtime_target_id,
    )
    benchmark_summary = _benchmark_performance_summary(benchmark)
    if benchmark is None:
        if slo:
            return {
                "status": "attention",
                "state": "benchmark missing",
                "detail": "No benchmark evidence for declared performance SLO on this edge/runtime target",
                "slo": slo,
                "benchmark": None,
                "failures": ["missing benchmark evidence"],
            }
        return {
            "status": "go",
            "state": "not required",
            "detail": "No performance SLO declared for the selected model",
            "slo": {},
            "benchmark": None,
            "failures": [],
        }

    freshness = _benchmark_freshness(benchmark_summary, slo)
    if slo and freshness["status"] != "go":
        return {
            "status": "attention",
            "state": "benchmark stale",
            "detail": freshness["detail"],
            "slo": slo,
            "benchmark": benchmark_summary,
            "benchmark_freshness": freshness,
            "failures": ["stale benchmark evidence"],
        }

    failures = _benchmark_slo_failures(benchmark_summary, slo)
    if failures:
        return {
            "status": "attention",
            "state": "slo miss",
            "detail": "; ".join(failures),
            "slo": slo,
            "benchmark": benchmark_summary,
            "benchmark_freshness": freshness,
            "failures": failures,
        }
    if slo:
        return {
            "status": "go",
            "state": "slo met",
            "detail": _performance_detail(benchmark_summary, slo, meets_slo=True),
            "slo": slo,
            "benchmark": benchmark_summary,
            "benchmark_freshness": freshness,
            "failures": [],
        }
    return {
        "status": "go",
        "state": "benchmarked",
        "detail": _performance_detail(benchmark_summary, slo, meets_slo=False),
        "slo": {},
        "benchmark": benchmark_summary,
        "benchmark_freshness": freshness,
        "failures": [],
    }


def _performance_fit_refs(fit: dict[str, Any]) -> dict[str, Any]:
    benchmark = fit.get("benchmark") if isinstance(fit.get("benchmark"), dict) else {}
    freshness = (
        fit.get("benchmark_freshness")
        if isinstance(fit.get("benchmark_freshness"), dict)
        else {}
    )
    slo = fit.get("slo") if isinstance(fit.get("slo"), dict) else {}
    refs: dict[str, Any] = {
        "performance_state": fit.get("state"),
        "benchmark_id": benchmark.get("benchmark_id"),
        "benchmark_created_at": benchmark.get("created_at"),
        "benchmark_freshness_state": freshness.get("state"),
        "benchmark_age_seconds": freshness.get("benchmark_age_seconds"),
        "benchmark_stale_after_seconds": freshness.get("benchmark_stale_after_seconds"),
        "latency_ms_p95": benchmark.get("latency_ms_p95"),
        "throughput_ips": benchmark.get("throughput_ips"),
        "max_latency_ms_p95": slo.get("max_latency_ms_p95"),
        "min_throughput_ips": slo.get("min_throughput_ips"),
        "max_benchmark_age_seconds": slo.get("max_benchmark_age_seconds"),
    }
    failures = fit.get("failures")
    if isinstance(failures, list) and failures:
        refs["performance_failures"] = failures
    return refs


def _fallback_model_candidate(
    data: dict[str, Any],
    *,
    package: dict[str, Any],
    device: dict[str, Any],
    runtime_target: dict[str, Any] | None,
    current_model_id: str,
    allow_runtime_switch: bool = False,
) -> dict[str, Any] | None:
    package_id = str(package.get("package_id") or "")
    device_id = str(device.get("device_id") or "")
    runtime_options: list[dict[str, Any] | None]
    if allow_runtime_switch:
        runtime_options = list(_runtime_targets_with_defaults(data).values())
        if runtime_target is not None:
            current_target_id = str(runtime_target.get("runtime_target_id") or runtime_target.get("id") or "")
            runtime_options.sort(
                key=lambda target: (
                    str((target or {}).get("runtime_target_id") or (target or {}).get("id") or "")
                    != current_target_id,
                    str((target or {}).get("runtime_target_id") or (target or {}).get("id") or ""),
                )
            )
    else:
        runtime_options = [runtime_target]
    candidates: list[dict[str, Any]] = []
    for model_id in sorted(_catalog_model_ids(package)):
        if model_id == current_model_id:
            continue
        for candidate_runtime_target in runtime_options:
            if candidate_runtime_target is not None:
                runtime_failures = _runtime_target_failures(
                    candidate_runtime_target,
                    package,
                    device,
                    model_id=model_id,
                )
            else:
                runtime_failures = _runtime_constraint_failures(
                    package,
                    device,
                    model_id=model_id,
                )
            if runtime_failures:
                continue

            performance = _performance_fit_summary(
                data,
                package=package,
                device=device,
                runtime_target=candidate_runtime_target,
                model_id=model_id,
            )
            if performance.get("status") != "go":
                continue
            resource = _resource_envelope_summary(
                package=package,
                device=device,
                model_id=model_id,
            )
            if resource.get("status") != "go":
                continue

            benchmark = (
                performance.get("benchmark")
                if isinstance(performance.get("benchmark"), dict)
                else {}
            )
            latency = _float_of(benchmark.get("latency_ms_p95"))
            throughput = _float_of(benchmark.get("throughput_ips"))
            candidate_runtime_target_id = (
                str(
                    candidate_runtime_target.get("runtime_target_id")
                    or candidate_runtime_target.get("id")
                    or ""
                )
                if candidate_runtime_target
                else ""
            )
            candidates.append(
                {
                    "model_id": model_id,
                    "package_id": package_id,
                    "device_id": device_id,
                    "runtime_target_id": candidate_runtime_target_id,
                    "performance_state": performance.get("state"),
                    "performance_detail": performance.get("detail"),
                    "resource_state": resource.get("state"),
                    "resource_detail": resource.get("detail"),
                    "benchmark_id": benchmark.get("benchmark_id"),
                    "latency_ms_p95": latency,
                    "throughput_ips": throughput,
                }
            )

    if not candidates:
        return None

    def candidate_score(candidate: dict[str, Any]) -> tuple[int, float, float, str]:
        latency = _float_of(candidate.get("latency_ms_p95"))
        throughput = _float_of(candidate.get("throughput_ips"))
        has_benchmark = 0 if candidate.get("benchmark_id") else 1
        latency_score = latency if latency is not None else float("inf")
        throughput_score = -(throughput or 0.0)
        return (
            has_benchmark,
            latency_score,
            throughput_score,
            str(candidate.get("model_id") or ""),
        )

    candidates.sort(key=candidate_score)
    return candidates[0]


def _resource_envelope_summary(
    *,
    package: dict[str, Any],
    device: dict[str, Any],
    model_id: str,
) -> dict[str, Any]:
    model = _catalog_model(package, model_id)
    requirements = _model_resource_requirements(model)
    observed = _device_resource_snapshot(device)
    artifact_size_mb = _model_artifact_size_mb(model)
    if artifact_size_mb is not None:
        observed["artifact_size_mb"] = artifact_size_mb

    if not requirements:
        return {
            "status": "go",
            "state": "not declared",
            "detail": "No resource envelope declared for the selected model",
            "requirements": {},
            "observed": observed,
            "failures": [],
            "missing": [],
        }

    failures: list[str] = []
    missing: list[str] = []
    min_memory = _float_of(requirements.get("min_memory_available_mb"))
    memory_available = _float_of(observed.get("memory_available_mb"))
    if min_memory is not None:
        if memory_available is None:
            missing.append("available memory")
        elif memory_available < min_memory:
            failures.append(
                f"available memory {memory_available:g} MB below required {min_memory:g} MB"
            )

    min_storage = _float_of(requirements.get("min_storage_available_mb"))
    storage_available = _float_of(observed.get("storage_available_mb"))
    if min_storage is not None:
        if storage_available is None:
            missing.append("available storage")
        elif storage_available < min_storage:
            failures.append(
                f"available storage {storage_available:g} MB below required {min_storage:g} MB"
            )

    max_temperature = _float_of(requirements.get("max_temperature_c"))
    temperature = _float_of(observed.get("temperature_c"))
    if max_temperature is not None:
        if temperature is None:
            missing.append("temperature")
        elif temperature > max_temperature:
            failures.append(
                f"temperature {temperature:g} C exceeds limit {max_temperature:g} C"
            )

    min_battery = _float_of(requirements.get("min_battery_percent"))
    battery = _float_of(observed.get("battery_percent"))
    if min_battery is not None:
        if battery is None:
            missing.append("battery percent")
        elif battery < min_battery:
            failures.append(f"battery {battery:g}% below required {min_battery:g}%")

    required_power = str(requirements.get("required_power_source") or "").strip().lower()
    power_source = str(observed.get("power_source") or "").strip().lower()
    if required_power:
        if not power_source:
            missing.append("power source")
        elif power_source != required_power:
            failures.append(f"power source {power_source} does not match {required_power}")

    if failures:
        return {
            "status": "blocked",
            "state": "constrained",
            "detail": "; ".join(failures),
            "requirements": requirements,
            "observed": observed,
            "failures": failures,
            "missing": missing,
        }
    if missing:
        return {
            "status": "attention",
            "state": "telemetry missing",
            "detail": "Resource telemetry missing: " + ", ".join(missing),
            "requirements": requirements,
            "observed": observed,
            "failures": [],
            "missing": missing,
        }
    return {
        "status": "go",
        "state": "met",
        "detail": _resource_envelope_detail(requirements, observed),
        "requirements": requirements,
        "observed": observed,
        "failures": [],
        "missing": [],
    }


def _resource_envelope_refs(fit: dict[str, Any]) -> dict[str, Any]:
    requirements = fit.get("requirements") if isinstance(fit.get("requirements"), dict) else {}
    observed = fit.get("observed") if isinstance(fit.get("observed"), dict) else {}
    refs: dict[str, Any] = {
        "resource_state": fit.get("state"),
        "min_memory_available_mb": requirements.get("min_memory_available_mb"),
        "memory_available_mb": observed.get("memory_available_mb"),
        "min_storage_available_mb": requirements.get("min_storage_available_mb"),
        "storage_available_mb": observed.get("storage_available_mb"),
        "max_temperature_c": requirements.get("max_temperature_c"),
        "temperature_c": observed.get("temperature_c"),
        "min_battery_percent": requirements.get("min_battery_percent"),
        "battery_percent": observed.get("battery_percent"),
        "required_power_source": requirements.get("required_power_source"),
        "power_source": observed.get("power_source"),
        "artifact_size_mb": observed.get("artifact_size_mb"),
    }
    failures = fit.get("failures")
    missing = fit.get("missing")
    if isinstance(failures, list) and failures:
        refs["resource_failures"] = failures
    if isinstance(missing, list) and missing:
        refs["resource_missing"] = missing
    return _readiness_refs(refs)


def _latest_matching_benchmark(
    data: dict[str, Any],
    *,
    package_id: str,
    model_id: str,
    device_id: str,
    runtime_target_id: str | None,
) -> dict[str, Any] | None:
    benchmarks = [
        benchmark
        for benchmark in data.get("benchmarks", {}).values()
        if isinstance(benchmark, dict)
        and benchmark.get("package_id") == package_id
        and benchmark.get("model_id") == model_id
        and benchmark.get("device_id") == device_id
        and (
            not runtime_target_id
            or benchmark.get("runtime_target_id") == runtime_target_id
        )
    ]
    benchmarks.sort(key=lambda benchmark: str(benchmark.get("created_at") or ""), reverse=True)
    return benchmarks[0] if benchmarks else None


def _benchmark_performance_summary(benchmark: dict[str, Any] | None) -> dict[str, Any] | None:
    if benchmark is None:
        return None
    result = benchmark.get("result") if isinstance(benchmark.get("result"), dict) else {}
    latency_ms_p95 = _benchmark_latency_ms_p95(result)
    throughput_ips = _benchmark_throughput_ips(result)
    return _readiness_refs(
        {
            "benchmark_id": benchmark.get("benchmark_id"),
            "device_id": benchmark.get("device_id"),
            "package_id": benchmark.get("package_id"),
            "model_id": benchmark.get("model_id"),
            "runtime_target_id": benchmark.get("runtime_target_id"),
            "latency_ms_p95": latency_ms_p95,
            "throughput_ips": throughput_ips,
            "created_at": benchmark.get("created_at"),
            "actor": benchmark.get("actor"),
        }
    )


def _model_performance_slo(model: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(model, dict):
        return {}
    metadata = model.get("metadata") if isinstance(model.get("metadata"), dict) else {}
    constraints = (
        model.get("runtime_constraints")
        if isinstance(model.get("runtime_constraints"), dict)
        else {}
    )
    sources = [
        model.get("performance_slo"),
        model.get("slo"),
        metadata.get("performance_slo"),
        metadata.get("slo"),
        constraints.get("performance_slo"),
    ]
    raw = next((source for source in sources if isinstance(source, dict)), {})
    max_latency = _first_float(
        raw,
        (
            "max_latency_ms_p95",
            "latency_ms_p95_max",
            "p95_latency_ms_max",
            "max_p95_latency_ms",
        ),
    )
    min_throughput = _first_float(
        raw,
        (
            "min_throughput_ips",
            "throughput_ips_min",
            "min_inferences_per_second",
            "inferences_per_second_min",
            "throughput_fps_min",
        ),
    )
    max_benchmark_age = _first_float(
        raw,
        (
            "max_benchmark_age_seconds",
            "benchmark_stale_after_seconds",
            "benchmark_freshness_seconds",
            "max_age_seconds",
        ),
    )
    return _readiness_refs(
        {
            "max_latency_ms_p95": max_latency,
            "min_throughput_ips": min_throughput,
            "max_benchmark_age_seconds": max_benchmark_age,
        }
    )


def _model_resource_requirements(model: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(model, dict):
        return {}
    metadata = model.get("metadata") if isinstance(model.get("metadata"), dict) else {}
    constraints = (
        model.get("runtime_constraints")
        if isinstance(model.get("runtime_constraints"), dict)
        else {}
    )
    sources = [
        model.get("resource_requirements"),
        model.get("resources"),
        metadata.get("resource_requirements"),
        metadata.get("resources"),
        constraints.get("resource_requirements"),
        constraints.get("resources"),
    ]
    raw = next((source for source in sources if isinstance(source, dict)), {})
    min_memory = _first_float(
        raw,
        (
            "min_memory_available_mb",
            "min_available_memory_mb",
            "memory_available_mb_min",
            "min_memory_mb",
            "min_ram_mb",
            "peak_memory_mb",
        ),
    )
    min_storage = _first_float(
        raw,
        (
            "min_storage_available_mb",
            "min_available_storage_mb",
            "storage_available_mb_min",
            "min_disk_available_mb",
            "min_storage_mb",
            "min_disk_mb",
        ),
    )
    max_temperature = _first_float(
        raw,
        (
            "max_temperature_c",
            "max_cpu_temp_c",
            "max_thermal_c",
            "temperature_c_max",
            "thermal_c_max",
        ),
    )
    min_battery = _first_float(
        raw,
        (
            "min_battery_percent",
            "battery_percent_min",
            "min_battery_pct",
        ),
    )
    required_power = raw.get("required_power_source") or raw.get("power_source")
    return _readiness_refs(
        {
            "min_memory_available_mb": min_memory,
            "min_storage_available_mb": min_storage,
            "max_temperature_c": max_temperature,
            "min_battery_percent": min_battery,
            "required_power_source": (
                str(required_power).strip().lower()
                if required_power is not None and str(required_power).strip()
                else None
            ),
        }
    )


def _device_resource_snapshot(device: dict[str, Any]) -> dict[str, Any]:
    inventory = device.get("inventory") if isinstance(device.get("inventory"), dict) else {}
    memory = inventory.get("memory") if isinstance(inventory.get("memory"), dict) else {}
    storage = inventory.get("storage") if isinstance(inventory.get("storage"), dict) else {}
    disk = inventory.get("disk") if isinstance(inventory.get("disk"), dict) else {}
    thermal = inventory.get("thermal") if isinstance(inventory.get("thermal"), dict) else {}
    power = inventory.get("power") if isinstance(inventory.get("power"), dict) else {}
    memory_available = _first_float_in_sources(
        (memory, inventory),
        (
            "available_mb",
            "memory_available_mb",
            "available_memory_mb",
            "free_memory_mb",
            "platform.compute.memory_available_mb",
        ),
    )
    storage_available = _first_float_in_sources(
        (storage, disk, inventory),
        (
            "available_mb",
            "storage_available_mb",
            "disk_available_mb",
            "disk_free_mb",
            "free_storage_mb",
        ),
    )
    temperature = _first_float_in_sources(
        (thermal, inventory),
        (
            "temperature_c",
            "cpu_temp_c",
            "max_observed_c",
            "platform.compute.cpu_temp_c",
        ),
    )
    battery = _first_float_in_sources(
        (power, inventory),
        (
            "battery_percent",
            "battery_pct",
            "platform.power.battery_percent",
        ),
    )
    power_source = (
        power.get("source")
        or inventory.get("power_source")
        or inventory.get("platform.power.source")
    )
    return _readiness_refs(
        {
            "memory_available_mb": memory_available,
            "storage_available_mb": storage_available,
            "temperature_c": temperature,
            "battery_percent": battery,
            "power_source": str(power_source).strip().lower() if power_source else None,
        }
    )


def _model_artifact_size_mb(model: dict[str, Any] | None) -> float | None:
    if not isinstance(model, dict):
        return None
    size_bytes = _float_of(model.get("size_bytes"))
    if size_bytes is None:
        return None
    return round(size_bytes / (1024 * 1024), 3)


def _resource_envelope_detail(requirements: dict[str, Any], observed: dict[str, Any]) -> str:
    parts: list[str] = []
    memory = _float_of(observed.get("memory_available_mb"))
    min_memory = _float_of(requirements.get("min_memory_available_mb"))
    if memory is not None and min_memory is not None:
        parts.append(f"{memory:g} MB RAM >= {min_memory:g} MB")
    storage = _float_of(observed.get("storage_available_mb"))
    min_storage = _float_of(requirements.get("min_storage_available_mb"))
    if storage is not None and min_storage is not None:
        parts.append(f"{storage:g} MB storage >= {min_storage:g} MB")
    temperature = _float_of(observed.get("temperature_c"))
    max_temperature = _float_of(requirements.get("max_temperature_c"))
    if temperature is not None and max_temperature is not None:
        parts.append(f"{temperature:g} C <= {max_temperature:g} C")
    battery = _float_of(observed.get("battery_percent"))
    min_battery = _float_of(requirements.get("min_battery_percent"))
    if battery is not None and min_battery is not None:
        parts.append(f"{battery:g}% battery >= {min_battery:g}%")
    required_power = requirements.get("required_power_source")
    power_source = observed.get("power_source")
    if required_power and power_source:
        parts.append(f"power source {power_source}")
    return "; ".join(parts) or "Device resource telemetry satisfies model envelope"


def _benchmark_freshness(
    benchmark: dict[str, Any] | None,
    slo: dict[str, Any],
) -> dict[str, Any]:
    stale_after_seconds = _benchmark_stale_after_seconds(slo)
    created_at = str((benchmark or {}).get("created_at") or "")
    created = _parse_hub_timestamp(created_at)
    checked_at = _parse_hub_timestamp(_now())
    if created is None or checked_at is None:
        return {
            "status": "attention",
            "state": "benchmark timestamp unknown",
            "detail": "benchmark evidence timestamp is missing or invalid",
            "benchmark_created_at": created_at,
            "benchmark_stale_after_seconds": stale_after_seconds,
        }

    age_seconds = max(0, int((checked_at - created).total_seconds()))
    if age_seconds > stale_after_seconds:
        return {
            "status": "attention",
            "state": "benchmark stale",
            "detail": (
                f"benchmark evidence is {_format_seconds(age_seconds)} old; "
                f"freshness budget is {_format_seconds(stale_after_seconds)}"
            ),
            "benchmark_created_at": created_at,
            "benchmark_age_seconds": age_seconds,
            "benchmark_stale_after_seconds": stale_after_seconds,
        }
    return {
        "status": "go",
        "state": "benchmark fresh",
        "detail": f"benchmark evidence is {_format_seconds(age_seconds)} old",
        "benchmark_created_at": created_at,
        "benchmark_age_seconds": age_seconds,
        "benchmark_stale_after_seconds": stale_after_seconds,
    }


def _benchmark_stale_after_seconds(slo: dict[str, Any]) -> int:
    max_age = _float_of(slo.get("max_benchmark_age_seconds"))
    if max_age is None or max_age <= 0:
        return READINESS_BENCHMARK_STALE_SECONDS
    return max(1, int(max_age))


def _benchmark_slo_failures(
    benchmark: dict[str, Any] | None,
    slo: dict[str, Any],
) -> list[str]:
    if not slo:
        return []
    if benchmark is None:
        return ["missing benchmark evidence"]
    failures: list[str] = []
    max_latency = _float_of(slo.get("max_latency_ms_p95"))
    latency = _float_of(benchmark.get("latency_ms_p95"))
    if max_latency is not None:
        if latency is None:
            failures.append("benchmark missing p95 latency")
        elif latency > max_latency:
            failures.append(f"p95 latency {latency:g} ms exceeds SLO {max_latency:g} ms")
    min_throughput = _float_of(slo.get("min_throughput_ips"))
    throughput = _float_of(benchmark.get("throughput_ips"))
    if min_throughput is not None:
        if throughput is None:
            failures.append("benchmark missing throughput")
        elif throughput < min_throughput:
            failures.append(
                f"throughput {throughput:g} ips is below SLO {min_throughput:g} ips"
            )
    return failures


def _performance_detail(
    benchmark: dict[str, Any] | None,
    slo: dict[str, Any],
    *,
    meets_slo: bool,
) -> str:
    if benchmark is None:
        return "No benchmark evidence recorded"
    metrics: list[str] = []
    latency = _float_of(benchmark.get("latency_ms_p95"))
    throughput = _float_of(benchmark.get("throughput_ips"))
    if latency is not None:
        metrics.append(f"p95 {latency:g} ms")
    if throughput is not None:
        metrics.append(f"{throughput:g} ips")
    metric_text = " / ".join(metrics) if metrics else "benchmark recorded"
    if slo and meets_slo:
        return f"{metric_text} meets declared performance SLO"
    return metric_text


def _benchmark_latency_ms_p95(result: dict[str, Any]) -> float | None:
    latency = result.get("latency_ms") if isinstance(result.get("latency_ms"), dict) else {}
    return _float_of(
        latency.get("p95")
        or result.get("latency_ms_p95")
        or result.get("p95_latency_ms")
    )


def _benchmark_throughput_ips(result: dict[str, Any]) -> float | None:
    throughput = result.get("throughput") if isinstance(result.get("throughput"), dict) else {}
    return _float_of(
        throughput.get("inferences_per_second")
        or result.get("throughput_ips")
        or result.get("throughput_fps")
        or result.get("inferences_per_second")
    )


def _first_float(source: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _float_of(source.get(key))
        if value is not None:
            return value
    return None


def _first_float_in_sources(
    sources: tuple[dict[str, Any], ...],
    keys: tuple[str, ...],
) -> float | None:
    for source in sources:
        for key in keys:
            value = _float_of(source.get(key))
            if value is not None:
                return value
    return None


def _float_of(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sanitize_runtime_validation_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return runtime validation result metadata safe for long-lived audit records."""
    command = result.get("command") if isinstance(result.get("command"), list) else []
    sanitized_command = _sanitize_command(command)
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    return {
        "schema_version": "temms-runtime-target-validation/v1",
        "runtime_target_id": result.get("runtime_target_id"),
        "image": result.get("image"),
        "package_path": result.get("package_path"),
        "command": sanitized_command,
        "command_text": shlex.join(sanitized_command) if sanitized_command else "",
        "dry_run": bool(result.get("dry_run")),
        "exit_code": result.get("exit_code"),
        "ok": bool(result.get("ok")),
        "stdout": stdout[:16000],
        "stderr": stderr[:16000],
        "stdout_truncated": len(stdout) > 16000,
        "stderr_truncated": len(stderr) > 16000,
    }


def _sanitize_benchmark_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return benchmark metadata safe for Hub Lite JSON state."""
    try:
        sanitized = json.loads(json.dumps(result, default=str))
    except Exception:
        sanitized = {"raw": str(result)}
    for key in ("stdout", "stderr"):
        if isinstance(sanitized.get(key), str) and len(sanitized[key]) > 4000:
            sanitized[key] = sanitized[key][:4000] + "...[truncated]"
            sanitized[f"{key}_truncated"] = True
    return sanitized


def _sanitize_command(command: list[Any]) -> list[str]:
    sanitized: list[str] = []
    redact_next = False
    for item in command:
        value = str(item)
        if redact_next:
            sanitized.append("********")
            redact_next = False
            continue
        if value == "--signing-key":
            sanitized.append(value)
            redact_next = True
            continue
        if value.startswith("TEMMS_PACKAGE_SIGNING_KEY="):
            sanitized.append("TEMMS_PACKAGE_SIGNING_KEY=********")
            continue
        sanitized.append(value)
    return sanitized


def _latest_passing_runtime_validation(
    data: dict[str, Any],
    *,
    package_id: str,
    runtime_target_id: str,
    package: dict[str, Any],
) -> dict[str, Any] | None:
    expected_sha = package.get("source_sha256") or package.get("sha256")
    candidates = [
        validation
        for validation in data.get("runtime_validations", {}).values()
        if validation.get("package_id") == package_id
        and validation.get("runtime_target_id") == runtime_target_id
        and _runtime_validation_passed(validation)
        and _runtime_validation_source_matches(validation, expected_sha)
    ]
    candidates.sort(key=lambda validation: validation.get("created_at", ""), reverse=True)
    return candidates[0] if candidates else None


def _rollout_approval(
    *,
    required: bool,
    actor: str | None,
    reason: str | None,
    updated_at: str,
) -> dict[str, Any]:
    approved = bool(required and actor)
    return {
        "schema_version": "temms-rollout-approval/v1",
        "required": required,
        "approved": approved,
        "state": "approved" if approved else ("pending" if required else "not_required"),
        "actor": actor,
        "reason": reason,
        "updated_at": updated_at,
    }


def _runtime_validation_passed(validation: dict[str, Any]) -> bool:
    result = validation.get("result") if isinstance(validation.get("result"), dict) else {}
    return result.get("ok") is True and result.get("dry_run") is not True


def _runtime_validation_source_matches(
    validation: dict[str, Any],
    expected_sha: str | None,
) -> bool:
    if not expected_sha:
        return True
    return validation.get("source_sha256") == expected_sha


def _rollout_runtime_validation_summary(
    validation: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if validation is None:
        return None
    result = validation.get("result") if isinstance(validation.get("result"), dict) else {}
    return {
        "validation_id": validation.get("validation_id"),
        "runtime_target_id": validation.get("runtime_target_id"),
        "package_id": validation.get("package_id"),
        "source_sha256": validation.get("source_sha256"),
        "ok": result.get("ok"),
        "dry_run": result.get("dry_run"),
        "exit_code": result.get("exit_code"),
        "actor": validation.get("actor"),
        "created_at": validation.get("created_at"),
    }


def _runtime_target_failures(
    runtime_target: dict[str, Any],
    package: dict[str, Any],
    device: dict[str, Any],
    model_id: str | None = None,
) -> list[str]:
    """Return runtime target compatibility failures for a package/device pair."""
    failures: list[str] = []
    target_profiles = [
        normalized
        for normalized in (
            normalize_device_profile(profile)
            for profile in runtime_target.get("device_profiles", [])
        )
        if normalized
    ]
    device_profile = normalize_device_profile(device.get("profile"))
    if target_profiles and device_profile not in target_profiles:
        failures.append(
            f"device profile {device_profile} is not in runtime target profiles "
            f"{target_profiles}"
        )

    package_profiles = [
        normalized
        for normalized in (
            normalize_device_profile(profile) for profile in package.get("device_profiles", [])
        )
        if normalized
    ]
    if (
        package_profiles
        and target_profiles
        and not set(package_profiles).intersection(target_profiles)
    ):
        failures.append(
            f"package profiles {package_profiles} do not overlap runtime target profiles "
            f"{target_profiles}"
        )

    failures.extend(_device_inventory_runtime_target_failures(runtime_target, device))

    capabilities = {
        "device_profile": target_profiles[0] if target_profiles else device_profile,
        "runtimes": runtime_target.get("runtimes", {}),
        "accelerators": runtime_target.get("accelerators", {}),
    }
    for constrained_model_id, constraints in _catalog_runtime_constraints(
        package,
        model_id=model_id,
    ):
        satisfied, reasons = runtime_constraints_satisfied(constraints, capabilities)
        if not satisfied:
            failures.extend(f"{constrained_model_id}: {reason}" for reason in reasons)

    target_constraints = runtime_target.get("runtime_constraints") or {}
    if target_constraints:
        satisfied, reasons = runtime_constraints_satisfied(target_constraints, capabilities)
        if not satisfied:
            failures.extend(f"runtime target: {reason}" for reason in reasons)

    artifact_fit = _model_artifact_lane_summary(
        package,
        model_id=model_id,
        runtime_target=runtime_target,
    )
    if artifact_fit.get("status") == "blocked":
        failures.append(str(artifact_fit.get("detail") or "model artifact does not fit runtime lane"))

    return failures


ARTIFACT_LANE_FORMATS = {
    "cpu-onnx": {
        "native": {"onnx"},
        "convertible": set(),
    },
    "jetson-cuda": {
        "native": {"onnx"},
        "convertible": set(),
    },
    "rpi5-tflite": {
        "native": {"tflite"},
        "convertible": set(),
    },
    "orin-tensorrt": {
        "native": {"engine", "plan", "tensorrt"},
        "convertible": {"onnx"},
    },
}


def _model_artifact_lane_summary(
    package: dict[str, Any],
    *,
    model_id: str | None,
    runtime_target: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return whether the selected model artifact is native for the runtime lane."""
    runtime_lane = runtime_lane_summary(runtime_target)
    lane_id = str(runtime_lane.get("lane_id") or "device-inventory")
    selected_model_id = model_id or _first_declared_model_id(package)
    model = _catalog_model(package, selected_model_id) if selected_model_id else None
    model_format = _model_artifact_format(model)
    filename = str((model or {}).get("filename") or "")
    format_sets = ARTIFACT_LANE_FORMATS.get(lane_id)

    base = {
        "schema_version": "temms-artifact-lane/v1",
        "lane_id": lane_id,
        "lane_label": runtime_lane.get("label"),
        "model_id": selected_model_id,
        "model_format": model_format,
        "filename": filename or None,
        "native_formats": sorted(format_sets["native"]) if format_sets else [],
        "convertible_formats": sorted(format_sets["convertible"]) if format_sets else [],
    }
    if model is None:
        return {
            **base,
            "status": "attention",
            "state": "model metadata missing",
            "detail": "Model artifact metadata is not available for lane verification",
        }
    if not model_format:
        return {
            **base,
            "status": "attention",
            "state": "format missing",
            "detail": f"{model_id} does not declare an artifact format for {runtime_lane.get('label')}",
        }
    if not format_sets:
        return {
            **base,
            "status": "attention",
            "state": "lane format unknown",
            "detail": f"{runtime_lane.get('label')} has no artifact format policy",
        }
    if model_format in format_sets["native"]:
        return {
            **base,
            "status": "go",
            "state": "native artifact",
            "detail": f"{model_format} artifact is native for {runtime_lane.get('label')}",
        }
    if model_format in format_sets["convertible"]:
        return {
            **base,
            "status": "attention",
            "state": "conversion path",
            "detail": (
                f"{model_format} artifact can target {runtime_lane.get('label')}, "
                "but a built runtime artifact should be validated before production apply"
            ),
        }

    accepted_formats = sorted(format_sets["native"] | format_sets["convertible"])
    return {
        **base,
        "status": "blocked",
        "state": "artifact mismatch",
        "detail": (
            f"{model_format} artifact is not compatible with {runtime_lane.get('label')}; "
            f"package one of: {', '.join(accepted_formats)}"
        ),
    }


def _model_artifact_format(model: dict[str, Any] | None) -> str | None:
    if not isinstance(model, dict):
        return None
    model_format = str(model.get("format") or "").strip().lower()
    if model_format:
        aliases = {
            "trt": "tensorrt",
            "tensorrt_engine": "tensorrt",
            "tensorflow_lite": "tflite",
        }
        return aliases.get(model_format, model_format)
    filename = str(model.get("filename") or "").lower()
    suffix_map = {
        ".onnx": "onnx",
        ".tflite": "tflite",
        ".engine": "engine",
        ".plan": "plan",
        ".pt": "torchscript",
        ".pth": "torchscript",
    }
    for suffix, inferred_format in suffix_map.items():
        if filename.endswith(suffix):
            return inferred_format
    return None


def _device_inventory_runtime_target_failures(
    runtime_target: dict[str, Any],
    device: dict[str, Any],
) -> list[str]:
    """Return failures when live edge inventory contradicts a selected runtime target."""
    inventory = device.get("inventory") if isinstance(device.get("inventory"), dict) else {}
    reports_runtime_surface = isinstance(inventory.get("runtimes"), dict) or isinstance(
        inventory.get("accelerators"), dict
    )
    if not reports_runtime_surface:
        return []

    constraints = _runtime_target_inventory_constraints(runtime_target)
    if not constraints:
        return []

    capabilities = {
        **inventory,
        "device_profile": normalize_device_profile(
            inventory.get("device_profile") or device.get("profile")
        ),
        "runtimes": inventory.get("runtimes") if isinstance(inventory.get("runtimes"), dict) else {},
        "accelerators": (
            inventory.get("accelerators") if isinstance(inventory.get("accelerators"), dict) else {}
        ),
    }
    satisfied, reasons = runtime_constraints_satisfied(constraints, capabilities)
    if satisfied:
        return []

    runtime_target_id = runtime_target.get("runtime_target_id") or runtime_target.get("id")
    return [
        f"edge inventory cannot host runtime target {runtime_target_id}: {reason}"
        for reason in reasons
    ]


def _runtime_target_inventory_constraints(runtime_target: dict[str, Any]) -> dict[str, Any]:
    """Return runtime/provider/accelerator constraints implied by a runtime target."""
    constraints = dict(runtime_target.get("runtime_constraints") or {})
    runtimes = runtime_target.get("runtimes") if isinstance(runtime_target.get("runtimes"), dict) else {}
    accelerators = (
        runtime_target.get("accelerators")
        if isinstance(runtime_target.get("accelerators"), dict)
        else {}
    )

    if "runtimes" not in constraints:
        required_runtimes = [
            str(runtime)
            for runtime, status in runtimes.items()
            if not isinstance(status, dict) or status.get("available") is not False
        ]
        if required_runtimes:
            constraints["runtimes"] = required_runtimes

    onnxruntime = runtimes.get("onnxruntime") if isinstance(runtimes.get("onnxruntime"), dict) else {}
    if (
        "providers" not in constraints
        and "provider_order" not in constraints
        and "preferred_providers" not in constraints
        and isinstance(onnxruntime.get("providers"), list)
        and onnxruntime["providers"]
    ):
        constraints["preferred_providers"] = [
            str(provider) for provider in onnxruntime["providers"] if provider
        ]

    if "accelerators" not in constraints:
        required_accelerators = [
            str(accelerator)
            for accelerator, status in accelerators.items()
            if isinstance(status, dict) and status.get("available") is True
        ]
        if required_accelerators:
            constraints["accelerators"] = required_accelerators

    return constraints


def _rollout_runtime_target_summary(
    runtime_target: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return compact runtime target metadata embedded in rollout history."""
    if runtime_target is None:
        return None
    return {
        "runtime_target_id": runtime_target.get("runtime_target_id"),
        "name": runtime_target.get("name"),
        "image": runtime_target.get("image"),
        "registry": runtime_target.get("registry"),
        "os": runtime_target.get("os"),
        "arch": runtime_target.get("arch"),
        "device_profiles": runtime_target.get("device_profiles", []),
        "runtime_lane": runtime_lane_summary(runtime_target),
        "source": runtime_target.get("source"),
    }


def _parse_hub_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    timestamp = str(value).strip()
    if not timestamp:
        return None
    if timestamp.endswith("Z"):
        timestamp = timestamp[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _format_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} minutes" if remainder == 0 else f"{minutes}m {remainder}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hours" if minutes == 0 else f"{hours}h {minutes}m"


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _evidence_bundle_id(bundle: dict[str, Any]) -> str:
    integrity = bundle.get("integrity") if isinstance(bundle.get("integrity"), dict) else {}
    digest = integrity.get("payload_sha256")
    if not digest:
        encoded = json.dumps(bundle, sort_keys=True, separators=(",", ":"), default=str).encode()
        digest = hashlib.sha256(encoded).hexdigest()
    return f"evidence-{str(digest)[:16]}"


def _infer_evidence_device_id(bundle: dict[str, Any]) -> str | None:
    hub_lite = bundle.get("hub_lite") if isinstance(bundle.get("hub_lite"), dict) else {}
    devices = hub_lite.get("devices") if isinstance(hub_lite.get("devices"), dict) else {}
    if len(devices) == 1:
        return str(next(iter(devices)))
    telemetry = bundle.get("telemetry") if isinstance(bundle.get("telemetry"), dict) else {}
    for event in telemetry.get("events", []):
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        device_id = event.get("device_id") or payload.get("device_id")
        if device_id:
            return str(device_id)
    return None


def _package_artifact_payload(package_path: Path) -> tuple[str, bytes]:
    """Return an archive filename and bytes suitable for air-gap embedding."""
    from temms.core.package_archive import create_package_archive, is_package_archive

    if is_package_archive(package_path):
        return package_path.name, package_path.read_bytes()
    if package_path.is_dir():
        with tempfile.TemporaryDirectory(prefix="temms-airgap-") as tmp:
            archive_path = create_package_archive(
                package_path,
                Path(tmp) / f"{package_path.name.removesuffix('.temms')}.temms.tar.zst",
            )
            return archive_path.name, archive_path.read_bytes()
    raise ValueError(f"Package artifact not found: {package_path}")


def _verified_package_path(package_id: str, package: dict[str, Any]) -> Path:
    """Return package path after checking it still matches the catalog digest."""
    from temms.core.package_catalog import package_source_sha256

    package_path = package.get("path")
    if not package_path:
        raise PackageArtifactNotFound(f"Package {package_id} has no path")

    path = Path(package_path)
    if not path.exists():
        raise PackageArtifactNotFound(f"Package artifact not found: {path}")

    expected_sha = package.get("sha256")
    if expected_sha:
        actual_sha = package_source_sha256(path)
        if actual_sha != expected_sha:
            raise PackageArtifactIntegrityError(
                f"Package artifact changed after registration: {package_id}; "
                f"expected {expected_sha}, got {actual_sha}"
            )
    return path


def _sha256_file(path: Path) -> str:
    """Compute SHA256 for a package artifact."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(content: bytes) -> str:
    """Compute SHA256 for in-memory package artifact bytes."""
    return hashlib.sha256(content).hexdigest()


def _runtime_constraint_failures(
    package: dict[str, Any],
    device: dict[str, Any],
    model_id: str | None = None,
) -> list[str]:
    """Return package runtime constraint failures for a device inventory."""
    inventory = device.get("inventory") if isinstance(device.get("inventory"), dict) else {}
    capabilities = {
        **inventory,
        "device_profile": normalize_device_profile(
            inventory.get("device_profile") or device.get("profile")
        ),
    }
    failures: list[str] = []
    for constrained_model_id, constraints in _catalog_runtime_constraints(package, model_id=model_id):
        satisfied, reasons = runtime_constraints_satisfied(constraints, capabilities)
        if not satisfied:
            failures.extend(f"{constrained_model_id}: {reason}" for reason in reasons)
    return failures


def _catalog_runtime_constraints(
    package: dict[str, Any],
    model_id: str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Extract runtime constraints from a Hub Lite catalog entry."""
    metadata = package.get("metadata") if isinstance(package.get("metadata"), dict) else {}
    compatibility = (
        metadata.get("compatibility") if isinstance(metadata.get("compatibility"), dict) else {}
    )
    package_constraints = dict(package.get("runtime_constraints") or {})
    package_constraints.update(compatibility.get("runtime_constraints") or {})

    models = metadata.get("models") if isinstance(metadata.get("models"), list) else []
    extracted: list[tuple[str, dict[str, Any]]] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        if model_id and model.get("id") != model_id:
            continue
        constraints = dict(package_constraints)
        constraints.update(model.get("runtime_constraints") or {})
        if constraints:
            extracted.append(
                (model.get("id") or package.get("package_id") or "unknown", constraints)
            )

    if not extracted and package_constraints:
        extracted.append((package.get("package_id") or "package", package_constraints))
    return extracted


def _catalog_model_ids(package: dict[str, Any]) -> set[str]:
    """Return model ids declared by a Hub Lite catalog entry."""
    metadata = package.get("metadata") if isinstance(package.get("metadata"), dict) else {}
    models = metadata.get("models") if isinstance(metadata.get("models"), list) else []
    return {
        str(model.get("id"))
        for model in models
        if isinstance(model, dict) and model.get("id")
    }


def _catalog_model(package: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    """Return model metadata declared by a Hub Lite catalog entry."""
    metadata = package.get("metadata") if isinstance(package.get("metadata"), dict) else {}
    models = metadata.get("models") if isinstance(metadata.get("models"), list) else []
    for model in models:
        if isinstance(model, dict) and str(model.get("id") or "") == model_id:
            return model
    return None


def _validate_package_model(package: dict[str, Any], *, package_id: str, model_id: str) -> None:
    """Raise when a model-specific rollout targets a model outside the package."""
    model_ids = _catalog_model_ids(package)
    if model_ids and model_id not in model_ids:
        raise ValueError(f"Model {model_id} is not declared by package {package_id}")
