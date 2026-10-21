# TEMMS progress — updated 2026-10-21

TEMMS remains an alpha Python library for application-owned local inference.
This pass starts at `88083b1a`, where all five hosted workflows passed. A focused
review of first-install and offline preparation found no reproducible defect.

## Dependency boundary regression guard

**Hypothesis:** the architecture test's dependency denylist permits undeclared
third-party imports, weakening the zero-dependency boundary. Acceptance requires
rejecting those imports without rejecting standard-library or local imports and
without changing library behavior.

An isolated source-mutation probe confirmed four false passes: a `requests` import,
a `from requests.sessions` import, a NumPy import in public exports, and a deferred
NumPy import inside a function. The guard now checks all direct core files,
including `__init__.py`, against Python's standard-library module names and TEMMS.
All four mutations fail. Unchanged source, a standard-library import and a relative
local import pass; the existing SQLite prohibition remains enforced.

The test is **nine lines smaller**. Core, adapters, public APIs and distribution
metadata are unchanged. Before/after results and the standalone probe are retained
in `.demo-assets/architecture-20261021/`. This is preventive static coverage;
dynamic imports still require review and installed-package checks.

The full suite passed **314 tests with zero skips** before and after the change.
Ruff and strict mypy passed. The strict documentation build passed. No performance
measurement was needed for this test-only change; prior measurements below retain
their original revisions and scope.

## Previous pass: optional native dependency compatibility

The October 20 pass started at `20a0bd36` and reproduced a dependency-resolution
defect. Its evidence and shipped correction follow.

**Hypothesis:** the independently declared floors `onnxruntime>=1.16` and
`numpy>=1.26` permit an installation that cannot import the native runtime. Acceptance
requires reproducing that failure, rejecting the broken combination through package
metadata, and checking the corrected runtime floor against both NumPy 1.x and 2.x.

The baseline wheel was built from archived `20a0bd36`. On macOS 26.6.2 ARM64 /
Python 3.11.8, a fresh environment with **ONNX Runtime 1.18.0 and NumPy 2.4.2**
installed successfully and passed `pip check`. Its explicit native import exited 1
with `_ARRAY_API not found` and `ImportError`. No inference was attempted in that
broken environment. This is an observed binary-compatibility failure, not a missing
wheel or an inferred failure from a version number.

