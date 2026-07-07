# Product Summary

TEMMS makes edge AI trustworthy in the field by controlling what runs,
adapting when local conditions change, and proving why every model decision
happened.

TEMMS is controlled deployment and adaptive runtime decisioning for edge
inference. It ships signed model packages to edge devices, lets a local daemon
choose the right model from local conditions, keeps inference useful when the
network is unavailable, and exports evidence showing why each model ran.

The core problem is that edge AI models are often deployed as static artifacts,
but field conditions are dynamic. Fog, darkness, thermal pressure, low battery,
degraded sensors, runtime failures, network loss, and operator input can all
change which model should be active. TEMMS sits beside the inference
application and makes that choice locally.

## Product Loop

The product loop is:

```text
mission spec / YAML
-> model and runtime plan
-> sensor and model-handling policy
-> signed edge package
-> edge runtime proof
-> edge rollout
-> local condition monitoring
-> policy-driven model selection
-> hot-swap / fallback / rollback
-> operator override
-> evidence export
```

The canonical demo follows this loop end to end: deploy signed vision models,
coordinate a staged rollout plan, start with a daylight model, simulate fog,
switch to a low-light model, serve an inference request while offline, simulate
low battery, switch to a smaller model, trigger a model-load failure, execute
fallback, apply operator override, and export evidence, then ingest that evidence
into Hub Lite for central aggregation and mission replay.

Run it with:

```bash
make product-demo
temms evidence --input temms-canonical-evidence.json --summary
temms evidence --input temms-canonical-evidence.json --replay
```

When a daemon is running, the same mission replay shape is available from the
API:

```bash
curl "http://localhost:8080/v1/evidence?summary=true&summary_limit=20" | python -m json.tool
curl "http://localhost:8080/v1/evidence?replay=true&replay_limit=50" | python -m json.tool
```

## Product Layers

TEMMS has two main layers:

