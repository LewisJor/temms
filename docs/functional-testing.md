# Functional Testing

Use this checklist when validating TEMMS locally before an industry or field
demo. It focuses on the product path: signed model inventory, compatible
on-device runtime target, sensor/model handling policy, mission package
handoff, rollout approval, activation controls, and evidence export.

## 1. Run The Server Regression Suite

Run the focused server regression suite:

```bash
uv run pytest tests/unit/test_server.py -q
```

## 2. Seed A Real Demo Workspace

For the normal Docker demo stack, Hub is seeded automatically with a signed,
released example package, an online simulated edge node, a passing runtime
validation, and fresh benchmark records for each packaged model. Each Docker
demo seed refreshes only its own synthetic benchmark rows for the selected
device/package/model/runtime, so a restarted local demo does not inherit stale
edge evidence. Operator-uploaded benchmark records are left untouched. Docker
demo mode also gives `edge-sim` a deterministic healthy memory/storage envelope
on heartbeat while leaving runtime/provider detection real, so the first
mission package plan starts on the deployable path instead of depending on
the Docker VM's transient free-memory number. Use the manual canonical
workspace when you want the full offline/fallback/rollback/evidence loop in a
local foreground daemon:

```bash
DEMO_ROOT=/tmp/temms-functional-demo

uv run python scripts/canonical_product_demo.py \
  --work-dir "$DEMO_ROOT" \
  --evidence-output "$DEMO_ROOT/temms-canonical-evidence.json" \
  --json-summary
```

The JSON summary now includes `work_dir`, `daemon_config`,
`daemon_start_command`, and `hub_url`. Copy the `work_dir` value when you want
to inspect artifacts directly:

```bash
RUN_DIR=/tmp/temms-functional-demo/run-YYYYMMDDHHMMSSffffff
```

The script writes the daemon config for you at `$RUN_DIR/temms-demo.yaml`. Start
the foreground daemon with the generated `daemon_start_command`, or run the same
command manually:

```bash
env TEMMS_PACKAGE_SIGNING_KEY=canonical-demo-secret \
  TEMMS_DEVICE_ID=edge-demo \
  TEMMS_DEVICE_PROFILE=x86_64-cpu \
  TEMMS_EDGE_HEARTBEAT_INTERVAL_S=10 \
  uv run temms daemon start --foreground \
  --host 127.0.0.1 \
  --port 18080 \
  --config "$RUN_DIR/temms-demo.yaml"
```

The pinned device identity makes the local foreground daemon refresh the seeded
`edge-demo` heartbeat instead of creating an unrelated host-named edge node.
It does not fake runtime support: if the machine running the daemon lacks the
declared provider or capability, the proof gate should remain blocked and show
the runtime/capability gap.

Point the CLI and curl checks below at `http://127.0.0.1:18080`.

## 3. Product Contract Smoke Test

Exercise the product path against the seeded daemon with the CLI and API. The
flow follows **Mission -> Model Plan -> Runtime Fit -> Sensor Handling ->
Package Handoff -> Edge Deploy -> Field Ops**: define the mission goal or YAML,
choose the signed model, rank and prove the runtime, set switching and DDIL
policy, plan and download the mission package, stage the rollout, and finish
with DDIL state plus evidence export. `temms hub mission-package-plan` should
populate sensor, slot, latency/throughput SLO, switch policy, fallback model,
and DDIL behavior from a mission YAML. When the YAML carries `model_id`,
`package_id`, `device_id`, or `runtime_target_id` values that match Hub
inventory, the selected model/runtime/edge path should hydrate from the spec
before package planning. The execution contract for the selected path
should show selected model artifact, target runtime, edge node, fit score,
runtime lane, artifact path, SLO/resource evidence, admission state, ranked
measured runtime candidates, and target runtime coverage for every known
runtime target, including blocked Jetson/Orin/RPi/TFLite-style lanes and the
exact capability or evidence gap that made them ineligible for the selected
edge. Each runtime lane should include a compact remediation record such
as use matching edge class, validate runtime, record edge benchmark, or use for
field apply, plus a command payload when the execution contract carries an
operator or edge-local remediation command for that lane. Edge-run actions such
as benchmark collection or heartbeat refresh should be explicitly marked
`requires_edge_execution` so the operator does not mistake them for safe
central mutations. Each lane should also carry compact component proof states
for compatibility, validation, performance, resource, and telemetry. Use
`temms hub edge-runtime-mission` and `temms hub verify-edge-proof` for the
runtime proof lane, inspecting the same `temms-edge-runtime-proof/v1` JSON
envelope from Hub.

Expected state with the Docker Hub seed:

- `POST /v1/hub/mission-package/plan` should return a
  `temms-edge-mission-package/v1` payload. The payload should
  include mission, selection, SLO, model handling, DDIL policy, runtime plan,
  proof gate, `deployment_intent`, `edge_handoff`, component digests, and
  `integrity.payload_sha256`. The `edge_handoff` block should use schema
  `temms-edge-mission-package-handoff/v1`, mode `stage_approve_apply`, and
  include package stage, rollout approval, rollout apply, and digest-verification
  commands for the edge operator. Package planning is advisory so operators can plan while readiness is still
  `attention`; proof generation and proof download stay strict. The
  same endpoint should derive missing `package_id`, `model_id`, `device_id`,
  `runtime_target_id`, `slot`, SLO, handling, and DDIL fields from
  `mission_yaml` when the YAML carries them, while explicit JSON fields still
  take precedence.
- `POST /v1/hub/mission-package/download` should return a
  `temms-edge-mission-package-*.json` attachment and expose the package
  identity, payload, runtime-plan, deployment-intent, and edge-handoff hashes in
  its response headers, with the preserved `edge_handoff`
  runbook matching the package body. Repeated plan/download
  calls may produce different payload hashes because artifact observation time
  changes, but they must keep the same package identity hash for the same
  mission/model/runtime/device policy.
