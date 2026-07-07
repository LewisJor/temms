# Functional Testing

Use this checklist when validating TEMMS locally before an industry or field
demo. It focuses on the product path: signed model inventory, compatible
on-device runtime target, sensor/model handling policy, mission package
handoff, rollout approval, activation controls, and evidence export.

## 1. Build And Verify The Product UI

Install the React Hub dependencies once, then typecheck, build, and smoke the
production bundle served by the daemon. The source package is under `ui/`, but
the repo root exposes the demo-facing npm scripts:

```bash
npm --prefix ui install
npm run typecheck
npm run build
npm run smoke:workbench
```

The same commands are available as `make ui-install`, `make ui-typecheck`,
`make ui-build`, and `make ui-smoke`.

Expected build output has one Hub JavaScript bundle and one Hub CSS bundle under
`src/temms/ui/static/hub/assets/`. The Hub product source lives in `ui/src/` and
is a Vite + React + TypeScript app. The smoke check verifies that the built Hub
keeps the mission-first flow, `temms-edge-mission-package/v1` handoff manifest,
Runtime workbench, selected model, edge node, target runtime, proof, and
per-runtime retarget contracts used by browser automation and operator demos.
It also enforces conservative raw and gzip bundle-size budgets so the daemon
served Hub cannot quietly bloat before a field demo.

Run the focused server/UI regression suite:

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
on heartbeat while leaving runtime/provider detection real, so the first-open
Mission Package Workbench starts on the deployable path instead of depending on
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

Open the product UI:

```text
http://127.0.0.1:18080/ui/hub
```

`/ui/` redirects to `/ui/hub` when Hub is enabled.

## 3. Product UI Smoke Test

The first screen should be **Mission Package Workbench** with the **Mission** step
active. The first viewport should show the **Mission workflow cockpit**, not a
wall of system telemetry: the operator path rail chooses the stage, the stage
focus panel shows the current decision and next action, package path signals
show mission/model/runtime/handling/package state, and **Live context** keeps
inventory, rollout, evidence, and DDIL health available without making them the
primary surface. Start the demo there: define the mission goal or use
**Import YAML** to load a mission spec; the workbench should populate
sensor, slot, latency/throughput SLO, switch policy, fallback model, and DDIL
behavior. When the YAML carries `model_id`, `package_id`, `device_id`, or
`runtime_target_id` values that match Hub inventory, the selected
model/runtime/edge path should hydrate from the spec before package planning.
The stage focus panel should keep the ready condition, risk, and primary actions
visible while you move
through the top flow in order: **Model Plan** for signed package/model
selection, **Runtime Fit** for edge node/runtime ranking and proof,
**Sensor Handling** for switching and DDIL policy, **Package Handoff** for the
mission package boundary, **Edge Deploy** for rollout staging, and **Field Ops**
for DDIL state plus evidence export.

The Runtime step should show the **Runtime workbench** before the deeper proof
panels. It should preserve the model chosen in **Model Plan** as a locked
selected-model context, then let the operator choose the edge node and runtime
target. The ranked runtime table should show selected, best, validated,
benchmarked, inventory-matched, and blocked target lanes. The workbench should
show the selected fit score, expose the best runtime target, and keep
**Generate proof** beside the active model/runtime/device path. Then narrate the active **On-device runtime proof**
path, **Edge execution contract**, **Operational verdict**, **Edge runtime
mission** proof band, and **Runtime proof artifact**. The execution contract
should show selected model artifact, target runtime, edge node, fit score,
runtime lane, artifact path, SLO/resource evidence, admission state, ranked
measured runtime candidates, and **Target runtime coverage** for every known
runtime target, including blocked Jetson/Orin/RPi/TFLite-style lanes and the
exact capability or evidence gap that made them ineligible for the selected
edge. Each runtime lane should include a short **Next:** remediation line such
as use matching edge class, validate runtime, record edge benchmark, or use for
field apply, plus a command copy control when the execution contract carries an
operator or edge-local remediation command for that lane. Edge-run actions such
as benchmark collection or heartbeat refresh should be visibly labeled as
edge-run so the operator does not mistake them for safe central mutations. Each
lane should also show compact component proof chips for compatibility,
validation, performance, resource, and telemetry. Finally use **Runtime proof
artifact** to copy the generated
`temms hub edge-runtime-mission` and `temms hub verify-edge-proof` commands for
the currently selected model/runtime/device path, or press **Generate artifact**
to inspect the same `temms-edge-runtime-proof/v1` JSON envelope from Hub. Use
**Download JSON** when you want the browser demo to hand off the exact proof
file for offline verification.

