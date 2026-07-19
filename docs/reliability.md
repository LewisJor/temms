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

## Crash atomicity (`SIGKILL` mid-write)

The soak above restarts *gracefully*. The DDIL question is harsher: power is cut,
or the supervisor kills the process, **in the middle of writing state**. Nothing
runs — no `finally`, no signal handler, no flush. Whatever is on disk is what the
device wakes up to.

`scripts/crash_soak.py` spawns a real subprocess that hammers the production
write paths (deployment state, the signed intent queue, the decision chain, the
trust store), sends it `SIGKILL` at a random offset, and verifies what survived:

| Check | Meaning |
|---|---|
| `deployment_state_parses` | No truncated or half-written state file; the state is a legal value. |
| `intent_queue_parses` | The queue is a list of well-formed entries — no partial record. |
| `trust_store_parses` | Provisioned trust survives the kill (a device must not wake up trusting nothing). |
| `decision_chain_intact` | The hash-linked chain still verifies end to end. |
| `deterministic_recovery` | The slot returns to the last **committed** decision, never a partially applied one. |

This works because every state file is written through
`temms.core.atomic.write_json_atomic`: same-directory temp file → `fsync` →
atomic `rename` → `fsync` of the parent directory. A reader therefore sees either
the old contents or the new ones, never a mix.

Verification reads the files **straight from disk** rather than through the store
accessors, because `DeploymentStateStore._read()` deliberately swallows a corrupt
file and reports `PENDING` — which would mask exactly the corruption being
hunted.

### Proving the harness actually detects

A check that never fires proves nothing, so the harness ships with a
falsification mode. `TEMMS_CRASH_SOAK_UNSAFE_WRITES=1` swaps the atomic writer
for one that writes in two flushed chunks with a pause between them:

```bash
# Expected: PASS, 0 cycles leave inconsistent state
uv run python scripts/crash_soak.py --iterations 40

# Expected: FAIL — this is the point
TEMMS_CRASH_SOAK_UNSAFE_WRITES=1 uv run python scripts/crash_soak.py --iterations 8
```

Under the unsafe flag the harness reports corrupt JSON in the majority of cycles
(`Expecting property name enclosed in double quotes`), confirming it would catch
a real atomicity regression rather than passing vacuously.

`make crash-soak-selftest` inverts the exit code: it **fails** if the harness
passes despite torn writes, since a detector that has stopped detecting is worse
than a failing soak. CI runs `tests/integration/test_crash_atomicity.py`, which
asserts both directions.

### Crash-atomicity baseline

40 `SIGKILL` cycles on a single Mac (Apple Silicon). Machine-readable copy:
[`crash-atomicity-report.json`](crash-atomicity-report.json).

| Metric | Value |
|---|---|
| Result | **PASS** |
| Kill cycles | 40 |
| Kills landing on a live worker | **40 / 40** |
| Cycles leaving inconsistent state | **0** |

The second row matters: a kill that arrives after the worker already died proves
nothing about atomicity, so `kills_landed_mid_write` is asserted as an invariant
in its own right.

### A note on scope

The issue proposed driving a full `temms daemon` subprocess. A real daemon takes
longer to start than to kill, so a run buys only a handful of kills — while the
property under test is atomicity of the *state writes*, not daemon startup.
Driving the same store classes directly buys hundreds of kills in the same
wall-clock budget, which is what actually catches a torn write. The write paths
exercised are the production ones, unmodified.

## Known follow-ups

- **Multi-hour run.** The committed baseline is a short run; a 24–72 h soak is an
  operator/nightly activity whose report supersedes the baseline.
- **Mid-package-import kills.** Crash atomicity currently covers state, queue,
  chain, and trust store. Package import writes into the model cache and is the
  remaining path to cover.