- `POST /v1/hub/mission-package/stage` should create a rollout from the
  planned package/model/device/runtime/slot, require approval and runtime
  validation, and use the package payload's
  `deployment_intent.command.body`. The deployment intent should also carry the
  exact `mission_contract_sha256`, `runtime_capability_lock_sha256`, and
  `runtime_plan_sha256`, and staging should reject an artifact whose intent
  points at a different mission-contract, capability-lock, or runtime-plan
  digest. The rollout reason should include the mission package identity digest.
  Staging requires a planned package, so the rollout is tied to the hashed
  deployment intent rather than a draft preview.
- Model inventory shows three signed vision models: daylight, lowlight, and
  mobilenet-tiny.
- Generated proofs for the seeded path should pass gate policy
  `go + best runtime + capability lock + fit >= 95 + proof <= 15m + path bound`.
  A generated proof payload exposes
  `integrity.payload_sha256` and `runtime_capability_lock.capability_sha256`.
  For a current proof, the trace should report
  `trace consistent`, schema `temms-runtime-decision-trace/v1`, the ranked
  target count, remediation command count, and
  agreement with `runtime_workbench`. The execution manifest should report schema
  `temms-edge-execution-manifest/v1`, selected runtime image, runtime lane,
  capability-lock digest, validation and benchmark ids, best-target status, and
  gate admission policy. The proof should also carry
  `component_digests.schema_version:
  temms-edge-runtime-proof-component-digests/v1` with separate hashes for
  `runtime_workbench`, `runtime_decision_trace`, and
  `edge_execution_manifest`. Proof download responses should include the
  selected proof
  filename, payload hash, gate status, attestation state, and workbench, trace,
  and manifest header digests matching the proof body, so the
  `verify-edge-proof` command can validate signature, best-runtime selection,
  capability lock, runtime fit, proof freshness, and exact path binding locally
  without contacting Hub.
- A locked capability proof must be backed by fresh edge inventory telemetry.
  If the edge heartbeat is stale or missing, `runtime_capability_lock.status`
  should be `blocked`, its failures should name the heartbeat freshness gap,
  and strict proof generation or local verification with `--require-capability-lock`
  should fail closed.
- The edge execution contract carries the selected model -> runtime -> edge
  path, the runtime decision action such as `apply or stage`, `use best
  runtime`, or `collect evidence`, top measured runtime candidates, and full
  target runtime coverage with selected/best/blocked status plus per-target
  remediation guidance and component proof states.
- `/v1/hub/readiness`, `/v1/hub/edge-runtime-proof`, downloaded proof JSON, and
  runtime-fit evidence exports should include
  `edge_execution_contract.schema_version:
  temms-edge-execution-contract/v1`, including `target_assessments` and each
  assessment's remediation command payloads.
- `/v1/hub/readiness`, `/v1/hub/edge-runtime-proof`, and downloaded proof JSON
  should also include `runtime_workbench.schema_version:
  temms-runtime-workbench/v1` with the selected target, best target, target
  count, selected-is-best flag, capability-lock status, validation id, benchmark
  evidence, telemetry state, and blocked target rows. The runtime decision
  trace should expose each target's rank, selected/best state, proof component
  states, retained capability digest, blocker reason or penalty, and copyable
  operator or edge-run remediation command from the same contract.
- `/v1/hub/edge-runtime-proof` and downloaded proof JSON should include
  `edge_execution_manifest.schema_version:
  temms-edge-execution-manifest/v1`. The manifest should be part of the signed
  proof payload and retain the exact package/model/device/runtime path, runtime
  image, runtime lane/provider context, artifact fit, capability lock digest,
  validation id, benchmark id, gate policy, gate status, and selected
  remediation command for the chosen runtime target.
- A blocked or advisory pending replay should preserve the
  target-assessment remediation command in the evidence summary as
  `runtime_remediation_contract_command_text`, with
  `runtime_remediation_contract_kind` identifying operator versus edge-local
  execution and `runtime_remediation_contract_requires_edge_execution` making
  edge-only actions explicit. Evidence exports should retain the queued
  runtime, best measured runtime, runtime-fit score, capability
  lock, validation id, benchmark id, target coverage, and replay source after
  sync.
- Selecting a different model changes the deployment context. If you select
  `yolov8-lowlight`, compatibility preview, rollout creation, rollout apply,
  and DDIL queueing should all carry `model-yolov8-lowlight-001`, not just the
  package ID.
- `/v1/hub/readiness` gives a single go/attention/blocked status
  and lists eight gates: model package, runtime target, performance fit,
  resource envelope, edge target, rollout gate, DDIL queue, and evidence chain.
  Each gate should include a
  short state and the next operator action when it is not ready.
- With the seeded data, `yolov8-lowlight` should show a green `go` verdict with
  no remediation actions. `mobilenet-tiny` should show `attention` because it
  has no selected-model rollout yet; the rollout gate should expose a
  create-rollout action. The mobilenet readiness API
  response should include action `refs` for package, model, device, runtime
  target, slot, and approval defaults.
- Executable readiness actions should include a `command` object with HTTP
  method, API path, and any suggested body. For example, mobilenet's
  create-rollout action should point to `POST /v1/hub/rollouts` with
  the selected model/device/runtime body
  and `actor: "operator:readiness-remediation"` for audit history. Rollout
  remediation bodies should include deterministic `rollout_id`
  values so retrying the same command does not create duplicate
  records, plus a `reason` explaining the readiness gate remediation.
- Benchmark remediation should not be centrally executable.
  A missing or stale SLO benchmark action should expose
  `requires_edge_execution: true`, show the exact `temms benchmark ... --hub-url`
  command for the selected device/package/model/runtime, and leave the central
  POST body as an inspection envelope only. Runtime-lane remediation rows should
  expose the same contract-carried edge-local command and
  label it as edge-run, so clients cannot publish synthetic
  performance proof centrally.
