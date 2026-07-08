import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { webcrypto } from "node:crypto";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { build, transform } from "esbuild";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const uiRoot = resolve(scriptDir, "..");
const repoRoot = resolve(uiRoot, "..");
const indexPath = join(uiRoot, "index.html");
const sourcePath = join(uiRoot, "src", "App.tsx");
const capabilityDossierPath = join(uiRoot, "src", "components", "capability-dossier.tsx");
const edgeDeployStagePath = join(uiRoot, "src", "components", "edge-deploy-stage.tsx");
const deployListsPath = join(uiRoot, "src", "components", "deploy-lists.tsx");
const edgeProofPath = join(uiRoot, "src", "components", "edge-proof.tsx");
const deploymentIntentPath = join(uiRoot, "src", "lib", "deployment-intent.ts");
const fieldOpsPath = join(uiRoot, "src", "components", "field-ops.tsx");
const fieldOpsStagePath = join(uiRoot, "src", "components", "field-ops-stage.tsx");
const packageHandoffPath = join(uiRoot, "src", "components", "package-handoff.tsx");
const packageStagePath = join(uiRoot, "src", "components", "package-stage.tsx");
const missionStagesPath = join(uiRoot, "src", "components", "mission-stages.tsx");
const modelPlanPath = join(uiRoot, "src", "components", "model-plan.tsx");
const readinessPanelsPath = join(uiRoot, "src", "components", "readiness-panels.tsx");
const runtimeDecisionTracePath = join(uiRoot, "src", "components", "runtime-decision-trace.tsx");
const runtimeExecutionContractPath = join(uiRoot, "src", "components", "runtime-execution-contract.tsx");
const runtimeContractRowsPath = join(uiRoot, "src", "components", "runtime-contract-rows.tsx");
const runtimeMissionPath = join(uiRoot, "src", "components", "runtime-mission.tsx");
const runtimeOperatorProofPath = join(uiRoot, "src", "components", "runtime-operator-proof.tsx");
const runtimeOptimizerPath = join(uiRoot, "src", "components", "runtime-optimizer.tsx");
const runtimeWorkbenchPath = join(uiRoot, "src", "components", "runtime-workbench.tsx");
const workbenchFlowPath = join(uiRoot, "src", "components", "workbench-flow.tsx");
const apiPath = join(uiRoot, "src", "api.ts");
const edgeProofWorkflowPath = join(uiRoot, "src", "lib", "edge-proof-workflow.ts");
const edgeRuntimeMissionPath = join(uiRoot, "src", "lib", "edge-runtime-mission.ts");
const fieldOpsProofPath = join(uiRoot, "src", "lib", "field-ops-proof.ts");
const hubActionsPath = join(uiRoot, "src", "lib", "hub-actions.ts");
const readinessPath = join(uiRoot, "src", "lib", "readiness.ts");
const runtimeDecisionPath = join(uiRoot, "src", "lib", "runtime-decision.ts");
const proofCommandPath = join(uiRoot, "src", "lib", "proof-command.ts");
const runtimeRemediationPath = join(uiRoot, "src", "lib", "runtime-remediation.ts");
const runtimeStageViewPath = join(uiRoot, "src", "lib", "runtime-stage-view.ts");
const missionPackagePath = join(uiRoot, "src", "lib", "mission-package.ts");
const missionWorkflowPath = join(uiRoot, "src", "lib", "mission-workflow.ts");
const missionSpecPath = join(uiRoot, "src", "lib", "mission-spec.ts");
const proofHashPath = join(uiRoot, "src", "lib", "proof-hash.ts");
const hubTemplatePath = join(repoRoot, "src", "temms", "ui", "templates", "hub.html");
const manifestPath = join(repoRoot, "src", "temms", "ui", "static", "hub", ".vite", "manifest.json");
const staticIndexPath = join(repoRoot, "src", "temms", "ui", "static", "hub", "index.html");
const docsBuildPath = join(repoRoot, "docs", "_build");
const docsContractPaths = [
  "README.md",
  "docs/QUICKSTART.md",
  "docs/hub-lite.md",
  "docs/functional-testing.md",
  "docs/product-summary.md"
];
const docsRunbookPaths = [
  "docs/hub-lite.md",
  "docs/functional-testing.md",
  "docs/product-summary.md"
];
const bundleBudgets = {
  cssGzipBytes: 12_000,
  cssRawBytes: 70_000,
  jsGzipBytes: 140_000,
  jsRawBytes: 450_000
};

function assertContains(label, content, needle) {
  if (!content.includes(needle)) {
    throw new Error(`${label} is missing required runtime workbench contract: ${needle}`);
  }
}

function assertNotContains(label, content, needle) {
  if (content.includes(needle)) {
    throw new Error(`${label} contains retired Hub product contract: ${needle}`);
  }
}

function collectTextFiles(root) {
  if (!existsSync(root)) return [];
  const entries = readdirSync(root);
  return entries.flatMap((entry) => {
    const path = join(root, entry);
    const stats = statSync(path);
    if (stats.isDirectory()) return collectTextFiles(path);
    return [".html", ".js", ".md", ".txt"].includes(extname(path)) ? [path] : [];
  });
}

function assertAssetBudget(label, content, { rawBytes, gzipBytes }) {
  const rawSize = Buffer.byteLength(content);
  const gzipSize = gzipSync(content).byteLength;
  if (rawSize > rawBytes) {
    throw new Error(`${label} raw bundle size ${rawSize} exceeds budget ${rawBytes}`);
  }
  if (gzipSize > gzipBytes) {
    throw new Error(`${label} gzip bundle size ${gzipSize} exceeds budget ${gzipBytes}`);
  }
}

