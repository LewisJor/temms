"""Best-feasible dispatch — the pure selection core (issue #43, slice 2).

Given a model **portfolio** (each member an operating point with an
``optimal_when`` preference region and a ``requires`` feasibility block) and the
current **operating point** (conditions + free resources), return the model that
should serve: the highest-preference member that is actually feasible right now.

This module is deliberately pure and side-effect free — no daemon, no I/O, no
clock. It is the mechanism the design calls for:

    filter to feasible → rank by preference → (hysteresis is applied by the caller)

There is no fallback list. Degradation emerges: when the wanted model is
infeasible, the next-best *feasible* member wins. The reasoning for every member
is returned, so the caller can record a proof of best-feasible-under-constraint
rather than a bare "switched to X".

Wiring this into the live policy engine (and retiring ``fallback_chain``) is a
separate change; this module changes no behavior on its own.
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass, field
from typing import Any

# A model whose optimal_when is non-empty but not satisfied is *out of its
# region*: still feasible-eligible, but only as a last resort. The empty-envelope
# floor outranks it (0 > -1); a model in its region (score = #predicates) outranks
# the floor.
_OUT_OF_REGION = -1

_COMPARATORS = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    "<": operator.lt,
}
_COMPARATOR_RE = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$")


@dataclass(frozen=True)
class PortfolioMember:
    """One model as an operating point.

    ``optimal_when`` is the preference region (conditions this model is *for*);
    ``requires`` is the feasibility block (what it needs to run at all).
    ``priority`` breaks ties between members of equal preference.
    """

    id: str
    optimal_when: dict[str, Any] = field(default_factory=dict)
    requires: dict[str, Any] = field(default_factory=dict)
    priority: int = 0


@dataclass(frozen=True)
class OperatingPoint:
    """The live state the portfolio is evaluated against."""

    conditions: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemberFeasibility:
    """Why a member is (or is not) eligible, and how preferred it is."""

    member_id: str
    feasible: bool
    unmet: list[str]  # human-readable reasons a requires predicate failed
    in_region: bool  # optimal_when fully satisfied
    preference: int  # region match count, 0 for the floor, -1 out of region


@dataclass
class DispatchDecision:
    """The selection plus the full reasoning behind it."""

    selected: str | None
    ranked: list[str]  # feasible members, best-first
    feasibility: list[MemberFeasibility]  # every member, evaluated
    reason: str


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _compare(actual: Any, spec: Any) -> bool:
    """Evaluate one predicate: does ``actual`` satisfy ``spec``?

    ``spec`` may be a comparator string (">=512", "<80"), a list (membership), or
    a scalar (equality). Numeric comparisons coerce both sides; a non-numeric
    actual against a numeric comparator fails closed.
    """
    if isinstance(spec, str):
        match = _COMPARATOR_RE.match(spec)
        if match:
            op_symbol, operand = match.group(1), match.group(2)
            op = _COMPARATORS[op_symbol]
            operand_num = _coerce_number(operand)
            actual_num = _coerce_number(actual)
            if operand_num is not None:
                if actual_num is None:
                    return False
                return op(actual_num, operand_num)
            # Non-numeric operand: only equality/inequality are meaningful.
            if op_symbol == "==":
                return actual == operand
            if op_symbol == "!=":
                return actual != operand
            return False
    if isinstance(spec, (list, tuple, set)):
        return actual in spec
    return actual == spec


def _requires_satisfied(
    requires: dict[str, Any], resources: dict[str, Any]
) -> tuple[bool, list[str]]:
    """A bare number in ``requires`` is a minimum (``>=``); strings are comparators."""
    unmet: list[str] = []
    for key, spec in requires.items():
        if key not in resources:
            unmet.append(f"{key} unknown (no reading)")
            continue
        actual = resources[key]
        # A bare number means "at least this much" — resources are budgets.
        if isinstance(spec, (int, float)) and not isinstance(spec, bool):
            ok = _compare(actual, f">={spec}")
        else:
            ok = _compare(actual, spec)
        if not ok:
            unmet.append(f"{key}={actual!r} does not satisfy {spec!r}")
    return (not unmet), unmet


def _region_match(
    optimal_when: dict[str, Any], conditions: dict[str, Any]
) -> tuple[bool, int]:
    """Return (fully-in-region, preference score).

    Empty envelope → the floor: applicable everywhere, preference 0. Non-empty and
    fully satisfied → in region, preference = #predicates (more specific wins).
    Non-empty but any predicate unmet → out of region.
    """
    if not optimal_when:
        return False, 0
    for key, spec in optimal_when.items():
        if key not in conditions or not _compare(conditions[key], spec):
            return False, _OUT_OF_REGION
    return True, len(optimal_when)


def evaluate_portfolio(
    members: list[PortfolioMember], operating_point: OperatingPoint
) -> list[MemberFeasibility]:
    """Evaluate every member against the operating point (no ranking)."""
    results: list[MemberFeasibility] = []
    for member in members:
        feasible, unmet = _requires_satisfied(member.requires, operating_point.resources)
        in_region, preference = _region_match(member.optimal_when, operating_point.conditions)
        results.append(
            MemberFeasibility(
                member_id=member.id,
                feasible=feasible,
                unmet=unmet,
                in_region=in_region,
                preference=preference,
            )
        )
    return results


def best_feasible(
    members: list[PortfolioMember], operating_point: OperatingPoint
) -> DispatchDecision:
    """Select the highest-preference feasible member.

    Ranking key (all descending except the final stabiliser): preference score,
    then declared ``priority``, then the portfolio's declared order — so the
    result is fully deterministic. Returns ``selected=None`` when nothing is
    feasible; that empty-feasible-set outcome is a first-class, recordable state.
    """
    order = {member.id: index for index, member in enumerate(members)}
    priority = {member.id: member.priority for member in members}
    feasibility = evaluate_portfolio(members, operating_point)
    by_id = {f.member_id: f for f in feasibility}

    feasible_ids = [f.member_id for f in feasibility if f.feasible]
    ranked = sorted(
        feasible_ids,
        key=lambda mid: (-by_id[mid].preference, -priority[mid], order[mid]),
    )

    if not ranked:
        return DispatchDecision(
            selected=None,
            ranked=[],
            feasibility=feasibility,
            reason="no feasible model (the device does not satisfy any member's requires)",
        )

    selected = ranked[0]
    chosen = by_id[selected]
    if chosen.in_region:
        reason = f"{selected} is feasible and in its preferred region"
    elif chosen.preference == 0:
        reason = f"{selected} is the feasible floor (no specialist is both wanted and feasible)"
    else:
        reason = f"{selected} is the best feasible model, though out of its preferred region"
    return DispatchDecision(
        selected=selected, ranked=ranked, feasibility=feasibility, reason=reason
    )