Expected visible state with the Docker Hub seed:

- The top flow is **Mission -> Model Plan -> Runtime Fit -> Sensor Handling ->
  Package Handoff -> Edge Deploy -> Field Ops**. Mission should be a focused builder, not a dashboard
  dump: goal or YAML should be editable without burying the operator in rollout
  controls, and **Live context** should keep inventory and telemetry available
  without making them the primary surface. **Sensor Handling** should own sensor input, slot,
  latency/throughput SLO, switch policy, fallback model, and DDIL behavior in
  one place.
- The staged Hub should not show unrelated console sections under the active
  step. **Model Plan** owns model inventory and selected model detail, while
  package registration, edge enrollment, and bundle import sit behind
  **Advanced intake**. **Edge Deploy** opens on the mission-package deploy lane;
  direct rollout forms live behind **Manual controls**, and rollout-plan/fleet
  panels surface only when a workflow action opens them. **Field Ops** owns DDIL
  field state and mission evidence export.
- The **Mission workflow cockpit** should show the current step status, the
  operator decision, package path signals, and previous/next buttons, so the
  demo can move through the path without hunting through the page. Changing
  stages should return focus to the operator path rail, while readiness actions
  and package rollout staging may focus the exact operational section they opened.
- The active stage should show ready condition and risk facts plus only the
  actions relevant to that stage. On
  **Package Handoff**, **Stage rollout** should stay disabled until
  **Plan package** produces a package identity, deployment intent, and passed proof gate; after proof passes, the cockpit should unlock **Stage rollout**
  and keep **Download package** available for field handoff.
- **Plan package** should call `POST /v1/hub/mission-package/plan` and return a
  `temms-edge-mission-package/v1` payload. The preview/copy payload should
  include mission, selection, SLO, model handling, DDIL policy, runtime plan,
  proof gate, `deployment_intent`, `edge_handoff`, component digests, and
  `integrity.payload_sha256`. The `edge_handoff` block should use schema
  `temms-edge-mission-package-handoff/v1`, mode `stage_approve_apply`, and
  include package stage, rollout approval, rollout apply, and digest-verification
  commands for the edge operator. Package planning is advisory so operators can plan while readiness is still
  `attention`; **Generate artifact** and **Download JSON** stay strict. The
  same endpoint should derive missing `package_id`, `model_id`, `device_id`,
  `runtime_target_id`, `slot`, SLO, handling, and DDIL fields from
  `mission_yaml` when the YAML carries them, while explicit JSON fields still
  take precedence.
- The **Package Handoff** step should show a **Mission package binding chain** before the
  action buttons are used: mission spec, model/runtime/edge selection, handling
  policy, and deploy intent should all be visible as the package boundary. If
  the operator has not pressed **Plan package** yet, the deploy lane may show a
  draft rollout path; planning upgrades it to a hashed mission handoff.
- The top flow's **Package Handoff** status should describe package progress, not raw
  runtime proof export state: `draft handoff` before planning, `package
  planned` after `POST /v1/hub/mission-package/plan`, and `downloaded` after a
  retained package handoff.
- The **Package Handoff** step should keep the primary package handoff first. Deeper
  readiness gates, runtime mission proof, runtime proof artifact, and execution
  contract should live behind **Advanced verification** so the demo can stay on
  the package boundary unless the audience asks for proof internals.
- **Download package** should call `POST /v1/hub/mission-package/download`,
  save a `temms-edge-mission-package-*.json` file, and expose the package
  identity, payload, runtime-plan, deployment-intent, and edge-handoff hashes in
  the preview handoff.
  The Package stage should then show **Mission package handoff** with retained
  filename, package identity hash, payload hash, mission hash, runtime-plan
  hash, deploy-intent hash, and preserved `edge_handoff` runbook matching the
  package body. Repeated plan/download
  calls may produce different payload hashes because artifact observation time
  changes, but they must keep the same package identity hash for the same
  mission/model/runtime/device policy.