const indexHtml = readFileSync(indexPath, "utf8");
const source = readFileSync(sourcePath, "utf8");
const capabilityDossierSource = readFileSync(capabilityDossierPath, "utf8");
const edgeDeployStageSource = readFileSync(edgeDeployStagePath, "utf8");
const deployListsSource = readFileSync(deployListsPath, "utf8");
const edgeProofSource = readFileSync(edgeProofPath, "utf8");
const fieldOpsSource = readFileSync(fieldOpsPath, "utf8");
const fieldOpsStageSource = readFileSync(fieldOpsStagePath, "utf8");
const packageHandoffSource = readFileSync(packageHandoffPath, "utf8");
const packageStageSource = readFileSync(packageStagePath, "utf8");
const missionStagesSource = readFileSync(missionStagesPath, "utf8");
const modelPlanSource = readFileSync(modelPlanPath, "utf8");
const readinessPanelsSource = readFileSync(readinessPanelsPath, "utf8");
const runtimeDecisionTraceSource = readFileSync(runtimeDecisionTracePath, "utf8");
const runtimeExecutionContractSource = readFileSync(runtimeExecutionContractPath, "utf8");
const runtimeContractRowsSource = readFileSync(runtimeContractRowsPath, "utf8");
const runtimeMissionSource = readFileSync(runtimeMissionPath, "utf8");
const runtimeOperatorProofSource = readFileSync(runtimeOperatorProofPath, "utf8");
const runtimeOptimizerSource = readFileSync(runtimeOptimizerPath, "utf8");
const runtimeWorkbenchSource = readFileSync(runtimeWorkbenchPath, "utf8");
const workbenchFlowSource = readFileSync(workbenchFlowPath, "utf8");
const edgeProofWorkflowSource = readFileSync(edgeProofWorkflowPath, "utf8");
const deploymentIntentSource = readFileSync(deploymentIntentPath, "utf8");
const edgeRuntimeMissionSource = readFileSync(edgeRuntimeMissionPath, "utf8");
const fieldOpsProofSource = readFileSync(fieldOpsProofPath, "utf8");
const hubActionsSource = readFileSync(hubActionsPath, "utf8");
const readinessSource = readFileSync(readinessPath, "utf8");
const runtimeDecisionSource = readFileSync(runtimeDecisionPath, "utf8");
const proofCommandSource = readFileSync(proofCommandPath, "utf8");
const runtimeRemediationSource = readFileSync(runtimeRemediationPath, "utf8");
const runtimeStageViewSource = readFileSync(runtimeStageViewPath, "utf8");
const missionPackageSource = readFileSync(missionPackagePath, "utf8");
const missionWorkflowSource = readFileSync(missionWorkflowPath, "utf8");
const workbenchSource = `${source}\n${capabilityDossierSource}\n${edgeDeployStageSource}\n${deployListsSource}\n${edgeProofSource}\n${fieldOpsSource}\n${fieldOpsStageSource}\n${packageHandoffSource}\n${packageStageSource}\n${missionStagesSource}\n${modelPlanSource}\n${readinessPanelsSource}\n${runtimeDecisionTraceSource}\n${runtimeExecutionContractSource}\n${runtimeContractRowsSource}\n${runtimeMissionSource}\n${runtimeOperatorProofSource}\n${runtimeOptimizerSource}\n${runtimeWorkbenchSource}\n${workbenchFlowSource}\n${edgeProofWorkflowSource}\n${deploymentIntentSource}\n${edgeRuntimeMissionSource}\n${fieldOpsProofSource}\n${hubActionsSource}\n${readinessSource}\n${runtimeDecisionSource}\n${proofCommandSource}\n${runtimeRemediationSource}\n${runtimeStageViewSource}\n${missionPackageSource}\n${missionWorkflowSource}`;
const apiSource = readFileSync(apiPath, "utf8");
const missionSpecSource = readFileSync(missionSpecPath, "utf8");
const proofHashSource = readFileSync(proofHashPath, "utf8");
const hubTemplate = readFileSync(hubTemplatePath, "utf8");

docsContractPaths.forEach((docPath) => {
  const content = readFileSync(join(repoRoot, docPath), "utf8");
  [
    "Mission Package Workbench",
    "Mission workflow cockpit",
    "Live context",
    "Model Plan",
    "Runtime Fit",
    "Sensor Handling",
    "Package Handoff",
    "Edge Deploy",
    "Field Ops",
    "Advanced intake",
    "Manual controls"
  ].forEach((needle) => assertContains(docPath, content, needle));
  [
    "Edge Runtime Control",
    "operator:edge-runtime-control",
    "Mission -> Models -> Runtime -> Handling -> Package -> Deploy -> Operate"
  ].forEach((needle) => assertNotContains(docPath, content, needle));
});

docsRunbookPaths.forEach((docPath) => {
  const content = readFileSync(join(repoRoot, docPath), "utf8");
  [
    "Mission workflow cockpit",
    "stage focus",
    "ready condition",
    "risk",
    "Import YAML",
    "Stage rollout",
    "Plan package",
    "package identity",
    "deployment intent",
    "passed proof gate",
    "mission-package-plan",
    "mission-package-download",
    "mission-package-stage",
    "mission-package/stage",
    "edge_handoff",
    "temms-edge-mission-package-handoff/v1",
    "stage_approve_apply"
  ].forEach((needle) => assertContains(docPath, content, needle));
});

collectTextFiles(docsBuildPath).forEach((path) => {
  const content = readFileSync(path, "utf8");
  [
    "Edge Runtime Control",
    "operator:edge-runtime-control",
    "Mission -> Models -> Runtime -> Handling -> Package -> Deploy -> Operate"
  ].forEach((needle) => assertNotContains(path, content, needle));
});

[
  "TEMMS - Mission Package Workbench"
].forEach((needle) => assertContains("ui/index.html", indexHtml, needle));
[
  "Edge Runtime Control"
].forEach((needle) => assertNotContains("ui/index.html", indexHtml, needle));

[
  "The React Hub UI has not been built yet.",
  "npm install",
  "npm run build"
].forEach((needle) => assertContains("src/temms/ui/templates/hub.html", hubTemplate, needle));
[
  "cd ui"
].forEach((needle) => assertNotContains("src/temms/ui/templates/hub.html", hubTemplate, needle));

