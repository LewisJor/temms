import json
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def churn():
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from examples.live_swap import churn

    return churn


def test_linux_current_rss_converts_kib_to_bytes(churn, monkeypatch):
    monkeypatch.setattr(churn.sys, "platform", "linux")

    def read(path):
        assert path == Path("/proc/self/smaps_rollup")
        return "header\nRss:                 123 kB\nPss:                  45 kB\n"

    monkeypatch.setattr(Path, "read_text", read)
    assert churn.current_rss() == (123 * 1024, None)


def test_darwin_current_rss_preserves_native_byte_units(churn, monkeypatch):
    monkeypatch.setattr(churn.sys, "platform", "darwin")
    monkeypatch.setattr(churn, "_darwin_rss", lambda: 123)
    assert churn.current_rss() == (123, None)


@pytest.mark.parametrize("platform", ["linux", "darwin", "unsupported"])
def test_unavailable_current_rss_is_reported_as_error_not_zero(churn, monkeypatch, platform):
    monkeypatch.setattr(churn.sys, "platform", platform)

    def unavailable(path=None):
        raise PermissionError("RSS denied")

    monkeypatch.setattr(Path, "read_text", unavailable)
    monkeypatch.setattr(churn, "_darwin_rss", unavailable)
    rss, error = churn.current_rss()
    assert rss is None
    assert error == ("PermissionError: RSS denied" if platform != "unsupported"
                     else "current RSS unsupported on unsupported")


@pytest.fixture
def measured(churn, tmp_path, monkeypatch):
    import numpy as np

    names, hooks, calls, arenas = ("small", "large"), {}, [], []
    manifest = {
        "files": [{"name": f"{name}.onnx", "sha256": name * 8} for name in names],
        "models": [{"id": name, "file": f"{name}.onnx"} for name in names],
    }
    for name in names:
        (tmp_path / f"{name}.onnx").write_bytes(b"fake ONNX fixture")
    (tmp_path / "labels.txt").write_text("\n".join(f"class-{i}" for i in range(1000)))

    class Session:
        def __init__(self, index):
            self.index = index
            self.runs = 0

        def run(self, output_names, feed):
            assert output_names is None and feed == {"data": "same input"}
            self.runs += 1
            hooks.get("run", lambda session: None)(self)
            calls.append(self.index)
            scores = np.zeros((1, 1000), dtype=np.float32)
            scores[0, self.index] = 1
            hooks.get("output", lambda scores: None)(scores)
            return [scores]

    def factory(path, providers, *, cpu_arena):
        assert providers == ("CPUExecutionProvider",)
        arenas.append(cpu_arena)
        session = Session(names.index(Path(path).stem))
        hooks.get("load", lambda session: None)(session)
        return session

    monkeypatch.setattr(churn.run, "cpu_session", factory)
    monkeypatch.setattr(churn.assets, "verify_assets", lambda directory: manifest)
    monkeypatch.setattr(churn.assets, "preprocess", lambda path: {"data": "same input"})
    monkeypatch.setattr(churn, "current_rss", lambda: (12345, None))
    monkeypatch.setattr(churn.compare, "peak_rss_bytes", lambda: 23456)
    return SimpleNamespace(churn=churn, directory=tmp_path, hooks=hooks, calls=calls, arenas=arenas)


@pytest.fixture(params=[None, "direct"], ids=["default-temms", "direct"])
def backend(request, measured, monkeypatch):
    if request.param == "direct":
        def forbidden(**kwargs):
            pytest.fail("direct backend must not construct TEMMS or OnnxRuntime")

        monkeypatch.setattr(measured.churn, "TEMMS", forbidden)
        monkeypatch.setattr(measured.churn, "OnnxRuntime", forbidden)
    return request.param


def _measure(measured, strategy, monkeypatch, backend, options=()):
    records = measured.directory / "records.jsonl"
    monkeypatch.setattr(measured.churn.sys, "argv", [
        "churn.py", "--strategy", strategy, "--assets", str(measured.directory),
        "--records", str(records),
    ] + (["--backend", backend] if backend else []) + list(options))
    measured.churn.main()
    return [json.loads(line) for line in records.read_text().splitlines()]


