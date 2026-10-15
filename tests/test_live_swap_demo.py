import asyncio
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from temms import BestFeasibleSelector, Constraint, ModelPolicy, ModelRef, Operator
from temms.adapters import OnnxModel, OnnxRuntime


@pytest.fixture
def demo():
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from examples.live_swap import run

    return run


def _scores(demo, model, image):
    scores = demo.np.zeros((1, 1000), dtype=demo.np.float32)
    scores[0, model] = 1
    scores[0, 2] = (1 + model * 2 + image) / 10
    return scores


def _samples(demo, refs):
    return [
        {
            "image": {"name": f"image-{image}.jpg", "sha256": f"image-digest-{image}"},
            "expected": {"index": image, "label": f"class-{image}"},
            "feed": {"data": image},
            "references": {
                (ref.id, ref.digest): _scores(demo, model, image)
                for model, ref in enumerate(refs)
            },
        }
        for image in range(3)
    ]


def _recorder(records, current, activation_done, published, large_path):
    def emit(record):
        records.append(record)
        if record["event"] == "stage":
            current["stage"] = record["stage"]
            activation_done.clear()
        elif record["event"] == "activation":
            if record["stage"] == "more-capacity":
                published.set()
            if record["stage"] == "injected-missing-artifact":
                assert not large_path.exists() and large_path.with_suffix(".unavailable").exists()
            activation_done.set()

    return emit


