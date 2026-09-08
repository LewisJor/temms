"""Pure model selection."""

from __future__ import annotations

import operator
from collections.abc import Container, Mapping, Sequence
from typing import Protocol

from temms.types import (
    CandidateEvaluation,
    Constraint,
    Decision,
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
    """Choose the highest-priority model whose hard constraints pass."""

    def select(
        self,
        *,
        slot: str,
        models: Sequence[ModelRef],
        runtime: RuntimeState,
        context: Mapping[str, Scalar],
    ) -> Decision:
        evaluations = tuple(
            self._evaluate(model, context=context, resources=runtime.resources)
            for model in models
        )
        feasible = [item.model for item in evaluations if item.feasible]
        selected = self._rank(feasible, runtime.active_model)
        return Decision(
            slot=slot,
            selected_model=selected,
            current_model=runtime.active_model,
            evaluations=evaluations,
            reason=self._reason(selected, runtime.active_model, evaluations),
        )

    @staticmethod
    def _evaluate(
        model: ModelRef,
        *,
        context: Mapping[str, Scalar],
        resources: Mapping[str, Scalar],
    ) -> CandidateEvaluation:
        failures = tuple(
            failure
            for constraint in model.constraints
            if (failure := _constraint_failure(constraint, context, resources)) is not None
        )
        return CandidateEvaluation(model=model, feasible=not failures, failures=failures)

    @staticmethod
    def _rank(models: Sequence[ModelRef], current: ModelRef | None) -> ModelRef | None:
        if not models:
            return None
        current_identity = (current.id, current.digest) if current else None
        return min(
            models,
            key=lambda model: (
                -model.priority,
                (model.id, model.digest) != current_identity,
                model.id,
                model.digest,
            ),
        )

    @staticmethod
    def _reason(
        selected: ModelRef | None,
        current: ModelRef | None,
        evaluations: Sequence[CandidateEvaluation],
    ) -> str:
        if not evaluations:
            return "runtime exposed no models for the slot"
        if selected is None:
            return "no feasible model"
        if current and (current.id, current.digest) == (selected.id, selected.digest):
            return "current model remains the best feasible model"
        return "selected the highest-priority feasible model"


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