@pytest.mark.parametrize("strategy", ["fixed-small", "fixed-large", "swapping"])
def test_churn_counts_sessions_and_releases_all_after_runner_shutdown(
    measured, monkeypatch, strategy, backend,
):
    records = _measure(measured, strategy, monkeypatch, backend)
    steps = [record for record in records if record["event"] == "step"]
    assert records[0]["backend"] == (backend or "temms")
    protocol = records[0]["protocol"]
    assert protocol["steps"] == 31 and protocol["cpu_arena"] == "on"
    assert protocol["replacements"] == (30 if strategy == "swapping" else 0)
    assert all(isinstance(protocol[key], bool) for key in ("memory_pattern", "memory_reuse"))
    assert len(steps) == 31
    assert records[1]["event"] == "prepared" and records[1]["created"] == 0
    for step, record in enumerate(steps):
        index = step % 2 if strategy == "swapping" else int(strategy == "fixed-large")
        created = step + 1 if strategy == "swapping" else 1
        name = ("small", "large")[index]
        assert record["step"] == step and record["created"] == created
        assert record["live_instances"] == [created - 1]
        assert record["active"] == record["serving"] == {"id": name, "digest": "sha256:" + name * 8}
        assert record["prediction"] == {"index": index, "label": f"class-{index}"}
        original = steps[index if strategy == "swapping" else 0]
        assert record["output_sha256"] == original["output_sha256"]
    assert len(measured.calls) == 31 + steps[-1]["created"]  # One warmup for each session.
    assert measured.arenas == [True] * steps[-1]["created"]
    assert [record["event"] for record in records[-2:]] == ["owners_dropped", "post_gc"]
    assert all(record["live_instances"] == [] for record in records[-2:])


def test_churn_rejects_retained_retired_sessions(measured, monkeypatch, backend):
    retained = []
    measured.hooks["load"] = retained.append
    with pytest.raises(RuntimeError, match="surviving session wrappers"):
        _measure(measured, "swapping", monkeypatch, backend)


def test_churn_checks_output_survives_next_reconciliation(measured, monkeypatch, backend):
    previous = []
    measured.hooks["output"] = lambda output: previous.__setitem__(slice(None), [output])

    def mutate_output(session):
        if previous:
            previous[0][0, 0] += 1

    measured.hooks["load"] = mutate_output
    with pytest.raises(RuntimeError, match="retained output changed across reconciliation"):
        _measure(measured, "swapping", monkeypatch, backend)


def test_churn_rejects_nonfinite_predictions(measured, monkeypatch, backend):
    measured.hooks["output"] = lambda scores: scores.__setitem__((0, 0), float("nan"))
    with pytest.raises(ValueError, match="finite classifier scores"):
        _measure(measured, "fixed-small", monkeypatch, backend)


def test_old_session_lives_through_warmup_and_is_released_after_publish(
    measured, monkeypatch, backend,
):
    references, warmups = [], []
    measured.hooks["load"] = lambda session: references.append(weakref.ref(session))

    def check(session):
        if session.runs == 1 and len(references) > 1:
            assert references[-2]() is not None
            assert all(reference() is None for reference in references[:-2])
            warmups.append(session.index)

    measured.hooks["run"] = check
    records = _measure(measured, "swapping", monkeypatch, backend)
    assert warmups == [1, 0] * 15
    steps = [record for record in records if record["event"] == "step"]
    assert [record["live_instances"] for record in steps] == [[i] for i in range(31)]
    assert all(reference() is None for reference in references)


