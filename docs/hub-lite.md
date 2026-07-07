# Hub Lite

Hub Lite is the MVP control-plane layer for multi-VM TEMMS deployments. It is intentionally small: one JSON-backed store, exposed through the TEMMS API, with enough state to coordinate devices and rollouts without replacing MLflow.

Hub Lite responsibilities:

- Device enrollment
- Heartbeat and inventory
- Package catalog
- Runtime target catalog
- Deployment readiness verdicts
- Rollout assignment
- Rollout lifecycle status
- Deployment status snapshots
- Air-gap export/import

Hub Lite does not train models and does not replace MLflow. MLflow remains the registry. TEMMS packages remain the deployment unit.

## Online Edge Sync

Each edge VM can sync to a central Hub Lite API by setting environment variables in `/etc/temms/temms.env`:

```ini
TEMMS_HUB_URL=http://hub-vm:8080
TEMMS_HUB_TOKEN=change-me
TEMMS_DEVICE_ID=edge-1
TEMMS_DEVICE_PROFILE=x86_64-cpu
TEMMS_EDGE_HEARTBEAT_INTERVAL_S=60
TEMMS_HUB_SYNC_INTERVAL_S=30
```

The daemon always refreshes its local Hub Lite heartbeat/inventory/deployment
status on `TEMMS_EDGE_HEARTBEAT_INTERVAL_S`, even when disconnected or running
without a central Hub URL. When central sync is enabled, the same detected
inventory is sent upstream, central rollout assignments for the device ID are
mirrored into the local Hub Lite store, package archives for assigned rollouts
are downloaded, and local rollout state transitions are replayed back to the
hub. That replay preserves edge-side lifecycle history such as `downloading`,
`imported`, and `activated`, not just the final state. Downloaded archives are
cached beside the edge Hub Lite state and the local package catalog path is
rewritten before apply. Leave `TEMMS_HUB_URL` unset for a fully local or
air-gapped edge VM; readiness will still use the local heartbeat freshness
loop.

On each online sync, the edge preserves a previously downloaded package artifact when its `source_sha256` still matches the central catalog. Before reuse, it verifies the cached artifact SHA. If the artifact digest has drifted, TEMMS emits `hub.package_cache_mismatch` telemetry and downloads a fresh copy from Hub Lite. If the central `source_sha256` changes, the old cached artifact is discarded and the new package is fetched.

To let the edge VM execute assigned rollouts without a separate operator call, enable signed auto-apply:

```ini
TEMMS_HUB_AUTO_APPLY=true
TEMMS_ROLLOUT_REQUIRE_SIGNATURE=true
TEMMS_PACKAGE_SIGNING_KEY_FILE=/etc/temms/hub-signing.key
```

Auto-apply only acts on local rollouts in `assigned` state. If signature verification is required and no package signing key is configured, the local rollout is marked `failed` and that state is pushed back to Hub Lite on the next successful sync.
When auto-apply calls the local rollout apply API, it inherits the same
edge-safety preflight as manual apply. A readiness rejection leaves the rollout
assigned and emits structured `rollout.auto_apply_failed` telemetry with
`failure_kind: readiness_preflight`, `blocking_gates`, and the readiness
selection so operators can tell whether to benchmark, refresh inventory,
validate a runtime, or choose another recommendation.

## API

Hub Lite routes live under `/v1/hub/*`. When `TEMMS_API_TOKEN` is configured, these routes require the same token as `/v1/control/*`. Web UI write actions use that same protection for slot overrides, condition injection, override clearing, and package import; UI package import also inherits the daemon package signature policy.

Set `TEMMS_RBAC_TOKENS` for role-scoped tokens. Entries use `role=token`
pairs, such as `operator=op-token;approver=approve-token;edge=edge-token`.
When configured, API and UI write actions require the matching role while
`TEMMS_API_TOKEN` remains an admin token. Approval-gated rollouts require the
`approver` role for approval, package promotion to `approved` also requires
the `approver` role, and edge-agent lifecycle updates accept the `edge` role.
Fleet audit reads such as deployment status, replayed telemetry, runtime
validation evidence, benchmark evidence, Hub evidence lists, and evidence
exports require the `operator` or `auditor` role.

When the API is running inside the TEMMS daemon, `/v1/hub/packages/from-mlflow`, `/v1/hub/packages/register`, and `/v1/hub/rollouts/{id}/apply` inherit `TEMMS_ROLLOUT_REQUIRE_SIGNATURE` plus `TEMMS_PACKAGE_SIGNING_KEY` or `TEMMS_PACKAGE_SIGNING_KEY_FILE`. MLflow packaging and package registration sign artifacts with the daemon key before cataloging whenever signatures are required and a key is configured, then verify the resulting artifact metadata. Package registration and Hub-side MLflow packaging run strict production metadata validation by default, so catalog metadata records whether schemas, provenance, runtime constraints, and benchmark metadata were checked. Rollout assignment refuses catalog entries that do not carry verified signature metadata and, when daemon signature policy is enabled, strict metadata validation. Operators can still pass a signing key or set `strict_metadata` false in the request for one-off lab calls, but deployed agents use the daemon signature policy and strict catalog posture by default.

Cataloged packages enter a promotion lifecycle: `candidate` -> `validated` ->
`approved` -> `released` -> `retired`. Hub Lite refuses rollout assignment
unless the package is `released`. Each transition records actor, reason,
timestamp, and optional evidence such as a runtime validation ID, so release
state appears in Hub exports, evidence summaries, mission replay, and air-gap
bundles.

Package catalog registration plus rollout assignment, status, apply, and rollback history records include an `actor` field. Operators can send it as `X-TEMMS-Actor` or in the JSON body; online edge sync uses `edge:<device-id>`. TEMMS never derives actors from the bearer token itself, so evidence bundles can identify the operator or edge agent without storing secrets.

