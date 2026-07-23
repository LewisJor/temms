# Best-Feasible Model Control (target model)

This is the conceptual model TEMMS is being built around. It is the **target
direction**, tracked by issues
[#43](https://github.com/LewisJor/temms/issues/43),
[#44](https://github.com/LewisJor/temms/issues/44), and
[#45](https://github.com/LewisJor/temms/issues/45).
[Architecture](architecture.md) documents the *current* implementation (a
`default_model` plus a `fallback_chain`); this document describes what supersedes
it and why.

## The one idea

TEMMS keeps the **best feasible model** serving on an edge device at every
instant, and produces a signed proof of why each choice was the best feasible
one. It is a deterministic control loop, not a model server with a failover
list.

Three words carry the design:

- **Best** — a preference order says which model is *wanted* for the current
  conditions. Specialists are co-equal: a low-light model is not a degraded
  daylight model, it is the *right* model after dark.
- **Feasible** — a model is eligible only if it *can actually run right now*: its
  resource, thermal, and runtime requirements are satisfied by what the device
  has free. Feasibility is evaluated **before** selection, never discovered by a
  failed load.
- **Model** — the unit that serves. Models are a **portfolio of operating
  points**, not a primary with backups.

There is deliberately **no "fallback chain."** Degradation is not a separate
mechanism; it emerges from the same solve. When the wanted model is infeasible
(no memory, won't load, throttled), the next-best *feasible* model wins — not
because it is "backup #2", but because it is the best model that fits the world
as it is this tick. One mechanism replaces two.

## The operating point

Every tick, the device is at an **operating point** — a live state vector:

```
operating_point = conditions × resources × connectivity × observed_performance
```

- **conditions** — sensors and environment (light, visibility, weather).
- **resources** — memory, storage, thermal headroom, and — on a shared machine —
  what is left after co-resident packages (see *Co-residency*).
- **connectivity** — online / DDIL-offline.
- **observed_performance** — the currently-serving model watching itself (see
  *The closed loop*).

## Operating envelopes

Each model declares the region of the world it is **for** and what it **costs**:

```yaml
models:
  - id: yolov8-daylight
    provides: object-detection
    optimal_when: { light: bright, visibility_m: ">1000" }    # preference region
    requires:     { memory_mb: 512, thermal_headroom_c: ">5" } # feasibility
  - id: mobilenet-tiny
    provides: object-detection
    optimal_when: { }              # no preferred region — the floor
    requires:     { memory_mb: 96 }     # always feasible
```

`provides` decouples the *requirement* (a capability the mission needs) from the
*implementation* (which model, on which hardware). The same mission is satisfied
by different portfolios on a Pi vs a Jetson — the feasibility filter adapts. That
is the "any target" claim, expressed at the model layer.

## Best-feasible dispatch (the controller)

The mechanism is deliberately dumb — a safety property, not a limitation:

1. **Filter** to the feasible set (every `requires` satisfied by the operating
   point).
2. **Rank** the feasible set by preference (`optimal_when` match, then declared
   priority).
3. **Hold** through hysteresis (`min_dwell_s`) so a flapping signal cannot
   thrash.
4. **Serve** the top of the ranking; warm-before-serve, so no request is dropped.

No search, no optimisation, no ML in the loop. That determinism *is* the
safety-case and certification story. If this ever needs a solver, the design has
overreached.

## The closed loop (self-observation)

The serving model's own output feeds back into the decision. Deterministic
aggregations of inference output — rolling mean confidence, detection rate, class
distribution — become **conditions** in the same condition store as the sensors.
Observed performance is therefore part of the operating point that selects the
model: if the daylight model's mean confidence collapses in unexpected fog, that
is a deterministic condition, and the controller swaps.

This stays **non-agentic**: the output→condition mapping is a fixed aggregation
(`mean(confidence) < 0.4`), not a model judging what should run. A deterministic
rule over a measured signal decides — the signal is simply the model watching
itself.

**Output contract.** Where results go is declared, not implicit: a mission
declares its output sink (downstream consumer, bus, callback), so provenance
covers the *output*, not only the model.

## Evidence: a proof, not a log

Because feasibility and preference are explicit, every decision records *why*:
the operating point, the feasible set, the model chosen, and the models excluded
with their reason.

> `yolov8-lowlight` selected — conditions ∈ its envelope; `requires` satisfied
> (640 MB free ≥ 512); `yolov8-daylight` (higher preference) excluded: visibility
> 40 m < 1000. Output-derived: prior model mean confidence 0.31 < 0.40.

That is a **decision with a proof of best-feasible-under-constraint**, hash-linked
and signed (see [the evidence chain](evidence-chain.md)). No serving framework or
MLOps tool produces this, because none of them close the loop deterministically at
the edge.

## Co-residency (and the k3s boundary)

A machine may run several TEMMS packages at once. The boundary is strict:

- **TEMMS is the in-package controller** — which model serves *within* one
  package, and the proof.
- **An orchestrator (e.g. k3s) is the cross-package scheduler** — which package
  runs on which node, with resource limits. TEMMS does not reinvent this.

The only thing co-residency forces into the core model is that the **resource
axis is a shared budget**: a model's `requires` is checked against what is
actually free on the box, so co-resident packages cannot over-commit.

Export to k3s is then a compile target, not a rewrite: `mission.yaml` compiles to
a Deployment where `requires` becomes resource requests and the package becomes a
pod. Deferred, but the model is built to reach it.

## What this replaces

The earlier framing — a `default_model` plus a `fallback_chain`, with selection
over conditions only and feasibility discovered by a failed load — is retired. It
conflated two different things (which model is *right* vs what to do when the
right one *can't run*) and could not explain its own choices. Best-feasible
dispatch is one mechanism where the earlier design had two, and it turns the audit
trail from a log into a proof.
