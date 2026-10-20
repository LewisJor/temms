"""Exercise the qualification's fault/recovery path with small real native graphs."""

import hashlib
from contextlib import nullcontext

import pytest


@pytest.mark.asyncio
async def test_edge_scenario_preserves_real_outputs_through_fault_and_recovery(
    tmp_path, monkeypatch,
):
    np = pytest.importorskip("numpy")
    ort = pytest.importorskip("onnxruntime")
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    from examples.edge import run
    from examples.live_swap.run import identity
    from temms import ModelRef
    from temms.adapters import OnnxModel, OnnxRuntime

    refs, models = [], []
    for name, operation in (("mobilenetv2", "Identity"), ("resnet18", "Neg")):
        path = tmp_path / f"{name}.onnx"
        graph = helper.make_graph(
            [helper.make_node(operation, ["data"], ["output"])], name,
            [helper.make_tensor_value_info("data", TensorProto.FLOAT, [1, 1000])],
            [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 1000])],
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 8
        onnx.save(model, path)
        ref = ModelRef(name, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
        refs.append(ref)
        models.append(OnnxModel(ref, path, ("CPUExecutionProvider",)))

    def factory(path, providers):
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        return ort.InferenceSession(path, options, providers=list(providers))

    source = tmp_path / "original.onnx"
    source.write_bytes(models[1].path.read_bytes())
    original = source.read_bytes()
    samples = []
    for index in range(20):
        scores = np.arange(1000, dtype=np.float32).reshape(1, 1000) + index
        samples.append({
            "image": {"name": f"{index}.jpg", "sha256": f"input-{index}"},
            "expected": {"index": 999, "label": "999"}, "feed": {"data": scores},
            "references": {(refs[0].id, refs[0].digest): scores,
                           (refs[1].id, refs[1].digest): -scores},
        })
    # Linux pressure is independently tested and witnessed in hosted qualification.
    monkeypatch.setattr(run.resources, "cpu_pressure", lambda: nullcontext({}))
    runtime = OnnxRuntime(models={"vision": models}, session_factory=factory)
    records = []
    measured = await run.exercise(runtime, run.policies(refs), samples,
                                  [str(index) for index in range(1000)],
                                  models[1].path, source, records.append)

    assert len(measured) == 113
    assert records[0] == {"event": "startup", "active": None,
                          "inference_error": "NoActiveModelError"}
    frames = [record for record in records if record["event"] == "frame"]
    assert all(record["reference_match"] and record["identity_match"] for record in frames)
    activations = {record["phase"]: record for record in records
                   if record["event"] == "activation"}
    failure = activations["corrupt-update"]
    assert failure["error"]["type"] == "InvalidProtobuf"
    assert failure["previous"] == failure["active"] == identity(refs[0])
    assert failure["selected"] == identity(refs[1])
    assert all(record["serving"] == identity(refs[0]) for record in frames
               if record["phase"] == "corrupt-update")
    assert activations["restored-update"]["active"] == identity(refs[1])
    assert activations["missing-context"]["selected"] is None
    assert not any(record["phase"] == "missing-context" for record in frames)
    assert frames[-1]["phase"] == "resume"
    assert source.read_bytes() == models[1].path.read_bytes() == original