- **Stage rollout** in the Package/Deploy path should create a rollout from the
  planned package/model/device/runtime/slot, require approval and runtime
  validation, switch the UI to **Edge Deploy**, and use the package payload's
  `deployment_intent.command.body`. The deployment intent should also carry the
  exact `mission_contract_sha256`, `runtime_capability_lock_sha256`, and
  `runtime_plan_sha256`, and staging should reject an artifact whose intent
  points at a different mission-contract, capability-lock, or runtime-plan
  digest. The rollout reason should include the mission package identity digest.
  It should not stage directly from the draft package preview; press
  **Plan package** first so the rollout is tied to the hashed deployment intent.
- Model inventory shows three signed vision models: daylight, lowlight, and
  mobilenet-tiny.
- The Runtime workbench shows the selected model from **Model Plan** as locked
  context, plus edge node and runtime target selectors above the proof panels.
  Its ranked target table should put the selected
  runtime first, mark the best target, display fit score or proof-needed state,
  and show validation, benchmark, and live inventory status per runtime target.
  Directly below the controls, the **On-device runtime capability vector**
  should show runtime image/arch/profile, provider match, artifact lane, and
  capability-lock state for the selected path.
- The active edge path panel shows the selected
  `model-yolov8-lowlight-001 -> temms-x86_64-cpu -> edge-sim` style path,
  runtime fit, target coverage counts, admission state, and signed proof
  policy together before the detailed workflow sections.
- The selected model panel shows package, version, runtime, provider, source,
  and update time.
- Model rows and the selected model panel show p95 latency, throughput,
  benchmark target, declared performance SLO, SLO met/miss state, and passing
  runtime validation status.
- The Runtime proof artifact panel shows gate policy
  `go + best runtime + capability lock + fit >= 95 + proof <= 15m + path bound`,
  a stable `/tmp/temms-edge-runtime-proof-*.json` output path, and copyable
  generate/verify commands. **Generate artifact** opens a proof payload whose
  `integrity.payload_sha256` and `runtime_capability_lock.capability_sha256`
  can be inspected in the browser, and the Hub UI should immediately adopt the
  returned `proof.readiness` for the selected path. The same panel shows
  **Signed runtime trace**, **Execution manifest**, and component digest status
  for the last generated or downloaded proof. For a current proof, the trace
  should report
  `trace consistent`, schema `temms-runtime-decision-trace/v1`, the ranked
  target count, remediation command count, and
  `trace agrees with runtime_workbench`; stale or mismatched proofs must warn
  before the raw JSON is expanded. The execution manifest should report schema
  `temms-edge-execution-manifest/v1`, selected runtime image, runtime lane,
  capability-lock digest, validation and benchmark ids, best-target status, and
  gate admission policy. The proof should also carry
  `component_digests.schema_version:
  temms-edge-runtime-proof-component-digests/v1` with separate hashes for
  `runtime_workbench`, `runtime_decision_trace`, and
  `edge_execution_manifest`. The Hub UI recomputes those component hashes in
  the browser for the selected proof and should report them as verified before
  the operator falls back to the offline verifier. After **Download JSON**, the
  same panel should show **Download handoff headers** with the selected proof
  filename, payload hash, gate status, attestation state, and workbench, trace,
  and manifest header digests matching the proof body. The preview summary should read as an operator proof
  report: gate status, runtime fit, selected model, selected runtime, edge node,
  capability lock digest, heartbeat freshness, payload hash, and signer
  fingerprint should be visible before expanding the raw JSON. **Download JSON**
  saves the same payload with the selected proof filename, so the copied
  `verify-edge-proof` command can validate signature, best-runtime selection,
  capability lock, runtime fit, proof freshness, and exact path binding locally
  without contacting Hub.
- A locked capability proof must be backed by fresh edge inventory telemetry.
  If the edge heartbeat is stale or missing, `runtime_capability_lock.status`
  should be `blocked`, its failures should name the heartbeat freshness gap,
  and strict proof generation or local verification with `--require-capability-lock`
  should fail closed.
- The Edge execution contract panel shows the selected model -> runtime -> edge
  path, the runtime decision action such as `apply or stage`, `use best
  runtime`, or `collect evidence`, top measured runtime candidates, and full
  target runtime coverage with selected/best/blocked status plus per-target
  remediation guidance and component proof chips.
  If a pinned runtime is lower-scoring or ineligible, the panel should expose
  **Use best runtime** and switch the selected runtime without mutating Hub
  state.
- `/v1/hub/readiness`, `/v1/hub/edge-runtime-proof`, downloaded proof JSON, and
  runtime-fit evidence exports should include
  `edge_execution_contract.schema_version:
  temms-edge-execution-contract/v1`. The browser panel should reflect that
  contract, including `target_assessments` and each assessment's remediation
  command payloads, not a separate UI-only reconstruction.
