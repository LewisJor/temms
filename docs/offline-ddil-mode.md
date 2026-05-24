# Offline / DDIL Mode

When offline mode is enabled, control/deployment operations are accepted locally and buffered in `/var/lib/temms/pending_operations.json`.

The runtime continues serving with last-known-good loaded models and current slot state.

When online mode is restored, buffered operations can be replayed via `/v1/control/sync`.

Future Hub/control-plane responsibilities (out of scope): fleet rollout orchestration, multi-node drift correction, signed remote intent distribution.
