import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from temms import BestFeasibleSelector, Constraint, ModelPolicy, ModelRef, Operator
from temms.adapters import InMemoryRuntime, OnnxModel, OnnxRuntime


@pytest.fixture
def demo():
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("PIL")
    from examples.webots.controllers.temms_camera import temms_camera

    return temms_camera


def _camera(data, width=2, height=1):
    return SimpleNamespace(getImage=lambda: data, getWidth=lambda: width, getHeight=lambda: height)


def test_camera_copy_preserves_rgb_and_inference_input_after_next_frame(demo):
    buffer = bytearray([30, 20, 10, 255, 60, 50, 40, 255])
    image = demo.camera_image(_camera(buffer))
    digest = demo.rgb_digest(image)
    feed = demo.captured_feed(image, digest)["data"]
    frozen = feed.copy()
    buffer[:] = bytes(len(buffer))
    assert image.mode == "RGB" and image.size == (2, 1)
    assert image.tobytes() == bytes([10, 20, 30, 40, 50, 60])
    assert demo.rgb_digest(image) == digest
    assert feed.shape == (1, 3, 224, 224) and feed.dtype == demo.run.np.float32
    assert demo.run.np.isfinite(feed).all() and not feed.flags.writeable
    demo.run.np.testing.assert_array_equal(feed, frozen)
    image.putpixel((0, 0), (1, 2, 3))
    with pytest.raises(ValueError, match="camera pixels changed"):
        demo.captured_feed(image, digest)


@pytest.mark.parametrize("data,width,height", [
    (None, 2, 1), (b"", 2, 1), (bytes(7), 2, 1), (bytes(8), 0, 1), (bytes(8), 2, -1),
])
def test_camera_rejects_incomplete_frames(demo, data, width, height):
    with pytest.raises(ValueError, match="incomplete BGRA image"):
        demo.camera_image(_camera(data, width, height))


@pytest.mark.asyncio
async def test_capture_runs_on_every_request_and_supplies_the_inference_feed(
    demo, monkeypatch, tmp_path,
):
    run = demo.run
    ref = ModelRef("small", "small-digest")
    seen, inferred, records = [], [], []
    scores = [run.np.eye(1, 1000, index, dtype=run.np.float32) for index in range(2)]
    samples = [{
        "image": {"name": str(index), "sha256": str(index)},
        "expected": {"index": index, "label": str(index)},
        "feed": {"data": "stale preflight feed"},
        "references": {(ref.id, ref.digest): scores[index]},
    } for index in range(2)]

    def capture(sample, request):
        seen.append((request, sample["image"]["name"]))
        return sample | {"feed": {"data": scores[(request - 1) % 2], "request": request}}

    def infer(feed):
        inferred.append(feed["request"])
        return (feed["data"],)

    runtime = InMemoryRuntime(models={"vision": [ref]}, handlers={ref.digest: infer})
    selector = BestFeasibleSelector([
        ModelPolicy(ref, 1, (Constraint("battery", Operator.GTE, 10),)),
    ])
    monkeypatch.setattr(run, "SCENARIO", (
        ("start-small", 20, False), ("no-feasible-model", 5, False), ("recover", 20, False),
    ))
    await run.replay(
        runtime, selector, samples, [str(i) for i in range(1000)], tmp_path / "unused.onnx",
        records.append, frames=2, interval=0, capture=capture,
    )
    frames = [record for record in records if record["event"] == "frame"]
    assert seen == [(i, str((i - 1) % 2)) for i in range(1, 7)]
    assert [frame["outcome"] for frame in frames] == [
        "waiting", "ok", "abstained", "abstained", "ok", "ok",
    ]
    assert inferred == [2, 5, 6]
    assert all(frame["reference_match"] for frame in frames if frame["outcome"] == "ok")


def _controlled_factory(path, reached, release, finished, scores):
    def infer(names, feed):
        assert reached.wait(3), "transition did not reach the controlled boundary"
        return [scores]

    def factory(candidate, providers):
        if Path(candidate) == path:
            reached.set()
            assert release.wait(5), "abandoned load was not released"
            finished.set()
        return SimpleNamespace(run=infer)

    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [False, True], ids=["pending-replacement", "hidden-artifact"])