- `/v1/hub/readiness`, `/v1/hub/edge-runtime-proof`, and downloaded proof JSON
  should also include `runtime_workbench.schema_version:
  temms-runtime-workbench/v1`. The selected target, best target, target count,
  selected-is-best flag, capability-lock status, validation id, benchmark
  evidence, telemetry state, and blocked target rows in the browser Runtime
  workbench should match that backend contract. The browser Runtime decision
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
- DDIL runtime repair should reject unproved targets. When a queued deploy is
  retargeted, the pending row and evidence export should show a
  `runtime_target_proof`/retarget proof with status `proved`, a runtime-fit
  score, validation id, benchmark id, locked capability hash, and fresh heartbeat
  telemetry. The same proof should expose
  `runtime_workbench_schema_version: temms-runtime-workbench/v1`,
  `runtime_workbench_previous_selected_runtime_target_id`,
  `runtime_workbench_selected_runtime_target_id`,
  `runtime_workbench_best_runtime_target_id`, target counts, and
  `runtime_workbench_selected_is_best`, plus
  `target_assessment_sha256`, so the reviewer can distinguish the originally
  queued runtime from the runtime that was actually proved and bind the proof to
  the selected runtime target metadata, lane, artifact, capability, validation,
  and benchmark facts. A blocked or advisory pending replay should also preserve the
  target-assessment remediation command in the evidence summary as
  `runtime_remediation_contract_command_text`, with
  `runtime_remediation_contract_kind` identifying operator versus edge-local
  execution and `runtime_remediation_contract_requires_edge_execution` making
  edge-only actions explicit. In the Hub DDIL ledger, pending and quarantined
  rows should show the same command as a copyable operator or edge-run runtime
  command. The Hub DDIL readiness and Evidence views should also keep a visible
  **DDIL runtime repair proof** card after sync, showing the queued runtime,
  proved runtime, best measured runtime, runtime-fit score, capability lock,
  validation id, benchmark id, target coverage, and replay source.
- DDIL replay should reject stale retarget proof. If runtime target metadata,
  validation evidence, benchmark evidence, eligibility, or best-target status
  changes after the retarget audit is signed, `sync/preview` should block the
  repaired intent and show the stale proof reason instead of replaying silently.
  The blocked entry should include
  `runtime_retarget_replay_signed_target_assessment_sha256` and
  `runtime_retarget_replay_current_target_assessment_sha256` when the compact
  target-assessment digest changed.
- Selecting a different model changes the deployment context. If you select
  `yolov8-lowlight`, compatibility preview, rollout creation, rollout apply,
  and DDIL queueing should all carry `model-yolov8-lowlight-001`, not just the
  package ID.
- The **Operational verdict** panel gives a single go/attention/blocked status
  and lists eight gates: model package, runtime target, performance fit,
  resource envelope, edge target, rollout gate, DDIL queue, and evidence chain.
  The panel should be backed by `/v1/hub/readiness`; each gate should include a
  short state and the next operator action when it is not ready.
- With the seeded data, `yolov8-lowlight` should show a green `go` verdict with
  no remediation chips. `mobilenet-tiny` should show `attention` because it has
  no selected-model rollout yet; the rollout gate should show **Create rollout**
  and **Create staged plan** action buttons. Clicking each button should focus
  the matching rollout or staged-plan workflow section and open a
  **Readiness remediation** review panel. The action should not mutate state
  until **Run command** is pressed. The mobilenet readiness API response should
  include action `refs` for package, model, device, runtime target, slot, and
  approval defaults.
- Executable readiness actions should include a `command` object with HTTP
  method, API path, and any suggested body. For example, mobilenet's
  **Create rollout** action should point to `POST /v1/hub/rollouts`; the UI
  review panel should show that path and the selected model/device/runtime body
  with `actor: "operator:readiness-remediation"` for audit history. Rollout and
  staged-plan remediation bodies should include deterministic `rollout_id` or
  `plan_id` values so retrying the same command does not create duplicate
  records, plus a `reason` explaining the readiness gate remediation.
- Benchmark remediation should not be centrally executable from the cockpit.
  A missing or stale SLO benchmark action should expose
  `requires_edge_execution: true`, show the exact `temms benchmark ... --hub-url`
  command for the selected device/package/model/runtime, and leave the central
  POST body as an inspection envelope only. Runtime-lane remediation rows should
  expose the same contract-carried edge-local command as a copyable action and
  label it as edge-run. The UI should label that action as
  edge-run required and prevent **Run command** from publishing synthetic
  performance proof.
