import asyncio
import gc
import threading
import weakref
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
async def test_falsey_session_factory_is_used_for_activation_and_inference(tmp_path, monkeypatch):
    path = tmp_path / "injected.onnx"
    path.write_bytes(b"injected session fixture")
    model = ModelRef("injected", "test:injected")
    session = FakeSession()
    calls = []

    class Factory:
        def __bool__(self):
            return False

        def __call__(self, model_path, providers):
            calls.append((model_path, providers))
            return session

    def unexpected_default(*args):
        pytest.fail("an explicitly supplied callable must not use the default session factory")

    monkeypatch.setattr("temms.adapters.onnx._create_session", unexpected_default)
    runtime = OnnxRuntime(
        models={"vision": [OnnxModel(model, path, ("CPUExecutionProvider",), {"x": "warm"})]},
        session_factory=Factory(),
    )

    activation = await runtime.activate("vision", model)
    result = await runtime.infer("vision", {"x": "request"})

    assert activation.active_model == result.model == model
    assert result.output == ("request",)
    assert calls == [(str(path), ("CPUExecutionProvider",))]
    assert session.calls == [{"x": "warm"}, {"x": "request"}]


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
@pytest.mark.parametrize("operation", ["state", "infer"])
async def test_unknown_slot_queries_do_not_retain_caller_slot_names(operation):
    class Slot(str):
        __slots__ = ("__weakref__",)

    runtime = OnnxRuntime(models={})
    references = []
    for index in range(32):
        slot = Slot(f"unknown-{index}")
        references.append(weakref.ref(slot))
        if operation == "state":
            assert (await runtime.state(slot)).active_model is None
        else:
            with pytest.raises(NoActiveModelError):
                await runtime.infer(slot, {"x": "value"})
    del slot
    gc.collect()  # Release unreachable inference-exception traceback frames too.

    assert all(reference() is None for reference in references)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_fails", [False, True], ids=["returns", "raises"])
async def test_onnx_inference_stays_on_old_session_across_swap(tmp_path: Path, old_fails) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, Operator

    first_path = tmp_path / "first.onnx"
    second_path = tmp_path / "second.onnx"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    first = ModelRef(id="first", digest="sha256:first")
    second = ModelRef(id="second", digest="sha256:second")
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    failure = RuntimeError("admitted first-model inference failed")
    calls, constructions = [], []

    class BlockingSession:
        def run(self, output_names, input_feed):
            calls.append(("first", input_feed["x"]))
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=10), "test did not release admitted inference"
            if old_fails:
                raise failure
            return [f"first:{input_feed['x']}"]

    class ImmediateSession:
        def run(self, output_names, input_feed):
            calls.append(("second", input_feed["x"]))
            return [f"second:{input_feed['x']}"]

    def factory(model_path: str, providers):
        constructions.append(model_path)
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
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector([
        ModelPolicy(ref, constraints=(Constraint("mode", Operator.EQ, ref.id),))
        for ref in (first, second)
    ]))
    executor = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(executor)
    in_flight = None
    try:
        await asyncio.wait_for(temms.reconcile("vision", {"mode": first.id}), 2)
        in_flight = asyncio.create_task(temms.infer("vision", {"x": "old-request"}))
        await asyncio.wait_for(started.wait(), 2)
        published = await asyncio.wait_for(temms.reconcile("vision", {"mode": second.id}), 2)
        assert published.activation.active_model == second
        assert (await runtime.state("vision")).active_model == second
        assert not in_flight.done() and not release.is_set()
        before = await asyncio.wait_for(temms.infer("vision", {"x": "before-release"}), 2)
        assert before.model == second and before.output == ("second:before-release",)
        assert not in_flight.done()

        release.set()
        if old_fails:
            with pytest.raises(RuntimeError) as caught:
                await asyncio.wait_for(in_flight, 2)
            assert caught.value is failure
        else:
            old_result = await asyncio.wait_for(in_flight, 2)
            assert old_result.model == first and old_result.output == ("first:old-request",)
        assert (await runtime.state("vision")).active_model == second
        after = await asyncio.wait_for(temms.infer("vision", {"x": "after-release"}), 2)
        assert after.model == second and after.output == ("second:after-release",)
        assert calls == [("first", "old-request"), ("second", "before-release"),
                         ("second", "after-release")]
        assert constructions == [str(first_path), str(second_path)]
    finally:
        release.set()
        try:
            if in_flight is not None:
                in_flight.cancel()
                await asyncio.wait_for(asyncio.gather(in_flight, return_exceptions=True), 2)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "construction", "warmup"])
