# Offline / DDIL Mode

When offline mode is enabled, control/deployment operations are accepted locally and buffered in `/var/lib/temms/pending_operations.json`.
If the daemon has `TEMMS_PACKAGE_SIGNING_KEY` or
`TEMMS_PACKAGE_SIGNING_KEY_FILE` configured, each buffered operation is written
as a signed DDIL intent using a canonical HMAC-SHA256 envelope.

The runtime continues serving with last-known-good loaded models and current slot state.

Offline mode is also published into the condition store so local policies can
adapt without Hub connectivity:

- `operational.connectivity.offline`
- `operational.connectivity.mode`
- `operational.connectivity.network_available`

When online mode is restored, buffered operations can be replayed via `/v1/control/sync`.
When signature policy is enabled, sync verifies every pending-operation
signature before replay. A missing or tampered signature returns HTTP `409` and
leaves the queue intact.
Evidence summaries also preflight the queue: each pending operation reports a
verification status such as `verified`, `invalid`, `missing_signature`, or
`key_unavailable`, plus non-secret signature metadata like signer, key
fingerprint, and canonical payload digest.
The same dry-run plan is available directly from `/v1/control/sync/preview`.
It validates signatures, operation shape, slot existence, and model
availability without mutating runtime state. `/v1/control/sync` refuses blocked
preflights with HTTP `409` and leaves the queue intact.
Deploy intents that carry Hub context are also checked against Hub Lite
deployment readiness before replay. When a queued deploy names or implies
`package_id`, `device_id`, and `runtime_target_id`, preflight calls the same
readiness gates used by mission package planning. Runtime target mismatch,
active runtime drift, stale or failing performance proof, resource-envelope
violations, and selected-edge blockers stop replay until the operator fixes the
edge state or clears the queue. A
rollout-gate warning by itself does not block replay; direct field deploys can
still sync when the model/package/runtime/device evidence is otherwise valid.
Runtime optimizer attention is carried through as an advisory instead of a hard
block. If a queued deploy is safe but pinned to a lower-scoring runtime target,
preflight keeps the plan ready with `replay_status:
ready_with_runtime_advisory`, increments `optimization_advisories`, and records
the Runtime optimizer gate refs so the DDIL ledger can show the better target
before sync. The same preflight entry carries the selected runtime capability
lock, capability digest, and edge heartbeat freshness; stale or missing
telemetry makes the lock `blocked` and prevents DDIL replay until the edge
reports fresh on-device inventory. A queued deploy is replayed as-is: if the
pinned target is no longer eligible, the intent stays blocked until the
operator fixes the edge evidence or clears the queue, then syncs again.
The same inspection can be run from the edge CLI without hand-written HTTP:

```bash
uv run temms control sync-preview --control-url http://127.0.0.1:8080 --json
uv run temms control sync --control-url http://127.0.0.1:8080
```

Deploy context is normalized from either top-level payload fields or a nested
`request` object before preview, replay, operator override, slot activation,
telemetry, and audit metadata are written.
When multiple valid model-activation intents target the same slot, preflight
keeps the replay plan ready but annotates older entries as `superseded` and
reports `slot_outcomes` so operators can see the model that will ultimately be
active after replay. Sync uses that preflight plan to skip superseded
activations, emits a compact `pending_operations.superseded_skipped` telemetry
event for audit, and only loads the winning model for each slot.
Blocked entries stay in the pending queue until the cause is fixed:
`/v1/control/sync/preview` shows each blocked intent with its blocking reason,
sync keeps refusing replay while the blocker remains, and the next sync replays
the intent once the operator remediates the model, slot, runtime, or edge
evidence.
Condition updates are re-applied to the condition store. Deploy intents that
include both `slot` and `model_id` load that model, record an operator override
with `source=deploy_sync`, and activate the slot during sync so the policy loop
does not immediately revert the requested deployment. If the slot or model
cannot be found, sync refuses to clear the pending queue so the operator can
recover instead of losing the intent. If Hub readiness blocks the deploy, the
preflight entry includes `hub_readiness_status`, selected deployment context,
and compact blocking/attention gate refs so the evidence bundle can show
why the edge should not run that model/runtime.
If replay fails after earlier entries were already consumed, TEMMS atomically
rewrites the active queue to keep the failing entry and anything after it while
dropping entries that were already applied or superseded-skipped. The retry path
therefore resumes from the failed operation instead of reapplying the whole
offline buffer. A compact `pending_operations.partial_replay_failed` telemetry
event records failed index, consumed count, remaining count, replayed count, and
skipped count when telemetry is configured.

The `temms control` CLI drives the same flow: `temms control offline`
switches the daemon into offline mode, `temms control deploy` buffers a local
deploy intent for the selected model, `temms control online` returns
connectivity,
and `temms control sync` replays the buffered operations. DDIL readiness reports connectivity mode,
deployment state, pending operation count, active slot/model, latest proof
events, and a compact pending-operation ledger from the evidence summary. Each
ledger row includes operator-facing identifiers, verification status, and a
canonical payload digest instead of exposing the full buffered payload. Healthy
signed queues show `verified intent` and `ready to replay`; tampered or
unreplayable queues show the blocking reason before sync is attempted. Stacked
valid deploys to the same slot show the earlier row as a `superseded intent`
and point to the later model that wins after replay; sync skips that superseded
activation instead of loading the older model first. A blocked intent stays in
the pending ledger with its target, signature state, digest, and replay-block
reason until the operator fixes the cause and syncs again, or clears the queue
when the intent should not be replayed.

Recommended remaining hardening areas include authenticated local control,
tamper-evident decision logs, and expanded evidence export. Fleet rollout
orchestration and multi-node drift correction can be handled by external
control-plane integrations.