- The deployment path shows model, runtime, runtime lane, edge, runtime fit,
  resource envelope, rollout, and evidence status together. The on-device fit
  band should name live runtime inventory, runtime-target requirements, the
  selected execution lane such as CPU portable, Jetson CUDA, Raspberry Pi 5
  TFLite, or Orin TensorRT, declared performance SLO, resource envelope,
  validation or benchmark proof, and any missing runtime/provider/accelerator/
  resource telemetry. The
  **On-device capability dossier** should condense that same selected
  model/device/runtime context into runtime fit, runtime lane, resource
  envelope, performance proof, validation state, live edge inventory, target
  requirements, and the current Hub admission gate without opening raw JSON. If
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
  exposing the measured best runtime target when one exists. The cockpit's
  **On-device capability dossier** should show this as a target-rank warning or
  blocker rather than making the pinned target look optimal. The readiness
  gates should also include **Runtime optimizer** with attention state
  `better target available` or blocked state `selected not eligible` and a
  **Use best runtime** action whose refs point at the better runtime target.
  Clicking that action should switch the cockpit's selected runtime context and
  focus the deployment path without making a mutating API call. The readiness
  payload should include
  `production_admission.schema_version: temms-production-admission/v1`; when a
  pinned runtime is compatible but lower scoring than the best measured target,
  `production_admission.apply_allowed` should be `false` and the blocking gate
  should be `runtime_optimizer`. The readiness payload and downloaded
  edge-runtime proof should also include `runtime_decision.schema_version:
  temms-runtime-decision/v1`, with selected path, recommended action, selected
  target, best target, score delta, runtime/artifact lane, blocking or
  attention gates, and top measured alternatives. The Edge runtime mission band
  should surface this as **Runtime decision** so operators can see whether the
  selected path is ready to apply/stage, needs the best runtime, or is blocked.
  Evidence export should include
  `runtime_fit_evidence` records derived from the same readiness payload, and
  those summaries should preserve the selected runtime lane, accelerator,
  artifact-lane state, and production-admission decision. Mission replay should
  include a `runtime_fit` phase that names the lane and artifact state in the
  phase summary. Repeated rollout proof should collapse to the newest record,
  and replay should prefer the active slot's model/runtime evidence even if a
  newer inactive rollout has fit data. A best-target fit should be complete; a
  safe but lower-scoring pinned target should be `preview_only` with the better
  target and score delta in the phase summary. In the Hub cockpit's field operating picture, the evidence feed
  should label the active model/runtime row as `active runtime proof` before
  any inactive runtime-fit rows with similar timestamps. Evidence summary
  timelines, full bundle timelines, and mission replay events should carry
  `active_runtime_proof: true` on that active runtime-fit row so exported
  artifacts remain clear outside the browser.
  During normal daemon operation the local edge heartbeat loop should refresh
  runtime/resource/deployment telemetry automatically; use manual heartbeat
  curls only to force a test condition.
  If the Hub shows **Refresh edge inventory**, open the readiness remediation
  panel and copy the **Edge execution command**. The browser must not execute
  that command centrally; heartbeat refresh, benchmark collection, and runtime
  validation have to run on the actual edge node so the resulting capability
  lock is tied to live on-device inventory.
  The top-level **Edge runtime mission** band should mirror the selected path
  from the same data. It should show `model -> runtime -> edge`, runtime fit,
  runtime lane, artifact fit, live inventory, performance SLO, resource
  envelope, validation, and DDIL repair status without requiring raw JSON.
  The `/v1/hub/readiness` response should also expose
  `edge_runtime_mission.schema_version: temms-edge-runtime-mission/v1` with the
  same path and metric states, so curl/API demos and the browser are proving the
  same on-device story.
  After a retargeted DDIL replay, that band should show **retarget proved** even
  though the pending queue is empty.
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
  counts, blocked/quarantined counts, payload hashes, proof events, and replay
  phase state.
- DDIL deploy replay is Hub-readiness gated when the queued intent includes
  package, device, and runtime target context. Sync/preview should refuse replay
  if the latest edge inventory, runtime target, runtime capability lock,
  heartbeat freshness, performance proof, resource envelope, or selected edge
  gate is blocked. A rollout-only warning remains replayable so direct field
  deploy intents are not forced through staged rollout creation.
