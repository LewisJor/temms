"""
Hub Lite state store.

Hub Lite is intentionally small for the MVP: it tracks device enrollment,
heartbeats/inventory, package catalog entries, rollout assignments, rollout
state, and air-gap bundle exchange in a local JSON file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from temms.core.atomic import write_json_atomic
from temms.core.runtime_profiles import (
    default_runtime_targets,
    normalize_device_profile,
    runtime_constraints_satisfied,
)

ROLLOUT_STATES = {
    "assigned",
    "downloading",
    "imported",
    "activated",
    "failed",
    "rolled_back",
}


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
    ) -> dict[str, Any]:
        """Return a side-effect-free package/device/runtime compatibility preview."""
        data = self._read()
        return _rollout_compatibility_preview(
            data,
            device_id=device_id,
            package_id=package_id,
            runtime_target_id=runtime_target_id,
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
        require_runtime_validation: bool = False,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Assign a package rollout to one device."""
        data = self._read()
        if device_id not in data["devices"]:
            raise ValueError(f"Unknown device: {device_id}")
        if package_id not in data["packages"]:
            raise ValueError(f"Unknown package: {package_id}")
        device = data["devices"][device_id]
        package = data["packages"][package_id]
        preview = _rollout_compatibility_preview(
            data,
            device_id=device_id,
            package_id=package_id,
            runtime_target_id=runtime_target_id,
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
            "slot": slot,
            "runtime_target_id": runtime_target_id,
            "runtime_target": _rollout_runtime_target_summary(runtime_target),
            "runtime_validation_required": require_runtime_validation,
            "runtime_validation": _rollout_runtime_validation_summary(runtime_validation),
            "state": "assigned",
            "created_at": now,
            "updated_at": now,
            "actor": actor,
            "history": [
                {
                    "state": "assigned",
                    "updated_at": now,
                    "detail": "assigned",
                    "actor": actor,
                }
            ],
        }
        data["rollouts"][rollout_id] = rollout
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
            "telemetry_events": data.get("telemetry_events", {}),
            "telemetry_replays": data.get("telemetry_replays", {}),
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
                    merge_history=key == "rollouts",
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
        failures.extend(_runtime_target_failures(runtime_target, package, device))
    else:
        failures.extend(_runtime_constraint_failures(package, device))

    return {
        "schema_version": "temms-rollout-compatibility/v1",
        "compatible": not failures,
        "failures": failures,
        "device": _device_compatibility_summary(device),
        "package": _package_compatibility_summary(package),
        "runtime_target_id": runtime_target_id,
        "runtime_target": _rollout_runtime_target_summary(runtime_target),
        "checked_at": _now(),
    }


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

    capabilities = {
        "device_profile": target_profiles[0] if target_profiles else device_profile,
        "runtimes": runtime_target.get("runtimes", {}),
        "accelerators": runtime_target.get("accelerators", {}),
    }
    for model_id, constraints in _catalog_runtime_constraints(package):
        satisfied, reasons = runtime_constraints_satisfied(constraints, capabilities)
        if not satisfied:
            failures.extend(f"{model_id}: {reason}" for reason in reasons)

    target_constraints = runtime_target.get("runtime_constraints") or {}
    if target_constraints:
        satisfied, reasons = runtime_constraints_satisfied(target_constraints, capabilities)
        if not satisfied:
            failures.extend(f"runtime target: {reason}" for reason in reasons)

    return failures


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
        "source": runtime_target.get("source"),
    }


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _package_artifact_payload(package_path: Path) -> tuple[str, bytes]:
    """Return an archive filename and bytes suitable for air-gap embedding."""
    from temms.core.package_archive import is_package_archive, create_package_archive

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
    for model_id, constraints in _catalog_runtime_constraints(package):
        satisfied, reasons = runtime_constraints_satisfied(constraints, capabilities)
        if not satisfied:
            failures.extend(f"{model_id}: {reason}" for reason in reasons)
    return failures


def _catalog_runtime_constraints(package: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
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
        constraints = dict(package_constraints)
        constraints.update(model.get("runtime_constraints") or {})
        if constraints:
            extracted.append(
                (model.get("id") or package.get("package_id") or "unknown", constraints)
            )

    if not extracted and package_constraints:
        extracted.append((package.get("package_id") or "package", package_constraints))
    return extracted
