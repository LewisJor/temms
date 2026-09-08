import asyncio

import pytest

from temms import (
    TEMMS,
    ActivationResult,
    BestFeasibleSelector,
    Constraint,
    InferenceResult,
    ModelPolicy,
    ModelRef,
    Operator,
    RuntimeContractError,
    RuntimeState,
)
from temms.adapters import InMemoryRuntime


@pytest.mark.asyncio
async def test_reconcile_activates_selected_model_and_infer_attributes_it() -> None:
    daylight = ModelRef(id="daylight", digest="sha256:day")
    runtime = InMemoryRuntime[str, str](
        models={"vision": [daylight]},
        handlers={daylight.digest: lambda value: value.upper()},
    )
    temms = TEMMS(
        runtime=runtime,
        selector=BestFeasibleSelector([ModelPolicy(daylight, priority=10)]),
    )

    reconciled = await temms.reconcile("vision", {})
    inferred = await temms.infer("vision", "frame")

    assert reconciled.activation is not None
    assert reconciled.activation.active_model == daylight
    assert inferred == InferenceResult(model=daylight, output="FRAME")


@pytest.mark.asyncio
async def test_reconcile_does_not_reactivate_current_model() -> None:
    model = ModelRef(id="model", digest="sha256:model")

    class CountingRuntime(InMemoryRuntime[str, str]):
        activations = 0

        async def activate(self, slot: str, selected: ModelRef) -> ActivationResult:
            self.activations += 1
            return await super().activate(slot, selected)

    runtime = CountingRuntime(
        models={"vision": [model]},
        handlers={model.digest: lambda value: value},
    )
    temms = TEMMS(
        runtime=runtime,
        selector=BestFeasibleSelector([ModelPolicy(model)]),
    )

    first = await temms.reconcile("vision", {})
    second = await temms.reconcile("vision", {})

    assert first.activation is not None
    assert second.activation is None
    assert runtime.activations == 1


@pytest.mark.asyncio
async def test_reconcile_is_serialized_per_slot() -> None:
    first = ModelRef(id="first", digest="sha256:first")
    second = ModelRef(id="second", digest="sha256:second")

    class SlowRuntime(InMemoryRuntime[str, str]):
        concurrent_activations = 0
        max_concurrent_activations = 0

        async def activate(self, slot: str, model: ModelRef) -> ActivationResult:
            self.concurrent_activations += 1
            self.max_concurrent_activations = max(
                self.max_concurrent_activations,
                self.concurrent_activations,
            )
            await asyncio.sleep(0.01)
            try:
                return await super().activate(slot, model)
            finally:
                self.concurrent_activations -= 1

    runtime = SlowRuntime(
        models={"vision": [first, second]},
        handlers={first.digest: str, second.digest: str},
    )
    selector = BestFeasibleSelector(
        [
            ModelPolicy(
                first,
                priority=10,
                constraints=(Constraint("mode", Operator.EQ, "first"),),
            ),
            ModelPolicy(
                second,
                priority=10,
                constraints=(Constraint("mode", Operator.EQ, "second"),),
            ),
        ]
    )
    temms = TEMMS(runtime=runtime, selector=selector)

    await asyncio.gather(
        temms.reconcile("vision", {"mode": "first"}),
        temms.reconcile("vision", {"mode": "second"}),
    )

    assert runtime.max_concurrent_activations == 1


@pytest.mark.asyncio
async def test_runtime_mismatch_fails_loudly() -> None:
    selected = ModelRef(id="selected", digest="sha256:selected")
    wrong = ModelRef(id="wrong", digest="sha256:wrong")

    class LyingRuntime:
        async def models(self, slot: str):
            return [selected]

        async def state(self, slot: str):
            return RuntimeState(active_model=None)

        async def activate(self, slot: str, model: ModelRef):
            return ActivationResult(model, wrong, changed=True)

        async def infer(self, slot: str, value: str):
            return InferenceResult(wrong, value)

    temms = TEMMS(
        runtime=LyingRuntime(),
        selector=BestFeasibleSelector([ModelPolicy(selected)]),
    )

    with pytest.raises(RuntimeContractError, match="activate reported active model"):
        await temms.reconcile("vision", {})


@pytest.mark.asyncio
async def test_runtime_state_mismatch_fails_after_activation() -> None:
    selected = ModelRef(id="selected", digest="sha256:selected")
    wrong = ModelRef(id="wrong", digest="sha256:wrong")

    class StaleStateRuntime:
        state_calls = 0

        async def models(self, slot: str):
            return [selected]

        async def state(self, slot: str):
            self.state_calls += 1
            active = None if self.state_calls == 1 else wrong
            return RuntimeState(active_model=active)

        async def activate(self, slot: str, model: ModelRef):
            return ActivationResult(model, model, changed=True)

        async def infer(self, slot: str, value: str):
            return InferenceResult(wrong, value)

    temms = TEMMS(
        runtime=StaleStateRuntime(),
        selector=BestFeasibleSelector([ModelPolicy(selected)]),
    )

    with pytest.raises(RuntimeContractError, match="state reported active model"):
        await temms.reconcile("vision", {})