- If a queued deploy is safe but pinned to a lower-scoring runtime target,
  sync/preview should remain ready while marking that entry
  `ready_with_runtime_advisory`, incrementing `optimization_advisories`, and
  exposing the Runtime optimizer gate refs. The DDIL ledger should show the
  runtime advisory, best runtime target, runtime fit score, selected runtime
  lane, artifact fit, runtime capability lock, capability hash, heartbeat
  freshness, and production-apply admission before the operator syncs.
- Rollout coordination shows staged-plan creation plus advance/pause/resume
  controls.
- Rollouts show approval, apply, and rollback controls where applicable.
- Evidence offers Summary, Replay, Full bundle, and Air-gap bundle actions.
- No old tabbed admin console should appear in the Hub product UI.

DDIL drill from the UI:

1. Select `yolov8-lowlight`, then click **Link loss** in the DDIL readiness
   section. The DDIL tile should move to offline mode and deployment state
   should show `OFFLINE`.
2. Click **Queue intent**. The daemon buffers a deployment intent locally while
   offline, the DDIL tile should show pending operations after refresh, and the
   readiness panel should show a queued-operation row for
   `model-yolov8-lowlight-001` with operation type, actor, target, and a short
   `sha256:` digest plus `verified intent` and `ready to replay`. The API
   evidence summary should also report
   `pending_operation_verification.verified: 1` and
   `pending_operation_preflight.ready: 1`.
3. Click **Restore link**. The daemon returns to online mode while preserving the
   queued intent until sync.
4. Click **Sync pending**. Pending operations should replay and clear, the
   active slot should change to `model-yolov8-lowlight-001`, and evidence export
   should include connectivity, deploy-request, and deploy-replayed telemetry
   with zero pending operations.
5. For tamper testing, edit the pending operation file before sync only in a
   throwaway workspace. A daemon with `TEMMS_PACKAGE_SIGNING_KEY` configured
   should show `tampered intent` in the pending ledger, reject sync with HTTP
   `409`, and leave the pending queue intact.
6. For blocked-replay testing, queue or craft an intent that names a missing
   model or slot. The DDIL tile should show a blocked intent, **Sync pending**
   should not be the recovery action, and **Quarantine blocked** should move the
   bad intent into the dead-letter ledger while preserving any replay-ready
   intents in the active queue. After quarantine, the readiness panel should
   show a compact **Quarantined DDIL intents** ledger row with the model/slot
   target, digest, signature state, and replay-block reason. After fixing the
   missing model, slot, runtime validation, or edge inventory evidence, click
   **Requeue intent** to run current DDIL preflight and restore that signed
   payload to the active queue only if it is ready. If the issue is not truly
   remediated, the response should report a blocked requeue candidate and the
   row should remain quarantined. Once ready, the row should leave the active
   quarantine ledger while evidence exports retain `requeued_at`,
   `requeued_by`, and `requeue_reason`. Use
   **Acknowledge quarantine** only for intents that should not be replayed; the
   row should leave the active readiness panel while remaining in evidence
   exports as acknowledged audit history.
7. For edge-runtime replay testing, queue or craft a deploy intent that names
   `package_id`, `device_id`, and `runtime_target_id`, then make the selected
   edge inventory incompatible with that runtime target before sync. Preview
   should return `blocked`, the row should include
   `hub_readiness_status: blocked`, and the blocking gate should name the failed
   runtime/provider/accelerator fit. When a measured compatible target exists,
   the runtime optimizer gate should carry **Use best runtime** refs and the
   pending row should show a compact runtime-fix line with previous target,
   corrected target, and score delta. Artifact-lane mismatches, such as ONNX on
   `temms-rpi5-tflite`, should show `artifact mismatch` and `production apply
   blocked`. Click **Use best runtime** on the pending row to call
   `/v1/control/sync/retarget-runtime`; the row should refresh with the new
   runtime target, `verified intent`, and a retarget audit line that names the
   previous and selected targets. After **Sync pending**, evidence summary and
   mission replay should preserve the retarget under the replayed activation
   decision, even though the pending queue is empty. **Sync pending** should
   leave the queue intact until the operator retargets the intent, fixes
   inventory, or quarantines the bad intent.
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

Runtime-retarget proof drill from the API:

This is the quickest industry demo of why edge-runtime optimization matters.
It intentionally queues a deploy for the wrong on-device lane, retargets the
signed DDIL intent to the measured compatible runtime, replays it, and exports
proof.

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
  --runtime-target-id temms-rpi5-tflite \
  --slot vision

uv run temms control online --control-url http://127.0.0.1:18080
uv run temms control sync-preview --control-url http://127.0.0.1:18080

PAYLOAD_SHA=$(
  uv run temms control sync-preview --control-url http://127.0.0.1:18080 --json \
    | python -c 'import json,sys; print(json.load(sys.stdin)["entries"][0]["payload_sha256"])'
)

uv run temms control retarget-runtime \
  --control-url http://127.0.0.1:18080 \
  --payload-sha256 "$PAYLOAD_SHA" \
  --actor operator:edge-runtime-drill \
  --reason "selected measured compatible on-device runtime"

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
Inspect the `retarget-runtime` response and later evidence export for the DDIL
repair proof as well: `runtime_workbench_previous_selected_runtime_target_id`
should show the intentionally wrong queued lane,
`runtime_workbench_selected_runtime_target_id` should show the proved runtime,
`runtime_workbench_best_runtime_target_id` should match that proved runtime, and
`runtime_workbench_selected_is_best` should be `true`.

`retarget-runtime` can auto-select the measured candidate from readiness refs
when `--runtime-target-id` is omitted. Pass `--runtime-target-id` only when the
operator deliberately wants to override the recommended target.
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
envelope from the evidence-enriched readiness path used by the React Hub. `GET
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
The React Hub shows the command pair, a **Generate artifact** inspection action,
and a **Download JSON** artifact handoff in the Runtime proof artifact panel for
the selected path. Successful proof generation also refreshes the selected
readiness state from the returned proof, so the browser demo and CLI proof trail
stay aligned even when a previous page snapshot was stale.

Raw API version:

For the Docker demo on `localhost:8080`, run the live contract smoke first. It
checks `/ui/hub`, `POST /v1/hub/mission-package/plan`,
`POST /v1/hub/mission-package/download`, and
`POST /v1/hub/mission-package/stage`, including the digest headers that tie the
mission package to its mission contract, capability lock, runtime plan, and
deployment intent. The stage step must report a passed stage gate with
`mission_contract: verified`, `runtime_capability_lock: verified`, and
`runtime_plan: verified`, which proves failed/advisory proof-gate artifacts plus
mission-contract, capability-lock, or runtime-plan digest mismatches cannot
become edge rollouts. The smoke then approves and applies the staged rollout so
repeated runs leave the selected edge path activated rather than stuck in an
approval or assigned state. It exercises both explicit JSON planning and
YAML-only mission planning so the backend path stays aligned with the browser
importer:

```bash
make docker-product-smoke
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
  -d '{"actor":"operator:edge-runtime-drill","source":"industry-runtime-demo","package_id":"pkg-vision-models-20240115","model_id":"model-yolov8-lowlight-001","device_id":"edge-sim","runtime_target_id":"temms-rpi5-tflite","slot":"vision"}' \
  | python -m json.tool

curl -X POST http://127.0.0.1:18080/v1/control/online | python -m json.tool

curl http://127.0.0.1:18080/v1/control/sync/preview | python -m json.tool

PAYLOAD_SHA=$(
  curl -s -X POST http://127.0.0.1:18080/v1/hub/evidence/export \
    -H "Content-Type: application/json" \
    -d '{"summary":true,"summary_limit":20}' \
    | python -c 'import json,sys; data=json.load(sys.stdin); print(data["runtime"]["pending_operations"][0]["payload_sha256"])'
)

curl -X POST http://127.0.0.1:18080/v1/control/sync/retarget-runtime \
  -H "Content-Type: application/json" \
  -d "{\"payload_sha256\":\"$PAYLOAD_SHA\",\"runtime_target_id\":\"temms-x86_64-cpu\",\"actor\":\"operator:edge-runtime-drill\",\"reason\":\"selected measured compatible on-device runtime\"}" \
  | python -m json.tool

curl http://127.0.0.1:18080/v1/control/sync/preview | python -m json.tool

curl -X POST http://127.0.0.1:18080/v1/control/sync | python -m json.tool

curl -X POST http://127.0.0.1:18080/v1/hub/evidence/export \
  -H "Content-Type: application/json" \
  -d '{"replay":true,"replay_limit":50}' \
  | python -m json.tool
