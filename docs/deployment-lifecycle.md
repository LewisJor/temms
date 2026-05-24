# Deployment Lifecycle

Deployment states:
- PENDING
- DOWNLOADING
- READY
- FAILED
- OFFLINE
- DEGRADED

State is persisted to `/var/lib/temms/deployment_state.json` and transitions are logged by the daemon reconciliation loop.

Reconciliation evaluates current slot states and offline mode to explicitly transition state.
