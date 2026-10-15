import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples.live_swap import assets, workload
from temms import ModelRef
from temms.adapters import InMemoryRuntime

STAGES = (
    "start-small", "more-capacity", "less-capacity", "injected-missing-artifact",
    "retry-restored-artifact", "no-feasible-model", "recover",
)
MAPPING = {
    "basset_hound": ("n02088238", 161), "beagle": ("n02088364", 162),
    "boxer": ("n02108089", 242), "chihuahua": ("n02085620", 151),
    "english_setter": ("n02100735", 212), "great_pyrenees": ("n02111500", 257),
    "japanese_chin": ("n02085782", 152), "keeshond": ("n02112350", 261),
    "leonberger": ("n02111129", 255), "miniature_pinscher": ("n02107312", 237),
}


def test_validation_manifest_is_disjoint_balanced_and_preassigned():
    small, validation = workload._manifest(), workload._manifest("validation")
    records = validation["files"]
    assert validation["revision"] == small["revision"]
    assert validation["dataset"] == small["dataset"] and validation["split"] == "test"
    assert validation["excluded_workload_sha256"] == hashlib.sha256(
        workload.MANIFEST.read_bytes()
    ).hexdigest()
    assert validation["breed_order"] == list(MAPPING) and validation["stage_order"] == list(STAGES)
    assert validation["arrival_period_ms"] == validation["deadline_ms"] == 100
    assert len(records) == 140
    for key in ("name", "source_row", "source_image_id"):
        values = {r[key] for r in records}
        assert len(values) == 140 and values.isdisjoint(r[key] for r in small["files"])
    assert Counter((r["stage"], r["source_label_name"]) for r in records) == Counter({
        (stage, breed): 2 for stage in STAGES for breed in MAPPING
    })
    breeds = list(MAPPING)
    expected_order = [
        (stage, breed, 3 + 2 * phase + offset)
        for phase, stage in enumerate(STAGES) for offset in (0, 1)
        for breed in breeds[phase:] + breeds[:phase]
    ]
    assert [(r["stage"], r["source_label_name"], r["source_breed_occurrence_rank"])
            for r in records] == expected_order
    for record in records:
        assert (record["expected_synset"], record["expected_index"]) == MAPPING[
            record["source_label_name"]
        ]
        assert isinstance(record["bytes"], int) and record["bytes"] > 0
        assert re.fullmatch(r"[0-9a-f]{64}", record["sha256"])


@pytest.fixture
def named_manifests(tmp_path, monkeypatch):
    result = {}
    for name, attribute in (("small", "MANIFEST"), ("validation", "VALIDATION_MANIFEST")):
        content = name.encode()
        manifest = {"selection_rule": f"frozen {name}", "files": [{
            "name": f"{name}.jpg", "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(), "expected_synset": "n0",
        }]}
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(manifest))
        (tmp_path / f"{name}.jpg").write_bytes(content)
        monkeypatch.setattr(workload, attribute, path)
        result[name] = manifest
    for module in (assets, workload):
        monkeypatch.setattr(module, "urlopen", lambda *a, **k: pytest.fail("unexpected network"))
    return result


@pytest.mark.parametrize("name", ["small", "validation"])
def test_named_workload_verifies_and_prepares_only_selected_manifest(
    name, named_manifests, tmp_path,
):
    assert workload._manifest(name) == named_manifests[name]
    assert workload.verify_workload(tmp_path, name) == named_manifests[name]
    assert workload.prepare_workload(tmp_path, name) == named_manifests[name]
    assert workload._manifest() == named_manifests["small"]
    assert workload.verify_workload(tmp_path) == named_manifests["small"]


