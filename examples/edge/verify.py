"""Audit two native constrained-worker runs without trusting their PASS summaries."""

import argparse
import hashlib
import json
import math
import uuid
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = {"temms": "0.2.0a1", "onnxruntime": "1.24.1", "numpy": "2.4.2", "Pillow": "12.1.0"}
TOLERANCE = {"rtol": 1e-5, "atol": 1e-6}
CORRUPT_SHA = hashlib.sha256(b"temms deliberately truncated ONNX artifact\n").hexdigest()
PHASES = [
    ("offline-start", "mobilenetv2", False, "published", range(20)),
    ("detail-request", "resnet18", True, "published", range(20)),
    ("cpu-contention", "mobilenetv2", False, "published", range(20)),
    ("corrupt-update", "resnet18", True, "failed", range(20)),
    ("restored-update", "resnet18", True, "published", range(20)),
    *[(f"swap-{i + 1:02}", "resnet18" if i % 2 else "mobilenetv2", bool(i % 2),
       "published", [i]) for i in range(12)],
    ("missing-context", None, None, "unchanged", []),
    ("resume", "mobilenetv2", False, "published", [0]),
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text())


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def vector(value):
    values = np.asarray(value)
    require(values.shape == (1000,) and np.isfinite(values).all(), "invalid score vector")
    return values


def profile(value):
    expected = {"memory_max": 536870912, "swap_max": 0, "cpu_quota": 100000, "cpu_period": 100000}
    require(all(type(value[k]) is int and value[k] == v for k, v in expected.items()),
            "wrong enforced resource limits")
    require(all(finite(value[k]) for k in ("memory_current", "memory_peak"))
            and value["memory_peak"] >= value["memory_current"], "invalid memory accounting")
    cpu, memory = value["cpu_stat"], value["memory_events"]
    require({"usage_usec", "nr_throttled", "throttled_usec"} <= cpu.keys(), "missing CPU counters")
    require(all(type(v) is int and v >= 0 for counters in (cpu, memory) for v in counters.values()),
            "invalid resource counter")
    require(memory.get("oom") == memory.get("oom_kill") == memory.get("oom_group_kill", 0) == 0,
            "worker encountered an OOM event")


def progression(before, after):
    profile(before)
    profile(after)
    require(after["memory_peak"] >= before["memory_peak"], "memory peak counter rolled back")
    require(all(after[group].get(key, -1) >= value
                for group in ("cpu_stat", "memory_events") for key, value in before[group].items()),
            "resource counter rolled back")


def pressure(record):
    progression(record["before"], record["after"])
    pids = record["pids"]
    require(len(pids) == len(set(pids)) == 2 and all(type(pid) is int and pid > 0 for pid in pids),
            "expected two distinct CPU peers")
    before, after = record["worker_cpu_before_seconds"], record["worker_cpu_after_seconds"]
    require(len(before) == len(after) == 2 and all(finite(v) for v in before + after),
            "invalid worker CPU accounting")
    deltas = [b - a for a, b in zip(before, after, strict=True)]
    require(all(delta > 0 for delta in deltas) and record["worker_cpu_seconds"] == deltas,
            "missing or forged worker CPU progress")
    require(all(record["after"]["cpu_stat"][key] > record["before"]["cpu_stat"][key]
                for key in ("usage_usec", "nr_throttled", "throttled_usec")),
            "CPU contention was not witnessed during inference")


def container(record, image, name):
    state, config = record["State"], record["HostConfig"]
    require(state["ExitCode"] == 0 and state["OOMKilled"] is False and state["Running"] is False,
            "container failed, remained running or was OOM-killed")
    expected = {"CpuQuota": 100000, "CpuPeriod": 100000, "Memory": 536870912,
                "MemorySwap": 536870912, "NetworkMode": "none", "CgroupnsMode": "private",
                "ReadonlyRootfs": True, "PidsLimit": 64}
    require(all(config[k] == v for k, v in expected.items()), "container limits/isolation differ")
    require(config["CapDrop"] == ["ALL"] and "no-new-privileges" in config["SecurityOpt"],
            "container privileges differ")
    require(record["Image"] == image["Id"] and record["Platform"] == "linux",
            "worker did not use the inspected Linux image")
    require(record["Config"]["Cmd"] == ["python", "-m", "examples.edge.run", "--assets", "/assets",
                                          "--output", f"/evidence/{name}"], "wrong worker command")
    uid = int(record["Config"]["User"].split(":")[0])
    require(uid > 0, "worker ran as root")
    mounts = {mount["Destination"]: mount for mount in record["Mounts"]}
    require(mounts["/assets"]["Type"] == mounts["/evidence"]["Type"] == "bind"
            and mounts["/assets"]["RW"] is False and mounts["/evidence"]["RW"] is True,
            "original assets or evidence have incorrect mount access")
    return uid, {key: mounts[key]["Source"] for key in ("/assets", "/evidence")}