- The deployment path shows model, runtime, runtime lane, edge, runtime fit,
  resource envelope, rollout, and evidence status together. The on-device fit
  band should name live runtime inventory, runtime-target requirements, the
  selected execution lane such as CPU portable, Jetson CUDA, Raspberry Pi 5
  TFLite, or Orin TensorRT, declared performance SLO, resource envelope,
  validation or benchmark proof, and any missing runtime/provider/accelerator/
  resource telemetry. If
  a runtime target
  requires something the selected edge inventory does not report, readiness
  should block before rollout assignment. After a rollout is `imported` or
  `activated`, if the edge loses a required runtime, ONNX provider, or
  accelerator, the runtime target gate should surface `runtime drift`, include
  runtime failure refs plus rollout id/state, and expose a reviewed rollback
  command. If a sibling model is already proven on another compatible runtime
  target for the same edge, the drift gate should expose **Stage fallback
  model** with that fallback runtime target and preserved runtime-validation
  proof. If the edge heartbeat is stale, runtime target, resource envelope, and
  edge target gates should move to attention and show the heartbeat age plus the
  freshness budget; the runtime capability lock should also move to blocked so
  stale inventory cannot certify the selected runtime path. If a model declares
  `performance_slo`,
  the performance fit gate should stay attention until a fresh benchmark for
  the selected model/device/runtime meets the p95 latency and throughput
  budget. Benchmark evidence defaults to a 24-hour freshness budget unless the
  model SLO declares `max_benchmark_age_seconds`; stale benchmark proof should
  show as attention and include benchmark age plus the freshness budget.
  Runtime recommendations and `/v1/hub/readiness` should expose
  `runtime_fit.schema_version: temms-runtime-fit/v1` with a 0-100 score,
  component scores for compatibility, runtime validation, performance,
  resource, and telemetry, plus reasons/penalties. `runtime_fit.runtime_lane`
  should expose `schema_version: temms-runtime-lane/v1`, `lane_id`, label,
  execution engine, acceleration path, providers, accelerators, and optimization
  goal so operators can distinguish CPU fallback, CUDA, TFLite, and TensorRT
  paths without opening the target record. `runtime_fit.artifact_lane` should
  expose `schema_version: temms-artifact-lane/v1` and show whether the selected
  model artifact is native, convertible, or mismatched for the selected runtime
  lane. ONNX on CPU/CUDA should read as native, ONNX on Orin/TensorRT may read
  as a conversion path until a built engine is validated, and ONNX-only
  artifacts should be blocked on Raspberry Pi/TFLite lanes until a TFLite
  artifact is packaged. If multiple compatible runtime
  targets exist for the selected model/device and no runtime target is
  requested, readiness should select the highest-scoring measured target.
  If a lower-scoring runtime target is pinned explicitly, readiness should keep
  that selected context but expose `runtime_fit.target_selection.status:
  upgrade_available`, the selected rank, best target id, score delta, and top
  alternatives. If an impossible runtime target is pinned, readiness should set
  `runtime_fit.target_selection.status: selected_not_eligible` while still
  exposing the measured best runtime target when one exists. The readiness
  gates should also include **Runtime optimizer** with attention state
  `better target available` or blocked state `selected not eligible` and a
  non-mutating `select_runtime_target` action whose refs point at the better
  runtime target, so a client can switch the selected runtime context without
  a mutating API call. The readiness
  payload should include
  `production_admission.schema_version: temms-production-admission/v1`; when a
  pinned runtime is compatible but lower scoring than the best measured target,
  `production_admission.apply_allowed` should be `false` and the blocking gate
  should be `runtime_optimizer`. The readiness payload and downloaded
  edge-runtime proof should also include `runtime_decision.schema_version:
  temms-runtime-decision/v1`, with selected path, recommended action, selected
  target, best target, score delta, runtime/artifact lane, blocking or
  attention gates, and top measured alternatives, so operators can see whether
  the selected path is ready to apply/stage, needs the best runtime, or is
  blocked.
  Evidence export should include
  `runtime_fit_evidence` records derived from the same readiness payload, and
  those summaries should preserve the selected runtime lane, accelerator,
  artifact-lane state, and production-admission decision. Mission replay should
  include a `runtime_fit` phase that names the lane and artifact state in the
  phase summary. Repeated rollout proof should collapse to the newest record,
  and replay should prefer the active slot's model/runtime evidence even if a
  newer inactive rollout has fit data. A best-target fit should be complete; a
  safe but lower-scoring pinned target should be `preview_only` with the better
  target and score delta in the phase summary. Evidence summary
  timelines, full bundle timelines, and mission replay events should carry
  `active_runtime_proof: true` on the active runtime-fit row so exported
  artifacts remain clear.
  During normal daemon operation the local edge heartbeat loop should refresh
  runtime/resource/deployment telemetry automatically; use manual heartbeat
  curls only to force a test condition.
  A **Refresh edge inventory** remediation carries an **Edge execution
  command** that must not run centrally; heartbeat refresh, benchmark
  collection, and runtime
  validation have to run on the actual edge node so the resulting capability
  lock is tied to live on-device inventory.
  The `/v1/hub/readiness` response should expose
  `edge_runtime_mission.schema_version: temms-edge-runtime-mission/v1` as a
  compact mirror of the selected path:
  `model -> runtime -> edge`, runtime fit,
  runtime lane, artifact fit, live inventory, performance SLO, resource
  envelope, validation, and DDIL queue status without requiring raw JSON.
  After a rollout is `imported` or `activated`, a benchmark that misses the
  declared SLO should surface as `performance drift`, include benchmark and
  rollout refs, and offer a reviewed rollback command for the active rollout.
  Missing or stale benchmark proof for an active rollout should surface as
  `drift unverified`, not as a green SLO pass.
  If another model in the same package is runtime-compatible and already green
  on performance plus resource checks, the drift gate should also expose
  **Stage fallback model** with a deterministic rollout command for that sibling
  model and `require_approval: true`. When the selected package/runtime target
  has passing runtime validation evidence, the fallback action refs should
  include `fallback_runtime_validation_id` and the command body should include
  `require_runtime_validation: true`.
  If a model declares `resource_requirements`, the resource envelope gate should
  block on proven RAM/storage/thermal/power violations and warn when the edge
  has not reported enough telemetry to verify the envelope. After a rollout is
  `imported` or `activated`, the same proven violation should surface as
  `resource drift`, include the rollout id/state in readiness refs, and offer a
  reviewed rollback command for the active rollout. When a lighter sibling model
  fits the degraded envelope, the resource drift gate should expose the same
  **Stage fallback model** action before rollback, again preserving approval and
  runtime-validation requirements in the generated rollout command.
