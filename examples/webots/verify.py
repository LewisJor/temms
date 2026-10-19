"""Independently audit the camera images, routing and full scores in a Webots run."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
STAGES = (
    ("start-small", 20, "mobilenetv2", "published"),
    ("more-capacity", 80, "resnet18", "published"),
    ("less-capacity", 20, "mobilenetv2", "published"),
    ("injected-missing-artifact", 80, "resnet18", "failed"),
    ("retry-restored-artifact", 80, "resnet18", "published"),
    ("no-feasible-model", 5, None, "unchanged"),
    ("recover", 20, "mobilenetv2", "published"),
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def scores(value):
    array = np.asarray(value)
    require(array.shape == (1000,) and np.isfinite(array).all(), "invalid score vector")
    return array


def verify_inputs(output, header):
    manifest = json.loads((ROOT / "examples/live_swap/workload.json").read_text())
    require(header["source_workload"] == manifest, "unexpected source workload")
    inputs = header["inputs"]
    require(len(inputs) == 20, "expected twenty camera captures")
    for index, (item, source) in enumerate(zip(inputs, manifest["files"], strict=True), 1):
        require(item["source"] == source, "camera/source mapping changed")
        require(item["expected"]["index"] == source["expected_index"], "wrong expected class")
        require(item["image"]["name"] == f"inputs/camera-{index:03}.png", "wrong capture order")
        path = output / item["image"]["name"]
        require(hashlib.sha256(path.read_bytes()).hexdigest() == item["image"]["sha256"],
                "camera PNG digest mismatch")
        with Image.open(path) as image:
            require(image.mode == "RGB" and image.size == (256, 256), "invalid camera PNG")
            require(hashlib.sha256(image.tobytes()).hexdigest() == item["camera_rgb_sha256"],
                    "camera pixel digest mismatch")
    require(len({item["camera_rgb_sha256"] for item in inputs}) == 20, "duplicate camera images")
    return inputs


def verify_references(header, inputs):
    manifest = json.loads((ROOT / "examples/live_swap/assets.json").read_text())
    require(header["manifest"] == manifest, "unexpected model manifest")
    models = {item["name"].removesuffix(".onnx"):
              {"id": item["name"].removesuffix(".onnx"), "digest": "sha256:" + item["sha256"]}
              for item in manifest["files"] if item["name"].endswith(".onnx")}
    references = header["references"]
    require(len(references) == 40, "expected forty direct camera/model references")
    lookup = {}
    for record in references:
        model, image = record["model"], record["image"]
        require(model == models.get(model["id"]), "unknown reference model identity")
        require(image in [item["image"] for item in inputs], "unknown reference input")
        key = (model["id"], image["name"])
        require(key not in lookup, "duplicate reference pair")
        vector = scores(record["scores"])
        require(not any(np.allclose(vector, other, rtol=1e-5, atol=1e-6)
                        for other in lookup.values()), "indistinguishable reference vectors")
        lookup[key] = vector
    require(len(lookup) == 40, "missing reference pairs")
    return models, lookup


def eligible(model, battery):
    return model is not None and battery >= {"mobilenetv2": 10, "resnet18": 40}[model["id"]]


def verify_admission(frame, previous, active, activation, observed):
    require(frame["active"] == (active if observed else previous), "active state drift")
    require(frame["activation"] == (activation if observed else "pending"),
            "activation status differs from event order")
    if frame["selected"] is None:
        require(frame["outcome"] == "abstained" and frame["serving"] is None,
                "inference admitted without a feasible model")
        return False
    if frame["outcome"] == "waiting":
        require(frame["serving"] is None and not eligible(frame["active"], frame["battery"]),
                "waiting despite a feasible active model")
        return False
    require(frame["outcome"] == "ok", "abstention despite a selected model")
    admission = frame["activation_at_admission"]
    require(admission in ("pending", activation) and (admission == "pending" or observed),
            "admission preceded its activation evidence")
    require(frame["admitted"] == (previous if admission == "pending" else active),
            "admitted identity differs from activation state")
    require(eligible(frame["admitted"], frame["battery"]), "infeasible model admitted")
    return observed and admission == activation and frame["serving"] == active


def verify_scene(records, previous, model, activation):
    require(records[0]["event"] == "stage" and records[0]["active"] == previous,
            "scene did not begin with the previous active model")
    events = [record for record in records if record["event"] == "activation"]
    require(len(events) == 1 and events[0]["activation"] == activation,
            "missing activation evidence")
    active = previous if activation in ("failed", "unchanged") else model
    require(events[0]["active"] == active, "wrong published model")
    if activation == "failed":
        require(previous is not None and model != previous and bool(events[0].get("error")),
                "missing failed replacement evidence")
    else:
        require(events[0]["decision_selected"] == model, "activation decision drift")
    frames = [record for record in records if record["event"] == "frame"]
    require(len(frames) == 12, "expected twelve attempts in each scene")
    observed, witness = False, False
    for record in records[1:]:
        if record["event"] == "activation":
            observed = True
        elif record["event"] == "frame":
            witness |= verify_admission(record, previous, active, activation, observed)
    require(model is None or witness, "scene has no post-activation serving witness")
    return active


def verify_frames(frames, inputs, models, references):
    counts = Counter(offered=0, served=0, waiting=0, abstained=0, correct=0)
    seen, pairs = set(), set()
    advancing = 0
    for request, frame in enumerate(frames, 1):
        item = inputs[(request - 1) % len(inputs)]
        require(frame["request"] == request and frame["image"] == item["image"],
                "input order drift")
        require(frame["capture"]["request"] == request
                and frame["capture"]["rgb_sha256"] == item["camera_rgb_sha256"],
                "stale camera frame")
        require(frame["expected"] == item["expected"], "expected label drift")
        counts["offered"] += 1
        if frame["outcome"] != "ok":
            require(frame["outcome"] in ("waiting", "abstained") and frame["serving"] is None,
                    "invalid unserved outcome")
            counts[frame["outcome"]] += 1
            continue
        model = frame["serving"]
        require(model == models.get(model["id"]) == frame["admitted"], "model attribution drift")
        key = (model["id"], frame["image"]["name"])
        vector = scores(frame["scores"])
        require(np.allclose(vector, references[key], rtol=1e-5, atol=1e-6), "full-score mismatch")
        require(frame["prediction"]["index"] == int(vector.argmax()),
                "prediction differs from scores")
        correct = int(vector.argmax()) == item["expected"]["index"]
        require(frame["reference_match"] is True and frame["identity_match"] is True
                and frame["correct"] is correct,
                "reported integrity differs from raw evidence")
        counts["served"] += 1
        counts["correct"] += correct
        seen.add(frame["image"]["name"])
        pairs.add(key)
        advancing += frame["simulation_seconds"] > frame["capture"]["simulation_seconds"]
    require(len(seen) == 20 and advancing > 0,
            "missing input coverage or concurrent simulation steps")
    return dict(counts), len(pairs), advancing


def verify(output):
    records = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    require(len(records) == 100, "expected one header, seven complete scenes and one summary")
    header, summary = records[0], records[-1]
    require(header["event"] == "run" and header["fixture"] == "webots-camera", "wrong run header")
    require(summary["event"] == "summary" and summary["complete"] is True, "incomplete run")
    require(header["score_tolerance"] == {"rtol": 1e-5, "atol": 1e-6}, "changed score tolerance")
    inputs = verify_inputs(output, header)
    models, references = verify_references(header, inputs)
    active = None
    for index, (stage, battery, model_id, activation) in enumerate(STAGES):
        scene = records[1 + index * 14:1 + (index + 1) * 14]
        model = models.get(model_id)
        require(all(r["event"] in ("stage", "activation", "frame") and r["stage"] == stage
                    for r in scene), "unknown or reordered scene event")
        require(all(r["battery"] == battery and r["simulated"] is True
                    and r["selected"] == model for r in scene), "changed policy scenario")
        active = verify_scene(scene, active, model, activation)
    frames = [r for r in records if r["event"] == "frame"]
    counts, pairs, advancing = verify_frames(frames, inputs, models, references)
    require(summary["requests"] == counts and len(frames) == 84, "summary counts differ")
    require(summary["integrity"] == {"checked": counts["served"], "output_mismatches": 0,
                                     "identity_mismatches": 0}, "summary integrity differs")
    require(summary["scenes"] == dict.fromkeys((s[0] for s in STAGES), True),
            "scene verdicts differ")
    require(summary["missing"] == [] and summary["inputs"] == {
        "total": 20, "checked": 20, "missing": [],
    }, "summary input coverage differs")
    require(summary["model_input_pairs"] == {"total": 40, "checked": pairs},
            "summary model/input coverage differs")
    return {"verified": True, "scenes": 7, "camera_inputs": 20, "references": 40,
            "model_input_pairs": pairs, "requests": counts,
            "served_requests_with_simulation_progress": advancing}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(json.dumps(verify(parser.parse_args().output), indent=2))
