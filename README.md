# TEMMS

TEMMS is a tiny runtime-independent library for selecting and activating the
best feasible model under changing operating conditions.

> Inject a runtime. Supply context. TEMMS selects the model.

TEMMS does not own a model registry, HTTP server, daemon, database, fleet
manager, UI, or condition-collection framework. The injected runtime owns model
access, actual active state, activation, and inference.

```python
import asyncio

from temms import BestFeasibleSelector, Constraint, ModelRef, Operator, TEMMS
from temms.adapters import InMemoryRuntime

small = ModelRef(id="small", digest="sha256:small", priority=10)
large = ModelRef(
    id="large",
    digest="sha256:large",
    priority=100,
    constraints=(Constraint("battery", Operator.GTE, 40),),
)

async def main() -> None:
    runtime = InMemoryRuntime(
        models={"vision": [large, small]},
        handlers={large.digest: lambda x: x, small.digest: lambda x: x},
    )
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector())
    result = await temms.reconcile("vision", {"battery": 18})
    inference = await temms.infer("vision", b"frame")
    assert result.decision.selected_model == small
    assert inference.model == small

asyncio.run(main())
```

## Runtime contract

A runtime implements four asynchronous methods:

```python
class Runtime(Protocol[InputT, OutputT]):
    async def models(self, slot: str) -> Sequence[ModelRef]: ...
    async def state(self, slot: str) -> RuntimeState: ...
    async def activate(self, slot: str, model: ModelRef) -> ActivationResult: ...
    async def infer(self, slot: str, value: InputT) -> InferenceResult[OutputT]: ...
```

The runtime is the source of truth. TEMMS never records or claims activation on
its own. After `activate()`, it asks the runtime for state and fails if the
reported active model identity does not match the selected model.

## Selection

`BestFeasibleSelector` evaluates hard constraints against caller context or
runtime resources, excludes failures, then selects by:

1. Higher explicit priority.
2. Current model on a priority tie, avoiding unnecessary churn.
3. Stable model ID and digest.

There is no fallback chain. When no model is feasible, the decision explicitly
contains `selected_model=None`.

## Development

```bash
python -m pip install -e ".[dev]"
pytest
ruff check src tests examples
```

The core package has no runtime dependencies. A test-enforced complexity budget
keeps core below 600 lines and blocks platform dependencies.

## Scope

Current scope:

- Injected runtime contract.
- Runtime-owned model access and inference.
- Pure best-feasible selection.
- Runtime-verified activation.
- Deterministic in-memory reference runtime.

Next adapter: a local ONNX Runtime implementation extracted from the previous
TEMMS prototype without bringing back its database, daemon, server, Hub, or
fleet machinery.