- Compatibility matrix responses are model-aware. When `model_ids` is supplied,
  every returned cell should carry that same `model_id`; without the filter, a
  multi-model package should produce separate cells for each declared model so
  runtime constraints are not flattened at package level. Matrix cells should
  also include `performance.status`, `performance.benchmark`, and
  `performance.slo` when benchmark evidence or model SLOs are present; stale
  SLO proof should count as `performance_attention`. The matrix should also
  include ranked `recommendations`; the first deploy-ready recommendation should
  name the best model/device/runtime path, expose a score and confidence, and
  keep required actions visible for cells that still need release, validation,
  benchmark, telemetry, or compatibility remediation.
- Rollout apply should enforce edge readiness, not just display it. A rollout
  with a pinned runtime target should fail with HTTP `409` before import when
  runtime validation is missing/stale, the edge inventory is stale/offline, or
  the pinned target is lower scoring than the best measured runtime target for
  that model/device. The response should include a `runtime_optimizer` blocking
  gate and the better target refs for that suboptimal-runtime case.
  If the selected model declares `performance_slo` or `resource_requirements`,
  missing, stale, or failing benchmark/resource evidence should also block
  apply. The response should include `blocking_gates`, the model loader should
  not be called, and the rollout should remain in its assigned or approved
  state rather than moving to `downloading` or `failed`.
- Policy-driven hot-swaps should also respect edge readiness when Hub Lite has
  package context for the selected model. A resource-unsafe policy-selected
  model should emit `slot.activation_preflight_blocked`, skip model loading,
  leave the previous model running until a safe fallback is activated, and store
  `activation_preflight` in the fallback decision audit metadata. Exercise the
  same behavior through `/v1/control/slots/{slot}/evaluate` with `apply: true`
  to confirm API-triggered adaptive applies cannot bypass on-device admission.
- Daemon startup default-model activation should follow the same local edge
  admission. A resource-unsafe default model should emit
  `slot.activation_preflight_blocked`, skip `load_model`, leave the slot
  `stopped`, and record `slot.startup_failed` with
  `failure_kind: readiness_preflight`; a safe default startup should record
  `activation_preflight` in the slot decision audit metadata.
- Operator override, rollback, and queued DDIL deploy/override replay should
  also respect on-device admission when Hub Lite has package/device context.
  Unsafe control-plane activations should fail with HTTP `409`, include
  `blocking_gates`, skip `load_model`, and emit
  `slot.activation_preflight_blocked`; safe activations should record
  `activation_preflight` in decision audit metadata and telemetry.
- DDIL readiness shows connectivity mode, deployment state, active slot/model,
  evidence chain strength, and the latest proof events without opening raw JSON.
- DDIL/evidence readiness actions should include bounded refs such as pending
  counts, blocked counts, payload hashes, proof events, and replay
  phase state.
- DDIL deploy replay is Hub-readiness gated when the queued intent includes
  package, device, and runtime target context. Sync/preview should refuse replay
  if the latest edge inventory, runtime target, runtime capability lock,
  heartbeat freshness, performance proof, resource envelope, or selected edge
  gate is blocked. A rollout-only warning remains replayable so direct field
  deploy intents are not forced through Hub rollout assignment.
- If a queued deploy is safe but pinned to a lower-scoring runtime target,
  sync/preview should remain ready while marking that entry
  `ready_with_runtime_advisory`, incrementing `optimization_advisories`, and
  exposing the Runtime optimizer gate refs. The DDIL ledger should show the
  runtime advisory, best runtime target, runtime fit score, selected runtime
  lane, artifact fit, runtime capability lock, capability hash, heartbeat
  freshness, and production-apply admission before the operator syncs.
- Rollouts support approval, apply, and rollback where applicable.
- Evidence export offers summary, replay, full bundle, and air-gap bundle
  modes.

DDIL drill from the CLI and API:

1. Take the daemon offline with
   `uv run temms control offline --control-url http://127.0.0.1:18080`. DDIL
   readiness should show offline mode and deployment state `OFFLINE`.
2. Queue a deploy intent for `model-yolov8-lowlight-001` with
   `uv run temms control deploy ...` while offline. The daemon buffers the
   deployment intent locally, and
   `uv run temms control sync-preview` should show a queued-operation row for
   `model-yolov8-lowlight-001` with operation type, actor, target, and a short
   `sha256:` digest plus `verified intent` and `ready to replay`. The API
   evidence summary should also report
   `pending_operation_verification.verified: 1` and
   `pending_operation_preflight.ready: 1`.
3. Restore the link with `uv run temms control online`. The daemon returns to
   online mode while preserving the queued intent until sync.
4. Sync with `uv run temms control sync`. Pending operations should replay and
   clear, the
   active slot should change to `model-yolov8-lowlight-001`, and evidence export
   should include connectivity, deploy-request, and deploy-replayed telemetry
   with zero pending operations.
5. For tamper testing, edit the pending operation file before sync only in a
   throwaway workspace. A daemon with `TEMMS_PACKAGE_SIGNING_KEY` configured
   should show `tampered intent` in the pending ledger, reject sync with HTTP
   `409`, and leave the pending queue intact.
6. For blocked-replay testing, queue or craft an intent that names a missing
   model or slot. Sync preview should show the blocked intent with the
   model/slot target, digest, signature state, and replay-block reason, and
   `sync` should refuse replay while leaving the pending queue intact. After
   fixing the missing model, slot, runtime validation, or edge inventory
   evidence, sync preview should show the intent as ready and the next `sync`
   should replay it.