[
  'data-testid="runtime-workbench"',
  'data-testid="runtime-workbench-model"',
  'data-testid="runtime-workbench-edge-node"',
  'data-testid="runtime-workbench-target-runtime"',
  'data-testid="mission-workflow-cockpit"',
  'data-testid={`operator-flow-${stage.id}`}',
  'data-testid="hub-stage-mission"',
  'data-testid="hub-stage-runtime"',
  'data-testid="hub-stage-handling"',
  'data-testid="hub-stage-package"',
  'data-testid={`hub-stage-${activeHubStage}`}',
  "navigateHubStage",
  "data-stage-id",
  'aria-label={`${index + 1}. ${stage.label}. ${stage.value}. ${stage.detail}`}',
  "scrollIntoView",
  "showProductStage",
  "MissionWorkflowCockpit",
  "buildHubStages",
  "mission-workflow-cockpit",
  "operator-path-rail",
  "operator-path-step-active",
  "stage-focus-panel",
  "mission-signal-panel",
  "mission-context-drawer",
  "Mission package workflow cockpit",
  "Mission package operator path",
  "Current stage",
  "Package path",
  "Live context",
  "Next: ${next.label}",
  "Mission Package Workbench",
  "Model Plan",
  "Runtime Fit",
  "Sensor Handling",
  "Package Handoff",
  "Edge Deploy",
  "Field Ops",
  "StageRunbookFact",
  "hubStageRunbookFor",
  "Ready when",
  "Risk",
  "Staging before package planning leaves rollout intent detached from the hashed mission handoff.",
  "Mission package identity exists; download or stage next.",
  "Produce the package identity and deployment intent for the edge.",
  "deploy-primary-lane",
  "stage-inline-drawer",
  "stage-advanced-drawer",
  "Manual controls",
  "Advanced intake",
  "operator:mission-package-workbench",
  "buildMissionPackageManifest",
  "buildMissionPackagePlanRequest",
  "buildMissionPackageStageStatus",
  "missionPackageStageStatus",
  "hasPlannedDeploymentIntent",
  "hasMissionPackageDeploymentIntent",
  "draftRolloutId",
  "deployRolloutId",
  "draft handoff",
  "package planned",
  "downloaded",
  "Plan package first",
  "Stage rollout uses the mission package deployment intent.",
  "Proof gate blocks staging",
  "proof gate failed",
  "proof gate pending",
  "proof gate must pass before staging",
  "deploy intent missing",
  "Stage rollout unlocks only after the mission package proof gate passes.",
  "missionSlotValue",
  "Mission slot",
  "value={missionSlotValue}",
  "stageable",
  "plan package to hash mission handoff",
  "Mission spec",
  "Sensor and model handling",
  "Handling plan",
  "Mission YAML",
  "Import YAML",
  "Mission YAML imported",
  "Unmatched hints:",
  "from the spec",
  'accept=".yaml,.yml,text/yaml,application/x-yaml,text/plain"',
  "temms-edge-mission-package/v1",
  "Plan package",
  "Stage rollout",
  "Deploy intent",
  "Mission package binding chain",
  'data-testid="mission-package-binding"',
  "Mission package handoff",
  'data-testid="mission-package-download-handoff"',
  'data-testid="package-advanced-verification"',
  "Advanced verification",
  "mission package deployment handoff",
  "Package mission, models, runtime, policy, and proof gates",
  'aria-label="Target runtime"',
  'data-testid="runtime-repair-proof"',
  'data-testid="runtime-decision-trace"',
  'data-testid="edge-proof-trace-consistency"',
  'data-testid="edge-proof-component-digests"',
  "DDIL runtime repair proof",
  "Runtime decision trace",
  "Signed runtime trace",
  "Component digests",
  "Download handoff headers",
  'data-testid="edge-proof-download-handoff"',
  "headers match the retained component digests",
  "package-bound",
  "Browser recomputed workbench, trace, and manifest hashes against the proof payload.",
  "trace agrees with runtime_workbench",
  "temms-edge-runtime-proof-component-digests/v1",
  "temms-runtime-decision-trace/v1",
  "On-device runtime capability vector",
  "Runtime image",
  "Provider match",
  "Artifact lane",
  "Capability lock",
  "Selected model from Model Plan",
  "Open Model Plan to choose a signed model",
  "Compare the model selected in Model Plan",
  'data-testid="model-plan-inventory"',
  'data-testid="model-plan-decision"',
  'data-testid="model-plan-advanced-intake"',
  "Continue to Runtime Fit",
  "Edge execution command",
  "Run on the edge node to refresh heartbeat",
  "Ranked on-device capability proof",
  "Edge runtime mission",
  "Selected on-device runtime proof",
  'testId="runtime-workbench-go-handling"',
  "Continue to Sensor Handling",
  'data-testid="handling-go-package"',
  "Continue to Package Handoff",
  'data-testid="package-go-deploy"',
  "Continue to Edge Deploy",
  "runtime_retarget_workbench_previous_selected_runtime_target_id",
  "Select runtime target",
  "Target the model to the edge runtime"
].forEach((needle) => assertContains("Hub workbench sources", workbenchSource, needle));

[
  "Runtime workbench",
  "Target the model to the edge runtime",
  "Selected model from Model Plan",
  "Runtime path controls",
  "On-device runtime capability vector",
  "Selected edge runtime summary",
  "Ranked target runtimes",
  "Generate runtime proof",
  "Continue to Sensor Handling"
].forEach((needle) => assertContains("Runtime workbench sources", runtimeWorkbenchSource, needle));

[
  "On-device runtime proof",
  "Active model runtime edge proof",
  "Runtime target alternatives",
  "EdgeOperatorCommandPanel",
  "OperatorCommandMetric"
].forEach((needle) => assertContains("Runtime operator proof sources", runtimeOperatorProofSource, needle));

[
  "Edge execution contract",
  "Runtime decision",
  "Selected model runtime edge path",
  "On-device runtime capabilities",
  "Target runtime coverage",
  "Measured runtime candidates",
  "Runtime blockers and evidence gaps",
  "Use best runtime"
].forEach((needle) => assertContains("Runtime execution contract sources", runtimeExecutionContractSource, needle));

[
  "RuntimeCandidateRow",
  "TargetRuntimeAssessmentRow",
  "ExecutionPathNode",
  "Copy command"
].forEach((needle) => assertContains("Runtime contract row sources", runtimeContractRowsSource, needle));

[
  "runtimeDecisionCandidates",
  "runtimeTargetAssessments",
  "runtimeWorkbenchRows",
  "targetRuntimeCoverageSummary"
].forEach((needle) => assertContains("Runtime decision sources", runtimeDecisionSource, needle));

[
  "buildReadinessContext",
  "readinessContextKey",
  "hasReadinessContextSelection",
  "scopedReadinessFor",
  "readinessMatchesContext",
  "selectionMatchesContext",
  "slot || \"vision\""
].forEach((needle) => assertContains("Readiness sources", readinessSource, needle));

[
  "buildRuntimeStageView",
  "RuntimeStageView",
  "runtimeWorkbenchRows",
  "runtimeCapabilityLockForProof",
  "slot || \"vision\""
].forEach((needle) => assertContains("Runtime stage view sources", runtimeStageViewSource, needle));

