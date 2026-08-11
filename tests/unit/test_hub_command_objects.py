"""Hub commands as objects — no CLI, no httpx, no patching.

This is the point of the decomposition. Previously a hub action could only be
exercised through `runner.invoke(app, ["hub", ...])` with `httpx.Client`
monkeypatched, because the action was a branch inside an 836-line function that
built its own client. Now a command receives its transport and its own inputs,
so a test is three lines.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from temms.cli.hub import commands
from temms.cli.hub.commands import (
    DeploymentStatus,
    EnrollDevice,
    ExportAirgapBundle,
    ImportAirgapBundle,
    ListBenchmarks,
    ListDevices,
    ListPackages,
    ListRuntimeValidations,
    PauseRolloutPlan,
    Readiness,
    ResumeRolloutPlan,
)
from temms.cli.hub.transport import HttpHubTransport, HubTransportError


class FakeTransport:
    """Records requests; returns a canned payload."""

    def __init__(self, payload: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self._payload = payload if payload is not None else {"ok": True}

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        return self._payload

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return self._payload


# -- transport -------------------------------------------------------------


class _Response:
    def __init__(self, status_code=200, payload=None, text="err"):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_transport_returns_json_on_success():
    class Client:
        def get(self, path, params=None):
            return _Response(200, {"devices": []})

    assert HttpHubTransport(Client()).get("/devices") == {"devices": []}


def test_transport_raises_with_status_and_body():
    class Client:
        def post(self, path, json=None):
            return _Response(409, text="conflict")

    with pytest.raises(HubTransportError, match="HTTP 409: conflict"):
        HttpHubTransport(Client()).post("/devices/enroll", json={})


# -- listings --------------------------------------------------------------


@pytest.mark.parametrize(
    "command,path",
    [
        (ListDevices, "/devices"),
        (ListPackages, "/packages"),
        (DeploymentStatus, "/deployment-status"),
    ],
)
def test_listing_commands_take_no_inputs(command, path):
    """Eight actions need zero options; their constructors reflect that."""
    transport = FakeTransport()

    result = command(transport).execute()

    assert transport.calls == [("GET", path, None)]
    assert result.payload == {"ok": True}
    assert result.handled is False


def test_filters_are_omitted_when_not_supplied():
    transport = FakeTransport()

    ListBenchmarks(transport, device_id="edge-1").execute()

    assert transport.calls == [("GET", "/benchmarks", {"device_id": "edge-1"})]


def test_no_filters_sends_none_rather_than_empty_dict():
    transport = FakeTransport()

    ListRuntimeValidations(transport).execute()

    assert transport.calls == [("GET", "/runtime-targets/validations", None)]


# -- writes ----------------------------------------------------------------


def test_enroll_sends_only_its_own_inputs():
    transport = FakeTransport()

    EnrollDevice(
        transport,
        device_id="edge-1",
        device_profile="arm64-cpu",
        labels={"site": "north"},
    ).execute()

    _, path, body = transport.calls[0]
    assert path == "/devices/enroll"
    assert body == {
        "device_id": "edge-1",
        "profile": "arm64-cpu",
        "labels": {"site": "north"},
        "inventory": {},
    }


@pytest.mark.parametrize(
    "command,verb", [(PauseRolloutPlan, "pause"), (ResumeRolloutPlan, "resume")]
)
def test_plan_lifecycle_shares_one_implementation(command, verb):
    transport = FakeTransport()

    command(transport, plan_id="plan-7", reason="storm", actor="op:jo").execute()

    _, path, body = transport.calls[0]
    assert path == f"/rollout-plans/plan-7/{verb}"
    assert body == {"reason": "storm", "actor": "op:jo"}


def test_import_posts_the_bundle_contents(tmp_path):
    bundle = {"schema_version": "temms-airgap/v1", "packages": []}
    path = tmp_path / "b.json"
    path.write_text(json.dumps(bundle))
    transport = FakeTransport()

    ImportAirgapBundle(transport, bundle_path=path).execute()

    assert transport.calls == [("POST", "/airgap/import", bundle)]


def test_export_without_output_is_not_handled():
    result = ExportAirgapBundle(FakeTransport({"packages": []})).execute()
    assert result.handled is False


def test_export_to_a_file_marks_itself_handled(tmp_path):
    """`handled` replaces the bare `return` that used to exit hub() directly."""
    out = tmp_path / "bundle.json"

    result = ExportAirgapBundle(
        FakeTransport({"packages": []}), include_packages=True, output=out
    ).execute()

    assert result.handled is True
    assert json.loads(out.read_text()) == {"packages": []}
    assert any("Hub bundle written" in m for m in result.messages)


# -- readiness -------------------------------------------------------------


def test_readiness_carries_the_full_document_as_proof():
    doc = {"status": "go", "edge_runtime_mission": {"goal": "detect"}}

    result = Readiness(FakeTransport(doc), device_id="edge-1").execute()

    assert result.payload == doc
    assert result.proof == doc


def test_mission_mode_narrows_the_payload_but_keeps_the_proof():
    doc = {"status": "go", "edge_runtime_mission": {"goal": "detect"}}

    result = Readiness(FakeTransport(doc), mission_only=True).execute()

    assert result.payload == {"goal": "detect"}
    assert result.proof == doc  # the proof still needs the whole document


def test_readiness_without_a_mission_block_yields_empty_payload():
    result = Readiness(FakeTransport({"status": "go"}), mission_only=True).execute()
    assert result.payload == {}


# -- emission policy -------------------------------------------------------


class FakeConsole:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, text=""):
        self.lines.append(str(text))


def _emitter(**kwargs):
    from temms.cli.hub.emit import ResultEmitter

    console = FakeConsole()
    echoed: list[str] = []
    return ResultEmitter(console=console, echo=echoed.append, **kwargs), console, echoed


def test_failure_key_replaces_per_action_special_casing():
    """`if action == "validate-runtime" and not payload["ok"]` becomes policy."""
    from temms.cli.hub.commands import HubResult
    from temms.cli.hub.emit import EmissionPolicy

    emitter, _, _ = _emitter(json_output=True)
    policy = EmissionPolicy(failure_key="ok")

    assert emitter.emit(HubResult({"ok": True}), policy) is False
    assert emitter.emit(HubResult({"ok": False}), policy) is True


def test_absent_failure_key_never_fails():
    from temms.cli.hub.commands import HubResult
    from temms.cli.hub.emit import EmissionPolicy

    emitter, _, _ = _emitter(json_output=True)
    assert emitter.emit(HubResult({"anything": False}), EmissionPolicy()) is False


def test_json_output_bypasses_the_renderer():
    from temms.cli.hub.commands import HubResult
    from temms.cli.hub.emit import EmissionPolicy

    rendered: list[dict] = []
    emitter, _, echoed = _emitter(json_output=True)

    emitter.emit(HubResult({"a": 1}), EmissionPolicy(renderer=rendered.append))

    assert rendered == []
    assert json.loads(echoed[0]) == {"a": 1}


def test_renderer_runs_when_not_json():
    from temms.cli.hub.commands import HubResult
    from temms.cli.hub.emit import EmissionPolicy

    rendered: list[dict] = []
    emitter, _, echoed = _emitter(json_output=False)

    emitter.emit(HubResult({"a": 1}), EmissionPolicy(renderer=rendered.append))

    assert rendered == [{"a": 1}]
    assert echoed == []


def test_handled_result_skips_emission_entirely():
    from temms.cli.hub.commands import HubResult
    from temms.cli.hub.emit import EmissionPolicy

    rendered: list[dict] = []
    emitter, _, echoed = _emitter(json_output=False)

    failed = emitter.emit(
        HubResult({"a": 1}, handled=True), EmissionPolicy(renderer=rendered.append)
    )

    assert (rendered, echoed, failed) == ([], [], False)


def test_payload_is_written_to_output_when_policy_says_so(tmp_path):
    from temms.cli.hub.commands import HubResult
    from temms.cli.hub.emit import EmissionPolicy

    out = tmp_path / "plan.json"
    emitter, _, _ = _emitter(json_output=True, output=out)

    emitter.emit(HubResult({"plan": 1}), EmissionPolicy(writes_payload_to_output=True))

    assert json.loads(out.read_text()) == {"plan": 1}


def test_gate_failures_are_quiet_in_json_mode():
    emitter, console, _ = _emitter(json_output=True)
    emitter.report_gate_failures(["fit too low"])
    assert console.lines == []

    emitter, console, _ = _emitter(json_output=False)
    emitter.report_gate_failures(["fit too low"])
    assert any("fit too low" in line for line in console.lines)


# ---------------------------------------------------------------------------
# Lifecycle verbs: the base class means the interesting part of each subclass
# is its path and body, so that is what these assert.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected_path", "expected_body"),
    [
        (
            lambda t: commands.ApproveRollout(t, resource_id="r1", reason="ok", actor="op"),
            "/rollouts/r1/approve",
            {"reason": "ok", "actor": "op"},
        ),
        (
            lambda t: commands.RollbackRollout(t, resource_id="r1", reason="bad", actor="op"),
            "/rollouts/r1/rollback",
            {"reason": "bad", "actor": "op"},
        ),
        (
            lambda t: commands.ApplyRollout(
                t, resource_id="r1", require_signature=True, signing_key="K", actor="op"
            ),
            "/rollouts/r1/apply",
            {"require_signature": True, "signing_key": "K", "actor": "op"},
        ),
        (
            lambda t: commands.AdvanceRolloutPlan(t, resource_id="p1", batch_size=5, actor="op"),
            "/rollout-plans/p1/advance",
            {"limit": 5, "actor": "op"},
        ),
        (
            lambda t: commands.PromotePackage(
                t, resource_id="pkg", state="released", reason="r", actor="op"
            ),
            "/packages/pkg/promote",
            {"state": "released", "reason": "r", "actor": "op"},
        ),
    ],
    ids=["approve", "rollback", "apply", "advance-plan", "promote"],
)
def test_lifecycle_verbs_post_their_own_path_and_body(command, expected_path, expected_body):
    transport = FakeTransport({"ok": True})
    command(transport).execute()
    assert transport.calls == [("POST", expected_path, expected_body)]


def test_apply_rollout_does_not_leak_the_approval_body():
    """Apply and approve share a base class but must not share a body shape."""
    transport = FakeTransport({"ok": True})
    commands.ApplyRollout(transport, resource_id="r1", actor="op").execute()
    (_, _, body) = transport.calls[0]
    assert "reason" not in body
    assert set(body) == {"require_signature", "signing_key", "actor"}


# ---------------------------------------------------------------------------
# Bundle uploads.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("factory", "expected_path"),
    [
        (commands.ReplayTelemetry, "/telemetry/replay"),
        (commands.IngestEvidence, "/evidence/ingest"),
    ],
    ids=["telemetry", "evidence"],
)
def test_bundle_upload_posts_the_parsed_file(tmp_path, factory, expected_path):
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"records": [1, 2]}), encoding="utf-8")

    transport = FakeTransport({"ok": True})
    factory(transport, bundle_path=bundle, device_id="edge-1", actor="op").execute()

    assert transport.calls == [
        (
            "POST",
            expected_path,
            {"bundle": {"records": [1, 2]}, "device_id": "edge-1", "actor": "op"},
        )
    ]


# ---------------------------------------------------------------------------
# Compatibility: optional fields are omitted, not sent as null.
# ---------------------------------------------------------------------------


def test_preview_compatibility_omits_model_id_when_absent():
    transport = FakeTransport({"compatible": True})
    commands.PreviewCompatibility(transport, device_id="d", package_id="p").execute()
    (_, _, body) = transport.calls[0]
    assert "model_id" not in body


def test_compatibility_matrix_wraps_each_filter_in_a_list():
    transport = FakeTransport({"rows": []})
    commands.CompatibilityMatrix(
        transport, package_id="p", device_id="d", runtime_target_id="rt", model_id="m"
    ).execute()
    (_, _, body) = transport.calls[0]
    assert body["package_ids"] == ["p"]
    assert body["device_ids"] == ["d"]
    assert body["runtime_target_ids"] == ["rt"]
    assert body["model_ids"] == ["m"]


def test_compatibility_matrix_sends_null_for_unfiltered_dimensions():
    """A null dimension means "all"; an empty list would mean "none"."""
    transport = FakeTransport({"rows": []})
    commands.CompatibilityMatrix(transport, package_id="p").execute()
    (_, _, body) = transport.calls[0]
    assert body["device_ids"] is None
    assert body["runtime_target_ids"] is None


# ---------------------------------------------------------------------------
# Creation.
# ---------------------------------------------------------------------------


def test_create_rollout_plan_rejects_an_empty_target_set():
    """A plan with no devices is meaningless, so it fails before the network."""
    transport = FakeTransport({})
    with pytest.raises(ValueError, match="at least one target device"):
        commands.CreateRolloutPlan(transport, package_id="p", device_ids=[])
    assert transport.calls == []


def test_register_package_expands_the_package_path():
    transport = FakeTransport({"package_id": "p"})
    commands.RegisterPackage(transport, package_path=Path("~/pkg.tar.gz")).execute()
    (_, _, body) = transport.calls[0]
    assert not body["package_path"].startswith("~")
    assert body["package_path"].endswith("/pkg.tar.gz")


def test_register_runtime_target_marks_declared_capabilities_available():
    transport = FakeTransport({"runtime_target_id": "rt"})
    commands.RegisterRuntimeTarget(
        transport,
        runtime_target_id="rt",
        image="img",
        runtimes=["onnxruntime"],
        providers=["CPUExecutionProvider"],
        accelerators=["cuda"],
    ).execute()
    (_, _, body) = transport.calls[0]
    assert body["runtimes"]["onnxruntime"] == {
        "available": True,
        "providers": ["CPUExecutionProvider"],
    }
    assert body["accelerators"] == {"cuda": {"available": True}}
    assert body["runtime_constraints"]["preferred_providers"] == ["CPUExecutionProvider"]


def test_register_runtime_target_records_providers_without_a_declared_runtime():
    """Providers imply onnxruntime, so an operator need not restate it."""
    transport = FakeTransport({"runtime_target_id": "rt"})
    commands.RegisterRuntimeTarget(
        transport, runtime_target_id="rt", image="img", providers=["CPUExecutionProvider"]
    ).execute()
    (_, _, body) = transport.calls[0]
    assert body["runtimes"]["onnxruntime"]["available"] is True


def test_build_package_from_mlflow_defaults_to_strict_metadata():
    """The CLI's default is strict; the object must not silently relax it."""
    transport = FakeTransport({"package_id": "p"})
    commands.BuildPackageFromMLflow(transport, model_uri="models:/m/1", slot="vision").execute()
    (_, _, body) = transport.calls[0]
    assert body["strict_metadata"] is True


# ---------------------------------------------------------------------------
# Mission packages: the endpoint is the only difference between plan and
# download, and that difference is a flag rather than an action-string branch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("download", "expected_path"),
    [(False, "/mission-package/plan"), (True, "/mission-package/download")],
    ids=["plan", "download"],
)
def test_mission_package_plan_selects_its_endpoint(download, expected_path):
    transport = FakeTransport({"plan": {}})
    commands.MissionPackagePlan(
        transport, request={"mission": "m"}, download=download
    ).execute()
    assert transport.calls == [("POST", expected_path, {"mission": "m"})]
