from types import SimpleNamespace

import pytest


@pytest.fixture
def configured_session(monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from examples.live_swap import run

    observed = SimpleNamespace(options=None, reported=None)
    session = SimpleNamespace(
        get_session_options=lambda: observed.reported or observed.options,
        get_inputs=lambda: [
            SimpleNamespace(name="data", type="tensor(float)", shape=[1, 3, 224, 224]),
        ],
        get_outputs=lambda: [SimpleNamespace(shape=[1, 1000])],
    )

    def construct(path, options, providers):
        assert path == "fixture.onnx" and providers == ["CPUExecutionProvider"]
        observed.options = options
        return session

    monkeypatch.setattr(run, "ort", SimpleNamespace(
        SessionOptions=SimpleNamespace, InferenceSession=construct,
    ))
    return run, observed, session


@pytest.mark.parametrize("kwargs,expected", [
    ({}, True), ({"cpu_arena": True}, True), ({"cpu_arena": False}, False),
])
def test_cpu_session_applies_arena_setting_and_preserves_thread_defaults(
    configured_session, kwargs, expected,
):
    run, observed, session = configured_session
    assert run.cpu_session("fixture.onnx", ("CPUExecutionProvider",), **kwargs) is session
    assert observed.options.enable_cpu_mem_arena is expected
    assert observed.options.intra_op_num_threads == observed.options.inter_op_num_threads == 1


def test_cpu_session_rejects_unapplied_arena_setting(configured_session):
    run, observed, _ = configured_session
    observed.reported = SimpleNamespace(enable_cpu_mem_arena=True)
    with pytest.raises(ValueError, match="did not apply the requested CPU arena"):
        run.cpu_session("fixture.onnx", ("CPUExecutionProvider",), cpu_arena=False)