[
  "runtimeWorkbenchRowRemediationCommand",
  "runtimeTargetAssessmentRemediationCommand",
  "runtimeTargetContractRemediationCommand",
  "record_benchmark",
  "validate_runtime",
  "refresh_edge_inventory",
  "package_runtime_artifact",
  "compatibility-matrix",
  "edge-runtime-mission"
].forEach((needle) => assertContains("Runtime remediation sources", runtimeRemediationSource, needle));

[
  "defaultValue=\"vision\""
].forEach((needle) => assertNotContains("Edge deploy stage sources", edgeDeployStageSource, needle));

[
  "formatProofCommand",
  "shellArg"
].forEach((needle) => assertContains("Proof command sources", proofCommandSource, needle));

[
  "Selected on-device capability dossier",
  "On-device capability dossier",
  "Runtime fit components",
  "Live edge inventory",
  "Target requirements",
  "Admission gates"
].forEach((needle) => assertContains("Capability dossier sources", capabilityDossierSource, needle));

[
  "MissionDraft",
  "defaultMissionDraft",
  "missionDraftFromYaml",
  "missionSelectionFromYaml",
  "extractMissionYamlScalars",
  "collectMissionYamlBlock",
  "normalizeMissionYamlKey"
].forEach((needle) => assertContains("ui/src/lib/mission-spec.ts", missionSpecSource, needle));
[
  "Edge Runtime Control",
  "operator:edge-runtime-control",
  "Pick the model, edge node, and runtime target",
  "product-grid-stage-models",
  "product-grid-stage-operate"
].forEach((needle) => assertNotContains("ui/src/App.tsx", source, needle));

[
  "MissionPackagePlanRequest",
  "planMissionPackage",
  "/mission-package/plan",
  "downloadMissionPackage",
  "/mission-package/download",
  "stageMissionPackage",
  "/mission-package/stage",
  "X-TEMMS-Mission-Package-SHA256",
  "X-TEMMS-Mission-Package-Deployment-Intent-SHA256",
  "EdgeProofDownloadHandoff",
  "edgeProofDownloadHandoff",
  "X-TEMMS-Edge-Proof-Runtime-Workbench-SHA256",
  "X-TEMMS-Edge-Proof-Runtime-Decision-Trace-SHA256",
  "X-TEMMS-Edge-Proof-Execution-Manifest-SHA256",
  "X-TEMMS-Edge-Proof-SHA256"
].forEach((needle) => assertContains("ui/src/api.ts", apiSource, needle));

[
  "EDGE_PROOF_COMPONENT_DIGEST_TARGETS",
  "canonicalJsonStringify",
  "sha256Hex",
  "runtime_workbench_sha256",
  "runtime_decision_trace_sha256",
  "edge_execution_manifest_sha256"
].forEach((needle) => assertContains("ui/src/lib/proof-hash.ts", proofHashSource, needle));

if (!globalThis.crypto) {
  Object.defineProperty(globalThis, "crypto", { value: webcrypto });
}
const transformedProofHash = await transform(proofHashSource, {
  format: "esm",
  loader: "ts"
});
const proofHashModule = await import(
  `data:text/javascript;base64,${Buffer.from(transformedProofHash.code).toString("base64")}`
);
const canonicalVector = { z: [3, "x", true, null], a: { b: 2, a: "edge" }, n: 12.5 };
const canonicalString = proofHashModule.canonicalJsonStringify(canonicalVector);
const expectedCanonicalString = '{"a":{"a":"edge","b":2},"n":12.5,"z":[3,"x",true,null]}';
if (canonicalString !== expectedCanonicalString) {
  throw new Error(`proof-hash canonical JSON mismatch: ${canonicalString}`);
}
const expectedDigest = "4cc7d0dd44ca8f915530d9d1b0312f1a01ce16361075d3b405f666f735dd2bdb";
const digest = await proofHashModule.sha256Hex(canonicalString);
if (digest !== expectedDigest) {
  throw new Error(`proof-hash SHA256 mismatch: ${digest}`);
}

const bundledReadiness = await build({
  bundle: true,
  entryPoints: [readinessPath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const readinessModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledReadiness.outputFiles[0].text).toString("base64")}`
);
const readinessContextFixture = readinessModule.buildReadinessContext({
  device: { device_id: "edge-thermal-01" },
  model: {
    id: "model-thermal-detector-001",
    packageId: "pkg-thermal-models-20260708"
  },
  runtime: { runtime_target_id: "temms-jetson-ort-trt" },
  slot: "thermal"
});
const readinessContextKeyFixture = readinessModule.readinessContextKey(readinessContextFixture);
if (
  readinessContextKeyFixture !==
  "pkg-thermal-models-20260708|model-thermal-detector-001|edge-thermal-01|temms-jetson-ort-trt|thermal"
) {
  throw new Error(`readiness context key should bind package/model/device/runtime/slot: ${readinessContextKeyFixture}`);
}
if (!readinessModule.hasReadinessContextSelection(readinessContextFixture)) {
  throw new Error("readiness context should be fetchable when package, device, or runtime is selected");
}
if (readinessModule.hasReadinessContextSelection({ slot: "thermal" })) {
  throw new Error("readiness context should not fetch when only a sensor slot is selected");
}
const matchingContextReadiness = {
  gates: [{ gate_id: "runtime-fit", status: "go" }],
  selection: readinessContextFixture,
  status: "go"
};
const matchingSnapshotReadiness = {
  gates: [{ gate_id: "snapshot", status: "attention" }],
  selection: readinessContextFixture,
  status: "attention"
};
if (
  readinessModule.scopedReadinessFor({
    context: readinessContextFixture,
    contextReadiness: matchingContextReadiness,
    snapshotReadiness: matchingSnapshotReadiness
  }) !== matchingContextReadiness
) {
  throw new Error("scoped readiness should prefer the freshly fetched context readiness");
}
const mismatchedSlotReadiness = {
  gates: [{ gate_id: "slot", status: "attention" }],
  selection: { ...readinessContextFixture, slot: "vision" },
  status: "attention"
};
if (
  readinessModule.scopedReadinessFor({
    context: readinessContextFixture,
    contextReadiness: mismatchedSlotReadiness,
    snapshotReadiness: undefined
  }) !== undefined
) {
  throw new Error("scoped readiness should reject readiness captured for a different sensor slot");
}

const bundledRuntimeStageView = await build({
  bundle: true,
  entryPoints: [runtimeStageViewPath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const runtimeStageViewModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledRuntimeStageView.outputFiles[0].text).toString("base64")}`
);
const inactiveRuntimeStageView = runtimeStageViewModule.buildRuntimeStageView({
  activeHubStage: "mission",
  edgeExecutionContract: {},
  readiness: undefined,
  runtimeDecision: {},
  runtimeTargets: [],
  runtimeValidations: [],
  selectedDevice: { device_id: "edge-thermal-01" },
  selectedModel: {
    id: "model-thermal-detector-001",
    packageId: "pkg-thermal-models-20260708"
  },
  selectedRuntime: { runtime_target_id: "temms-jetson-ort-trt" },
  slot: "thermal"
});
if (
  inactiveRuntimeStageView.remediationContext.slot !== "thermal" ||
  inactiveRuntimeStageView.remediationContext.deviceId !== "edge-thermal-01"
) {
  throw new Error("runtime stage remediation context should retain the selected mission slot and edge");
}
const defaultSlotRuntimeStageView = runtimeStageViewModule.buildRuntimeStageView({
  activeHubStage: "mission",
  edgeExecutionContract: {},
  readiness: undefined,
  runtimeDecision: {},
  runtimeTargets: [],
  runtimeValidations: [],
  selectedDevice: undefined,
  selectedModel: undefined,
  selectedRuntime: undefined,
  slot: ""
});
if (defaultSlotRuntimeStageView.remediationContext.slot !== "vision") {
  throw new Error("runtime stage remediation context should default blank mission slots to vision");
}