Use `GET /v1/hub/readiness` for the product-level deployment verdict that powers
Mission Package Workbench. The response uses schema
`temms-deployment-readiness/v1`, returns an overall `go`, `attention`, or
`blocked` status, and includes gates for model package, runtime target,
performance fit, resource envelope, edge target, rollout gate, DDIL queue, and
evidence chain. Optional query parameters `package_id`, `model_id`,
`device_id`, `runtime_target_id`, and `slot` pin the verdict to a specific
deployment context; without them, Hub Lite selects the latest
active/released context from the local store. Gates that are not `go` can
include structured remediation `actions`; the top-level `actions` list
deduplicates those operator steps for clients that want a compact next-action
queue. Each action includes `kind` plus `refs` for the selected deployment
context, such as package, model, device, runtime target, slot, and recommended
approval defaults for rollout or rollout-plan creation. Performance actions
include benchmark refs, p95 latency, throughput, and declared model SLO limits;
resource refs include declared RAM/storage/thermal/power requirements plus
observed edge telemetry; DDIL actions include pending, blocked, quarantined,
and payload-hash summaries; evidence actions include proof event counts,
mission replay phase counts, incomplete phases, and the recommended export
mode. Directly executable actions also include a `command` object with HTTP
`method`, API `path`, and optional suggested `body`; clients can use that
metadata for explicit
operator-confirmed remediation. The
React cockpit uses those commands as reviewable actions: selecting a readiness
action focuses the matching workflow section, opens the exact method/path/body
for inspection, and only executes the command after the operator presses **Run
command**. Mutating remediation command bodies include
`actor: "operator:readiness-remediation"` plus a reason when the endpoint
supports one, so package promotion, rollout approval, rollout assignment, and
DDIL quarantine/acknowledgement remain traceable in evidence history. Readiness
generated rollout and rollout-plan commands also include deterministic
`rollout_id` or `plan_id` values derived from the selected package, model,
device, runtime, and slot. Re-running the same command returns the existing
matching rollout/plan, while conflicting reuse of the same explicit ID is
rejected. Rollout and rollout-plan creation commands include an explicit
readiness reason, so the created history entries explain that the assignment or
plan came from a readiness gate remediation.
The same response includes `edge_runtime_mission.schema_version:
temms-edge-runtime-mission/v1`, a compact API-readable summary of the selected
`model -> runtime -> edge` path. Its metrics include runtime fit, runtime lane,
artifact fit, live inventory, performance, resources, runtime validation,
production admission, and daemon-enriched DDIL repair state. Evidence exports
carry that mission object inside runtime-fit evidence records and flatten the
mission status/path into runtime-fit summaries for quick post-mission review.
Before producing the final proof, React Hub can call
`POST /v1/hub/mission-package/plan` with the mission goal or YAML, sensor, slot,
latency/throughput SLO, model-switch policy, fallback model, DDIL behavior, and
the selected package/model/device/runtime path. When a request supplies
`mission_yaml`, the server derives any missing selection, SLO, handling, and
DDIL fields from that YAML before readiness evaluation; explicit JSON fields
take precedence over YAML hints. The response is a
`temms-edge-mission-package/v1` envelope with component digests for the mission,
selection, SLO, model handling, DDIL policy, runtime plan, proof gate, edge
execution contract, runtime workbench, `deployment_intent`, and `edge_handoff`.
The `edge_handoff` block uses schema
`temms-edge-mission-package-handoff/v1`, mode `stage_approve_apply`, and carries
the package stage, rollout approval, rollout apply, and digest-verification
runbook that should travel with the downloaded artifact. That deploy intent
includes the deterministic rollout ID plus the exact
`POST /v1/hub/rollouts` body for an approval- and runtime-validation-gated edge
staging action. `POST /v1/hub/mission-package/download` returns the same package
plan as an attachment with `X-TEMMS-Mission-Package-SHA256`,
`X-TEMMS-Mission-Package-Runtime-Plan-SHA256`, and
`X-TEMMS-Mission-Package-Deployment-Intent-SHA256` headers for field handoff.
`POST /v1/hub/mission-package/stage` accepts that
`temms-edge-mission-package/v1` artifact, verifies the package identity,
payload, deployment-intent digests, and passed proof gate, then stages the
embedded rollout intent while preserving the artifact's `edge_handoff` runbook
in the stage proof. Failed proof gates stay advisory artifacts; they cannot
be staged to edge deploy until readiness is remediated and the package is
planned again.
The package planner can run in an advisory mode while readiness is still
`attention`; final proof generation and download remain the strict `go`,
best-runtime, capability-lock, fit, signature, and path-bound gates.
Operators can inspect the same artifact without opening raw readiness JSON:

```bash
temms hub mission-package-plan ./mission.yaml \
  --hub-url http://127.0.0.1:18080 \
  --json

temms hub mission-package-download ./mission.yaml \
  --hub-url http://127.0.0.1:18080 \
  --output /tmp/temms-edge-mission-package.json

temms hub mission-package-stage /tmp/temms-edge-mission-package.json \
  --hub-url http://127.0.0.1:18080 \
  --actor operator:cli-demo

temms hub edge-runtime-mission \
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
  --signing-key-file /etc/temms/hub-signing.key \
  --output /tmp/temms-edge-runtime-proof.json

temms hub verify-edge-proof /tmp/temms-edge-runtime-proof.json \
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
  --signing-key-file /etc/temms/hub-signing.key \
  --require-proof-signature

temms hub edge-runtime-mission \
  --hub-url http://127.0.0.1:18080 \
  --package-id pkg-vision-models-20240115 \
  --model-id model-yolov8-lowlight-001 \
  --device-id edge-sim \
  --runtime-target-id temms-x86_64-cpu \
  --slot vision \
  --json
```

`mission-package-plan`, `mission-package-download`, and `mission-package-stage`
post the same
`temms-edge-mission-package/v1` request shape as the React workbench. The
positional argument can be a mission YAML file; operators can also pass
`--mission-yaml-file`, `--mission-yaml`, or explicit overrides such as `--goal`,
`--sensor`, `--latency-budget-ms`, `--min-throughput-ips`, `--switch-policy`,
`--confidence-threshold`, `--fallback-model-id`, and `--ddil-mode`. When a YAML
file carries package/model/device/runtime IDs, Hub derives the deployment path
before readiness evaluation; explicit CLI fields override YAML hints. After
download, `mission-package-stage` posts the artifact to
`/v1/hub/mission-package/stage`; Hub verifies the mission package digest chain
and stages the package-bound rollout with optional `--actor`, `--reason`, and
`--rollout-id` overrides.

