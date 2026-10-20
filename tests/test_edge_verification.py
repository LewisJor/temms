"""Independent evidence gates use generated scores, without models or Docker."""

import copy
import hashlib
import json
import uuid

import pytest


def write_json(path, value):
    path.write_text(json.dumps(value))


def profile(level):
    return {"memory_max": 536870912, "swap_max": 0, "cpu_quota": 100000,
            "cpu_period": 100000, "memory_current": level * 1024,
            "memory_peak": level * 2048,
            "cpu_stat": {"usage_usec": level * 100, "nr_throttled": level,
                         "throttled_usec": level * 10},
            "memory_events": {"oom": 0, "oom_kill": 0, "oom_group_kill": 0}}


def header(manifest, workload, number):
    models = [{"id": item["name"].removesuffix(".onnx"), "digest": "sha256:" + item["sha256"]}
              for item in manifest["files"] if item["name"].endswith(".onnx")]
    references = []
    for model_index, model in enumerate(models):
        for index, item in enumerate(workload["files"]):
            scores = [0.0] * 1000
            scores[model_index * 20 + index] = 1.0
            references.append({"model": model, "image": {k: item[k] for k in ("name", "sha256")},
                               "scores": scores})
    return {"event": "run", "run_id": str(uuid.UUID(int=number)), "uid": 1001,
            "architecture": "x86_64", "os": {"ID": "ubuntu", "VERSION_ID": "24.04"},
            "python": "3.12.3", "executable": "/opt/venv/bin/python",
            "temms_path": "/opt/venv/lib/python3.12/site-packages/temms/__init__.py",
            "versions": {"temms": "0.2.0a1", "onnxruntime": "1.24.1", "numpy": "2.4.2",
                         "Pillow": "12.1.0"},
            "provider": "CPUExecutionProvider", "threads": 1,
            "score_tolerance": {"rtol": 1e-5, "atol": 1e-6},
            "manifest": manifest, "workload": workload, "references": references,
            "profile": profile(1),
            "network": {"interfaces": [[1, "lo"]], "routes": "Iface\tDestination\n",
                        "connect_errno": 101},
            "filesystem": {"root_readonly": True, "assets_readonly": True},
            "policy": {"economy": {"input_ready": True, "detail_required": False},
                       "detail": {"input_ready": True, "detail_required": True},
                       "source": "explicit application requests; no automatic resource monitor"}}


def frames(run, model, phase, indices, start):
    results = []
    for offset, index in enumerate(indices, start + 1):
        item = run["workload"]["files"][index]
        reference = next(ref for ref in run["references"]
                         if ref["model"] == model and ref["image"]["name"] == item["name"])
        predicted = reference["scores"].index(1.0)
        results.append({"event": "frame", "phase": phase, "request": offset,
                        "image": reference["image"], "admitted": model, "serving": model,
                        "active": model, "scores": reference["scores"], "elapsed_ms": offset / 10,
                        "expected": {"index": item["expected_index"],
                                     "label": f"label-{item['expected_index']}"},
                        "prediction": {"index": predicted, "label": f"label-{predicted}"},
                        "correct": predicted == item["expected_index"],
                        "reference_match": True, "identity_match": True})
    return results


def phase_records(run, phase, detail, indices, previous, start, failed=False):
    selected = run["references"][20 if detail else 0]["model"]
    active = previous if failed else selected
    activation = {"event": "activation", "phase": phase, "previous": previous,
                  "selected": selected, "active": active,
                  "context": {"input_ready": True, "detail_required": detail},
                  "activation": "failed" if failed else "published", "elapsed_ms": 1.0,
                  "error": {"type": "InvalidProtobuf", "message": "invalid graph"} if failed
                  else None}
    return [activation, *frames(run, active, phase, indices, start)], active


def records(run):
    result = [run, {"event": "startup", "active": None, "inference_error": "NoActiveModelError"}]
    previous, count = None, 0
    phases = [("offline-start", False), ("detail-request", True), ("cpu-contention", False),
              ("corrupt-update", True), ("restored-update", True)]
    for phase, detail in phases:
        if phase in ("corrupt-update", "restored-update"):
            original = run["references"][20]["model"]["digest"].removeprefix("sha256:")
            corrupt = hashlib.sha256(b"temms deliberately truncated ONNX artifact\n").hexdigest()
            result.append({"event": "artifact", "phase": phase, "original_sha256": original,
                           "actual_sha256": corrupt if phase == "corrupt-update" else original})
        added, previous = phase_records(run, phase, detail, range(20), previous, count,
                                        failed=phase == "corrupt-update")
        result.extend(added)
        count += 20
        if phase == "cpu-contention":
            result.append({"event": "pressure", "phase": phase, "before": profile(2),
                           "after": profile(3), "pids": [101, 102],
                           "worker_cpu_before_seconds": [1.0, 2.0],
                           "worker_cpu_after_seconds": [1.5, 2.75],
                           "worker_cpu_seconds": [0.5, 0.75]})
    for index in range(12):
        added, previous = phase_records(run, f"swap-{index + 1:02}", bool(index % 2), [index],
                                        previous, count)
        result.extend(added)
        count += 1
    result.extend([
        {"event": "activation", "phase": "missing-context", "context": {}, "selected": None,
         "previous": previous, "active": previous, "activation": "unchanged", "error": None,
         "elapsed_ms": 1.0},
        {"event": "abstention", "phase": "missing-context", "active": previous,
         "reason": "input_ready is missing; application did not call infer"},
    ])
    added, _ = phase_records(run, "resume", False, [0], previous, count)
    result.extend(added)
    result.append({"event": "summary", "complete": True, "served": 113, "abstained": 1,
                   "swaps": 12, "assets_unchanged": True, "profile": profile(4),
                   "inference_ms": {"n": 113, "p50": 5.7, "p95": 10.8, "max": 11.3}})
    return result