def _assert_activation_sequence(demo, activations, previous):
    for activation in activations.values():
        assert demo.activation_observed(activation, previous)
        previous = activation["active"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_first", [False, True], ids=["valid-outputs", "sticky-mismatch"])
async def test_replay_swaps_failures_abstention_and_serving_identity(
    demo, monkeypatch, tmp_path, corrupt_first,
):
    refs = [ModelRef(name, f"sha256:{name}") for name in ("mobilenetv2", "resnet18")]
    paths = [tmp_path / f"{ref.id}.onnx" for ref in refs]
    for path in paths:
        path.write_bytes(b"injected classifier fixture")
    labels = [f"class-{index}" for index in range(1000)]
    samples = _samples(demo, refs)
    activation_done, published = asyncio.Event(), threading.Event()
    records, calls, current = [], [], {}

    class Session:
        def __init__(self, index):
            self.index = index

        def run(self, output_names, input_feed):
            calls.append((current["stage"], refs[self.index].id))
            if current["stage"] == "more-capacity" and self.index == 0:
                assert published.wait(5), "replacement did not publish while old inference waited"
            scores = _scores(demo, self.index, input_feed["data"])
            if corrupt_first and len(calls) == 1:
                # Preserve argmax: checking only the displayed class must not pass.
                scores[0, 2] += 0.125
            return [scores]

    runtime = OnnxRuntime(
        models={"vision": [OnnxModel(ref, path) for ref, path in zip(refs, paths, strict=True)]},
        session_factory=lambda path, providers: Session(paths.index(Path(path))),
    )
    selector = BestFeasibleSelector([
        ModelPolicy(refs[0], 10, (Constraint("battery", Operator.GTE, 10),)),
        ModelPolicy(refs[1], 20, (Constraint("battery", Operator.GTE, 40),)),
    ])
    emit = _recorder(records, current, activation_done, published, paths[1])

    async def next_frame(interval):
        await asyncio.wait_for(activation_done.wait(), 2)

    monkeypatch.setattr(demo, "asyncio", SimpleNamespace(
        create_task=asyncio.create_task, gather=asyncio.gather, sleep=next_frame,
    ))
    executor = ThreadPoolExecutor(max_workers=2)
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        await runtime.activate("vision", refs[0])
        summary = await asyncio.wait_for(
            demo.replay(runtime, selector, samples, labels, paths[1], emit, frames=2),
            10,
        )
    finally:
        published.set()
        executor.shutdown(wait=True, cancel_futures=True)

    activations = {r["stage"]: r for r in records if r["event"] == "activation"}
    frames = [r for r in records if r["event"] == "frame"]
    crossing = next(r for r in frames if r["stage"] == "more-capacity")
    assert crossing["serving"] == demo.identity(refs[0])
    assert crossing["active"] == demo.identity(refs[1])
    assert crossing["prediction"] == {"index": 0, "label": "class-0"}
    assert crossing["activation"] == "published"
    assert crossing["activation_at_admission"] == "pending"
    failed = activations["injected-missing-artifact"]
    assert failed["activation"] == "failed" and failed["active"] == demo.identity(refs[0])
    assert "does not exist" in failed["error"]
    for frame in (r for r in frames if r["stage"] == "injected-missing-artifact"):
        assert frame["serving"] == demo.identity(refs[0]) and frame["outcome"] == "ok"
        assert frame["prediction"]["label"] == "class-0"
    retry = activations["retry-restored-artifact"]
    assert retry["activation"] == "published" and retry["active"] == demo.identity(refs[1])
    abstained = [r for r in frames if r["stage"] == "no-feasible-model"]
    assert len(abstained) == 2
    assert all(r["outcome"] == "abstained" and r["serving"] is None for r in abstained)
    assert all(r["active"] == demo.identity(refs[1]) for r in abstained)
    assert all(stage != "no-feasible-model" for stage, _ in calls)
    assert paths[1].exists() and not paths[1].with_suffix(".unavailable").exists()
    assert activations["start-small"]["activation"] == "unchanged"
    assert summary == records[-1]
    assert summary["event"] == "summary" and summary["complete"] is (not corrupt_first)
    assert summary["scenes"] == {
        stage: not (corrupt_first and stage == "start-small") for stage, _, _ in demo.SCENARIO
    }
    assert summary["missing"] == (["start-small"] if corrupt_first else [])
    assert all(frame["image"] == samples[(frame["request"] - 1) % 3]["image"] for frame in frames)
    assert all(
        frame["expected"] == samples[(frame["request"] - 1) % 3]["expected"] for frame in frames
    )
    served = [frame for frame in frames if frame["outcome"] == "ok"]
    assert summary["integrity"] == {
        "checked": len(served), "output_mismatches": int(corrupt_first), "identity_mismatches": 0,
    }
    assert summary["inputs"] == {"total": 3, "checked": 3, "missing": []}
    assert summary["requests"]["correct"] < summary["requests"]["served"] == len(served)
    assert all(frame["reference_match"] for frame in served[1:])
    assert served[0]["reference_match"] is (not corrupt_first)
    assert served[0]["prediction"] == {"index": 0, "label": "class-0"}
    assert all(frame["identity_match"] for frame in served)
    _assert_activation_sequence(demo, activations, demo.identity(refs[0]))
    later_start = next(frame for frame in frames if frame["request"] == 2)
    assert later_start["reference_match"] and later_start["activation_at_admission"] == "unchanged"


@pytest.mark.parametrize("shape,value", [
    ((1000,), 0), ((1, 999), 0), ((2, 1000), 0), ((1, 1000), float("nan")),
    ((1, 1000), float("inf")),
])
def test_prediction_rejects_invalid_classifier_scores(demo, shape, value):
    with pytest.raises(ValueError, match=r"finite classifier scores with shape \(1, 1000\)"):
        demo.prediction((demo.np.full(shape, value),), ["label"] * 1000)


@pytest.fixture
def terminal_demo(demo, tmp_path, monkeypatch):
    names = ("mobilenetv2", "resnet18")
    for name in names:
        (tmp_path / f"{name}.onnx").write_bytes(b"injected classifier fixture")
    (tmp_path / "labels.txt").write_text("\n".join(f"n{i} class-{i}" for i in range(1000)))
    manifest = {"files": [{"name": f"{name}.onnx", "sha256": name} for name in names]}
    workload = {"files": [
        {"name": f"image-{image}.jpg", "sha256": f"image-digest-{image}",
         "expected_index": image}
        for image in range(2)
    ]}

    class Session:
        def __init__(self, path):
            self.index = names.index(Path(path).stem)

        def run(self, names, feed):
            return [_scores(demo, self.index, feed["data"])]

    monkeypatch.setattr(demo, "verify_assets", lambda directory: manifest)
    monkeypatch.setattr(demo.workload, "verify_workload", lambda directory: workload)
    monkeypatch.setattr(demo, "preprocess", lambda path: {
        "data": demo.np.array(int(path.stem.split("-")[1])),
    })
    monkeypatch.setattr(demo, "cpu_session", lambda path, providers: Session(path))
    records = tmp_path / "terminal.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "run.py", "--assets", str(tmp_path), "--records", str(records),
        "--frames", "1", "--interval", "0",
    ])
    return records


def test_terminal_replay_cannot_report_complete_without_scene_evidence(demo, terminal_demo, capsys):
    status = demo.main()
    records = [json.loads(line) for line in terminal_demo.read_text().splitlines()]
    assert status == 1
    summary = records[-1]
    assert summary["event"] == "summary" and summary["complete"] is False
    assert list(summary["scenes"]) == [stage for stage, _, _ in demo.SCENARIO]
    assert {"start-small", "less-capacity", "recover"} <= set(summary["missing"])
    missing = [stage for stage, observed in summary["scenes"].items() if not observed]
    assert summary["missing"] == missing
    assert "Demo complete." not in capsys.readouterr().out