Use `temms hub readiness` with the same selection flags when you need the full
gate/action payload instead of the compact edge mission table. Add
`--require-go` to make either command exit non-zero unless the selected
deployment verdict is `go`, `--require-best-runtime` when the selected target
must be the best measured eligible runtime, `--require-capability-lock` when the
proof must include a locked on-device runtime/provider/accelerator capability
basis backed by fresh edge inventory telemetry, and
`--min-runtime-fit <score>` when a demo, release gate, or CI job
should fail unless the measured runtime-fit score meets the required bar. When
`--output` is used with `readiness` or
`edge-runtime-mission`, the CLI writes a `temms-edge-runtime-proof/v1` envelope
with the gate policy, pass/fail status, gate failures, compact mission, full
readiness payload, selected path, runtime-fit score, and
`integrity.payload_sha256` for audit handoff. The proof also embeds
`component_digests.schema_version:
temms-edge-runtime-proof-component-digests/v1`, with SHA256 digests for the
runtime workbench, runtime decision trace, and edge execution manifest so each
auditable proof component can be referenced independently while still being
covered by the top-level payload hash. The proof also embeds
`runtime_decision_trace.schema_version: temms-runtime-decision-trace/v1`,
derived from the canonical workbench rows, so the signed artifact carries the
same per-target rank, proof components, capability digest, blocker reason, and
remediation command visible in the Hub. When a signing key is supplied,
the proof also carries `integrity.attestation` with algorithm, signer, key
fingerprint, payload hash, and signature. API-generated proofs are signed
automatically when the daemon has `TEMMS_PACKAGE_SIGNING_KEY` or
`TEMMS_PACKAGE_SIGNING_KEY_FILE` configured. `verify-edge-proof` is local-only:
it checks the proof schema and canonical payload hash without contacting Hub
Lite, verifies the attestation when a signing key is supplied, then reapplies
any requested `--require-go`, `--require-best-runtime`,
`--require-capability-lock`, `--min-runtime-fit`, or
`--max-proof-age-seconds` gate. Add `--require-proof-signature` when the proof
must fail closed unless the attestation verifies. Add
`--max-proof-age-seconds 900` for field handoff when a proof older than 15
minutes should be treated as stale even if the hash and attestation are valid.
Pass `--package-id`, `--model-id`, `--device-id`, `--runtime-target-id`, and
`--slot` to bind the offline verification to the exact intended edge path; a
fresh valid signature for another device/runtime/model fails the requested gate
instead of being accepted by accident.
When the proof includes `edge_execution_contract.target_assessments`, the
verifier also prints a **Target Runtime Coverage** section with assessed,
eligible, and blocked target counts, per-runtime selected/best state, lane,
score, compact capability proof, the next remediation action, and a **Target
Remediation Commands** table when the contract carries operator or edge command
payloads. The JSON verification result exposes the same summary plus
`target_runtime_coverage.commands` and `runtime_decision_trace` for scripts
that need to fail a field check or hand the next exact runtime action to an edge
operator when the selected on-device path is not the best proven runtime.
Verification also derives `runtime_decision_trace_consistency` from the embedded
canonical workbench. If a signed trace disagrees with workbench-selected target,
best target, per-target proof state, capability digest, or remediation command,
the proof is invalid even when the payload hash and attestation are otherwise
well formed.
Verification also derives `edge_execution_manifest_consistency` from the signed
execution manifest. If the manifest disagrees with the selected path, runtime
image, runtime lane, artifact state, capability lock, evidence ids, admission
policy, or selected remediation command, the proof is invalid even when its
hash and attestation verify.
Verification also recomputes `component_digest_consistency`; if a modern proof
contains runtime workbench, trace, or manifest components but omits
`component_digests`, or if a recorded component digest does not match the
embedded proof component, the proof is invalid even when the payload hash and
attestation have been recomputed.
The React Hub mirrors this check for operators by recomputing the three
component hashes in-browser on generated or downloaded proofs and surfacing
whether the workbench, trace, and manifest digests are verified for the selected
model/runtime/edge path.
The React contract panel uses the same remediation records to show copyable
operator commands per runtime lane. Benchmark and inventory refresh actions are
marked edge-run and produce edge-local commands, while validation, packaging,
compatibility inspection, and strict proof checks remain explicit operator
commands.
The React Hub proof panel adopts the `readiness` payload embedded in a
successful generated or downloaded proof for the selected path, so a strict
proof also refreshes the visible runtime fit, execution contract, and capability
lock state without waiting for a manual page reload. The preview summary is
proof-first: gate status, runtime fit, selected model/runtime/edge, capability
lock digest, heartbeat freshness, payload hash, and signer fingerprint are
visible before the operator expands raw JSON.
The same proof panel also renders a browser-side **Signed runtime trace** check
from the embedded `runtime_decision_trace` and canonical `runtime_workbench`.
For a current proof on the selected path, the card shows the
`temms-runtime-decision-trace/v1` schema, ranked runtime target count,
remediation command count, and whether the signed trace agrees with the
workbench. If the payload belongs to another model/runtime/device path, lacks
the trace, or disagrees with the workbench, the panel warns before an operator
hands the artifact to the offline verifier.
Generated and downloaded proofs also carry
`edge_execution_manifest.schema_version:
temms-edge-execution-manifest/v1`. The manifest is included in the signed proof
payload and summarizes the exact package/model/device/runtime path, selected
runtime image, runtime lane/provider context, artifact fit, capability-lock
digest, validation id, benchmark id, best-target status, gate policy, gate
status, and selected remediation command. The React proof panel surfaces this
as **Execution manifest** so operators can answer what will execute on the edge
without expanding the full nested proof.
Proofs also carry `runtime_capability_lock` inside `runtime_fit`,
`runtime_decision`, and `edge_execution_contract`. The lock records the compact
model requirements, selected runtime target constraints, reported edge
inventory, telemetry freshness, artifact-lane fit, capability failures, and
`capability_sha256`. A stale or missing heartbeat blocks the lock even when the
runtime target and model artifact otherwise match, so strict proofs cannot pass
on stale edge inventory. This gives field reviewers a single hash-bound answer
for what on-device runtime/provider/accelerator surface was proven.
A valid proof can record a blocked mission; the verifier exits non-zero only
when the proof is invalid or the gate policy supplied to verification is not
satisfied. The React Hub surfaces this as a **Runtime proof artifact** lane
directly under **Edge runtime mission**, with copyable generate, verify, and
JSON verify commands for the selected model/runtime/device path. The same lane
can call `GET /v1/hub/edge-runtime-proof` to return a
`temms-edge-runtime-proof/v1` envelope directly from the evidence-enriched Hub
readiness path for browser-side inspection, and it can download that envelope as
the selected proof JSON artifact from `GET
/v1/hub/edge-runtime-proof/download` for local verifier handoff. Download
responses include proof filename, gate status, payload hash, attestation state,
key-fingerprint headers when signed, and component digest headers for the
runtime workbench, runtime decision trace, and execution manifest:
`X-TEMMS-Edge-Proof-Runtime-Workbench-SHA256`,
`X-TEMMS-Edge-Proof-Runtime-Decision-Trace-SHA256`, and
`X-TEMMS-Edge-Proof-Execution-Manifest-SHA256`. The React Hub retains those
download headers in the Runtime proof artifact lane and compares the header
digests with the proof body so handoff mismatches are visible before local
verification.

Benchmark remediation is intentionally different from central remediation. When
the performance gate needs benchmark proof, its `record_benchmark` command
includes `requires_edge_execution: true`, an `edge_command`/text form of the
`temms benchmark ... --hub-url ...` invocation, and a note that the central POST
body is only the target envelope for the result. The React cockpit shows that
edge command for inspection and disables central execution, because latency,
throughput, provider choice, and accelerator availability are only valid when
measured on the selected device/runtime.

DDIL preflight uses the same readiness logic before replaying queued deploy
intents when those intents carry package, device, and runtime target context.
`/v1/control/sync/preview` and `/v1/control/sync` block replay when the latest
Hub Lite gates or runtime capability lock prove the selected edge cannot safely
host the runtime/model, including runtime-target mismatch, active runtime drift,
stale heartbeat telemetry, stale or failing performance proof,
resource-envelope violations, or selected-edge blockers. The preflight entry
includes `hub_readiness_status`, `hub_readiness_selection`,
`hub_blocking_gates`, `hub_attention_gates`,
`hub_runtime_capability_lock`, `hub_capability_sha256`, and heartbeat
freshness fields so the Hub cockpit can explain the exact edge capability
failure without exposing raw payloads. A rollout-only attention gate remains
replayable to preserve direct field deploy workflows.
When DDIL preflight surfaces runtime optimizer advice, the entry also carries
the compact `hub_target_assessments` contract with each target's remediation
command. Evidence summaries flatten that selected/best-target remediation into
`runtime_remediation_contract_command_text`,
`runtime_remediation_contract_kind`, and
`runtime_remediation_contract_requires_edge_execution`, so an offline replay
artifact still tells the operator whether to run a central proof check or an
edge-local inventory/benchmark command.

