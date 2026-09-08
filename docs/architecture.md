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
             selected model differs
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
- **TEMMS:** serializes reconciliation per slot and enforces the runtime contract.

The runtime, not TEMMS, is the authority for which model is active.

## Concurrency contract

One `TEMMS` instance owns reconciliation for each controlled runtime slot.
Same-slot reconciliations are serialized. Cross-process ownership is outside the
kernel and must not be attempted without an application-provided coordinator.

An inference request is pinned to the model instance captured at admission. A
concurrent activation may change later requests but cannot change the in-flight
request's model or attribution.

## Non-goals

The kernel has no registry, package manager, HTTP server, daemon, database,
condition store, UI, fleet orchestration, or cryptographic evidence subsystem.
Runtime-specific dependencies live only in adapters.

## Complexity budget

- Zero required core runtime dependencies.
- At most six direct core modules.
- Two public protocols: `Runtime` and `Selector`.
- One orchestration class: `TEMMS`.
- No core subpackages except `adapters`.
- No compatibility facade for the previous prototype.
- No abstraction without two concrete consumers.
