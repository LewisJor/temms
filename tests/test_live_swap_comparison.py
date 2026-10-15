import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from temms import ModelRef
from temms.adapters import OnnxModel, OnnxRuntime


@pytest.fixture(autouse=True)
def optional_dependencies():
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")


def test_demo_helpers_support_normal_package_import():
    result = subprocess.run(
        [sys.executable, "-c", "from examples.live_swap.run import cpu_session, preprocess; "
         "assert callable(cpu_session) and callable(preprocess)"],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("entrypoint", [
    ["-m", "examples.live_swap.run"], ["examples/live_swap/run.py"],
])
def test_demo_help_does_not_require_or_prepare_assets(entrypoint, tmp_path):
    missing = tmp_path / "unprepared"
    result = subprocess.run(
        [sys.executable, *entrypoint, "--assets", str(missing), "--help"],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--assets" in result.stdout and "--records" in result.stdout
    assert not missing.exists()


@pytest.fixture
def measured(monkeypatch):
    from examples.live_swap import compare

    clock = SimpleNamespace(ms=0)

    async def sleep(seconds):
        clock.ms += seconds * 1000

    def preprocess(path):
        clock.ms += 5
        return {"data": path.name}

    monkeypatch.setattr(compare, "now_ms", lambda: clock.ms)
    monkeypatch.setattr(compare, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(compare.assets, "preprocess", preprocess)
    return compare, clock


def _runtime(directory, clock):
    import numpy as np

    refs = [ModelRef(name, f"sha256:{name}") for name in ("mobilenetv2", "resnet18")]
    paths = [directory / f"{ref.id}.onnx" for ref in refs]
    for path in paths:
        path.write_bytes(b"private fixture")
    loads = []

    class Session:
        def __init__(self, index):
            self.index = index

        def run(self, names, feed):
            clock.ms += 15
            scores = np.zeros((1, 1000), dtype=np.float32)
            scores[0, self.index] = 1
            return [scores]

    def factory(path, providers):
        clock.ms += 25
        index = paths.index(Path(path))
        loads.append(index)
        return Session(index)

    runtime = OnnxRuntime(
        models={"vision": [OnnxModel(ref, path) for ref, path in zip(refs, paths, strict=True)]},
        session_factory=factory,
    )
    return runtime, refs, paths[1], loads


@pytest.mark.asyncio
async def test_strategies_share_offered_trace_and_preserve_feasible_serving(measured, tmp_path):
    compare, clock = measured
    files = [{"name": f"image-{i}", "expected_synset": f"n{i}"} for i in range(2)]
    labels = [f"n{i} class-{i}" for i in range(1000)]
    expected = [
        (stage, battery, item["name"], item["expected_synset"])
        for stage, battery, _ in compare.run.SCENARIO for item in files
    ]
    for strategy, served in (("fixed-small", 12), ("fixed-large", 6), ("adaptive", 12)):
        directory = tmp_path / strategy
        directory.mkdir()
        clock.ms = 0
        runtime, refs, large, loads = _runtime(directory, clock)
        records = []
        await compare.evaluate(
            runtime, compare.policies(refs, strategy), strategy, files, directory,
            labels, large, records.append, period_ms=10, deadline_ms=100,
        )
        requests = [r for r in records if r["event"] == "request"]
        assert [(r["stage"], r["battery"], r["input"], r["expected_synset"])
                for r in requests] == expected
        assert [r["scheduled_ms"] for r in requests] == list(range(0, 140, 10))
        assert [r["request"] for r in requests] == list(range(14))
        summary = compare.summarize(records)
        assert summary["offered"] == 14 and summary["served"] == served
        assert summary["correct"] == served // 2
        assert summary["abstained"] == 14 - served and summary["errors"] == 0
        for request in requests:
            if request["outcome"] == "ok":
                minimum = 10 if request["serving"]["id"] == "mobilenetv2" else 40
                assert request["battery"] >= minimum
            else:
                assert request["serving"] is None and not request["correct"]
        assert summary["activation_failures"] == (strategy == "adaptive")
        assert large.read_bytes() == b"private fixture"
        assert not large.with_suffix(".unavailable").exists()
        assert loads == {"fixed-small": [0], "fixed-large": [1],
                         "adaptive": [0, 1, 0, 1, 0]}[strategy]
        fault = [r for r in requests if r["stage"] == "injected-missing-artifact"]
        expected_model = refs[1] if strategy == "fixed-large" else refs[0]
        assert all(r["outcome"] == "ok" and r["serving"] == compare.run.identity(expected_model)
                   for r in fault)


@pytest.mark.asyncio
async def test_deadline_includes_activation_and_fifo_backlog(measured, monkeypatch, tmp_path):
    compare, clock = measured
    monkeypatch.setattr(compare.run, "SCENARIO", (("start", 20, False),))
    runtime, refs, large, _ = _runtime(tmp_path, clock)
    records = []
    await compare.evaluate(
        runtime, compare.policies(refs, "fixed-small"), "fixed-small",
        [{"name": f"image-{i}", "expected_synset": "n0"} for i in range(3)],
        tmp_path, [f"n{i} class-{i}" for i in range(1000)], large, records.append,
        period_ms=10, deadline_ms=45,
    )
    requests = [r for r in records if r["event"] == "request"]
    assert [r["completed_ms"] for r in requests] == [45, 65, 85]
    assert [r["latency_ms"] for r in requests] == [45, 55, 65]
    assert [r["queue_ms"] for r in requests] == [0, 35, 45]
    assert [r["late"] for r in requests] == [False, True, True]
    assert records[0]["reconcile_ms"] == 25
    assert compare.summarize(records)["useful"] == 1


def test_summary_retains_abstentions_errors_and_late_results_in_denominators(measured):
    compare, _ = measured
    records = [
        {"event": "request", "outcome": outcome, "correct": correct, "late": late,
         "latency_ms": latency, "queue_ms": 0, "completed_ms": 400}
        for outcome, correct, late, latency in (
            ("ok", True, False, 10), ("ok", False, True, 30),
            ("abstained", False, True, 50), ("error", False, False, 70),
        )
    ]
    summary = compare.summarize(records)
    assert tuple(summary[k] for k in ("offered", "served", "correct", "useful")) == (4, 2, 1, 1)
    assert tuple(summary[k] for k in ("abstained", "errors", "late", "dropped")) == (1, 1, 2, 0)
    assert summary["correct_fraction_all"] == 0.25 and summary["accuracy_served"] == 0.5
    assert summary["served_per_second"] == 5
    assert summary["latency_ms_all"] == {"n": 4, "p50": 30, "p95": 70, "max": 70}


@pytest.mark.parametrize("error", [
    PermissionError("CPU metadata denied"), subprocess.CalledProcessError(1, "sysctl"),
])
def test_unavailable_cpu_metadata_does_not_prevent_measurement(measured, monkeypatch, error):
    compare, _ = measured
    monkeypatch.setattr(compare.sys, "platform", "darwin")

    def denied(*args, **kwargs):
        raise error

    monkeypatch.setattr(compare.subprocess, "check_output", denied)
    result = compare.environment()
    assert result["cpu_detail_error"]
    assert "python" in result and "onnxruntime" in result["versions"]


@pytest.mark.asyncio
async def test_inference_errors_count_as_offered_and_late(measured, monkeypatch, tmp_path):
    compare, clock = measured
    monkeypatch.setattr(compare.run, "SCENARIO", (("start", 20, False),))
    runtime, refs, large, _ = _runtime(tmp_path, clock)

    async def broken_infer(slot, feed):
        clock.ms += 15
        raise ValueError("injected inference failure")

    monkeypatch.setattr(runtime, "infer", broken_infer)
    records = []
    await compare.evaluate(
        runtime, compare.policies(refs, "adaptive"), "adaptive",
        [{"name": "image", "expected_synset": "n0"}], tmp_path, ["n0 class"],
        large, records.append, period_ms=10, deadline_ms=10,
    )
    request = records[-1]
    assert request["outcome"] == "error" and "injected inference failure" in request["error"]
    assert request["serving"] is None and not request["correct"]
    assert request["late"] and request["latency_ms"] == 45
    summary = compare.summarize(records)
    assert tuple(summary[k] for k in ("offered", "errors", "late", "served")) == (1, 1, 1, 0)


@pytest.mark.asyncio
async def test_failed_record_write_restores_private_artifact(measured, monkeypatch, tmp_path):
    compare, clock = measured
    monkeypatch.setattr(compare.run, "SCENARIO", (("fault", 80, True),))
    runtime, refs, large, _ = _runtime(tmp_path, clock)
    await runtime.activate("vision", refs[0])

    def broken_emit(record):
        assert not large.exists() and large.with_suffix(".unavailable").exists()
        raise OSError("record output unavailable")

    with pytest.raises(OSError, match="record output unavailable"):
        await compare.evaluate(
            runtime, compare.policies(refs, "adaptive"), "adaptive",
            [{"name": "image", "expected_synset": "n0"}], tmp_path,
            [f"n{i} class-{i}" for i in range(1000)], large, broken_emit, period_ms=0,
        )
    assert large.read_bytes() == b"private fixture"
    assert not large.with_suffix(".unavailable").exists()
