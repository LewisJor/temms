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

from temms import hub_lite as hub
from temms.cli import main as cli
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


# -- the equivalence that matters -----------------------------------------


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
def test_cli_and_hub_agree_on_capability_lock(placement):
    """The drift that motivated this module: top-level locks must resolve in both."""
    ctx = PLACEMENTS[placement]("runtime_capability_lock", VALID_LOCK)
    assert cli._runtime_capability_lock_gate_failures(ctx) == (
        hub._runtime_capability_lock_gate_failures(ctx)
    )
    # and it must actually PASS — a shared implementation that blocks everything
    # would satisfy equivalence while being useless.
    assert cli._runtime_capability_lock_gate_failures(ctx) == []


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
def test_cli_and_hub_agree_on_target_selection(placement):
    ctx = PLACEMENTS[placement]("target_selection", BEST_SELECTION)
    assert cli._runtime_target_best_gate_failures(ctx) == (
        hub._runtime_target_best_gate_failures(ctx)
    )
    assert cli._runtime_target_best_gate_failures(ctx) == []


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
def test_cli_and_hub_agree_on_rejection_cases(ctx):
    """Both sides must reject the same malformed proofs, with the same reasons."""
    assert cli._runtime_capability_lock_gate_failures(ctx) == (
        hub._runtime_capability_lock_gate_failures(ctx)
    )


def test_full_gate_agreement_across_both_entrypoints():
    payload = {"status": "go", "runtime_fit": {"score": 95}}
    ctx = {"runtime_capability_lock": VALID_LOCK, "target_selection": BEST_SELECTION}
    kwargs = dict(
        require_go=True,
        min_runtime_fit=90,
        require_best_runtime=True,
        require_capability_lock=True,
        runtime_context=ctx,
    )
    assert cli._hub_gate_failures("readiness", payload, **kwargs) == (
        hub.edge_runtime_proof_gate_failures("readiness", payload, **kwargs)
    )
    assert cli._hub_gate_failures("readiness", payload, **kwargs) == []


def test_non_dict_context_does_not_crash_either_side():
    """The Hub previously raised AttributeError where the CLI returned {}."""
    assert cli._runtime_capability_lock_for_gate(None) == {}
    assert hub._runtime_capability_lock_for_proof_gate(None) == {}


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
