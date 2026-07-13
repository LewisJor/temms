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
readiness gates used by Mission Package Workbench. Runtime target mismatch,
active runtime drift, stale or failing performance proof, resource-envelope
violations, and selected-edge blockers stop replay until the operator fixes the
edge state, chooses a compatible target, or quarantines the intent. A
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
reports fresh on-device inventory. If the pinned target is not eligible or trails
a measured compatible target, operators can repair the buffered intent through
`POST /v1/control/sync/retarget-runtime`. Retargeting is intentionally strict:
the requested target must appear in Hub target assessments as the best eligible
runtime, carry non-dry-run validation evidence, carry benchmark evidence, and
have a locked runtime capability hash for fresh edge inventory. The daemon
rewrites only the selected deploy payload, records a signed
`_temms_runtime_retarget` audit entry with the previous target, new target,
actor, reason, prior payload digest, and runtime target proof, and re-signs the
pending operation when DDIL signatures are configured. That proof also carries
the canonical runtime workbench fields: previous selected target, proved selected
target, best target, target-selection status, eligible/blocked target counts,
selected-is-best, and `target_assessment_sha256`, a compact digest over the
proved target metadata, runtime lane, artifact lane, capability digest,
validation id, and benchmark id. When the repaired intent is later synced,
replay recomputes Hub readiness and blocks the intent if the
signed retarget proof no longer matches the live target-assessment digest,
capability hash, validation id, benchmark id, best-target status, or
eligibility. Successful activation decisions preserve the same
retarget record under `ddil_runtime_retarget`, so evidence exports and mission
replay can prove the on-device runtime was repaired even after the active queue
is empty. Evidence summary timelines render those replayed activations as
`DDIL replay retargeted <old-runtime> -> <new-runtime>`, and mission replay
classifies them under the offline-operation phase.
The same repair can be run from the edge CLI without hand-written HTTP:

```bash
uv run temms control sync-preview --control-url http://127.0.0.1:8080 --json
uv run temms control retarget-runtime \
  --control-url http://127.0.0.1:8080 \
  --payload-sha256 <pending-payload-sha256> \
  --actor operator:edge-runtime-drill \
  --reason "selected measured compatible on-device runtime"
uv run temms control sync-preview --control-url http://127.0.0.1:8080
uv run temms control sync --control-url http://127.0.0.1:8080
```

Omit `--runtime-target-id` to let the daemon use the Runtime optimizer's
measured candidate, or include it when the operator is deliberately pinning a
specific runtime target.
Deploy context is normalized from either top-level payload fields or a nested
`request` object before preview, replay, operator override, slot activation,
telemetry, and audit metadata are written.
When multiple valid model-activation intents target the same slot, preflight
keeps the replay plan ready but annotates older entries as `superseded` and
reports `slot_outcomes` so operators can see the model that will ultimately be
active after replay. Sync uses that preflight plan to skip superseded
activations, emits a compact `pending_operations.superseded_skipped` telemetry
event for audit, and only loads the winning model for each slot.
Operators can move blocked entries out of the active queue with
`/v1/control/sync/quarantine-blocked`. Quarantine writes the full original
entry plus preflight reason to the local
`pending_operations_dead_letter.json` ledger, removes only the blocked entries
from `pending_operations.json`, and leaves replay-ready entries available for
sync. When the edge issue is remediated, operators can move one or more
quarantined records back into the active DDIL queue with
`/v1/control/sync/requeue-dead-letters`; by default the daemon first runs the
original signed intent through current DDIL preflight and restores it only when
the model, slot, runtime target, Hub readiness, and edge capability checks are
ready. Restored records are marked with `requeued_at`, `requeued_by`, and
`requeue_reason`, and duplicate active entries for the same payload digest are
refused. Operators can pass `force: true` for break-glass drills, but normal
field recovery should leave still-blocked candidates in quarantine. Truly
unrecoverable records can be acknowledged with
`/v1/control/sync/acknowledge-dead-letters`; acknowledgement marks the
dead-letter record as handled without deleting the forensic payload or digest.
Condition updates are re-applied to the condition store. Deploy intents that
include both `slot` and `model_id` load that model, record an operator override
with `source=deploy_sync`, and activate the slot during sync so the policy loop
does not immediately revert the requested deployment. If the slot or model
cannot be found, sync refuses to clear the pending queue so the operator can
recover instead of losing the intent. If Hub readiness blocks the deploy, the
preflight entry includes `hub_readiness_status`, selected deployment context,
and compact blocking/attention gate refs so the UI and evidence bundle can show
why the edge should not run that model/runtime.
If replay fails after earlier entries were already consumed, TEMMS atomically
rewrites the active queue to keep the failing entry and anything after it while
dropping entries that were already applied or superseded-skipped. The retry path
therefore resumes from the failed operation instead of reapplying the whole
offline buffer. A compact `pending_operations.partial_replay_failed` telemetry
event records failed index, consumed count, remaining count, replayed count, and
skipped count when telemetry is configured.

The Hub product cockpit exposes the same flow in `/ui/hub`: **Link loss**
switches the daemon into offline mode, **Queue intent** buffers a local deploy
intent for the currently selected model, **Restore link** returns connectivity,
and **Sync pending** replays the buffered operations. The DDIL readiness band shows connectivity mode,
deployment state, pending operation count, active slot/model, latest proof
events, and a compact pending-operation ledger from the evidence summary. Each
ledger row includes operator-facing identifiers, verification status, and a
canonical payload digest instead of exposing the full buffered payload. Healthy
signed queues show `verified intent` and `ready to replay`; tampered or
unreplayable queues show the blocking reason before sync is attempted. Stacked
valid deploys to the same slot show the earlier row as a `superseded intent`
and point to the later model that wins after replay; sync skips that superseded
activation instead of loading the older model first. When a
blocked intent has a measured runtime alternative, the row exposes **Use best
runtime** to retarget the queued deploy and refresh the signed DDIL proof in
place. If the intent is still unrecoverable, the Hub exposes **Quarantine
blocked** so the active queue can recover without losing the bad intent's
forensic record. Quarantined entries remain visible in a compact Hub
dead-letter ledger with target, signature, digest, and replay-block reason
until the operator either clicks **Requeue intent** after fixing runtime or
inventory proof, or **Acknowledge quarantine** after deciding the intent should
not be replayed. Requeue is safe-by-default: if the refreshed preflight still
blocks, the row remains quarantined and the response names the current blocking
reason. Requeued and acknowledged records are removed from the active readiness
panel but remain in evidence exports with recovery metadata.
Mission replay treats retained, requeued, or acknowledged DDIL quarantine as completed
offline-operation proof because the system preserved the intent, recovered the
active queue, and recorded operator review.

Recommended remaining hardening areas include authenticated local control,
tamper-evident decision logs, and expanded evidence export. Fleet rollout
orchestration and multi-node drift correction can be handled by external
control-plane integrations.
