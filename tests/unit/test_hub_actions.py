"""Hub action handlers, tested without the CLI (hub() decomposition).

The point of splitting hub()'s 33-branch chain into a dispatch table: each action
is now a small function that can be exercised directly with a fake client and an
option bundle — no Typer runner, no argument parsing, no 60-parameter invocation.
That is what makes the actions verifiable at all; previously every path ran
through one 836-line, complexity-129 function.
"""

from __future__ import annotations

import pytest
import typer

from temms.cli.main import HUB_ACTIONS, HubActionContext, HubActionResult


class FakeResponse:
    status_code = 200
    text = "ok"

    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


class FakeClient:
    """Records requests so a handler's wire behaviour can be asserted."""

    def __init__(self, payload=None):
        self.calls: list[tuple] = []
        self._payload = payload

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        return FakeResponse(self._payload)

    def post(self, path, json=None, params=None):
        self.calls.append(("POST", path, json))
        return FakeResponse(self._payload)


def make_ctx(**overrides) -> HubActionContext:
    """An option bundle with everything defaulted to None."""
    fields = {f: None for f in HubActionContext.__dataclass_fields__}
    fields.update(overrides)
    return HubActionContext(**fields)


# -- the dispatch table itself --------------------------------------------


def test_every_advertised_action_has_a_handler():
    assert len(HUB_ACTIONS) == 35
    assert all(callable(h) for h in HUB_ACTIONS.values())


def test_readiness_and_mission_share_one_handler():
    """Two actions, one implementation — the chain used a combined branch."""
    assert HUB_ACTIONS["readiness"] is HUB_ACTIONS["edge-runtime-mission"]
    assert HUB_ACTIONS["mission-package-plan"] is HUB_ACTIONS["mission-package-download"]


# -- individual handlers, no CLI involved ---------------------------------


@pytest.mark.parametrize(
    "action,path",
    [
        ("devices", "/devices"),
        ("packages", "/packages"),
        ("rollouts", "/rollouts"),
        ("runtime-targets", "/runtime-targets"),
        ("telemetry", "/telemetry"),
        ("evidence", "/evidence"),
        ("status", "/deployment-status"),
    ],
)
def test_listing_handlers_issue_a_get(action, path):
    client = FakeClient()
    result = HUB_ACTIONS[action](make_ctx(action=action), client)

    assert isinstance(result, HubActionResult)
    assert client.calls == [("GET", path, None)]


def test_enroll_builds_the_device_body():
    client = FakeClient()
    ctx = make_ctx(action="enroll", device_id="edge-1", device_profile="arm64-cpu",
                   labels=["site=north"], inventory=["ram=8192"])

    HUB_ACTIONS["enroll"](ctx, client)

    verb, path, body = client.calls[0]
    assert (verb, path) == ("POST", "/devices/enroll")
    assert body["device_id"] == "edge-1"
    assert body["profile"] == "arm64-cpu"
    assert body["labels"] == {"site": "north"}


def test_enroll_without_device_id_exits_before_any_request():
    client = FakeClient()
    with pytest.raises(typer.Exit):
        HUB_ACTIONS["enroll"](make_ctx(action="enroll"), client)
    assert client.calls == []


def test_benchmarks_only_sends_the_filters_it_was_given():
    client = FakeClient()
    HUB_ACTIONS["benchmarks"](make_ctx(action="benchmarks", device_id="edge-1"), client)

    _, path, params = client.calls[0]
    assert path == "/benchmarks"
    assert params == {"device_id": "edge-1"}


def test_readiness_carries_the_proof_payload_through():
    """The one handler that returns more than a payload."""
    client = FakeClient({"edge_runtime_mission": {"m": 1}, "status": "go"})

    result = HUB_ACTIONS["readiness"](make_ctx(action="readiness"), client)

    assert result.readiness_proof == {"edge_runtime_mission": {"m": 1}, "status": "go"}
    assert result.payload == result.readiness_proof


def test_edge_runtime_mission_narrows_to_the_mission_block():
    client = FakeClient({"edge_runtime_mission": {"goal": "detect"}, "status": "go"})

    result = HUB_ACTIONS["edge-runtime-mission"](
        make_ctx(action="edge-runtime-mission"), client
    )

    assert result.payload == {"goal": "detect"}
    # the full readiness document is still carried for proof generation
    assert result.readiness_proof["status"] == "go"


@pytest.mark.parametrize("action", ["pause-rollout-plan", "resume-rollout-plan"])
def test_plan_lifecycle_posts_reason_and_actor(action):
    client = FakeClient()
    ctx = make_ctx(action=action, source="plan-9", reason="storm", actor="op:jo")

    HUB_ACTIONS[action](ctx, client)

    verb, path, body = client.calls[0]
    assert verb == "POST"
    assert path.endswith(f"/{action.split('-')[0]}")
    assert body == {"reason": "storm", "actor": "op:jo"}


def test_export_that_writes_a_file_marks_itself_handled(tmp_path):
    """`handled` replaces the old bare `return` out of hub()."""
    out = tmp_path / "bundle.json"
    client = FakeClient({"packages": []})

    result = HUB_ACTIONS["export"](
        make_ctx(action="export", output=out, include_packages=True), client
    )

    assert result.handled is True
    assert out.is_file()


def test_export_without_output_is_not_handled():
    client = FakeClient({"packages": []})
    result = HUB_ACTIONS["export"](make_ctx(action="export"), client)
    assert result.handled is False
