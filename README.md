# TEMMS

TEMMS is a tiny runtime-independent library for selecting and activating the
best feasible model under changing operating conditions.

> Inject a runtime. Supply context. TEMMS selects the model.

The runtime owns model access, actual state, activation, and inference. The
selector owns policy. TEMMS connects them and verifies the runtime after a
change. It has no database, daemon, server, registry, fleet manager, UI, or
condition-collection framework.

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
Reconciliation calls for the same slot are serialized; different processes must
not independently control the same runtime slot.

## Policy is separate from model identity

`ModelRef` identifies a runtime-accessible artifact. `ModelPolicy` defines its
priority and constraints. A runtime inventory cannot silently create selection
policy, and a policy can explicitly report a configured model as unavailable.

There is no fallback chain. When no configured model is available and feasible,
the decision contains `selected_model=None`.

## Local ONNX Runtime adapter

Install the optional adapter dependencies:

```bash
python -m pip install -e ".[onnx]"
```

```python
from pathlib import Path
from temms.adapters import OnnxModel, OnnxRuntime

runtime = OnnxRuntime(models={
    "vision": [OnnxModel(model_ref, Path("models/model.onnx"), warmup=warmup_inputs)]
})
```

`OnnxRuntime` loads and optionally warms a new session before atomically replacing
the active session. In-flight inference retains its original session reference.

## Development

```bash
python -m pip install -e ".[dev,onnx]"
pytest
ruff check src tests examples
```

The core package has zero required runtime dependencies. Tests automatically
enforce module, line, protocol, dependency, subpackage, and example-size budgets.