Runtime readiness is edge-inventory aware. When a deployment is scoped to a
runtime target, Hub Lite checks the target's declared runtimes, ONNX providers,
and accelerators against the selected device's latest reported inventory. A
runtime image that is compatible on paper but requires an unavailable provider
or accelerator is reported as blocked before rollout assignment. The React
cockpit surfaces the same signal first as **Edge runtime mission**, then as
**On-device runtime fit** in the deployment path. The mission band condenses
runtime-fit score, runtime lane, artifact fit, live inventory, performance SLO,
resource envelope, runtime validation, and DDIL runtime repair proof for the
selected model/device/runtime path. Runtime fit is scored as
`temms-runtime-fit/v1` with component evidence for compatibility, runtime
validation, performance, resource headroom, and telemetry freshness. Heartbeat
freshness is part of that check: runtime,
resource, and edge target gates move to attention when the selected edge has
not reported within the freshness budget, so a demo cannot claim runtime fit
from stale inventory.
If the selected rollout is already `imported` or `activated`, a newly
incompatible runtime/provider/accelerator inventory is reported as active
runtime drift. The readiness gate includes runtime failure refs, the affected
rollout id/state, and a reviewable rollback command. When a sibling model is
already proven on another compatible runtime target for the same edge, readiness
also offers **Stage fallback model** with that fallback runtime target, approval
enabled, and runtime-validation proof preserved in the generated rollout
command.
Model metadata can also declare `performance_slo`, for example
`max_latency_ms_p95` and `min_throughput_ips`. When an SLO exists, readiness
requires current benchmark evidence for the selected package/model/device/runtime
target and marks the `performance_fit` gate as attention if evidence is missing
or stale, or if the latest benchmark misses the budget. By default, benchmark
evidence must be no more than 24 hours old; set `max_benchmark_age_seconds` in
the model SLO when a runtime target needs a tighter or looser freshness budget.
Compatibility matrix cells include the same compact performance summary so
fleet planners can see which target is compatible but stale, under-proven, or
under-performing.
The matrix also returns ranked `recommendations` for the operator console. A
recommendation score is derived from the same evidence already present in the
cell: technical compatibility, package release state, runtime validation,
performance SLO proof, resource envelope fit, pinned runtime target discipline,
and measured headroom. Decisions such as `deploy`, `validate_runtime`,
`benchmark_or_tune`, `release_required`, and `blocked` are therefore explainable
instead of heuristic-only; each recommendation carries required actions,
warnings, and optimization refs such as latency p95, throughput, and resource
headroom when available.
Runtime recommendations and readiness now also include a
`temms-runtime-fit/v1` object. The fit score is a 0-100 breakdown across
technical compatibility, non-dry-run runtime validation, benchmark/SLO margin,
resource headroom, and edge telemetry freshness. When no runtime target is
specified, readiness chooses the highest-scoring compatible target for the
selected model/device instead of simply using the newest validation record.
When a runtime target is pinned explicitly, readiness embeds
`runtime_fit.target_selection` with schema
`temms-runtime-target-selection/v1`, the selected target rank, best eligible
target, score delta, and top measured alternatives. This is the
operator-facing proof that an on-device path is not just valid, but the best
measured fit among available runtime targets, or that a better target should be
selected before field rollout.
Readiness also emits `runtime_decision.schema_version:
temms-runtime-decision/v1`, a compact decision capsule built from the same fit,
target-selection, gate, and production-admission evidence. It records the
selected path, recommended action, selected target, best target, score delta,
runtime lane, artifact lane, runtime capability lock, blocking/attention gates,
and top measured alternatives. Edge-runtime proof artifacts include this
capsule so an operator or auditor can verify why TEMMS selected, blocked, or
retargeted a runtime without reconstructing the decision from scattered
readiness fields.
Readiness and edge proof artifacts also expose
`runtime_workbench.schema_version: temms-runtime-workbench/v1`. This is the
canonical backend-ranked workbench contract consumed by the React first screen:
selected and best runtime target IDs, target-selection status, production
admission summary, selected/best target details, and one compact row per known
runtime target with eligibility, score, lane, validation, benchmark,
resource/telemetry proof, capability-lock status, blocker reasons, and next
remediation action. The UI should prefer this object whenever it is present so
runtime targeting remains identical across API, CLI, DDIL proof, downloaded
JSON, and browser demos.
Edge-runtime proof artifacts additionally expose
`edge_execution_manifest.schema_version:
temms-edge-execution-manifest/v1`, derived from the selected workbench target
and execution contract. The manifest gives auditors a compact signed execution
intent: runtime image, model/artifact lane, device id, capability digest,
validation/benchmark evidence, gate policy, and admission result for the
selected path.
The Runtime workbench renders those rows as a **Runtime decision trace** below
the ranked table, so field operators can inspect rank, selected/best state,
validation, benchmark, resource, telemetry, capability digest, blocker reason,
and the exact copyable operator or edge-run remediation command for each target
without opening raw JSON.
The React cockpit surfaces the same capsule as an active **Edge runtime
mission** path followed by **Edge execution contract** above the broader
verdict and proof lanes. The active path panel keeps model ID, selected target
runtime, edge node, target coverage counts, admission, signed proof status, and
a compact selected/blocked runtime-lane strip together in the first viewport.
The contract panel remains the operator's
deeper fit inspection surface: it shows the selected model -> runtime -> edge
path, ranked runtime candidates, runtime lane, artifact path,
`runtime_capability_lock`, resource evidence, admission state, full
`target_assessments` coverage for eligible and blocked runtime lanes, and a
non-mutating **Use best runtime** control when the pinned runtime is not the
measured best target. Each target assessment carries the lane, fit score,
selected/best flags, compact component states, capability-lock summary, and
reasons or penalties so an operator can explain why CPU, CUDA, TensorRT, or
TFLite was selected or rejected for the current edge. Assessments also include
`remediation` with a compact next action, such as `ready`,
`validate_runtime`, `record_benchmark`, `refresh_edge_inventory`,
`package_runtime_artifact`, or `select_matching_edge_class`, and whether that
action must execute on the edge. Those remediation records carry copyable
command payloads in the same contract the API signs and the CLI verifies:
`record_benchmark` includes an edge-local
`temms benchmark ... --hub-url` command, `validate_runtime` produces a
`temms hub validate-runtime` command with an explicit package-path placeholder,
`refresh_edge_inventory` produces an edge daemon heartbeat command, and generic
capability or edge-class blockers produce a compatibility-matrix/proof check
instead of mutating state. The cockpit renders those contract-provided commands
directly and only falls back to local synthesis when connected to an older
daemon. The React readiness remediation panel shows commands marked
`requires_edge_execution` as copyable **Edge execution command** handoffs and
keeps the browser run button disabled for them, because heartbeat, benchmark,
and runtime-validation evidence must be produced on the actual edge/runtime
surface.
The same view is also emitted by the API as
`edge_execution_contract.schema_version:
temms-edge-execution-contract/v1` and embedded in signed edge-runtime proof
artifacts. This keeps the browser, CLI verifier, evidence exports, and offline
audits tied to one contract instead of recreating the edge path from separate
readiness fields.
Readiness also renders this as a **Runtime optimizer** gate. The gate is green
when the selected target is the best measured fit, attention when a pinned
target has a higher-scoring eligible alternative, and blocked when no eligible
target remains. The attention state exposes a non-mutating **Use best runtime**
action with `kind: select_runtime_target`, so the cockpit can switch the
operator's selected model/device/runtime context before creating a rollout. For
already-buffered DDIL deploy intents, the pending ledger uses the same Runtime
optimizer refs to call `/v1/control/sync/retarget-runtime`; the daemon rewrites
the queued runtime target only when Hub target assessments prove the requested
target is the best eligible runtime with non-dry-run validation, benchmark
evidence, and a locked capability hash from fresh edge inventory. The signed
`_temms_runtime_retarget` audit entry preserves that runtime proof with the
previous selected target, proved selected target, best target, runtime-workbench
selection status, target counts, selected-is-best flag, validation id, benchmark
id, capability digest, and `target_assessment_sha256`. Replay rechecks the live
target-assessment digest, capability hash, validation id, benchmark id,
eligibility, and best-target status before applying the repaired queue, so a
proof minted before runtime image, runtime lane, artifact, evidence, or edge
inventory drift cannot be replayed silently.
The React cockpit keeps that audit visible as **DDIL runtime repair proof** and
**DDIL repair evidence** cards, including the queued runtime, proved runtime,
best measured runtime, fit score, capability lock, validation id, benchmark id,
target coverage, and replay source after the queue drains.
If the operator quarantines a blocked DDIL intent first, the same recovery loop
can put it back into service after the edge evidence is fixed:
`/v1/control/sync/requeue-dead-letters` runs current preflight against the
quarantined signed payload and restores it to the active replay queue only when
the intent is ready. The dead-letter record is kept with `requeued_at`,
`requeued_by`, `requeue_reason`, and the original digest. Blocked candidates
stay quarantined with the current preflight reason; `force: true` is reserved
for explicit break-glass drills. The Hub ledger exposes this as **Requeue
intent** beside each quarantined row, so runtime remediation does not require
losing the forensic quarantine record.
The same field repair path is scriptable from the edge node:

```bash
uv run temms control sync-preview --control-url http://127.0.0.1:8080
uv run temms control retarget-runtime \
  --control-url http://127.0.0.1:8080 \
  --payload-sha256 <pending-payload-sha256> \
  --actor operator:edge-runtime-drill
uv run temms control requeue-dead-letters \
  --control-url http://127.0.0.1:8080 \
  --payload-sha256 <quarantined-payload-sha256> \
  --actor operator:edge-runtime-drill \
  --reason "edge runtime proof remediated"
uv run temms control sync --control-url http://127.0.0.1:8080
```