The optional runtime minimum is now **1.19**, whose
[upstream release added NumPy 2 support](https://github.com/microsoft/onnxruntime/releases/tag/v1.19.0).
NumPy's 1.26 floor remains unchanged. Normal pip resolution of the corrected wheel
rejects the formerly allowed 1.18.0/2.4.2 combination with `ResolutionImpossible`
before installation. Two separate fresh environments using the unchanged baseline
library code passed native import and all three existing native integration tests:

| ONNX Runtime | NumPy | Import | Native tests |
| --- | --- | --- | --- |
| 1.19.0 | 1.26.0 | Passed, empty stderr | 3 passed, zero skips |
| 1.19.0 | 2.4.2 | Passed, empty stderr | 3 passed, zero skips |

Those local positives used ONNX 1.21.0, pytest 9.1.1 and pytest-asyncio 1.4.0.
All three environments were created independently and executed once. Commands,
resolver reports, versions, stdout/stderr, JUnit, source archive and wheel hashes
remain in `.demo-assets/compatibility-20261020/`. Local evidence is not an Ubuntu run.

The existing CI now adds four installed-wheel compatibility profiles on Ubuntu
24.04: Python 3.11/3.12, each with NumPy 1.26.0/2.4.2, ONNX Runtime 1.19.0 and
ONNX 1.16.0. Explicit imports, installed-source checks, built metadata and named
native-test/no-skip gates prevent a broken optional dependency from appearing as a
passing skipped suite. All four profiles passed at `a626ad14` in
[CI run 39507934190](https://github.com/LewisJor/temms/actions/runs/39507934190),
with three native tests per profile and zero skips.
An independent audit of the downloaded artifacts verified Ubuntu 24.04.5 x86_64,
Python 3.11.17/3.12.15, exact native dependency versions, all nine installed module
hashes against that revision, wheel contents/metadata and all twelve native cases.
Raw artifacts and the audit remain in
`.demo-assets/compatibility-20261020/ci-39507934190/`.
The existing current-dependency, packaging and deployment checks are preserved.
See [compatibility scope](../CONTRIBUTING.md#development-dependency-compatibility).

All five workflows passed at that revision: the linked CI,
[installed core](https://github.com/LewisJor/temms/actions/runs/39507934153),
[documentation](https://github.com/LewisJor/temms/actions/runs/39507934187),
[native qualification](https://github.com/LewisJor/temms/actions/runs/39507934148)
and [Ubuntu replay](https://github.com/LewisJor/temms/actions/runs/39507934260).

## Other review decisions

Retain the core's per-slot reconciliation lock cache for the fixed-slot target.
A controlled probe reproduced retention of 1,000 unavailable slot names. A weak-value
substitution released them and preserved tested holder/waiter/cancellation ordering,
but created 100 locks for 100 sequential fixed-slot calls instead of one. Fixed-slot
storage is already bounded; no speed benefit or real dynamic-slot consumer was
established. The retained probe is in the same evidence directory. No core lifecycle
mechanism or new public abstraction was added.

The independent adapter audit found no remaining demonstrated defect in load/warmup
failure recovery, cancellation before publication, session lifetime or borrowed
input ownership. Its focused suite passed 32 tests. Thread-pool saturation, native
termination and caller mutation remain documented application/runtime boundaries.

## Local verification and complexity

After the metadata change, **314 tests passed with zero skips**, Ruff passed on
`src tests examples`, strict mypy passed all ten configured files, and the strict
Sphinx documentation build passed. The wheel
build succeeds; the corrected local wheel is **20,210 bytes**, including metadata
and license. Core and adapter source did not change. Core remains **429 physical
lines** across five modules, largest 178; exports 50, adapters **223**, tiny example
37, and required runtime dependencies **zero**. The new support cost is 84 lines
for one compatibility job in the existing CI workflow; there is no new test framework.

## Existing runtime and performance evidence

The latest independently audited implementation at `f28365ad` passed
[CI](https://github.com/LewisJor/temms/actions/runs/39502793613),
[installed core](https://github.com/LewisJor/temms/actions/runs/39502793629),
[documentation](https://github.com/LewisJor/temms/actions/runs/39502793600),
[native qualification](https://github.com/LewisJor/temms/actions/runs/39502793672) and
[Ubuntu replay](https://github.com/LewisJor/temms/actions/runs/39502793645).
Both native architectures ran 314 installed-wheel tests without skips. Four fresh
workers checked **452 served score vectors and model identities**, forty model/image
pairs per worker, with zero OOM events. The enforced profile is one CPU quota,
512 MiB, no swap, no external network, non-root execution and read-only assets.

| Architecture | Served across two workers | Peak cgroup memory, workers 1 / 2 |
| --- | ---: | ---: |
| x86_64 | 226 | 315.84 / 332.61 MiB |
| ARM64 | 226 | 410.04 / 324.99 MiB |

All workers completed corrupt-artifact recovery, twelve alternating swaps and
missing-context abstention/resume; CPU helpers and throttling were observed. The
separate replay checked 69 served outputs, twenty inputs and all seven scenes.
Downloaded records and independent audits remain under
`.demo-assets/edge/ci-39502793672/` and `.demo-assets/edge/cpu-39502793645/`.
These are functional qualifications, not controlled before/after memory comparisons.

The [dispatch study](../examples/live_swap/COMPARISON.md#four-path-dispatch-result--2026-10-20)
at `d0e2f324` independently verified four fresh processes, 1,920 balanced blocks
and **7,680 timed calls**. No layer met its predeclared optimization threshold across
all four processes for either model. Inference dispatch was retained. The later
adapter storage fix at `a8230850` removed six lines and eliminated retained locks
for unknown-slot reads; that study is not a speed comparison for the fix.
Follow-up documentation review at `f6c8c0a7` corrected an outdated blanket statement
that all measurements were local Mac results. The October 12 studies remain Mac
evidence; the October 20 hosted Ubuntu study measures dispatch timing only.

On the separate 140-image task validation, background adaptation produced 102 timely
correct responses versus 101 for fixed-small, while using substantially more memory.
Direct ONNX controls also reproduced memory growth. The
[comparison and memory studies](../examples/live_swap/COMPARISON.md) establish no
general speed advantage, leak-freedom or unique hot-swapping capability.

## Earlier repair and remaining limits

The graphical simulator's workflow and support were removed. At `8caf55ac`,
[native qualification](https://github.com/LewisJor/temms/actions/runs/39492290202) and
[Ubuntu replay](https://github.com/LewisJor/temms/actions/runs/39492290128) failed before
inference on dataset-viewer HTTP 502 responses. The ordinary twenty unchanged,
pinned inputs now ship as 654,484 bytes of
[attributed example fixtures](../examples/live_swap/inputs/README.md). Fresh offline
preparation and corrupt-source handling passed, followed by
[native qualification at `a0c8beca`](https://github.com/LewisJor/temms/actions/runs/39496958193).
Those failures remain part of the record. The optional 140-image preparation still
uses upstream metadata; model acquisition and image builds require networking
before disconnected execution.

The qualification uses scripted application context and direct references sharing
preprocessing and ONNX Runtime with serving. It checks integration consistency,
not independent engine correctness or cold-device startup. Recoverable artifact
rejection is not hard-OOM survival, a deadline guarantee or a power measurement.

The mounted-device target is **Raspberry Pi 5 (4 GB), running 64-bit Ubuntu Server
24.04 LTS**. Its [setup guide](../examples/edge/README.md#set-up-the-reference-device)
uses the same profile. No physical Pi execution has been established. Hosted ARM64
evidence does not establish board latency, thermal behavior, battery life or sensor
integration. See the [release/support criteria](../CONTRIBUTING.md#release-and-support-criteria).

**Exactly one next action:** run the unchanged constrained qualification on that
physical Pi and retain its board identity, power/cooling configuration and complete
raw evidence before treating the mounted-device target as qualified.

Read `OnnxRuntime.infer` in `src/temms/adapters/onnx.py` and the controlled interleavings
in `tests/test_onnx_runtime.py`. The hardest invariant is that an admitted request
retains its captured model and attribution across replacement. Exercise without AI:
make the controlled old-model request fail after a new model publishes, and verify
that the new model still serves.
