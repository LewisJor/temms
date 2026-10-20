"""Replay simulated battery conditions while real ONNX classifiers swap locally."""

import argparse
import asyncio
import json
import platform
import shutil
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import numpy as np
import onnxruntime as ort

from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, ModelRef, Operator
from temms.adapters import OnnxModel, OnnxRuntime
from temms.errors import ModelUnavailableError

if __package__:
    from . import workload
    from .assets import preprocess, verify_assets
else:
    import workload
    from assets import preprocess, verify_assets

SCORE_TOLERANCE = {"rtol": 1e-5, "atol": 1e-6}

SCENARIO = (
    ("start-small", 20, False),
    ("more-capacity", 80, False),
    ("less-capacity", 20, False),
    ("injected-missing-artifact", 80, True),
    ("retry-restored-artifact", 80, False),
    ("no-feasible-model", 5, False),
    ("recover", 20, False),
)


def identity(model):
    return None if model is None else {"id": model.id, "digest": model.digest}


def cpu_session(path, providers, *, cpu_arena=True):
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = cpu_arena
    session = ort.InferenceSession(path, options, providers=list(providers))
    if session.get_session_options().enable_cpu_mem_arena != cpu_arena:
        raise ValueError("session did not apply the requested CPU arena setting")
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if len(inputs) != 1 or inputs[0].name != "data" or inputs[0].type != "tensor(float)":
        raise ValueError("demo requires one float32 input named data")
    if inputs[0].shape[1:] != [3, 224, 224] or len(outputs) != 1:
        raise ValueError("demo requires NCHW 224x224 input and one output")
    if outputs[0].shape[-1] != 1000:
        raise ValueError("demo requires the shared ImageNet1000 output space")
    return session


def classifier_scores(output):
    if len(output) != 1:
        raise ValueError("expected exactly one classifier output")
    scores = np.asarray(output[0])
    if scores.shape != (1, 1000) or not np.isfinite(scores).all():
        raise ValueError("expected finite classifier scores with shape (1, 1000)")
    return scores


def prediction(output, labels):
    scores = classifier_scores(output)
    index = int(scores[0].argmax())
    return {"index": index, "label": labels[index]}


def prepare_inputs(directory, manifest, refs, labels):
    if not manifest["files"]:
        raise ValueError("replay requires prepared inputs")
    samples = []
    for item in manifest["files"]:
        feed = preprocess(directory / item["name"])
        for value in feed.values():
            value.setflags(write=False)
        samples.append({
            "image": {key: item[key] for key in ("name", "sha256")},
            "expected": {"index": item["expected_index"], "label": labels[item["expected_index"]]},
            "feed": feed, "references": {},
        })
    seen = []
    for ref in refs:
        session = cpu_session(str(directory / f"{ref.id}.onnx"), ("CPUExecutionProvider",))
        for sample in samples:
            scores = classifier_scores(session.run(None, sample["feed"])).copy()
            if any(np.allclose(scores, previous, **SCORE_TOLERANCE) for previous in seen):
                raise ValueError("reference outputs are indistinguishable; cannot verify routing")
            scores.setflags(write=False)
            sample["references"][(ref.id, ref.digest)] = scores
            seen.append(scores)
        del session
    return samples


def check_output(result, sample, admitted, labels):
    scores = classifier_scores(result.output)
    reference = sample["references"].get((result.model.id, result.model.digest))
    predicted = prediction(result.output, labels)
    return {
        "prediction": predicted, "scores": scores[0].tolist(),
        "reference_match": reference is not None and bool(
            np.allclose(scores, reference, **SCORE_TOLERANCE)
        ),
        "identity_match": identity(result.model) == identity(admitted),
        "correct": predicted["index"] == sample["expected"]["index"],
    }


