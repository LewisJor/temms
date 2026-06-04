# Offline / DDIL Mode

When offline mode is enabled, control/deployment operations are accepted locally and buffered in `/var/lib/temms/pending_operations.json`.

The runtime continues serving with last-known-good loaded models and current slot state.

When online mode is restored, buffered operations can be replayed via `/v1/control/sync`.

Recommended hardening areas include authenticated local control, Hub-signed
package verification, signed intent verification, tamper-evident decision logs,
and evidence export. Fleet rollout orchestration and multi-node drift correction
can be handled by external control-plane integrations.
