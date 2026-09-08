"""TEMMS: tiny runtime-independent adaptive model selection."""

from temms.engine import TEMMS
from temms.errors import (
    ModelUnavailableError,
    NoActiveModelError,
    RuntimeContractError,
    RuntimeDependencyError,
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
    ModelPolicy,
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
    "ModelPolicy",
    "ModelRef",
    "ModelUnavailableError",
    "NoActiveModelError",
    "Operator",
    "ReconcileResult",
    "Runtime",
    "RuntimeContractError",
    "RuntimeDependencyError",
    "RuntimeState",
    "Scalar",
    "Selector",
    "TEMMS",
    "TEMMSError",
    "ValueSource",
]
