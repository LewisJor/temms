"""Minimal TEMMS orchestration."""

from __future__ import annotations

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

    async def reconcile(
        self,
        slot: str,
        context: Mapping[str, Scalar],
    ) -> ReconcileResult:
        """Select the best model and activate it when the runtime needs a change."""
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

    @staticmethod
    def _assert_selected(
        expected: ModelRef,
        actual: ModelRef | None,
        source: str,
    ) -> None:
        if actual is None or (actual.id, actual.digest) != (expected.id, expected.digest):
            actual_identity = None if actual is None else (actual.id, actual.digest)
            expected_identity = (expected.id, expected.digest)
            raise RuntimeContractError(
                f"runtime {source} reported active model {actual_identity!r}; "
                f"expected {expected_identity!r}"
            )


def _same_model(left: ModelRef | None, right: ModelRef) -> bool:
    return left is not None and (left.id, left.digest) == (right.id, right.digest)