def test_terminal_completion_requires_verified_scenes(demo, terminal_demo, monkeypatch, capsys):
    activation_done = asyncio.Event()
    show = demo.show

    def report(record):
        if record["event"] == "stage":
            activation_done.clear()
        elif record["event"] == "activation":
            activation_done.set()
        show(record)

    async def next_frame(interval):
        await asyncio.wait_for(activation_done.wait(), 2)

    monkeypatch.setattr(demo, "show", report)
    monkeypatch.setattr(demo, "asyncio", SimpleNamespace(
        create_task=asyncio.create_task, gather=asyncio.gather, sleep=next_frame, run=asyncio.run,
    ))
    monkeypatch.setattr(sys, "argv", sys.argv + ["--frames", "2"])
    assert demo.main() == 0
    records = [json.loads(line) for line in terminal_demo.read_text().splitlines()]
    assert records[-1]["event"] == "summary" and records[-1]["complete"] is True
    assert records[-1]["scenes"] == {stage: True for stage, _, _ in demo.SCENARIO}
    assert records[-1]["missing"] == []
    displayed = capsys.readouterr().out
    assert "Demo complete." in displayed and "  MISSING " not in displayed


def _scene_records():
    previous = {"id": "small", "digest": "sha256:small"}
    selected = {"id": "large", "digest": "sha256:large"}
    common = {"stage": "more-capacity", "selected": selected, "active": selected,
              "activation": "published"}
    activation = common | {"event": "activation", "decision_selected": selected}
    frame = common | {"event": "frame", "outcome": "ok", "serving": selected,
                      "activation_at_admission": "published", "reference_match": True,
                      "identity_match": True}
    return previous, activation, frame


def _evidence(demo, previous, *records):
    evidence = {"activation": False, "frame": False, "invalid": False}
    for record in records:
        demo.observe_scene(record, previous, evidence)
    return evidence


def test_scene_requires_publication_before_selected_model_admission(demo):
    previous, activation, frame = _scene_records()
    evidence = _evidence(demo, previous, frame, activation)
    assert evidence == {"activation": True, "frame": False, "invalid": False}
    # Completion can see publication even though this call was admitted before it.
    demo.observe_scene(frame | {"activation_at_admission": "pending"}, previous, evidence)
    assert evidence["frame"] is False
    demo.observe_scene(frame, previous, evidence)
    assert evidence == {"activation": True, "frame": True, "invalid": False}


@pytest.mark.parametrize("event,key,mismatch", [
    ("activation", "decision_selected", "old"), ("activation", "active", "old"),
    ("frame", "serving", "old"), ("frame", "serving", "digest"),
])
def test_scene_rejects_identity_mismatch_despite_latest_active_target(demo, event, key, mismatch):
    previous, activation, frame = _scene_records()
    replacement = previous if mismatch == "old" else frame["serving"] | {"digest": "sha256:other"}
    {"activation": activation, "frame": frame}[event][key] = replacement
    assert _evidence(demo, previous, activation, frame)["frame"] is False


@pytest.mark.parametrize("admission,preserved,observed", [
    ("failed", True, True), ("pending", True, False), ("failed", False, False),
])
def test_fault_requires_failed_admission_and_preserved_previous_identity(
    demo, admission, preserved, observed,
):
    previous, activation, frame = _scene_records()
    activation.update(stage="injected-missing-artifact", activation="failed",
                      active=previous if preserved else activation["selected"])
    frame.update(stage="injected-missing-artifact", activation="failed",
                 activation_at_admission=admission, serving=previous, active=previous)
    assert _evidence(demo, previous, activation, frame)["frame"] is observed


def test_abstention_can_precede_reconciliation_but_any_inference_invalidates_it(demo):
    previous, activation, frame = _scene_records()
    activation.update(stage="no-feasible-model", activation="unchanged", selected=None,
                      decision_selected=None, active=previous)
    frame.update(stage="no-feasible-model", selected=None, outcome="abstained", serving=None)
    evidence = _evidence(demo, previous, frame, activation)
    assert evidence == {"activation": True, "frame": True, "invalid": False}
    assert not demo.scene_observed(frame | {"selected": previous}, previous)
    assert not demo.scene_observed(frame | {"serving": previous}, previous)
    demo.observe_scene(frame | {"outcome": "ok", "serving": previous}, previous, evidence)
    demo.observe_scene(frame, previous, evidence)
    assert evidence["invalid"] is True
