import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from temms import BestFeasibleSelector, Constraint, ModelPolicy, ModelRef, Operator
from temms.adapters import OnnxModel, OnnxRuntime


class _FailingSession:
    def __init__(self, index, reached, failure, scores):
        self.index, self.reached, self.failure, self.scores = index, reached, failure, scores

    def run(self, names, feed):
        if feed["data"] == "request":
            assert self.index == 0
            assert self.reached.wait(3), "replacement did not reach the controlled boundary"
            raise self.failure
        return [self.scores]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [False, True], ids=["pending-replacement", "hidden-artifact"])
async def test_inference_failure_settles_transition_and_restores_private_artifact(
    tmp_path, monkeypatch, fault,
):
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from examples.live_swap import run

    refs = [ModelRef(name, f"sha256:{name}") for name in ("small", "large")]
    paths = [tmp_path / f"{ref.id}.onnx" for ref in refs]
    original = b"\x00private model artifact\xff"
    for path in paths:
        path.write_bytes(original)
    stage = "injected-missing-artifact" if fault else "more-capacity"
    monkeypatch.setattr(run, "SCENARIO", ((stage, 80, fault), ("recover", 20, False)))
    reached, release, finished = threading.Event(), threading.Event(), threading.Event()
    records, transitions, constructed = [], [], []
    failure = RuntimeError("injected inference failure")

    def factory(path, providers):
        index = paths.index(Path(path))
        constructed.append(index)
        session = _FailingSession(index, reached, failure, run.np.zeros((1, 1000)))
        if index == 1:
            reached.set()
            assert release.wait(5), "test did not release abandoned load"
            finished.set()
        return session

    def emit(record):
        records.append(record)
        if record["event"] == "activation" and record["activation"] == "failed":
            assert not paths[1].exists()
            assert paths[1].with_suffix(".unavailable").read_bytes() == original
            reached.set()

    def create_task(coroutine):
        task = asyncio.create_task(coroutine)
        transitions.append(task)
        return task

    monkeypatch.setattr(run, "asyncio", SimpleNamespace(
        create_task=create_task, gather=asyncio.gather, sleep=asyncio.sleep,
    ))
    runtime = OnnxRuntime(
        models={"vision": [
            OnnxModel(ref, path, warmup={"data": "warmup"})
            for ref, path in zip(refs, paths, strict=True)
        ]}, session_factory=factory,
    )
    selector = BestFeasibleSelector([
        ModelPolicy(refs[0], 10, (Constraint("battery", Operator.GTE, 10),)),
        ModelPolicy(refs[1], 20, (Constraint("battery", Operator.GTE, 40),)),
    ])
    executor = ThreadPoolExecutor(max_workers=2)
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        await runtime.activate("vision", refs[0])
        with pytest.raises(RuntimeError) as caught:
            # Direct await keeps the settlement assertion in the same caller turn.
            async with asyncio.timeout(3):
                await run.replay(runtime, selector, {"data": "request"}, ["label"] * 1000,
                                 paths[1], emit, frames=2, interval=0)
        assert caught.value is failure
        assert len(transitions) == 1 and transitions[0].done()
        assert transitions[0].cancelled() is (not fault)
        assert (await runtime.state("vision")).active_model == refs[0]
        assert [record["stage"] for record in records if record["event"] == "stage"] == [stage]
        assert not any(record["event"] in {"summary", "frame"} for record in records)
        assert paths[1].read_bytes() == original
        assert not paths[1].with_suffix(".unavailable").exists()

        release.set()
        barrier = threading.Barrier(2)
        await asyncio.wait_for(asyncio.gather(
            asyncio.to_thread(barrier.wait, 2), asyncio.to_thread(barrier.wait, 2),
        ), 3)
        assert finished.is_set() is (not fault)
        assert constructed == ([0] if fault else [0, 1])
        assert (await runtime.state("vision")).active_model == refs[0]
        assert not any(record["event"] == "summary" for record in records)
    finally:
        reached.set()
        release.set()
        for transition in transitions:
            transition.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*transitions, return_exceptions=True), 3)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