7. For edge-runtime replay testing, queue or craft a deploy intent that names
   `package_id`, `device_id`, and `runtime_target_id`, then make the selected
   edge inventory incompatible with that runtime target before sync. Preview
   should return `blocked`, the row should include
   `hub_readiness_status: blocked`, and the blocking gate should name the failed
   runtime/provider/accelerator fit. When a measured compatible target exists,
   the runtime optimizer gate should carry `select_runtime_target` refs so the
   operator can see the better target. Artifact-lane mismatches, such as ONNX on
   `temms-rpi5-tflite`, should show `artifact mismatch` and `production apply
   blocked`. Sync should
   leave the queue intact until the operator fixes
   inventory or clears the queue.
8. For stacked-intent testing, queue two valid deploy or operator override
   intents for the same slot before sync. The first row should remain replayable
   but show `superseded intent`, identify the later model that will win, and
   `/v1/control/sync/preview` should report `superseded: 1` plus a
   `slot_outcomes` entry for the final model. `/v1/control/sync` should skip the
   superseded activation, report `superseded_skipped: 1`, clear both buffered
   intents, and only load the final model for that slot.
9. For partial-replay failure testing, queue a condition update followed by a
   deploy intent, then force the deploy load to fail. Sync should apply the
   condition, keep only the failing deploy plus any later entries in the active
   queue, and leave the already-applied condition out of the retry path. After
   fixing the runtime load issue, a second sync should replay only the remaining
   deploy and clear the queue.

Edge-runtime proof drill from the API:

This is the quickest industry demo of why edge-runtime optimization matters.
It queues a signed deploy for the measured compatible on-device runtime while
offline, replays it, and exports proof.

CLI-first drill:

```bash
uv run temms control offline --control-url http://127.0.0.1:18080

uv run temms control deploy \
  --control-url http://127.0.0.1:18080 \
  --actor operator:edge-runtime-drill \
  --source industry-runtime-demo \
  --package-id pkg-vision-models-20240115 \
  --model-id model-yolov8-lowlight-001 \
  --device-id edge-sim \
  --runtime-target-id temms-x86_64-cpu \
  --slot vision

uv run temms control online --control-url http://127.0.0.1:18080
uv run temms control sync-preview --control-url http://127.0.0.1:18080
uv run temms control sync --control-url http://127.0.0.1:18080

uv run temms hub edge-runtime-mission \
  --hub-url http://127.0.0.1:18080 \
  --package-id pkg-vision-models-20240115 \
  --model-id model-yolov8-lowlight-001 \
  --device-id edge-sim \
  --runtime-target-id temms-x86_64-cpu \
  --slot vision \
  --require-go \
  --require-best-runtime \
  --require-capability-lock \
  --min-runtime-fit 95 \
  --signing-key temms-local-demo-signing-key \
  --output /tmp/temms-edge-runtime-proof.json

uv run temms hub verify-edge-proof /tmp/temms-edge-runtime-proof.json \
  --require-go \
  --require-best-runtime \
  --require-capability-lock \
  --min-runtime-fit 95 \
  --max-proof-age-seconds 900 \
  --package-id pkg-vision-models-20240115 \
  --model-id model-yolov8-lowlight-001 \
  --device-id edge-sim \
  --runtime-target-id temms-x86_64-cpu \
  --slot vision \
  --signing-key temms-local-demo-signing-key \
  --require-proof-signature
```

Inspect the generated JSON and confirm `edge_execution_contract` contains
`runtime_capability_lock.status=locked`, a 64-character
`runtime_capability_lock.capability_sha256`, the selected
`runtime_target_id`, reported edge inventory, runtime/provider requirements,
and artifact-lane fit. This is the field-review proof that the selected model
is not merely assigned to an edge, but bound to a concrete on-device capability
surface.
The proof file uses `schema_version: temms-edge-runtime-proof/v1`, records the
gate policy and pass/fail result, embeds the compact edge runtime mission and
full readiness payload, exposes top-level `runtime_workbench.schema_version:
temms-runtime-workbench/v1` for the ranked runtime contract, includes
`runtime_decision_trace.schema_version: temms-runtime-decision-trace/v1` for
the signed operator trace, and includes `integrity.payload_sha256` for handoff
audit. When a signing key is available,
the envelope also includes
`integrity.attestation` with the signing algorithm, signer, key fingerprint,
payload hash, and signature. In the Docker demo stack, the daemon is configured
with `TEMMS_PACKAGE_SIGNING_KEY=temms-local-demo-signing-key`, so API-generated
proof artifacts are signed by Hub and can be verified locally with
`--require-proof-signature`. `GET /v1/hub/edge-runtime-proof` returns the same
envelope from the evidence-enriched readiness path. `GET
/v1/hub/edge-runtime-proof/download` returns the same envelope as a JSON
attachment with proof filename, gate status, payload hash, attestation state,
signing-key fingerprint, and component digest headers. The digest headers are
`X-TEMMS-Edge-Proof-Runtime-Workbench-SHA256`,
`X-TEMMS-Edge-Proof-Runtime-Decision-Trace-SHA256`, and
`X-TEMMS-Edge-Proof-Execution-Manifest-SHA256`, so a field handoff can bind the
workbench, signed runtime trace, and execution manifest without parsing the
body first. `verify-edge-proof` runs locally without the Hub API, validates the
canonical hash, verifies the attestation when a signing key is supplied, and
reapplies the requested `go`/runtime-fit gate so a field operator can prove
whether the selected model/runtime/device path is
actually acceptable on the target edge. Add `--max-proof-age-seconds 900` when
the handoff should fail closed for proof artifacts older than 15 minutes; the
verifier reports this as `proof_freshness`, so a cryptographically valid but
stale proof is not treated as operationally current. Pass the selected
`--package-id`, `--model-id`, `--device-id`, `--runtime-target-id`, and `--slot`
to bind verification to the intended edge path; the verifier reports this as
`path_expectations`, so a valid proof for a different edge or runtime fails the
requested gate. The verifier now prints target runtime
coverage from the embedded execution contract: assessed, eligible, and blocked
counts, explicit per-target lines, a **Target Runtime Coverage** table with
runtime lane, score, capability proof, remediation, and contract-carried
operator or edge command payloads, plus
`target_runtime_coverage` and `runtime_decision_trace` in `--json` output for
automation. The trace should show per-target rank, selected/best state,
validation, benchmark, resource, telemetry, capability digest, blocker reason,
and remediation command. `verify-edge-proof` should also report
`runtime_decision_trace_consistency.status: consistent` and
`edge_execution_manifest_consistency.status: consistent`, plus
`component_digest_consistency.status: consistent` when component digests are
present; a proof with workbench/trace/manifest components but no
`component_digests`, or whose signed trace, execution manifest, or recorded
component digest disagrees with the canonical `runtime_workbench` rows,
execution contract, selected runtime image, capability lock, admission policy,
or remediation command should be invalid even when its payload hash and
attestation verify. For the seeded demo path, a strong proof should show the
selected runtime as eligible/best and the non-matching edge classes as blocked
with remediation such as selecting a matching edge class or running edge-local
benchmark proof.

