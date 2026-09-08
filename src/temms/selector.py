"""Pure model selection."""

from __future__ import annotations

import operator
from collections.abc import Container, Mapping, Sequence
from typing import Protocol

from temms.types import (
    CandidateEvaluation,
    Constraint,
    Decision,
    ModelPolicy,
    ModelRef,
    Operator,
    RuntimeState,
    Scalar,
    ValueSource,
)

_COMPARATORS = {
    Operator.EQ: operator.eq,
    Operator.NE: operator.ne,
    Operator.GT: operator.gt,
    Operator.GTE: operator.ge,
    Operator.LT: operator.lt,
    Operator.LTE: operator.le,
}


class Selector(Protocol):
    """Selects a model without mutating runtime state."""

    def select(
        self,
        *,
        slot: str,
        models: Sequence[ModelRef],
        runtime: RuntimeState,
        context: Mapping[str, Scalar],
    ) -> Decision:
        ...


class BestFeasibleSelector:
    """Choose the highest-priority available model whose constraints pass."""

    def __init__(self, policies: Sequence[ModelPolicy]) -> None:
        self._policies = tuple(policies)
        identities = [_identity(policy.model) for policy in self._policies]
        if len(identities) != len(set(identities)):
            raise ValueError("model policies must have unique model identities")

    def select(
        self,
        *,
        slot: str,
        models: Sequence[ModelRef],
        runtime: RuntimeState,
        context: Mapping[str, Scalar],
    ) -> Decision:
        available = {_identity(model): model for model in models}
        evaluations = tuple(
            self._evaluate(
                policy,
                available.get(_identity(policy.model)),
                context=context,
                resources=runtime.resources,
            )
            for policy in self._policies
        )
        selected = self._rank(evaluations, runtime.active_model)
        return Decision(
            slot=slot,
            selected_model=selected,
            current_model=runtime.active_model,
            evaluations=evaluations,
            reason=self._reason(selected, runtime.active_model, evaluations),
        )

    @staticmethod
    def _evaluate(
        policy: ModelPolicy,
        model: ModelRef | None,
        *,
        context: Mapping[str, Scalar],
        resources: Mapping[str, Scalar],
    ) -> CandidateEvaluation:
        if model is None:
            return CandidateEvaluation(
                model=policy.model,
                priority=policy.priority,
                feasible=False,
                failures=("model is unavailable from the runtime",),
            )
        failures = tuple(
            failure
            for constraint in policy.constraints
            if (failure := _constraint_failure(constraint, context, resources)) is not None
        )
        return CandidateEvaluation(
            model=model,
            priority=policy.priority,
            feasible=not failures,
            failures=failures,
        )

    @staticmethod
    def _rank(
        evaluations: Sequence[CandidateEvaluation],
        current: ModelRef | None,
    ) -> ModelRef | None:
        feasible = [item for item in evaluations if item.feasible]
        if not feasible:
            return None
        current_identity = _identity(current) if current else None
        return min(
            feasible,
            key=lambda item: (
                -item.priority,
                _identity(item.model) != current_identity,
                item.model.id,
                item.model.digest,
            ),
        ).model

    @staticmethod
    def _reason(
        selected: ModelRef | None,
        current: ModelRef | None,
        evaluations: Sequence[CandidateEvaluation],
    ) -> str:
        if not evaluations:
            return "selector has no configured model policies"
        if selected is None:
            return "no feasible model"
        if current and _identity(current) == _identity(selected):
            return "current model remains the best feasible model"
        return "selected the highest-priority feasible model"


def _identity(model: ModelRef) -> tuple[str, str]:
    return model.id, model.digest


def _constraint_failure(
    constraint: Constraint,
    context: Mapping[str, Scalar],
    resources: Mapping[str, Scalar],
) -> str | None:
    values = context if constraint.source is ValueSource.CONTEXT else resources
    namespace = constraint.source.value
    if constraint.key not in values:
        return f"{namespace}.{constraint.key} is missing"

    actual = values[constraint.key]
    try:
        passed = _compare(actual, constraint.operator, constraint.expected)
    except (TypeError, ValueError):
        passed = False
    if passed:
        return None
    return (
        f"{namespace}.{constraint.key}={actual!r} does not satisfy "
        f"{constraint.operator.value} {constraint.expected!r}"
    )


def _compare(actual: object, op: Operator, expected: object) -> bool:
    if op in _COMPARATORS:
        return bool(_COMPARATORS[op](actual, expected))
    if not isinstance(expected, Container) or isinstance(expected, (str, bytes)):
        raise TypeError(f"{op.value} requires a non-string container")
    if op is Operator.IN:
        return actual in expected
    if op is Operator.NOT_IN:
        return actual not in expected
    raise ValueError(f"unsupported operator: {op}")