async def test_failed_replacement_preserves_session_and_allows_retry(tmp_path, failure) -> None:
    from temms import (
        TEMMS,
        BestFeasibleSelector,
        Constraint,
        ModelPolicy,
        ModelUnavailableError,
        Operator,
    )

    first = ModelRef("first", "sha256:first")
    second = ModelRef("second", "sha256:second")
    first_path, second_path = tmp_path / "first.onnx", tmp_path / "second.onnx"
    first_path.write_bytes(b"fixture")
    if failure != "missing":
        second_path.write_bytes(b"fixture")
    failing = True
    old_session = FakeSession()

    class ReplacementSession(FakeSession):
        def run(self, output_names, input_feed):
            if failing and failure == "warmup":
                raise ValueError("replacement warmup failed")
            return [f"second:{input_feed['x']}"]

    def factory(path, providers):
        if path == str(first_path):
            return old_session
        if failing and failure == "construction":
            raise ValueError("replacement construction failed")
        return ReplacementSession()

    runtime = OnnxRuntime(
        models={"vision": [OnnxModel(first, first_path),
                           OnnxModel(second, second_path, warmup={"x": "warm"})]},
        session_factory=factory,
    )
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector([
        ModelPolicy(ref, constraints=(Constraint("mode", Operator.EQ, ref.id),))
        for ref in (first, second)
    ]))
    await temms.reconcile("vision", {"mode": "first"})
    error = ModelUnavailableError if failure == "missing" else ValueError
    message = "file does not exist.*second.onnx" if failure == "missing" else failure
    with pytest.raises(error, match=message):
        await temms.reconcile("vision", {"mode": "second"})

    assert (await runtime.state("vision")).active_model == first
    result = await temms.infer("vision", {"x": "after failure"})
    assert result.model == first
    assert result.output == ("after failure",)
    assert old_session.calls == [{"x": "after failure"}]

    failing = False
    second_path.write_bytes(b"fixture")
    recovered = await asyncio.wait_for(temms.reconcile("vision", {"mode": "second"}), 2)
    assert recovered.activation.active_model == second
    result = await temms.infer("vision", {"x": "after retry"})
    assert result.model == second
    assert result.output == ("second:after retry",)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["construction", "warmup"])
async def test_cancelled_replacement_cannot_publish_late(tmp_path, phase) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, Operator

    refs = [ModelRef(name, f"sha256:{name}") for name in ("first", "cancelled", "third")]
    configs = []
    for ref in refs:
        path = tmp_path / f"{ref.id}.onnx"
        path.write_bytes(b"fixture")
        configs.append(OnnxModel(ref, path, warmup={"x": "warm"}))
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    executor = ThreadPoolExecutor(max_workers=3)
    loop.set_default_executor(executor)

    def block_worker():
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(timeout=5), "test did not release replacement worker"
        finally:
            worker_finished.set()

    class Session:
        def __init__(self, name):
            self.name = name

        def run(self, output_names, input_feed):
            if self.name == "cancelled" and phase == "warmup":
                block_worker()
            return [f"{self.name}:{input_feed['x']}"]

    def factory(path, providers):
        name = Path(path).stem
        if name == "cancelled" and phase == "construction":
            block_worker()
        return Session(name)

    runtime = OnnxRuntime(models={"vision": configs}, session_factory=factory)
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector([
        ModelPolicy(ref, constraints=(Constraint("mode", Operator.EQ, ref.id),))
        for ref in refs
    ]))
    replacement = asyncio.create_task(temms.reconcile("vision", {"mode": "first"}))
    try:
        await asyncio.wait_for(replacement, 2)
        replacement = asyncio.create_task(temms.reconcile("vision", {"mode": "cancelled"}))
        await asyncio.wait_for(entered.wait(), 2)
        replacement.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(replacement, 2)
        assert not worker_finished.is_set()  # Coroutine cancellation did not stop the thread.
        assert (await runtime.state("vision")).active_model == refs[0]
        result = await asyncio.wait_for(temms.infer("vision", {"x": "request"}), 2)
        assert result.model == refs[0]
        assert result.output == ("first:request",)

        recovered = await asyncio.wait_for(temms.reconcile("vision", {"mode": "third"}), 2)
        assert recovered.activation.active_model == refs[2]
        result = await asyncio.wait_for(temms.infer("vision", {"x": "request"}), 2)
        assert result.model == refs[2]
        assert result.output == ("third:request",)
    finally:
        release.set()
        replacement.cancel()
        await asyncio.wait_for(asyncio.gather(replacement, return_exceptions=True), 2)
        # All workers have bounded waits; join before testing for late publication.
        executor.shutdown(wait=True, cancel_futures=True)
    assert worker_finished.is_set()
    assert (await runtime.state("vision")).active_model == refs[2]


