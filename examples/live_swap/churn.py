"""Measure bounded ONNX session lifetime and whole-process memory, offline."""

import argparse
import asyncio
import ctypes
import gc
import hashlib
import json
import os
import sys
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, ModelRef, Operator
from temms.adapters import OnnxModel, OnnxRuntime

if __package__:
    from . import assets, compare, run
else:
    import assets
    import compare
    import run

STRATEGIES = ("fixed-small", "fixed-large", "swapping")
BACKENDS = ("temms", "direct")
PROTOCOL = {
    "strategies": STRATEGIES,
    "backends": BACKENDS,
    "replacements": 30,
    "steps": 31,
    "cpu_arena": "on",
    "repetitions": 3,
    "order": "fresh serial processes; experiment launcher records the exact order",
    "input": "same preprocessed Chelsea photo at every step",
    "warmup": "one inference per newly constructed session",
    "provider": "CPUExecutionProvider",
    "intra_threads": 1,
    "inter_threads": 1,
    "executor_workers": 1,
    "boundary": "serial reconcile/infer then awaited worker barrier; current output retained",
    "output_lifetime": "retain through next reconciliation, verify unchanged, then release",
    "gc": "normal during churn; forced once after owner drop and worker shutdown",
    "memory_scope": "whole-process current/high-water RSS; no leak threshold",
    "acceptance": "correct identity; finite outputs; only active wrapper alive; none after drop",
    "direct_control": "same load/warm/infer worker; publish/drop old on caller; no TEMMS/runtime",
    "timing": (
        "load and warmup inside factory; reconcile wall time includes publication and retirement; "
        "step includes prior-output integrity check; infer and step end after prediction, "
        "before new-output hashing/barrier/RSS/record writes"
    ),
}


def current_rss():
    """Return bytes plus an explicit error when a platform cannot supply RSS."""
    try:
        if sys.platform == "darwin":
            return _darwin_rss(), None
        if sys.platform == "linux":
            lines = Path("/proc/self/smaps_rollup").read_text().splitlines()
            value = next(line.split()[1] for line in lines if line.startswith("Rss:"))
            return int(value) * 1024, None
        return None, f"current RSS unsupported on {sys.platform}"
    except (OSError, AttributeError, ValueError, StopIteration) as error:
        return None, f"{type(error).__name__}: {error}"


class _BasicInfo(ctypes.Structure):
    # Apple's mach/task_info.h: MACH_TASK_BASIC_INFO = 20, resident_size is bytes.
    _pack_ = 4
    _fields_ = [
        ("virtual_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64),
        ("resident_size_max", ctypes.c_uint64), ("user_time", ctypes.c_int32 * 2),
        ("system_time", ctypes.c_int32 * 2), ("policy", ctypes.c_int32),
        ("suspend_count", ctypes.c_int32),
    ]


@cache
def _darwin_library():
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    library.mach_task_self.argtypes = []
    library.mach_task_self.restype = ctypes.c_uint32
    library.task_info.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
    ]
    library.task_info.restype = ctypes.c_int32
    return library


def _darwin_rss():
    library = _darwin_library()
    info = _BasicInfo()
    count = ctypes.c_uint32(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_uint32))
    status = library.task_info(
        library.mach_task_self(), 20, ctypes.byref(info), ctypes.byref(count),
    )
    if status or count.value != 12:
        raise OSError(f"task_info failed: status={status}, count={count.value}")
    return info.resident_size


def sample(event, references, **fields):
    rss, error = current_rss()
    return {
        "event": event, "monotonic_ns": time.perf_counter_ns(),
        "created": len(references),
        "live_instances": [i for i, reference in enumerate(references) if reference() is not None],
        "rss_bytes": rss, "rss_error": error, "peak_rss_bytes": compare.peak_rss_bytes(),
        **fields,
    }


def fingerprint(output):
    return hashlib.sha256(output[0].tobytes()).hexdigest()


def managed(configs, session):
    runtime = OnnxRuntime(models={"vision": configs}, session_factory=session)
    selector = BestFeasibleSelector([
        ModelPolicy(item.ref, 1, (Constraint("model", Operator.EQ, item.ref.id),))
        for item in configs
    ])
    return TEMMS(runtime=runtime, selector=selector), runtime


def check_step(record, ref, expected):
    if record["serving"] != run.identity(ref) or record["active"] != run.identity(ref):
        raise RuntimeError("active/serving identity does not match the requested model")
    if record["created"] != expected or record["live_instances"] != [expected - 1]:
        raise RuntimeError("unexpected constructed or surviving session wrappers")


