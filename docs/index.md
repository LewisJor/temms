# TEMMS Documentation

TEMMS is an optimized edge model management system for **DDIL** environments
(Denied, Disrupted, Intermittent, Limited connectivity). It is a deterministic,
auditable control plane that runs next to an inference stack, evaluates local
conditions, switches models under policy, records signed evidence, and keeps
serving when connectivity is degraded or unavailable.

Start with **[Direction](direction.md)** for the north star and the
integrate-vs-build boundary. The rest of the docs are organized around the
deployable pieces: the edge daemon, Hub Lite model/package workflows, policy
evaluation, offline operation, and deployment validation.

```{toctree}
:maxdepth: 2
:caption: Start Here

direction
QUICKSTART
demo-runbook
functional-testing
product-summary
architecture-overview
architecture
```

```{toctree}
:maxdepth: 2
:caption: Runtime and Operations

edge-operations
offline-ddil-mode
swap-contract
reliability
deployment-lifecycle
operational-notes
run-on-linux-vm
```

```{toctree}
:maxdepth: 2
:caption: Hub, Packages, and Policies

hub-lite
mlflow-packaging
package-signing
trust-store
evidence-chain
proof-canonicalization
policy-reference
```