async def transition(temms, runtime, context, fault, report, status):
    started = time.perf_counter()
    try:
        result = await temms.reconcile("vision", context)
    except ModelUnavailableError as error:
        status["activation"] = "failed"
        report(
            {
                "event": "activation",
                "error": str(error),
                "active": identity((await runtime.state("vision")).active_model),
            }
        )
        if not fault:
            raise
    else:
        if fault:
            raise RuntimeError("the injected missing artifact unexpectedly activated")
        status["activation"] = "published" if result.activation else "unchanged"
        report(
            {
                "event": "activation",
                "decision_selected": identity(result.decision.selected_model),
                "active": identity((await runtime.state("vision")).active_model),
                "activation_ms": (time.perf_counter() - started) * 1000,
            }
        )


def activation_observed(record, previous):
    selected, active = record["selected"], record["active"]
    if record["stage"] == "injected-missing-artifact":
        return (
            record["activation"] == "failed"
            and previous is not None
            and selected is not None
            and selected != previous
            and active == previous
        )
    if record["stage"] == "no-feasible-model":
        return (
            record["activation"] == "unchanged"
            and selected is None
            and record["decision_selected"] is None
            and active == previous
        )
    allowed = ("published", "unchanged") if record["stage"] == "start-small" else ("published",)
    return (
        record["activation"] in allowed
        and selected is not None
        and record["decision_selected"] == selected == active
        and (record["activation"] != "unchanged" or active == previous)
    )


def scene_observed(record, previous):
    if record["stage"] == "no-feasible-model":
        return (
            record["outcome"] == "abstained"
            and record["selected"] is None
            and record["serving"] is None
        )
    if (record["outcome"] != "ok" or not record["reference_match"]
            or not record["identity_match"]):
        return False
    admission = record["activation_at_admission"]
    if record["stage"] == "injected-missing-artifact":
        return (
            admission == "failed"
            and previous is not None
            and record["selected"] != previous
            and record["serving"] == record["active"] == previous
        )
    allowed = ("published", "unchanged") if record["stage"] == "start-small" else ("published",)
    return (
        admission in allowed
        and record["selected"] is not None
        and record["serving"] == record["selected"]
    )


def observe_scene(record, previous, evidence):
    if record["event"] == "activation":
        evidence["activation"] = activation_observed(record, previous)
    elif record["event"] == "frame":
        evidence["frame"] |= scene_observed(record, previous) and (
            evidence["activation"] or record["stage"] == "no-feasible-model"
        )
        evidence["invalid"] |= record["stage"] == "no-feasible-model" and record["outcome"] == "ok"
        evidence["invalid"] |= record["outcome"] == "ok" and not (
            record["reference_match"] and record["identity_match"]
        )


