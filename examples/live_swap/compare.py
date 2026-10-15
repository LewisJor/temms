"""Compare fixed and adaptive policies on the predeclared local CPU workload."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, ModelRef, Operator
from temms.adapters import OnnxModel, OnnxRuntime
from temms.errors import ModelUnavailableError

if __package__:
    from . import assets, run, workload
else:
    import assets
    import run
    import workload

STRATEGIES = ("fixed-small", "fixed-large", "adaptive", "adaptive-background")
PROTOCOL = {
    "strategies": STRATEGIES,
    "period_ms": 100,
    "deadline_ms": 100,
    "repetitions": 3,
    "queue": "serial FIFO; no drops; arrival-time context",
    "replacement": {
        "adaptive": "block request worker during reconciliation",
        "adaptive-background": (
            "serve feasible active model while replacement loads; otherwise wait; "
            "drain replacement before next phase"
        ),
    },
    "phase_order": run.SCENARIO,
    "input_order": "manifest order within each assigned phase; unstaged inputs repeat every phase",
    "policy": {
        "mobilenetv2": {"minimum_battery": 10, "priority": 10},
        "resnet18": {"minimum_battery": 40, "priority": 20},
    },
    "warmup": "one Chelsea inference for every constructed session",
    "provider": "CPUExecutionProvider",
    "intra_threads": 1,
    "inter_threads": 1,
    "executor_workers": 2,
    "deadline_scope": "scheduled arrival through output interpretation; excludes record writes",
    "memory_scope": "whole fresh process high-water RSS, including native allocations and overlap",
    "acceptance": "complete identical offered trace; no infeasible serving or unexpected errors",
}


def now_ms():
    return time.perf_counter_ns() / 1_000_000


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    return {
        "n": len(ordered),
        "p50": ordered[(len(ordered) - 1) // 2],
        "p95": ordered[(95 * len(ordered) + 99) // 100 - 1],
        "max": ordered[-1],
    }


def summarize(records):
    frames = [r for r in records if r["event"] == "request"]
    served = [r for r in frames if r["outcome"] == "ok"]
    correct = sum(r["correct"] for r in frames)
    elapsed = frames[-1]["completed_ms"] if frames else 0
    return {
        "offered": len(frames),
        "served": len(served),
        "correct": correct,
        "useful": sum(r["correct"] and not r["late"] for r in frames),
        "abstained": sum(r["outcome"] == "abstained" for r in frames),
        "errors": sum(r["outcome"] == "error" for r in frames),
        "dropped": 0,
        "late": sum(r["late"] for r in frames),
        "correct_fraction_all": correct / len(frames) if frames else None,
        "accuracy_served": correct / len(served) if served else None,
        "served_per_second": len(served) * 1000 / elapsed if elapsed else None,
        "latency_ms_all": distribution([r["latency_ms"] for r in frames]),
        "latency_ms_served": distribution([r["latency_ms"] for r in served]),
        "queue_ms_all": distribution([r["queue_ms"] for r in frames]),
        "reconcile_ms": distribution(
            [r["reconcile_ms"] for r in records if r["event"] == "activation"]
        ),
        "activation_failures": sum(r.get("activation") == "failed" for r in records),
        "unexpected_activation_errors": sum(r.get("unexpected_error", False) for r in records),
    }


def policies(refs, strategy):
    choices = refs if strategy.startswith("adaptive") else [refs[STRATEGIES.index(strategy)]]
    return BestFeasibleSelector(
        [
            ModelPolicy(
                ref,
                PROTOCOL["policy"][ref.id]["priority"],
                (
                    Constraint(
                        "battery", Operator.GTE, PROTOCOL["policy"][ref.id]["minimum_battery"]
                    ),
                ),
            )
            for ref in choices
        ]
    )


async def select(runtime, selector, battery):
    return selector.select(
        slot="vision",
        models=await runtime.models("vision"),
        runtime=await runtime.state("vision"),
        context={"battery": battery},
    )


async def reconcile(temms, runtime, selector, battery, fault):
    started = now_ms()
    preview = await select(runtime, selector, battery)
    record = {"event": "activation", "selected": run.identity(preview.selected_model)}
    try:
        result = await temms.reconcile("vision", {"battery": battery})
        record["activation"] = "published" if result.activation else "unchanged"
    except Exception as error:
        record.update(
            activation="failed",
            error=f"{type(error).__name__}: {error}",
            unexpected_error=not (fault and isinstance(error, ModelUnavailableError)),
        )
    record["started_ms"], record["completed_ms"] = started, now_ms()
    record["reconcile_ms"] = record["completed_ms"] - started
    record["active"] = run.identity((await runtime.state("vision")).active_model)
    return preview, record


def feasible(model, preview):
    return model is not None and any(
        e.feasible and (e.model.id, e.model.digest) == (model.id, model.digest)
        for e in preview.evaluations
    )


async def serve(temms, runtime, preview, directory, item, labels):
    active = (await runtime.state("vision")).active_model
    record = {
        "active": run.identity(active),
        "active_at_completion": run.identity(active),
        "serving": None,
        "correct": False,
        "outcome": "abstained",
        "prediction": None,
    }
    if not feasible(active, preview):
        return record
    try:
        started = now_ms()
        feed = assets.preprocess(directory / item["name"])
        record["preprocess_ms"] = now_ms() - started
        started = now_ms()
        result = await temms.infer("vision", feed)
        record["infer_ms"] = now_ms() - started
        prediction = run.prediction(result.output, labels)
        synset = labels[prediction["index"]].split(" ", 1)[0]
        record.update(
            outcome="ok",
            serving=run.identity(result.model),
            prediction=prediction,
            predicted_synset=synset,
            correct=synset == item["expected_synset"],
        )
    except Exception as error:
        record.update(outcome="error", error=f"{type(error).__name__}: {error}")
    record["active_at_completion"] = run.identity((await runtime.state("vision")).active_model)
    return record


async def evaluate(
    runtime,
    selector,
    strategy,
    files,
    directory,
    labels,
    large_path,
    emit,
    period_ms=100,
    deadline_ms=100,
):
    """Offer identical arrival-time context and inputs, preserving the entire FIFO trace."""
    assignments = {item.get("stage") for item in files}
    if assignments not in ({None}, {stage for stage, _, _ in run.SCENARIO}):
        raise ValueError("workload must be unstaged or cover all scenario phases without mixing")
    temms = TEMMS(runtime=runtime, selector=selector)
    origin, request = now_ms(), 0
    for stage, battery, fault in run.SCENARIO:
        phase_files = [item for item in files if item.get("stage") in (None, stage)]
        hidden = large_path.with_suffix(".unavailable")
        if fault:
            large_path.rename(hidden)
        try:
            await evaluate_phase(
                temms, runtime, selector, strategy, phase_files, directory, labels, emit,
                stage, battery, fault, request, origin, period_ms, deadline_ms,
            )
            request += len(phase_files)
        finally:
            if fault and hidden.exists():
                hidden.rename(large_path)
    return now_ms() - origin


async def begin_phase(temms, runtime, selector, strategy, battery, fault):
    if strategy != "adaptive-background":
        preview, activation = await reconcile(temms, runtime, selector, battery, fault)
        return preview, activation, None
    preview = await select(runtime, selector, battery)
    pending = asyncio.create_task(reconcile(temms, runtime, selector, battery, fault))
    try:
        # Dispatch loading before preprocessing/admitting the first request.
        await asyncio.sleep(0)
        active = (await runtime.state("vision")).active_model
        if not feasible(active, preview) and preview.selected_model is not None:
            _, activation = await pending
            return preview, activation, None
        return preview, None, pending
    except BaseException:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        raise


def emit_activation(emit, activation, stage, request, origin):
    emit(activation | {
        "stage": stage, "request": request,
        "started_ms": activation["started_ms"] - origin,
        "completed_ms": activation["completed_ms"] - origin,
    })


async def evaluate_phase(
    temms, runtime, selector, strategy, files, directory, labels, emit,
    stage, battery, fault, first_request, origin, period_ms, deadline_ms,
):
    pending = None
    try:
        for index, item in enumerate(files):
            request = first_request + index
            scheduled = request * period_ms
            await asyncio.sleep(max(0, (origin + scheduled - now_ms()) / 1000))
            started = now_ms()
            activation = None
            if index == 0:
                preview, activation, pending = await begin_phase(
                    temms, runtime, selector, strategy, battery, fault
                )
            record = await serve(temms, runtime, preview, directory, item, labels)
            completed = now_ms() - origin
            record.update(
                event="request", request=request, strategy=strategy, stage=stage,
                battery=battery, simulated=True, input=item["name"],
                expected_synset=item["expected_synset"], scheduled_ms=scheduled,
                completed_ms=completed, queue_ms=max(0, started - origin - scheduled),
                latency_ms=completed - scheduled, late=completed - scheduled > deadline_ms,
                selected=run.identity(preview.selected_model),
            )
            if pending is not None and pending.done():
                _, activation = await pending
                pending = None
            if activation is not None:
                emit_activation(emit, activation, stage, first_request, origin)
            emit(record)
        # Drain before changing context or restoring the private fault artifact.
        if pending is not None:
            _, activation = await pending
            pending = None
            emit_activation(emit, activation, stage, first_request, origin)
    except BaseException:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        raise


def peak_rss_bytes():
    if sys.platform not in ("darwin", "linux"):
        return None
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def environment():
    cpu, cpu_error = platform.machine(), None
    try:
        if sys.platform == "darwin":
            cpu = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True,
                stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
        else:
            cpu = platform.processor() or cpu
    except (OSError, subprocess.SubprocessError) as error:
        cpu_error = str(error)
    return {
        "platform": platform.platform(),
        "cpu": cpu,
        "cpu_detail_error": cpu_error,
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "versions": {name: version(name) for name in ("temms", "onnxruntime", "numpy", "Pillow")},
    }


def worker(args):
    manifest = assets.verify_assets(args.assets)
    data = workload.verify_workload(args.assets, args.workload)
    labels = (args.assets / "labels.txt").read_text().splitlines()
    if not all(
        item["expected_synset"] in {line.split(" ")[0] for line in labels} for item in data["files"]
    ):
        raise ValueError("workload label is outside the model label space")
    warmup = assets.preprocess(args.assets / "chelsea.png")
    digests = {item["name"]: item["sha256"] for item in manifest["files"]}
    refs = [
        ModelRef(name, "sha256:" + digests[name + ".onnx"]) for name in ("mobilenetv2", "resnet18")
    ]
    records, loads = [], []

    def session(path, providers):
        started = now_ms()
        loaded = run.cpu_session(path, providers)
        load_ms = now_ms() - started
        started = now_ms()
        loaded.run(None, warmup)
        loads.append(
            {
                "event": "session",
                "model_file": Path(path).name,
                "load_ms": load_ms,
                "warmup_ms": now_ms() - started,
            }
        )
        return loaded

    with (
        args.records.open("x") as output,
        tempfile.TemporaryDirectory(prefix="temms-compare-") as tmp,
    ):

        def emit(record):
            records.append(record)
            output.write(json.dumps(record, allow_nan=False) + "\n")
            output.flush()

        emit(
            {
                "event": "run",
                "strategy": args.strategy,
                "workload_name": args.workload,
                "repetition": args.repetition,
                "utc": datetime.now(UTC).isoformat(),
                "protocol": PROTOCOL,
                "environment": environment(),
                "assets": manifest,
                "workload": data,
                "command": sys.argv,
                "source_sha256": {
                    name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                    for name in ("compare.py", "run.py", "assets.py", "workload.py")
                },
            }
        )
        large = Path(tmp) / "resnet18.onnx"
        shutil.copyfile(args.assets / large.name, large)
        runtime = OnnxRuntime(
            models={
                "vision": [
                    OnnxModel(refs[0], args.assets / "mobilenetv2.onnx", ("CPUExecutionProvider",)),
                    OnnxModel(refs[1], large, ("CPUExecutionProvider",)),
                ]
            },
            session_factory=session,
        )
        with (
            ThreadPoolExecutor(max_workers=PROTOCOL["executor_workers"]) as executor,
            asyncio.Runner() as runner,
        ):
            runner.get_loop().set_default_executor(executor)
            finished_ms = runner.run(evaluate(
                runtime, policies(refs, args.strategy), args.strategy, data["files"],
                args.assets, labels, large, emit, PROTOCOL["period_ms"], PROTOCOL["deadline_ms"],
            ))
        for entry in loads:
            emit(entry)
        summary = summarize(records) | {
            "event": "summary",
            "peak_rss_bytes": peak_rss_bytes(),
            "load_ms": distribution([r["load_ms"] for r in loads]),
            "warmup_ms": distribution([r["warmup_ms"] for r in loads]),
            "trace_and_drain_ms": finished_ms,
        }
        emit(summary)
    print(
        f"{args.strategy} repeat {args.repetition}: "
        f"{summary['useful']}/{summary['offered']} useful, "
        f"{summary['abstained']} abstained, {summary['late']} late",
        flush=True,
    )
    if summary["errors"] or summary["unexpected_activation_errors"]:
        raise RuntimeError(f"unexpected errors; inspect {args.records}")


def compare(args):
    assets.verify_assets(args.assets)
    data = workload.verify_workload(args.assets, args.workload)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "protocol.json").write_text(json.dumps(PROTOCOL, indent=2) + "\n")
    (args.output / "workload.json").write_text(json.dumps(data, indent=2) + "\n")
    summaries = []
    for repetition in range(PROTOCOL["repetitions"]):
        order = STRATEGIES[repetition:] + STRATEGIES[:repetition]
        for strategy in order:
            records = args.output / f"{strategy}-{repetition + 1}.jsonl"
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--strategy",
                    strategy,
                    "--assets",
                    str(args.assets.resolve()),
                    "--workload",
                    args.workload,
                    "--records",
                    str(records.resolve()),
                    "--repetition",
                    str(repetition + 1),
                ],
                check=True,
            )
            rows = [json.loads(line) for line in records.read_text().splitlines()]
            summaries.append({"strategy": strategy, "repetition": repetition + 1, **rows[-1]})
    (args.output / "summary.json").write_text(json.dumps(summaries, indent=2) + "\n")
    print("\nStrategy             Useful/offered  Correct  Abstained  Late  p95 all ms   Peak MiB")
    for strategy in STRATEGIES:
        runs = [row for row in summaries if row["strategy"] == strategy]
        counts = {key: sum(row[key] for row in runs)
                  for key in ("useful", "offered", "correct", "abstained", "late")}
        p95 = [row["latency_ms_all"]["p95"] for row in runs]
        peaks = [row["peak_rss_bytes"] for row in runs if row["peak_rss_bytes"] is not None]
        peak = f"{max(peaks) / 1024**2:.1f}" if peaks else "unavailable"
        print(f"{strategy:20} {counts['useful']:3}/{counts['offered']:<10} "
              f"{counts['correct']:7} {counts['abstained']:10} {counts['late']:5} "
              f"{min(p95):5.1f}–{max(p95):5.1f} {peak:>10}")
    print("Counts pool three runs; p95 is per-run range; RSS is whole-process maximum.")
    print(f"Comparison complete; raw runs and summaries: {args.output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=assets.DEFAULT_ASSETS)
    parser.add_argument("--workload", choices=("small", "validation"), default="small")
    parser.add_argument(
        "--output",
        type=Path,
        default=assets.DEFAULT_ASSETS / datetime.now(UTC).strftime("comparison-%Y%m%dT%H%M%S%fZ"),
    )
    parser.add_argument("--strategy", choices=STRATEGIES, help=argparse.SUPPRESS)
    parser.add_argument("--records", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--repetition", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.strategy:
        if args.records is None:
            parser.error("a worker needs --records")
        worker(args)
    else:
        compare(args)


if __name__ == "__main__":
    main()