def references(header, directory):
    manifest = read(ROOT / "examples/live_swap/assets.json")
    workload = read(ROOT / "examples/live_swap/workload.json")
    require(header["manifest"] == manifest and header["workload"] == workload,
            "unpinned inputs/models")
    digests = {item["name"]: item["sha256"] for item in manifest["files"]}
    label_bytes = (directory / "labels.txt").read_bytes()
    require(hashlib.sha256(label_bytes).hexdigest() == digests["labels.txt"],
            "label digest mismatch")
    labels = [line.split(" ", 1)[1] for line in label_bytes.decode().splitlines()]
    require(len(labels) == 1000, "expected 1000 labels")
    models = {name: {"id": name, "digest": "sha256:" + digests[name + ".onnx"]}
              for name in ("mobilenetv2", "resnet18")}
    inputs = [{"name": item["name"], "sha256": item["sha256"]} for item in workload["files"]]
    require(len(inputs) == 20 and len(header["references"]) == 40, "incomplete reference grid")
    lookup = {}
    for item in header["references"]:
        model, image = item["model"], item["image"]
        require(model == models.get(model["id"]) and image in inputs, "unknown reference identity")
        key = (model["id"], image["name"])
        require(key not in lookup, "duplicate reference pair")
        values = vector(item["scores"])
        require(not any(np.allclose(values, other, **TOLERANCE) for other in lookup.values()),
                "indistinguishable reference vectors")
        lookup[key] = values
    require(set(lookup) == {(name, item["name"]) for name in models for item in inputs},
            "missing model/input reference pair")
    return models, workload["files"], labels, lookup


def metadata(header, machine, uid):
    require(header["event"] == "run" and str(uuid.UUID(header["run_id"])) == header["run_id"],
            "invalid run identity")
    require(header["architecture"] == machine and header["os"]["ID"] == "ubuntu"
            and header["os"]["VERSION_ID"] == "24.04", "wrong native worker platform")
    python = header["python"].split(".")
    require(len(python) == 3 and python[:2] == ["3", "12"] and python[2].isdigit(),
            "worker Python is not 3.12")
    require(header["uid"] == uid and header["versions"] == VERSIONS,
            "wrong user/dependency profile")
    require(header["temms_path"] == "/opt/venv/lib/python3.12/site-packages/temms/__init__.py"
            and header["executable"] == "/opt/venv/bin/python",
            "wrong installed-package provenance")
    require(header["provider"] == "CPUExecutionProvider" and header["threads"] == 1
            and header["score_tolerance"] == TOLERANCE, "wrong inference profile")
    require(header["filesystem"] == {"root_readonly": True, "assets_readonly": True},
            "worker filesystem was not read-only")
    require(header["policy"] == {
        "economy": {"input_ready": True, "detail_required": False},
        "detail": {"input_ready": True, "detail_required": True},
        "source": "explicit application requests; no automatic resource monitor",
    }, "application policy description drift")
    network = header["network"]
    require([name for _, name in network["interfaces"]] == ["lo"]
            and network["routes"].startswith("Iface")
            and not any(line.strip() for line in network["routes"].splitlines()[1:])
            and network["connect_errno"] == 101, "network was not isolated (Linux ENETUNREACH)")
    profile(header["profile"])


def event(records, kind, phase):
    record = next(records)
    require(record["event"] == kind and record.get("phase") == phase, "event/phase order drift")
    return record


def frame(record, model, item, labels, lookup, request):
    image = {key: item[key] for key in ("name", "sha256")}
    require(record["request"] == request and record["image"] == image, "request/input order drift")
    require(record["admitted"] == record["serving"] == record["active"] == model,
            "serving/admission identity drift")
    require(record["expected"] == {"index": item["expected_index"],
                                   "label": labels[item["expected_index"]]}, "expected class drift")
    values = vector(record["scores"])
    require(np.allclose(values, lookup[(model["id"], image["name"])], **TOLERANCE),
            "full-score reference mismatch")
    predicted = int(values.argmax())
    require(record["prediction"] == {"index": predicted, "label": labels[predicted]},
            "prediction differs from scores/labels")
    require(record["reference_match"] is True and record["identity_match"] is True
            and record["correct"] is (predicted == item["expected_index"]), "false integrity flags")
    require(finite(record["elapsed_ms"]), "invalid inference timing")


