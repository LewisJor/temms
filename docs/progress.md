# TEMMS progress — updated 2026-10-22

TEMMS remains an alpha Python library for application-owned local inference.
This pass starts at `4acb3d9d` and follows the completed software audit below.
It checks a fresh clone and the old-request failure interleaving previously left
as an exercise. The runtime implementation is unchanged.
Physical-device testing is deferred at the owner's request.

## Inference failure after publication

**Hypothesis:** an admitted old-model request can fail after a replacement publishes
without undoing that publication, retrying the request or hiding its original error.
The existing controlled swap test now covers both success and failure through one
TEMMS owner. Events establish worker admission, replacement publication and successful
replacement service before releasing the old request. Its original exception must
reach the caller; a second replacement request must still succeed afterward.

The unchanged implementation passes both cases. Three isolated deliberate regressions
are rejected at the intended assertions: restoring the old model, retrying on the
replacement before rethrowing, and swallowing the error. Exact invocation and
construction logs detect extra work; bounded waits and finally cleanup drain the
worker. This is injected-session contract coverage, separate from the existing
real-ONNX numerical pinning test, and makes no native-crash or hard-cancellation claim.

The full suite increased from **314 to 315 passing tests, zero skips**. Ruff,
strict mypy and strict Sphinx passed. The extended test adds **40 net support lines**; core, adapters,
public APIs and dependencies did not grow. Mutation controls, failure logs, source/test
hashes and full-suite XML are retained in `.demo-assets/inflight-failure-20261022/`.

## Fresh-clone trial — October 22

A new HTTPS clone of `4acb3d9d05c450478926a29390dd3f9dc41bc42a` and a fresh
CPython 3.11.8 environment on macOS ARM64 passed the documented `pip install .`,
tiny example, README quickstart and no-active-model example. All nine installed
modules matched source hashes, installation was non-editable, and NumPy, ONNX,
ONNX Runtime and Pillow were absent from the core-only environment.

The documented `pip install '.[onnx,demo]'` resolved ONNX Runtime **1.31.0**,
NumPy **2.4.6** and Pillow **13.0.0** without version overrides. Empty-cache
preparation verified four shared assets and twenty bundled input images.
One default replay then ran with macOS network denial; its launcher observed
TCP `EPERM` before executing the demo in the same sandbox. It offered **84 requests**:
**69 served, 3 waiting and 12 abstained**. All seven scenes passed, including
eleven successful requests after replacement failure. Independent record checks
confirmed twenty-input coverage, **35/40** served model/input pairs and zero
full-score or model-attribution mismatches. Sources and all 24 assets stayed unchanged.

No preparation or replay retry was needed. Pip reported an unwritable cache, and
the separate native-import probe warned that its telemetry identifier could not
be persisted; installation and inference still succeeded. Replay stderr was empty.
This validates the documented Mac installation and prepared offline execution,
not performance, independent engine correctness, Linux resource limits or physical
hardware. The library source matches this checkout; only tests and documentation
changed afterward. Commands, dependency versions, hashes, network evidence and
raw records are retained in `.demo-assets/fresh-clone-20261022/`.

## Current software milestone status

The original available-CPU milestones are complete within their declared scope:

| Milestone | Evidence rechecked on October 22 |
| --- | --- |
| A: trustworthy runtime contracts | Current source and bounded tests cover selection, runtime ownership, serialization, failed load/warmup, cancellation, recovery and admitted-model output attribution. Custom-runtime and native-thread boundaries remain explicit. |
| B: one real local workload | Current runtime, executable examples and manifests match the retained `f28365ad` Ubuntu evidence. An independent audit recomputed all 69 full-score/admission checks, twenty-input coverage, seven scenes, eleven post-failure continuity witnesses and twelve abstentions. Namespace evidence establishes offline execution. |
| C: measured value and clean integration | An independent audit recalculated 33 retained strategy runs / 4,620 requests, confirming matched protocols, outcome counts, latency, throughput and summaries. The separate 7,680-call dispatch audit and installed-package trial retain their historical scope. |