If `--runtime-target-id` is omitted, `retarget-runtime` selects the measured
candidate carried by the Runtime optimizer gate; supplying the flag makes the
operator's target explicit. `requeue-dead-letters` requires the restored intent
to pass current preflight by default; add `--force` only for a deliberate
break-glass drill where the operator wants to inspect the active queue response
without treating the intent as field-ready.
Rollout apply performs the same edge-safety preflight before the daemon marks a
rollout as `downloading` or touches the model loader. Dashboard discovery can
show attention-level gates so operators know what to fix, but apply fails closed
with HTTP `409` when a pinned runtime target is not validated/current, the
selected edge target is stale/offline, or declared `performance_slo` or
`resource_requirements` cannot be proven from fresh evidence. The response
includes `blocking_gates` plus the full readiness payload, and the rollout
remains in its assigned/approved state so the operator can benchmark, refresh
inventory, validate the runtime, or choose a healthier recommendation without
mislabeling the attempt as a failed import.
The daemon also uses local activation preflight for policy and fallback
hot-swaps when the selected model's package exists in Hub Lite. A policy can
still react quickly to changing conditions, but the switch is refused before
`LOADING` when current edge telemetry proves the model cannot satisfy runtime,
performance, resource, package, or edge-target gates. The old model remains
running, fallback candidates are checked through the same admission path, and
decision evidence records `activation_preflight` for the model that ultimately
runs. Blocked switches emit `slot.activation_preflight_blocked` telemetry with
the selected package/model/device, blocking gates, and readiness selection.
Daemon startup default-model activation uses the same local preflight. A
resource-unsafe default is refused before `load_model`, the slot remains
`stopped`, and `slot.startup_failed` records `failure_kind:
readiness_preflight` plus the blocking gates; safe startup activations store
`activation_preflight` in the decision audit metadata.
Control-plane activations are also admission-controlled. Operator overrides,
rollback targets, and queued DDIL deploy/override replay call the same Hub Lite
activation preflight before loading a model when package/device context exists.
Unsafe control actions return HTTP `409` with `blocking_gates` and readiness
detail; safe actions add `activation_preflight` to the slot decision audit
metadata and telemetry payload.
The `/v1/control/slots/{slot}/evaluate` apply path is wired through the same
activation preflight callback when Hub Lite is configured, so API-triggered
adaptive applies cannot bypass edge runtime, resource, performance, package, or
target admission.
If the selected rollout is already `imported` or `activated`, a benchmark that
now misses the model SLO is reported as active performance drift. The readiness
gate includes the benchmark id, measured p95/throughput, declared SLO, rollout
id/state, and a reviewable rollback command, while missing benchmark evidence
or stale benchmark evidence for an active rollout is treated as drift that
cannot yet be proven.
When another model in the same package is compatible with the selected
runtime/device and already satisfies both performance and resource checks,
readiness also offers **Stage fallback model**, a deterministic rollout command
for that healthier model with approval enabled. If the package/runtime target
already has passing runtime validation evidence, the fallback command preserves
that discipline with `require_runtime_validation: true` and includes the
validation id in readiness refs.
Model metadata can declare `resource_requirements`, including
`min_memory_available_mb`, `min_storage_available_mb`, `max_temperature_c`,
`min_battery_percent`, and `required_power_source`. Edge inventory can report
matching `memory`, `storage`, `thermal`, and `power` blocks. Readiness blocks
when telemetry proves the node is resource-constrained, warns when declared
resource requirements cannot be verified from current telemetry, and marks the
`resource_envelope` gate as `go` only when the observed node envelope satisfies
the model. Compatibility matrix cells include the same `resource_envelope`
summary plus resource-ready/blocker counts.
If the selected rollout is already `imported` or `activated`, a newly failing
resource envelope is reported as active resource drift instead of a generic
preflight constraint. The readiness response keeps the failure refs, adds the
rollout id/state, and exposes a reviewable rollback command for the affected
rollout, so operators can prove why an edge model was pulled back after RAM,
storage, thermal, battery, or power telemetry changed.
The same viable-fallback search is used for resource drift, so a lighter sibling
model can be staged immediately when it is proven to fit the degraded node,
without bypassing rollout approval or runtime-validation proof.

## Product UI

The Hub product UI is served at `/ui/hub`. Hub-enabled daemons redirect `/ui/`
to that product cockpit. The current UI opens as **Mission Package Workbench** and
is organized around the operator path **Mission -> Model Plan -> Runtime Fit ->
Sensor Handling -> Package Handoff -> Edge Deploy -> Field Ops**:

- The default shell opens as a **Mission workflow cockpit**: an operator path
  rail, one focused current-stage decision panel, package path signals, and a
  compact **Live context** drawer for inventory, rollout, evidence, and DDIL
  telemetry. The first screen reads as a packaging workflow instead of a status
  dump.
- **Mission** captures the goal or uses **Import YAML** to load a mission spec
  that hydrates sensor, SLO, switching, fallback, and DDIL fields for the
  downstream package plan. Matching `model_id`, `package_id`, `device_id`, and
  `runtime_target_id` hints also preselect the model/runtime/edge path when
  those ids exist in Hub inventory.
- **Model Plan** owns signed model inventory, selected model/package release
  state, declared performance SLO, resource envelope, and benchmark evidence.
  Package registration, edge enrollment, and bundle import are available under
  **Advanced intake**.
- **Runtime Fit** preserves the model chosen in **Model Plan** as locked
  context, owns edge node and target runtime selection, then ranks runtime
  targets by fit score, validation, benchmark freshness, live inventory match,
  and blocker state from
  `runtime_workbench.schema_version: temms-runtime-workbench/v1`, with a compact
  on-device capability vector and Runtime decision trace.
- **Sensor Handling** owns sensor input, slot, latency/throughput SLO,
  model-switch policy, fallback model, and DDIL behavior.
- **Package Handoff** owns mission package planning, the mission-to-deploy binding
  chain, stable package identity, field handoff hashes, and rollout staging.
  Runtime proof generation, readiness gates, component digests, and the edge
  execution contract remain available under **Advanced verification** for
  operator drill-down.
- The operator path rail and stage focus panel show the current stage,
  ready condition, risk, previous/next movement, and stage-specific actions.
  This keeps the default path operational: **Stage rollout** is not enabled
  until **Plan package** returns the stable mission package identity and
  deployment intent with a passed proof gate.
- **Edge Deploy** opens on the planned mission package deploy lane. Direct
  rollout forms remain available under **Manual controls**, while rollout-plan
  and fleet panels surface only when a workflow action opens them.
- **Field Ops** owns DDIL link state, pending/quarantined intent repair, mission
  replay, evidence export, and air-gap evidence bundle handoff.
- evidence summary, mission replay, full bundle, and air-gap bundle export

The Operational verdict panel is backed by `/v1/hub/readiness` and falls back to
client-side derivation only when an older daemon does not expose that endpoint.
When a selected model needs operator work, the panel shows gate-specific action
buttons that focus the matching workflow section without opening raw JSON.
The deployment path also includes an **On-device capability dossier** for the
selected model/device/runtime target. It condenses runtime fit, resource
envelope, performance proof, runtime validation, live edge inventory, target
requirements, runtime-fit component scores, and the current Hub admission gate
into one operator-readable view so field users can explain why a model is safe,
blocked, or still needs proof before rollout.

Build the React + TypeScript bundle before testing UI changes:

```bash
npm --prefix ui install
npm run typecheck
npm run build
npm run smoke:workbench
```

Those repo-root npm commands delegate to the React + TypeScript project in
`ui/`. The equivalent Makefile targets are `make ui-install`,
`make ui-typecheck`, `make ui-build`, and `make ui-smoke`.

For a local seeded rehearsal with signed models, rollout state, runtime
validation, fresh demo benchmark evidence, and mission proof already present,
follow `docs/functional-testing.md`. The Docker demo seed refreshes its own
synthetic edge benchmark rows on startup; real performance proof for production
paths should still be recorded from the selected device/runtime with
`temms benchmark ... --hub-url`.

## CLI

The same MVP workflow is available through `temms hub`:

