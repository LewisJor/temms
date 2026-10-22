# Architecture

TEMMS has one orchestration path:

```text
runtime inventory + runtime state + caller context
                       │
                       ▼
               pure Selector policy
                       │
                       ▼
                    Decision
                       │
         selection exists and differs
                       ▼
               Runtime.activate()
                       │
                       ▼
                Runtime.state()
                       │
                 identity check
```

## Ownership

- **Application:** decides when to reconcile and supplies context.
- **ModelRef:** identifies a runtime-addressable artifact.
- **ModelPolicy:** contains priority and hard constraints.
- **Selector:** owns policy and chooses without side effects.
- **Runtime:** owns model access, active state, activation, and inference.
- **TEMMS:** serializes reconciliation per slot and checks reported active identity.

The runtime, not TEMMS, is the authority for which model is active.

## Selection and reconciliation

`BestFeasibleSelector` matches inventory to policies by `(id, digest)`; metadata
does not change identity. Unavailable policies are explicitly infeasible, and
inventory entries without a policy cannot be selected. Duplicate policy identities
are rejected. Feasible candidates rank by descending priority, then preference for
the current identity, then ascending ID and digest. Policy order is not a tie-break.

Every constraint must pass. `ValueSource.CONTEXT` reads the caller's mapping;
`ValueSource.RESOURCE` reads runtime resources. A missing key fails its constraint.
Comparisons use Python semantics without string-to-number coercion; a comparison
raising `TypeError` or `ValueError` fails its constraint. `IN` and `NOT_IN` require
a non-string container as the expected value. The supported operators are `EQ`,
`NE`, `GT`, `GTE`, `LT`, `LTE`, `IN`, and `NOT_IN`.

The input contract is `Mapping[str, Scalar]`, where `Scalar` is bool, int, float,
or str. Annotations are not runtime schema validation: callers should validate
units, finite numeric values, and domain-specific types before selection. Python
boolean/numeric equality and non-finite float behavior are not normalized. There
is no policy-file parser, built-in sensor vocabulary, or resource enforcement.

For example, `"40" >= 40` fails its constraint without conversion, but `True == 1`
passes. `NaN >= 40` fails, while `NaN != 40` and positive infinity `>= 40` pass.
Thus a malformed sensor value is not guaranteed to make a policy infeasible;
validate sensor data at the application boundary if these values are invalid in
your domain. A missing context key never falls back to a resource key, or vice versa.

`selected_model=None` and an already-active selected identity both return without
activation. No-feasible selection leaves any active session usable; the application
must decide whether to admit inference. Direct `OnnxRuntime.activate()` calls reload
and optionally warm even the same identity; `ActivationResult.changed` describes
identity change, not whether a session was constructed. `TEMMS.reconcile()` avoids
that redundant activation. Direct activation must not compete with the slot's
reconciliation owner.

## Concurrency contract

One `TEMMS` instance owns reconciliation for each controlled runtime slot.
Use that instance from one event loop; its `asyncio.Lock` objects do not coordinate
threads or independent event loops. Same-slot reconciliations are serialized.
Cross-process ownership is outside the
kernel and must not be attempted without an application-provided coordinator.
Different slots use independent TEMMS locks. Progress still requires a cooperative
runtime and available execution resources; independent locks do not prevent thread
pool saturation or a custom runtime from blocking the event loop.

An inference request is pinned to the model instance captured at admission. A
concurrent activation may change later requests but cannot change the in-flight
request's model or attribution.

An ordinary inference exception from the ONNX adapter propagates to its caller.
If a replacement has already published, the adapter neither restores the old active
session nor retries the failed request on the replacement. The controlled swap test
checks original exception identity and continued replacement service; failures do
not return a model-tagged `InferenceResult`.