Raw API version:

For the Docker demo on `localhost:8080`, run the live contract smoke first. It
checks `POST /v1/hub/mission-package/plan`,
`POST /v1/hub/mission-package/download`, and
`POST /v1/hub/mission-package/stage`, including the digest headers that tie the
mission package to its edge handoff, mission contract, capability lock, runtime
plan, and deployment intent. The stage step must report a passed stage gate with
`edge_handoff: verified`, `mission_contract: verified`,
`runtime_capability_lock: verified`, and `runtime_plan: verified`, which proves
failed/advisory proof-gate artifacts plus edge-handoff, mission-contract,
capability-lock, or runtime-plan digest mismatches cannot become edge rollouts.
The staged rollout should also retain `mission_package_stage` with the verified
package identity, edge-handoff, mission-contract, capability-lock, runtime-plan,
and deployment-intent digests. The smoke then approves and applies the staged rollout so
repeated runs leave the selected edge path activated rather than stuck in an
approval or assigned state. It exercises both explicit JSON planning and
YAML-only mission planning so both intake paths stay aligned:

```bash
uv run python scripts/mission_package_smoke.py --hub-url http://localhost:8080
```

CLI version:

```bash
cat > /tmp/temms-mission.yaml <<'YAML'
schema_version: temms-edge-mission/v1
mission:
  goal: Detect vehicles locally through DDIL link loss.
  sensor: camera.rgb
  slot: vision
selection:
  package_id: pkg-vision-models-20240115
  model_id: model-yolov8-lowlight-001
  device_id: edge-sim
  runtime_target_id: temms-x86_64-cpu
slo:
  latency_budget_ms: 95
  min_throughput_ips: 25
model_handling:
  switch_policy: condition_and_confidence
  confidence_threshold: 0.65
  fallback_model_id: auto
ddil:
  mode: queue_signed_intents
YAML

uv run temms hub mission-package-plan /tmp/temms-mission.yaml \
  --hub-url http://127.0.0.1:18080 \
  --json

uv run temms hub mission-package-download /tmp/temms-mission.yaml \
  --hub-url http://127.0.0.1:18080 \
  --output /tmp/temms-edge-mission-package.json

uv run temms hub mission-package-stage /tmp/temms-edge-mission-package.json \
  --hub-url http://127.0.0.1:18080 \
  --actor operator:functional-test \
  --reason "functional test staged from mission package"
```

```bash
curl -s -X POST http://127.0.0.1:18080/v1/hub/mission-package/plan \
  -H "Content-Type: application/json" \
  -d '{"package_id":"pkg-vision-models-20240115","model_id":"model-yolov8-lowlight-001","device_id":"edge-sim","runtime_target_id":"temms-x86_64-cpu","slot":"vision","goal":"Detect vehicles locally through DDIL link loss.","sensor":"camera.rgb","latency_budget_ms":95,"min_throughput_ips":25,"switch_policy":"condition_and_confidence","confidence_threshold":0.65,"ddil_mode":"queue_signed_intents","require_go":false,"require_best_runtime":true,"require_capability_lock":true,"min_runtime_fit":95,"require_proof_signature":true}' \
  | python -m json.tool

cat <<'JSON' | curl -s -X POST http://127.0.0.1:18080/v1/hub/mission-package/plan \
  -H "Content-Type: application/json" \
  -d @- | python -m json.tool
{
  "mission_yaml": "schema_version: temms-edge-mission/v1\nmission:\n  goal: Detect vehicles locally through DDIL link loss.\n  sensor: camera.rgb\n  slot: vision\nselection:\n  package_id: pkg-vision-models-20240115\n  model_id: model-yolov8-lowlight-001\n  device_id: edge-sim\n  runtime_target_id: temms-x86_64-cpu\nslo:\n  latency_budget_ms: 95\n  min_throughput_ips: 25\nmodel_handling:\n  switch_policy: condition_and_confidence\n  confidence_threshold: 0.65\n  fallback_model_id: auto\nddil:\n  mode: queue_signed_intents\n",
  "require_go": false,
  "require_best_runtime": true,
  "require_capability_lock": true,
  "min_runtime_fit": 95,
  "require_proof_signature": true
}
JSON

curl -OJ -X POST http://127.0.0.1:18080/v1/hub/mission-package/download \
  -H "Content-Type: application/json" \
  -d '{"package_id":"pkg-vision-models-20240115","model_id":"model-yolov8-lowlight-001","device_id":"edge-sim","runtime_target_id":"temms-x86_64-cpu","slot":"vision","goal":"Detect vehicles locally through DDIL link loss.","sensor":"camera.rgb","latency_budget_ms":95,"min_throughput_ips":25,"switch_policy":"condition_and_confidence","confidence_threshold":0.65,"ddil_mode":"queue_signed_intents","require_go":false,"require_best_runtime":true,"require_capability_lock":true,"min_runtime_fit":95,"require_proof_signature":true}'

curl -OJ "http://127.0.0.1:18080/v1/hub/edge-runtime-proof/download?package_id=pkg-vision-models-20240115&model_id=model-yolov8-lowlight-001&device_id=edge-sim&runtime_target_id=temms-x86_64-cpu&slot=vision&source_action=edge-runtime-mission&require_go=true&require_best_runtime=true&require_capability_lock=true&min_runtime_fit=95"

curl -X POST http://127.0.0.1:18080/v1/control/offline | python -m json.tool

curl -X POST http://127.0.0.1:18080/v1/control/deploy \
  -H "Content-Type: application/json" \
  -d '{"actor":"operator:edge-runtime-drill","source":"industry-runtime-demo","package_id":"pkg-vision-models-20240115","model_id":"model-yolov8-lowlight-001","device_id":"edge-sim","runtime_target_id":"temms-x86_64-cpu","slot":"vision"}' \
  | python -m json.tool

curl -X POST http://127.0.0.1:18080/v1/control/online | python -m json.tool

curl http://127.0.0.1:18080/v1/control/sync/preview | python -m json.tool

curl -X POST http://127.0.0.1:18080/v1/control/sync | python -m json.tool

curl -X POST http://127.0.0.1:18080/v1/hub/evidence/export \
  -H "Content-Type: application/json" \
  -d '{"replay":true,"replay_limit":50}' \
  | python -m json.tool
```