async def replay(runtime, selector, samples, labels, large_path, emit, frames=12, interval=0.4):
    temms = TEMMS(runtime=runtime, selector=selector)
    request = 0
    scenes = {}
    counts = Counter(offered=0, served=0, waiting=0, abstained=0, correct=0)
    integrity = Counter(checked=0, output_mismatches=0, identity_mismatches=0)
    served_inputs, served_pairs = set(), set()
    for stage, battery, fault in SCENARIO:
        context = {"battery": battery}
        preview = selector.select(
            slot="vision",
            models=await runtime.models("vision"),
            runtime=await runtime.state("vision"),
            context=context,
        )
        status = {"activation": "pending"}
        previous = identity(preview.current_model)
        evidence = {"activation": False, "frame": False, "invalid": False}
        common = {
            "stage": stage,
            "battery": battery,
            "simulated": True,
            "selected": identity(preview.selected_model),
            "selection_source": "policy_preview",
        }

        def report(record, common=common, status=status, evidence=evidence, previous=previous):
            record = common | status | record
            observe_scene(record, previous, evidence)
            emit(record)

        report(
            {
                "event": "stage",
                "active": identity(preview.current_model),
                "evaluations": [
                    {"model": identity(e.model), "feasible": e.feasible, "failures": e.failures}
                    for e in preview.evaluations
                ],
            }
        )
        hidden = large_path.with_suffix(".unavailable")
        if fault:
            large_path.rename(hidden)  # Only a private temporary copy is used here.
        pending = asyncio.create_task(transition(temms, runtime, context, fault, report, status))
        try:
            for _ in range(frames):
                request += 1
                sample = samples[(request - 1) % len(samples)]
                offered = {"image": sample["image"], "expected": sample["expected"]}
                counts["offered"] += 1
                active = (await runtime.state("vision")).active_model
                admitted = any(e.model == active and e.feasible for e in preview.evaluations)
                if admitted:
                    started = time.perf_counter()
                    activation_at_admission = status["activation"]
                    result = await temms.infer("vision", sample["feed"])
                    infer_ms = (time.perf_counter() - started) * 1000
                    checked = check_output(result, sample, active, labels)
                    counts["served"] += 1
                    counts["correct"] += checked["correct"]
                    integrity["checked"] += 1
                    integrity["output_mismatches"] += not checked["reference_match"]
                    integrity["identity_mismatches"] += not checked["identity_match"]
                    if checked["reference_match"] and checked["identity_match"]:
                        served_inputs.add(sample["image"]["name"])
                        served_pairs.add((result.model.id, result.model.digest,
                                          sample["image"]["name"]))
                    report(
                        {
                            **offered, **checked,
                            "event": "frame",
                            "request": request,
                            "outcome": "ok",
                            "activation_at_admission": activation_at_admission,
                            "serving": identity(result.model),
                            "admitted": identity(active),
                            "active": identity((await runtime.state("vision")).active_model),
                            "infer_ms": infer_ms,
                        }
                    )
                else:
                    outcome = "abstained" if preview.selected_model is None else "waiting"
                    counts[outcome] += 1
                    report(
                        {
                            **offered,
                            "event": "frame",
                            "request": request,
                            "outcome": outcome,
                            "active": identity(active),
                            "serving": None,
                        }
                    )
                await asyncio.sleep(interval)  # Presentation pacing, never simulated load time.
            await pending
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if fault and hidden.exists():
                hidden.rename(large_path)
        scenes[stage] = evidence["activation"] and evidence["frame"] and not evidence["invalid"]
    missing_inputs = [s["image"]["name"] for s in samples
                      if s["image"]["name"] not in served_inputs]
    summary = {
        "event": "summary",
        "complete": all(scenes.values()) and not missing_inputs and integrity["checked"] > 0,
        "scenes": scenes,
        "missing": [stage for stage, observed in scenes.items() if not observed],
        "requests": dict(counts), "integrity": dict(integrity),
        "inputs": {"total": len(samples), "checked": len(served_inputs), "missing": missing_inputs},
        "model_input_pairs": {"total": sum(len(s["references"]) for s in samples),
                              "checked": len(served_pairs)},
    }
    emit(summary)
    return summary


