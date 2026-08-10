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


class ListRolloutPlans(_Listing):
    path = "/rollout-plans"


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


class _PlanLifecycle:
    """Pause / resume / advance differ only by the verb in their path."""

    verb: str

    def __init__(
        self,
        transport: HubTransport,
        *,
        plan_id: str,
        reason: str | None = None,
        actor: str | None = None,
    ) -> None:
        self._transport = transport
        self._plan_id = plan_id
        self._reason = reason
        self._actor = actor

    def execute(self) -> HubResult:
        return HubResult(
            self._transport.post(
                f"/rollout-plans/{self._plan_id}/{self.verb}",
                json={"reason": self._reason, "actor": self._actor},
            )
        )


class PauseRolloutPlan(_PlanLifecycle):
    verb = "pause"


class ResumeRolloutPlan(_PlanLifecycle):
    verb = "resume"


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
