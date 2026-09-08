# Architecture

TEMMS has one orchestration path:

```text
context + runtime models + runtime state
                  │
                  ▼
            pure Selector
                  │
                  ▼
               Decision
                  │
          selected model differs
                  ▼
          Runtime.activate()
                  │
                  ▼
          Runtime.state() check
```

## Ownership

- **Application:** decides when to reconcile and supplies context.
- **Selector:** chooses a model without side effects.
- **Runtime:** owns model access, active state, activation, and inference.
- **TEMMS:** connects the selector to the runtime and enforces the contract.

## Non-goals

The kernel has no registry, package manager, HTTP server, daemon, database,
condition store, UI, fleet orchestration, or cryptographic evidence subsystem.
Those may exist as independent adapters after the core interface proves useful.

## Complexity budget

- Zero core runtime dependencies.
- Two public protocols: `Runtime` and `Selector`.
- One orchestration class: `TEMMS`.
- No compatibility facade for the previous prototype.
- No abstraction without two concrete consumers.
