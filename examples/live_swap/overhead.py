"""Compare four inference paths on one warmed ONNX session, without retries."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import time
import uuid
from importlib.metadata import version
from itertools import permutations
from pathlib import Path

import numpy as np

from temms import TEMMS, BestFeasibleSelector, ModelPolicy, ModelRef
from temms.adapters import OnnxModel, OnnxRuntime

if __package__:
    from .assets import preprocess, verify_assets
    from .run import classifier_scores, cpu_session
else:
    from assets import preprocess, verify_assets
    from run import classifier_scores, cpu_session

PATHS = ("native", "threaded", "adapter", "temms")
ORDERS = tuple(permutations(PATHS))
DELTAS = tuple(zip(PATHS, PATHS[1:], strict=False))
WARMUPS = 8


def block_count(value):
    count = int(value)
    if count <= 0 or count % len(ORDERS):
        raise argparse.ArgumentTypeError("blocks must be a positive multiple of 24")
    return count


def freeze(output):
    scores = classifier_scores(output).copy(order="C")
    scores.setflags(write=False)
    return scores


def check(result, path, ref, reference, fingerprint):
    serving = None
    if path in ("adapter", "temms"):
        if (result.model.id, result.model.digest) != (ref.id, ref.digest):
            raise ValueError("inference attribution changed during a fixed-session block")
        serving = {"id": result.model.id, "digest": result.model.digest}
        result = result.output
    scores = classifier_scores(result)
    if scores.dtype != reference.dtype or not np.array_equal(scores, reference):
        raise ValueError("full output differs from the frozen reference")
    actual = hashlib.sha256(scores.tobytes()).hexdigest()
    if actual != fingerprint:
        raise ValueError("output fingerprint differs from the frozen reference")
    return {"sha256": actual, "serving": serving}


async def invoke(path, session, runtime, temms, feed, *, timed=False):
    # Select the branch before starting the timer; no coroutine/harness setup is timed.
    clock = time.perf_counter_ns if timed else lambda: 0
    if path == "native":
        started = clock()
        result = session.run(None, dict(feed))
    elif path == "threaded":
        started = clock()
        result = await asyncio.to_thread(session.run, None, dict(feed))
    elif path == "adapter":
        started = clock()
        result = await runtime.infer("vision", feed)
    else:
        started = clock()
        result = await temms.infer("vision", feed)
    return result, clock() - started


def summarize(rows):
    summary = {"event": "summary", "model": rows[0]["model"],
               "digest": rows[0]["digest"], "blocks": len(rows)}
    for field in ("ns", "delta_ns"):
        summary[field] = {}
        for name in rows[0][field]:
            values = [row[field][name] for row in rows]
            summary[field][name] = {
                "mean": float(np.mean(values)),
                "p50": float(np.percentile(values, 50, method="linear")),
                "p95": float(np.percentile(values, 95, method="linear")),
            }
    return summary


async def measure(runtime, session, ref, feed, blocks, emit):
    block_count(blocks)
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector([ModelPolicy(ref)]))
    reference = freeze(session.run(None, dict(feed)))
    fingerprint = hashlib.sha256(reference.tobytes()).hexdigest()
    for _ in range(WARMUPS):
        for path in PATHS:
            result, _ = await invoke(path, session, runtime, temms, feed)
            check(result, path, ref, reference, fingerprint)
    emit({"event": "reference", "model": ref.id, "digest": ref.digest,
          "shape": list(reference.shape), "dtype": reference.dtype.str,
          "sha256": fingerprint, "scores": reference.tolist()})
    rows = []
    for block in range(blocks):
        order, durations, outputs = ORDERS[block % len(ORDERS)], {}, {}
        for path in order:
            result, durations[path] = await invoke(
                path, session, runtime, temms, feed, timed=True,
            )
            outputs[path] = check(result, path, ref, reference, fingerprint)
        row = {
            "event": "block", "model": ref.id, "digest": ref.digest,
            "block": block, "order": order, "ns": durations,
            "delta_ns": {f"{right}-{left}": durations[right] - durations[left]
                         for left, right in DELTAS},
            "reference_sha256": fingerprint, "outputs": outputs,
        }
        rows.append(row)
        emit(row)
    emit(summarize(rows))


async def run(directory, manifest, feed, blocks, emit, *, reverse_models=False):
    block_count(blocks)
    files = {record["name"]: record for record in manifest["files"]}
    models = manifest["models"][::-1] if reverse_models else manifest["models"]
    for model in models:
        captured = []

        def factory(path, providers, captured=captured):
            session = cpu_session(path, providers)
            captured.append(session)
            return session

        ref = ModelRef(model["id"], "sha256:" + files[model["file"]]["sha256"])
        runtime = OnnxRuntime(
            models={"vision": [OnnxModel(
                ref, directory / model["file"], ("CPUExecutionProvider",), feed,
            )]}, session_factory=factory,
        )
        await runtime.activate("vision", ref)
        session, = captured
        await measure(runtime, session, ref, feed, blocks, emit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path(".demo-assets/live-swap"))
    parser.add_argument("--blocks", type=block_count, default=240)
    parser.add_argument("--reverse-models", action="store_true")
    parser.add_argument("--records", type=Path, required=True)
    args = parser.parse_args()
    manifest = verify_assets(args.assets)
    feed = preprocess(args.assets / "chelsea.png")
    for value in feed.values():
        value.setflags(write=False)
    with args.records.open("x", encoding="utf-8") as log:
        def emit(record):
            log.write(json.dumps(record, allow_nan=False) + "\n")
            log.flush()
            if record["event"] == "summary":
                medians = ", ".join(f"{name} {stats['p50'] / 1000:+.3f} us"
                                    for name, stats in record["delta_ns"].items())
                print(f"{record['model']}: {record['blocks']} blocks; median deltas {medians}")

        models = manifest["models"][::-1] if args.reverse_models else manifest["models"]
        emit({
            "event": "run", "schema": "temms-overhead", "schema_version": 2,
            "run_id": str(uuid.uuid4()), "pid": os.getpid(), "platform": platform.platform(),
            "python": platform.python_version(), "manifest": manifest,
            "versions": {name: version(name)
                         for name in ("temms", "onnxruntime", "numpy", "Pillow")},
            "clock": {"name": "perf_counter_ns", "unit": "ns",
                      **vars(time.get_clock_info("perf_counter"))},
            "provider": "CPUExecutionProvider", "intra_op_threads": 1, "inter_op_threads": 1,
            "cpu_arena": True, "paths": PATHS, "orders": ORDERS,
            "model_order": [model["id"] for model in models], "blocks_per_model": args.blocks,
            "warmup": {"activation_calls_per_model": 1, "activation_output_checked": False,
                       "reference_calls_per_model": 1, "checked_calls_per_path": WARMUPS,
                       "total_untimed_calls_per_model": 2 + len(PATHS) * WARMUPS},
            "input": "chelsea.png", "quantiles": "linear",
            "output_check": "exact shape/dtype/values and SHA-256",
            "scope": "Serial warmed calls, one captured session per model; native blocks the loop.",
            "timed": "Method call, dispatch, feed mapping, result wrapping and two clock reads.",
            "excluded": "Activation, preprocessing, selection, reconciliation, checks, reference "
                        "copies and I/O.",
            "limitation": "Signed deltas include execution/scheduling noise; blocks are not "
                          "independent process replicates. Activation warmup output is unobserved.",
        })
        asyncio.run(run(args.assets, manifest, feed, args.blocks, emit,
                        reverse_models=args.reverse_models))
    print(f"Raw timings: {args.records}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
