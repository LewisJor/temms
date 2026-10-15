from types import SimpleNamespace

import pytest

from temms import InferenceResult, ModelRef
from temms.adapters import InMemoryRuntime


@pytest.fixture
def overhead():
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from examples.live_swap import overhead

    return overhead


@pytest.mark.asyncio
async def test_paired_timings_alternate_order_and_preserve_negative_differences(
    overhead, monkeypatch,
):
    ref = ModelRef("model", "digest")
    runtime = InMemoryRuntime(
        models={"vision": [ref]}, handlers={ref.digest: lambda feed: (overhead.np.array([1]),)},
    )
    await runtime.activate("vision", ref)
    timestamps = iter([10, 20, 30, 50, 60, 65, 90, 100])
    monkeypatch.setattr(overhead, "time", SimpleNamespace(perf_counter_ns=lambda: next(timestamps)))
    records = []

    await overhead.measure(runtime, ref, {"data": "prepared"}, 2, records.append)

    first, second, summary = records
    assert first["order"] == ("direct", "wrapped")
    assert second["order"] == ("wrapped", "direct")
    assert (first["direct_ns"], first["wrapped_ns"], first["delta_ns"]) == (10, 20, 10)
    assert (second["direct_ns"], second["wrapped_ns"], second["delta_ns"]) == (10, 5, -5)
    assert summary["pairs"] == 2
    assert summary["delta_ns"] == {"mean": 2.5, "p50": 2.5, "p95": 9.25}
    assert list(timestamps) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["attribution", "output"])
async def test_mismatched_pair_does_not_emit_a_measurement(overhead, mismatch):
    ref = ModelRef("model", "digest")
    calls = 0

    async def infer(slot, feed):
        nonlocal calls
        calls += 1
        model = ModelRef("wrong", "wrong") if calls == 2 and mismatch == "attribution" else ref
        value = calls if mismatch == "output" else 1
        return InferenceResult(model, (overhead.np.array([value]),))

    records = []
    error = RuntimeError if mismatch == "attribution" else AssertionError
    with pytest.raises(error):
        await overhead.measure(SimpleNamespace(infer=infer), ref, {}, 1, records.append)
    assert records == []