@pytest.fixture
def comparison(monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from examples.live_swap import compare

    clock = SimpleNamespace(ms=0)

    async def sleep(seconds):
        clock.ms += seconds * 1000

    monkeypatch.setattr(compare, "now_ms", lambda: clock.ms)
    monkeypatch.setattr(compare, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(compare.assets, "preprocess", lambda path: {"data": path.name})
    monkeypatch.setattr(compare.run, "prediction", lambda output, labels: {"index": 0})
    return compare


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
async def test_inputs_keep_global_arrival_sequence(comparison, monkeypatch, tmp_path, staged):
    monkeypatch.setattr(comparison.run, "SCENARIO", (("low", 20, False), ("high", 80, False)))
    refs = [ModelRef("mobilenetv2", "small"), ModelRef("resnet18", "large")]
    runtime = InMemoryRuntime(models={"vision": refs}, handlers={r.digest: lambda value: ()
                                                               for r in refs})
    files = [{"name": f"image-{i}", "expected_synset": "n0", **({"stage": stage} if staged else {})}
             for i, stage in enumerate(("high", "low", "high", "low"))]
    records = []
    await comparison.evaluate(
        runtime, comparison.policies(refs, "adaptive"), "adaptive", files, tmp_path,
        ["n0 label"], tmp_path / "unused", records.append, period_ms=100, deadline_ms=100,
    )
    requests = [r for r in records if r["event"] == "request"]
    expected = ["image-1", "image-3", "image-0", "image-2"] if staged else [
        f"image-{i}" for _ in range(2) for i in range(4)
    ]
    assert [r["input"] for r in requests] == expected
    assert [r["request"] for r in requests] == list(range(len(expected)))
    assert [r["scheduled_ms"] for r in requests] == [100 * i for i in range(len(expected))]
    assert all(r["correct"] and not r["late"] for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("assignments", [[], ["unknown"], ["low"], ["low", None]])
async def test_invalid_assignments_fail_before_runtime_or_output(
    comparison, monkeypatch, tmp_path, assignments,
):
    monkeypatch.setattr(comparison.run, "SCENARIO", (("low", 20, False), ("high", 80, False)))
    runtime = InMemoryRuntime(models={}, handlers={})

    async def unexpected_models(slot):
        pytest.fail("invalid trace must be rejected before touching runtime")

    monkeypatch.setattr(runtime, "models", unexpected_models)
    records = []
    with pytest.raises(ValueError, match="workload must be unstaged or cover all scenario phases"):
        await comparison.evaluate(
            runtime, comparison.policies([], "adaptive"), "adaptive",
            [{"name": str(i), "expected_synset": "n0", "stage": stage}
             for i, stage in enumerate(assignments)],
            tmp_path, ["n0 label"], tmp_path / "unused", records.append,
        )
    assert not records


@pytest.mark.parametrize("name", ["small", "validation"])
def test_workload_cli_selects_explicit_name_and_default(name, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(workload, "prepare_workload", lambda directory, selected: (
        calls.append((directory, selected)) or {"files": []}
    ))
    arguments = ["workload.py", "--assets", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", arguments + ([] if name == "small" else ["--workload", name]))
    workload.main()
    assert calls == [(tmp_path, name)]


@pytest.mark.parametrize("name", ["small", "validation"])
def test_compare_cli_forwards_workload_to_every_child(comparison, monkeypatch, tmp_path, name):
    verified, children = [], []
    monkeypatch.setattr(comparison.assets, "verify_assets", lambda directory: {})
    monkeypatch.setattr(comparison.workload, "verify_workload", lambda directory, selected: (
        verified.append(selected) or {"files": []}
    ))
    monkeypatch.setitem(comparison.PROTOCOL, "repetitions", 1)

    def child(command, check):
        children.append(command)
        path = Path(command[command.index("--records") + 1])
        summary = {"event": "summary", "useful": 0, "offered": 0, "correct": 0,
                   "abstained": 0, "late": 0, "latency_ms_all": {"p95": 0}, "peak_rss_bytes": None}
        path.write_text(json.dumps(summary) + "\n")

    monkeypatch.setattr(comparison.subprocess, "run", child)
    arguments = ["compare.py", "--assets", str(tmp_path), "--output", str(tmp_path / "results")]
    monkeypatch.setattr(sys, "argv", arguments + ([] if name == "small" else ["--workload", name]))
    comparison.main()
    assert verified == [name] and len(children) == len(comparison.STRATEGIES)
    assert all(command[command.index("--workload") + 1] == name for command in children)


@pytest.mark.parametrize("name", ["small", "validation"])
def test_worker_header_records_selected_manifest_and_name(
    comparison, monkeypatch, tmp_path, named_manifests, name,
):
    import asyncio

    monkeypatch.setattr(comparison, "asyncio", asyncio)
    model_manifest = {"files": [{"name": f"{model}.onnx", "sha256": model}
                                for model in ("mobilenetv2", "resnet18")]}
    monkeypatch.setattr(comparison.assets, "verify_assets", lambda directory: model_manifest)
    selected = named_manifests[name]
    monkeypatch.setattr(comparison.workload, "verify_workload", lambda directory, workload_name: (
        named_manifests[workload_name]
    ))
    monkeypatch.setattr(comparison, "environment", lambda: {"fixture": True})
    monkeypatch.setattr(comparison, "peak_rss_bytes", lambda: None)
    (tmp_path / "labels.txt").write_text("n0 label\n")
    (tmp_path / "resnet18.onnx").write_bytes(b"private fixture")

    async def evaluate(*args):
        assert args[3] == selected["files"]
        return 0

    monkeypatch.setattr(comparison, "evaluate", evaluate)
    path = tmp_path / "run.jsonl"
    comparison.worker(SimpleNamespace(
        assets=tmp_path, workload=name, records=path, strategy="fixed-small", repetition=1,
    ))
    header = json.loads(path.read_text().splitlines()[0])
    assert header["workload_name"] == name and header["workload"] == selected
