import asyncio
import gc
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from temms import ModelRef
from temms.adapters import OnnxModel, OnnxRuntime


@pytest.fixture
async def sessions(tmp_path):
    models = [ModelRef(name, f"sha256:{name}") for name in ("old", "new", "last")]
    configs = []
    for model in models:
        path = tmp_path / f"{model.id}.onnx"
        path.write_bytes(b"session-lifetime fixture")
        configs.append(OnnxModel(model, path, warmup={"x": "warm"}))
    hooks, references, collected, releases = {}, [], [], []

    class Session:
        def __init__(self, name):
            self.name = name

        def run(self, names, feed):
            hooks.get("run", lambda name, value: None)(self.name, feed["x"])
            return [f"{self.name}:{feed['x']}"]

    def factory(path, providers):
        name = Path(path).stem
        session = Session(name)
        references.append(weakref.ref(session))
        weakref.finalize(session, collected.append, name)
        hooks.get("load", lambda name: None)(name)
        return session

    executor = ThreadPoolExecutor(max_workers=2)
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        yield SimpleNamespace(
            runtime=OnnxRuntime(models={"vision": configs}, session_factory=factory),
            models=models, hooks=hooks, references=references, collected=collected,
            releases=releases, executor=executor,
        )
    finally:
        for release in releases:
            release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def _block(sessions):
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    sessions.releases.append(release)

    def wait():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test did not release session worker"

    return entered, release, wait


async def _collect():
    # Both workers must finish prior jobs, while the executor remains usable.
    barrier = threading.Barrier(2)
    await asyncio.wait_for(asyncio.gather(
        asyncio.to_thread(barrier.wait, 2), asyncio.to_thread(barrier.wait, 2),
    ), 3)
    checkpoint = asyncio.Event()
    asyncio.get_running_loop().call_soon(checkpoint.set)
    await asyncio.wait_for(checkpoint.wait(), 2)
    gc.collect()


@pytest.mark.asyncio
@pytest.mark.parametrize("swaps", [1, 3])
async def test_retired_session_outlives_admitted_call_but_not_completed_result(sessions, swaps):
    runtime, models = sessions.runtime, sessions.models
    await runtime.activate("vision", models[0])
    entered, release, block = _block(sessions)
    sessions.hooks["run"] = lambda name, value: block() if value == "request" else None
    pending = asyncio.create_task(runtime.infer("vision", {"x": "request"}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for model in (models[1], models[2], models[1])[:swaps]:
            await runtime.activate("vision", model)
            gc.collect()
            assert sessions.references[0]() is not None
        release.set()
        result = await asyncio.wait_for(pending, 2)
    finally:
        release.set()
        pending.cancel()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
    await _collect()
    assert pending.result() is result  # Retain both the successful task and its result.
    assert result.model == models[0] and result.output == ("old:request",)
    assert all(reference() is None for reference in sessions.references[:-1])
    assert sessions.references[-1]() is not None and "old" in sessions.collected
    assert (await runtime.infer("vision", {"x": "still usable"})).model == models[1]


@pytest.mark.asyncio
async def test_repeated_replacements_retain_only_the_active_session(sessions):
    results = []
    for model in sessions.models * 4:
        results.append(await sessions.runtime.activate("vision", model))
    await _collect()
    assert len(results) == len(sessions.references) == 12
    assert all(reference() is None for reference in sessions.references[:-1])
    assert sessions.references[-1]() is not None and len(sessions.collected) == 11
    assert (await sessions.runtime.state("vision")).active_model == results[-1].active_model


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["construction", "warmup"])
async def test_cancelled_unpublished_session_collects_after_worker_finishes(sessions, phase):
    runtime, models = sessions.runtime, sessions.models
    await runtime.activate("vision", models[0])
    entered, release, block = _block(sessions)
    if phase == "construction":
        sessions.hooks["load"] = lambda name: block() if name == "new" else None
    else:
        sessions.hooks["run"] = lambda name, value: block() if name == "new" else None
    pending = asyncio.create_task(runtime.activate("vision", models[1]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert sessions.references[1]() is not None  # Cancelling did not terminate the worker.
        await runtime.activate("vision", models[2])
        assert (await runtime.infer("vision", {"x": "progress"})).output == ("last:progress",)
    finally:
        release.set()
        pending.cancel()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
    await _collect()
    assert pending.cancelled()  # Keep the cancelled caller referenced during collection.
    assert sessions.references[1]() is None and "new" in sessions.collected
    assert (await runtime.state("vision")).active_model == models[2]


@pytest.mark.asyncio
async def test_cancelled_inference_keeps_worker_session_alive_until_return(sessions):
    runtime, models = sessions.runtime, sessions.models
    await runtime.activate("vision", models[0])
    entered, release, block = _block(sessions)
    sessions.hooks["run"] = lambda name, value: block() if value == "request" else None
    pending = asyncio.create_task(runtime.infer("vision", {"x": "request"}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        await runtime.activate("vision", models[1])
        gc.collect()
        assert sessions.references[0]() is not None
        assert (await runtime.infer("vision", {"x": "progress"})).output == ("new:progress",)
    finally:
        release.set()
        pending.cancel()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
    await _collect()
    assert pending.cancelled() and sessions.references[0]() is None
    assert sessions.references[1]() is not None


@pytest.mark.asyncio
async def test_cancelled_inference_snapshots_mapping_but_borrows_mutable_values(
    sessions, monkeypatch,
):
    runtime, model = sessions.runtime, sessions.models[0]
    await runtime.activate("vision", model)
    entered, release, block = _block(sessions)
    borrowed = [7]
    feed = {"x": borrowed}
    observed = []
    session = sessions.references[0]()
    original_run = session.run

    def run(names, received):
        if isinstance(received["x"], list):
            block()
            observed.append((received is feed, received["x"] is borrowed, tuple(received["x"])))
        return original_run(names, received)

    monkeypatch.setattr(session, "run", run)
    pending = asyncio.create_task(runtime.infer("vision", feed))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert not release.is_set() and observed == []
        # Deliberate misuse demonstrates why cancellation does not permit buffer reuse.
        feed["x"] = [123]
        borrowed[0] = 99
    finally:
        release.set()
        pending.cancel()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
    await _collect()
    assert pending.cancelled()
    assert observed == [(False, True, (99,))]
    assert (await runtime.state("vision")).active_model == model
    result = await runtime.infer("vision", {"x": "progress"})
    assert result.model == model and result.output == ("old:progress",)


async def _capture_failure(runtime, model):
    try:
        await runtime.activate("vision", model)
    except ValueError as error:
        return error
    pytest.fail("replacement should have failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["construction", "warmup"])
async def test_failed_session_collects_after_caller_releases_exception(sessions, phase):
    runtime, models = sessions.runtime, sessions.models
    await runtime.activate("vision", models[0])

    def fail(name, value=None):
        if name == "new":
            raise ValueError("replacement failed")

    sessions.hooks["load" if phase == "construction" else "run"] = fail
    error = await _capture_failure(runtime, models[1])
    assert str(error) == "replacement failed"
    assert (await runtime.state("vision")).active_model == models[0]
    await runtime.activate("vision", models[2])
    await _collect()
    assert sessions.references[1]() is not None  # Caller-held traceback includes the session.
    del error
    await _collect()
    assert sessions.references[1]() is None and "new" in sessions.collected
    assert sessions.references[-1]() is not None