async def measure(
    directory, manifest, strategy, feed, labels, references, emit, backend="temms",
    steps=31, cpu_arena=True,
):
    digests = {item["name"]: item["sha256"] for item in manifest["files"]}
    refs = [ModelRef(item["id"], "sha256:" + digests[item["file"]]) for item in manifest["models"]]
    load_timings = {}

    def session(path, providers):
        started = time.perf_counter_ns()
        loaded = run.cpu_session(path, providers, cpu_arena=cpu_arena)
        load_ms = (time.perf_counter_ns() - started) / 1_000_000
        references.append(weakref.ref(loaded))
        started = time.perf_counter_ns()
        loaded.run(None, dict(feed))
        load_timings.update(
            load_ms=load_ms, warmup_ms=(time.perf_counter_ns() - started) / 1_000_000,
        )
        return loaded

    configs = [
        OnnxModel(ref, directory / item["file"], ("CPUExecutionProvider",))
        for ref, item in zip(refs, manifest["models"], strict=True)
    ]

    temms, runtime = managed(configs, session) if backend == "temms" else (None, None)
    direct = None
    previous, previous_digest = None, None
    for step in range(steps):
        index = step % 2 if strategy == "swapping" else STRATEGIES.index(strategy)
        ref = refs[index]
        load_timings.clear()
        started = time.perf_counter_ns()
        if backend == "direct" and (step == 0 or strategy == "swapping"):
            direct = await asyncio.to_thread(
                session, str(configs[index].path), configs[index].providers,
            )
        elif backend == "temms":
            await temms.reconcile("vision", {"model": ref.id})
        reconcile_ms = (time.perf_counter_ns() - started) / 1_000_000
        if previous is not None and fingerprint(previous) != previous_digest:
            raise RuntimeError("retained output changed across reconciliation")
        previous = None
        infer_started = time.perf_counter_ns()
        if backend == "direct":
            output = tuple(await asyncio.to_thread(direct.run, None, dict(feed)))
            serving = active = ref
        else:
            result = await temms.infer("vision", feed)
            output, serving = result.output, result.model
            active = (await runtime.state("vision")).active_model
            del result
        prediction = run.prediction(output, labels)
        finished = time.perf_counter_ns()
        previous_digest = fingerprint(output)
        previous = output
        await asyncio.to_thread(lambda: None)  # One worker: prior callables have been released.
        record = sample(
            "step", references, step=step, active=run.identity(active),
            serving=run.identity(serving), prediction=prediction,
            output_sha256=previous_digest,
            load_ms=load_timings.get("load_ms", 0.0), warmup_ms=load_timings.get("warmup_ms", 0.0),
            reconcile_ms=reconcile_ms, infer_ms=(finished - infer_started) / 1_000_000,
            step_ms=(finished - started) / 1_000_000,
        )
        emit(record)
        expected = step + 1 if strategy == "swapping" else 1
        check_step(record, ref, expected)
        del output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=assets.DEFAULT_ASSETS)
    parser.add_argument("--strategy", choices=STRATEGIES, required=True)
    parser.add_argument("--backend", choices=BACKENDS, default="temms")
    parser.add_argument("--cpu-arena", choices=("on", "off"), default="on")
    parser.add_argument("--steps", type=int, default=PROTOCOL["steps"])
    parser.add_argument("--records", type=Path, required=True)
    args = parser.parse_args()
    if args.steps < 1:
        parser.error("--steps must be positive")
    protocol = PROTOCOL | {
        "steps": args.steps, "replacements": args.steps - 1 if args.strategy == "swapping" else 0,
        "cpu_arena": args.cpu_arena,
        "memory_pattern": run.ort.SessionOptions().enable_mem_pattern,
        "memory_reuse": run.ort.SessionOptions().enable_mem_reuse,
    }
    manifest = assets.verify_assets(args.assets)
    feed = assets.preprocess(args.assets / "chelsea.png")
    labels = (args.assets / "labels.txt").read_text().splitlines()
    references = []
    with args.records.open("x") as output:

        def emit(record):
            output.write(json.dumps(record, allow_nan=False) + "\n")
            output.flush()

        emit({
            "event": "run", "utc": datetime.now(UTC).isoformat(), "strategy": args.strategy,
            "backend": args.backend, "pid": os.getpid(),
            "protocol": protocol, "environment": compare.environment(), "assets": manifest,
            "sources": {
                Path(module.__file__).name:
                    hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                for module in (sys.modules[__name__], assets, run, compare)
            },
        })
        emit(sample("prepared", references))
        with ThreadPoolExecutor(max_workers=1) as executor, asyncio.Runner() as runner:
            runner.get_loop().set_default_executor(executor)
            runner.run(measure(
                args.assets, manifest, args.strategy, feed, labels, references, emit, args.backend,
                args.steps, args.cpu_arena == "on",
            ))
        dropped = sample("owners_dropped", references)
        emit(dropped)
        collected = gc.collect()
        final = sample("post_gc", references, gc_collected=collected)
        emit(final)
        if dropped["live_instances"] or final["live_instances"]:
            raise RuntimeError("session wrappers survived owner and executor shutdown")
    print(json.dumps({
        "strategy": args.strategy, "backend": args.backend, "cpu_arena": args.cpu_arena,
        "records": str(args.records), **final,
    }))


if __name__ == "__main__":
    main()
