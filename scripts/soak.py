#!/usr/bin/env python3
"""TEMMS soak & chaos harness (issue #13).

Drives the daemon's decision + swap + inference path under continuous condition
churn and injected faults, then emits a machine-readable reliability report.

Everything runs in-process on a single machine with the simulation runtime
(`TEMMS_INFERENCE_SIMULATE_RUNTIME=1`), so it validates on a Mac with no
hardware, no GPU, and no external services (see docs/direction.md).

Faults exercised here:
- flapping sensor across a policy threshold (hysteresis must hold);
- garbage, contradictory, and stale condition values (must not crash or thrash);
- clock jumps into the dwell timer (must stay bounded);
- simulated process restart (persisted state must recover the last active model).

The `kill -9` mid-write variants from #13 need a subprocess daemon and are a
documented follow-up; the restart scenario here covers state/evidence recovery
via the persisted `active_model_id` anchor.

Usage:
    python scripts/soak.py --short                     # ~fast CI mode, gates
    python scripts/soak.py --duration 3600 --report docs/reliability-report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temms.conditions.store import ConditionStore
from temms.controller import AdaptiveInferenceController
from temms.core.cache import ModelCache, ModelFormat
from temms.core.storage import ModelStorage
from temms.inference.runtime import InferenceRuntime
from temms.policy.engine import PolicyEngine
from temms.policy.schema import (
    Condition,
    ConditionGroup,
    PolicyAction,
    PolicyRule,
    SlotPolicy,
    SlotPolicyMetadata,
    SlotPolicySpec,
)
from temms.slots.manager import SlotManager, SlotState

VISIBILITY = "environmental.atmospheric.visibility_m"
FOG_THRESHOLD = 100
INPUT = b"\x00" * (48 * 4)  # 48 float32 = input_shape (1, 3, 4, 4)
MIN_DWELL_S = 5.0


class MonotonicClock:
    """Injectable clock that advances with real time but can be jumped."""

    def __init__(self) -> None:
        self._base = time.monotonic()
        self._offset = 0.0

    def __call__(self) -> float:
        return (time.monotonic() - self._base) + self._offset

    def jump(self, seconds: float) -> None:
        self._offset += seconds


@dataclass
class Metrics:
    iterations: int = 0
    swaps: int = 0
    inferences: int = 0
    inference_errors: int = 0
    decisions: int = 0
    swap_latencies_ms: list[float] = field(default_factory=list)
    faults: dict[str, int] = field(default_factory=lambda: {
        "flap": 0,
        "garbage_sensor": 0,
        "contradictory_sensor": 0,
        "stale_sensor": 0,
        "clock_jump": 0,
        "restart": 0,
    })
    recovery_ok: int = 0
    recovery_fail: int = 0
    errors: list[str] = field(default_factory=list)


def _model_metadata() -> dict[str, Any]:
    return {
        "input_shape": [1, 3, 4, 4],
        "runtime_constraints": {
            "device_profiles": ["x86_64-cpu"],
            "runtimes": ["onnxruntime"],
        },
    }


def build_system(root: Path) -> dict[str, Any]:
    """Construct an in-process daemon system with two models and a fog policy."""
    db_path = root / "temms.db"
    model_dir = root / "models"
    model_dir.mkdir(parents=True, exist_ok=True)

    model_cache = ModelCache(db_path)
    model_storage = ModelStorage(model_dir)
    slot_manager = SlotManager(db_path)
    condition_store = ConditionStore(db_path)
    clock = MonotonicClock()
    policy_engine = PolicyEngine(condition_store, time_fn=clock)

    for model_id, name in (("daylight-v1", "daylight"), ("lowlight-v1", "lowlight")):
        model_file = root / f"{model_id}.onnx"
        model_file.write_bytes(b"onnx-" + model_id.encode())
        dest, sha, size = model_storage.store_model(model_file, model_id)
        model_cache.add_cached_model(
            model_id=model_id,
            name=name,
            version="1",
            format=ModelFormat.ONNX,
            path=dest,
            sha256=sha,
            size_bytes=size,
            package_id="pkg-soak",
            metadata=_model_metadata(),
        )

    slot_manager.create_slot("vision", "Vision", required=True, default_model="daylight")
    policy_engine.load_policy(
        SlotPolicy(
            metadata=SlotPolicyMetadata(name="fog-policy"),
            spec=SlotPolicySpec(
                slot="vision",
                default_model="daylight",
                fallback_chain=["daylight"],
                rules=[
                    PolicyRule(
                        name="fog",
                        priority=100,
                        min_dwell_s=MIN_DWELL_S,
                        conditions=ConditionGroup(
                            all=[Condition(metric=VISIBILITY, operator="lt", value=FOG_THRESHOLD)]
                        ),
                        action=PolicyAction(switch_to="lowlight"),
                    )
                ],
            ),
        )
    )

    runtime = InferenceRuntime(model_cache, model_storage, max_workers=8)
    controller = AdaptiveInferenceController(
        slot_manager, condition_store, policy_engine, model_cache, runtime
    )
    return {
        "model_cache": model_cache,
        "model_storage": model_storage,
        "slot_manager": slot_manager,
        "condition_store": condition_store,
        "policy_engine": policy_engine,
        "clock": clock,
        "runtime": runtime,
        "controller": controller,
    }


async def _inference_worker(system: dict[str, Any], metrics: Metrics, stop: asyncio.Event) -> None:
    """Continuously fire inference; every request must be served or the slot is
    legitimately not running — never a silent error against a loaded model."""
    runtime: InferenceRuntime = system["runtime"]
    slot_manager: SlotManager = system["slot_manager"]
    while not stop.is_set():
        slot = slot_manager.get_slot("vision")
        if slot is None or slot.state != SlotState.RUNNING or slot.active_model_id is None:
            await asyncio.sleep(0.001)
            continue
        try:
            await runtime.infer("vision", slot.active_model_id, INPUT, "application/octet-stream")
            metrics.inferences += 1
        except Exception as exc:  # a loaded slot must never error a request
            metrics.inference_errors += 1
            metrics.errors.append(f"inference error: {exc}")
        await asyncio.sleep(0)


def _inject_sensor_chaos(system: dict[str, Any], metrics: Metrics, i: int) -> None:
    """Mutate the visibility condition with a mix of flap and bad values."""
    store: ConditionStore = system["condition_store"]
    roll = random.random()
    if roll < 0.15:  # garbage value
        store.set(VISIBILITY, "not-a-number", "sensor", 100)
        metrics.faults["garbage_sensor"] += 1
    elif roll < 0.25:  # contradictory: two sources disagree same tick
        store.set(VISIBILITY, 40, "sensor-a", 100)
        store.set(VISIBILITY, 900, "sensor-b", 100)
        metrics.faults["contradictory_sensor"] += 1
    elif roll < 0.32:  # stale: low-priority write that should be ignored
        store.set(VISIBILITY, 10, "stale", 1)
        metrics.faults["stale_sensor"] += 1
    else:  # flap across the fog threshold
        store.set(VISIBILITY, 40 if i % 2 == 0 else 900, "sensor", 100)
        metrics.faults["flap"] += 1


async def _simulate_restart(system: dict[str, Any], metrics: Metrics) -> dict[str, Any]:
    """Rebuild the runtime against the persisted DBs (fresh process, empty
    runtime) and restore the last fully-activated model — the recovery anchor."""
    slot_manager: SlotManager = system["slot_manager"]
    before = slot_manager.get_slot("vision")
    expected = before.active_model_id if before else None

    new_runtime = InferenceRuntime(system["model_cache"], system["model_storage"], max_workers=8)
    system["runtime"] = new_runtime
    system["controller"] = AdaptiveInferenceController(
        slot_manager,
        system["condition_store"],
        system["policy_engine"],
        system["model_cache"],
        new_runtime,
    )
    metrics.faults["restart"] += 1

    if expected is None:
        metrics.recovery_ok += 1
        return system
    try:
        await new_runtime.load_model("vision", expected)
        restored = new_runtime.get_slot_info("vision").get("model_id")
        if restored == expected:
            metrics.recovery_ok += 1
        else:
            metrics.recovery_fail += 1
            metrics.errors.append(f"recovery mismatch: expected {expected}, got {restored}")
    except Exception as exc:
        metrics.recovery_fail += 1
        metrics.errors.append(f"recovery load failed: {exc}")
    return system


async def run_soak(duration_s: float, seed: int = 1234) -> dict[str, Any]:
    random.seed(seed)
    metrics = Metrics()
    with tempfile.TemporaryDirectory(prefix="temms-soak-") as tmp:
        system = build_system(Path(tmp))
        # Cold start on the default model.
        await system["runtime"].load_model("vision", "daylight-v1")
        system["slot_manager"].activate_model("vision", "daylight-v1", "startup", "default")

        stop = asyncio.Event()
        workers = [
            asyncio.create_task(_inference_worker(system, metrics, stop))
            for _ in range(4)
        ]

        deadline = time.monotonic() + duration_s
        i = 0
        try:
            while time.monotonic() < deadline:
                i += 1
                metrics.iterations = i
                _inject_sensor_chaos(system, metrics, i)

                if i % 37 == 0:  # occasional clock jump into the dwell timer
                    system["clock"].jump(random.choice([-30.0, 30.0, 120.0]))
                    metrics.faults["clock_jump"] += 1

                # Drive one decision + swap, timing the activation.
                controller = system["controller"]
                start = time.monotonic()
                decision = await controller.evaluate_slot("vision", apply=True)
                metrics.decisions += 1
                if decision.applied and decision.activated_model:
                    metrics.swaps += 1
                    metrics.swap_latencies_ms.append((time.monotonic() - start) * 1000.0)

                if i % 200 == 0:  # periodic restart-recovery drill
                    system = await _simulate_restart(system, metrics)

                await asyncio.sleep(0.002)
        finally:
            stop.set()
            await asyncio.gather(*workers, return_exceptions=True)
            system["runtime"].shutdown()

    return _build_report(metrics, duration_s, seed)


def _build_report(metrics: Metrics, duration_s: float, seed: int) -> dict[str, Any]:
    lat = sorted(metrics.swap_latencies_ms)

    def pct(p: float) -> float:
        if not lat:
            return 0.0
        return round(lat[min(len(lat) - 1, int(p / 100 * len(lat)))], 3)

    swaps_per_min = metrics.swaps / (duration_s / 60.0) if duration_s else 0.0
    invariants = [
        {
            "name": "zero_silent_inference_errors",
            "passed": metrics.inference_errors == 0,
            "detail": f"{metrics.inference_errors} errors over {metrics.inferences} inferences",
        },
        {
            "name": "bounded_swap_frequency",
            # Hysteresis (min_dwell_s) must keep a flapping sensor from thrashing.
            "passed": swaps_per_min <= 60.0,
            "detail": f"{swaps_per_min:.1f} swaps/min (dwell={MIN_DWELL_S}s)",
        },
        {
            "name": "clean_recovery_on_restart",
            "passed": metrics.recovery_fail == 0,
            "detail": f"{metrics.recovery_ok} ok / {metrics.recovery_fail} failed",
        },
        {
            "name": "no_harness_errors",
            "passed": not metrics.errors,
            "detail": f"{len(metrics.errors)} errors",
        },
    ]
    return {
        "schema_version": "temms-reliability-report/v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "config": {"duration_s": duration_s, "seed": seed, "min_dwell_s": MIN_DWELL_S},
        "totals": {
            "iterations": metrics.iterations,
            "swaps": metrics.swaps,
            "inferences": metrics.inferences,
            "inference_errors": metrics.inference_errors,
            "decisions": metrics.decisions,
        },
        "swap_latency_ms": {
            "count": len(lat),
            "p50": pct(50),
            "p95": pct(95),
            "max": round(lat[-1], 3) if lat else 0.0,
        },
        "faults_injected": metrics.faults,
        "recovery": {"ok": metrics.recovery_ok, "failed": metrics.recovery_fail},
        "invariants": invariants,
        "errors": metrics.errors[:20],
        "passed": all(inv["passed"] for inv in invariants),
    }


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    t = report["totals"]
    lat = report["swap_latency_ms"]
    lines = [
        "# TEMMS Reliability Report",
        "",
        f"_Generated {report['generated_at']} · schema `{report['schema_version']}`_",
        "",
        f"**Result: {'PASS ✅' if report['passed'] else 'FAIL ❌'}** "
        f"({report['config']['duration_s']:.0f}s, seed {report['config']['seed']})",
        "",
        "## Totals",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Iterations | {t['iterations']} |",
        f"| Model swaps | {t['swaps']} |",
        f"| Inferences served | {t['inferences']} |",
        f"| Inference errors | {t['inference_errors']} |",
        f"| Decisions | {t['decisions']} |",
        f"| Swap latency p50 / p95 / max (ms) | {lat['p50']} / {lat['p95']} / {lat['max']} |",
        "",
        "## Faults injected",
        "",
        "| Fault | Count |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in report["faults_injected"].items()],
        "",
        "## Invariants",
        "",
        "| Invariant | Result | Detail |",
        "|---|---|---|",
        *[
            f"| {inv['name']} | {'PASS' if inv['passed'] else 'FAIL'} | {inv['detail']} |"
            for inv in report["invariants"]
        ],
        "",
    ]
    if report["errors"]:
        lines += ["## Errors", "", *[f"- {e}" for e in report["errors"]], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="TEMMS soak & chaos harness")
    parser.add_argument("--duration", type=float, default=30.0, help="Run seconds")
    parser.add_argument("--short", action="store_true", help="Fast CI mode (~10s)")
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--report", type=Path, help="Write JSON report here")
    parser.add_argument("--markdown", type=Path, help="Write Markdown report here")
    args = parser.parse_args()

    # The harness drives the simulation runtime so it runs anywhere with no
    # real model files, GPU, or services. Callers (e.g. tests) that invoke
    # run_soak directly must set this themselves.
    os.environ["TEMMS_INFERENCE_SIMULATE_RUNTIME"] = "1"

    duration = 10.0 if args.short else args.duration
    report = asyncio.run(run_soak(duration, seed=args.seed))

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        _write_markdown(report, args.markdown)

    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"\nSoak {'PASSED' if report['passed'] else 'FAILED'}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
