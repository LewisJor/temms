# TEMMS progress — updated 2026-10-20

TEMMS remains an alpha Python library for application-owned local inference. This
pass started at `fe6a079e`, whose 275-test baseline passed. Three bounded iterations
addressed measurement quality, input-buffer ownership and adapter storage. Core
policy, public protocols and application scheduling are unchanged.

## Kept changes and evidence

The [four-path dispatch study](../examples/live_swap/COMPARISON.md#four-path-dispatch-result--2026-10-20)
tested native execution, thread dispatch, the ONNX adapter and TEMMS on the same
warmed session. Its repetition count, balanced order and decision threshold were
fixed before collection. Source `d0e2f324` completed
[one Ubuntu run](https://github.com/LewisJor/temms/actions/runs/39501140290): four fresh
processes, 1,920 blocks and **7,680 checked timed calls**, with no retries. Independent
recomputation verified raw summaries, recorded output hashes, model identities,
network isolation and installed-source provenance. No layer met the predeclared
threshold across all four processes for either model. **Retain inference dispatch**;
no speedup or zero-overhead claim follows. Probe support grows by 85 lines and its
tests by 259 lines, outside the installed library. Raw records and the frozen source
remain under `.demo-assets/live-swap/dispatch-20261020/` and in the linked workflow.

The ownership characterization at `53d02fe5` adds 42 test lines and seven architecture
lines: inference snapshots its mapping but borrows mutable values, including after
caller cancellation. A controlled worker observes a later mutation to the original
buffer, while replacement of the caller's mapping entry does not change its input.
Applications must preserve buffers until native work finishes. No deep copy,
executor or new lifecycle API was introduced.

The storage fix at `a8230850` replaces the ONNX adapter's growing per-slot lock map
with one lock protecting only brief active-state capture/publication. Previously,
1,000 unknown-slot reads retained 1,000 locks. Weak-reference regressions fail on
that implementation and pass after the fix. Native construction, warmup and
inference remain outside the lock. Existing progress tests now also exercise a
separate calling thread; deliberately holding the lock across loading makes both
new cases fail at bounded timeouts with clean teardown. The fix removes **six
adapter lines** and adds a net 43 test lines. TEMMS reconciliation locks remain per
slot; this bounds adapter synchronization storage, not every library allocation.
The dispatch study predates this fix and is not its speed comparison.

## Verification and size

The final local suite passes **314 tests with zero skips** on macOS ARM64,
Python 3.11.8, ONNX Runtime 1.24.1, NumPy 2.4.2 and Pillow 12.1.0. The focused adapter,
lifetime, conformance and architecture checks pass 37 tests. Ruff passes on the
maintained `src tests examples` paths, and strict mypy passes all ten configured
files. The strict Sphinx documentation build passes. Source/wheel builds succeed
with the installed backend. A clean core-only
environment imports every library module and runs the maintained example from the
installed wheel without optional ML dependencies. Final hosted qualification of
the storage fix is pending; earlier green runs do not substitute for it.

Core remains **429 physical lines** across five modules, largest 178; exports 50,
adapters **223** and the tiny example 37. Required runtime dependencies remain zero.
The locally built library wheel is **20,048 bytes**, including metadata and license.
The experiment added no installed dependency, public abstraction or execution
framework. No constrained Docker workload ran on this development Mac.

## Previous qualification and preparation repair

At `8caf55ac`, [native architecture jobs](https://github.com/LewisJor/temms/actions/runs/39492290202)
and the [Ubuntu replay](https://github.com/LewisJor/temms/actions/runs/39492290128)
failed before inference: Hugging Face dataset-viewer metadata returned HTTP 502.
The ordinary twenty inputs now ship as **654,484 bytes** of pinned example JPEGs
with [attribution and CC BY-SA 4.0 terms](../examples/live_swap/inputs/README.md).
Their bytes and frozen workload manifest did not change. Fresh offline preparation,
corrupt/missing-source rejection, atomic destination preservation and prepared-cache
reuse passed. The source distribution includes them; the library wheel does not.
The optional 140-image validation preparation still depends on upstream metadata;
model acquisition and image builds still need networking before offline execution.

The same pass removed about 1,400 lines of graphical-simulator support. The maintained
path is the [headless Ubuntu qualification](../examples/edge/README.md). A separate
regression corrected falsey injected session factories: the adapter chooses its
default only when the supplied factory is `None`.

The repaired source `a0c8beca` passed the
[native matrix](https://github.com/LewisJor/temms/actions/runs/39496958193) and
[Ubuntu replay](https://github.com/LewisJor/temms/actions/runs/39496958438).
Both native architectures ran 275 installed-wheel tests without skips, including
all three real ONNX integration tests, and two fresh constrained containers each.
The fixed profile is one CPU quota, 512 MiB, no swap, no external network, non-root
execution and read-only original assets. No QEMU was used.

| Architecture | Served across two workers | Peak cgroup memory, workers 1 / 2 |
| --- | ---: | ---: |
| x86_64 | 226 | 348.77 / 373.07 MiB |
| ARM64 | 226 | 346.41 / 330.95 MiB |

All **452 served score vectors and identities matched** direct references; every
worker checked 20/20 images and 40/40 model/image pairs, with zero OOM events. All
completed corrupt-artifact recovery, twelve alternating swaps and missing-context
abstention/resume. Helpers consumed CPU and cgroup counters witnessed throttling.
The separate offline replay checked 69 served outputs, all twenty inputs and all
seven scenes. Downloaded records were independently reverified and remain under
`.demo-assets/edge/ci-39496958193/` and `.demo-assets/edge/cpu-39496958438/`.
The intervening failed runs remain part of the record.

All five push workflows also passed at `d0e2f324`, before the later storage fix:
[CI](https://github.com/LewisJor/temms/actions/runs/39500758344),
[installed core](https://github.com/LewisJor/temms/actions/runs/39500758338),
[documentation](https://github.com/LewisJor/temms/actions/runs/39500758367),
[native matrix](https://github.com/LewisJor/temms/actions/runs/39500758332) and
[Ubuntu replay](https://github.com/LewisJor/temms/actions/runs/39500758331).
Each revision's measurements stand separately.

## Limits and next action

The existing [task comparison and memory studies](../examples/live_swap/COMPARISON.md)
remain narrow measurements. Background adaptation produced 102 timely correct
responses versus 101 for fixed-small on the 140-image validation workload, while
using substantially more memory. Direct ONNX controls also reproduced memory growth.
No general speed advantage, leak-freedom or unique hot-swapping capability is established.

The constrained trace uses scripted application context. Its direct references
share preprocessing and ONNX Runtime with serving, checking integration consistency
rather than independent engine correctness. Reused inputs and warmed caches do not
measure cold-device startup. Whole-container memory includes native allocations,
helpers and charged cache. Recoverable artifact rejection is not hard-OOM survival,
a deadline guarantee or a power measurement.

The mounted-device target is **Raspberry Pi 5 (4 GB), running 64-bit Ubuntu Server
24.04 LTS**. Its [setup guide](../examples/edge/README.md#set-up-the-reference-device)
uses the same profile. No physical Pi execution has been established; hosted ARM64
evidence does not establish device latency, thermal behavior, battery life or sensor
integration. See the [release/support criteria](../CONTRIBUTING.md#release-and-support-criteria).

**Exactly one next action:** run the unchanged constrained qualification on that
physical Pi and retain its board identity, power/cooling configuration and complete
raw evidence before treating the mounted-device target as qualified.

Read `OnnxRuntime.infer` in `src/temms/adapters/onnx.py` and the controlled interleavings
in `tests/test_onnx_runtime.py`. The hardest invariant is that an admitted request
retains its captured model and attribution across replacement. Exercise without AI:
make the controlled old-model request fail after a new model publishes, and verify
that the new model still serves.