const bundledEdgeProofWorkflow = await build({
  bundle: true,
  entryPoints: [edgeProofWorkflowPath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const edgeProofWorkflowModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledEdgeProofWorkflow.outputFiles[0].text).toString("base64")}`
);
const thermalEdgeProofWorkflow = edgeProofWorkflowModule.buildEdgeProofWorkflow({
  device: { device_id: "edge-thermal-01", status: "online" },
  model: {
    format: "onnx",
    id: "model-thermal-detector-001",
    packageId: "pkg-thermal-models-20260708"
  },
  readiness: {
    runtime_fit: { score: 98 },
    runtime_capability_lock: { status: "locked" }
  },
  readinessVerdict: {
    label: "go",
    nextAction: "Stage thermal mission package",
    tone: "good"
  },
  runtime: { runtime_target_id: "temms-jetson-ort-trt" },
  runtimeFitDisplay: {
    detail: "thermal runtime fit",
    label: "98/100",
    tileDetail: "thermal fit",
    tone: "good"
  },
  slot: "thermal"
});
if (!thermalEdgeProofWorkflow.generateCommand.includes("--slot thermal")) {
  throw new Error("edge proof generate command should use the selected mission slot");
}
if (!thermalEdgeProofWorkflow.verifyCommand.includes("--slot thermal")) {
  throw new Error("edge proof verify command should use the selected mission slot");
}
if (!thermalEdgeProofWorkflow.verifyJsonCommand.includes("--slot thermal")) {
  throw new Error("edge proof JSON verify command should use the selected mission slot");
}
if (!thermalEdgeProofWorkflow.proofPath.includes("thermal")) {
  throw new Error("edge proof path should include the selected mission slot to avoid artifact collisions");
}
const defaultSlotEdgeProofWorkflow = edgeProofWorkflowModule.buildEdgeProofWorkflow({
  device: undefined,
  model: undefined,
  readiness: undefined,
  readinessVerdict: {
    label: "attention",
    nextAction: "Select mission path",
    tone: "warn"
  },
  runtime: undefined,
  runtimeFitDisplay: {
    detail: "runtime pending",
    label: "pending",
    tileDetail: "pending",
    tone: "warn"
  },
  slot: ""
});
if (!defaultSlotEdgeProofWorkflow.generateCommand.includes("--slot vision")) {
  throw new Error("edge proof command should default blank mission slots to vision");
}

const bundledMissionWorkflow = await build({
  bundle: true,
  entryPoints: [missionWorkflowPath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const missionWorkflowModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledMissionWorkflow.outputFiles[0].text).toString("base64")}`
);
const missionDraftFixture = {
  confidenceThreshold: "0.7",
  ddilMode: "queue_signed_intents",
  fallbackModelId: "model-fallback",
  goal: "Detect vehicles locally while disconnected.",
  latencyBudgetMs: "95",
  sensor: "camera.rgb",
  slot: "vision",
  switchPolicy: "confidence_and_condition",
  throughputMinIps: "20",
  yaml: ""
};
const bundledDeploymentIntent = await build({
  bundle: true,
  entryPoints: [deploymentIntentPath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const deploymentIntentModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledDeploymentIntent.outputFiles[0].text).toString("base64")}`
);
const bundledMissionPackage = await build({
  bundle: true,
  entryPoints: [missionPackagePath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const missionPackageModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledMissionPackage.outputFiles[0].text).toString("base64")}`
);
const modelFixture = {
  format: "onnx",
  id: "model-yolov8-lowlight-001",
  maxLatencyP95Ms: 95,
  minThroughputIps: 20,
  name: "YOLOv8 lowlight",
  packageId: "pkg-vision-models-20240115"
};
const thermalDeploymentIntent = deploymentIntentModule.buildDeploymentIntentRequest({
  device: { device_id: "edge-thermal-1" },
  draft: { ...missionDraftFixture, sensor: "camera.thermal", slot: "thermal" },
  model: modelFixture,
  requestedAt: "2026-07-08T00:00:00.000Z",
  runtime: { runtime_target_id: "temms-rpi5-tflite" }
});
const expectedThermalDeploymentIntent = {
  actor: "operator:mission-package-workbench",
  device_id: "edge-thermal-1",
  model_id: "model-yolov8-lowlight-001",
  package_id: "pkg-vision-models-20240115",
  requested_at: "2026-07-08T00:00:00.000Z",
  runtime_target_id: "temms-rpi5-tflite",
  slot: "thermal",
  source: "hub-ddil-drill"
};
Object.entries(expectedThermalDeploymentIntent).forEach(([key, value]) => {
  if (thermalDeploymentIntent[key] !== value) {
    throw new Error(`deployment intent ${key} mismatch: ${thermalDeploymentIntent[key]}`);
  }
});
const defaultSlotDeploymentIntent = deploymentIntentModule.buildDeploymentIntentRequest({
  device: undefined,
  draft: { ...missionDraftFixture, slot: "" },
  model: undefined,
  requestedAt: "2026-07-08T00:00:00.000Z",
  runtime: undefined
});
if (defaultSlotDeploymentIntent.slot !== "vision") {
  throw new Error("deployment intent should default blank mission slots to vision");
}
const planRequestFixture = missionPackageModule.buildMissionPackagePlanRequest({
  draft: {
    ...missionDraftFixture,
    yaml: "schema_version: temms-edge-mission/v1\nmission:\n  goal: Detect vehicles locally while disconnected.\n"
  },
  readinessContext: {
    device_id: "edge-rpi5",
    model_id: "model-yolov8-lowlight-001",
    package_id: "pkg-vision-models-20240115",
    runtime_target_id: "temms-rpi5-tflite",
    slot: "vision"
  }
});
const expectedPlanRequestFields = {
  confidence_threshold: 0.7,
  ddil_mode: "queue_signed_intents",
  device_id: "edge-rpi5",
  fallback_model_id: "model-fallback",
  goal: "Detect vehicles locally while disconnected.",
  latency_budget_ms: 95,
  min_runtime_fit: 95,
  min_throughput_ips: 20,
  model_id: "model-yolov8-lowlight-001",
  package_id: "pkg-vision-models-20240115",
  require_best_runtime: true,
  require_capability_lock: true,
  require_go: false,
  require_proof_signature: true,
  runtime_target_id: "temms-rpi5-tflite",
  sensor: "camera.rgb",
  slot: "vision",
  switch_policy: "confidence_and_condition"
};
Object.entries(expectedPlanRequestFields).forEach(([key, value]) => {
  if (planRequestFixture[key] !== value) {
    throw new Error(`mission package plan request ${key} mismatch: ${planRequestFixture[key]}`);
  }
});
if (!String(planRequestFixture.mission_yaml || "").includes("temms-edge-mission/v1")) {
  throw new Error("mission package plan request should preserve source mission YAML");
}
const manifestFixture = missionPackageModule.buildMissionPackageManifest({
  device: { device_id: "edge-rpi5" },
  draft: missionDraftFixture,
  model: modelFixture,
  runtime: { runtime_target_id: "temms-rpi5-tflite" }
});
if (manifestFixture.slo?.latency_budget_ms !== 95 || manifestFixture.slo?.min_throughput_ips !== 20) {
  throw new Error("mission package manifest should preserve numeric SLO values");
}
if (manifestFixture.model_handling?.confidence_threshold !== 0.7) {
  throw new Error("mission package manifest should preserve numeric confidence threshold");
}
if (manifestFixture.selection?.runtime_target_id !== "temms-rpi5-tflite") {
  throw new Error("mission package manifest should bind selected runtime target");
}
const invalidNumberPlanRequest = missionPackageModule.buildMissionPackagePlanRequest({
  draft: {
    ...missionDraftFixture,
    confidenceThreshold: "auto",
    latencyBudgetMs: "fast",
    throughputMinIps: ""
  },
  readinessContext: { slot: "vision" }
});
["confidence_threshold", "latency_budget_ms", "min_throughput_ips"].forEach((key) => {
  if (invalidNumberPlanRequest[key] !== undefined) {
    throw new Error(`mission package plan request should omit non-finite ${key}`);
  }
});
const invalidNumberManifest = missionPackageModule.buildMissionPackageManifest({
  device: undefined,
  draft: {
    ...missionDraftFixture,
    confidenceThreshold: "auto",
    latencyBudgetMs: "",
    throughputMinIps: "fast"
  },
  model: undefined,
  runtime: undefined
});
if (
  invalidNumberManifest.slo?.latency_budget_ms !== undefined ||
  invalidNumberManifest.slo?.min_throughput_ips !== undefined ||
  invalidNumberManifest.model_handling?.confidence_threshold !== undefined
) {
  throw new Error("mission package manifest should omit blank or non-finite numeric controls");
}
const stageablePackageStatus = missionPackageModule.buildMissionPackageStageStatus({
  handoff: undefined,
  manifest: {
    ...manifestFixture,
    component_digests: { edge_handoff_sha256: "d".repeat(64) },
    deployment_intent: {
      command: { method: "POST", path: "/v1/hub/rollouts/assign" },
      mission_contract_sha256: "b".repeat(64),
      requires: {
        mission_contract_digest: true,
        runtime_capability_lock_digest: true,
        runtime_plan_digest: true
      },
      runtime_capability_lock_sha256: "c".repeat(64),
      runtime_plan_sha256: "a".repeat(64),
      rollout_id: "rollout-model-yolov8-lowlight-001-temms-rpi5-tflite-edge-rpi5"
    },
    edge_handoff: { schema_version: "temms-edge-mission-package-handoff/v1" },
    proof_gate: { status: "passed" }
  },
  missionReady: true,
  plan: { schema_version: "temms-edge-mission-package/v1" }
});
if (stageablePackageStatus.stageable !== true || stageablePackageStatus.value !== "package planned") {
  throw new Error("mission package stage status should be stageable only after proof gate and deploy intent");
}
const missingIntentPackageStatus = missionPackageModule.buildMissionPackageStageStatus({
  handoff: undefined,
  manifest: {
    ...manifestFixture,
    proof_gate: { status: "passed" }
  },
  missionReady: true,
  plan: { schema_version: "temms-edge-mission-package/v1" }
});
if (missingIntentPackageStatus.stageable !== false || missingIntentPackageStatus.value !== "deploy intent missing") {
  throw new Error("mission package stage status should block passed plans that lack deployment intent");
}
const missingEdgeHandoffPackageStatus = missionPackageModule.buildMissionPackageStageStatus({
  handoff: undefined,
  manifest: {
    ...manifestFixture,
    deployment_intent: {
      command: { method: "POST", path: "/v1/hub/rollouts/assign" },
      mission_contract_sha256: "b".repeat(64),
      requires: {
        mission_contract_digest: true,
        runtime_capability_lock_digest: true,
        runtime_plan_digest: true
      },
      runtime_capability_lock_sha256: "c".repeat(64),
      runtime_plan_sha256: "a".repeat(64),
      rollout_id: "rollout-model-yolov8-lowlight-001-temms-rpi5-tflite-edge-rpi5"
    },
    proof_gate: { status: "passed" }
  },
  missionReady: true,
  plan: { schema_version: "temms-edge-mission-package/v1" }
});
if (
  missingEdgeHandoffPackageStatus.stageable !== false ||
  missingEdgeHandoffPackageStatus.value !== "edge handoff missing"
) {
  throw new Error("mission package stage status should block deploy intents without edge handoff binding");
}
const missingMissionContractPackageStatus = missionPackageModule.buildMissionPackageStageStatus({
  handoff: undefined,
  manifest: {
    ...manifestFixture,
    component_digests: { edge_handoff_sha256: "d".repeat(64) },
    deployment_intent: {
      command: { method: "POST", path: "/v1/hub/rollouts/assign" },
      requires: { runtime_plan_digest: true },
      runtime_plan_sha256: "a".repeat(64),
      rollout_id: "rollout-model-yolov8-lowlight-001-temms-rpi5-tflite-edge-rpi5"
    },
    edge_handoff: { schema_version: "temms-edge-mission-package-handoff/v1" },
    proof_gate: { status: "passed" }
  },
  missionReady: true,
  plan: { schema_version: "temms-edge-mission-package/v1" }
});
if (
  missingMissionContractPackageStatus.stageable !== false ||
  missingMissionContractPackageStatus.value !== "mission contract missing"
) {
  throw new Error("mission package stage status should block deploy intents without mission contract binding");
}
const missingRuntimeDigestPackageStatus = missionPackageModule.buildMissionPackageStageStatus({
  handoff: undefined,
  manifest: {
    ...manifestFixture,
    component_digests: { edge_handoff_sha256: "d".repeat(64) },
    deployment_intent: {
      command: { method: "POST", path: "/v1/hub/rollouts/assign" },
      mission_contract_sha256: "b".repeat(64),
      requires: {
        mission_contract_digest: true,
        runtime_capability_lock_digest: true
      },
      runtime_capability_lock_sha256: "c".repeat(64),
      rollout_id: "rollout-model-yolov8-lowlight-001-temms-rpi5-tflite-edge-rpi5"
    },
    edge_handoff: { schema_version: "temms-edge-mission-package-handoff/v1" },
    proof_gate: { status: "passed" }
  },
  missionReady: true,
  plan: { schema_version: "temms-edge-mission-package/v1" }
});
if (
  missingRuntimeDigestPackageStatus.stageable !== false ||
  missingRuntimeDigestPackageStatus.value !== "runtime digest missing"
) {
  throw new Error("mission package stage status should block deploy intents without runtime digest binding");
}
const missingCapabilityLockDigestPackageStatus = missionPackageModule.buildMissionPackageStageStatus({
  handoff: undefined,
  manifest: {
    ...manifestFixture,
    component_digests: { edge_handoff_sha256: "d".repeat(64) },
    deployment_intent: {
      command: { method: "POST", path: "/v1/hub/rollouts/assign" },
      mission_contract_sha256: "b".repeat(64),
      requires: { mission_contract_digest: true, runtime_plan_digest: true },
      runtime_plan_sha256: "a".repeat(64),
      rollout_id: "rollout-model-yolov8-lowlight-001-temms-rpi5-tflite-edge-rpi5"
    },
    edge_handoff: { schema_version: "temms-edge-mission-package-handoff/v1" },
    proof_gate: { status: "passed" }
  },
  missionReady: true,
  plan: { schema_version: "temms-edge-mission-package/v1" }
});
if (
  missingCapabilityLockDigestPackageStatus.stageable !== false ||
  missingCapabilityLockDigestPackageStatus.value !== "capability lock missing"
) {
  throw new Error("mission package stage status should block deploy intents without capability lock binding");
}
const readyStageOptions = {
  ddilDetail: "ready for replay",
  deadLetteredOperations: 0,
  evidenceBundleCount: 0,
  evidenceDetail: "2 proof events",
  evidenceValue: 2,
  latestRollout: { state: "activated" },
  missionDraft: missionDraftFixture,
  missionPackageStageStatus: {
    detail: "passed proof gate",
    downloaded: true,
    gateStatus: "passed",
    planned: true,
    stageable: true,
    tone: "good",
    value: "downloaded"
  },
  missionProofComplete: true,
  missionReady: true,
  missionRolloutCount: 1,
  offlineMode: false,
  proofEvents: 2,
  replayBlockedOperations: 0,
  rolloutDetail: "activated model-yolov8-lowlight-001",
  runtimeFitDisplay: {
    detail: "score 98 optimal",
    failures: [],
    label: "98 optimal",
    tileDetail: "on-device runtime fit",
    tone: "good"
  },
  selectedModel: modelFixture,
  selectedRuntime: { runtime_target_id: "temms-rpi5-tflite" }
};
const stageFixture = missionWorkflowModule.buildHubStages(readyStageOptions);
const expectedStageOrder = "mission>model>runtime>handling>package>deploy>field";
const stageOrder = stageFixture.map((stage) => stage.id).join(">");
if (stageOrder !== expectedStageOrder) {
  throw new Error(`mission workflow stage order mismatch: ${stageOrder}`);
}
if (stageFixture.find((stage) => stage.id === "package")?.decision !== "Hash the mission, model, runtime plan, and handling policy into one deployable handoff.") {
  throw new Error("mission workflow package stage no longer binds mission/model/runtime/handling into the package handoff");
}
if (stageFixture.find((stage) => stage.id === "deploy")?.tone !== "good") {
  throw new Error("mission workflow deploy stage should be good after an activated rollout");
}
const blockedStageFixture = missionWorkflowModule.buildHubStages({
  ...readyStageOptions,
  ddilDetail: "blocked replay",
  deadLetteredOperations: 1,
  evidenceBundleCount: 0,
  evidenceDetail: "no evidence",
  evidenceValue: 0,
  latestRollout: { state: "failed" },
  missionDraft: missionDraftFixture,
  missionPackageStageStatus: {
    detail: "proof gate failed",
    downloaded: false,
    gateStatus: "failed",
    planned: true,
    stageable: false,
    tone: "bad",
    value: "proof gate failed"
  },
  missionProofComplete: false,
  missionReady: true,
  missionRolloutCount: 1,
  offlineMode: true,
  proofEvents: 0,
  replayBlockedOperations: 2,
  rolloutDetail: "failed",
  runtimeFitDisplay: {
    detail: "runtime proof blocked",
    failures: ["runtime proof blocked"],
    label: "blocked",
    tileDetail: "runtime proof blocked",
    tone: "bad"
  },
  selectedModel: modelFixture,
  selectedRuntime: { runtime_target_id: "temms-rpi5-tflite" }
});
if (blockedStageFixture.find((stage) => stage.id === "deploy")?.tone !== "bad") {
  throw new Error("mission workflow deploy stage should be bad when DDIL replay or rollout state blocks deploy");
}
if (blockedStageFixture.find((stage) => stage.id === "field")?.detail !== "DDIL offline; blocked replay") {
  throw new Error("mission workflow field stage should surface offline DDIL detail");
}

