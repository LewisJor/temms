"""Small immutable values shared by the TEMMS kernel."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")
Scalar = bool | int | float | str


class ValueSource(StrEnum):
    """Where a model constraint reads its actual value."""

    CONTEXT = "context"
    RESOURCE = "resource"


class Operator(StrEnum):
    """Constraint operators supported by the minimal selector."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"


@dataclass(frozen=True, slots=True)
class Constraint:
    """One hard requirement that a model needs to be feasible."""

    key: str
    operator: Operator
    expected: object
    source: ValueSource = ValueSource.CONTEXT


@dataclass(frozen=True, slots=True)
class ModelRef:
    """A runtime-addressable model plus its selection contract."""

    id: str
    digest: str
    priority: int = 0
    constraints: tuple[Constraint, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """State reported by the injected runtime."""

    active_model: ModelRef | None
    resources: Mapping[str, Scalar] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActivationResult:
    """The model a runtime was asked to activate and what became active."""

    requested_model: ModelRef
    active_model: ModelRef
    changed: bool


@dataclass(frozen=True, slots=True)
class InferenceResult(Generic[OutputT]):
    """Inference output attributed to the model that actually served it."""

    model: ModelRef
    output: OutputT


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """Feasibility result for one model candidate."""

    model: ModelRef
    feasible: bool
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Decision:
    """A pure model-selection result."""

    slot: str
    selected_model: ModelRef | None
    current_model: ModelRef | None
    evaluations: tuple[CandidateEvaluation, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """The decision plus any runtime activation it caused."""

    decision: Decision
    activation: ActivationResult | None
