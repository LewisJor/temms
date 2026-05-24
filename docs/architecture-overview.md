# TEMMS Architecture Overview

TEMMS edge node runs a single daemon process that owns condition collection, policy evaluation, deployment reconciliation, and API serving.

## Ownership boundaries
- `TEMMSDaemon`: lifecycle loops, deployment state transitions, reconciliation, and orchestration.
- `InferenceRuntime`: model load/hot-swap/inference execution per slot.
- `PolicyEngine`: desired model decisions from conditions.
- `ConditionStore`: SQLite-backed state + history.

## Desired vs actual state
Desired state is represented by deployment lifecycle (`deployment_state.json`), while actual state is derived from slot runtime health.
