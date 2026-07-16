# Model Swap — Behavior and Adapter Seam

See [`docs/direction.md`](direction.md) for why this is deliberately thin.

Swapping a model in a slot is a **commodity**, not a TEMMS differentiator. TEMMS
does not ship a bespoke swap runtime. It defines a small **runtime-adapter
boundary** and drives whatever runtime is present:

- **Server runtimes** (Triton, TorchServe) already provide hot-reload, warmup,
  draining, and versioned rollback through their model-control API. The adapter
  *delegates* — TEMMS writes/loads and lets the server do the swap.
- **Library runtimes** (ONNX Runtime, TFLite) have no swap primitive — a runtime
  is just `InferenceSession(path)`. There the adapter provides the minimal swap
  below. It is ~40 lines, not a subsystem.

The reference in-process ONNX Runtime adapter is
`src/temms/inference/runtime.py::InferenceRuntime`.

## The runtime-adapter boundary

Any backend TEMMS drives exposes, conceptually:

| Operation | Meaning |
|-----------|---------|
| `load(model)` | Materialize an executable instance (pull + open). May be a no-op reference for a server that already has it. |
| `warm(instance)` | Run one inference so the first real request isn't cold. No-op if the backend warms itself. |
| `activate(slot, instance)` | Atomically make this instance the one new requests use. |
| `infer(slot, input)` | Run against the currently-active instance. |
| `unload(instance)` | Release. For in-process this is implicit (see draining). |
| `fits(model)` | Whether the target has capacity for this model (admission). |

TEMMS is the control plane above this line: registry → signed → DDIL-aware
policy decides *which* model; the adapter just loads/activates it. The formal
`Protocol` is intentionally **not** written yet — it will be extracted when a
second backend (e.g. a Triton adapter) actually exists, to avoid designing the
interface against a single implementation.

## Minimal in-process swap behavior (library runtimes)

For a hot-swap of slot `S` from `M_old` to `M_new`, the in-process adapter MUST:

1. **Keep serving.** The slot stays available throughout. `M_old` answers every
   request until cutover. (Concretely: the controller keeps the slot `RUNNING`
   and only uses `LOADING` on a cold start, because request admission is gated
   on slot state.)
2. **Load then warm `M_new`** before it is published. Warmup is best-effort: a
   synthetic input is derived from the runtime's declared input shape; if it
   can't be built or the model rejects it, warmup is logged and skipped rather
   than aborting the swap (the first real request pays the cold cost, and
   inference-time fallback covers a genuinely broken model).
3. **Atomic cutover.** Publish `M_new` as the active instance under a short lock
   (a single pointer assignment). Requests admitted before bind to `M_old`,
   after to `M_new`.
4. **Drain by reference, not bookkeeping.** Do **not** eagerly unload `M_old`.
   An in-flight request holds its own reference to the instance it began on, so
   the old instance (and its native session) is released only once the last
   in-flight request returns. In CPython this is reference counting — no manual
   drain counter, and memory is reclaimed promptly.

Consequences: no request is dropped or errored because a swap is in progress; a
request that began before a swap always completes on the instance it started on.
`infer` treats the caller's expected model id as a hint, not a gate — it serves
whatever is loaded.

For a **server backend**, steps 1–4 are the server's job; the adapter issues the
load/activate calls and reports readiness.

## Anti-flap hysteresis

A sensor oscillating across a policy threshold must not thrash swaps. Dwell is a
policy concern (a condition must hold for a minimum time / N evaluations before
it can trigger a switch), specified with the decision engine in
`src/temms/policy/`, not in the swap layer. (Tracked in issue #12.)

## Capacity / admission

Whether `M_new` fits is delegated to the adapter/target via `fits(model)`. If it
does not fit, the control plane either sequential-loads (documented brief
unavailability, recorded in the decision log) or refuses the swap with an
evidence entry. There is no bespoke footprint estimator — the runtime/target
knows its own capacity.

## Crash recovery

No swap-intent journal is needed. `active_model_id` is committed to the slot
store only inside the single transaction that finalizes a successful swap (after
load + warm), so a half-swap is never persisted — the store always names the
last **fully-activated** model.

On startup the daemon restores each slot's persisted `active_model_id` (falling
back to the configured `default_model`) rather than always loading the default,
so a process that died mid-operation comes back on the model it was actually
running. See `src/temms/daemon/service.py::_auto_start_slots`.

## Conformance tests

- `tests/unit/test_swap_contract.py` — warmup before serve; a request served
  after a completed swap; 25 concurrent requests during an in-progress swap with
  zero errors; an in-flight request completing across a swap.
- `tests/unit/test_controller.py` — hot-swap never surfaces `LOADING`; cold
  start does.
- `tests/unit/test_daemon.py::...restores_persisted_active_model_over_default` —
  startup restores the last fully-activated model, not the default.
