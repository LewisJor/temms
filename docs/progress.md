# TEMMS progress — updated 2026-10-20

TEMMS remains an alpha Python library for application-owned local inference. This
iteration starts from `8caf55ac` and makes three bounded changes: remove the
graphical simulator, repair fresh qualification input preparation, and honor every
explicitly injected ONNX session factory. No core policy or public protocol changed.

## Failure and acceptance

At `8caf55ac`, [both native architecture jobs](https://github.com/LewisJor/temms/actions/runs/39492290202)
and the [Ubuntu CPU replay](https://github.com/LewisJor/temms/actions/runs/39492290128)
failed with HTTP 502 while requesting Hugging Face dataset-viewer metadata. Shared
models, labels and the Chelsea image had downloaded and verified successfully.
The failures occurred before inference; nevertheless, a fresh user's preparation
path was broken. CI, core packaging and documentation passed at that revision.

**Hypothesis:** the ordinary qualification can prepare its unchanged twenty inputs
without a live metadata service. Acceptance requires fresh offline preparation,
the original pinned hashes, fail-closed handling of damaged source fixtures,
unchanged validation-workload provenance, and successful native Linux qualification
without weakening its resource or output gates.

The twenty existing JPEGs now ship as example fixtures: **654,484 bytes**, with
[attribution and CC BY-SA 4.0 terms](../examples/live_swap/inputs/README.md). Their
bytes and frozen manifest are unchanged. Preparation checks needed sources before
copying through the existing atomic staging path. Prepared caches need neither
the bundle nor networking. The optional 140-image validation workload still checks
remote revision and annotations before downloading; its preparation retains that
upstream availability dependency. Model acquisition and image builds still need
networking before disconnected execution.

The graphical simulator's workflow, world, controller, verifier and dedicated
tests were removed, along with camera-only helpers. This deletes about **1,400
lines** of support code. The maintained execution path is the
[headless Ubuntu qualification](../examples/edge/README.md).

Separately, the ONNX adapter used `session_factory or default`, silently ignoring
a valid callable with false truthiness. It now defaults only on `None`. A regression
failed before the fix and passes afterward, checking injected construction, warmup,
activation and inference. The adapter's publication and session-capture logic is
unchanged.

## Local verification and size

The full suite passes **275 tests with zero skips** on macOS ARM64, Python 3.11.8,
ONNX Runtime 1.24.1, NumPy 2.4.2 and Pillow 12.1.0. The lower count reflects removal
of simulator-specific tests, with new preparation and injection regressions added.
Ruff, strict mypy on ten files, the architecture budget and the strict Sphinx build
pass. Fresh preparation tests permit only local fixture URLs and reject metadata
access. They also check
missing/corrupt sources, untouched prior destinations and cached offline reuse.

Source and wheel builds succeed using the installed backend without isolation.
The source distribution contains all twenty exact JPEGs and their attribution;
the wheel contains only the library. The Docker build context excludes the fixtures
because preparation supplies the separate read-only asset mount. No constrained
Docker workload was executed on this development Mac.

An extracted source archive also prepared all twenty inputs into an empty directory
with a verified socket-connection denial hook: zero connection attempts, all pinned
bytes intact. Neither distribution contains simulator paths. The built library
wheel is **20,078 bytes**, including standard distribution metadata and license.

Core remains **429 physical lines** across five modules, largest 178; exports 50,
adapters 229 and the tiny example 37. Required runtime dependencies remain zero.
No executor, cache, registry, backend or new public abstraction was introduced.

## Hosted qualification

The fix at
[`a0c8beca`](https://github.com/LewisJor/temms/commit/a0c8becab54d169e156b34837ce4c4dbbf417e52)
passed the [native matrix](https://github.com/LewisJor/temms/actions/runs/39496958193)
and [Ubuntu replay](https://github.com/LewisJor/temms/actions/runs/39496958438).
[CI](https://github.com/LewisJor/temms/actions/runs/39496958066),
[installed core packaging](https://github.com/LewisJor/temms/actions/runs/39496958061)
and [documentation publication](https://github.com/LewisJor/temms/actions/runs/39496958125)
also passed. Each native architecture ran **275 installed-wheel tests without skips**
and two fresh constrained containers. The images used Ubuntu 24.04, Python 3.12.3, ONNX Runtime
1.24.1, NumPy 2.4.2 and Pillow 12.1.0; the host test interpreter was Python 3.12.15.
No QEMU was used. These measurements identify that code revision; later
documentation-only status updates do not change the tested implementation.

| Architecture | Served across two workers | Peak cgroup memory, workers 1 / 2 |
| --- | ---: | ---: |
| x86_64 | 226 | 348.77 / 373.07 MiB |
| ARM64 | 226 | 346.41 / 330.95 MiB |

All **452 served score vectors and identities matched** direct references, with
20/20 images and 40/40 model/image pairs checked in each worker. All four completed
corrupt-artifact recovery, twelve alternating swaps and missing-context abstention
followed by recovery. They had zero OOM events and verified read-only assets.
Both contention helpers consumed CPU during each serving block and cgroup counters
witnessed throttling. Independent verification passed again on both downloaded
architecture records. Raw artifacts are retained by GitHub for 30 days; downloaded
copies remain under `.demo-assets/edge/ci-39496958193/`.

The separate offline Ubuntu replay passed all seven scenes: **69 served outputs**,
20/20 inputs and 35/40 model/input pairs, with zero score or identity mismatches.
Its raw-record audit, network isolation and 275-test XML were checked again locally.
Records remain under `.demo-assets/edge/cpu-39496958438/`. Earlier qualification
[at `827b7242`](https://github.com/LewisJor/temms/actions/runs/39485701787) remains
historical evidence; the intervening metadata failures above were not erased or
treated as passing runs.

## Performance and evidence boundaries

The existing [comparison and memory studies](../examples/live_swap/COMPARISON.md)
remain dated, narrow measurements. On the 140-image validation workload, background
adaptation produced 102 timely correct responses per run versus 101 for fixed-small,
while using substantially more memory. Existing wrapper-overhead differences
changed sign between processes, and both comparison paths include adapter/thread
dispatch. Direct ONNX controls also reproduced memory growth. None establishes
a general speed advantage, leak-freedom or unique hot-swapping capability.

The constrained trace uses application-scripted context, not automatic resource
sensing. Its direct references share preprocessing and ONNX Runtime with serving;
agreement checks integration consistency, not independent engine correctness.
References warm native code and filesystem caches. The same twenty inputs are
reused; this is neither a new accuracy study nor a cold-device benchmark.
Whole-container memory includes native allocations, helpers and charged file cache.
Recoverable corrupt-file rejection is not hard-OOM survival or a deadline guarantee.

The selected mounted-device target is **Raspberry Pi 5 (4 GB), running 64-bit
Ubuntu Server 24.04 LTS**. Its [setup guide](../examples/edge/README.md#set-up-the-reference-device)
uses the same one-CPU, 512 MiB, no-swap, disconnected profile. No physical Pi run
has been established. Hosted ARM64 evidence does not establish battery life,
thermal limits, power use or sensor integration. See the
[release/support criteria](../CONTRIBUTING.md#release-and-support-criteria).

**Exactly one next action:** measure direct native inference, thread dispatch,
the ONNX adapter and TEMMS against the same warmed session using balanced paired
ordering, full-output checks and bounded fresh-process repetitions before choosing
an optimization.

Read `TEMMS.reconcile` in `src/temms/engine.py`, `OnnxRuntime.infer` in
`src/temms/adapters/onnx.py`, and `prepare_workload` in `examples/live_swap/workload.py`.
The hardest invariant is that an admitted request retains its captured model and
attribution across replacement. Exercise without AI: make the controlled old-model
request fail after a new model publishes, and verify that the new model still serves.
