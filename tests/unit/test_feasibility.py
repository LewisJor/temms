"""Best-feasible dispatch core (#43 slice 2) — pure selection logic."""

from __future__ import annotations

import pytest

from temms.policy.feasibility import (
    OperatingPoint,
    PortfolioMember,
    best_feasible,
    evaluate_portfolio,
)


def _member(mid, optimal_when=None, requires=None, priority=0):
    return PortfolioMember(
        id=mid,
        optimal_when=optimal_when or {},
        requires=requires or {},
        priority=priority,
    )


# The demo portfolio: two specialists + an always-cheap floor.
DAYLIGHT = _member("daylight", {"light": "bright"}, {"memory_mb": 512})
LOWLIGHT = _member("lowlight", {"light": "low"}, {"memory_mb": 512})
FLOOR = _member("tiny", {}, {"memory_mb": 96})
PORTFOLIO = [DAYLIGHT, LOWLIGHT, FLOOR]


# -- feasibility filtering -------------------------------------------------


def test_requires_minimum_is_satisfied():
    op = OperatingPoint(resources={"memory_mb": 600})
    result = {f.member_id: f for f in evaluate_portfolio(PORTFOLIO, op)}
    assert result["daylight"].feasible is True


def test_requires_minimum_unmet_is_infeasible_with_reason():
    op = OperatingPoint(resources={"memory_mb": 128})
    result = {f.member_id: f for f in evaluate_portfolio(PORTFOLIO, op)}
    assert result["daylight"].feasible is False
    assert "memory_mb" in result["daylight"].unmet[0]
    assert result["tiny"].feasible is True  # floor still fits


def test_unknown_resource_reading_is_infeasible_not_assumed():
    op = OperatingPoint(resources={})  # no memory reading at all
    result = {f.member_id: f for f in evaluate_portfolio(PORTFOLIO, op)}
    assert result["daylight"].feasible is False
    assert "unknown" in result["daylight"].unmet[0]


@pytest.mark.parametrize(
    "spec,actual,ok",
    [
        (">5", 6, True),
        (">5", 5, False),
        (">=512", 512, True),
        ("<80", 60, True),
        ("<80", 90, False),
        ("<=5", 5, True),
        ("==prod", "prod", True),
        ("!=prod", "dev", True),
    ],
)
def test_comparator_predicates(spec, actual, ok):
    member = _member("m", requires={"x": spec})
    op = OperatingPoint(resources={"x": actual})
    assert evaluate_portfolio([member], op)[0].feasible is ok


# -- region matching -------------------------------------------------------


def test_in_region_when_all_optimal_when_satisfied():
    op = OperatingPoint(conditions={"light": "bright"}, resources={"memory_mb": 600})
    result = {f.member_id: f for f in evaluate_portfolio(PORTFOLIO, op)}
    assert result["daylight"].in_region is True
    assert result["daylight"].preference == 1
    assert result["lowlight"].in_region is False  # out of region
    assert result["tiny"].preference == 0  # the floor


def test_membership_in_optimal_when():
    member = _member("m", optimal_when={"ambient": ["low", "dark"]}, requires={})
    op = OperatingPoint(conditions={"ambient": "dark"})
    assert evaluate_portfolio([member], op)[0].in_region is True


# -- selection + emergent degradation --------------------------------------


def test_specialist_in_region_beats_floor():
    op = OperatingPoint(conditions={"light": "bright"}, resources={"memory_mb": 600})
    assert best_feasible(PORTFOLIO, op).selected == "daylight"


def test_switches_specialist_with_conditions():
    op = OperatingPoint(conditions={"light": "low"}, resources={"memory_mb": 600})
    assert best_feasible(PORTFOLIO, op).selected == "lowlight"


def test_degradation_emerges_when_preferred_is_infeasible():
    # Conditions want daylight, but only the floor fits the memory budget.
    op = OperatingPoint(conditions={"light": "bright"}, resources={"memory_mb": 128})
    decision = best_feasible(PORTFOLIO, op)
    assert decision.selected == "tiny"
    assert "floor" in decision.reason


def test_floor_beats_out_of_region_specialist():
    # Neither specialist is in region (unknown light); both + floor are feasible.
    op = OperatingPoint(conditions={}, resources={"memory_mb": 600})
    assert best_feasible(PORTFOLIO, op).selected == "tiny"


def test_empty_feasible_set_selects_nothing():
    op = OperatingPoint(conditions={"light": "bright"}, resources={"memory_mb": 32})
    decision = best_feasible(PORTFOLIO, op)
    assert decision.selected is None
    assert decision.ranked == []
    assert "no feasible model" in decision.reason


def test_more_specific_region_outranks_less_specific():
    broad = _member("broad", {"light": "low"})
    specific = _member("specific", {"light": "low", "weather": "fog"})
    op = OperatingPoint(conditions={"light": "low", "weather": "fog"})
    assert best_feasible([broad, specific], op).selected == "specific"


def test_priority_breaks_ties_between_equal_preference():
    a = _member("a", {"light": "low"}, priority=1)
    b = _member("b", {"light": "low"}, priority=9)
    op = OperatingPoint(conditions={"light": "low"})
    assert best_feasible([a, b], op).selected == "b"


def test_selection_is_deterministic_on_full_ties():
    # Same preference, same priority — declared order stabilises the result.
    a = _member("a", {"light": "low"})
    b = _member("b", {"light": "low"})
    op = OperatingPoint(conditions={"light": "low"})
    assert best_feasible([a, b], op).selected == "a"
    assert best_feasible([b, a], op).selected == "b"


def test_reasoning_lists_every_member():
    op = OperatingPoint(conditions={"light": "bright"}, resources={"memory_mb": 600})
    decision = best_feasible(PORTFOLIO, op)
    assert {f.member_id for f in decision.feasibility} == {"daylight", "lowlight", "tiny"}
    assert decision.ranked[0] == "daylight"