```bash
temms hub enroll \
  --hub-url http://hub-vm:8080 \
  --device-id edge-1 \
  --device-profile x86_64-cpu \
  --label site=lab \
  --inventory runtime=onnx

temms hub register-package ./dist/mlflow-detector-7.temms.tar.zst \
  --hub-url http://hub-vm:8080 \
  --token "$TEMMS_HUB_TOKEN"

temms hub package-from-mlflow models:/detector/7 \
  --hub-url http://hub-vm:8080 \
  --token "$TEMMS_HUB_TOKEN" \
  --slot vision \
  --tracking-uri http://mlflow.example:5000 \
  --device-profile x86_64-cpu \
  --runtime onnxruntime \
  --provider CPUExecutionProvider \
  --actor operator:alice

temms hub register-runtime \
  --hub-url http://hub-vm:8080 \
  --runtime-target-id customer-orin \
  --image registry.example.com/customer/orin-runtime:2026.06 \
  --device-profile orin-tensorrt \
  --runtime onnxruntime \
  --runtime tensorrt \
  --provider CUDAExecutionProvider \
  --accelerator nvidia \
  --actor operator:alice

temms hub validate-runtime ./dist/mlflow-detector-7.temms.tar.zst \
  --hub-url http://hub-vm:8080 \
  --runtime-target-id customer-orin \
  --signing-key-file ./hub-signing.key \
  --pull-image

temms hub validate-runtime ./dist/mlflow-detector-7.temms.tar.zst \
  --hub-url http://hub-vm:8080 \
  --runtime-target-id customer-orin \
  --allow-unsigned-package \
  --no-strict-metadata \
  --dry-run \
  --json

temms hub promote-package mlflow-detector-7 \
  --hub-url http://hub-vm:8080 \
  --promotion-state validated \
  --actor operator:validator \
  --reason "runtime validation passed"

temms hub promote-package mlflow-detector-7 \
  --hub-url http://hub-vm:8080 \
  --promotion-state approved \
  --actor operator:approver \
  --reason "package approved for mission release"

temms hub promote-package mlflow-detector-7 \
  --hub-url http://hub-vm:8080 \
  --promotion-state released \
  --actor operator:release \
  --reason "released for edge rollout"

temms hub assign \
  --hub-url http://hub-vm:8080 \
  --device-id edge-1 \
  --package-id mlflow-detector-7 \
  --model-id model-detector-7 \
  --slot vision \
  --rollout-id rollout-vision-001 \
  --runtime-target-id customer-orin \
  --actor operator:alice

temms hub export \
  --hub-url http://hub-vm:8080 \
  --include-packages \
  --output hub-lite-package-bundle.json

temms hub import hub-lite-package-bundle.json \
  --hub-url http://edge-vm:8080

temms hub apply rollout-vision-001 \
  --hub-url http://edge-vm:8080 \
  --actor edge:edge-1
```

Use `temms hub devices`, `temms hub packages`, `temms hub runtime-targets`, `temms hub rollouts`, and `temms hub status` to inspect fleet state. Add `--json` to any command for scriptable output.
Hub CLI package registration, Hub MLflow packaging, runtime validation, and rollout apply require production metadata and signature verification by default; use `--no-strict-metadata` or `--allow-unsigned-package` only for isolated labs.
Use `preview-compatibility` before assignment to check package/device/runtime fit without creating a rollout:

```bash
temms hub preview-compatibility \
  --hub-url http://hub-vm:8080 \
  --device-id edge-1 \
  --package-id pkg-vision-1 \
  --model-id model-yolov8-lowlight-001 \
  --runtime-target-id temms-x86_64-cpu

temms hub compatibility-matrix \
  --hub-url http://hub-vm:8080 \
  --package-id pkg-vision-1 \
  --model-id model-yolov8-lowlight-001 \
  --include-device-inventory

temms hub create-rollout-plan \
  --hub-url http://hub-vm:8080 \
  --plan-id plan-vision-1 \
  --package-id pkg-vision-1 \
  --model-id model-yolov8-lowlight-001 \
  --target-device-id edge-1 \
  --target-device-id edge-2 \
  --runtime-target-id temms-x86_64-cpu \
  --batch-size 1 \
  --require-approval

temms hub advance-rollout-plan plan-vision-1 --hub-url http://hub-vm:8080
```

`register-package`, `package-from-mlflow`, and `validate-runtime` all use strict metadata by default. `validate-runtime` fetches the selected runtime target from Hub Lite and runs `temms package validate --check-runtime --strict-metadata` inside that target's container image. Use `--dry-run` to see the exact `docker run` command before executing it. Use `--no-strict-metadata` only for lab packages that predate the production metadata contract.
Each validation or dry-run is written back to Hub Lite as runtime validation evidence with the target image, package path or package ID, pass/fail state, actor, timestamp, and a redacted command. Inspect those records with `temms hub runtime-validations`; evidence exports include them in `runtime_validations` and in the merged audit timeline. Evidence exports also derive `runtime_fit_evidence` from the same readiness engine used by `/v1/hub/readiness`, dedupe repeated rollout proofs, and rank active slot evidence first so mission replay proves the selected model/device/runtime score. A lower-scoring pinned target is flagged as `preview_only` instead of fully optimized.
For stricter rollout control, pass `--package-id` when validating and `--require-runtime-validation` when assigning. Hub Lite then requires a non-dry-run passing validation record for that exact package artifact and runtime target before it creates the rollout. The package must also be promoted to `released`; use `temms hub promote-package` to record the validated, approved, and released transitions.
`compatibility-matrix` expands the same preview checks across selected packages, models, devices, and runtime targets. For multi-model packages, unfiltered results include a separate cell per declared model, and `--model-id` or API `model_ids` filters constrain the matrix to the exact on-device workload under review. Each cell includes `model_id` when it was evaluated against a declared package model. The matrix separates technical compatibility from assignment readiness, so a package can show as compatible while still blocked by package promotion state or missing runtime validation evidence. Runtime-target cells also evaluate reported edge inventory when it is present; mismatched live runtimes, ONNX providers, or accelerators are technical blockers, not cosmetic warnings. Use the returned `recommendations` list when you need the highest-confidence edge path first; it ranks cells by deployability, runtime validation, SLO/resource proof, and optimization headroom while preserving required actions for anything short of deploy-ready.
`create-rollout-plan` records a staged rollout plan across multiple devices. Advancing the plan creates the next assignment batch through the same release, compatibility, validation, and approval gates as `assign`, then records plan history in evidence exports and mission replay. Use `pause-rollout-plan` and `resume-rollout-plan` to hold a canary or batch while operators inspect health evidence.
`apply-rollout` is stricter than assignment: before import or activation it
runs an apply-time readiness preflight against the selected package, model,
device, runtime target, and slot. Pinned runtime targets must have passing
runtime validation, and any declared model SLO or resource envelope must have
fresh edge evidence. A blocked preflight returns HTTP `409` with
`blocking_gates`; fix the evidence or select another recommended path, then
retry the same rollout.

For a local x86 VM or laptop rehearsal, build the same image tag used by the built-in `temms-x86_64-cpu` runtime target:

```bash
make docker-build-runtime

temms hub validate-runtime ./dist/pkg-vision.temms.tar.zst \
  --hub-url http://localhost:8080 \
  --package-id pkg-vision-1 \
  --runtime-target-id temms-x86_64-cpu \
  --allow-unsigned-package
```

The TEMMS container entrypoint executes explicit commands directly, so the validation container runs `temms package validate` instead of starting the daemon. Customer runtime images should follow the same contract: when Hub passes a command, execute it as the container process.

## HTTP API

Enroll a device:

```bash
curl -X POST http://localhost:8080/v1/hub/devices/enroll \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "edge-1",
    "profile": "x86_64-cpu",
    "labels": {"site": "lab"},
    "inventory": {"python": "3.11", "runtime": "onnx"}
  }'
```

Send a heartbeat:

