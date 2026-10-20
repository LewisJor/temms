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

## Qualification status

The [native workflow](../.github/workflows/edge.yml) targets Ubuntu 24.04 x86_64 and
ARM64 without QEMU. Its host installed-wheel suite and two constrained workers per
architecture are configured; **hosted qualification has not yet completed** for
this revision. Configuration and local unit results are not native Linux evidence.
The image pins its Ubuntu base digest and ONNX Runtime 1.24.1, NumPy 2.4.2 and
Pillow 12.1.0. Ubuntu's Python 3.12 patch and installed environment are recorded;
apt package resolution is not frozen by the base-image digest.

Local checks will be recorded alongside the completed hosted result. A native
ONNX regression already exercises the full 113-request fault/recovery trace using
small real graphs. Focused resource tests reject unenforced or invalid limits,
network leakage, inactive/escaped competitors, absent throttling and failed
cleanup. These tests do not replace executing the real models under Linux limits.

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

**Exactly one next action:** complete the native Linux matrix and audit its retained
evidence against the declared acceptance criteria.

Read `exercise` in `examples/edge/run.py`, `cpu_pressure` in
`examples/edge/resources.py`, and `OnnxRuntime.infer` in `src/temms/adapters/onnx.py`.
The hardest invariant remains that a request finishes with its captured model's
output and identity even after another model publishes. Exercise without AI:
extend the controlled in-flight test so the old native request fails after
publication, and assert that the new model still serves afterward.
