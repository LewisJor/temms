"""Exercise real local models in a constrained, disconnected Linux worker."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from onnxruntime.capi.onnxruntime_pybind11_state import InvalidProtobuf

import temms
from examples.edge import resources
from examples.live_swap import assets, compare, run, workload
from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, ModelRef, Operator
from temms.adapters import OnnxModel, OnnxRuntime
from temms.errors import NoActiveModelError

ECONOMY = {"input_ready": True, "detail_required": False}
DETAIL = {"input_ready": True, "detail_required": True}
SWAPS = 12


def policies(refs):
    ready = Constraint("input_ready", Operator.EQ, True)
    return BestFeasibleSelector([
        ModelPolicy(refs[0], 10, (ready,)),
        ModelPolicy(refs[1], 20, (ready, Constraint("detail_required", Operator.EQ, True))),
    ])


async def switch(temms, runtime, selector, context, phase, emit, corrupt=False):
    before = (await runtime.state("vision")).active_model
    decision = selector.select(slot="vision", models=await runtime.models("vision"),
                               runtime=await runtime.state("vision"), context=context)
    started = time.perf_counter()
    error = None
    try:
        result = await temms.reconcile("vision", context)
    except InvalidProtobuf as failure:
        if not corrupt:
            raise
        error = {"type": type(failure).__name__, "message": str(failure)}
        activation = "failed"
    else:
        if corrupt:
            raise RuntimeError("corrupted replacement unexpectedly activated")
        activation = "published" if result.activation else "unchanged"
    active = (await runtime.state("vision")).active_model
    expected = before if corrupt or decision.selected_model is None else decision.selected_model
    if active != expected:
        raise RuntimeError("activation did not preserve or publish the expected model")
    emit({"event": "activation", "phase": phase, "context": context,
          "previous": run.identity(before), "selected": run.identity(decision.selected_model),
          "active": run.identity(active), "activation": activation, "error": error,
          "elapsed_ms": (time.perf_counter() - started) * 1000})
    return active


async def serve(temms, runtime, samples, labels, phase, emit, measured):
    for sample in samples:
        admitted = (await runtime.state("vision")).active_model
        started = time.perf_counter()
        result = await temms.infer("vision", sample["feed"])
        elapsed = (time.perf_counter() - started) * 1000
        checked = run.check_output(result, sample, admitted, labels)
        emit({"event": "frame", "phase": phase, "request": len(measured) + 1,
              "image": sample["image"], "expected": sample["expected"],
              "admitted": run.identity(admitted), "serving": run.identity(result.model),
              "active": run.identity((await runtime.state("vision")).active_model),
              "elapsed_ms": elapsed, **checked})
        if not checked["reference_match"] or not checked["identity_match"]:
            raise RuntimeError("served output or admitted identity differs from direct reference")
        measured.append(elapsed)


async def exercise(runtime, selector, samples, labels, large, source, emit):
    temms = TEMMS(runtime=runtime, selector=selector)
    measured = []
    if (await runtime.state("vision")).active_model is not None:
        raise RuntimeError("fresh runtime unexpectedly has an active model")
    try:
        await temms.infer("vision", samples[0]["feed"])
    except NoActiveModelError:
        emit({"event": "startup", "active": None, "inference_error": "NoActiveModelError"})
    else:
        raise RuntimeError("fresh runtime unexpectedly served without activation")

    await switch(temms, runtime, selector, ECONOMY, "offline-start", emit)
    await serve(temms, runtime, samples, labels, "offline-start", emit, measured)
    await switch(temms, runtime, selector, DETAIL, "detail-request", emit)
    await serve(temms, runtime, samples, labels, "detail-request", emit, measured)

    with resources.cpu_pressure() as pressure:
        await switch(temms, runtime, selector, ECONOMY, "cpu-contention", emit)
        await serve(temms, runtime, samples, labels, "cpu-contention", emit, measured)
    emit({"event": "pressure", "phase": "cpu-contention", **pressure})

    # The cached source is mounted read-only. Damage only this private working copy.
    original = hashlib.sha256(large.read_bytes()).hexdigest()
    large.write_bytes(b"temms deliberately truncated ONNX artifact\n")
    emit({"event": "artifact", "phase": "corrupt-update", "original_sha256": original,
          "actual_sha256": hashlib.sha256(large.read_bytes()).hexdigest()})
    await switch(temms, runtime, selector, DETAIL, "corrupt-update", emit, corrupt=True)
    await serve(temms, runtime, samples, labels, "corrupt-update", emit, measured)

    restored = large.with_suffix(".restored")
    shutil.copyfile(source, restored)
    if hashlib.sha256(restored.read_bytes()).hexdigest() != original:
        raise RuntimeError("restored artifact differs from the prepared source")
    restored.replace(large)
    emit({"event": "artifact", "phase": "restored-update", "actual_sha256": original})
    await switch(temms, runtime, selector, DETAIL, "restored-update", emit)
    await serve(temms, runtime, samples, labels, "restored-update", emit, measured)

    for index in range(SWAPS):
        phase = f"swap-{index + 1:02}"
        context = ECONOMY if index % 2 == 0 else DETAIL
        await switch(temms, runtime, selector, context, phase, emit)
        await serve(temms, runtime, [samples[index % len(samples)]], labels, phase, emit, measured)
    # Missing input health is an application admission decision, not a TEMMS shutdown.
    before = (await runtime.state("vision")).active_model
    active = await switch(temms, runtime, selector, {}, "missing-context", emit)
    if active != before:
        raise RuntimeError("abstention changed runtime ownership")
    emit({"event": "abstention", "phase": "missing-context", "active": run.identity(active),
          "reason": "input_ready is missing; application did not call infer"})
    await switch(temms, runtime, selector, ECONOMY, "resume", emit)
    await serve(temms, runtime, [samples[0]], labels, "resume", emit, measured)
    return measured


def header(manifest, data, samples, profile, network, filesystem):
    site = Path(sys.prefix).resolve()
    module = Path(temms.__file__).resolve()
    if not module.is_relative_to(site) or os.getuid() == 0:
        raise RuntimeError("qualification requires an installed package and an unprivileged worker")
    return {
        "event": "run", "run_id": str(uuid.uuid4()), "utc": datetime.now(UTC).isoformat(),
        "pid": os.getpid(), "uid": os.getuid(), "architecture": platform.machine(),
        "os": platform.freedesktop_os_release(), "python": platform.python_version(),
        "temms_path": str(module), "executable": sys.executable,
        "versions": {name: version(name) for name in ("temms", "onnxruntime", "numpy", "Pillow")},
        "provider": "CPUExecutionProvider", "threads": 1,
        "manifest": manifest, "workload": data, "profile": profile, "network": network,
        "filesystem": filesystem,
        "policy": {"economy": ECONOMY, "detail": DETAIL,
                   "source": "explicit application requests; no automatic resource monitor"},
        "score_tolerance": run.SCORE_TOLERANCE,
        "references": [{"model": {"id": model, "digest": digest}, "image": sample["image"],
                        "scores": scores[0].tolist()}
                       for sample in samples
                       for (model, digest), scores in sample["references"].items()],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    profile = resources.snapshot()
    resources.validate_profile(profile)
    network = resources.network_probe()
    filesystem = {name: bool(os.statvfs(path).f_flag & os.ST_RDONLY)
                  for name, path in (("root_readonly", "/"), ("assets_readonly", args.assets))}
    if not all(filesystem.values()):
        raise RuntimeError("qualification requires read-only root and original assets")
    manifest = assets.verify_assets(args.assets)
    data = workload.verify_workload(args.assets)
    shutil.copyfile(args.assets / "labels.txt", args.output / "labels.txt")
    if len(data["files"]) != 20:
        raise ValueError("qualification requires the pinned twenty-image workload")
    labels = [line.split(" ", 1)[1]
              for line in (args.assets / "labels.txt").read_text().splitlines()]
    if len(labels) != 1000:
        raise ValueError("qualification requires exactly 1000 labels")
    digests = {item["name"]: item["sha256"] for item in manifest["files"]}
    refs = [ModelRef(name, "sha256:" + digests[name + ".onnx"])
            for name in ("mobilenetv2", "resnet18")]
    # Direct references warm native code and the filesystem cache, never runtime active state.
    samples = run.prepare_inputs(args.assets, data, refs, labels)
    with (args.output / "events.jsonl").open("x") as log:
        def emit(record):
            log.write(json.dumps(record, allow_nan=False) + "\n")
            log.flush()
            if record["event"] == "activation":
                active = record["active"]
                print(f"{record['phase']:20} {record['activation']:10} "
                      f"active={active['id'] if active else 'none'}", flush=True)

        emit(header(manifest, data, samples, profile, network, filesystem))
        with tempfile.TemporaryDirectory(prefix="temms-edge-") as private:
            large = Path(private) / "resnet18.onnx"
            shutil.copyfile(args.assets / "resnet18.onnx", large)
            runtime = OnnxRuntime(models={"vision": [
                OnnxModel(refs[0], args.assets / "mobilenetv2.onnx",
                          ("CPUExecutionProvider",), samples[0]["feed"]),
                OnnxModel(refs[1], large, ("CPUExecutionProvider",), samples[0]["feed"]),
            ]}, session_factory=run.cpu_session)
            measured = asyncio.run(exercise(runtime, policies(refs), samples, labels,
                                            large, args.assets / "resnet18.onnx", emit))
        assets.verify_assets(args.assets)
        workload.verify_workload(args.assets)
        final = resources.snapshot()
        resources.validate_profile(final)
        summary = {"event": "summary", "complete": True, "served": len(measured),
                   "abstained": 1, "swaps": SWAPS, "profile": final,
                   "inference_ms": compare.distribution(measured), "assets_unchanged": True}
        emit(summary)
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