The pre-change baseline passed all five workflows at `4acb3d9d`:
[CI](https://github.com/LewisJor/temms/actions/runs/39789224968),
[installed core](https://github.com/LewisJor/temms/actions/runs/39789224820),
[documentation](https://github.com/LewisJor/temms/actions/runs/39789224814),
[native x86/ARM qualification](https://github.com/LewisJor/temms/actions/runs/39789224837)
and [Ubuntu replay](https://github.com/LewisJor/temms/actions/runs/39789224858).
The native jobs completed installed-source verification, the no-skip test gate,
two fresh constrained workers and record verification on each architecture.

The graphical simulator implementation and workflow are removed. That audit
rechecked records and source identity without rerunning benchmarks or inference.
Completion means the requested software demonstration and qualification are
established; it does not establish unique novelty, general superiority or production readiness.

## Recent repairs and assurance

- **Dependency guard, `aaf27f4f`:** hypothesis: the old denylist permitted undeclared
  core dependencies. Isolated mutations confirmed false passes for ordinary and
  `from` imports of `requests`, NumPy in public exports, and a deferred NumPy import.
  All four now fail the standard-library/TEMMS check. Valid local/standard-library
  controls pass; SQLite remains prohibited. The test is nine lines smaller.
  This is static coverage, not analysis of dynamic imports.
- **Native compatibility, `a626ad14`:** the `20a0bd36` baseline admitted ORT 1.18.0
  with NumPy 2.4.2. A fresh Mac installation and `pip check` passed, but native import
  failed with `_ARRAY_API not found`; no inference ran in that broken environment.
  The corrected **ORT >=1.19** floor rejects that pair. ORT 1.19.0 passed native import
  and three integration cases in each of two fresh Mac environments, using NumPy
  1.26.0/2.4.2 and ONNX 1.21.0. These are compatibility checks, not timing studies.
  [Ubuntu CI](https://github.com/LewisJor/temms/actions/runs/39507934190) then passed
  Python 3.11/3.12 × NumPy 1.26.0/2.4.2 with ORT 1.19.0 and ONNX 1.16.0:
  twelve native cases, zero skips, with independently audited source/wheel hashes.
  See [compatibility scope](../CONTRIBUTING.md#development-dependency-compatibility).
- **Bounded adapter storage, `a8230850`:** removing six lines and replacing retained
  per-slot state locks with one brief lock eliminated growth on unknown-slot reads.
  Construction, warmup and inference remain outside it. This was not a measured speedup.
- **Offline input preparation, `a0c8beca`:** at `8caf55ac`,
  [native](https://github.com/LewisJor/temms/actions/runs/39492290202) and
  [replay](https://github.com/LewisJor/temms/actions/runs/39492290128) qualification
  failed before inference on dataset-viewer HTTP 502 responses. The unchanged twenty
  pinned images now ship as 654,484 bytes of [attributed fixtures](../examples/live_swap/inputs/README.md).
  Fresh offline preparation and corrupt-source handling passed, followed by
  [native qualification](https://github.com/LewisJor/temms/actions/runs/39496958193).

The full local suite passed **314 tests, zero skips**, before and after the dependency
guard change; Ruff, strict mypy and strict Sphinx passed. Core remains **429 physical
lines** across five modules, largest 178; exports 50, adapters **223**, tiny example
37, and required runtime dependencies **zero**. The corrected wheel measured
**20,210 bytes at `a626ad14`**, including metadata/license; it was not rebuilt for
this documentation audit. The dependency check adds 84 workflow lines and no runtime service.

## Retained runtime and performance evidence

The independently audited implementation at `f28365ad` passed
[native qualification](https://github.com/LewisJor/temms/actions/runs/39502793672)
and [Ubuntu replay](https://github.com/LewisJor/temms/actions/runs/39502793645).
Both architectures ran 314 installed-wheel tests without skips. Four fresh workers
checked **452 served score vectors and model identities**, forty model/image pairs
per worker, with zero OOM events. Linux enforced one CPU quota, 512 MiB, no swap,
no external network, non-root execution and read-only assets.

| Architecture | Served across two workers | Peak cgroup memory, workers 1 / 2 |
| --- | ---: | ---: |
| x86_64 | 226 | 315.84 / 332.61 MiB |
| ARM64 | 226 | 410.04 / 324.99 MiB |

All workers completed corrupt-artifact recovery, twelve alternating swaps and
missing-context abstention/resume; CPU helpers and throttling were observed.
These are functional qualifications, not controlled memory comparisons or cold-device runs.

The [October 12 Mac comparison](../examples/live_swap/COMPARISON.md) found **102/140
timely correct responses per run** with background adaptation versus 101 with
fixed-small and 100 with blocking swaps. Background's maximum process RSS was
271.1 MiB versus fixed-small's 121.0 MiB. The advantage over fixed-small rests on
one distinct image across repetitions. Direct ONNX controls also reproduced memory growth;
neither wrapper collection nor these runs establishes leak-freedom.

The [October 20 Ubuntu dispatch study](../examples/live_swap/COMPARISON.md#four-path-dispatch-result--2026-10-20),
source `d0e2f324`, checked four fresh processes, 1,920 balanced blocks and **7,680
timed calls**. No layer met its predeclared optimization threshold consistently
for either model. Dispatch was retained; there is no zero-overhead or general speedup claim.

Other rejected changes remain part of the record: disabling the CPU arena reduced
peaks but failed the predeclared final-RSS criterion, with mixed latency effects.
Replacing TEMMS's fixed-slot lock cache with weak values released unknown-slot names
but created 100 locks for
100 sequential calls instead of one; no measured benefit justified that change.

Raw records, frozen sources and independent audits remain locally in:

| Evidence | Retained location |
| --- | --- |
| Guard mutations and 314-test XML | `.demo-assets/architecture-20261021/` |
| Broken/fixed native dependency installations and Ubuntu artifacts | `.demo-assets/compatibility-20261020/` |
| Native containers and offline replay | `.demo-assets/edge/ci-39502793672/`, `.demo-assets/edge/cpu-39502793645/` |
| Held-out comparison and dispatch | `.demo-assets/live-swap/validation-20261012/`, `.demo-assets/live-swap/dispatch-20261020/` |
| Clean-install and disconnected-install trial | `.demo-assets/live-swap/integration-trial-20261013/` |

Historical local evidence is not included in a clone. Published workflow artifacts
and the maintained runbooks provide a reproduction path; preserve accepted artifacts
before GitHub retention expires.

## Limits and handoff

Prepared inference works offline. Model acquisition, dependency/image builds and
the optional 140-image workload's upstream metadata require connectivity beforehand.
Interrupted acquisition restarts the incomplete file; it has no byte-range resume.
Scripted context is application policy, not automatic pressure detection. Direct
references share preprocessing and ONNX Runtime with serving, so they test integration
consistency rather than independent engine correctness. Whole-process/container
memory includes session overlap without isolating each allocation source.

One owner/event loop, caller-owned input lifetime, cooperative runtime behavior
and native-worker completion remain required. Recoverable artifact rejection does
not prove hard-OOM survival, native termination, power savings or real-time deadlines.
See the [architecture](architecture.md) and [release criteria](../CONTRIBUTING.md#release-and-support-criteria).

The reference device remains **Pi 5, 4 GB, 64-bit Ubuntu Server 24.04 LTS**.
Physical qualification is deferred until hardware is available. Hosted ARM64 does
not establish board latency, thermal behavior, battery life or sensor integration.

**Exactly one next action:** choose one application-specific recorded input/context
trace and declare its success criterion before changing policy or adding models.

Read `OnnxRuntime.infer` in `src/temms/adapters/onnx.py` and its controlled interleavings
in `tests/test_onnx_runtime.py`. The hardest invariant is that an admitted request
retains its captured model across replacement. The previously suggested failure
exercise is now maintained coverage: the old request can fail after publication
while the replacement remains active and serves subsequent requests.
