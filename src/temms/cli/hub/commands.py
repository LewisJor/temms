"""Hub Lite commands as objects.

One class per action. Each takes **only the inputs it actually uses** — the
interface-segregation point that the previous 55-field context object violated,
where every handler depended on ~51 fields it never touched.

Each command:

* receives its transport (dependency inversion — it never builds one),
* receives its own inputs as named constructor arguments,
* answers ``execute() -> HubResult``.

Because a command constructs nothing, ``ListDevices(FakeTransport()).execute()``
is a complete test: no CLI runner, no httpx, no patching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from temms.cli.hub.transport import HubTransport


@dataclass(frozen=True)
class HubResult:
    """What a command produced.

    ``proof`` carries the full readiness document for actions that also emit an
    edge-runtime proof; ``handled`` says the command already produced its own
    output and the shared emission step must be skipped.
    """

    payload: dict[str, Any]
    proof: dict[str, Any] | None = None
    handled: bool = False
    messages: tuple[str, ...] = field(default_factory=tuple)


class HubCommand(Protocol):
    """Every Hub action answers the same question, so callers stay uniform."""

    def execute(self) -> HubResult: ...


# --------------------------------------------------------------------------
# Reads: a path, no inputs.
# --------------------------------------------------------------------------


class _Listing:
    """Shared shape for the bare GET listings.

    Eight actions were identical but for their path. Subclasses name the
    resource; the behaviour lives here once.
    """

    path: str

    def __init__(self, transport: HubTransport) -> None:
        self._transport = transport

    def execute(self) -> HubResult:
        return HubResult(self._transport.get(self.path))


class ListDevices(_Listing):
    path = "/devices"


class ListPackages(_Listing):
    path = "/packages"


class ListRuntimeTargets(_Listing):
    path = "/runtime-targets"


class ListRollouts(_Listing):
    path = "/rollouts"




class ListTelemetry(_Listing):
    path = "/telemetry"


class ListEvidence(_Listing):
    path = "/evidence"


class DeploymentStatus(_Listing):
    path = "/deployment-status"


# --------------------------------------------------------------------------
# Reads: filtered.
# --------------------------------------------------------------------------


def _filters(**candidates: Any) -> dict[str, Any] | None:
    """Only send the filters the operator actually supplied."""
    supplied = {key: value for key, value in candidates.items() if value}
    return supplied or None


class ListRuntimeValidations:
    def __init__(
        self,
        transport: HubTransport,
        *,
        package_id: str | None = None,
        runtime_target_id: str | None = None,
    ) -> None:
        self._transport = transport
        self._package_id = package_id
        self._runtime_target_id = runtime_target_id

    def execute(self) -> HubResult:
        return HubResult(
            self._transport.get(
                "/runtime-targets/validations",
                params=_filters(
                    package_id=self._package_id,
                    runtime_target_id=self._runtime_target_id,
                ),
            )
        )


class ListBenchmarks:
    def __init__(
        self,
        transport: HubTransport,
        *,
        device_id: str | None = None,
        package_id: str | None = None,
        runtime_target_id: str | None = None,
    ) -> None:
        self._transport = transport
        self._device_id = device_id
        self._package_id = package_id
        self._runtime_target_id = runtime_target_id

    def execute(self) -> HubResult:
        return HubResult(
            self._transport.get(
                "/benchmarks",
                params=_filters(
                    device_id=self._device_id,
                    package_id=self._package_id,
                    runtime_target_id=self._runtime_target_id,
                ),
            )
        )


# --------------------------------------------------------------------------
# Writes.
# --------------------------------------------------------------------------


class EnrollDevice:
    def __init__(
        self,
        transport: HubTransport,
        *,
        device_id: str,
        device_profile: str | None = None,
        labels: dict[str, str] | None = None,
        inventory: dict[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._device_id = device_id
        self._device_profile = device_profile
        self._labels = labels or {}
        self._inventory = inventory or {}

    def execute(self) -> HubResult:
        return HubResult(
            self._transport.post(
                "/devices/enroll",
                json={
                    "device_id": self._device_id,
                    "profile": self._device_profile,
                    "labels": self._labels,
                    "inventory": self._inventory,
                },
            )
        )








class ImportAirgapBundle:
    def __init__(self, transport: HubTransport, *, bundle_path: Path) -> None:
        self._transport = transport
        self._bundle_path = bundle_path

    def execute(self) -> HubResult:
        import json

        bundle = json.loads(self._bundle_path.read_text(encoding="utf-8"))
        return HubResult(self._transport.post("/airgap/import", json=bundle))


class ExportAirgapBundle:
    """Writes its own artifact, so it reports ``handled``."""

    def __init__(
        self,
        transport: HubTransport,
        *,
        include_packages: bool = False,
        output: Path | None = None,
    ) -> None:
        self._transport = transport
        self._include_packages = include_packages
        self._output = output

    def execute(self) -> HubResult:
        import json

        payload = self._transport.post(
            "/airgap/export", json={"include_packages": self._include_packages}
        )
        if self._output is None:
            return HubResult(payload)
        self._output.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return HubResult(
            payload,
            handled=True,
            messages=(f"[green]Hub bundle written:[/green] {self._output}",),
        )


# --------------------------------------------------------------------------
# Readiness: the one command that also yields a proof document.
# --------------------------------------------------------------------------


class Readiness:
    """``readiness`` and ``edge-runtime-mission`` share a request.

    They differ only in what they present: the mission action narrows the
    payload to the mission block while both carry the full readiness document
    forward as the proof source.
    """

    def __init__(
        self,
        transport: HubTransport,
        *,
        package_id: str | None = None,
        model_id: str | None = None,
        device_id: str | None = None,
        runtime_target_id: str | None = None,
        slot: str | None = None,
        mission_only: bool = False,
    ) -> None:
        self._transport = transport
        self._filters = _filters(
            package_id=package_id,
            model_id=model_id,
            device_id=device_id,
            runtime_target_id=runtime_target_id,
            slot=slot,
        )
        self._mission_only = mission_only

    def execute(self) -> HubResult:
        readiness = self._transport.get("/readiness", params=self._filters)
        if not self._mission_only:
            return HubResult(readiness, proof=readiness)
        mission = readiness.get("edge_runtime_mission")
        return HubResult(mission if isinstance(mission, dict) else {}, proof=readiness)


# --------------------------------------------------------------------------
# Rollout and plan lifecycle.
#
# Six actions are "POST a verb at a resource with a small body". Expressing
# that once keeps the difference between them visible: the path segment and
# which fields the body carries.
# --------------------------------------------------------------------------


class _ResourceAction:
    """POST ``/{collection}/{id}/{verb}``. Subclasses own their body."""

    collection: str
    verb: str

    def __init__(self, transport: HubTransport, *, resource_id: str) -> None:
        self._transport = transport
        self._resource_id = resource_id

    def _body(self) -> dict[str, Any]:
        raise NotImplementedError

    def execute(self) -> HubResult:
        return HubResult(
            self._transport.post(
                f"/{self.collection}/{self._resource_id}/{self.verb}", json=self._body()
            )
        )




class ApproveRollout(_ResourceAction):
    collection, verb = "rollouts", "approve"

    def __init__(
        self,
        transport: HubTransport,
        *,
        resource_id: str,
        reason: str | None = None,
        actor: str | None = None,
    ) -> None:
        super().__init__(transport, resource_id=resource_id)
        self._reason = reason
        self._actor = actor

    def _body(self) -> dict[str, Any]:
        return {"reason": self._reason, "actor": self._actor}


class RollbackRollout(ApproveRollout):
    verb = "rollback"


class ApplyRollout(_ResourceAction):
    collection, verb = "rollouts", "apply"

    def __init__(
        self,
        transport: HubTransport,
        *,
        resource_id: str,
        require_signature: bool = True,
        signing_key: str | None = None,
        actor: str | None = None,
    ) -> None:
        super().__init__(transport, resource_id=resource_id)
        self._require_signature = require_signature
        self._signing_key = signing_key
        self._actor = actor

    def _body(self) -> dict[str, Any]:
        return {
            "require_signature": self._require_signature,
            "signing_key": self._signing_key,
            "actor": self._actor,
        }


class PromotePackage(_ResourceAction):
    collection, verb = "packages", "promote"

    def __init__(
        self,
        transport: HubTransport,
        *,
        resource_id: str,
        state: str | None = None,
        reason: str | None = None,
        actor: str | None = None,
    ) -> None:
        super().__init__(transport, resource_id=resource_id)
        self._state = state
        self._reason = reason
        self._actor = actor

    def _body(self) -> dict[str, Any]:
        return {"state": self._state, "reason": self._reason, "actor": self._actor}


# --------------------------------------------------------------------------
# Bundle uploads: read a JSON file, post it with attribution.
# --------------------------------------------------------------------------


class _BundleUpload:
    path: str

    def __init__(
        self,
        transport: HubTransport,
        *,
        bundle_path: Path,
        device_id: str | None = None,
        actor: str | None = None,
    ) -> None:
        self._transport = transport
        self._bundle_path = bundle_path
        self._device_id = device_id
        self._actor = actor

    def execute(self) -> HubResult:
        import json

        bundle = json.loads(self._bundle_path.read_text(encoding="utf-8"))
        return HubResult(
            self._transport.post(
                self.path,
                json={"bundle": bundle, "device_id": self._device_id, "actor": self._actor},
            )
        )


class ReplayTelemetry(_BundleUpload):
    path = "/telemetry/replay"


class IngestEvidence(_BundleUpload):
    path = "/evidence/ingest"


# --------------------------------------------------------------------------
# Compatibility.
# --------------------------------------------------------------------------


class PreviewCompatibility:
    def __init__(
        self,
        transport: HubTransport,
        *,
        device_id: str,
        package_id: str,
        runtime_target_id: str | None = None,
        model_id: str | None = None,
    ) -> None:
        self._transport = transport
        self._request: dict[str, Any] = {
            "device_id": device_id,
            "package_id": package_id,
            "runtime_target_id": runtime_target_id,
        }
        if model_id:
            self._request["model_id"] = model_id

    def execute(self) -> HubResult:
        return HubResult(self._transport.post("/compatibility/preview", json=self._request))


class CompatibilityMatrix:
    """Every filter is optional; each is sent as a single-element list."""

    def __init__(
        self,
        transport: HubTransport,
        *,
        package_id: str | None = None,
        device_id: str | None = None,
        runtime_target_id: str | None = None,
        model_id: str | None = None,
        include_device_inventory: bool = False,
    ) -> None:
        self._transport = transport
        self._request: dict[str, Any] = {
            "package_ids": [package_id] if package_id else None,
            "device_ids": [device_id] if device_id else None,
            "runtime_target_ids": [runtime_target_id] if runtime_target_id else None,
            "include_device_inventory": include_device_inventory,
        }
        if model_id:
            self._request["model_ids"] = [model_id]

    def execute(self) -> HubResult:
        return HubResult(self._transport.post("/compatibility/matrix", json=self._request))


# --------------------------------------------------------------------------
# Rollouts and plans: creation.
# --------------------------------------------------------------------------


class AssignRollout:
    def __init__(
        self,
        transport: HubTransport,
        *,
        device_id: str,
        package_id: str,
        slot: str | None = None,
        rollout_id: str | None = None,
        runtime_target_id: str | None = None,
        require_runtime_validation: bool = False,
        require_approval: bool = False,
        actor: str | None = None,
        model_id: str | None = None,
    ) -> None:
        self._transport = transport
        self._request: dict[str, Any] = {
            "device_id": device_id,
            "package_id": package_id,
            "slot": slot,
            "rollout_id": rollout_id,
            "runtime_target_id": runtime_target_id,
            "require_runtime_validation": require_runtime_validation,
            "require_approval": require_approval,
            "actor": actor,
        }
        if model_id:
            self._request["model_id"] = model_id

    def execute(self) -> HubResult:
        return HubResult(self._transport.post("/rollouts", json=self._request))




# --------------------------------------------------------------------------
# Packages.
# --------------------------------------------------------------------------


class RegisterPackage:
    def __init__(
        self,
        transport: HubTransport,
        *,
        package_path: Path,
        require_signature: bool = False,
        signing_key: str | None = None,
        device_profile: str | None = None,
        strict_metadata: bool = True,
        actor: str | None = None,
    ) -> None:
        self._transport = transport
        self._request = {
            "package_path": str(package_path.expanduser()),
            "require_signature": require_signature,
            "signing_key": signing_key,
            "device_profiles": [device_profile] if device_profile else None,
            "strict_metadata": strict_metadata,
            "actor": actor,
        }

    def execute(self) -> HubResult:
        return HubResult(self._transport.post("/packages/register", json=self._request))


class BuildPackageFromMLflow:
    def __init__(
        self,
        transport: HubTransport,
        *,
        model_uri: str,
        slot: str,
        tracking_uri: str | None = None,
        device_profile: str | None = None,
        runtimes: list[str] | None = None,
        providers: list[str] | None = None,
        accelerators: list[str] | None = None,
        model_artifact: str | None = None,
        require_schema: bool = True,
        require_signature: bool = False,
        signing_key: str | None = None,
        archive: bool = False,
        overwrite: bool = False,
        strict_metadata: bool = True,
        actor: str | None = None,
    ) -> None:
        self._transport = transport
        constraints: dict[str, Any] = {}
        if device_profile:
            constraints["device_profiles"] = [device_profile]
        if runtimes:
            constraints["runtimes"] = runtimes
        if providers:
            constraints["preferred_providers"] = providers
        if accelerators:
            constraints["accelerators"] = accelerators
        self._request = {
            "model_uri": model_uri,
            "slot": slot,
            "tracking_uri": tracking_uri,
            "device_profile": device_profile,
            "runtime_constraints": constraints,
            "runtime_options": {"providers": providers} if providers else {},
            "model_artifact_path": model_artifact,
            "require_schema": require_schema,
            "require_signature": require_signature,
            "signing_key": signing_key,
            "archive": archive,
            "overwrite": overwrite,
            "strict_metadata": strict_metadata,
            "actor": actor,
        }

    def execute(self) -> HubResult:
        return HubResult(self._transport.post("/packages/from-mlflow", json=self._request))


class RegisterRuntimeTarget:
    def __init__(
        self,
        transport: HubTransport,
        *,
        runtime_target_id: str,
        image: str,
        os_name: str | None = None,
        arch: str | None = None,
        device_profile: str | None = None,
        runtimes: list[str] | None = None,
        providers: list[str] | None = None,
        accelerators: list[str] | None = None,
        labels: dict[str, str] | None = None,
        actor: str | None = None,
    ) -> None:
        self._transport = transport
        runtime_inventory: dict[str, Any] = {r: {"available": True} for r in runtimes or []}
        if providers:
            runtime_inventory.setdefault("onnxruntime", {"available": True})["providers"] = providers
        constraints: dict[str, Any] = {}
        if device_profile:
            constraints["device_profiles"] = [device_profile]
        if runtimes:
            constraints["runtimes"] = runtimes
        if providers:
            constraints["preferred_providers"] = providers
        if accelerators:
            constraints["accelerators"] = accelerators
        self._request = {
            "runtime_target_id": runtime_target_id,
            "name": runtime_target_id,
            "image": image,
            "os": os_name,
            "arch": arch,
            "device_profiles": [device_profile] if device_profile else [],
            "runtimes": runtime_inventory,
            "accelerators": {a: {"available": True} for a in accelerators or []},
            "runtime_constraints": constraints,
            "labels": labels or {},
            "actor": actor,
        }

    def execute(self) -> HubResult:
        return HubResult(self._transport.post("/runtime-targets", json=self._request))


# --------------------------------------------------------------------------
# Mission packages: the request body is assembled by the CLI layer, since it
# is drawn from ~20 options. The command owns only the call.
# --------------------------------------------------------------------------


class MissionPackagePlan:
    def __init__(
        self, transport: HubTransport, *, request: dict[str, Any], download: bool = False
    ) -> None:
        self._transport = transport
        self._request = request
        self._path = "/mission-package/download" if download else "/mission-package/plan"

    def execute(self) -> HubResult:
        return HubResult(self._transport.post(self._path, json=self._request))


class MissionPackageStage:
    def __init__(self, transport: HubTransport, *, request: dict[str, Any]) -> None:
        self._transport = transport
        self._request = request

    def execute(self) -> HubResult:
        return HubResult(self._transport.post("/mission-package/stage", json=self._request))


class ValidateRuntimeTarget:
    """The one action that is not a single request.

    It reads the runtime targets, runs the package against the named one
    locally, then records the outcome back on the Hub. The local validator is
    injected so this class stays a coordinator: it sequences the three steps
    and owns none of them.
    """

    def __init__(
        self,
        transport: HubTransport,
        *,
        package_path: Path,
        runtime_target_id: str,
        validator: Any,
        find_target: Any,
        package_id: str | None = None,
        actor: str | None = None,
    ) -> None:
        self._transport = transport
        self._package_path = package_path
        self._runtime_target_id = runtime_target_id
        self._validate = validator
        self._find_target = find_target
        self._package_id = package_id
        self._actor = actor

    def execute(self) -> HubResult:
        targets = self._transport.get("/runtime-targets")
        target = self._find_target(
            targets.get("runtime_targets", []), self._runtime_target_id
        )
        outcome = {
            "schema_version": "temms-runtime-target-validation/v1",
            **self._validate(target, self._package_path).to_dict(),
        }
        record = self._transport.post(
            "/runtime-targets/validations",
            json={
                "runtime_target_id": self._runtime_target_id,
                "package_id": self._package_id,
                "package_path": str(self._package_path.expanduser()),
                "result": dict(outcome),
                "actor": self._actor,
            },
        )
        return HubResult({**outcome, "validation_record": record})