def trace(records, models, inputs, labels, lookup):
    stream, active, measured, pairs, pressured = iter(records[2:-1]), None, [], set(), None
    require(records[1] == {"event": "startup", "active": None,
                           "inference_error": "NoActiveModelError"}, "cold-start contract failed")
    for name, selected, detail, status, indices in PHASES:
        if name in ("corrupt-update", "restored-update"):
            artifact = event(stream, "artifact", name)
            original = models["resnet18"]["digest"].removeprefix("sha256:")
            expected = CORRUPT_SHA if name == "corrupt-update" else original
            require(artifact["actual_sha256"] == expected,
                    "fault/restoration artifact digest mismatch")
            require(name != "corrupt-update" or artifact["original_sha256"] == original,
                    "fault changed the wrong source")
        activation = event(stream, "activation", name)
        context = {} if detail is None else {"input_ready": True, "detail_required": detail}
        require(activation["previous"] == active and activation["selected"] == models.get(selected)
                and activation["context"] == context and activation["activation"] == status,
                "activation policy/state drift")
        active = active if status != "published" else models[selected]
        require(activation["active"] == active and finite(activation["elapsed_ms"]),
                "activation did not publish/preserve the expected model")
        error = activation["error"]
        require((error is not None and error["type"] == "InvalidProtobuf"
                 and bool(error["message"]))
                if status == "failed" else error is None, "wrong activation failure evidence")
        for index in indices:
            record = event(stream, "frame", name)
            frame(record, active, inputs[index], labels, lookup, len(measured) + 1)
            measured.append(record["elapsed_ms"])
            pairs.add((active["id"], inputs[index]["name"]))
        if name == "cpu-contention":
            pressured = event(stream, "pressure", name)
            pressure(pressured)
        if selected is None:
            abstention = event(stream, "abstention", name)
            require(abstention["active"] == active and abstention["reason"] ==
                    "input_ready is missing; application did not call infer", "invalid abstention")
    require(next(stream, None) is None and set(lookup) == pairs,
            "extra events or missing served pairs")
    return measured, pressured


def verify_run(directory, inspection, image, machine):
    uid, mounts = container(inspection, image, directory.name)
    records = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    require(len(records) == 139, "incomplete or extra run events")
    header, summary = records[0], records[-1]
    metadata(header, machine, uid)
    measured, pressured = trace(records, *references(header, directory))
    ordered = sorted(measured)
    timing = {"n": len(ordered), "p50": ordered[(len(ordered) - 1) // 2],
              "p95": ordered[math.ceil(len(ordered) * 0.95) - 1], "max": ordered[-1]}
    require(summary == read(directory / "summary.json") and summary["event"] == "summary"
            and summary["complete"] is True and summary["assets_unchanged"] is True,
            "missing/inconsistent completed summary")
    require(summary["served"] == len(measured) == 113 and summary["abstained"] == 1
            and summary["swaps"] == 12 and summary["inference_ms"] == timing,
            "forged summary totals")
    profiles = [header["profile"], pressured["before"], pressured["after"], summary["profile"]]
    for before, after in zip(profiles, profiles[1:], strict=False):
        progression(before, after)
    return {"run_id": header["run_id"], "served": 113, "abstained": 1, "model_input_pairs": 40,
            "inputs": 20, "activations": 19,
            "correct": sum(record["correct"] for record in records if record["event"] == "frame"),
            "inference_ms": timing, "peak_cgroup_bytes": summary["profile"]["memory_peak"],
            "cpu_pressure": {"worker_cpu_seconds": pressured["worker_cpu_seconds"],
                             "counter_deltas": {key: pressured["after"]["cpu_stat"][key]
                                                - pressured["before"]["cpu_stat"][key]
                                                for key in ("usage_usec", "nr_throttled",
                                                            "throttled_usec")}},
            "mounts": mounts}


def verify(output):
    images = read(output / "image.json")
    require(len(images) == 1 and images[0]["Os"] == "linux", "expected one Linux image")
    image = images[0]
    machine = {"amd64": "x86_64", "arm64": "aarch64"}[image["Architecture"]]
    host = (output / "host.txt").read_text().split()
    engine = (output / "docker-host.txt").read_text().split()
    aliases = {machine, image["Architecture"]}
    require(host[0] == "Linux" and host[-1] in aliases and len(engine) == 3
            and engine[0] == "linux" and engine[1] in aliases and engine[2] == "2",
            "host/image/engine are not the same native cgroup-v2 architecture")
    inspections = [read(output / f"run-{index}-container.json") for index in (1, 2)]
    require(all(len(items) == 1 for items in inspections), "expected one inspection per worker")
    require(inspections[0][0]["Id"] != inspections[1][0]["Id"], "containers were reused")
    runs = [verify_run(output / f"run-{index}", items[0], image, machine)
            for index, items in enumerate(inspections, 1)]
    require(runs[0]["run_id"] != runs[1]["run_id"] and runs[0]["mounts"] == runs[1]["mounts"],
            "restart reused runtime state or different assets/evidence")
    return {"verified": True, "architecture": machine, "runs": runs}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(json.dumps(verify(parser.parse_args().output), indent=2))