if (!existsSync(manifestPath)) {
  throw new Error("Hub static manifest is missing; run npm run build before npm run smoke:workbench.");
}
if (!existsSync(staticIndexPath)) {
  throw new Error("Hub static index is missing; run npm run build before npm run smoke:workbench.");
}

const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
const entry = manifest["index.html"];
const jsFile = entry?.file;
const cssFile = entry?.css?.[0];
if (!jsFile || !cssFile) {
  throw new Error("Hub static manifest does not point to both JS and CSS assets.");
}

const jsPath = join(repoRoot, "src", "temms", "ui", "static", "hub", jsFile);
const cssPath = join(repoRoot, "src", "temms", "ui", "static", "hub", cssFile);
const staticIndexHtml = readFileSync(staticIndexPath, "utf8");
const bundle = readFileSync(jsPath, "utf8");
const css = readFileSync(cssPath, "utf8");
assertAssetBudget(jsFile, bundle, {
  rawBytes: bundleBudgets.jsRawBytes,
  gzipBytes: bundleBudgets.jsGzipBytes
});
assertAssetBudget(cssFile, css, {
  rawBytes: bundleBudgets.cssRawBytes,
  gzipBytes: bundleBudgets.cssGzipBytes
});
[
  "TEMMS - Mission Package Workbench",
  jsFile,
  cssFile
].forEach((needle) => assertContains("src/temms/ui/static/hub/index.html", staticIndexHtml, needle));
[
  "Edge Runtime Control"
].forEach((needle) => assertNotContains("src/temms/ui/static/hub/index.html", staticIndexHtml, needle));
[
  "Target the model to the edge runtime",
  "Runtime decision trace",
  "Ranked on-device capability proof",
  "DDIL runtime repair proof",
  "runtime-decision-trace",
  "runtime-repair-proof",
  "edge-proof-trace-consistency",
  "edge-proof-component-digests",
  "Signed runtime trace",
  "Component digests",
  "Download handoff headers",
  "edge-proof-download-handoff",
  "headers match the retained component digests",
  "Browser recomputed workbench, trace, and manifest hashes against the proof payload.",
  "temms-edge-runtime-proof-component-digests/v1",
  "temms-runtime-decision-trace/v1",
  "On-device runtime capability vector",
  "Runtime image",
  "Provider match",
  "runtime-workbench-target-runtime",
  "Mission",
  "Model Plan",
  "Runtime Fit",
  "Sensor Handling",
  "Package Handoff",
  "Edge Deploy",
  "Field Ops",
  "Mission package workflow cockpit",
  "Mission package operator path",
  "Package path",
  "Live context",
  "mission-workflow-cockpit",
  "operator-path-rail",
  "operator-path-step-active",
  "stage-focus-panel",
  "mission-signal-panel",
  "mission-context-drawer",
  "hub-stage-mission",
  "hub-stage-handling",
  "hub-stage-",
  "Ready when",
  "Risk",
  "Mission package identity exists; download or stage next.",
  "Staging before package planning leaves rollout intent detached from the hashed mission handoff.",
  "data-stage-id",
  "Next:",
  "Mission Package Workbench",
  "operator:mission-package-workbench",
  "draft handoff",
  "package planned",
  "downloaded",
  "Plan package first",
  "Stage rollout uses the mission package deployment intent.",
  "plan package to hash mission handoff",
  "temms-edge-mission-package/v1",
  "Plan package",
  "Stage rollout",
  "Stage package rollout",
  "Download package",
  "Deploy intent",
  "Manual controls",
  "Advanced intake",
  "mission-package-binding",
  "Mission package handoff",
  "mission-package-download-handoff",
  "package-advanced-verification",
  "Advanced verification",
  "Generate runtime proof for selected edge path",
  "Select runtime target"
].forEach((needle) => assertContains(jsFile, bundle, needle));
[
  "Edge Runtime Control",
  "operator:edge-runtime-control",
  "product-grid-stage-models",
  "product-grid-stage-operate",
  "hub-stage-handoff",
  "hub-stage-runbook",
  "mission-path-summary",
  "system-context-drawer"
].forEach((needle) => assertNotContains(jsFile, bundle, needle));
[
  "mission-workflow-shell",
  "mission-workflow-cockpit",
  "operator-path-rail",
  "operator-path-step-active",
  "stage-focus-panel",
  "stage-focus-actions",
  "stage-secondary-actions",
  "mission-signal-panel",
  "mission-signal-grid",
  "mission-context-drawer",
  "stage-fact",
  "package-verification-drawer",
  "package-verification-stack",
  ".package-verification-drawer:not([open]) .package-verification-stack",
  "stage-stack",
  "product-grid-stage-model",
  "product-grid-stage-deploy",
  "product-grid-stage-field",
  "product-grid-stage-model .assets-section",
  "product-grid-stage-field .repair-section",
  "runtime-workbench",
  "runtime-workbench-row-selected",
  "runtime-decision-trace",
  "runtime-decision-trace-metric",
  "runtime-repair-proof",
  "runtime-repair-metric",
  "edge-proof-trace",
  "package-binding-strip",
  "deploy-primary-lane",
  "stage-inline-drawer",
  "stage-advanced-drawer",
  "deploy-secondary-section",
  "edge-proof-trace-grid",
  "runtime-capability-strip",
  "remediation-command-topline"
].forEach((needle) =>
  assertContains(cssFile, css, needle)
);
[
  "product-grid-stage-models",
  "product-grid-stage-operate",
  "hub-flow",
  "hub-flow-shell",
  "hub-flow-step-active",
  "hub-stage-handoff",
  "hub-stage-command",
  "hub-stage-runbook",
  "mission-path-summary",
  "system-context-drawer"
].forEach((needle) => assertNotContains(cssFile, css, needle));

console.log(`Runtime workbench contract OK: ${jsFile}, ${cssFile}`);