- TEMMS Hub prepares missions before they reach the device: mission spec/YAML,
  model selection, runtime planning, sensor bindings, model switch/fallback
  policy, package assembly, signing, compatibility matrices, runtime/device
  validation, rollout approval, staged rollout plans, and evidence aggregation.
  The guided **Mission Package Workbench** UI now follows the operator path:
  **Mission -> Model Plan -> Runtime Fit -> Sensor Handling -> Package Handoff
  -> Edge Deploy -> Field Ops**.
  The shell opens as a **Mission workflow cockpit** with an operator path rail,
  a focused current-stage decision panel, package path signals, and a compact
  **Live context** drawer for inventory, rollout, evidence, and DDIL telemetry,
  so operators start with a deployable workflow rather than a mixed status board.
  It starts with a mission package builder that captures the goal or uses
  **Import YAML** to hydrate the mission spec into sensor input, slot,
  latency/throughput SLO, confidence-based model switching, fallback model, and
  DDIL behavior. If the spec carries known `model_id`, `package_id`,
  `device_id`, or `runtime_target_id` hints, the workbench also preselects the
  matching model/runtime/edge path before package planning. Those handling
  controls then get their own **Sensor Handling** step, while model/runtime
  targeting moves into explicit planning steps. The resulting
  `temms-edge-mission-package/v1` manifest binds the selected model, runtime
  target, edge node, policy, and proof gates.
  The manifest is now backed by `POST /v1/hub/mission-package/plan`, which
  reuses Hub readiness to hash the stable mission identity: mission, selection,
  SLO, handling policy, DDIL behavior, runtime plan, and advisory proof gate.
  When callers submit `mission_yaml`, the planner derives missing selection,
  SLO, handling, and DDIL fields from that spec before readiness evaluation;
  explicit JSON fields still override YAML hints.
  From the package stage, operators can stage a validation- and
  approval-gated rollout directly from that plan; the rollout reason carries the
  mission package identity digest, and the `deployment_intent` block carries the exact
  `POST /v1/hub/rollouts` body so package planning and edge deployment remain
  traceable as one handoff. The same artifact carries an `edge_handoff` block
  with schema `temms-edge-mission-package-handoff/v1`, mode
  `stage_approve_apply`, and the stage, approve, apply, and digest-verification
  runbook expected by the edge operator. Operators can also download that package plan from
  `POST /v1/hub/mission-package/download`, which returns the same JSON artifact
  with package-identity, payload, runtime-plan, and deployment-intent digest
  headers.
  The CLI mirrors the same handoff with `temms hub mission-package-plan` and
  `temms hub mission-package-download`, accepting a mission YAML file plus
  explicit overrides for sensor, SLO, switching, fallback, and DDIL behavior.
  `POST /v1/hub/mission-package/stage` and
  `temms hub mission-package-stage` then accept the downloaded package artifact,
  verify its identity/payload/deployment-intent digest chain plus the passed
  proof gate, preserve the embedded `edge_handoff`, and stage the package-bound
  rollout without reconstructing the model/runtime/device body by hand. Advisory
  or failed proof-gate packages remain inspectable but are fail-closed at deploy time.
  The Hub stage renderer keeps the active step narrow: model selection under
  **Model Plan**, runtime ranking under **Runtime Fit**, sensor/model switching
  policy under **Sensor Handling**, package/proof handoff under **Package
  Handoff**, package-bound rollout staging under **Edge Deploy**, and DDIL plus
  evidence export under **Field Ops**. Setup-only controls such as package
  registration and edge enrollment sit behind **Advanced intake**, and direct
  rollout forms sit behind **Manual controls**.
  The **Package Handoff** step now shows a mission package binding chain before action:
  the product smoke covers that chain end to end by planning the package,
  downloading the artifact, staging through `/mission-package/stage`, approving
  the policy gate, and applying the rollout until the selected edge path is
  activated.
  mission spec, selected model/runtime/edge path, handling policy, and deploy
  intent are visible as the exact package boundary. Readiness gates, runtime
  mission proof, proof artifacts, and the edge execution contract are preserved
  behind **Advanced verification** so the default demo path stays package-first
  while still exposing the full proof system when needed.
  The cockpit's stage focus keeps the current step, operator decision, ready condition,
  operational risk, previous step, next step, and the small set of actions for
  that stage visible so operators can drive the demo as a continuous
  mission-to-edge workflow instead of a dashboard hunt. For example,
  **Package Handoff** keeps
  **Stage rollout** disabled until **Plan package** has produced the mission
  package identity, deployment intent, and passed proof gate.
  The runtime stage still includes the **Runtime workbench** for ranking every
  available target by measured fit, validation, benchmark freshness, live
  inventory, and blocker state. It preserves the model chosen in **Model Plan**
  as locked context, so Runtime Fit only changes the edge node and runtime
  target rather than reopening model selection. That workbench is backed by the canonical
  `temms-runtime-workbench/v1` contract in Hub readiness and edge proof
  artifacts, so the UI, CLI, downloaded JSON, DDIL retarget proof, and API all
  explain the same selected target, best target, capability lock, benchmark,
  telemetry, and blocked-runtime reasons. The package stage shows runtime-fit
  score, runtime lane,
  selected/blocked runtime alternatives, artifact fit, live edge inventory,
  declared SLO proof, resource envelope, runtime validation, and DDIL runtime
  repair proof before the operator reaches lower-level workflow controls. The
  next lane produces the
  edge-runtime proof artifact for that exact path through Hub, lets the operator
  download the server-backed JSON artifact for handoff, mirrors the download
  response headers for payload, attestation, and component-digest parity, and
  keeps the local CLI commands beside it, including offline `verify-edge-proof`
  gate verification for `go`, best measured runtime selection, runtime fit,
  proof freshness, exact path binding, capability-locked runtime, provider, and
  accelerator evidence, and proof attestation when a signing key is configured. Signed model
  inventory, signed runtime proof artifacts, compatible runtime targets,
  benchmark proof, rollout approval/apply controls, and mission evidence remain
  in the same cockpit, so policy approval is part of the normal operator path
  rather than a hidden API.
  Active rollouts are rechecked against fresh benchmark, runtime capability, and
  edge telemetry, so lost providers, missing accelerators, latency, throughput,
  RAM, storage, thermal, battery, or power drift becomes a readiness finding
  with rollback evidence instead of silent demo-state optimism. Heartbeat and
  benchmark freshness are both part of readiness, so stale edge inventory or
  stale SLO proof cannot keep a deployment green. When a sibling model is
  already proven to fit the same degraded node or another compatible runtime
  target, Hub readiness can stage that fallback as an approval- and
  runtime-validation-gated rollout from the same drift finding.
  Optional role-scoped tokens separate operator, approver, edge-agent, and
  auditor actions.