async def _probe_other_slot(runtime, model, owner=None):
    assert (await runtime.state("other")).active_model is None
    if owner is None:
        activation = await runtime.activate("other", model)
    else:
        activation = (await owner.reconcile("other", {"mode": "other"})).activation
    assert (await runtime.state("other")).active_model == model
    return activation, await runtime.infer("other", {"x": "request"})


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["construction", "warmup"])
@pytest.mark.parametrize("other_thread", [False, True], ids=["same-loop", "other-thread"])
async def test_blocked_replacement_preserves_serving_and_slot_progress(
    tmp_path, phase, other_thread,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, Operator

    refs = [ModelRef(name, f"sha256:{name}") for name in ("old", "replacement", "other")]
    configs = []
    for ref in refs:
        path = tmp_path / f"{ref.id}.onnx"
        path.write_bytes(b"fixture")
        configs.append(OnnxModel(ref, path, warmup={"x": "warm"}))
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def block_worker():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=10), "test did not release replacement worker"

    class Session:
        def __init__(self, path, providers):
            self.name = Path(path).stem
            if self.name == "replacement" and phase == "construction":
                block_worker()

        def run(self, output_names, input_feed):
            if self.name == "replacement" and phase == "warmup" and input_feed["x"] == "warm":
                block_worker()
            return [f"{self.name}:{input_feed['x']}"]

    runtime = OnnxRuntime(
        models={"vision": configs[:2], "other": configs[2:]}, session_factory=Session,
    )
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector([
        ModelPolicy(ref, constraints=(Constraint("mode", Operator.EQ, ref.id),))
        for ref in refs
    ]))

    # Leave one worker available while replacement construction or warmup is blocked.
    executor = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(executor)
    replacement = asyncio.create_task(temms.reconcile("vision", {"mode": "old"}))
    pending = [replacement]
    try:
        await asyncio.wait_for(replacement, 2)
        replacement = asyncio.create_task(temms.reconcile("vision", {"mode": "replacement"}))
        pending.append(replacement)
        await asyncio.wait_for(entered.wait(), 2)

        # The other thread uses direct runtime calls; it never receives the TEMMS owner.
        probe = asyncio.create_task(
            asyncio.to_thread(lambda: asyncio.run(_probe_other_slot(runtime, refs[2])))
            if other_thread else _probe_other_slot(runtime, refs[2], temms)
        )
        pending.append(probe)
        activation, result = await asyncio.wait_for(probe, 2)
        assert activation.active_model == refs[2]
        assert result.model == refs[2]
        assert result.output == ("other:request",)
        result = await asyncio.wait_for(temms.infer("vision", {"x": "request"}), 2)
        assert result.model == refs[0]
        assert result.output == ("old:request",)
        assert (await runtime.state("vision")).active_model == refs[0]
        assert not replacement.done() and not release.is_set()

        release.set()
        completed = await asyncio.wait_for(replacement, 2)
        assert completed.activation.active_model == refs[1]
        result = await asyncio.wait_for(temms.infer("vision", {"x": "request"}), 2)
        assert result.model == refs[1]
        assert result.output == ("replacement:request",)
        assert (await runtime.state("vision")).active_model == refs[1]
        assert (await runtime.state("other")).active_model == refs[2]
    finally:
        release.set()
        for task in pending:
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 2)
        finally:
            # Also join a cancelled probe's thread and its asyncio.run-owned worker pool.
            executor.shutdown(wait=True, cancel_futures=True)