```bash
curl -X POST http://localhost:8080/v1/hub/devices/edge-1/heartbeat \
  -H "Content-Type: application/json" \
  -d '{
    "status": "online",
    "inventory": {"runtime": "onnx"},
    "deployment_status": {"state": "READY"}
  }'
```

Build, Hub-sign, and register a package from the MLflow registry:

```bash
curl -X POST http://localhost:8080/v1/hub/packages/from-mlflow \
  -H "Content-Type: application/json" \
  -d '{
    "model_uri": "models:/detector/7",
    "slot": "vision",
    "tracking_uri": "http://mlflow.example:5000",
    "device_profile": "x86_64-cpu",
    "runtime_constraints": {"runtimes": ["onnx"]},
    "runtime_options": {"providers": ["CPUExecutionProvider"]},
    "archive": true
  }'
```

Hub writes generated artifacts under its local `packages/` state directory by default, signs them with signer `temms-hub-lite` when daemon signature policy is enabled, validates strict production metadata unless `"strict_metadata": false` is set for a lab package, and returns both the catalog entry and package path. Existing `models:/name/version` outputs are immutable unless the request sets `"overwrite": true`, which should be reserved for isolated lab rebuilds before registration or distribution.

Promote a package through release before assignment:

```bash
curl -X POST http://localhost:8080/v1/hub/packages/pkg-vision-1/promote \
  -H "Content-Type: application/json" \
  -d '{"state": "validated", "actor": "operator:validator", "reason": "runtime validation passed"}'

curl -X POST http://localhost:8080/v1/hub/packages/pkg-vision-1/promote \
  -H "Content-Type: application/json" \
  -d '{"state": "approved", "actor": "operator:approver", "reason": "package approved for release"}'

curl -X POST http://localhost:8080/v1/hub/packages/pkg-vision-1/promote \
  -H "Content-Type: application/json" \
  -d '{"state": "released", "actor": "operator:release", "reason": "released for rollout"}'
```

Register and Hub-sign a package artifact directly from its manifest:

```bash
curl -X POST http://localhost:8080/v1/hub/packages/register \
  -H "Content-Type: application/json" \
  -d '{
    "package_path": "/packages/pkg-vision-1.temms.tar.zst",
    "require_signature": true
  }'
```

This derives `package_id`, name, version, source SHA256, compatible device profiles, model metadata, policy metadata, provenance, signature verification status, and strict metadata validation status from the package itself. If the Hub daemon is configured with `TEMMS_PACKAGE_SIGNING_KEY_FILE` or `TEMMS_PACKAGE_SIGNING_KEY` and signatures are required, registration first writes or replaces `signature.json` using signer `temms-hub-lite`, then catalogs the verified artifact. The catalog keeps `source_sha256` for the registered package source even when a directory package is later streamed as an archive. On deployed Hub VMs, keep the signing key in the daemon environment so the key does not travel in request JSON.

Edges that use online sync fetch assigned package bytes from:

```bash
curl -o pkg-vision-1.temms.tar.zst \
  http://localhost:8080/v1/hub/packages/pkg-vision-1/artifact
```

The response includes `X-TEMMS-Package-Filename`, `X-TEMMS-Package-SHA256`, `X-TEMMS-Package-Artifact-SHA256`, and `X-TEMMS-Package-Source-SHA256` headers. Directory packages are streamed as `.temms.tar.zst` archives, so the source SHA and artifact SHA may differ; both are carried into edge cache metadata and air-gap bundles for audit.

Before Hub Lite serves an online package artifact, embeds one in an air-gap bundle, or applies a rollout from a local package path, it re-checks the cataloged source SHA256. If the package file or directory changed after registration, distribution or apply fails with a conflict instead of using a different artifact under the old catalog entry.

You can still register a package catalog entry manually:

```bash
curl -X POST http://localhost:8080/v1/hub/packages \
  -H "Content-Type: application/json" \
  -d '{
    "package_id": "pkg-vision-1",
    "name": "vision-models",
    "version": "1.0.0",
    "path": "/packages/pkg-vision-1.temms",
    "device_profiles": ["x86_64-cpu"]
  }'
```

List runtime targets:

```bash
curl http://localhost:8080/v1/hub/runtime-targets
```

Hub Lite starts with default runtime targets for `x86_64-cpu`, `arm64-jetson`, `rpi5-tflite`, and `orin-tensorrt`. A runtime target is the container execution environment used for simulation or validation: image reference, OS, architecture, compatible device profiles, available runtimes, ONNX providers, accelerators, and optional labels. Customers can bring their own runtime images without changing package metadata:

```bash
curl -X POST http://localhost:8080/v1/hub/runtime-targets \
  -H "Content-Type: application/json" \
  -d '{
    "runtime_target_id": "customer-orin",
    "name": "Customer Orin Runtime",
    "image": "registry.example.com/customer/orin-runtime:2026.06",
    "os": "linux",
    "arch": "arm64",
    "device_profiles": ["orin-tensorrt"],
    "runtimes": {
      "onnxruntime": {
        "available": true,
        "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"]
      },
      "tensorrt": {"available": true}
    },
    "accelerators": {"nvidia": {"available": true}},
    "runtime_constraints": {
      "device_profiles": ["orin-tensorrt"],
      "runtimes": ["onnxruntime", "tensorrt"],
      "preferred_providers": ["CUDAExecutionProvider"],
      "accelerators": ["nvidia"]
    },
    "labels": {"customer": "acme"}
  }'
```

Assign a rollout. The package must already be `released`:

```bash
curl -X POST http://localhost:8080/v1/hub/compatibility/preview \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "edge-1",
    "package_id": "pkg-vision-1",
    "model_id": "model-yolov8-lowlight-001",
    "runtime_target_id": "temms-x86_64-cpu"
  }'

curl -X POST http://localhost:8080/v1/hub/compatibility/matrix \
  -H "Content-Type: application/json" \
  -d '{
    "device_ids": ["edge-1"],
    "package_ids": ["pkg-vision-1"],
    "model_ids": ["model-yolov8-lowlight-001"],
    "runtime_target_ids": ["temms-x86_64-cpu"],
    "include_device_inventory": true
  }'

curl -X POST http://localhost:8080/v1/hub/rollout-plans \
  -H "Content-Type: application/json" \
  -d '{
    "plan_id": "plan-vision-1",
    "package_id": "pkg-vision-1",
    "model_id": "model-yolov8-lowlight-001",
    "device_ids": ["edge-1", "edge-2"],
    "slot": "vision",
    "runtime_target_id": "temms-x86_64-cpu",
    "batch_size": 1,
    "require_runtime_validation": true,
    "require_approval": true
  }'

curl -X POST http://localhost:8080/v1/hub/rollout-plans/plan-vision-1/advance \
  -H "Content-Type: application/json" \
  -d '{"actor": "operator:planner"}'

curl -X POST http://localhost:8080/v1/hub/rollouts \
  -H "Content-Type: application/json" \
  -d '{
    "rollout_id": "rollout-1",
    "device_id": "edge-1",
    "package_id": "pkg-vision-1",
    "model_id": "model-yolov8-lowlight-001",
    "slot": "vision",
    "runtime_target_id": "temms-x86_64-cpu",
    "require_runtime_validation": true,
    "require_approval": true
  }'
```

If the package catalog entry declares `device_profiles`, Hub Lite checks that the enrolled device profile is included before creating the assignment. When an assignment names `model_id`, Hub Lite validates that the model is declared by the package and filters model-level runtime constraints to that model; package-only assignments keep the older package-wide check. When the assignment names a `runtime_target_id`, Hub Lite checks the selected model/package constraints against that container target's declared runtimes, ONNX providers, accelerators, OS/arch metadata, and compatible device profiles. When `require_runtime_validation` is true, Hub Lite also requires a passing non-dry-run validation record for the selected package/runtime target and embeds a compact validation summary in the rollout. Without a runtime target, Hub Lite falls back to the device heartbeat inventory. Rollout apply repeats package-level and model-level runtime constraint checks on the edge before import or activation, which keeps air-gap/manual paths aligned with online assignment policy. This keeps `orin-tensorrt`, `tflite`, or customer-provided runtime images from being assigned to incompatible VMs by accident.