async def test_capture_failure_settles_transition_and_restores_artifact(
    demo, monkeypatch, tmp_path, fault,
):
    run = demo.run
    refs = [ModelRef(name, name) for name in ("small", "large")]
    paths = [tmp_path / f"{ref.id}.onnx" for ref in refs]
    original = b"\x00private model\xff"
    for path in paths:
        path.write_bytes(original)
    stage = "injected-missing-artifact" if fault else "more-capacity"
    monkeypatch.setattr(run, "SCENARIO", ((stage, 80, fault), ("recover", 20, False)))
    reached, release, finished = threading.Event(), threading.Event(), threading.Event()
    scores = run.np.zeros((1, 1000))
    records, transitions = [], []
    failure = ValueError("camera pixels changed during replay")

    def emit(record):
        records.append(record)
        if record["event"] == "activation" and record["activation"] == "failed":
            reached.set()

    def capture(sample, request):
        if request == 2:
            raise failure
        return sample

    def create_task(coroutine):
        task = asyncio.create_task(coroutine)
        transitions.append(task)
        return task

    monkeypatch.setattr(run, "asyncio", SimpleNamespace(
        create_task=create_task, gather=asyncio.gather, sleep=asyncio.sleep,
    ))
    runtime = OnnxRuntime(models={"vision": [
        OnnxModel(ref, path) for ref, path in zip(refs, paths, strict=True)
    ]}, session_factory=_controlled_factory(paths[1], reached, release, finished, scores))
    selector = BestFeasibleSelector([ModelPolicy(ref, index) for index, ref in enumerate(refs)])
    samples = [{"image": {"name": "camera.png", "sha256": "digest"},
                "expected": {"index": 0, "label": "label"}, "feed": {"data": "capture"},
                "references": {(ref.id, ref.digest): scores for ref in refs}}]
    executor = ThreadPoolExecutor(max_workers=2)
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        await runtime.activate("vision", refs[0])
        with pytest.raises(ValueError) as caught:
            async with asyncio.timeout(3):
                await run.replay(runtime, selector, samples, ["label"] * 1000, paths[1],
                                 emit, frames=2, interval=0, capture=capture)
        assert caught.value is failure
        assert len(transitions) == 1 and transitions[0].done()
        assert transitions[0].cancelled() is (not fault)
        assert (await runtime.state("vision")).active_model == refs[0]
        assert paths[1].read_bytes() == original
        assert not paths[1].with_suffix(".unavailable").exists()
        assert len([r for r in records if r["event"] == "frame"]) == 1
        assert all(r.get("stage") == stage for r in records)
        release.set()
        barrier = threading.Barrier(2)
        await asyncio.wait_for(asyncio.gather(
            asyncio.to_thread(barrier.wait, 2), asyncio.to_thread(barrier.wait, 2),
        ), 3)
        assert finished.is_set() is (not fault)
        assert (await runtime.state("vision")).active_model == refs[0]
    finally:
        reached.set()
        release.set()
        for transition in transitions:
            transition.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*transitions, return_exceptions=True), 3)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_stopped_simulator_cancels_and_awaits_replay_cleanup(demo, monkeypatch):
    settled = asyncio.Event()

    async def replay():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            settled.set()

    monkeypatch.setattr(demo.run, "replay", replay)
    supervisor = SimpleNamespace(step=lambda interval: -1)
    with pytest.raises(ExceptionGroup) as caught:
        async with asyncio.timeout(2):
            await demo.replay_with_steps(supervisor)
    assert settled.is_set()
    assert len(caught.value.exceptions) == 1
    assert "Webots stopped" in str(caught.value.exceptions[0])


@pytest.mark.parametrize("failure", [None, "encoder", "missing", "empty"])
def test_movie_completion_rejects_encoder_failure_and_missing_output(demo, tmp_path, failure):
    movie = tmp_path / "movie.mp4"
    if failure != "missing":
        movie.write_bytes(b"" if failure == "empty" else b"encoded movie fixture")
    stopped = []
    supervisor = SimpleNamespace(
        movieStopRecording=lambda: stopped.append(True), movieIsReady=lambda: True,
        movieFailed=lambda: failure == "encoder",
    )
    if failure is None:
        demo.finish_movie(supervisor, movie)
    else:
        with pytest.raises(RuntimeError, match="failed to encode"):
            demo.finish_movie(supervisor, movie)
    assert stopped == [True]


def test_movie_encoding_timeout_is_bounded_without_real_sleep(demo, monkeypatch, tmp_path):
    clock = iter([0, 2])
    monkeypatch.setattr(demo.time, "monotonic", lambda: next(clock))
    supervisor = SimpleNamespace(movieStopRecording=lambda: None, movieIsReady=lambda: False)
    with pytest.raises(TimeoutError, match="encoding did not finish"):
        demo.finish_movie(supervisor, tmp_path / "movie.mp4", timeout=1)


@pytest.mark.parametrize("fail", [False, True], ids=["completed", "controller-error"])
def test_controller_exit_status_follows_demonstration_outcome(demo, monkeypatch, tmp_path, fail):
    exits = []
    supervisor = SimpleNamespace(simulationQuit=exits.append)
    monkeypatch.setitem(sys.modules, "controller", SimpleNamespace(Supervisor=lambda: supervisor))
    monkeypatch.setenv("TEMMS_WEBOTS_OUTPUT", str(tmp_path / "new-output"))
    monkeypatch.setenv("TEMMS_WEBOTS_QUIT", "1")

    def demonstrate(supervisor, assets, output):
        if fail:
            raise ValueError("invalid camera output")

    monkeypatch.setattr(demo, "demonstrate", demonstrate)
    assert demo.main() == int(fail)
    assert exits == [int(fail)]