def test_failed_candidate_warmup_preserves_old_until_runner_aborts(measured, monkeypatch, backend):
    references, alive_at_failure = [], []
    measured.hooks["load"] = lambda session: references.append(weakref.ref(session))

    def fail(session):
        if session.index == 1:
            alive_at_failure.append(references[0]() is not None)
            raise ValueError("candidate warmup failed")

    measured.hooks["run"] = fail
    with pytest.raises(ValueError, match="candidate warmup failed"):
        _measure(measured, "swapping", monkeypatch, backend)
    assert alive_at_failure == [True]
    lines = (measured.directory / "records.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    steps = [record for record in records if record["event"] == "step"]
    assert len(steps) == 1 and steps[0]["active"]["id"] == "small"


@pytest.mark.parametrize("count,strategy", [(1, "swapping"), (35, "swapping"), (4, "fixed-large")])
def test_custom_trace_length_and_arena_are_effective(
    measured, monkeypatch, backend, count, strategy,
):
    records = _measure(
        measured, strategy, monkeypatch, backend, ["--steps", str(count), "--cpu-arena", "off"],
    )
    steps = [record for record in records if record["event"] == "step"]
    created = count if strategy == "swapping" else 1
    assert len(steps) == count and steps[-1]["created"] == created
    assert measured.arenas == [False] * created and len(measured.calls) == count + created
    assert records[0]["protocol"]["steps"] == count
    assert records[0]["protocol"]["replacements"] == created - 1
    assert records[0]["protocol"]["cpu_arena"] == "off"
    for i, record in enumerate(steps):
        index = i % 2 if strategy == "swapping" else 1
        assert record["prediction"]["index"] == index
        assert record["live_instances"] == [i if strategy == "swapping" else 0]
        original = steps[index if strategy == "swapping" else 0]
        assert record["output_sha256"] == original["output_sha256"]
    assert all(record["live_instances"] == [] for record in records[-2:])


@pytest.mark.parametrize("steps", ["0", "-1", "not-an-integer"])
def test_invalid_steps_fail_before_asset_work(churn, tmp_path, monkeypatch, steps):
    def forbidden(directory):
        pytest.fail("invalid steps must fail before asset work")

    monkeypatch.setattr(churn.assets, "verify_assets", forbidden)
    records = tmp_path / "records.jsonl"
    monkeypatch.setattr(churn.sys, "argv", [
        "churn.py", "--strategy", "swapping", "--steps", steps, "--records", str(records),
    ])
    with pytest.raises(SystemExit) as error:
        churn.main()
    assert error.value.code == 2 and not records.exists()


@pytest.mark.parametrize("strategy", ["fixed-small", "swapping"])
def test_timing_boundaries_exclude_observation_costs(measured, monkeypatch, backend, strategy):
    churn, clock = measured.churn, SimpleNamespace(ms=0)

    def charge(function, cost):
        def wrapped(*args, **kwargs):
            clock.ms += cost
            return function(*args, **kwargs)
        return wrapped

    measured.hooks["load"] = charge(lambda session: None, 2)
    measured.hooks["run"] = charge(lambda session: None, 3)
    monkeypatch.setattr(churn, "time", SimpleNamespace(
        perf_counter_ns=lambda: clock.ms * 1_000_000,
    ))
    monkeypatch.setattr(churn.run, "prediction", charge(churn.run.prediction, 5))
    monkeypatch.setattr(churn, "fingerprint", charge(churn.fingerprint, 7))
    monkeypatch.setattr(churn, "sample", charge(churn.sample, 200))
    monkeypatch.setattr(churn, "json", SimpleNamespace(dumps=charge(json.dumps, 300)))
    to_thread = churn.asyncio.to_thread

    async def timed_thread(function, *args):
        if function.__name__ == "<lambda>":
            clock.ms += 100  # The worker-draining observer barrier.
        return await to_thread(function, *args)

    monkeypatch.setattr(churn, "asyncio", SimpleNamespace(
        to_thread=timed_thread, Runner=churn.asyncio.Runner,
    ))
    records = _measure(measured, strategy, monkeypatch, backend, ["--steps", "2"])
    fields = ("load_ms", "warmup_ms", "reconcile_ms", "infer_ms", "step_ms")
    timings = [
        tuple(record[field] for field in fields) for record in records if record["event"] == "step"
    ]
    second = (2, 3, 5, 8, 20) if strategy == "swapping" else (0, 0, 0, 8, 15)
    assert timings == [(2, 3, 5, 8, 13), second]
