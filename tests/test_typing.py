"""Downstream usage checked by `python -m mypy`; pytest executes no inference here."""

from typing import assert_type

from temms import TEMMS, BestFeasibleSelector, InferenceResult, ModelPolicy, ModelRef, Runtime
from temms.adapters import InMemoryRuntime, OnnxInput, OnnxOutput, OnnxRuntime


async def describe(runtime: Runtime[str, object]) -> object:
    return (await runtime.infer("vision", "frame")).output


async def public_usage(onnx: OnnxRuntime, strings: Runtime[str, str]) -> None:
    model = ModelRef("model", "application-supplied-identity")
    runtime: Runtime[object, str] = InMemoryRuntime(
        models={"vision": [model]}, handlers={model.digest: str},
    )
    # A runtime accepting any object and returning str can serve this narrower caller.
    await describe(runtime)
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector([ModelPolicy(model)]))
    result = await temms.infer("vision", "frame")
    assert_type(result, InferenceResult[str])
    assert_type(result.output, str)
    broader_result: InferenceResult[object] = result
    assert_type(broader_result.output, object)

    native: Runtime[OnnxInput, OnnxOutput] = onnx
    assert_type((await native.infer("vision", {"x": object()})).output, OnnxOutput)

    # These must remain errors; strict mypy rejects unused ignores if types loosen.
    await strings.infer("vision", 42)  # type: ignore[arg-type]
    await describe(onnx)  # type: ignore[arg-type]
