"""Replay simulated battery conditions while real ONNX classifiers swap locally."""

import argparse
import asyncio
import json
import platform
import shutil
import tempfile
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import numpy as np
import onnxruntime as ort

from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, ModelRef, Operator
from temms.adapters import OnnxModel, OnnxRuntime
from temms.errors import ModelUnavailableError

if __package__:
    from .assets import preprocess, verify_assets
else:
    from assets import preprocess, verify_assets

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


def prediction(output, labels):
    scores = np.asarray(output[0])
    if scores.shape != (1, 1000) or not np.isfinite(scores).all():
        raise ValueError("expected finite classifier scores with shape (1, 1000)")
    index = int(scores[0].argmax())
    return {"index": index, "label": labels[index]}


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
    if record["outcome"] != "ok":
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


async def replay(runtime, selector, feed, labels, large_path, emit, frames=6, interval=0.4):
    temms = TEMMS(runtime=runtime, selector=selector)
    request = 0
    scenes = {}
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
                active = (await runtime.state("vision")).active_model
                admitted = any(e.model == active and e.feasible for e in preview.evaluations)
                if admitted:
                    started = time.perf_counter()
                    activation_at_admission = status["activation"]
                    result = await temms.infer("vision", feed)
                    report(
                        {
                            "event": "frame",
                            "request": request,
                            "outcome": "ok",
                            "activation_at_admission": activation_at_admission,
                            "serving": identity(result.model),
                            "active": identity((await runtime.state("vision")).active_model),
                            "prediction": prediction(result.output, labels),
                            "infer_ms": (time.perf_counter() - started) * 1000,
                        }
                    )
                else:
                    outcome = "abstained" if preview.selected_model is None else "waiting"
                    report(
                        {
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
    summary = {
        "event": "summary",
        "complete": all(scenes.values()),
        "scenes": scenes,
        "missing": [stage for stage, observed in scenes.items() if not observed],
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
        if not record["complete"]:
            print(
                "Increase --frames or --interval; defaults are --frames 6 --interval 0.4.",
                flush=True,
            )
    else:
        label = record.get("prediction", {}).get("label", "—")
        print(
            f"  FRAME {record['request']:03} {record['outcome']:9}"
            f" serving={name(record['serving']):11} active={name(record['active']):11}"
            f" | {label}",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path(".demo-assets/live-swap"))
    parser.add_argument("--records", type=Path)
    parser.add_argument("--frames", type=int, default=6)
    parser.add_argument("--interval", type=float, default=0.4)
    args = parser.parse_args()
    if args.frames < 1 or not 0 <= args.interval <= 10:
        parser.error("frames must be positive and interval must be between 0 and 10 seconds")
    manifest = verify_assets(args.assets)
    feed = preprocess(args.assets / "chelsea.png")
    labels = [
        line.split(" ", 1)[1] for line in (args.assets / "labels.txt").read_text().splitlines()
    ]
    if len(labels) != 1000:
        raise ValueError("expected 1000 ImageNet labels")
    digests = {f["name"]: f["sha256"] for f in manifest["files"]}
    refs = [
        ModelRef(name, "sha256:" + digests[name + ".onnx"]) for name in ("mobilenetv2", "resnet18")
    ]
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
                        refs[0], args.assets / "mobilenetv2.onnx", ("CPUExecutionProvider",), feed
                    ),
                    OnnxModel(refs[1], large, ("CPUExecutionProvider",), feed),
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
                    "image": "chelsea.png",
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

        print("TEMMS LIVE SWAP | real classifiers | one photo replayed | simulated battery")
        print(f"CPU, one inference thread per session. Records: {records}", flush=True)
        summary = asyncio.run(
            replay(runtime, selector, feed, labels, large, emit, args.frames, args.interval)
        )
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, ModelUnavailableError) as error:
        raise SystemExit(str(error)) from error
