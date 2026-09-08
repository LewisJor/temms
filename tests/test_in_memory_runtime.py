import asyncio

import pytest

from temms import ModelRef, ModelUnavailableError, NoActiveModelError
from temms.adapters import InMemoryRuntime


@pytest.mark.asyncio
async def test_runtime_exposes_models_resources_and_async_inference() -> None:
    model = ModelRef(id="model", digest="sha256:model")

    async def handler(value: int) -> int:
        return value * 2

    runtime = InMemoryRuntime[int, int](
        models={"slot": [model]},
        handlers={model.digest: handler},
        resources={"memory_mb": 256},
    )

    assert await runtime.models("slot") == (model,)
    assert (await runtime.state("slot")).resources == {"memory_mb": 256}

    await runtime.activate("slot", model)
    result = await runtime.infer("slot", 4)

    assert result.model == model
    assert result.output == 8


@pytest.mark.asyncio
async def test_inference_stays_bound_to_model_across_swap() -> None:
    first = ModelRef(id="first", digest="sha256:first")
    second = ModelRef(id="second", digest="sha256:second")
    started = asyncio.Event()
    release = asyncio.Event()

    async def first_handler(value: str) -> str:
        started.set()
        await release.wait()
        return f"first:{value}"

    runtime = InMemoryRuntime[str, str](
        models={"slot": [first, second]},
        handlers={
            first.digest: first_handler,
            second.digest: lambda value: f"second:{value}",
        },
    )
    await runtime.activate("slot", first)

    in_flight = asyncio.create_task(runtime.infer("slot", "request"))
    await started.wait()
    await runtime.activate("slot", second)
    release.set()

    old_result = await in_flight
    new_result = await runtime.infer("slot", "request")

    assert old_result.model == first
    assert old_result.output == "first:request"
    assert new_result.model == second
    assert new_result.output == "second:request"


@pytest.mark.asyncio
async def test_infer_requires_an_active_model() -> None:
    runtime = InMemoryRuntime[str, str](models={}, handlers={})

    with pytest.raises(NoActiveModelError):
        await runtime.infer("missing", "input")


@pytest.mark.asyncio
async def test_activate_rejects_models_not_exposed_by_runtime() -> None:
    runtime = InMemoryRuntime[str, str](models={"slot": []}, handlers={})
    missing = ModelRef(id="missing", digest="sha256:missing")

    with pytest.raises(ModelUnavailableError):
        await runtime.activate("slot", missing)
