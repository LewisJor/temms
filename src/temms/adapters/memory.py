"""Deterministic runtime for tests, examples, and application prototypes."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Generic, TypeVar, cast

from temms.errors import ModelUnavailableError, NoActiveModelError
from temms.types import ActivationResult, InferenceResult, ModelRef, RuntimeState, Scalar

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class InMemoryRuntime(Generic[InputT, OutputT]):
    """A small runtime implementation with real model access and inference hooks."""

    def __init__(
        self,
        *,
        models: Mapping[str, Sequence[ModelRef]],
        handlers: Mapping[str, Callable[[InputT], OutputT | Awaitable[OutputT]]],
        resources: Mapping[str, Scalar] | None = None,
    ) -> None:
        self._models = {slot: tuple(items) for slot, items in models.items()}
        self._handlers = dict(handlers)
        self._resources = dict(resources or {})
        self._active: dict[str, ModelRef] = {}

    async def models(self, slot: str) -> Sequence[ModelRef]:
        return self._models.get(slot, ())

    async def state(self, slot: str) -> RuntimeState:
        return RuntimeState(
            active_model=self._active.get(slot),
            resources=dict(self._resources),
        )

    async def activate(self, slot: str, model: ModelRef) -> ActivationResult:
        available = {
            (candidate.id, candidate.digest): candidate
            for candidate in self._models.get(slot, ())
        }
        key = (model.id, model.digest)
        if key not in available:
            raise ModelUnavailableError(
                f"model {model.id!r} ({model.digest}) is unavailable for slot {slot!r}"
            )
        actual = available[key]
        previous = self._active.get(slot)
        self._active[slot] = actual
        return ActivationResult(
            requested_model=model,
            active_model=actual,
            changed=previous is None
            or (previous.id, previous.digest) != (actual.id, actual.digest),
        )

    async def infer(self, slot: str, value: InputT) -> InferenceResult[OutputT]:
        model = self._active.get(slot)
        if model is None:
            raise NoActiveModelError(f"slot {slot!r} has no active model")
        try:
            handler = self._handlers[model.digest]
        except KeyError as exc:
            raise ModelUnavailableError(
                f"no inference handler is registered for model {model.digest}"
            ) from exc
        output = handler(value)
        if inspect.isawaitable(output):
            output = await cast(Awaitable[OutputT], output)
        return InferenceResult(model=model, output=cast(OutputT, output))

    def update_resources(self, **resources: Scalar) -> None:
        """Change the resource state exposed to the selector."""
        self._resources.update(resources)
