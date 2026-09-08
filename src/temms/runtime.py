"""Runtime contract injected into TEMMS."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

from temms.types import ActivationResult, InferenceResult, ModelRef, RuntimeState

InputT = TypeVar("InputT", contravariant=True)
OutputT = TypeVar("OutputT", covariant=True)


class Runtime(Protocol[InputT, OutputT]):
    """Owns model access, active state, activation, and inference."""

    async def models(self, slot: str) -> Sequence[ModelRef]:
        """Return models this runtime can access for a slot."""
        ...

    async def state(self, slot: str) -> RuntimeState:
        """Return the runtime's actual active model and resources."""
        ...

    async def activate(self, slot: str, model: ModelRef) -> ActivationResult:
        """Safely make a model active and report what became active."""
        ...

    async def infer(self, slot: str, value: InputT) -> InferenceResult[OutputT]:
        """Run inference and identify the model that served the request."""
        ...