def show(record):
    def name(value):
        return "none" if value is None else value["id"]

    if record["event"] == "stage":
        print(
            f"\n{record['stage'].upper()} | battery={record['battery']}% (simulated)"
            f" | selected={name(record['selected'])}",
            flush=True,
        )
    elif record["event"] == "activation":
        print(
            f"  SWITCH {record['activation']:9} active={name(record['active'])}"
            f" {record.get('error', '')}",
            flush=True,
        )
    elif record["event"] == "summary":
        verdict = "complete" if record["complete"] else "incomplete"
        print(f"\nDemo {verdict}. Scene evidence:", flush=True)
        for stage, observed in record["scenes"].items():
            print(f"  {'PASS' if observed else 'MISSING':7} {stage}", flush=True)
        requests, integrity = record["requests"], record["integrity"]
        print(f"Requests: {requests['offered']} offered, {requests['served']} served, "
              f"{requests['waiting']} waiting, {requests['abstained']} abstained")
        print(f"Integrity: {integrity['checked']} checked, "
              f"{integrity['output_mismatches']} output mismatches, "
              f"{integrity['identity_mismatches']} attribution mismatches")
        print(f"Coverage: {record['inputs']['checked']}/{record['inputs']['total']} inputs; "
              f"{record['model_input_pairs']['checked']}/{record['model_input_pairs']['total']} "
              "model/input pairs checked")
        print(f"Expected labels: {requests['correct']}/{requests['served']} correct "
              "(descriptive, not a pass criterion)", flush=True)
        if not record["complete"]:
            print(
                "Inspect mismatches and missing evidence. More frames/time can address coverage "
                "only; defaults are --frames 12 --interval 0.4.",
                flush=True,
            )
    else:
        print(
            f"  FRAME {record['request']:03} {record['outcome']:9}"
            f" serving={name(record['serving']):11} active={name(record['active']):11}"
            f" input={record['image']['name']}",
            flush=True,
        )
        if record["outcome"] == "ok":
            print(f"    predicted={record['prediction']['label']}"
                  f" | expected={record['expected']['label']}"
                  f" | class={'correct' if record['correct'] else 'wrong'}"
                  f" reference={'PASS' if record['reference_match'] else 'FAIL'}"
                  f" identity={'PASS' if record['identity_match'] else 'FAIL'}"
                  f" inference={record['infer_ms']:.2f}ms", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path(".demo-assets/live-swap"))
    parser.add_argument("--records", type=Path)
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--interval", type=float, default=0.4)
    args = parser.parse_args()
    if args.frames < 1 or not 0 <= args.interval <= 10:
        parser.error("frames must be positive and interval must be between 0 and 10 seconds")
    manifest = verify_assets(args.assets)
    data = workload.verify_workload(args.assets)
    labels = [
        line.split(" ", 1)[1] for line in (args.assets / "labels.txt").read_text().splitlines()
    ]
    if len(labels) != 1000:
        raise ValueError("expected 1000 ImageNet labels")
    digests = {f["name"]: f["sha256"] for f in manifest["files"]}
    refs = [
        ModelRef(name, "sha256:" + digests[name + ".onnx"]) for name in ("mobilenetv2", "resnet18")
    ]
    print(f"Preparing direct ONNX references for {len(data['files'])} pinned inputs...", flush=True)
    samples = prepare_inputs(args.assets, data, refs, labels)
    warmup = samples[0]["feed"]
    selector = BestFeasibleSelector(
        [
            ModelPolicy(refs[0], 10, (Constraint("battery", Operator.GTE, 10),)),
            ModelPolicy(refs[1], 20, (Constraint("battery", Operator.GTE, 40),)),
        ]
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    records = args.records or args.assets / f"run-{stamp}.jsonl"
    with tempfile.TemporaryDirectory(prefix="temms-demo-") as work, records.open("x") as log:
        large = Path(work) / "resnet18.onnx"
        shutil.copyfile(args.assets / "resnet18.onnx", large)
        runtime = OnnxRuntime(
            models={
                "vision": [
                    OnnxModel(
                        refs[0], args.assets / "mobilenetv2.onnx", ("CPUExecutionProvider",), warmup
                    ),
                    OnnxModel(refs[1], large, ("CPUExecutionProvider",), warmup),
                ]
            },
            session_factory=cpu_session,
        )
        log.write(
            json.dumps(
                {
                    "event": "run",
                    "utc": stamp,
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "manifest": manifest,
                    "versions": {p: version(p) for p in ("onnxruntime", "numpy", "Pillow")},
                    "provider": "CPUExecutionProvider",
                    "threads": 1,
                    "workload": data,
                    "score_tolerance": SCORE_TOLERANCE,
                    "input_order": (
                        "global request index modulo manifest length; advance on every attempt"
                    ),
                    "references": [
                        {"model": {"id": model, "digest": digest},
                         "image": sample["image"], "scores": scores[0].tolist()}
                        for sample in samples
                        for (model, digest), scores in sample["references"].items()
                    ],
                    "frames_per_stage": args.frames,
                    "interval_seconds": args.interval,
                }
            )
            + "\n"
        )

        def emit(record):
            log.write(json.dumps(record, allow_nan=False) + "\n")
            log.flush()
            show(record)

        print("TEMMS LIVE SWAP | varied pinned inputs | direct-output checks | simulated battery")
        print(f"CPU, one inference thread per session. Records: {records}", flush=True)
        summary = asyncio.run(
            replay(runtime, selector, samples, labels, large, emit, args.frames, args.interval)
        )
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, ModelUnavailableError) as error:
        raise SystemExit(str(error)) from error
