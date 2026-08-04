"""Edge-runtime proof gates — one implementation, one verdict.

The property under test is an *equivalence*: the Hub (enforcing a proof) and the
CLI (verifying one offline) must never disagree about whether a proof passes. If
they can disagree, an operator in the field can verify a proof the Hub would
block, and the signed-proof guarantee is hollow.

This suite pins that equivalence across payload shapes, and covers the specific
drift that motivated the shared module: a capability lock carried at the *top
level* of the runtime context resolved in the CLI but not in the Hub, so the same
proof was PASS offline and BLOCK at the Hub.
"""

from __future__ import annotations

import pytest

from temms.core import proof_gates
from temms.core.proof_gates import (
    proof_gate_failures,
    runtime_capability_lock,
    runtime_capability_lock_failures,
    runtime_fit_score,
    runtime_target_best_failures,
    runtime_target_selection,
)

VALID_LOCK = {"status": "locked", "capability_sha256": "a" * 64}
BEST_SELECTION = {
    "status": "best",
    "selected_runtime_target_id": "temms-arm64-cpu",
    "best_runtime_target_id": "temms-arm64-cpu",
    "score_delta": 0,
}

# Every placement a proof may legitimately carry its evidence in.
PLACEMENTS = {
    "top_level": lambda field, value: {field: value},
    "edge_execution_contract": lambda field, value: {"edge_execution_contract": {field: value}},
    "runtime_decision": lambda field, value: {"runtime_decision": {field: value}},
    "runtime_fit": lambda field, value: {"runtime_fit": {field: value}},
    "readiness_contract": lambda field, value: {
        "readiness": {"edge_execution_contract": {field: value}}
    },
    "readiness_runtime_fit": lambda field, value: {"readiness": {"runtime_fit": {field: value}}},
}


# -- one implementation, structurally ------------------------------------


def test_cli_and_hub_route_through_the_shared_module():
    """The property that prevents the original bug from returning.

    Comparing cli(x) == hub(x) was meaningful while each kept its own copy. Now
    both call temms.core.proof_gates directly, so that comparison would be
    f(x) == f(x) -- a tautology. What still has teeth is the structural claim:
    neither module may hold its own gate implementation.
    """
    import temms.cli.main as cli_mod
    import temms.hub_lite as hub_mod

    assert cli_mod.proof_gates is proof_gates
    assert hub_mod.proof_gates is proof_gates

    # And neither may define a competing local implementation.
    for mod in (cli_mod, hub_mod):
        for name in dir(mod):
            if "gate_failures" in name or "capability_lock_for" in name:
                attr = getattr(mod, name)
                assert getattr(attr, "__module__", None) != mod.__name__, (
                    f"{mod.__name__}.{name} is a local gate implementation; "
                    "gates must live only in temms.core.proof_gates"
                )


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
def test_capability_lock_resolves_from_every_placement(placement):
    """The drift that motivated the module: a top-level lock must resolve too."""
    ctx = PLACEMENTS[placement]("runtime_capability_lock", VALID_LOCK)
    assert runtime_capability_lock_failures(ctx) == []


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
def test_target_selection_resolves_from_every_placement(placement):
    ctx = PLACEMENTS[placement]("target_selection", BEST_SELECTION)
    assert runtime_target_best_failures(ctx) == []


@pytest.mark.parametrize(
    "ctx",
    [
        {},
        None,
        {"runtime_capability_lock": {}},
        {"runtime_capability_lock": {"status": "unlocked", "capability_sha256": "a" * 64}},
        {"runtime_capability_lock": {"status": "locked", "capability_sha256": "short"}},
        {"runtime_capability_lock": {"status": "locked", "capability_sha256": "z" * 64}},
        {"runtime_capability_lock": {**VALID_LOCK, "failures": ["provider missing"]}},
    ],
)
def test_malformed_proofs_are_rejected(ctx):
    assert runtime_capability_lock_failures(ctx) != []


def test_full_gate_passes_a_complete_proof():
    payload = {"status": "go", "runtime_fit": {"score": 95}}
    ctx = {"runtime_capability_lock": VALID_LOCK, "target_selection": BEST_SELECTION}
    assert proof_gate_failures(
        "readiness",
        payload,
        require_go=True,
        min_runtime_fit=90,
        require_best_runtime=True,
        require_capability_lock=True,
        runtime_context=ctx,
    ) == []


def test_non_dict_context_does_not_crash():
    """The Hub copy used to raise AttributeError here; the CLI copy returned {}."""
    assert runtime_capability_lock(None) == {}
    assert runtime_target_selection(None) == {}


# -- gate semantics --------------------------------------------------------


def test_capability_lock_requires_locked_status_and_valid_digest():
    assert runtime_capability_lock_failures({}) == ["runtime capability lock proof is missing"]
    unlocked = {"runtime_capability_lock": {"status": "draft", "capability_sha256": "a" * 64}}
    assert any("expected locked" in f for f in runtime_capability_lock_failures(unlocked))
    bad_digest = {"runtime_capability_lock": {"status": "locked", "capability_sha256": "a" * 63}}
    assert any("capability_sha256" in f for f in runtime_capability_lock_failures(bad_digest))


def test_target_selection_blocks_when_not_best():
    ctx = {
        "target_selection": {
            "status": "suboptimal",
            "selected_runtime_target_id": "cpu",
            "best_runtime_target_id": "gpu",
        }
    }
    failures = runtime_target_best_failures(ctx)
    assert failures and "is not best measured target" in failures[0]


def test_target_selection_blocks_on_positive_score_delta():
    ctx = {
        "target_selection": {
            "status": "best",
            "selected_runtime_target_id": "cpu",
            "best_runtime_target_id": "cpu",
            "score_delta": 7,
        }
    }
    assert any("trails best measured target" in f for f in runtime_target_best_failures(ctx))


def test_resolution_prefers_first_source_in_order():
    """Top-level evidence wins over nested — deterministic resolution order."""
    ctx = {
        "runtime_capability_lock": {"status": "locked", "capability_sha256": "a" * 64},
        "edge_execution_contract": {
            "runtime_capability_lock": {"status": "draft", "capability_sha256": "b" * 64}
        },
    }
    assert runtime_capability_lock(ctx)["capability_sha256"] == "a" * 64


def test_unknown_action_is_not_gated():
    assert proof_gate_failures("something-else", {}, require_go=True, min_runtime_fit=99) == []


def test_runtime_fit_score_reads_both_payload_shapes():
    assert runtime_fit_score("readiness", {"runtime_fit": {"score": 88}}) == 88.0
    mission = {"metrics": {"runtime_fit": {"score": 72}}}
    assert runtime_fit_score("edge-runtime-mission", mission) == 72.0
    assert runtime_fit_score("readiness", {}) is None


def test_missing_fit_score_is_a_failure_not_a_pass():
    failures = proof_gate_failures(
        "readiness", {"status": "go"}, require_go=True, min_runtime_fit=90
    )
    assert failures == ["runtime fit score is missing"]


def test_target_selection_resolver_finds_nested_evidence():
    ctx = {"readiness": {"runtime_decision": {"target_selection": BEST_SELECTION}}}
    assert runtime_target_selection(ctx) == BEST_SELECTION
