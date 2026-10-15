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
    NoActiveModelError,
    Operator,
    RuntimeContractError,
    RuntimeState,
)
from temms.adapters import InMemoryRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_active", [False, True])
async def test_no_feasible_selection_leaves_admission_to_application(initially_active) -> None:
    model = ModelRef(id="model", digest="sha256:model")

    class GuardedRuntime(InMemoryRuntime[str, str]):
        async def activate(self, slot: str, selected: ModelRef) -> ActivationResult:
            pytest.fail("no-feasible reconciliation must not activate a model")

    runtime = GuardedRuntime(
        models={"vision": [model]},
        handlers={model.digest: str.upper},
    )
    if initially_active:
        await InMemoryRuntime.activate(runtime, "vision", model)
    before = await runtime.state("vision")
    temms = TEMMS(
        runtime=runtime,
        selector=BestFeasibleSelector([
            ModelPolicy(model, constraints=(Constraint("battery", Operator.GTE, 40),)),
        ]),
    )

    result = await temms.reconcile("vision", {"battery": 10})

    assert result.decision.selected_model is None
    assert result.decision.current_model == before.active_model
    assert result.decision.reason == "no feasible model"
    assert result.activation is None
    assert await runtime.state("vision") == before
    if initially_active:
        assert await temms.infer("vision", "frame") == InferenceResult(model, "FRAME")
    else:
        with pytest.raises(NoActiveModelError, match="vision.*no active model"):
            await temms.infer("vision", "frame")


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
async def test_reconcile_serializes_one_slot_while_another_progresses() -> None:
    first = ModelRef(id="first", digest="sha256:first")
    second = ModelRef(id="second", digest="sha256:second")
    entered = asyncio.Event()
    release = asyncio.Event()
    queued = asyncio.Event()
    inventory_reads: list[str] = []

    class BlockingRuntime(InMemoryRuntime[str, str]):
        async def models(self, slot: str):
            inventory_reads.append(slot)
            return await super().models(slot)

        async def activate(self, slot: str, model: ModelRef) -> ActivationResult:
            if slot == "vision" and model == first:
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            return await super().activate(slot, model)

    runtime = BlockingRuntime(
        models={"vision": [first, second], "other": [first]},
        handlers={first.digest: str.upper, second.digest: str.lower},
    )
    selector = BestFeasibleSelector([
        ModelPolicy(ref, constraints=(Constraint("mode", Operator.EQ, ref.id),))
        for ref in (first, second)
    ])
    temms = TEMMS(runtime=runtime, selector=selector)

    async def reconcile_second():
        queued.set()
        return await temms.reconcile("vision", {"mode": "second"})

    tasks = [asyncio.create_task(temms.reconcile("vision", {"mode": "first"}))]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        tasks.append(asyncio.create_task(reconcile_second()))
        await asyncio.wait_for(queued.wait(), 2)
        # The second coroutine has reached reconcile, but cannot even read inventory.
        assert inventory_reads == ["vision"]
        other = await asyncio.wait_for(temms.reconcile("other", {"mode": "first"}), 2)
        assert other.activation.active_model == first
        assert await asyncio.wait_for(temms.infer("other", "Frame"), 2) == InferenceResult(
            first, "FRAME"
        )
        assert (await runtime.state("vision")).active_model is None
        assert inventory_reads == ["vision", "other"]
        assert not tasks[0].done() and not tasks[1].done()

        release.set()
        before, after = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert before.activation.active_model == first
        assert after.decision.current_model == first
        assert after.activation.active_model == second
        assert (await runtime.state("vision")).active_model == second
        assert (await runtime.state("other")).active_model == first
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)


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