Expected proof:

- Sync preview shows a `verified intent` that is `ready to replay` for
  `temms-x86_64-cpu`.
- Sync replays the queued deploy, clears the pending queue, and activates
  `model-yolov8-lowlight-001` on the `vision` slot.
- Mission replay includes the replayed deploy under the offline-operation
  phase, and evidence exports retain the queued runtime, capability lock, and
  validation/benchmark evidence after the replay queue drains.

Rollout and rollback drill from the API:

1. Assign a rollout for `yolov8-lowlight` with `POST /v1/hub/rollouts`
   (see the spot checks below). The rollout should appear in rollout history
   in the `assigned` state. Multi-device rollouts are sequenced by issuing one
   such per-device assignment for each target device.
2. Approve and apply the assigned rollout with `temms hub approve` and
   `temms hub apply`. The active slot should show
   `model-yolov8-lowlight-001`.
3. In the mission replay export, confirm the phase checklist. Before the
   rollback drill, `Fallback or rollback` may be the remaining incomplete phase.
4. Roll back the activated rollout with
   `POST /v1/hub/rollouts/{rollout_id}/rollback`. The rollout state should move
   to
   `rolled_back`, the active slot should return to the previous model from the
   slot decision log, and mission replay should mark both `rollout_coordination`
   and `fallback_rollback` complete.

Useful API spot checks:

```bash
curl http://127.0.0.1:18080/v1/health
curl http://127.0.0.1:18080/v1/hub/packages | python -m json.tool
curl http://127.0.0.1:18080/v1/hub/rollouts | python -m json.tool
curl http://127.0.0.1:18080/v1/hub/runtime-targets/validations | python -m json.tool
curl http://127.0.0.1:18080/v1/hub/benchmarks | python -m json.tool
curl http://127.0.0.1:18080/v1/hub/evidence | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/control/slots/vision/evaluate \
  -H "Content-Type: application/json" \
  -d '{"apply": false}' \
  | python -m json.tool
curl "http://127.0.0.1:18080/v1/hub/readiness?package_id=pkg-vision-models-20240115&model_id=model-yolov8-lowlight-001&device_id=edge-sim&runtime_target_id=temms-x86_64-cpu&slot=vision" | python -m json.tool
curl "http://127.0.0.1:18080/v1/hub/readiness?package_id=pkg-vision-models-20240115&model_id=model-mobilenet-tiny-001&device_id=edge-sim&runtime_target_id=temms-x86_64-cpu&slot=vision" | python -m json.tool
uv run temms hub edge-runtime-mission \
  --hub-url http://127.0.0.1:18080 \
  --package-id pkg-vision-models-20240115 \
  --model-id model-yolov8-lowlight-001 \
  --device-id edge-sim \
  --runtime-target-id temms-x86_64-cpu \
  --slot vision \
  --require-go \
  --require-best-runtime \
  --require-capability-lock \
  --min-runtime-fit 95 \
  --signing-key temms-local-demo-signing-key \
  --output /tmp/temms-edge-runtime-proof.json
uv run temms hub verify-edge-proof /tmp/temms-edge-runtime-proof.json \
  --require-go \
  --require-best-runtime \
  --require-capability-lock \
  --min-runtime-fit 95 \
  --max-proof-age-seconds 900 \
  --package-id pkg-vision-models-20240115 \
  --model-id model-yolov8-lowlight-001 \
  --device-id edge-sim \
  --runtime-target-id temms-x86_64-cpu \
  --slot vision \
  --signing-key temms-local-demo-signing-key \
  --require-proof-signature
uv run temms hub edge-runtime-mission \
  --hub-url http://127.0.0.1:18080 \
  --package-id pkg-vision-models-20240115 \
  --model-id model-yolov8-lowlight-001 \
  --device-id edge-sim \
  --runtime-target-id temms-x86_64-cpu \
  --slot vision \
  --json
uv run temms hub readiness \
  --hub-url http://127.0.0.1:18080 \
  --package-id pkg-vision-models-20240115 \
  --model-id model-mobilenet-tiny-001 \
  --device-id edge-sim \
  --runtime-target-id temms-x86_64-cpu \
  --slot vision
curl -X POST http://127.0.0.1:18080/v1/hub/benchmarks \
  -H "Content-Type: application/json" \
  -d '{"device_id":"edge-sim","package_id":"pkg-vision-models-20240115","runtime_target_id":"temms-x86_64-cpu","actor":"edge:edge-sim","result":{"schema_version":"temms-benchmark/v1","model_id":"model-yolov8-lowlight-001","slot":"vision","latency_ms":{"p95":18.0},"throughput":{"inferences_per_second":60.0}}}' \
  | python -m json.tool
curl "http://127.0.0.1:18080/v1/hub/readiness?package_id=pkg-vision-models-20240115&model_id=model-yolov8-lowlight-001&device_id=edge-sim&runtime_target_id=temms-x86_64-cpu&slot=vision" | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/hub/benchmarks \
  -H "Content-Type: application/json" \
  -d '{"device_id":"edge-sim","package_id":"pkg-vision-models-20240115","runtime_target_id":"temms-x86_64-cpu","actor":"edge:edge-sim","result":{"schema_version":"temms-benchmark/v1","model_id":"model-yolov8-lowlight-001","slot":"vision","latency_ms":{"p95":11.2},"throughput":{"inferences_per_second":89.3}}}' \
  | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/hub/devices/edge-sim/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"status":"online","inventory":{"runtimes":{"onnxruntime":{"available":true,"providers":["CPUExecutionProvider"]}},"memory":{"available_mb":256},"storage":{"available_mb":24576},"thermal":{"temperature_c":42},"power":{"source":"mains","battery_percent":100}},"deployment_status":{"state":"READY","source":"resource-drift-drill"}}' \
  | python -m json.tool
curl "http://127.0.0.1:18080/v1/hub/readiness?package_id=pkg-vision-models-20240115&model_id=model-yolov8-lowlight-001&device_id=edge-sim&runtime_target_id=temms-x86_64-cpu&slot=vision" | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/hub/devices/edge-sim/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"status":"online","inventory":{"runtimes":{"onnxruntime":{"available":true,"providers":["CPUExecutionProvider"]}},"memory":{"available_mb":4096},"storage":{"available_mb":24576},"thermal":{"temperature_c":42},"power":{"source":"mains","battery_percent":100}},"deployment_status":{"state":"READY","source":"resource-drift-reset"}}' \
  | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/hub/compatibility/matrix \
  -H "Content-Type: application/json" \
  -d '{"device_ids":["edge-sim"],"package_ids":["pkg-vision-models-20240115"],"model_ids":["model-yolov8-lowlight-001"],"runtime_target_ids":["temms-x86_64-cpu"],"include_device_inventory":true}' \
  | python -m json.tool
curl http://127.0.0.1:18080/v1/control/sync/preview | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/hub/rollouts \
  -H "Content-Type: application/json" \
  -d '{"package_id":"pkg-vision-models-20240115","model_id":"model-yolov8-lowlight-001","device_id":"edge-sim","slot":"vision","runtime_target_id":"temms-x86_64-cpu","require_approval":true,"actor":"operator:mission-package-workbench"}' \
  | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/hub/rollouts/rollout-id-from-response/rollback \
  -H "Content-Type: application/json" \
  -d '{"actor":"operator:mission-package-workbench","reason":"functional rollback drill"}' \
  | python -m json.tool
```

