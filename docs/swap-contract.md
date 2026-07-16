# TEMMS Model Hot-Swap Contract

**Contract version:** `temms-swap-contract/v1`
**Scope:** the daemon-side behavior of activating a model in a slot while that
slot may be serving inference. Hub and UI are out of scope.

This document is normative. It is written so that a future native
(C++/Rust) daemon can implement the same behavior from this document alone,
without reading the reference Python implementation. Where it names Python
symbols it does so only to anchor the reference implementation
(`src/temms/inference/runtime.py`, `src/temms/controller.py`,
`src/temms/daemon/service.py`, `src/temms/slots/manager.py`); the *behavior*, not
the symbol, is the contract.

Requirement keywords (MUST, MUST NOT, SHOULD, MAY) are used in the RFC 2119
sense.

## Status of tiers

| Tier | Topic | Status |
|------|-------|--------|
| 1 | In-flight swap semantics | Ratified |
| 2 | Switch hysteresis | Planned |
| 3 | Memory admission control | Planned |
| 4 | Crash recovery | Planned |

## Definitions

- **Slot** — a named, long-lived serving position that holds at most one
  *active* model at a time (e.g. `vision`). A slot has an operational state:
  `stopped`, `loading`, `running`, `error`.
- **Model instance** — a loaded, executable copy of a model in memory, bound to
  a runtime session. Distinct activations of the same model id are distinct
  instances.
- **Active model** — the model instance a slot currently serves inference from.
- **Swap** — replacing a slot's active model instance with another.
  - **Cold start** — a swap where the slot has no active model yet.
  - **Hot-swap** — a swap where the slot already has an active model that is
    (or may be) serving inference.
- **In-flight request** — an inference request that has been admitted (bound to
  a model instance) but has not yet returned.
- **Warm / warmed** — a model instance that has successfully executed at least
  one inference (real or synthetic) since being loaded, so the first
  client-visible request does not pay a first-inference initialization cost.

## Tier 1 — In-flight swap semantics

### Goal

A hot-swap MUST be invisible to inference callers except for which model
answers: no request is dropped or errored merely because a swap is in progress,
and every request is answered by exactly one fully-loaded model instance.

### Invariants

1. **No unavailability window (I-AVAIL).** While a slot has any loadable model
   available, an admitted inference request MUST NOT fail with a
   "no model / not running / loading" error because a swap is underway. During a
   hot-swap the previous model MUST keep serving until the new model is ready to
   take over.
2. **Atomic cutover (I-ATOMIC).** The transition of the active model instance
   MUST be atomic with respect to request admission: a request is admitted
   against either the old instance or the new instance, never a partially
   constructed one, and never "neither".
3. **In-flight completion (I-DRAIN).** A request admitted against a model
   instance MUST be allowed to complete on that same instance. A retired
   instance MUST NOT be unloaded until its in-flight count reaches zero.
4. **Warm-before-serve (I-WARM).** A model instance MUST become the active
   instance only after it has been loaded and a warmup inference has been
   attempted. Warmup is best-effort: if a synthetic warmup input cannot be
   constructed or is rejected by the model, activation still proceeds (the first
   real request pays the cold cost, and inference-time fallback covers a broken
   model). Warmup MUST NOT run against the currently-serving instance.
5. **Attribution (I-ATTR).** The decision log MUST record every completed swap
   with `from_model` and `to_model` equal to the instances actually swapped.
   Inference results are served by whichever instance was active at admission;
   the runtime exposes that served model id for callers that need per-request
   attribution.
6. **Failed swap preserves service (I-KEEP).** If loading or warming the new
   model fails, the slot MUST continue serving the previous model (or a
   fallback), and MUST NOT be left in a state where a still-loaded model is
   unreachable.

### Required sequence for a hot-swap

Given slot `S` currently serving instance `M_old`, activating model `M_new`:

1. **Preflight** (optional, policy-defined) MAY reject the activation before any
   load. Rejection leaves `M_old` serving unchanged.
2. **State** — the slot MUST remain `running` throughout a hot-swap. The
   `loading` state is reserved for cold start (no active model to serve). This
   is what upholds I-AVAIL: request admission gates on slot state, so a hot-swap
   must not flip the slot out of `running`.
3. **Load** `M_new` into a new instance without disturbing `M_old`. This step
   MAY be skipped if `M_new` was preloaded.
4. **Warm** `M_new` (I-WARM) before it is published as active.
5. **Cutover** — under a short mutual-exclusion region, publish `M_new` as the
   active instance and mark `M_old` retired. Admissions before this region bind
   to `M_old`; admissions after bind to `M_new` (I-ATOMIC).
6. **Drain + unload** — unload `M_old` only once its in-flight count is zero
   (I-DRAIN). If it had zero in-flight requests at cutover, it MAY be unloaded
   immediately; otherwise the last completing request performs the unload.
7. **Record** the swap in the decision log (I-ATTR).

On failure at steps 3–4, the swap is abandoned: `M_old` keeps serving, and the
controller MAY attempt a fallback chain (each fallback candidate follows the
same sequence). Only if the selected model and all fallbacks fail does the slot
transition to `error` — and only when no usable instance remains (I-KEEP).

### Request admission and draining

- Admission MUST, under the slot's mutual-exclusion region, (a) read the current
  active instance and (b) increment that instance's in-flight counter, then
  release the region before executing the model. Execution MUST NOT hold the
  region (so a concurrent swap is not blocked by a slow inference).
- On completion, the request MUST decrement the instance's in-flight counter
  under the region and, if the instance is retired and its counter reached zero,
  unload it.
- If a slot has no loaded instance at admission (only legitimate during cold
  start or after a total failure), the request fails with a retryable error.

### Conformance tests (reference)

`tests/unit/test_swap_contract.py`:

- `test_load_warms_model_before_serving` — I-WARM.
- `test_infer_attributes_to_currently_loaded_model` — I-ATOMIC/I-ATTR: after a
  completed swap, a request carrying the stale expected model id is served by,
  and attributed to, the new model.
- `test_no_error_window_while_swap_in_progress` — I-AVAIL: 25 concurrent
  requests fired while a swap is blocked mid-load are all served by the old
  model with zero errors; requests after cutover are served by the new model.
- `test_in_flight_request_drains_before_old_model_unload` — I-DRAIN: the old
  instance is not unloaded until an in-flight request against it completes.

`tests/unit/test_controller.py`:

- `test_hot_swap_never_surfaces_loading_state` — I-AVAIL at the controller
  level: a hot-swap never flips the slot to `loading`.
- `test_cold_start_uses_loading_state` — cold start still passes through
  `loading`.