For `OnnxRuntime`, admission occurs when `infer()` captures the loaded session
under the adapter's single lock, before dispatching inference to a thread.
Publication occurs when `activate()` assigns the fully loaded and warmed session
under that lock. The lock protects only active-state capture and publication;
native construction, warmup and inference execute outside it. Unknown-slot reads
do not create retained lock entries. TEMMS reconciliation locks remain per slot.
Calling an async method without awaiting or scheduling it does not admit a request.
TEMMS delegates inference; it must not compare an old result with the latest state.

Cancellation delivered while activation awaits loading or warmup prevents that
activation from publishing. The worker thread can continue; cancelling the waiting
coroutine does not terminate native work. The cancelled reconciliation releases its
slot lock, allowing another reconciliation before that worker finishes. Cancellation
after publication does not roll back a model already made active.

ONNX inference takes a shallow snapshot of the input mapping; its values and tensor
buffers remain borrowed from the caller. Do not mutate or reuse that storage until
the native inference finishes, including after the awaiting caller is cancelled.
Cancellation does not signal that the worker has stopped reading its inputs. Use
request-owned buffers when worker completion cannot be observed; the worker retains
references to those values while it runs.

The ONNX adapter keeps no retired-session cache. Admitted inference and unfinished
workers retain their captured sessions until they finish. Successful result objects
do not themselves retain the Python session wrapper in the tested adapter path;
caller-held exceptions can retain it through traceback frames. Dropping the runtime
and draining workers permits wrapper collection. Collection does not guarantee that
native allocators return resident pages to the operating system. See the separate
[lifetime tests and memory experiment](../examples/live_swap/COMPARISON.md#session-lifetime-and-swap-churn).

These publication and inference guarantees belong to the adapter. TEMMS checks
reported active identity after activation and on a subsequent state read; it cannot
undo arbitrary side effects of a custom runtime that violates its contract. Reported
digests are identities, not artifact authentication. There are no guarantees of
surviving process termination, native crashes, hard out-of-memory failures, or of
meeting real-time deadlines.

## Non-goals

The kernel has no registry, package manager, HTTP server, daemon, database,
condition store, UI, fleet orchestration, or cryptographic evidence subsystem.
Runtime-specific dependencies live only in adapters.

## Complexity budget

- Zero required core runtime dependencies.
- At most six direct core modules.
- At most 600 physical lines in direct core modules, 200 per module; exports in
  `__init__.py` are excluded by the test and must not hide orchestration logic.
- At most 40 lines in `examples/adaptive_vision.py`.
- Two public protocols: `Runtime` and `Selector`.
- One orchestration class: `TEMMS`.
- No core subpackages except `adapters`.
- No compatibility facade for the previous prototype.
- No abstraction without two concrete consumers.

The import check covers every direct core file, including public exports. It allows
standard-library and TEMMS imports, with SQLite explicitly excluded. This static
check supplements installed-package qualification; it does not analyze dynamic imports.

## Runtime choice and long-term fit

Reviewed against primary documentation on **2026-10-13**. The assumed customer is
a developer embedding inference in a single-device Python application, initially
on a Linux edge computer or robot. Execution evidence includes macOS ARM64 and
[constrained Ubuntu 24.04 native x86_64 and ARM64 qualification](https://github.com/LewisJor/temms/actions/runs/39485701787)
at `827b7242` on **2026-10-20**. A physical edge device has not been qualified. This
comparison concerns architecture and integration cost, not measured performance
across engines.

**Decision: retain the small library and the existing ONNX Runtime adapter.**
The durable boundary is application-owned context and admission, pure selection,
and runtime-owned execution and publication. Adding speculative engines would add
maintenance obligations without demonstrating a second real consumer. There is no
engine choice that guarantees compatibility with future hardware or model exports.

| Option | What it already provides | Fit and switching cost |
| --- | --- | --- |
| Direct ONNX Runtime in the application | Local sessions, inference, provider selection and session options. | Sufficient for a fixed model or simple application-owned replacement. TEMMS is justified when its tested policy, serialization and attribution contracts save integration work. [Python API](https://onnxruntime.ai/docs/api/python/api_summary.html) |
| TEMMS with ONNX Runtime | The current runtime implementation and real two-model workload; zero required dependencies in the selection core. | Best-supported path in this repository. Measured process/container memory includes Python, native sessions and replacement overlap; it does not isolate each contribution. Small Python source is not a small-device measurement. |
| LiteRT | On-device execution with `.tflite` artifacts. Its current CompiledModel Python API supports CPU and GPU, as well as a wider native ecosystem. | Consider when a concrete application's formats and target acceleration favor it. Existing ONNX models need conversion and output/preprocessing validation. Python availability does not establish support on every device. [Python API](https://developers.google.com/edge/litert/next/python), [platform overview](https://developers.google.com/edge/litert) |
| ExecuTorch | PyTorch export to `.pte` and native edge execution; the Python runtime wraps C++ and uses tensor inputs/outputs. | Consider for a PyTorch-native application. Export, delegate and lifecycle integration are additional work; a small native kernel build does not describe the footprint of a Python installation. [Overview](https://docs.pytorch.org/executorch/stable/intro-overview.html), [Python API](https://docs.pytorch.org/executorch/stable/runtime-python-api-reference.html) |
| Direct OpenVINO | Python/native inference, direct ONNX loading, CPU support including ARM/Apple Silicon, and Intel GPU/NPU acceleration with platform requirements. | If an independent second engine becomes a concrete requirement, this is a practical evaluation candidate because it can start with the existing artifacts. No TEMMS compatibility or performance result is established yet. [ONNX loading](https://docs.openvino.ai/2026/openvino-workflow/model-preparation/convert-model-onnx.html), [requirements](https://docs.openvino.ai/2026/about-openvino/release-notes-openvino/system-requirements.html) |
| Triton / Edge Impulse | Triton has model-control and background-loading capabilities; prepared Edge Impulse artifacts can run offline. | Relevant alternatives for customers wanting a serving system or broader deployment workflow. Hot loading and offline execution alone are not TEMMS differentiators. They do not justify bringing a server or platform into this library. [Triton model management](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html), [Edge Impulse offline execution](https://docs.edgeimpulse.com/hardware/deployments/run-docker#running-offline) |

An ONNX Runtime Execution Provider assigns supported graph operations to execution
backends; it does not choose model identities. Changing providers or placing the
same runtime in several containers does not prove independent-engine portability.
The in-memory adapter is useful contract evidence, not a second trained-model engine.
The maintained [Docker qualification](../examples/edge/README.md) checks installed
execution, resource limits and offline operation on native hosted CPUs. It does
not establish physical-device performance or another inference engine's compatibility.
See [providers](https://onnxruntime.ai/docs/execution-providers/) and
[Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/).

### Reliability constraints for another engine

A matching method signature is insufficient. Each adapter must establish when
inputs are captured, when a warmed replacement becomes visible, how admitted work
retains its model, and whether returned buffers remain usable after later inference.
OpenVINO shared outputs can be views changed by subsequent inference. ExecuTorch's
native `Module` lacks internal synchronization and can reuse output arenas; its
Python API has different copying rules. Do not copy the ONNX adapter's concurrency
strategy without testing the chosen API's actual ownership rules.
See [OpenVINO shared memory](https://docs.openvino.ai/2026/openvino-workflow/running-inference/inference-request/python-api-advanced-inference.html),
[ExecuTorch native ownership](https://docs.pytorch.org/executorch/stable/executorch-runtime-api-reference.html)
and [Python ownership](https://docs.pytorch.org/executorch/stable/runtime-python-api-reference.html).

Acceptance for a justified second engine would reuse unchanged artifact identities
where supported; converted artifacts need new identities and recorded conversion
provenance. Keep inputs, preprocessing semantics, labels and declared workload limits
comparable. Unchanged models need output equivalence within declared tolerances;
intentional quantization needs a predeclared task-quality threshold. Every adapter
must demonstrate failed-load/warmup continuity, pinned inference, cancellation and
worker draining, and outputs surviving later requests.
Measure complete request latency, replacement cost and peak memory including both
sessions on the intended device. Existing ONNX evidence supplies none of those
results for another engine.

### Size and optimization policy

Optimize the deployed application against measured limits. Report core/wheel bytes,
the complete dependency installation and models separately from process peak memory;
include simultaneous sessions, native worker pools and outputs in memory trials.
Our earlier serving-dependency reduction is useful evidence for one installation,
not a universal runtime-size ranking.

Use the ONNX adapter's existing `session_factory` when a measured need calls for
native session tuning. ONNX Runtime exposes thread counts and spinning controls;
these trade latency against CPU and power, and overlapping sessions can have their
own pools. No universally best setting follows from the docs.
[Threading controls](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)

Selective native builds can reduce deployment size, but couple the build to the
models' operators, types, formats and target backends. ONNX Runtime's basic minimal
build needs ORT format and has execution-provider restrictions; LiteRT's selective
Interpreter build is distinct from its modern CompiledModel Python package.
ExecuTorch selective kernels and OpenVINO conditional builds similarly require
deployment-specific qualification. Evaluate these only after standard packaging
fails a declared device budget; arbitrary future model replacement may then need
another native build.
[ORT custom builds](https://onnxruntime.ai/docs/build/custom.html),
[LiteRT selective builds](https://developers.google.com/edge/litert/build/reduce_binary_size),
[ExecuTorch integration](https://docs.pytorch.org/executorch/stable/using-executorch-runtime-integration.html),
[OpenVINO conditional builds](https://docs.openvino.ai/2026/openvino-workflow/deployment-locally/optimial-binary-size-conditional-compilation.html)

### Open source and customer readiness

TEMMS is already a public repository and remains an alpha library. A plausible
initial paid offering is integration and validation for one named Python/ONNX CPU
deployment, with explicitly agreed maintenance. That is a commercial hypothesis:
the demo establishes neither willingness to pay nor broad superiority to direct
runtime integration. A customer trial should test whether developers can integrate
the library and diagnose a failed transition without author assistance, as well as
whether it meets their workload limits.

Apache-2.0 permits commercial redistribution and separately offered support under
its conditions; it does not establish rights in every bundled dependency, model or
dataset. The runtime repositories declare MIT (ORT), Apache-2.0 (LiteRT/OpenVINO)
and BSD three-clause terms (ExecuTorch). Review the exact shipped versions, notices,
model terms and vendor SDK/driver terms before distributing a customer bundle.
[Apache terms](https://www.apache.org/licenses/LICENSE-2.0),
[ORT license](https://github.com/microsoft/onnxruntime/blob/main/LICENSE),
[LiteRT license](https://github.com/google-ai-edge/LiteRT/blob/main/LICENSE),
[ExecuTorch license](https://github.com/pytorch/executorch/blob/main/LICENSE),
[OpenVINO license](https://github.com/openvinotoolkit/openvino/blob/master/LICENSE)

Qualification must name the Python version, runtime/provider, OS/architecture,
drivers, artifact hashes and export/preprocessing contract. Upstream lifecycle
policies differ: ExecuTorch distinguishes experimental and stable APIs, while
OpenVINO's regular and LTS releases have different support periods. A moving
documentation page or a package resolver accepting a version is not a tested
support matrix. Preserve compatible library requirements and separately capture
the complete tested deployment environment.
[ExecuTorch lifecycle](https://docs.pytorch.org/executorch/stable/api-life-cycle.html),
[OpenVINO release policy](https://docs.openvino.ai/2026/about-openvino/release-notes-openvino/release-policy.html),
[Python dependency guidance](https://packaging.python.org/en/latest/discussions/install-requires-vs-requirements/)

The maintained [release criteria](../CONTRIBUTING.md#release-and-support-criteria)
separate a tested checkout from a supported release. Current execution evidence,
unresolved gaps and the single next action remain in [progress.md](progress.md).