```

Expected proof:

- The first sync preview is blocked or carries the runtime optimizer repair
  refs for `temms-rpi5-tflite -> temms-x86_64-cpu`.
- `retarget-runtime` rewrites and re-signs the queued deploy intent.
- The returned `runtime_target_proof` includes
  `runtime_workbench_schema_version`, previous selected runtime, proved selected
  runtime, best runtime, eligible/blocked counts, selected-is-best, validation
  id, benchmark id, capability hash, and `target_assessment_sha256`.
- The second sync preview is replay-ready for `temms-x86_64-cpu`.
- Mission replay includes an `offline_operation` event whose detail reads
  `retargeted temms-rpi5-tflite -> temms-x86_64-cpu`.
- The Hub **Edge runtime mission** band shows DDIL repair as **retarget proved**,
  and the DDIL readiness plus Evidence sections retain the runtime repair proof
  cards after the replay queue drains.

Staged rollout and rollback drill from the UI:

1. Select `yolov8-lowlight`, then use **Create plan** in Rollout coordination.
   The plan list should show a ready plan with one target and batch size `1`.
2. Click **Advance**. The plan should assign the next batch and a rollout should
   appear in Approval and activation. If there are no remaining pending targets,
   the plan state should move to `advancing` while the target waits for a
   terminal rollout outcome.
3. Approve and apply the assigned rollout. The active slot should show
   `model-yolov8-lowlight-001`, and the plan should show the target as
   reconciled.
4. In Mission proof, confirm the replay phase checklist is visible. Before the
   rollback drill, `Fallback or rollback` may be the remaining incomplete phase.
5. Click **Rollback** on the activated rollout. The rollout state should move to
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
curl -X POST http://127.0.0.1:18080/v1/hub/rollout-plans \
  -H "Content-Type: application/json" \
  -d '{"package_id":"pkg-vision-models-20240115","model_id":"model-yolov8-lowlight-001","device_ids":["edge-sim"],"slot":"vision","runtime_target_id":"temms-x86_64-cpu","batch_size":1,"require_approval":true,"actor":"operator:mission-package-workbench"}' \
  | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/hub/rollout-plans/plan-id-from-response/advance \
  -H "Content-Type: application/json" \
  -d '{"actor":"operator:mission-package-workbench"}' \
  | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/hub/rollouts/rollout-id-from-response/rollback \
  -H "Content-Type: application/json" \
  -d '{"actor":"operator:mission-package-workbench","reason":"functional rollback drill"}' \
  | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/control/sync/quarantine-blocked \
  -H "Content-Type: application/json" \
  -d '{"actor":"operator:mission-package-workbench","reason":"functional test quarantine"}' \
  | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/control/sync/requeue-dead-letters \
  -H "Content-Type: application/json" \
  -d '{"actor":"operator:mission-package-workbench","reason":"functional test runtime proof remediated","require_ready":true}' \
  | python -m json.tool
curl -X POST http://127.0.0.1:18080/v1/control/sync/acknowledge-dead-letters \
  -H "Content-Type: application/json" \
  -d '{"actor":"operator:mission-package-workbench","reason":"functional test reviewed"}' \
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
TEMMS Hub  http://localhost:8080/ui/hub
TEMMS API  http://localhost:8080/v1/health
API docs   http://localhost:8080/docs
MLflow UI  http://localhost:5001
```

Expected first-open Hub state:

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
  accelerator blockers. The on-device dossier should show the selected runtime
  lane; the local default path should read as CPU portable and an incompatible
  Orin/TensorRT target should read as Orin TensorRT while staying blocked. The
  dossier should also show **Artifact fit** so a native ONNX CPU/CUDA path,
  TensorRT conversion path, or TFLite artifact mismatch is visible without
  inspecting JSON.
- Create rollout, approve, and apply work from the UI because the daemon has
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

- If the Hub UI says the React app has not been built, run `npm run build` from
  the repo root.
- If `/ui/hub` loads but has no model rows, seed a workspace with
  `scripts/canonical_product_demo.py`, run `scripts/seed_docker_hub_demo.py`, or
  restart the Docker stack with `TEMMS_DEMO_SEED_HUB=1`.
- If UI rollout apply reports that signature verification needs a signing key,
  confirm `TEMMS_PACKAGE_SIGNING_KEY` is set for the daemon.
- If rollout assignment is blocked, confirm the package is `released`, strict
  metadata is present, and runtime validation exists for the selected runtime
  target.
- If port `18080` is busy, use another port in both the daemon command and the
  URL.
