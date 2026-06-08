# TEMMS Architecture Overview

TEMMS has two layers: Hub prepares artifacts before deployment, and the daemon
runs on the edge node.

Hub manages candidate models, packages models and policies, signs artifacts, and
runs targeted container tests for the runtimes or device profiles that will
consume them.

The daemon handles local adaptive inference control: condition collection,
policy evaluation, model activation, fallback, operator override, deployment
reconciliation, and API serving.

## Ownership boundaries
- `TEMMSHub`: model inventory, package assembly, signing, targeted container
  validation, and compatibility evidence.
- `TEMMSDaemon`: lifecycle loops, deployment state transitions, reconciliation, and orchestration.
- `InferenceRuntime`: model load/hot-swap/inference execution per slot.
- `PolicyEngine`: desired model decisions from conditions.
- `ConditionStore`: SQLite-backed state + history.

TEMMS does not implement model training, labeling, experiment tracking, or broad
fleet orchestration. It integrates with those systems and keeps the edge-node
decision loop working when they are unavailable.

## Desired vs actual state
Desired state is represented by deployment lifecycle (`deployment_state.json`), while actual state is derived from slot runtime health.
