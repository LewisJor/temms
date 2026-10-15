import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from temms import ModelRef
from temms.adapters import OnnxModel, OnnxRuntime


@pytest.fixture
async def case(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from examples.live_swap import compare

    refs = [ModelRef(name, f"sha256:{name}") for name in ("mobilenetv2", "resnet18")]
    paths = [tmp_path / f"{ref.id}.onnx" for ref in refs]
    for path in paths:
        path.write_bytes(b"private fixture")
    hooks, calls, releases, activation_tasks = {}, [], [], []

    class Session:
        def __init__(self, index):
            self.index = index

        def run(self, names, feed):
            calls.append(("infer", self.index))
            hooks.get("infer", lambda index: None)(self.index)
            scores = np.zeros((1, 1000), dtype=np.float32)
            scores[0, self.index] = 1
            return [scores]

    def factory(path, providers):
        index = paths.index(Path(path))
        calls.append(("load", index))
        hooks.get("load", lambda index: None)(index)
        return Session(index)

    class Runtime(OnnxRuntime):
        async def activate(self, slot, model):
            activation_tasks.append((model, asyncio.current_task()))
            result = await super().activate(slot, model)
            hooks.get("published", lambda model: None)(model)
            return result

    runtime = Runtime(
        models={"vision": [OnnxModel(ref, path) for ref, path in zip(refs, paths, strict=True)]},
        session_factory=factory,
    )
    monkeypatch.setattr(compare.assets, "preprocess", lambda path: {"data": path.name})
    executor = ThreadPoolExecutor(max_workers=2)
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        yield SimpleNamespace(
            compare=compare, runtime=runtime, refs=refs, large=paths[1], directory=tmp_path,
            hooks=hooks, calls=calls, releases=releases, activation_tasks=activation_tasks,
        )
    finally:
        for release in releases:
            release.set()
        executor.shutdown(wait=True, cancel_futures=True)


async def _evaluate(case, records, emit=None, period_ms=0):
    return await case.compare.evaluate(
        case.runtime, case.compare.policies(case.refs, "adaptive-background"),
        "adaptive-background", [{"name": "image", "expected_synset": "n0"}],
        case.directory, [f"n{i} class-{i}" for i in range(1000)],
        case.large, emit or records.append, period_ms=period_ms,
    )


def _blocking_load(case, index):
    loop = asyncio.get_running_loop()
    entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    case.releases.append(release)

    def load(actual):
        if actual == index:
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(5), "test did not release model construction"
            finally:
                finished.set()

    case.hooks["load"] = load
    return entered, release, finished


@pytest.mark.asyncio
async def test_background_inference_keeps_captured_identity_after_publication(case, monkeypatch):
    monkeypatch.setattr(case.compare.run, "SCENARIO", (("up", 80, False),))
    await case.runtime.activate("vision", case.refs[0])
    loading, publish, _ = _blocking_load(case, 1)
    inferred, published, finish = asyncio.Event(), asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    case.releases.append(finish)

    def infer(index):
        if index == 0:
            loop.call_soon_threadsafe(inferred.set)
            assert finish.wait(5), "test did not release old inference"

    case.hooks["infer"] = infer
    case.hooks["published"] = lambda model: published.set()
    records = []
    task = asyncio.create_task(_evaluate(case, records))
    try:
        await asyncio.wait_for(loading.wait(), 2)
        await asyncio.wait_for(inferred.wait(), 2)
        publish.set()
        await asyncio.wait_for(published.wait(), 2)
        finish.set()
        await asyncio.wait_for(task, 2)
    finally:
        publish.set()
        finish.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    request = next(r for r in records if r["event"] == "request")
    assert request["active"] == case.compare.run.identity(case.refs[0])
    assert request["active_at_completion"] == case.compare.run.identity(case.refs[1])
    assert request["serving"] == case.compare.run.identity(case.refs[0])
    assert request["prediction"]["index"] == 0 and request["correct"]


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_active", [False, True])
async def test_background_waits_when_active_model_is_absent_or_infeasible(
    case, monkeypatch, initially_active,
):
    monkeypatch.setattr(case.compare.run, "SCENARIO", (("low", 20, False),))
    if initially_active:
        await case.runtime.activate("vision", case.refs[1])
    loading, release, _ = _blocking_load(case, 0)
    records = []
    task = asyncio.create_task(_evaluate(case, records))
    try:
        await asyncio.wait_for(loading.wait(), 2)
        assert not any(event == "infer" for event, _ in case.calls)
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        release.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    request = next(r for r in records if r["event"] == "request")
    assert request["outcome"] == "ok"
    assert request["serving"] == case.compare.run.identity(case.refs[0])
    assert [index for event, index in case.calls if event == "infer"] == [0]


@pytest.mark.asyncio
async def test_background_failure_preserves_serving_and_restores_before_retry(case, monkeypatch):
    monkeypatch.setattr(case.compare.run, "SCENARIO", (("fault", 80, True), ("retry", 80, False)))
    await case.runtime.activate("vision", case.refs[0])
    records = []
    await asyncio.wait_for(_evaluate(case, records), 2)
    activations = {r["stage"]: r for r in records if r["event"] == "activation"}
    assert activations["fault"]["activation"] == "failed"
    assert not activations["fault"]["unexpected_error"]
    assert activations["fault"]["active"] == case.compare.run.identity(case.refs[0])
    assert activations["retry"]["activation"] == "published"
    assert activations["retry"]["active"] == case.compare.run.identity(case.refs[1])
    fault = next(r for r in records if r["event"] == "request" and r["stage"] == "fault")
    assert fault["outcome"] == "ok" and fault["serving"] == case.compare.run.identity(case.refs[0])
    assert case.large.read_bytes() == b"private fixture"
    assert not case.large.with_suffix(".unavailable").exists()


@pytest.mark.asyncio
async def test_background_writer_failure_cancels_pending_activation(case, monkeypatch):
    monkeypatch.setattr(case.compare.run, "SCENARIO", (("up", 80, False),))
    await case.runtime.activate("vision", case.refs[0])
    loading, release, finished = _blocking_load(case, 1)
    write_failed = asyncio.Event()

    def emit(record):
        if record["event"] == "request":
            write_failed.set()
            raise OSError("record write failed")

    task = asyncio.create_task(_evaluate(case, [], emit))
    try:
        await asyncio.wait_for(loading.wait(), 2)
        await asyncio.wait_for(write_failed.wait(), 2)
        with pytest.raises(OSError, match="record write failed"):
            await asyncio.wait_for(task, 2)
        assert all(t.done() for model, t in case.activation_tasks if model == case.refs[1])
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert (await case.runtime.state("vision")).active_model == case.refs[0]
    finally:
        release.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_background_cancellation_prevents_late_publication(case, monkeypatch):
    monkeypatch.setattr(case.compare.run, "SCENARIO", (("up", 80, False),))
    await case.runtime.activate("vision", case.refs[0])
    loading, release, finished = _blocking_load(case, 1)
    request_written = asyncio.Event()

    def emit(record):
        if record["event"] == "request":
            request_written.set()

    task = asyncio.create_task(_evaluate(case, [], emit))
    try:
        await asyncio.wait_for(loading.wait(), 2)
        await asyncio.wait_for(request_written.wait(), 2)  # Now draining at the phase boundary.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert all(t.done() for model, t in case.activation_tasks if model == case.refs[1])
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert (await case.runtime.state("vision")).active_model == case.refs[0]
    finally:
        release.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_active", [False, True])
async def test_cancellation_during_initialization_cleans_up_replacement(
    case, monkeypatch, initially_active,
):
    monkeypatch.setattr(case.compare.run, "SCENARIO", (("low", 20, False),))
    if initially_active:
        await case.runtime.activate("vision", case.refs[1])
    loading, release, finished = _blocking_load(case, 0)
    records = []
    task = asyncio.create_task(_evaluate(case, records))
    try:
        await asyncio.wait_for(loading.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert all(t.done() for model, t in case.activation_tasks if model == case.refs[0])
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        expected = case.refs[1] if initially_active else None
        assert (await case.runtime.state("vision")).active_model == expected
        assert not records and not any(event == "infer" for event, _ in case.calls)
    finally:
        release.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_phase_drain_becomes_next_request_backlog(case, monkeypatch):
    monkeypatch.setattr(case.compare.run, "SCENARIO", (("up", 80, False), ("down", 20, False)))
    await case.runtime.activate("vision", case.refs[0])
    clock = SimpleNamespace(ms=0)
    loading, release, _ = _blocking_load(case, 1)
    blocked_load = case.hooks["load"]

    def load(index):
        blocked_load(index)
        if index == 1:
            clock.ms += 100

    async def pacing(seconds):
        clock.ms += seconds * 1000
        await asyncio.sleep(0)

    case.hooks["load"] = load
    case.hooks["infer"] = lambda index: setattr(clock, "ms", clock.ms + 20)
    monkeypatch.setattr(case.compare, "now_ms", lambda: clock.ms)
    facade = SimpleNamespace(**vars(asyncio))
    facade.sleep = pacing
    monkeypatch.setattr(case.compare, "asyncio", facade)
    records, first_written = [], asyncio.Event()

    def emit(record):
        records.append(record)
        if record["event"] == "request" and record["stage"] == "up":
            first_written.set()

    task = asyncio.create_task(_evaluate(case, records, emit, period_ms=10))
    try:
        await asyncio.wait_for(loading.wait(), 2)
        await asyncio.wait_for(first_written.wait(), 2)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        release.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    requests = [r for r in records if r["event"] == "request"]
    assert [r["scheduled_ms"] for r in requests] == [0, 10]
    assert [r["completed_ms"] for r in requests] == [20, 140]
    assert [r["queue_ms"] for r in requests] == [0, 110]
    assert [r["latency_ms"] for r in requests] == [20, 130]
    assert [r["late"] for r in requests] == [False, True]
