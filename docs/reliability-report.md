# TEMMS Reliability Report

_Generated 2026-07-17T16:18:34.844056Z · schema `temms-reliability-report/v1`_

**Result: PASS ✅** (120s, seed 1234)

## Totals

| Metric | Value |
|---|---|
| Iterations | 49989 |
| Model swaps | 76 |
| Inferences served | 2125377 |
| Inference errors | 0 |
| Decisions | 49989 |
| Swap latency p50 / p95 / max (ms) | 2.42 / 3.85 / 4.581 |

## Faults injected

| Fault | Count |
|---|---|
| flap | 34117 |
| garbage_sensor | 7413 |
| contradictory_sensor | 4969 |
| stale_sensor | 3490 |
| clock_jump | 1351 |
| restart | 249 |

## Invariants

| Invariant | Result | Detail |
|---|---|---|
| zero_silent_inference_errors | PASS | 0 errors over 2125377 inferences |
| bounded_swap_frequency | PASS | 38.0 swaps/min (dwell=5.0s) |
| clean_recovery_on_restart | PASS | 249 ok / 0 failed |
| no_harness_errors | PASS | 0 errors |
