"""Compare paired direct-runtime and TEMMS inference on the same warmed session."""

import argparse
import asyncio
import json
import platform
import time
from importlib.metadata import version
from pathlib import Path

import numpy as np

from temms import TEMMS, BestFeasibleSelector, ModelPolicy, ModelRef
from temms.adapters import OnnxModel, OnnxRuntime

if __package__:
    from .assets import preprocess, verify_assets
    from .run import cpu_session
else:
    from assets import preprocess, verify_assets
    from run import cpu_session


def summarize(rows):
    summary = {"event": "summary", "model": rows[0]["model"], "pairs": len(rows)}
    for field in ("direct_ns", "wrapped_ns", "delta_ns"):
        values = [row[field] for row in rows]
        summary[field] = {
            "mean": float(np.mean(values)),
            "p50": float(np.percentile(values, 50, method="linear")),
            "p95": float(np.percentile(values, 95, method="linear")),
        }
    return summary


async def measure(runtime, ref, feed, pairs, emit):
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector([ModelPolicy(ref)]))
    methods = {"direct": runtime.infer, "wrapped": temms.infer}
    rows = []
    for pair in range(pairs):
        order = ("direct", "wrapped") if pair % 2 == 0 else ("wrapped", "direct")
        durations, results = {}, {}
        for name in order:
            started = time.perf_counter_ns()
            result = await methods[name]("vision", feed)
            durations[name] = time.perf_counter_ns() - started
            results[name] = result
        # Validation and record writing are deliberately outside both timed calls.
        if any(result.model != ref for result in results.values()):
            raise RuntimeError("inference attribution changed during a fixed-session pair")
        left, right = results["direct"].output, results["wrapped"].output
        if len(left) != len(right):
            raise RuntimeError("direct and wrapped inference returned different output counts")
        for direct, wrapped in zip(left, right, strict=True):
            np.testing.assert_array_equal(direct, wrapped)
        row = {
            "event": "pair", "model": ref.id, "digest": ref.digest,
            "pair": pair, "order": order,
            "direct_ns": durations["direct"], "wrapped_ns": durations["wrapped"],
            "delta_ns": durations["wrapped"] - durations["direct"],
        }
        rows.append(row)
        emit(row)
    emit(summarize(rows))


async def run(directory, manifest, feed, pairs, emit):
    files = {record["name"]: record for record in manifest["files"]}
    for model in manifest["models"]:
        ref = ModelRef(model["id"], "sha256:" + files[model["file"]]["sha256"])
        runtime = OnnxRuntime(
            models={"vision": [OnnxModel(
                ref, directory / model["file"], ("CPUExecutionProvider",), feed,
            )]},
            session_factory=cpu_session,
        )
        await runtime.activate("vision", ref)  # Loads and warms once, outside measurement.
        await measure(runtime, ref, feed, pairs, emit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path(".demo-assets/live-swap"))
    parser.add_argument("--pairs", type=int, default=100)
    parser.add_argument("--records", type=Path, required=True)
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("pairs must be positive")
    manifest = verify_assets(args.assets)
    feed = preprocess(args.assets / "chelsea.png")
    with args.records.open("x", encoding="utf-8") as log:
        def emit(record):
            log.write(json.dumps(record, allow_nan=False) + "\n")
            log.flush()
            if record["event"] == "summary":
                delta = record["delta_ns"]["p50"] / 1000
                print(f"{record['model']}: {record['pairs']} pairs; median delta {delta:+.3f} us")

        emit({
            "event": "run", "platform": platform.platform(),
            "python": platform.python_version(), "manifest": manifest,
            "versions": {name: version(name) for name in ("onnxruntime", "numpy", "Pillow")},
            "provider": "CPUExecutionProvider", "intra_op_threads": 1, "inter_op_threads": 1,
            "pairs_per_model": args.pairs, "warmup_calls_per_model": 1,
            "input": "chelsea.png", "quantiles": "linear",
            "scope": "Paired complete infer calls; wrapped minus direct, same warmed session.",
            "excluded": "Activation, preprocessing, selection, no-op reconciliation, checks, I/O.",
            "limitation": "Signed differences include native execution and scheduling noise.",
        })
        asyncio.run(run(args.assets, manifest, feed, args.pairs, emit))
    print(f"Raw timings: {args.records}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
