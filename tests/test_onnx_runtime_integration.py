from pathlib import Path

import pytest

from temms import ModelRef
from temms.adapters import OnnxModel, OnnxRuntime


def _write_identity_model(path: Path) -> None:
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    input_value = helper.make_tensor_value_info("x", TensorProto.FLOAT, [None, 1])
    output_value = helper.make_tensor_value_info("y", TensorProto.FLOAT, [None, 1])
    graph = helper.make_graph(
        [helper.make_node("Identity", ["x"], ["y"])],
        "identity",
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
    _write_identity_model(path)
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
    _write_identity_model(first_path)
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

    _write_identity_model(second_path)
    await temms.reconcile("vision", {"mode": "second"})
    result = await temms.infer("vision", value)
    assert result.model == second
    np.testing.assert_array_equal(result.output[0], value["x"])