Export proof artifacts from the seeded daemon:

```bash
curl -X POST http://127.0.0.1:18080/v1/hub/evidence/export \
  -H "Content-Type: application/json" \
  -d '{"summary": true, "summary_limit": 20}' \
  | python -m json.tool

curl -X POST http://127.0.0.1:18080/v1/hub/evidence/export \
  -H "Content-Type: application/json" \
  -d '{"replay": true, "replay_limit": 50}' \
  | python -m json.tool
```

## 4. Docker Stack Smoke

Use Docker when you want the normal local service stack. The compose entrypoint
copies the example package into the TEMMS data volume, signs it with the local
demo key, catalogs it in Hub Lite, promotes it to `released`, and enrolls
`edge-sim` before the daemon starts:

```bash
make docker-up
curl http://localhost:8080/v1/health
curl http://localhost:8080/v1/hub/packages | python -m json.tool
```

Open:

```text
TEMMS API  http://localhost:8080/v1/health
API docs   http://localhost:8080/docs
MLflow UI  http://localhost:5001
```

Expected seeded Hub state:

- Model inventory shows three signed models: daylight, lowlight, and tiny.
- Package state is `released`.
- `edge-sim` is online with the `x86_64-cpu` profile and a Docker-demo
  simulated resource floor high enough for the low-light model envelope.
- `temms-x86_64-cpu` is compatible with the selected model.
- Compatibility matrix cells include the selected `model_id` and evaluate live
  edge inventory when `include_device_inventory` is true.
- The Hub runtime optimizer shows ranked edge paths. A validated,
  SLO/resource-clean runtime target should outrank a generic device-inventory
  match, and selecting an incompatible runtime such as Orin TensorRT for the
  local x86 `edge-sim` should demote that path with concrete runtime/provider/
  accelerator blockers. Runtime fit should name the selected runtime
  lane; the local default path should read as CPU portable and an incompatible
  Orin/TensorRT target should read as Orin TensorRT while staying blocked.
  Artifact fit should make a native ONNX CPU/CUDA path,
  TensorRT conversion path, or TFLite artifact mismatch visible in the same
  readiness payload.
- Create rollout, approve, and apply work because the daemon has
  `TEMMS_PACKAGE_SIGNING_KEY` configured in `docker-compose.yml`.
- Create rollout records the selected model ID; applying a lowlight rollout
  activates `model-yolov8-lowlight-001` instead of defaulting back to the first
  model in the package.
- Evidence summary counts generated proof events after rollout activity, even
  before a separate evidence bundle is ingested.

Stop the stack:

```bash
make docker-down
```

## 5. Acceptance Checks

Run the local MVP checks:

```bash
make mvp-smoke
make mvp-acceptance
```

Run the containerized multi-agent acceptance flow:

```bash
make docker-acceptance
```

## Troubleshooting

- If Hub has no model rows, seed a workspace with
  `scripts/canonical_product_demo.py`, run `scripts/seed_docker_hub_demo.py`, or
  restart the Docker stack with `TEMMS_DEMO_SEED_HUB=1`.
- If rollout apply reports that signature verification needs a signing key,
  confirm `TEMMS_PACKAGE_SIGNING_KEY` is set for the daemon.
- If rollout assignment is blocked, confirm the package is `released`, strict
  metadata is present, and runtime validation exists for the selected runtime
  target.
- If port `18080` is busy, use another port in both the daemon command and the
  URL.
