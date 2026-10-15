import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from temms import ModelRef
from temms.adapters import OnnxModel, OnnxRuntime


def _write_unary_model(path: Path, operation: str = "Identity") -> None:
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    input_value = helper.make_tensor_value_info("x", TensorProto.FLOAT, [None, 1])
    output_value = helper.make_tensor_value_info("y", TensorProto.FLOAT, [None, 1])
    graph = helper.make_graph(
        [helper.make_node(operation, ["x"], ["y"])],
        operation.lower(),
        [input_value],
        [output_value],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, path)


@pytest.mark.asyncio
async def test_real_onnx_runtime_inference(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    path = tmp_path / "identity.onnx"
    _write_unary_model(path)
    ref = ModelRef(id="identity", digest="sha256:identity")
    warmup = {"x": np.zeros((1, 1), dtype=np.float32)}
    runtime = OnnxRuntime(models={"vision": [OnnxModel(ref, path, warmup=warmup)]})

    await runtime.activate("vision", ref)
    result = await runtime.infer(
        "vision",
        {"x": np.array([[3.0]], dtype=np.float32)},
    )

    assert result.model == ref
    np.testing.assert_array_equal(result.output[0], np.array([[3.0]], dtype=np.float32))


@pytest.mark.asyncio
async def test_corrupt_artifact_preserves_real_session_and_recovers(tmp_path: Path) -> None:
    from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, Operator

    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    first_path, second_path = tmp_path / "first.onnx", tmp_path / "second.onnx"
    _write_unary_model(first_path)
    second_path.write_bytes(b"not an ONNX protobuf")
    first, second = ModelRef("first", "sha256:first"), ModelRef("second", "sha256:second")
    runtime = OnnxRuntime(models={"vision": [
        OnnxModel(first, first_path), OnnxModel(second, second_path),
    ]})
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector([
        ModelPolicy(ref, constraints=(Constraint("mode", Operator.EQ, ref.id),))
        for ref in (first, second)
    ]))
    await temms.reconcile("vision", {"mode": "first"})
    # The native exception type is an ONNX Runtime detail; retain its useful diagnostic.
    with pytest.raises(Exception, match="(?i)(protobuf|invalid)"):
        await temms.reconcile("vision", {"mode": "second"})
    assert (await runtime.state("vision")).active_model == first
    value = {"x": np.array([[7.0]], dtype=np.float32)}
    result = await temms.infer("vision", value)
    assert result.model == first
    np.testing.assert_array_equal(result.output[0], value["x"])

    _write_unary_model(second_path)
    await temms.reconcile("vision", {"mode": "second"})
    result = await temms.infer("vision", value)
    assert result.model == second
    np.testing.assert_array_equal(result.output[0], value["x"])


@pytest.mark.asyncio
async def test_real_onnx_in_flight_output_remains_pinned_across_publication(tmp_path: Path) -> None:
    from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, Operator

    np = pytest.importorskip("numpy")
    ort = pytest.importorskip("onnxruntime")
    first_path, second_path = tmp_path / "identity.onnx", tmp_path / "negation.onnx"
    _write_unary_model(first_path)
    _write_unary_model(second_path, "Neg")
    first, second = ModelRef("identity", "sha256:identity"), ModelRef("negation", "sha256:negation")
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    class GatedSession:
        def __init__(self, native):
            self.native = native

        def run(self, output_names, input_feed):
            # Admission captured the old session; run its real graph after publication.
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "test did not release the admitted native inference"
            return self.native.run(output_names, input_feed)

    def factory(path, providers):
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        native = ort.InferenceSession(path, options, providers=list(providers))
        return GatedSession(native) if path == str(first_path) else native

    runtime = OnnxRuntime(models={"vision": [
        OnnxModel(first, first_path, ("CPUExecutionProvider",)),
        OnnxModel(second, second_path, ("CPUExecutionProvider",)),
    ]}, session_factory=factory)
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector([
        ModelPolicy(ref, constraints=(Constraint("mode", Operator.EQ, ref.id),))
        for ref in (first, second)
    ]))
    executor = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(executor)
    pending = None
    value = {"x": np.array([[3.0], [-7.0]], dtype=np.float32)}
    try:
        await asyncio.wait_for(temms.reconcile("vision", {"mode": first.id}), 5)
        pending = asyncio.create_task(temms.infer("vision", value))
        await asyncio.wait_for(entered.wait(), 5)
        published = await asyncio.wait_for(temms.reconcile("vision", {"mode": second.id}), 5)
        assert published.activation.active_model == second
        assert (await runtime.state("vision")).active_model == second
        assert not pending.done() and not release.is_set()

        new_result = await asyncio.wait_for(temms.infer("vision", value), 5)
        assert new_result.model == second
        np.testing.assert_array_equal(new_result.output[0], -value["x"])
        assert not pending.done()
        release.set()
        old_result = await asyncio.wait_for(pending, 5)
        assert old_result.model == first
        np.testing.assert_array_equal(old_result.output[0], value["x"])
    finally:
        release.set()
        try:
            if pending is not None:
                pending.cancel()
                await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 5)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
