# TEMMS progress — updated 2026-10-20

TEMMS remains an alpha Python library for application-owned local inference. This
iteration starts from [`eebe3ffb`](https://github.com/LewisJor/temms/commit/eebe3ffbcb6053c078a03f22d2fce05f7559b80f)
and adds one behavior: qualify real model replacement under an enforced native
Linux CPU and memory budget, without external networking. Core and adapters are
unchanged. The baseline passed 257 tests with zero skips or warnings.

## Kept improvement: constrained native execution

**Hypothesis and acceptance:** the existing runtime should serve correct,
properly attributed outputs while sharing a one-CPU quota with competing work,
reject a corrupt replacement without losing the active model, and recover when
the verified artifact returns. Both native x86_64 and ARM64 must complete the same
scenario in two fresh containers under a 512 MiB memory limit, with no swap or
external network. Failed exits, OOM events, unenforced limits, inactive contention
helpers, incorrect scores or attribution, and missing phases all fail acceptance.

The [headless runbook](../examples/edge/README.md) prepares pinned MobileNetV2 and
ResNet18 models and twenty existing photographs before disconnecting the worker.
Each container starts with an empty runtime, serves both models, introduces two
CPU competitors, rejects a deliberately corrupt private replacement, restores the
verified source, performs twelve alternating swaps, abstains when required input
context is missing, and resumes. The launcher repeats the entire trace in a new
container. Each worker serves 113 requests and performs one application abstention.
No model state is persisted between workers.

Application context explicitly requests the model; TEMMS does not automatically
measure pressure or administer containers. Linux enforces the limits. The worker
records cgroup limits and counters, CPU consumed by each competitor during the
serving block, networking probes and filesystem restrictions. The launcher retains
Docker's configuration, distinct container identities, exit status and OOM flag.
Original model/input files remain read-only and their hashes are checked again.

Every returned 1,000-score vector and admitted model identity is checked against a
direct reference for the exact image/model pair. A separate verifier reconstructs
the required trace and recomputes numerical, attribution, coverage and resource
checks from retained evidence. The inference image contains the built library wheel
and example code, not the library source checkout. Docker build context is explicitly
limited to those inputs. CPU/memory probes, fault fixtures and Docker integration
remain outside the published library; no public protocol or backend was added.

## Hosted qualification

Implementation [`827b7242`](https://github.com/LewisJor/temms/commit/827b7242fc318d6567f20f5f7715ab71d7d7c049)
passed the [native matrix](https://github.com/LewisJor/temms/actions/runs/39485701787)
and the [existing Ubuntu qualification](https://github.com/LewisJor/temms/actions/runs/39485701691).
Both architecture jobs ran **286 installed-wheel tests without skips** and two
fresh constrained workers. The image used Ubuntu 24.04, Python **3.12.3**, ONNX
Runtime **1.24.1**, NumPy **2.4.2** and Pillow **12.1.0**. The host test interpreter
was Python 3.12.15. No QEMU was used. The base image is digest-pinned; apt package
resolution is not frozen by that digest.

| Architecture | Served across two workers | Peak cgroup memory, worker 1 / 2 | Throttled periods during contention, worker 1 / 2 |
| --- | ---: | ---: | ---: |
| x86_64 | 226 | 358.61 / 344.42 MiB | 4 / 5 |
| ARM64 | 226 | 338.61 / 397.60 MiB | 18 / 18 |

All four workers checked **20/20 images and 40/40 model/image pairs**. All **452
served vectors and identities matched** their references. Each worker rejected
the corrupt replacement, kept the previous model serving, restored the artifact,
completed twelve alternating swaps and abstained once before resuming. Each had
**zero OOM events**, a successful exit and verified read-only assets. Both CPU
competitors consumed CPU during each contention block. Independent verification
passed in the constrained verifier container and again on the downloaded records.
Each worker classified 99/113 requests as expected; this is descriptive, not an
accuracy acceptance threshold or a new evaluation set.

Artifacts `edge-x64-39485701787-1` and `edge-arm64-39485701787-1` retain full scores,
references, labels, environment, cgroup counters, container inspections, build and
test logs. GitHub retains them for 30 days; downloaded copies remain under
`.demo-assets/edge/ci-39485701787/`, outside Git. Later revisions must be associated
with their own workflow conclusions; these measurements identify `827b7242`.

## Local checks and size

The complete local suite now passes **303 tests with zero skips** on macOS ARM64.
The additional seventeen verifier regressions include sixteen deliberately damaged
evidence cases: wrong non-argmax scores, identity/hash drift, forged summaries,
bad resource witnesses, reused containers and writable original assets. The full
native scenario also has a regression using small real ONNX graphs. Resource
tests reject unenforced limits, network leakage, escaped or inactive competitors,
absent throttling and failed cleanup. Scoped Ruff, strict mypy on ten files, the
architecture budget and the strict Sphinx documentation build pass. Source and
wheel builds succeed; the source distribution includes the Dockerfile and launcher,
while the wheel contains only the library. No constrained Docker workload was
executed on this development Mac.

Core remains **429 physical lines** across five modules, largest 178; exports 50,
adapters 229 and the tiny example 37. Required runtime dependencies remain zero.
The added qualification scripts, workflow, tests and documentation increase
repository support code but do not change the installed library's architecture.
The Webots workflow now runs only by manual dispatch; its earlier fixture remains
available as an optional example rather than the primary validation path.

## Evidence boundaries

The earlier [Webots qualification](https://github.com/LewisJor/temms/actions/runs/39271421225)
passed at the baseline commit. That stationary photo exhibit tested a rendered
camera integration; it did not establish edge-device suitability. The
[comparison and memory studies](../examples/live_swap/COMPARISON.md) remain dated
measurements. They were not rerun or converted into claims for this new profile.

Direct references share preprocessing and ONNX Runtime with serving. Agreement
establishes integration consistency, not independent engine correctness. The same
twenty photographs are reused, so this is not a new accuracy evaluation. Preparing
references warms native code and the filesystem cache; fresh-process startup is
not a cold-device measurement. Classification correctness and descriptive latency
are distinct from routing integrity; this scenario defines no deadline guarantee.

The 512 MiB limit covers the entire container, including native allocations,
helpers, temporary files and charged filesystem cache. Peak cgroup memory is not
TEMMS's incremental RSS. A recoverable corrupt-file failure is not an OOM-kill
survival test. Constrained hosted CPUs do not qualify a Raspberry Pi, Jetson,
thermal limits, battery life or sensor behavior. See the
[release/support criteria](../CONTRIBUTING.md#release-and-support-criteria).

The selected mounted-device target is now **Raspberry Pi 5 (4 GB), running 64-bit
Ubuntu Server 24.04 LTS**. Its [setup guide](../examples/edge/README.md#set-up-the-reference-device)
uses the existing native ARM64 qualification without adding a runtime or changing
acceptance limits. Host OS, kernel, board identity and source revision are retained
with the result. No physical Pi execution has been established yet.

**Exactly one next action:** run the documented profile unchanged on that Pi 5
and retain its revision, host identity, environment and qualification evidence.

Read `exercise` in `examples/edge/run.py`, `cpu_pressure` in
`examples/edge/resources.py`, and `OnnxRuntime.infer` in `src/temms/adapters/onnx.py`.
The hardest invariant remains that a request finishes with its captured model's
output and identity even after another model publishes. Exercise without AI:
extend the controlled in-flight test so the old native request fails after
publication, and assert that the new model still serves afterward.