def inspection(number):
    return {"Id": str(number) * 64, "Image": "sha256:" + "0" * 64, "Platform": "linux",
            "State": {"ExitCode": 0, "Running": False, "OOMKilled": False},
            "Config": {"User": "1001:1001", "Cmd": ["python", "-m", "examples.edge.run",
                       "--assets", "/assets", "--output", f"/evidence/run-{number}"]},
            "HostConfig": {"CpuQuota": 100000, "CpuPeriod": 100000, "Memory": 536870912,
                           "MemorySwap": 536870912, "NetworkMode": "none",
                           "CgroupnsMode": "private", "ReadonlyRootfs": True, "PidsLimit": 64,
                           "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"]},
            "Mounts": [{"Destination": "/assets", "Type": "bind", "RW": False,
                        "Source": "/host/assets"},
                       {"Destination": "/evidence", "Type": "bind", "RW": True,
                        "Source": "/host/evidence"}]}


def save_records(directory, values):
    (directory / "events.jsonl").write_text("\n".join(json.dumps(v) for v in values) + "\n")
    write_json(directory / "summary.json", values[-1])


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from examples.edge import verify

    source = tmp_path / "source/examples/live_swap"
    source.mkdir(parents=True)
    manifest = verify.read(verify.ROOT / "examples/live_swap/assets.json")
    workload = verify.read(verify.ROOT / "examples/live_swap/workload.json")
    labels = "".join(f"n{i:08} label-{i}\n" for i in range(1000)).encode()
    next(item for item in manifest["files"] if item["name"] == "labels.txt")["sha256"] = (
        hashlib.sha256(labels).hexdigest())
    write_json(source / "assets.json", manifest)
    write_json(source / "workload.json", workload)
    monkeypatch.setattr(verify, "ROOT", tmp_path / "source")
    output = tmp_path / "evidence"
    output.mkdir()
    write_json(output / "image.json", [{"Id": "sha256:" + "0" * 64, "Os": "linux",
                                        "Architecture": "amd64"}])
    (output / "host.txt").write_text("Linux 6.8.0 x86_64\n")
    (output / "docker-host.txt").write_text("linux x86_64 2\n")
    for number in (1, 2):
        directory = output / f"run-{number}"
        directory.mkdir()
        (directory / "labels.txt").write_bytes(labels)
        save_records(directory, records(header(copy.deepcopy(manifest), workload, number)))
        write_json(output / f"run-{number}-container.json", [inspection(number)])
    return verify, output


def test_two_fresh_workers_require_all_pairs_and_derive_results(evidence):
    verify, output = evidence
    result = verify.verify(output)
    assert result["verified"] is True
    assert result["architecture"] == "x86_64"
    assert len(result["runs"]) == 2
    for run in result["runs"]:
        assert (run["served"], run["abstained"], run["inputs"], run["model_input_pairs"]) == (
            113, 1, 20, 40)
        assert run["activations"] == 19 and run["correct"] == 0
        assert run["cpu_pressure"] == {"worker_cpu_seconds": [0.5, 0.75],
                                       "counter_deltas": {"usage_usec": 100, "nr_throttled": 1,
                                                          "throttled_usec": 10}}


@pytest.mark.parametrize(("kind", "phase", "path", "value", "message"), [
    ("frame", "offline-start", ("scores", 999), -0.5, "full-score"),
    ("frame", "offline-start", ("admitted", "digest"), "sha256:wrong", "identity drift"),
    ("frame", "offline-start", ("image", "sha256"), "wrong", "input order"),
    ("frame", "offline-start", ("scores", 2), float("nan"), "invalid score"),
    ("activation", "corrupt-update", ("error", "type"), "FileNotFoundError", "failure evidence"),
    ("artifact", "restored-update", ("actual_sha256",), "wrong", "artifact digest"),
    ("abstention", "missing-context", ("event",), "frame", "event/phase order"),
    ("summary", None, ("served",), 112, "summary totals"),
    ("summary", None, ("inference_ms", "p95"), 0.1, "summary totals"),
    ("pressure", "cpu-contention", ("worker_cpu_seconds", 0), 999, "worker CPU progress"),
    ("pressure", "cpu-contention", ("after", "cpu_stat", "nr_throttled"), 2, "contention"),
    ("run", None, ("network", "connect_errno"), 111, "network was not isolated"),
    ("run", None, ("profile", "swap_max"), 536870912, "resource limits"),
])
def test_rejects_tampered_raw_evidence_despite_completed_summary(
    evidence, kind, phase, path, value, message,
):
    verify, output = evidence
    directory = output / "run-1"
    values = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    target = next(item for item in values if item["event"] == kind and item.get("phase") == phase)
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    save_records(directory, values)
    with pytest.raises(ValueError, match=message):
        verify.verify(output)


@pytest.mark.parametrize(("path", "value", "message"), [
    (("Id",), "2" * 64, "containers were reused"),
    (("HostConfig", "CpuQuota"), 0, "container limits"),
    (("Mounts", 0, "RW"), True, "mount access"),
])
def test_rejects_container_inspection_drift(evidence, path, value, message):
    verify, output = evidence
    filename = output / "run-1-container.json"
    values = verify.read(filename)
    target = values[0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    write_json(filename, values)
    with pytest.raises(ValueError, match=message):
        verify.verify(output)
