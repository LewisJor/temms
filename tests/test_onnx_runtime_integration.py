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