- TEMMS Daemon runs on the edge node: it imports packages, manages the local
  model cache, evaluates policies, switches models, serves inference, handles
  fallback, supports operator override, buffers offline operations, and records
  evidence. DDIL replay is not a blind queue drain: deploy intents that include
  package/device/runtime context are rechecked against Hub readiness before
  replay, so a disconnected edge will not activate a model when its current
  runtime inventory, accelerator state, performance proof, or resource envelope
  no longer supports that workload. When a queued deploy selected the wrong
  on-device runtime but TEMMS has measured a compatible target, operators can
  retarget and re-sign that DDIL intent in place before replay. The retained
  audit proof records the previous target, proved target, best target, workbench
  counts, validation, benchmark, and capability hash so the repair remains
  explainable after the queue is drained. The Hub cockpit keeps that proof
  visible in DDIL readiness and Evidence views so field operators can verify the
  queued runtime, proved runtime, best measured runtime, capability lock, and
  validation/benchmark evidence without opening raw JSON.

This Hub/Daemon boundary matches the repository architecture and keeps the edge
device useful even when disconnected.

## Runtime Primitives

The main runtime primitives are:

- Slots: named inference endpoints such as `vision`, `navigation`, or
  `inspection`.
- Conditions: local facts such as visibility, lighting, battery, thermals,
  runtime health, mission phase, or operator input.
  Daemon collector health is published under `runtime.collectors.*`, so a
  failed or degraded local sensor can be handled by the same policy path as a
  normal measurement. Active inference failures are published under
  `runtime.inference.*` and can trigger the slot fallback chain while preserving
  the failed model in decision evidence.
- Policies: YAML rules that map conditions to model choices.
- Hot-swap: change the active model without restarting the inference service.
- Fallback chains: deterministic recovery when a selected model fails.
- Operator overrides: human-in-the-loop control when policy should not decide
  alone.
- Evidence bundles: post-run records showing which model ran, when it changed,
  what conditions triggered the decision, what package/policy was active,
  whether fallback occurred, and whether the system was offline.
- Mission replay: an operator-readable reconstruction derived from the evidence
  bundle, with product-loop phases, incidents, and chronological events.

## Wedge And Non-Goals

The strongest wedge is evidence-backed adaptive inference at the edge. TEMMS is
not a generic MLOps platform, training system, labeling tool, experiment
tracker, feature store, fleet orchestrator, or broad model registry. Its job is
narrower and more valuable: control what model runs on an edge device, adapt
when local conditions change, and prove why each decision happened. The Hub
workbench makes that proof operational by showing the ranked runtime decision
trace, retained capability digest, validation/benchmark state, blocker reason,
and copyable remediation command for each on-device target.

## Initial Users

The best initial users are teams deploying models onto Jetson, Orin, Raspberry
Pi, x86 edge boxes, or ruggedized compute in robotics, drones, industrial
vision, defense autonomy, remote inspection, agriculture, mining, energy, and
infrastructure monitoring.

## Founder-Market Fit

The founder-market fit is strong because the underlying problem sits at the
intersection of distributed systems, Kubernetes platforms, model serving,
MLOps, LLM inference, CI/CD, release tooling, and autonomous aircraft
infrastructure experience.

## Commercial Path

The commercial path is open-source daemon adoption plus a paid Hub for signed
packages, package promotion and release gates, compatibility matrices, rollout
plans, evidence aggregation, mission replay, policy approval, RBAC,
air-gapped workflows, and enterprise support.
