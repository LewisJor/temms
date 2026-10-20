# TEMMS

TEMMS is a tiny runtime-independent library for selecting and activating the
best feasible model under changing operating conditions.

> Inject a runtime. Supply context. TEMMS selects the model.

[Documentation](https://lewisjor.github.io/temms/) ·
[Edge CPU qualification](examples/edge/README.md) ·
[Real-model demo](examples/live_swap/README.md) ·
[Verified runs](docs/progress.md)

The runtime owns model access, actual state, activation, and inference. The
selector owns policy. TEMMS connects them and verifies the runtime after a
change. It has no database, daemon, server, registry, fleet manager, UI, or
condition-collection framework.

The [headless edge qualification](examples/edge/README.md) exercises real classifiers
under a Linux-enforced one-CPU, 512 MiB budget with no external networking. Its
native x86_64 and ARM64 scenarios cover contention, corrupt replacements, recovery
and fresh-container restart. See the [verification record](docs/progress.md) for
completed runs; the configured qualification is not itself evidence of a pass.
The chosen mounted-device target is a **Raspberry Pi 5 (4 GB) with 64-bit Ubuntu
Server 24.04 LTS**. Follow the [device setup](examples/edge/README.md#set-up-the-reference-device)
to run the same checks on the board; physical Pi qualification is still pending.

## Install and try

Requires Git and Python 3.11 or newer. Clone the repository and install the core
package in a virtual environment (the commands below use a macOS/Linux shell):

```bash
git clone https://github.com/LewisJor/temms.git
cd temms
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
python examples/adaptive_vision.py
```

The example prints:

```text
lowlight
lowlight low:frame-001
```

On Windows, create the environment with `py -3 -m venv .venv` and activate it in
PowerShell with `.\.venv\Scripts\Activate.ps1`. Run subsequent commands from the
repository root with the environment active.

The package version is `0.2.0a1`, an alpha library API incompatible with the v0.1
platform prototype. There is no `temms` command-line service. The example below
runs with core-only installation; both examples use handlers that simulate inference.
For real model predictions, follow the [terminal demo runbook](examples/live_swap/README.md).

```python
import asyncio
from temms import TEMMS, BestFeasibleSelector, Constraint, ModelPolicy, ModelRef, Operator
from temms.adapters import InMemoryRuntime

small = ModelRef(id="small", digest="sha256:small")
large = ModelRef(id="large", digest="sha256:large")
selector = BestFeasibleSelector([
    ModelPolicy(large, priority=100, constraints=(Constraint("battery", Operator.GTE, 40),)),
    ModelPolicy(small, priority=10),
])

async def main() -> None:
    runtime = InMemoryRuntime(
        models={"vision": [large, small]},
        handlers={large.digest: lambda x: x, small.digest: lambda x: x},
    )
    temms = TEMMS(runtime=runtime, selector=selector)
    result = await temms.reconcile("vision", {"battery": 18})
    inference = await temms.infer("vision", b"frame")
    assert result.decision.selected_model == small
    assert inference.model == small

asyncio.run(main())
```

## Runtime contract

A runtime implements four asynchronous methods:

The following is a signature summary; import `Runtime` from `temms` when typing
an implementation.

```python
class Runtime(Protocol[InputT, OutputT]):
    async def models(self, slot: str) -> Sequence[ModelRef]: ...
    async def state(self, slot: str) -> RuntimeState: ...
    async def activate(self, slot: str, model: ModelRef) -> ActivationResult: ...
    async def infer(self, slot: str, value: InputT) -> InferenceResult[OutputT]: ...
```

The runtime is the source of truth. After activation, TEMMS reads runtime state
and fails if the active model identity does not match the decision.

Inference is pinned to one model instance. A request admitted before a swap must
finish on and be attributed to the old model; a later request uses the new one.

One `TEMMS` instance is the reconciliation owner for each slot it controls.
Use it from one event loop. Reconciliation calls for the same slot are serialized;
different threads, event loops or processes must
not independently control the same runtime slot.

`InMemoryRuntime` rejects activation of a listed model with no registered inference
handler, raising `ModelUnavailableError` before changing active state. This preserves
any previous serving model. Registered handlers can still raise during inference;
activation does not execute them to validate their behavior.

## Policy is separate from model identity

`ModelRef` identifies a runtime-accessible artifact. `ModelPolicy` defines its
priority and constraints. A runtime inventory cannot silently create selection
policy, and a policy can explicitly report a configured model as unavailable.

Identity comparisons use `(id, digest)`, not metadata. `BestFeasibleSelector`
chooses the highest priority, keeps the current model among tied feasible models,
then breaks remaining ties by ascending model ID and digest. Constraints within
a policy must all pass. Context keys come from the caller; resource keys come
from `RuntimeState.resources`. Their names and units are application-defined.
See [selection semantics](docs/architecture.md#selection-and-reconciliation)
for missing values and type handling.

There is no fallback chain. When no configured model is available and feasible,
the decision contains `selected_model=None`.

This leaves the runtime's active model unchanged; it does **not** disable
inference. `TEMMS.infer()` delegates to the runtime without rechecking policy.
An existing active model can still serve requests; without an active model the
provided adapters raise `NoActiveModelError`. An application that must abstain
when no model is feasible must check the decision before admitting requests.

## Local ONNX Runtime adapter

Install the optional adapter dependencies:

```bash
python -m pip install ".[onnx]"
```

The `onnx` extra installs NumPy and ONNX Runtime for inference. The separate
`onnx` Python package builds the test models and belongs to the `dev` extra;
it is not needed to serve prepared models on a device.
The extra requires ONNX Runtime 1.19 or newer, whose
[NumPy 2 support](https://github.com/microsoft/onnxruntime/releases/tag/v1.19.0)
avoids the native import failure possible with older releases. NumPy 1.26 remains
the minimum. The deployment qualification uses its separately pinned versions;
see [compatibility checks](CONTRIBUTING.md#development-dependency-compatibility).

Configure a local artifact (replace the example path and identity with your own):

```python
from pathlib import Path
from temms import ModelRef
from temms.adapters import OnnxModel, OnnxRuntime

model_ref = ModelRef(id="classifier", digest="application-supplied-artifact-identity")
runtime = OnnxRuntime(models={
    "vision": [OnnxModel(model_ref, Path("models/model.onnx"),
                         providers=("CPUExecutionProvider",))]
})
```

This configures inventory; reconcile through `TEMMS` to activate the model.
Inference inputs map ONNX input names to arrays with the model's required shapes
and dtypes; outputs are returned as a tuple. Set `warmup` to such an input mapping
on `OnnxModel` to run it before publication. Preprocessing and labels belong to
the application. No model weights are bundled or downloaded, and the adapter
does not compute or authenticate the supplied digest.

`OnnxRuntime` loads and optionally warms a new session before atomically replacing
the active session. In-flight inference retains its original session reference.
Missing artifacts raise `ModelUnavailableError`; session-construction and warmup
errors propagate from the session implementation. These failures leave the prior
session active, and a later reconciliation can retry the replacement.

Inference before activation fails explicitly. This standalone failure example
also works with core-only installation:

```python
import asyncio
from temms import NoActiveModelError
from temms.adapters import InMemoryRuntime

async def main():
    runtime = InMemoryRuntime(models={}, handlers={})
    try:
        await runtime.infer("vision", "frame")
    except NoActiveModelError as error:
        print(error)  # slot 'vision' has no active model

asyncio.run(main())
```

Cancelling an ONNX activation while it awaits loading or warmup prevents its
publication; it does not stop the worker thread. See the
[concurrency contract](docs/architecture.md#concurrency-contract).

## Evidence and limitations

Start with the [edge CPU qualification runbook](examples/edge/README.md) to test
real model swaps under enforced resource limits. It explains preparation, two fresh
container runs, output-integrity checks and the evidence to inspect. Docker and
the qualification scripts remain outside the library.

The tiny [adaptive example](examples/adaptive_vision.py) uses in-memory handlers:

```bash
python examples/adaptive_vision.py
```

For a terminal demo with two real trained Hugging Face classifiers:

```bash
python -m pip install ".[onnx,demo]"
python examples/live_swap/assets.py
python examples/live_swap/workload.py
python examples/live_swap/run.py
```

Preparation downloads about 61 MB of shared model assets and copies 655 KB of
pinned, labeled dog images from the checkout. The offline replay cycles through
twenty images while simulated battery changes trigger swaps, a failed replacement,
continued serving and abstention.
Each served request displays its input, expected class, prediction and inference time.
Its full 1,000-score output must match direct ONNX execution for that input and model,
and its model identity must match admission. These checks catch stale-image outputs
and wrong-model attribution even when the predicted class stays the same.

Completion requires all seven scenes, successful checked service of every input,
and zero output or attribution mismatches. Classifier mistakes are displayed but
do not fail this functional check. The [demo runbook](examples/live_swap/README.md)
defines the score tolerance, coverage and independently auditable JSONL records.
Pillow is optional and used only for image preprocessing.
The [Ubuntu 24.04 qualification workflow](examples/live_swap/README.md#ubuntu-2404-qualification-in-github-actions)
runs the example against an installed wheel with external networking disabled.
[Qualification passed at `38e4a374`](https://github.com/LewisJor/temms/actions/runs/38810391287):
224 tests without skips, all seven scenes and twenty inputs covered, and zero output
or attribution mismatches. See [progress](docs/progress.md) for the exact environment,
raw evidence and earlier single-photo results.

For fixed-small, fixed-large, blocking-adaptive, and background-adaptive comparisons
on a pinned 20-image test workload:

```bash
python examples/live_swap/workload.py
python examples/live_swap/compare.py
```

The [comparison protocol and results](examples/live_swap/COMPARISON.md) explain
the shared request schedule, deadlines, policy admission, process memory, and raw
records. Background replacement keeps admitting requests on the old model while
it remains feasible; startup and infeasible-active transitions wait for replacement.
A separate balanced experiment measures native, thread-dispatch, adapter and TEMMS
inference costs. The initial
blocking comparison improved raw correctness slightly but did not improve correct
responses before the predeclared deadline over fixed MobileNetV2.
In the follow-up on the same Mac and workload, background swapping delivered
312/420 correct responses by the deadline versus 306 for blocking swaps and 305
for fixed-small, with a higher process memory peak. This is a narrow local result,
not a general performance ranking or deadline guarantee.

The separate validation workload uses 140 previously unused images, twenty per
phase, with the same models, policy, and deadlines:

```bash
python examples/live_swap/workload.py --workload validation
python examples/live_swap/compare.py --workload validation
```

Its fixed manifest prevents accidentally reusing development images or changing
the comparison inputs between strategies. In three local repetitions, background
swapping delivered **102/140 correct responses on time each run**, versus 100 with
blocking swaps and 101 with fixed-small. Its process memory peak was highest.
The benefit persisted on fresh images but remains small and specific to this task.
Both workloads remain available; the default is `--workload small`.

Real ONNX integration tests use small synthetic graphs, including a controlled
request spanning publication that checks both its numerical output and serving model.
The terminal references share preprocessing and ONNX Runtime with the serving path;
they check routing consistency, not independent engine correctness. Reference setup
and the paced replay are not a latency benchmark. The demo and comparison use small
dog-only subsets with unknown pretraining overlap, so they establish neither general
model quality nor a general advantage from switching.
Configured priority does not establish accuracy, and feasibility checks do not
physically enforce memory, energy, or latency limits. Process/native failures and
hard real-time guarantees are outside the contract.

See [progress and the next action](docs/progress.md) for dated test evidence and
remaining gaps, and [architecture](docs/architecture.md) for ownership boundaries.
The [runtime comparison](docs/architecture.md#runtime-choice-and-long-term-fit)
explains the current engine choice, alternatives and customer-readiness criteria.

## Development

```bash
python -m pip install -e ".[dev,onnx,demo]"
python -m pytest -ra
python -m ruff check src tests examples
python -m mypy
python -m build
```

The core package has zero required runtime dependencies. Tests automatically
enforce module, line, protocol, dependency, subpackage, and example-size budgets.
See [contributing](CONTRIBUTING.md) for clean-install verification and local
environment troubleshooting.
