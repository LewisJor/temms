"""The ``temms hub`` sub-commands.

This is the composition root for Hub operations, and the only layer that is
allowed to construct anything: it opens the HTTP client, wraps it in a
transport, builds the command object, and hands the result to the emitter.
The command objects it builds construct nothing themselves.

Each sub-command declares only the options its action accepts. That is the
observable difference from the single ``hub()`` it replaces: previously all
fifty-eight options were in scope for all thirty-five actions, so
``temms hub devices --archive`` parsed happily and did nothing. Now it is a
usage error, and ``temms hub devices --help`` lists four options instead of
fifty-eight.

The invocation surface is unchanged -- ``temms hub <action> [source] --opts``
parses identically before and after.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, NamedTuple

import typer

from temms.cli.hub import commands as cmd
from temms.cli.hub.emit import EmissionPolicy, ResultEmitter
from temms.cli.hub.options import (
    DEFAULT_HUB_URL,
    Accelerators,
    Actor,
    Archive,
    BatchSize,
    DeviceId,
    DeviceProfile,
    HubUrl,
    JsonOutput,
    MinRuntimeFit,
    ModelId,
    Output,
    PackageId,
    Providers,
    Reason,
    RequireApproval,
    RequireBestRuntime,
    RequireCapabilityLock,
    RequireGo,
    RequireRuntimeValidation,
    RequireSchema,
    RequireSignature,
    Runtimes,
    RuntimeTargetId,
    SigningKey,
    SigningKeyFile,
    Slot,
    StrictMetadata,
    Token,
)
from temms.cli.hub.render import HUB_PRINTERS, console
from temms.cli.hub.transport import HttpHubTransport

hub_app = typer.Typer(
    help="Hub Lite operations: enrol devices, register packages, drive rollouts.",
    no_args_is_help=True,
)


def _policy(action: str, **overrides: Any) -> EmissionPolicy:
    """Emission policy for an action, defaulting the renderer from the registry."""
    from temms.cli.hub.render import _render_generic_hub_payload

    renderer = HUB_PRINTERS.get(action, _render_generic_hub_payload)
    return EmissionPolicy(renderer=lambda payload: renderer(action, payload), **overrides)


def _emitter(*, json_output: bool, output: Path | None) -> ResultEmitter:
    return ResultEmitter(
        console=console, echo=typer.echo, json_output=json_output, output=output
    )


def _run(
    build: Any,
    *,
    action: str,
    hub_url: str,
    token: str | None,
    json_output: bool = False,
    output: Path | None = None,
    policy: EmissionPolicy | None = None,
) -> None:
    """Open a client, run one command, emit its result, set the exit code.

    Every sub-command funnels through here, so connection handling, error
    reporting and exit-code policy are defined once rather than thirty-five
    times.
    """
    import httpx

    from temms.cli.main import _hub_api_url, _hub_auth_headers

    try:
        with httpx.Client(
            base_url=_hub_api_url(hub_url), headers=_hub_auth_headers(token), timeout=30.0
        ) as client:
            result = build(HttpHubTransport(client))
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"[red]Hub command failed: {exc}[/red]")
        raise typer.Exit(1) from exc

    if _emitter(json_output=json_output, output=output).emit(
        result, policy or _policy(action)
    ):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _listing(name: str, command: type, help_text: str) -> None:
    """Register one of the bare listings.

    Eight sub-commands differ only in name and command class, so they are
    registered from a table rather than written out eight times.
    """

    def run(
        hub_url: HubUrl = DEFAULT_HUB_URL,
        token: Token = None,
        json_output: JsonOutput = False,
    ) -> None:
        _run(
            lambda transport: command(transport).execute(),
            action=name,
            hub_url=hub_url,
            token=token,
            json_output=json_output,
        )

    run.__doc__ = help_text
    hub_app.command(name)(run)


for _name, _command, _help in [
    ("devices", cmd.ListDevices, "List enrolled devices."),
    ("packages", cmd.ListPackages, "List registered packages."),
    ("runtime-targets", cmd.ListRuntimeTargets, "List registered runtime targets."),
    ("rollouts", cmd.ListRollouts, "List rollouts."),
    ("rollout-plans", cmd.ListRolloutPlans, "List rollout plans."),
    ("telemetry", cmd.ListTelemetry, "List telemetry records."),
    ("evidence", cmd.ListEvidence, "List evidence records."),
    ("status", cmd.DeploymentStatus, "Show deployment status across devices."),
]:
    _listing(_name, _command, _help)


@hub_app.command("runtime-validations")
def runtime_validations(
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    package_id: PackageId = None,
    runtime_target_id: RuntimeTargetId = None,
    json_output: JsonOutput = False,
) -> None:
    """List recorded runtime validations."""
    _run(
        lambda t: cmd.ListRuntimeValidations(
            t, package_id=package_id, runtime_target_id=runtime_target_id
        ).execute(),
        action="runtime-validations",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


@hub_app.command("benchmarks")
def benchmarks(
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    device_id: DeviceId = None,
    package_id: PackageId = None,
    runtime_target_id: RuntimeTargetId = None,
    json_output: JsonOutput = False,
) -> None:
    """List recorded benchmarks."""
    _run(
        lambda t: cmd.ListBenchmarks(
            t,
            device_id=device_id,
            package_id=package_id,
            runtime_target_id=runtime_target_id,
        ).execute(),
        action="benchmarks",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@hub_app.command("enroll")
def enroll(
    device: Annotated[str | None, typer.Argument(help="Device ID to enrol")] = None,
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    device_id: DeviceId = None,
    device_profile: DeviceProfile = None,
    labels: Annotated[
        list[str] | None, typer.Option("--label", help="key=value label (repeatable)")
    ] = None,
    inventory: Annotated[
        list[str] | None, typer.Option("--inventory", help="key=value inventory (repeatable)")
    ] = None,
    json_output: JsonOutput = False,
) -> None:
    """Enrol a device with the Hub."""
    from temms.cli.main import _parse_key_value_options

    target = device or device_id
    if target is None:
        console.print("[red]Device ID required[/red]")
        raise typer.Exit(1)
    _run(
        lambda t: cmd.EnrollDevice(
            t,
            device_id=target,
            device_profile=device_profile,
            labels=_parse_key_value_options(labels),
            inventory=_parse_key_value_options(inventory),
        ).execute(),
        action="enroll",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------


@hub_app.command("register-package")
def register_package(
    package_path: Annotated[Path, typer.Argument(help="Package directory or archive")],
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    require_signature: RequireSignature = True,
    signing_key: SigningKey = None,
    signing_key_file: SigningKeyFile = None,
    device_profile: DeviceProfile = None,
    strict_metadata: StrictMetadata = True,
    actor: Actor = None,
    json_output: JsonOutput = False,
) -> None:
    """Register a built package with the Hub."""
    _run(
        lambda t: cmd.RegisterPackage(
            t,
            package_path=package_path,
            require_signature=require_signature,
            signing_key=_signing_key(signing_key, signing_key_file),
            device_profile=device_profile,
            strict_metadata=strict_metadata,
            actor=actor,
        ).execute(),
        action="register-package",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


@hub_app.command("package-from-mlflow")
def package_from_mlflow(
    model_uri: Annotated[str, typer.Argument(help="MLflow model URI")],
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    slot: Slot = None,
    tracking_uri: Annotated[
        str | None, typer.Option("--tracking-uri", help="MLflow tracking URI")
    ] = None,
    device_profile: DeviceProfile = None,
    runtimes: Runtimes = None,
    providers: Providers = None,
    accelerators: Accelerators = None,
    model_artifact: Annotated[
        str | None, typer.Option("--model-artifact", help="Artifact path within the model")
    ] = None,
    require_schema: RequireSchema = True,
    require_signature: RequireSignature = True,
    signing_key: SigningKey = None,
    signing_key_file: SigningKeyFile = None,
    archive: Archive = True,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace an existing package")
    ] = False,
    strict_metadata: StrictMetadata = True,
    actor: Actor = None,
    json_output: JsonOutput = False,
) -> None:
    """Build a package from an MLflow model and register it."""
    if slot is None:
        console.print("[red]--slot is required[/red]")
        raise typer.Exit(1)
    _run(
        lambda t: cmd.BuildPackageFromMLflow(
            t,
            model_uri=model_uri,
            slot=slot,
            tracking_uri=tracking_uri,
            device_profile=device_profile,
            runtimes=runtimes,
            providers=providers,
            accelerators=accelerators,
            model_artifact=model_artifact,
            require_schema=require_schema,
            require_signature=require_signature,
            signing_key=_signing_key(signing_key, signing_key_file),
            archive=archive,
            overwrite=overwrite,
            strict_metadata=strict_metadata,
            actor=actor,
        ).execute(),
        action="package-from-mlflow",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


@hub_app.command("promote-package")
def promote_package(
    package_id: Annotated[str, typer.Argument(help="Package ID to promote")],
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    promotion_state: Annotated[
        str | None, typer.Option("--promotion-state", help="Target promotion state")
    ] = None,
    reason: Reason = None,
    actor: Actor = None,
    json_output: JsonOutput = False,
) -> None:
    """Move a package to a new promotion state."""
    _run(
        lambda t: cmd.PromotePackage(
            t, resource_id=package_id, state=promotion_state, reason=reason, actor=actor
        ).execute(),
        action="promote-package",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# Runtime targets
# ---------------------------------------------------------------------------


@hub_app.command("register-runtime")
def register_runtime(
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    runtime_target_id: RuntimeTargetId = None,
    image: Annotated[str | None, typer.Option("--image", help="Container image")] = None,
    os_name: Annotated[str | None, typer.Option("--os", help="Operating system")] = "linux",
    arch: Annotated[str | None, typer.Option("--arch", help="CPU architecture")] = None,
    device_profile: DeviceProfile = None,
    runtimes: Runtimes = None,
    providers: Providers = None,
    accelerators: Accelerators = None,
    labels: Annotated[
        list[str] | None, typer.Option("--label", help="key=value label (repeatable)")
    ] = None,
    actor: Actor = None,
    json_output: JsonOutput = False,
) -> None:
    """Register a runtime target."""
    from temms.cli.main import _parse_key_value_options

    if runtime_target_id is None or image is None:
        console.print("[red]--runtime-target-id and --image are required[/red]")
        raise typer.Exit(1)
    _run(
        lambda t: cmd.RegisterRuntimeTarget(
            t,
            runtime_target_id=runtime_target_id,
            image=image,
            os_name=os_name,
            arch=arch,
            device_profile=device_profile,
            runtimes=runtimes,
            providers=providers,
            accelerators=accelerators,
            labels=_parse_key_value_options(labels),
            actor=actor,
        ).execute(),
        action="register-runtime",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


@hub_app.command("preview-compatibility")
def preview_compatibility(
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    device_id: DeviceId = None,
    package_id: PackageId = None,
    runtime_target_id: RuntimeTargetId = None,
    model_id: ModelId = None,
    json_output: JsonOutput = False,
) -> None:
    """Check whether a package can run on a device."""
    if device_id is None or package_id is None:
        console.print("[red]--device-id and --package-id are required[/red]")
        raise typer.Exit(1)
    _run(
        lambda t: cmd.PreviewCompatibility(
            t,
            device_id=device_id,
            package_id=package_id,
            runtime_target_id=runtime_target_id,
            model_id=model_id,
        ).execute(),
        action="preview-compatibility",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
        policy=_policy("preview-compatibility", failure_key="compatible"),
    )


@hub_app.command("compatibility-matrix")
def compatibility_matrix(
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    package_id: PackageId = None,
    device_id: DeviceId = None,
    runtime_target_id: RuntimeTargetId = None,
    model_id: ModelId = None,
    include_device_inventory: Annotated[
        bool,
        typer.Option("--include-device-inventory", help="Include device inventory in the matrix"),
    ] = False,
    json_output: JsonOutput = False,
) -> None:
    """Show which packages can run where."""
    _run(
        lambda t: cmd.CompatibilityMatrix(
            t,
            package_id=package_id,
            device_id=device_id,
            runtime_target_id=runtime_target_id,
            model_id=model_id,
            include_device_inventory=include_device_inventory,
        ).execute(),
        action="compatibility-matrix",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# Rollouts
# ---------------------------------------------------------------------------


@hub_app.command("assign")
def assign(
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    device_id: DeviceId = None,
    package_id: PackageId = None,
    slot: Slot = None,
    rollout_id: Annotated[
        str | None, typer.Option("--rollout-id", help="Explicit rollout ID")
    ] = None,
    runtime_target_id: RuntimeTargetId = None,
    require_runtime_validation: RequireRuntimeValidation = False,
    require_approval: RequireApproval = False,
    actor: Actor = None,
    model_id: ModelId = None,
    json_output: JsonOutput = False,
) -> None:
    """Assign a package to a device."""
    if device_id is None or package_id is None:
        console.print("[red]--device-id and --package-id are required[/red]")
        raise typer.Exit(1)
    _run(
        lambda t: cmd.AssignRollout(
            t,
            device_id=device_id,
            package_id=package_id,
            slot=slot,
            rollout_id=rollout_id,
            runtime_target_id=runtime_target_id,
            require_runtime_validation=require_runtime_validation,
            require_approval=require_approval,
            actor=actor,
            model_id=model_id,
        ).execute(),
        action="assign",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


def _rollout_verb(name: str, command: type, help_text: str) -> None:
    """Register approve / rollback, which take the same arguments."""

    def run(
        rollout: Annotated[str | None, typer.Argument(help="Rollout ID")] = None,
        hub_url: HubUrl = DEFAULT_HUB_URL,
        token: Token = None,
        rollout_id: Annotated[
            str | None, typer.Option("--rollout-id", help="Rollout ID")
        ] = None,
        reason: Reason = None,
        actor: Actor = None,
        json_output: JsonOutput = False,
    ) -> None:
        target = rollout or rollout_id
        if target is None:
            console.print("[red]Rollout ID required[/red]")
            raise typer.Exit(1)
        _run(
            lambda t: command(t, resource_id=target, reason=reason, actor=actor).execute(),
            action=name,
            hub_url=hub_url,
            token=token,
            json_output=json_output,
        )

    run.__doc__ = help_text
    hub_app.command(name)(run)


_rollout_verb("approve", cmd.ApproveRollout, "Approve a held rollout.")
_rollout_verb("rollback", cmd.RollbackRollout, "Roll a rollout back.")


@hub_app.command("apply")
def apply_rollout(
    rollout: Annotated[str | None, typer.Argument(help="Rollout ID")] = None,
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    rollout_id: Annotated[str | None, typer.Option("--rollout-id", help="Rollout ID")] = None,
    require_signature: RequireSignature = True,
    signing_key: SigningKey = None,
    signing_key_file: SigningKeyFile = None,
    actor: Actor = None,
    json_output: JsonOutput = False,
) -> None:
    """Apply an approved rollout."""
    target = rollout or rollout_id
    if target is None:
        console.print("[red]Rollout ID required[/red]")
        raise typer.Exit(1)
    _run(
        lambda t: cmd.ApplyRollout(
            t,
            resource_id=target,
            require_signature=require_signature,
            signing_key=_signing_key(signing_key, signing_key_file),
            actor=actor,
        ).execute(),
        action="apply",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# Rollout plans
# ---------------------------------------------------------------------------


@hub_app.command("create-rollout-plan")
def create_rollout_plan(
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    package_id: PackageId = None,
    device_id: DeviceId = None,
    target_device_ids: Annotated[
        list[str] | None,
        typer.Option("--target-device-id", help="Device to include (repeatable)"),
    ] = None,
    plan_id: Annotated[str | None, typer.Option("--plan-id", help="Explicit plan ID")] = None,
    slot: Slot = None,
    runtime_target_id: RuntimeTargetId = None,
    batch_size: BatchSize = 1,
    require_runtime_validation: RequireRuntimeValidation = False,
    require_approval: RequireApproval = False,
    actor: Actor = None,
    model_id: ModelId = None,
    json_output: JsonOutput = False,
) -> None:
    """Create a staged rollout plan across several devices."""
    if package_id is None:
        console.print("[red]--package-id is required[/red]")
        raise typer.Exit(1)
    devices = list(target_device_ids or [])
    if device_id and device_id not in devices:
        devices.append(device_id)
    if not devices:
        console.print("[red]At least one --target-device-id or --device-id is required[/red]")
        raise typer.Exit(1)
    _run(
        lambda t: cmd.CreateRolloutPlan(
            t,
            package_id=package_id,
            device_ids=devices,
            plan_id=plan_id,
            slot=slot,
            runtime_target_id=runtime_target_id,
            batch_size=batch_size,
            require_runtime_validation=require_runtime_validation,
            require_approval=require_approval,
            actor=actor,
            model_id=model_id,
        ).execute(),
        action="create-rollout-plan",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


def _plan_verb(name: str, command: type, help_text: str) -> None:
    """Register pause / resume, which take the same arguments."""

    def run(
        plan: Annotated[str | None, typer.Argument(help="Rollout plan ID")] = None,
        hub_url: HubUrl = DEFAULT_HUB_URL,
        token: Token = None,
        plan_id: Annotated[
            str | None, typer.Option("--plan-id", help="Rollout plan ID")
        ] = None,
        reason: Reason = None,
        actor: Actor = None,
        json_output: JsonOutput = False,
    ) -> None:
        target = plan or plan_id
        if target is None:
            console.print("[red]Rollout plan ID required[/red]")
            raise typer.Exit(1)
        _run(
            lambda t: command(t, plan_id=target, reason=reason, actor=actor).execute(),
            action=name,
            hub_url=hub_url,
            token=token,
            json_output=json_output,
        )

    run.__doc__ = help_text
    hub_app.command(name)(run)


_plan_verb("pause-rollout-plan", cmd.PauseRolloutPlan, "Pause a rollout plan.")
_plan_verb("resume-rollout-plan", cmd.ResumeRolloutPlan, "Resume a paused rollout plan.")


@hub_app.command("advance-rollout-plan")
def advance_rollout_plan(
    plan: Annotated[str | None, typer.Argument(help="Rollout plan ID")] = None,
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    plan_id: Annotated[str | None, typer.Option("--plan-id", help="Rollout plan ID")] = None,
    batch_size: BatchSize = 1,
    actor: Actor = None,
    json_output: JsonOutput = False,
) -> None:
    """Advance a rollout plan by one batch."""
    target = plan or plan_id
    if target is None:
        console.print("[red]Rollout plan ID required[/red]")
        raise typer.Exit(1)
    _run(
        lambda t: cmd.AdvanceRolloutPlan(
            t, resource_id=target, batch_size=batch_size, actor=actor
        ).execute(),
        action="advance-rollout-plan",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# Telemetry and evidence
# ---------------------------------------------------------------------------


def _bundle_upload(name: str, command: type, help_text: str) -> None:
    """Register replay-telemetry / ingest-evidence, which take the same arguments."""

    def run(
        bundle: Annotated[Path, typer.Argument(help="Bundle JSON file")],
        hub_url: HubUrl = DEFAULT_HUB_URL,
        token: Token = None,
        device_id: DeviceId = None,
        actor: Actor = None,
        json_output: JsonOutput = False,
    ) -> None:
        _run(
            lambda t: command(
                t, bundle_path=bundle, device_id=device_id, actor=actor
            ).execute(),
            action=name,
            hub_url=hub_url,
            token=token,
            json_output=json_output,
        )

    run.__doc__ = help_text
    hub_app.command(name)(run)


_bundle_upload("replay-telemetry", cmd.ReplayTelemetry, "Replay a telemetry bundle into the Hub.")
_bundle_upload("ingest-evidence", cmd.IngestEvidence, "Ingest an evidence bundle into the Hub.")


# ---------------------------------------------------------------------------
# Air-gap
# ---------------------------------------------------------------------------


@hub_app.command("export")
def export_bundle(
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    include_packages: Annotated[
        bool, typer.Option("--include-packages", help="Embed package artifacts in the bundle")
    ] = False,
    output: Output = None,
    json_output: JsonOutput = False,
) -> None:
    """Export an air-gap bundle from the Hub."""
    _run(
        lambda t: cmd.ExportAirgapBundle(
            t, include_packages=include_packages, output=output
        ).execute(),
        action="export",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
        output=output,
    )


@hub_app.command("import")
def import_bundle(
    bundle: Annotated[Path, typer.Argument(help="Air-gap bundle JSON file")],
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    json_output: JsonOutput = False,
) -> None:
    """Import an air-gap bundle into the Hub."""
    _run(
        lambda t: cmd.ImportAirgapBundle(t, bundle_path=bundle).execute(),
        action="import",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# Readiness and the edge-runtime proof
#
# These are the only actions with proof gates, so gate evaluation lives here
# rather than in the shared path every other action would pay for.
# ---------------------------------------------------------------------------


def _readiness_command(name: str, help_text: str, *, mission_only: bool) -> None:
    def run(
        hub_url: HubUrl = DEFAULT_HUB_URL,
        token: Token = None,
        package_id: PackageId = None,
        model_id: ModelId = None,
        device_id: DeviceId = None,
        runtime_target_id: RuntimeTargetId = None,
        slot: Slot = None,
        require_go: RequireGo = False,
        min_runtime_fit: MinRuntimeFit = None,
        require_best_runtime: RequireBestRuntime = False,
        require_capability_lock: RequireCapabilityLock = False,
        signing_key: SigningKey = None,
        signing_key_file: SigningKeyFile = None,
        output: Output = None,
        json_output: JsonOutput = False,
    ) -> None:
        _run_readiness(
            action=name,
            mission_only=mission_only,
            hub_url=hub_url,
            token=token,
            package_id=package_id,
            model_id=model_id,
            device_id=device_id,
            runtime_target_id=runtime_target_id,
            slot=slot,
            gates=_Gates(
                require_go, min_runtime_fit, require_best_runtime, require_capability_lock
            ),
            signing_key=_signing_key(signing_key, signing_key_file),
            output=output,
            json_output=json_output,
        )

    run.__doc__ = help_text
    hub_app.command(name)(run)


class _Gates(NamedTuple):
    """The four proof-gate flags, passed as one value rather than four."""

    require_go: bool
    min_runtime_fit: float | None
    require_best_runtime: bool
    require_capability_lock: bool


def _run_readiness(
    *,
    action: str,
    mission_only: bool,
    hub_url: str,
    token: str | None,
    package_id: str | None,
    model_id: str | None,
    device_id: str | None,
    runtime_target_id: str | None,
    slot: str | None,
    gates: _Gates,
    signing_key: str | None,
    output: Path | None,
    json_output: bool,
) -> None:
    import httpx

    from temms.cli.main import _hub_api_url, _hub_auth_headers, _hub_edge_runtime_proof_payload
    from temms.core import proof_gates

    try:
        with httpx.Client(
            base_url=_hub_api_url(hub_url), headers=_hub_auth_headers(token), timeout=30.0
        ) as client:
            result = cmd.Readiness(
                HttpHubTransport(client),
                package_id=package_id,
                model_id=model_id,
                device_id=device_id,
                runtime_target_id=runtime_target_id,
                slot=slot,
                mission_only=mission_only,
            ).execute()
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"[red]Hub command failed: {exc}[/red]")
        raise typer.Exit(1) from exc

    failures = proof_gates.proof_gate_failures(
        action,
        result.payload,
        require_go=gates.require_go,
        min_runtime_fit=gates.min_runtime_fit,
        require_best_runtime=gates.require_best_runtime,
        require_capability_lock=gates.require_capability_lock,
        runtime_context=result.proof,
    )
    emitter = _emitter(json_output=json_output, output=output)
    if output is not None:
        emitter.write_proof(
            _hub_edge_runtime_proof_payload(
                action=action,
                payload=result.payload,
                readiness=result.proof or result.payload,
                require_go=gates.require_go,
                min_runtime_fit=gates.min_runtime_fit,
                require_best_runtime=gates.require_best_runtime,
                require_capability_lock=gates.require_capability_lock,
                gate_failures=failures,
                signing_key=signing_key,
            )
        )
    emitter.emit(result, _policy(action))
    if failures:
        emitter.report_gate_failures(failures)
        raise typer.Exit(1)


_readiness_command("readiness", "Show edge readiness.", mission_only=False)
_readiness_command(
    "edge-runtime-mission", "Show the edge runtime mission block.", mission_only=True
)


@hub_app.command("verify-edge-proof")
def verify_edge_proof(
    proof: Annotated[Path, typer.Argument(help="Edge-runtime proof JSON file")],
    require_go: RequireGo = False,
    min_runtime_fit: MinRuntimeFit = None,
    require_best_runtime: RequireBestRuntime = False,
    require_capability_lock: RequireCapabilityLock = False,
    max_proof_age_seconds: Annotated[
        int | None, typer.Option("--max-proof-age-seconds", help="Reject proofs older than this")
    ] = None,
    package_id: PackageId = None,
    model_id: ModelId = None,
    device_id: DeviceId = None,
    runtime_target_id: RuntimeTargetId = None,
    slot: Slot = None,
    signing_key: SigningKey = None,
    signing_key_file: SigningKeyFile = None,
    require_proof_signature: Annotated[
        bool, typer.Option("--require-proof-signature", help="Refuse unsigned proofs")
    ] = False,
    json_output: JsonOutput = False,
) -> None:
    """Verify an edge-runtime proof. Offline: this never contacts the Hub."""
    import json as _json

    from temms.cli.main import _print_edge_runtime_proof_verification, _verify_edge_runtime_proof

    payload = _verify_edge_runtime_proof(
        proof,
        require_go=require_go,
        min_runtime_fit=min_runtime_fit,
        require_best_runtime=require_best_runtime,
        require_capability_lock=require_capability_lock,
        max_proof_age_seconds=max_proof_age_seconds,
        expected_path={
            "package_id": package_id,
            "model_id": model_id,
            "device_id": device_id,
            "runtime_target_id": runtime_target_id,
            "slot": slot,
        },
        signing_key=_signing_key(signing_key, signing_key_file),
        require_attestation=require_proof_signature,
    )
    if json_output:
        typer.echo(_json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_edge_runtime_proof_verification(payload)
    if not payload.get("valid") or payload.get("requested_gate_failures"):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Runtime validation
# ---------------------------------------------------------------------------


@hub_app.command("validate-runtime")
def validate_runtime(
    package_path: Annotated[Path, typer.Argument(help="Package directory or archive")],
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    runtime_target_id: RuntimeTargetId = None,
    package_id: PackageId = None,
    require_signature: RequireSignature = True,
    strict_metadata: StrictMetadata = True,
    signing_key: SigningKey = None,
    signing_key_file: SigningKeyFile = None,
    pull_image: Annotated[
        bool, typer.Option("--pull-image", help="Pull the runtime image first")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan the validation without running it")
    ] = False,
    local_runtime: Annotated[
        bool, typer.Option("--local-runtime", help="Validate against the local runtime")
    ] = False,
    timeout_s: Annotated[
        int, typer.Option("--timeout-s", help="Validation timeout in seconds")
    ] = 300,
    actor: Actor = None,
    json_output: JsonOutput = False,
) -> None:
    """Validate a package against a runtime target and record the result."""
    from temms.cli.main import _find_runtime_target
    from temms.core.runtime_target_runner import validate_runtime_target_package

    if runtime_target_id is None:
        console.print("[red]--runtime-target-id is required[/red]")
        raise typer.Exit(1)

    key = _signing_key(signing_key, signing_key_file)

    def validator(target: Any, path: Path) -> Any:
        return validate_runtime_target_package(
            target,
            path,
            require_signature=require_signature,
            strict_metadata=strict_metadata,
            signing_key=key,
            signing_key_file=signing_key_file,
            pull_image=pull_image,
            dry_run=dry_run,
            local=local_runtime,
            timeout_s=timeout_s,
        )

    _run(
        lambda t: cmd.ValidateRuntimeTarget(
            t,
            package_path=package_path,
            runtime_target_id=runtime_target_id,
            validator=validator,
            find_target=_find_runtime_target,
            package_id=package_id,
            actor=actor,
        ).execute(),
        action="validate-runtime",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
        policy=_policy("validate-runtime", failure_key="ok"),
    )


# ---------------------------------------------------------------------------
# Mission packages
# ---------------------------------------------------------------------------


def _mission_package_command(name: str, help_text: str, *, download: bool) -> None:
    def run(
        mission: Annotated[
            Path | None, typer.Argument(help="Mission YAML file")
        ] = None,
        hub_url: HubUrl = DEFAULT_HUB_URL,
        token: Token = None,
        package_id: PackageId = None,
        model_id: ModelId = None,
        device_id: DeviceId = None,
        runtime_target_id: RuntimeTargetId = None,
        slot: Slot = None,
        goal: Annotated[str | None, typer.Option("--goal", help="Mission goal")] = None,
        mission_yaml: Annotated[
            str | None, typer.Option("--mission-yaml", help="Inline mission YAML")
        ] = None,
        mission_yaml_file: Annotated[
            Path | None, typer.Option("--mission-yaml-file", help="Mission YAML file")
        ] = None,
        sensor: Annotated[str | None, typer.Option("--sensor", help="Sensor name")] = None,
        latency_budget_ms: Annotated[
            float | None, typer.Option("--latency-budget-ms", help="Latency budget")
        ] = None,
        min_throughput_ips: Annotated[
            float | None, typer.Option("--min-throughput-ips", help="Minimum throughput")
        ] = None,
        switch_policy: Annotated[
            str | None, typer.Option("--switch-policy", help="Model switch policy")
        ] = None,
        confidence_threshold: Annotated[
            float | None, typer.Option("--confidence-threshold", help="Confidence threshold")
        ] = None,
        fallback_model_id: Annotated[
            str | None, typer.Option("--fallback-model-id", help="Fallback model ID")
        ] = None,
        ddil_mode: Annotated[
            str | None, typer.Option("--ddil-mode", help="DDIL mode")
        ] = None,
        require_go: RequireGo = False,
        min_runtime_fit: MinRuntimeFit = None,
        require_best_runtime: RequireBestRuntime = False,
        require_capability_lock: RequireCapabilityLock = False,
        require_proof_signature: Annotated[
            bool, typer.Option("--require-proof-signature", help="Require a signed proof")
        ] = False,
        output: Output = None,
        json_output: JsonOutput = False,
    ) -> None:
        from temms.cli.main import _hub_mission_package_request_body

        request = _hub_mission_package_request_body(
            source=str(mission) if mission else None,
            package_id=package_id,
            model_id=model_id,
            device_id=device_id,
            runtime_target_id=runtime_target_id,
            slot=slot,
            goal=goal,
            mission_yaml=mission_yaml,
            mission_yaml_file=mission_yaml_file,
            sensor=sensor,
            latency_budget_ms=latency_budget_ms,
            min_throughput_ips=min_throughput_ips,
            switch_policy=switch_policy,
            confidence_threshold=confidence_threshold,
            fallback_model_id=fallback_model_id,
            ddil_mode=ddil_mode,
            require_go=require_go,
            min_runtime_fit=min_runtime_fit,
            require_best_runtime=require_best_runtime,
            require_capability_lock=require_capability_lock,
            require_proof_signature=require_proof_signature,
        )
        _run(
            lambda t: cmd.MissionPackagePlan(
                t, request=request, download=download
            ).execute(),
            action=name,
            hub_url=hub_url,
            token=token,
            json_output=json_output,
            output=output,
            policy=_policy(name, writes_payload_to_output=True),
        )

    run.__doc__ = help_text
    hub_app.command(name)(run)


_mission_package_command(
    "mission-package-plan", "Plan a mission package.", download=False
)
_mission_package_command(
    "mission-package-download", "Plan and download a mission package.", download=True
)


@hub_app.command("mission-package-stage")
def mission_package_stage(
    artifact: Annotated[Path, typer.Argument(help="Mission package artifact")],
    hub_url: HubUrl = DEFAULT_HUB_URL,
    token: Token = None,
    rollout_id: Annotated[
        str | None, typer.Option("--rollout-id", help="Rollout ID")
    ] = None,
    actor: Actor = None,
    reason: Reason = None,
    json_output: JsonOutput = False,
) -> None:
    """Stage a mission package artifact for a rollout."""
    from temms.cli.main import _hub_mission_package_stage_request

    request = _hub_mission_package_stage_request(
        source=str(artifact), rollout_id=rollout_id, actor=actor, reason=reason
    )
    _run(
        lambda t: cmd.MissionPackageStage(t, request=request).execute(),
        action="mission-package-stage",
        hub_url=hub_url,
        token=token,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signing_key(signing_key: str | None, signing_key_file: Path | None) -> str | None:
    from temms.cli.main import _hub_package_signing_key
    from temms.core.signing import read_signing_key

    return _hub_package_signing_key(signing_key, signing_key_file, read_signing_key)


__all__ = ["hub_app"]
