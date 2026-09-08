"""Minimal TEMMS orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Generic, TypeVar

from temms.errors import RuntimeContractError
from temms.runtime import Runtime
from temms.selector import Selector
from temms.types import InferenceResult, ModelRef, ReconcileResult, Scalar

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class TEMMS(Generic[InputT, OutputT]):
    """Select through a pure policy and execute through an injected runtime."""

    def __init__(
        self,
        *,
        runtime: Runtime[InputT, OutputT],
        selector: Selector,
    ) -> None:
        self._runtime = runtime
        self._selector = selector
        self._reconcile_locks: dict[str, asyncio.Lock] = {}

    async def reconcile(
        self,
        slot: str,
        context: Mapping[str, Scalar],
    ) -> ReconcileResult:
        """Serialize one slot's selection and activation transaction."""
        async with self._lock_for(slot):
            return await self._reconcile_once(slot, context)

    async def _reconcile_once(
        self,
        slot: str,
        context: Mapping[str, Scalar],
    ) -> ReconcileResult:
        models = tuple(await self._runtime.models(slot))
        before = await self._runtime.state(slot)
        decision = self._selector.select(
            slot=slot,
            models=models,
            runtime=before,
            context=context,
        )
        selected = decision.selected_model
        if selected is None or _same_model(before.active_model, selected):
            return ReconcileResult(decision=decision, activation=None)

        activation = await self._runtime.activate(slot, selected)
        self._assert_selected(selected, activation.active_model, "activate")

        after = await self._runtime.state(slot)
        self._assert_selected(selected, after.active_model, "state")
        return ReconcileResult(decision=decision, activation=activation)

    async def infer(self, slot: str, value: InputT) -> InferenceResult[OutputT]:
        """Delegate inference entirely to the injected runtime."""
        return await self._runtime.infer(slot, value)

    def _lock_for(self, slot: str) -> asyncio.Lock:
        lock = self._reconcile_locks.get(slot)
        if lock is None:
            lock = self._reconcile_locks[slot] = asyncio.Lock()
        return lock

    @staticmethod
    def _assert_selected(
        expected: ModelRef,
        actual: ModelRef | None,
        source: str,
    ) -> None:
        if actual is None or _identity(actual) != _identity(expected):
            actual_identity = None if actual is None else _identity(actual)
            raise RuntimeContractError(
                f"runtime {source} reported active model {actual_identity!r}; "
                f"expected {_identity(expected)!r}"
            )


def _identity(model: ModelRef) -> tuple[str, str]:
    return model.id, model.digest


def _same_model(left: ModelRef | None, right: ModelRef) -> bool:
    return left is not None and _identity(left) == _identity(right)
