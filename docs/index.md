# TEMMS Documentation

TEMMS is an edge runtime for adaptive inference control. It runs next to an
inference stack, evaluates local conditions, switches models, records evidence,
and keeps serving when connectivity is degraded or unavailable.

The docs are organized around the deployable pieces in this repository: the
edge daemon, Hub Lite model/package workflows, policy evaluation, offline
operation, and deployment validation.

```{toctree}
:maxdepth: 2
:caption: Start Here

QUICKSTART
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
policy-reference
```
