"""TEMMS: tiny runtime-independent adaptive model selection."""

from temms.engine import TEMMS
from temms.errors import (
    ModelUnavailableError,
    NoActiveModelError,
    RuntimeContractError,
    TEMMSError,
)
from temms.runtime import Runtime
from temms.selector import BestFeasibleSelector, Selector
from temms.types import (
    ActivationResult,
    CandidateEvaluation,
    Constraint,
    Decision,
    InferenceResult,
    ModelRef,
    Operator,
    ReconcileResult,
    RuntimeState,
    Scalar,
    ValueSource,
)

__all__ = [
    "ActivationResult",
    "BestFeasibleSelector",
    "CandidateEvaluation",
    "Constraint",
    "Decision",
    "InferenceResult",
    "ModelRef",
    "ModelUnavailableError",
    "NoActiveModelError",
    "Operator",
    "ReconcileResult",
    "Runtime",
    "RuntimeContractError",
    "RuntimeState",
    "Scalar",
    "Selector",
    "TEMMS",
    "TEMMSError",
    "ValueSource",
]