Rollout plans are coordination records, not a weaker assignment path. Each
target still becomes an ordinary Hub Lite rollout only when `advance` assigns
that batch, and those rollouts keep their approval, validation, runtime target,
and package release metadata. After all pending targets are assigned, the plan
enters `advancing` while its rollouts move through download, import, activation,
failure, or rollback. The plan becomes `completed` only when every assigned
target reaches a terminal `activated` or `rolled_back` outcome. Plan history
records created, advanced, paused, resumed, reconciled, and completed events,
which travel in air-gap bundles and evidence exports as rollout coordination
proof.

When `require_approval` is true, rollout apply is blocked until an operator or
automation records approval. Approval is stored in the rollout history and
travels with air-gap bundles, so a disconnected edge can still prove that the
Hub-side policy gate was satisfied before apply:

```bash
curl -X POST http://localhost:8080/v1/hub/rollouts/rollout-1/approve \
  -H "Content-Type: application/json" \
  -H "X-TEMMS-Actor: operator:approver" \
  -d '{"reason": "mission policy approved"}'

temms hub assign --device-id edge-1 --package-id pkg-vision-1 \
  --slot vision --rollout-id rollout-1 --require-approval
temms hub approve rollout-1 --actor operator:approver \
  --reason "mission policy approved"
```

The web UI exposes the same gate in **Mission Package Workbench** at
`/ui/hub`. Rollout rows show `pending`, `approved`, or `not required`, and the
Apply button remains disabled for approval-gated rollouts until the operator
records approval. Retired `/ui/operate` and `/ui/runtimes` URLs redirect to the
Hub cockpit so bookmarked demo links land on the supported product UI.
Hub-enabled deployments also redirect legacy diagnostic GET pages such as
`/ui/dashboard`, `/ui/models`, `/ui/import`, `/ui/slots`, `/ui/conditions`, and
`/ui/decisions` to `/ui/hub`. Those diagnostic templates remain available only
when Hub Lite is not configured.

Update rollout state:

```bash
curl -X POST http://localhost:8080/v1/hub/rollouts/rollout-1/status \
  -H "Content-Type: application/json" \
  -d '{"state": "activated", "detail": "loaded on edge-1"}'
```

Valid rollout states:

- `assigned`
- `downloading`
- `imported`
- `activated`
- `failed`
- `rolled_back`

Apply a rollout on the edge agent:

```bash
curl -X POST http://localhost:8080/v1/hub/rollouts/rollout-1/apply \
  -H "Content-Type: application/json" \
  -d '{
    "require_signature": true
  }'
```

Apply reads the package catalog entry, imports the TEMMS package from its local path, promotes package policies, loads the selected model into the rollout slot, activates the slot, and moves rollout state through:

```text
assigned -> downloading -> imported -> activated
```

On failure the rollout is moved to `failed` with the error detail. If no slot is set on the rollout, apply stops after import and leaves the rollout in `imported`.

Export an air-gap bundle:

```bash
curl -X POST http://localhost:8080/v1/hub/airgap/export > hub-lite-bundle.json
```

Export an air-gap bundle that embeds signed package archives:

```bash
curl -X POST http://localhost:8080/v1/hub/airgap/export \
  -H "Content-Type: application/json" \
  -d '{"include_packages": true}' \
  > hub-lite-package-bundle.json
```

When `include_packages` is true, Hub Lite embeds package artifacts from catalog entries that have a readable `path`. Directory packages are archived as `.temms.tar.zst` for transfer. On import, artifacts are written under the receiving Hub Lite state directory in `packages/`, package catalog paths are rewritten to those local files, and SHA256 is checked before the bundle is accepted. Bundle import is conflict-aware: records missing locally are added, newer incoming records replace older local records, but stale bundle records do not overwrite newer local rollout, deployment, or package artifact state. Rollout histories are merged so an edge can import an older central assignment bundle after local activation without losing its `activated` audit trail.

Import an air-gap bundle:

```bash
curl -X POST http://localhost:8080/v1/hub/airgap/import \
  -H "Content-Type: application/json" \
  --data-binary @hub-lite-bundle.json
```

Export a post-mission evidence bundle:

```bash
curl -X POST http://localhost:8080/v1/hub/evidence/export \
  -H "Content-Type: application/json" \
  -d '{"decision_limit": 500, "telemetry_limit": 5000, "include_benchmarks": true}' \
  > temms-evidence-bundle.json
```

Export the same proof as a chronological mission replay:

```bash
curl -X POST http://localhost:8080/v1/hub/evidence/export \
  -H "Content-Type: application/json" \
  -d '{"decision_limit": 500, "telemetry_limit": 5000, "include_benchmarks": true, "replay": true, "replay_limit": 100}' \
  > temms-mission-replay.json

temms evidence --input temms-evidence-bundle.json --replay
```

The evidence bundle combines Hub Lite fleet state, current deployment status, centrally replayed telemetry, doctor-style diagnostics, slots, runtime state, condition snapshot, imported package/model metadata, package import audit events, rollout history with actors, decision logs with package/provenance metadata and package signature verification context, local telemetry events, local benchmark artifacts, Hub-recorded benchmark evidence, and a merged timeline. Policy-driven decision logs include the matched policy, matched rule, rule priority, action, and per-condition evidence with actual value, source, priority, confidence, and match result. Diagnostics include write-probed path health and model cache health, including missing cached model files, size mismatches, and SHA256 mismatches.
Runtime target validation records are included as first-class evidence so operators can prove that a package was preflighted against a customer or default runtime image before assignment or deployment.
Hardware benchmark records are also first-class Hub evidence. Publish them from an edge with `temms benchmark ... --hub-url http://hub-vm:8080 --device-id edge-1 --package-id pkg-vision-1 --runtime-target-id temms-x86_64-cpu`, then inspect central results with `temms hub benchmarks`.
The Hub UI evidence controls render the same proof as operator-facing summary,
mission replay, full bundle, or air-gap export actions. These views expose fleet
counts, package signature and strict metadata posture, mission replay phases,
recent "why models switched" cards with matched condition evidence, and a
merged mission timeline.

Air-gapped edges should normally bring the full evidence bundle back to Hub Lite
after a mission. Hub stores the original bundle, its integrity hash, and an
operator-readable summary, then includes the aggregate in later Hub evidence
exports and mission replay:

```bash
temms hub ingest-evidence temms-evidence-bundle.json \
  --hub-url http://hub-vm:8080 \
  --device-id edge-1 \
  --actor operator:post-mission

temms hub evidence --hub-url http://hub-vm:8080
```

Raw telemetry bundles are still useful when you only need event breadcrumbs:

```bash
temms hub replay-telemetry telemetry-bundle.json \
  --hub-url http://hub-vm:8080 \
  --device-id edge-1 \
  --actor operator:post-mission
```

## Edge Rollback

Rollback is exposed on the edge agent because the agent owns local slot state:

```bash
curl -X POST http://localhost:8080/v1/control/slots/vision/rollback
```

For Hub-managed rollouts, target the rollout directly:

```bash
curl -X POST http://localhost:8080/v1/hub/rollouts/rollout-1/rollback \
  -H "Content-Type: application/json" \
  -d '{"reason": "operator requested rollback"}'
```

The same operation is available from the CLI:

```bash
temms hub rollback rollout-1 --hub-url http://edge-vm:8080
```

The agent selects the previous model from the slot decision log, loads it, activates it, records a `rollback` decision, emits telemetry, and moves the targeted Hub Lite rollout to `rolled_back`.

## Storage

The daemon stores Hub Lite state beside the local TEMMS database as `hub_lite.json`. For non-root/local test runs, this path follows the configured data directory rather than assuming `/var/lib/temms`.
