import asyncio
import threading
from pathlib import Path

import pytest

from temms import ModelRef, NoActiveModelError
from temms.adapters import OnnxModel, OnnxRuntime


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, output_names, input_feed):
        self.calls.append(dict(input_feed))
        return [input_feed["x"]]


@pytest.mark.asyncio
async def test_onnx_runtime_loads_warms_activates_and_infers(tmp_path: Path) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"fixture")
    model = ModelRef(id="identity", digest="sha256:identity")
    session = FakeSession()
    factory_calls: list[tuple[str, object]] = []

    def factory(model_path: str, providers):
        factory_calls.append((model_path, providers))
        return session

    runtime = OnnxRuntime(
        models={
            "vision": [
                OnnxModel(
                    ref=model,
                    path=path,
                    providers=("CPUExecutionProvider",),
                    warmup={"x": "warm"},
                )
            ]
        },
        session_factory=factory,
    )

    activation = await runtime.activate("vision", model)
    result = await runtime.infer("vision", {"x": "real"})

    assert activation.active_model == model
    assert (await runtime.state("vision")).active_model == model
    assert result.model == model
    assert result.output == ("real",)
    assert factory_calls == [(str(path), ("CPUExecutionProvider",))]
    assert session.calls == [{"x": "warm"}, {"x": "real"}]


@pytest.mark.asyncio
async def test_onnx_runtime_requires_an_active_model() -> None:
    runtime = OnnxRuntime(models={})

    with pytest.raises(NoActiveModelError):
        await runtime.infer("missing", {"x": "value"})


@pytest.mark.asyncio
async def test_onnx_inference_stays_on_old_session_across_swap(tmp_path: Path) -> None:
    first_path = tmp_path / "first.onnx"
    second_path = tmp_path / "second.onnx"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    first = ModelRef(id="first", digest="sha256:first")
    second = ModelRef(id="second", digest="sha256:second")
    started = threading.Event()
    release = threading.Event()

    class BlockingSession:
        def run(self, output_names, input_feed):
            started.set()
            assert release.wait(timeout=2)
            return [f"first:{input_feed['x']}"]

    class ImmediateSession:
        def run(self, output_names, input_feed):
            return [f"second:{input_feed['x']}"]

    def factory(model_path: str, providers):
        return BlockingSession() if model_path == str(first_path) else ImmediateSession()

    runtime = OnnxRuntime(
        models={
            "vision": [
                OnnxModel(first, first_path),
                OnnxModel(second, second_path),
            ]
        },
        session_factory=factory,
    )
    await runtime.activate("vision", first)

    in_flight = asyncio.create_task(runtime.infer("vision", {"x": "request"}))
    assert await asyncio.to_thread(started.wait, 2)
    await runtime.activate("vision", second)
    release.set()

    old_result = await in_flight
    new_result = await runtime.infer("vision", {"x": "request"})

    assert old_result.model == first
    assert old_result.output == ("first:request",)
    assert new_result.model == second
    assert new_result.output == ("second:request",)
