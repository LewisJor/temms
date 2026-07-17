# Reliability: Soak & Chaos Harness

The soak/chaos harness (`scripts/soak.py`) drives the daemon's decision → swap →
inference path under continuous condition churn and injected faults, then emits a
machine-readable **reliability report**. It is the artifact that makes a
real-hardware demo credible: evidence that the control loop behaves correctly
under sustained stress and fault injection.

Per [`direction.md`](direction.md), it runs **entirely in-process on a single
machine** with the simulation runtime — no hardware, GPU, or external services —
so it validates on a Mac before any edge board exists.

## What it exercises

Under a continuous loop of policy evaluations, model swaps, and concurrent
inference load, the harness injects:

- **Flapping sensor** across a policy threshold at high frequency — hysteresis
  (`min_dwell_s`) must prevent swap thrash.
- **Garbage** condition values (non-numeric), **contradictory** values (two
  sources disagreeing in the same tick), and **stale** low-priority writes — the
  engine must neither crash nor thrash.
- **Clock jumps** into the dwell timer — swap frequency must stay bounded.
- **Simulated process restart** — a fresh runtime against the persisted store
  must recover the last fully-activated model (the `active_model_id` anchor from
  the swap contract).

## Invariants asserted

| Invariant | Meaning |
|---|---|
| `zero_silent_inference_errors` | Every request against a loaded slot is served; a swap in progress never errors a request. |
| `bounded_swap_frequency` | A flapping sensor does not thrash swaps (hysteresis holds). |
| `clean_recovery_on_restart` | Every simulated restart recovers the last active model. |
| `no_harness_errors` | No unexpected exception during the run. |

The harness exits non-zero if any invariant fails, so it gates in CI.

## Running it

```bash
# Fast CI gate (~2s) — also run as part of `make soak-short`
uv run pytest tests/integration/test_soak_smoke.py -q

# Local soak with a committed report (default 120s; override with SOAK_DURATION)
make soak
SOAK_DURATION=600 make soak

# Direct, with full control
uv run python scripts/soak.py --duration 300 \
  --report docs/reliability-report.json --markdown docs/reliability-report.md
```

CI runs `make soak-short` (the 2-second gate). The longer `make soak` produces
the published baseline (below) and is suitable for a nightly/operator job.

## Published baseline

First published baseline — a 120 s single-Mac run (Apple Silicon, simulation
runtime). Machine-readable copy: [`reliability-report.json`](reliability-report.json).
Regenerate with `make soak`.

| Metric | Value |
|---|---|
| Result | **PASS** |
| Duration | 120 s |
| Iterations | 49,989 |
| Inferences served | 2,125,377 |
| Inference errors | **0** |
| Model swaps | 76 (**38/min** under 34,117 flap events) |
| Swap latency p50 / p95 / max | 2.42 / 3.85 / 4.58 ms |
| Faults injected | 7,413 garbage · 4,969 contradictory · 3,490 stale · 1,351 clock jumps |
| Restart-recovery drills | **249 / 249 recovered** |

All four invariants held. This is a short baseline; a multi-hour run supersedes
it (see below).

## Known follow-ups

- **`kill -9` mid-write variants.** The restart scenario here recovers from the
  persisted state anchor; killing a real subprocess daemon mid-swap /
  mid-evidence-write / mid-package-import (to test write atomicity and journaling
  under an abrupt kill) needs a subprocess harness and is the next increment.
- **Multi-hour run.** The committed baseline is a short run; a 24–72 h soak is an
  operator/nightly activity whose report supersedes the baseline.
