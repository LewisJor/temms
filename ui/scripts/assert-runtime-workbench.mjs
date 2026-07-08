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
const hubFlowStatePath = join(uiRoot, "src", "lib", "hub-flow-state.ts");
const hubFormActionsPath = join(uiRoot, "src", "lib", "hub-form-actions.ts");
const hubMissionContextPath = join(uiRoot, "src", "lib", "hub-mission-context.ts");
const readinessPath = join(uiRoot, "src", "lib", "readiness.ts");
const runtimeDecisionPath = join(uiRoot, "src", "lib", "runtime-decision.ts");
const proofCommandPath = join(uiRoot, "src", "lib", "proof-command.ts");
const runtimeRemediationPath = join(uiRoot, "src", "lib", "runtime-remediation.ts");
const runtimeStageViewPath = join(uiRoot, "src", "lib", "runtime-stage-view.ts");
const hubStageNavigationPath = join(uiRoot, "src", "lib", "hub-stage-navigation.ts");
const missionYamlImportPath = join(uiRoot, "src", "lib", "mission-yaml-import.ts");
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
const hubFlowStateSource = readFileSync(hubFlowStatePath, "utf8");
const hubFormActionsSource = readFileSync(hubFormActionsPath, "utf8");
const hubMissionContextSource = readFileSync(hubMissionContextPath, "utf8");
const readinessSource = readFileSync(readinessPath, "utf8");
const runtimeDecisionSource = readFileSync(runtimeDecisionPath, "utf8");
const proofCommandSource = readFileSync(proofCommandPath, "utf8");
const runtimeRemediationSource = readFileSync(runtimeRemediationPath, "utf8");
const runtimeStageViewSource = readFileSync(runtimeStageViewPath, "utf8");
const hubStageNavigationSource = readFileSync(hubStageNavigationPath, "utf8");
const missionYamlImportSource = readFileSync(missionYamlImportPath, "utf8");
const missionPackageSource = readFileSync(missionPackagePath, "utf8");
const missionWorkflowSource = readFileSync(missionWorkflowPath, "utf8");
const workbenchSource = `${source}\n${capabilityDossierSource}\n${edgeDeployStageSource}\n${deployListsSource}\n${edgeProofSource}\n${fieldOpsSource}\n${fieldOpsStageSource}\n${packageHandoffSource}\n${packageStageSource}\n${missionStagesSource}\n${modelPlanSource}\n${readinessPanelsSource}\n${runtimeDecisionTraceSource}\n${runtimeExecutionContractSource}\n${runtimeContractRowsSource}\n${runtimeMissionSource}\n${runtimeOperatorProofSource}\n${runtimeOptimizerSource}\n${runtimeWorkbenchSource}\n${workbenchFlowSource}\n${edgeProofWorkflowSource}\n${deploymentIntentSource}\n${edgeRuntimeMissionSource}\n${fieldOpsProofSource}\n${hubActionsSource}\n${hubFlowStateSource}\n${hubFormActionsSource}\n${hubMissionContextSource}\n${readinessSource}\n${runtimeDecisionSource}\n${proofCommandSource}\n${runtimeRemediationSource}\n${runtimeStageViewSource}\n${hubStageNavigationSource}\n${missionYamlImportSource}\n${missionPackageSource}\n${missionWorkflowSource}`;
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
  "useHubStageNavigation",
  "navigateHubStage",
  "data-stage-id",
  'aria-label={`${index + 1}. ${stage.label}. ${stage.value}. ${stage.detail}`}',
  "scrollIntoView",
  "showProductStage",
  "activeSlotForMission(snapshot.evidenceSummary?.active_slots, missionDraft.slot)",
  "missionOperationLedgerForSlot",
  "prioritizedEvidenceEvents(",
  "missionRolloutsForSelection",
  "missionRolloutPlansForSelection",
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
  "buildMissionWorkflowSignals",
  "buildRuntimeFitTileSummary",
  "readinessActionPlan",
  "readinessActionFocus",
  "readinessActionFocusNotice",
  "readinessActionSelection",
  "readinessCommandExecutionPlan",
  "readinessCommandEdgeExecutionNotice",
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
  "missionPackageContextInvalidation",
  "missionPackagePlanAdoption",
  "missionPackageDownloadAdoption",
  "buildMissionPackageStageStatus",
  "missionPackageStageBlocker",
  "missionPackageStagePlan",
  "buildMissionPackageStageRequest",
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
  "successWorkflowTarget: \"rollouts\"",
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
  "useHubStageNavigation",
  "stageFlowRef",
  "workflowRefForTarget",
  "workflow-target-active",
  "scrollIntoView({ behavior: \"smooth\", block: \"start\" })",
  "focus({ preventScroll: true })",
  "querySelector<HTMLElement>(`[data-stage-id=\"${stage}\"]`)"
].forEach((needle) => assertContains("Hub stage navigation sources", hubStageNavigationSource, needle));

[
  "buildHubFlowState",
  "buildReadinessContext",
  "buildRuntimeStageView",
  "buildEdgeRuntimeMission",
  "buildEdgeProofWorkflow",
  "buildMissionPackageManifest",
  "buildMissionPackageStageStatus",
  "buildHubStages",
  "readinessContextKey"
].forEach((needle) => assertContains("Hub flow state sources", hubFlowStateSource, needle));

[
  "buildHubFormAction",
  "\"compatibility-preview\"",
  "\"assign-rollout\"",
  "\"create-rollout-plan\"",
  "slot: fieldValue(form, \"slot\") || undefined",
  "runtime_target_id: fieldValue(form, \"runtime_target_id\") || undefined",
  "reason: \"operator assigned rollout from Mission Package Workbench\"",
  "reason: \"operator created rollout plan from Mission Package Workbench\"",
  "refresh: name !== \"compatibility-preview\""
].forEach((needle) => assertContains("Hub form action sources", hubFormActionsSource, needle));

[
  "buildHubMissionContext",
  "defaultModelSelectionId",
  "defaultDeviceSelectionId",
  "defaultRuntimeSelectionId",
  "missionRolloutsForSelection",
  "missionRolloutPlansForSelection",
  "missionOperationLedgerForSlot",
  "latestRuntimeRepairProofFor",
  "prioritizedEvidenceEvents",
  "buildReadinessVerdict"
].forEach((needle) => assertContains("Hub mission context sources", hubMissionContextSource, needle));

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
  "edgeRecommendationSelection",
  "deploymentIntentQueueAction",
  "title: \"Queue DDIL deployment intent\"",
  "workflowTarget: \"deployment\"",
  "modelId: recommendation.model_id ? String(recommendation.model_id) : undefined",
  "runtimeTargetId: recommendation.runtime_target_id ? String(recommendation.runtime_target_id) : undefined"
].forEach((needle) => assertContains("Deployment intent sources", deploymentIntentSource, needle));

[
  "activeSlotForMission",
  "missionOperationLedgerForSlot",
  "prioritizedEvidenceEvents",
  "latestRuntimeRepairProofFor",
  "buildBlockedOperationsQuarantineRequest",
  "buildDeadLetterAcknowledgeRequest",
  "buildDeadLetterBatchRequeueRequest",
  "buildEvidenceExportRequest",
  "buildAirgapExportRequest",
  "deadLetterRequeueUnavailableNotice",
  "pendingRuntimeRetargetUnavailableNotice",
  "This quarantined DDIL intent does not include a payload hash.",
  "This pending DDIL intent does not include a measured runtime target candidate."
].forEach((needle) => assertContains("Field Ops proof sources", fieldOpsProofSource, needle));

[
  "copyOperatorCommand",
  "Command opened in the payload panel.",
  "syncPendingOperationsWithReconciliation",
  "controlApi.syncPending",
  "loadSnapshotAfterReconciliation",
  "return { payload, snapshot }"
].forEach((needle) => assertContains("Hub action sources", hubActionsSource, needle));

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
  "buildMissionYamlImportResult",
  "missionYamlImportAdoption",
  "missionYamlImportErrorNotice",
  "packagePlan: undefined",
  "packageHandoff: undefined",
  "stage: \"mission\"",
  "title: \"Mission YAML imported\"",
  "title: \"Mission YAML import failed\"",
  "missionDraftFromYaml(currentDraft, yaml)",
  "missionSelectionFromYaml(yaml)",
  "selectedModelId: selectedYamlModel?.id",
  "selectedDeviceId: selectedYamlDevice ? deviceId(selectedYamlDevice) : undefined",
  "selectedRuntimeId: selectedYamlRuntime ? runtimeTargetId(selectedYamlRuntime) : undefined",
  "Selected ${appliedSelection.join(\", \")} from the spec.",
  "Unmatched hints: ${missingSelection.join(\", \")}."
].forEach((needle) => assertContains("Mission YAML import sources", missionYamlImportSource, needle));

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

[
  "edgeProofReadinessForContext",
  "edgeProofReadinessAdoptionForContext",
  "EdgeProofArtifactAdoption",
  "edgeProofGeneratedAdoption",
  "edgeProofDownloadAdoption",
  "handoff: undefined",
  "fileName: artifact.fileName",
  "edgeProofComponentDigestVerificationPendingStatus",
  "edgeProofComponentDigestVerificationFailureStatus",
  "readinessMatchesContext",
  "selectionMatchesContext",
  "schema_version !== \"temms-edge-runtime-proof/v1\"",
  "runtime_decision_trace",
  "runtime_workbench"
].forEach((needle) => assertContains("Edge proof workflow sources", edgeProofWorkflowSource, needle));

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

const bundledFieldOpsProof = await build({
  bundle: true,
  entryPoints: [fieldOpsProofPath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const fieldOpsProofModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledFieldOpsProof.outputFiles[0].text).toString("base64")}`
);
const bundledHubActions = await build({
  bundle: true,
  entryPoints: [hubActionsPath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const hubActionsModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledHubActions.outputFiles[0].text).toString("base64")}`
);
const copiedOperatorCommands = [];
const copiedOperatorCommandResult = await hubActionsModule.copyOperatorCommand({
  command: "uv run temms hub verify-edge-proof /tmp/proof.json",
  label: "Edge proof verify",
  writeText: async (command) => {
    copiedOperatorCommands.push(command);
  }
});
if (
  copiedOperatorCommands[0] !== "uv run temms hub verify-edge-proof /tmp/proof.json" ||
  copiedOperatorCommandResult.preview !== undefined ||
  copiedOperatorCommandResult.toast.tone !== "success" ||
  copiedOperatorCommandResult.toast.title !== "Edge proof verify copied"
) {
  throw new Error("hub actions copy command should write the command and emit a success toast");
}
const fallbackOperatorCommandResult = await hubActionsModule.copyOperatorCommand({
  command: "uv run temms hub edge-runtime-mission",
  label: "Edge execution command",
  writeText: async () => {
    throw new Error("clipboard unavailable");
  }
});
if (
  fallbackOperatorCommandResult.preview?.title !== "Edge execution command" ||
  fallbackOperatorCommandResult.preview?.payload?.command !== "uv run temms hub edge-runtime-mission" ||
  fallbackOperatorCommandResult.toast.tone !== "info" ||
  fallbackOperatorCommandResult.toast.title !== "Edge execution command ready"
) {
  throw new Error("hub actions copy command should open a preview fallback when clipboard write fails");
}
const ddilSyncCalls = [];
const reconciledSnapshotFixture = {
  benchmarks: [],
  devices: [{ device_id: "edge-rpi5" }],
  evidenceBundles: [],
  packages: [],
  rolloutPlans: [],
  rollouts: [],
  runtimeTargets: [],
  runtimeValidations: []
};
const ddilSyncResult = await hubActionsModule.syncPendingOperationsWithReconciliation("token-123", {
  loadReconciledSnapshot: async (token) => {
    ddilSyncCalls.push(`load:${token}`);
    return reconciledSnapshotFixture;
  },
  syncPending: async (token) => {
    ddilSyncCalls.push(`sync:${token}`);
    return { synced: true, token };
  }
});
if (
  ddilSyncCalls.join(">") !== "sync:token-123>load:token-123" ||
  ddilSyncResult.payload.synced !== true ||
  ddilSyncResult.snapshot !== reconciledSnapshotFixture
) {
  throw new Error("hub actions DDIL sync should run sync before loading the reconciled snapshot");
}
const activeSlotFixture = [
  { active_model: "model-rgb", slot: "vision" },
  { active_model: "model-thermal", slot: "thermal" }
];
const thermalActiveSlot = fieldOpsProofModule.activeSlotForMission(activeSlotFixture, "thermal");
if (thermalActiveSlot?.active_model !== "model-thermal") {
  throw new Error("field ops active slot should follow the selected mission slot");
}
const defaultActiveSlot = fieldOpsProofModule.activeSlotForMission(activeSlotFixture, "");
if (defaultActiveSlot?.active_model !== "model-rgb") {
  throw new Error("field ops active slot should default blank mission slots to vision");
}
const fallbackActiveSlot = fieldOpsProofModule.activeSlotForMission(
  [{ active_model: "model-lidar", slot: "lidar" }],
  "thermal"
);
if (fallbackActiveSlot?.active_model !== "model-lidar") {
  throw new Error("field ops active slot should fall back to first evidence slot when the mission slot is absent");
}
if (fieldOpsProofModule.activeSlotForMission(undefined, "thermal") !== undefined) {
  throw new Error("field ops active slot should be empty when evidence slots are missing");
}
const scopedPendingOperations = fieldOpsProofModule.missionOperationLedgerForSlot(
  [
    { operation: "deploy", payload: { request: { slot: "thermal" } }, payload_sha256: "thermal" },
    { operation: "deploy", slot: "vision", payload_sha256: "vision" },
    { operation: "deploy", payload_sha256: "legacy" }
  ],
  "thermal"
);
const scopedPendingIds = scopedPendingOperations.map((operation) => operation.payload_sha256).join(",");
if (scopedPendingIds !== "thermal,legacy") {
  throw new Error(`field ops pending operations should be scoped to mission slot: ${scopedPendingIds}`);
}
const defaultPendingOperations = fieldOpsProofModule.missionOperationLedgerForSlot(
  [
    { operation: "deploy", payload_sha256: "vision", slot: "vision" },
    { operation: "deploy", payload_sha256: "thermal", slot: "thermal" },
    { operation: "deploy", payload_sha256: "legacy" }
  ],
  ""
);
const defaultPendingIds = defaultPendingOperations.map((operation) => operation.payload_sha256).join(",");
if (defaultPendingIds !== "vision,legacy") {
  throw new Error("field ops pending operations should default blank mission slots to vision");
}
const requeueRequestFixture = fieldOpsProofModule.buildDeadLetterRequeueRequest({
  payload_sha256: "deadbeef"
});
if (
  requeueRequestFixture?.actor !== "operator:mission-package-workbench" ||
  requeueRequestFixture?.payload_sha256s?.[0] !== "deadbeef" ||
  requeueRequestFixture?.require_ready !== true
) {
  throw new Error("field ops single requeue request should preserve DDIL payload hash and ready gate");
}
if (fieldOpsProofModule.buildDeadLetterRequeueRequest({}) !== undefined) {
  throw new Error("field ops single requeue request should be unavailable without a payload hash");
}
const requeueUnavailableNotice = fieldOpsProofModule.deadLetterRequeueUnavailableNotice();
if (
  requeueUnavailableNotice.tone !== "info" ||
  requeueUnavailableNotice.title !== "Requeue unavailable" ||
  !requeueUnavailableNotice.detail.includes("payload hash")
) {
  throw new Error("field ops single requeue unavailable notice should explain the missing payload hash");
}
const quarantineBatchRequestFixture = fieldOpsProofModule.buildBlockedOperationsQuarantineRequest();
if (
  quarantineBatchRequestFixture?.actor !== "operator:mission-package-workbench" ||
  quarantineBatchRequestFixture?.reason !== "operator quarantined blocked DDIL preflight"
) {
  throw new Error("field ops quarantine request should preserve Workbench actor and quarantine reason");
}
const acknowledgeBatchRequestFixture = fieldOpsProofModule.buildDeadLetterAcknowledgeRequest();
if (
  acknowledgeBatchRequestFixture?.actor !== "operator:mission-package-workbench" ||
  acknowledgeBatchRequestFixture?.reason !== "operator reviewed quarantined DDIL intents"
) {
  throw new Error("field ops acknowledge request should preserve Workbench actor and review reason");
}
const batchRequeueRequestFixture = fieldOpsProofModule.buildDeadLetterBatchRequeueRequest();
if (
  batchRequeueRequestFixture?.actor !== "operator:mission-package-workbench" ||
  batchRequeueRequestFixture?.reason !== "operator requeued remediated DDIL intents" ||
  batchRequeueRequestFixture?.require_ready !== true
) {
  throw new Error("field ops batch requeue request should preserve Workbench actor and ready gate");
}
const summaryExportRequestFixture = fieldOpsProofModule.buildEvidenceExportRequest("summary");
if (summaryExportRequestFixture?.summary !== true || summaryExportRequestFixture?.summary_limit !== 20) {
  throw new Error("field ops summary export request should preserve summary limit policy");
}
const replayExportRequestFixture = fieldOpsProofModule.buildEvidenceExportRequest("replay");
if (replayExportRequestFixture?.replay !== true || replayExportRequestFixture?.replay_limit !== 50) {
  throw new Error("field ops replay export request should preserve replay limit policy");
}
const fullExportRequestFixture = fieldOpsProofModule.buildEvidenceExportRequest("full");
if (fullExportRequestFixture?.decision_limit !== 100 || fullExportRequestFixture?.include_benchmarks !== true) {
  throw new Error("field ops full export request should preserve decision and benchmark policy");
}
const airgapWithPackagesFixture = fieldOpsProofModule.buildAirgapExportRequest(true);
const airgapWithoutPackagesFixture = fieldOpsProofModule.buildAirgapExportRequest(false);
if (airgapWithPackagesFixture?.include_packages !== true || airgapWithoutPackagesFixture?.include_packages !== false) {
  throw new Error("field ops air-gap export request should preserve package inclusion flag");
}
const retargetRequestFixture = fieldOpsProofModule.buildPendingRuntimeRetargetRequest({
  payload_sha256: "pending-hash",
  runtime_workbench_best_runtime_target_id: "temms-jetson-trt"
});
if (
  retargetRequestFixture?.payload_sha256 !== "pending-hash" ||
  retargetRequestFixture?.runtime_target_id !== "temms-jetson-trt" ||
  retargetRequestFixture?.reason !== "operator selected measured best runtime target"
) {
  throw new Error("field ops runtime retarget request should use the measured workbench best runtime");
}
const remediationRetargetRequestFixture = fieldOpsProofModule.buildPendingRuntimeRetargetRequest({
  best_runtime_target_id: "temms-rpi5-tflite",
  payload_sha256: "pending-hash",
  runtime_remediation_runtime_target_id: "temms-jetson-ort-trt"
});
if (remediationRetargetRequestFixture?.runtime_target_id !== "temms-jetson-ort-trt") {
  throw new Error("field ops runtime retarget request should prefer the explicit remediation runtime target");
}
if (fieldOpsProofModule.buildPendingRuntimeRetargetRequest({ payload_sha256: "missing-runtime" }) !== undefined) {
  throw new Error("field ops runtime retarget request should be unavailable without a runtime target candidate");
}
const retargetUnavailableNotice = fieldOpsProofModule.pendingRuntimeRetargetUnavailableNotice();
if (
  retargetUnavailableNotice.tone !== "info" ||
  retargetUnavailableNotice.title !== "Runtime retarget unavailable" ||
  !retargetUnavailableNotice.detail.includes("measured runtime target candidate")
) {
  throw new Error("field ops runtime retarget unavailable notice should explain the missing runtime target");
}
const timelineFixture = [
  {
    active_runtime_proof: true,
    kind: "runtime_fit",
    record: { selection: { model_id: "model-rgb", slot: "vision" } },
    slot: "vision",
    summary: "model-rgb runtime fit 95/100"
  },
  { kind: "deployment", summary: "legacy unscoped event" },
  {
    active_runtime_proof: true,
    kind: "runtime_fit",
    record: { selection: { model_id: "model-thermal", slot: "thermal" } },
    slot: "thermal",
    summary: "model-thermal runtime fit 98/100"
  },
  {
    kind: "runtime_fit",
    record: { selection: { model_id: "model-thermal-fallback", slot: "thermal" } },
    slot: "thermal",
    summary: "model-thermal-fallback runtime fit 82/100"
  }
];
const thermalEvents = fieldOpsProofModule.prioritizedEvidenceEvents(
  timelineFixture,
  "model-thermal",
  "thermal"
);
if (thermalEvents[0]?.summary !== "model-thermal runtime fit 98/100") {
  throw new Error("field ops events should prioritize active runtime proof for the selected mission slot/model");
}
if (thermalEvents.some((event) => event.slot === "vision")) {
  throw new Error("field ops events should exclude explicit evidence from other mission slots");
}
const defaultSlotEvents = fieldOpsProofModule.prioritizedEvidenceEvents(
  timelineFixture,
  "model-rgb",
  ""
);
if (defaultSlotEvents[0]?.summary !== "model-rgb runtime fit 95/100") {
  throw new Error("field ops events should default blank mission slots to vision evidence");
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
const edgeProofQueryFixture = edgeProofWorkflowModule.buildEdgeProofQuery({
  device_id: "edge-thermal-01",
  model_id: "model-thermal-detector-001",
  package_id: "pkg-thermal-models-20260708",
  runtime_target_id: "temms-jetson-ort-trt",
  slot: "thermal"
});
const expectedEdgeProofQuery = {
  device_id: "edge-thermal-01",
  min_runtime_fit: 95,
  model_id: "model-thermal-detector-001",
  package_id: "pkg-thermal-models-20260708",
  require_best_runtime: true,
  require_capability_lock: true,
  require_go: true,
  runtime_target_id: "temms-jetson-ort-trt",
  slot: "thermal",
  source_action: "edge-runtime-mission"
};
Object.entries(expectedEdgeProofQuery).forEach(([key, value]) => {
  if (edgeProofQueryFixture[key] !== value) {
    throw new Error(`edge proof query ${key} mismatch: ${edgeProofQueryFixture[key]}`);
  }
});
const matchingEdgeProofReadiness = {
  gates: [{ gate_id: "runtime-fit", status: "go" }],
  selection: {
    device_id: "edge-thermal-01",
    model_id: "model-thermal-detector-001",
    package_id: "pkg-thermal-models-20260708",
    runtime_target_id: "temms-jetson-ort-trt",
    slot: "thermal"
  },
  status: "go"
};
const matchingEdgeProofPayload = {
  readiness: matchingEdgeProofReadiness,
  schema_version: "temms-edge-runtime-proof/v1",
  selection: matchingEdgeProofReadiness.selection
};
const generatedProofAdoption = edgeProofWorkflowModule.edgeProofGeneratedAdoption(
  matchingEdgeProofPayload
);
if (
  generatedProofAdoption.proof !== matchingEdgeProofPayload ||
  generatedProofAdoption.preview !== matchingEdgeProofPayload ||
  generatedProofAdoption.handoff !== undefined
) {
  throw new Error("edge proof generated adoption should retain proof payload and clear download handoff");
}
const edgeProofDownloadHandoffFixture = {
  attestation: "signed",
  edgeExecutionManifestSha256: "c".repeat(64),
  fileName: "thermal-proof.json",
  gateStatus: "passed",
  keyFingerprint: "demo-key",
  payloadSha256: "d".repeat(64),
  runtimeDecisionTraceSha256: "e".repeat(64),
  runtimeWorkbenchSha256: "f".repeat(64)
};
const downloadedProofAdoption = edgeProofWorkflowModule.edgeProofDownloadAdoption({
  fileName: "thermal-proof.json",
  handoff: edgeProofDownloadHandoffFixture,
  payload: matchingEdgeProofPayload
});
if (
  downloadedProofAdoption.fileName !== "thermal-proof.json" ||
  downloadedProofAdoption.handoff !== edgeProofDownloadHandoffFixture ||
  downloadedProofAdoption.proof !== matchingEdgeProofPayload ||
  downloadedProofAdoption.preview !== matchingEdgeProofPayload
) {
  throw new Error("edge proof download adoption should retain proof payload, file name, and download handoff");
}
if (
  edgeProofWorkflowModule.edgeProofReadinessForContext(matchingEdgeProofPayload, edgeProofQueryFixture) !==
  matchingEdgeProofReadiness
) {
  throw new Error("edge proof readiness adoption should retain readiness for matching proof context");
}
const staleEdgeProofPayload = {
  ...matchingEdgeProofPayload,
  selection: { ...matchingEdgeProofReadiness.selection, slot: "vision" }
};
if (edgeProofWorkflowModule.edgeProofReadinessForContext(staleEdgeProofPayload, edgeProofQueryFixture) !== undefined) {
  throw new Error("edge proof readiness adoption should reject proofs for another mission slot");
}
const staleReadinessPayload = {
  ...matchingEdgeProofPayload,
  readiness: {
    ...matchingEdgeProofReadiness,
    selection: { ...matchingEdgeProofReadiness.selection, runtime_target_id: "temms-rpi5-tflite" }
  }
};
if (edgeProofWorkflowModule.edgeProofReadinessForContext(staleReadinessPayload, edgeProofQueryFixture) !== undefined) {
  throw new Error("edge proof readiness adoption should reject readiness for another runtime target");
}
if (
  edgeProofWorkflowModule.edgeProofReadinessForContext(
    { ...matchingEdgeProofPayload, schema_version: "temms-edge-runtime-proof/v0" },
    edgeProofQueryFixture
  ) !== undefined
) {
  throw new Error("edge proof readiness adoption should reject non-proof payloads");
}
const edgeProofReadinessAdoption = edgeProofWorkflowModule.edgeProofReadinessAdoptionForContext({
  context: edgeProofQueryFixture,
  proof: matchingEdgeProofPayload
});
if (!edgeProofReadinessAdoption || edgeProofReadinessAdoption.readiness !== matchingEdgeProofReadiness) {
  throw new Error("edge proof readiness adoption should expose matching proof readiness");
}
const adoptionBaseSnapshot = {
  benchmarks: [],
  devices: [{ device_id: "edge-thermal-01" }],
  evidenceBundles: [],
  packages: [],
  readiness: { status: "attention" },
  rolloutPlans: [],
  rollouts: [],
  runtimeTargets: [],
  runtimeValidations: []
};
const adoptedSnapshot = edgeProofReadinessAdoption.applyToSnapshot(adoptionBaseSnapshot);
if (
  adoptedSnapshot === adoptionBaseSnapshot ||
  adoptedSnapshot.devices !== adoptionBaseSnapshot.devices ||
  adoptedSnapshot.readiness !== matchingEdgeProofReadiness
) {
  throw new Error("edge proof readiness adoption should patch snapshot readiness without rebuilding snapshot lists");
}
if (
  edgeProofWorkflowModule.edgeProofReadinessAdoptionForContext({
    context: edgeProofQueryFixture,
    proof: staleEdgeProofPayload
  }) !== undefined
) {
  throw new Error("edge proof readiness adoption should not patch snapshots for stale proof paths");
}
const digestStatusFixture = {
  detail: "Runtime workbench, trace, and execution manifest hashes are retained.",
  digestCount: 3,
  digests: [
    { key: "runtime_workbench_sha256", label: "Runtime workbench", value: "a".repeat(64) }
  ],
  errors: [],
  schema: "temms-edge-runtime-proof-component-digests/v1",
  status: "retained",
  tone: "good",
  value: "digests retained"
};
const verifyingDigestStatus = edgeProofWorkflowModule.edgeProofComponentDigestVerificationPendingStatus(
  digestStatusFixture
);
if (
  verifyingDigestStatus.status !== "verifying" ||
  verifyingDigestStatus.tone !== "neutral" ||
  verifyingDigestStatus.value !== "verifying digests"
) {
  throw new Error("edge proof digest verification should expose deterministic verifying state");
}
const failedDigestStatus = edgeProofWorkflowModule.edgeProofComponentDigestVerificationFailureStatus(
  digestStatusFixture,
  new Error("manifest digest mismatch")
);
if (
  failedDigestStatus.status !== "mismatch" ||
  failedDigestStatus.tone !== "bad" ||
  failedDigestStatus.errors[0] !== "manifest digest mismatch" ||
  failedDigestStatus.value !== "digest verification failed"
) {
  throw new Error("edge proof digest verification should expose deterministic failure state");
}
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
const bundledHubMissionContext = await build({
  bundle: true,
  entryPoints: [hubMissionContextPath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const hubMissionContextModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledHubMissionContext.outputFiles[0].text).toString("base64")}`
);
if (
  hubMissionContextModule.defaultModelSelectionId({
    activeModelId: "model-active",
    models: [{ id: "model-fallback" }, { id: "model-active" }],
    selectedModelId: ""
  }) !== "model-active"
) {
  throw new Error("default model selection should prefer active mission evidence");
}
if (
  hubMissionContextModule.defaultModelSelectionId({
    activeModelId: "model-missing",
    models: [{ id: "model-first" }],
    selectedModelId: ""
  }) !== "model-first"
) {
  throw new Error("default model selection should fall back to first available model");
}
if (
  hubMissionContextModule.defaultModelSelectionId({
    activeModelId: "model-active",
    models: [{ id: "model-active" }],
    selectedModelId: "operator-model"
  }) !== undefined
) {
  throw new Error("default model selection should preserve explicit operator choices");
}
if (
  hubMissionContextModule.defaultDeviceSelectionId({
    devices: [{ device_id: "edge-rpi5" }],
    selectedDeviceId: ""
  }) !== "edge-rpi5"
) {
  throw new Error("default device selection should choose the first edge when none is selected");
}
if (
  hubMissionContextModule.defaultDeviceSelectionId({
    devices: [{ device_id: "edge-rpi5" }],
    selectedDeviceId: "operator-edge"
  }) !== undefined
) {
  throw new Error("default device selection should preserve explicit operator choices");
}
if (
  hubMissionContextModule.defaultRuntimeSelectionId({
    selectedRuntime: { runtime_target_id: "temms-rpi5-tflite" },
    selectedRuntimeId: ""
  }) !== "temms-rpi5-tflite"
) {
  throw new Error("default runtime selection should adopt the derived runtime");
}
if (
  hubMissionContextModule.defaultRuntimeSelectionId({
    selectedRuntime: { runtime_target_id: "temms-rpi5-tflite" },
    selectedRuntimeId: "operator-runtime"
  }) !== undefined
) {
  throw new Error("default runtime selection should preserve explicit operator choices");
}
const bundledHubFlowState = await build({
  bundle: true,
  entryPoints: [hubFlowStatePath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const hubFlowStateModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledHubFlowState.outputFiles[0].text).toString("base64")}`
);
const modelFixture = {
  format: "onnx",
  id: "model-yolov8-lowlight-001",
  maxLatencyP95Ms: 95,
  minThroughputIps: 20,
  name: "YOLOv8 lowlight",
  packageId: "pkg-vision-models-20240115"
};
const thermalMissionDraftFixture = { ...missionDraftFixture, sensor: "camera.thermal", slot: "thermal" };
const missionContextFixture = hubMissionContextModule.buildHubMissionContext({
  missionDraft: thermalMissionDraftFixture,
  selectedDeviceId: "edge-thermal-01",
  selectedModelId: "model-thermal-detector-001",
  selectedRuntimeId: "temms-jetson-ort-trt",
  snapshot: {
    benchmarks: [
      {
        benchmark_id: "bench-thermal",
        created_at: new Date().toISOString(),
        device_id: "edge-thermal-01",
        model_id: "model-thermal-detector-001",
        package_id: "pkg-thermal-models-20260708",
        result: {
          latency_ms: { p95: 76 },
          throughput: { inferences_per_second: 12 }
        },
        runtime_target_id: "temms-jetson-ort-trt"
      }
    ],
    compatibilityMatrix: {
      recommendations: [
        {
          device_id: "edge-thermal-01",
          model_id: "model-thermal-detector-001",
          runtime_target_id: "temms-jetson-ort-trt"
        }
      ]
    },
    devices: [
      {
        device_id: "edge-thermal-01",
        inventory: {
          accelerators: { gpu: { available: true } },
          memory_available_mb: 4096,
          power_source: "line",
          runtimes: { onnx: { available: true }, onnxruntime: { available: true, providers: ["tensorrt"] } },
          storage_available_mb: 8192,
          temperature_c: 44
        },
        profile: "jetson",
        status: "online"
      }
    ],
    evidenceBundles: [{ evidence_id: "evidence-thermal" }],
    evidenceSummary: {
      active_slots: [
        { active_model: "model-rgb", slot: "vision" },
        { active_model: "model-thermal-detector-001", slot: "thermal" }
      ],
      counts: { timeline_entries: 3 },
      runtime: {
        deployment_state: { state: "READY" },
        offline_mode: true,
        pending_operation_dead_letters: [
          { payload: { request: { slot: "thermal" } }, payload_sha256: "dead-thermal" },
          { payload: { request: { slot: "thermal" } }, payload_sha256: "dead-requeued", requeued: true },
          { payload_sha256: "dead-legacy" },
          { payload_sha256: "dead-vision", slot: "vision" }
        ],
        pending_operation_preflight: {
          blocked: 0,
          optimization_advisories: 1,
          ready: 1,
          superseded: 0
        },
        pending_operation_verification: { invalid: 0, verified: 1 },
        pending_operations: [
          { payload: { request: { slot: "thermal" } }, payload_sha256: "pending-thermal" },
          { payload_sha256: "pending-legacy" },
          { payload_sha256: "pending-vision", slot: "vision" }
        ]
      },
      timeline: [
        {
          active_runtime_proof: true,
          kind: "runtime_fit",
          record: { selection: { model_id: "model-thermal-detector-001", slot: "thermal" } },
          slot: "thermal",
          summary: "thermal runtime proof retained"
        },
        {
          active_runtime_proof: true,
          kind: "runtime_fit",
          record: { selection: { model_id: "model-rgb", slot: "vision" } },
          slot: "vision",
          summary: "vision runtime proof"
        },
        { kind: "deployment", summary: "legacy deployment event" }
      ],
      trust: { signed_package_imports: 1 }
    },
    missionReplay: {
      outcome: { completed_phases: 2, incomplete_phases: [] },
      phases: [
        { label: "plan", status: "complete" },
        { label: "deploy", status: "complete" }
      ]
    },
    packages: [
      {
        metadata: {
          models: [
            {
              format: "onnx",
              id: "model-thermal-detector-001",
              name: "Thermal detector",
              performance_slo: {
                max_benchmark_age_seconds: 86400,
                max_latency_ms_p95: 95,
                min_throughput_ips: 8
              },
              resource_requirements: {
                max_temperature_c: 80,
                min_memory_available_mb: 1024,
                min_storage_available_mb: 1024,
                required_power_source: "line"
              },
              runtime_constraints: {
                device_profiles: ["jetson"],
                providers: ["tensorrt"],
                runtimes: ["onnx"]
              },
              version: "1.0.0"
            }
          ],
          validation: { signature_verified: true }
        },
        name: "Thermal models",
        package_id: "pkg-thermal-models-20260708",
        promotion: { state: "approved" },
        version: "2026.7.8"
      }
    ],
    rolloutPlans: [
      {
        model_id: "model-thermal-detector-001",
        package_id: "pkg-thermal-models-20260708",
        plan_id: "plan-thermal",
        slot: "thermal"
      },
      {
        package_id: "pkg-thermal-models-20260708",
        plan_id: "plan-package-wide",
        slot: "thermal"
      },
      {
        model_id: "model-thermal-detector-001",
        package_id: "pkg-thermal-models-20260708",
        plan_id: "plan-vision",
        slot: "vision"
      }
    ],
    rollouts: [
      {
        model_id: "model-thermal-detector-001",
        package_id: "pkg-thermal-models-20260708",
        rollout_id: "rollout-thermal",
        slot: "thermal",
        state: "activated",
        updated_at: "2026-07-08T02:00:00.000Z"
      },
      {
        model_id: "model-thermal-detector-001",
        package_id: "pkg-thermal-models-20260708",
        rollout_id: "rollout-legacy",
        state: "assigned",
        updated_at: "2026-07-08T01:00:00.000Z"
      },
      {
        model_id: "model-thermal-detector-001",
        package_id: "pkg-thermal-models-20260708",
        rollout_id: "rollout-vision",
        slot: "vision",
        state: "assigned"
      }
    ],
    runtimeTargets: [
      {
        device_profiles: ["jetson"],
        runtime_target_id: "temms-jetson-ort-trt",
        runtimes: { onnx: { available: true }, onnxruntime: { available: true, providers: ["tensorrt"] } }
      }
    ],
    runtimeValidations: [
      {
        package_id: "pkg-thermal-models-20260708",
        result: { ok: true },
        runtime_target_id: "temms-jetson-ort-trt",
        validation_id: "validation-thermal"
      }
    ]
  }
});
if (missionContextFixture.selectedModel?.id !== "model-thermal-detector-001") {
  throw new Error("hub mission context should select the requested model");
}
if (missionContextFixture.selectedPackage?.package_id !== "pkg-thermal-models-20260708") {
  throw new Error("hub mission context should bind the selected model back to its package");
}
if (missionContextFixture.selectedRuntime?.runtime_target_id !== "temms-jetson-ort-trt") {
  throw new Error("hub mission context should select the requested runtime target");
}
if (missionContextFixture.activeModelId !== "model-thermal-detector-001") {
  throw new Error("hub mission context should follow active evidence for the selected mission slot");
}
if (missionContextFixture.edgeRuntimeFit.tone !== "good" || missionContextFixture.resourceEnvelopeFit.tone !== "good") {
  throw new Error(
    `hub mission context should preserve validated on-device runtime and resource fit: runtime=${missionContextFixture.edgeRuntimeFit.tone}/${missionContextFixture.edgeRuntimeFit.detail}; resource=${missionContextFixture.resourceEnvelopeFit.tone}/${missionContextFixture.resourceEnvelopeFit.detail}`
  );
}
if (missionContextFixture.nextPackageState !== "released") {
  throw new Error("hub mission context should keep package promotion state with the selected model package");
}
if (missionContextFixture.missionRollouts.map((rollout) => rollout.rollout_id).join(",") !== "rollout-thermal,rollout-legacy") {
  throw new Error("hub mission context should scope rollouts to selected model and mission slot");
}
if (missionContextFixture.missionRolloutPlans.map((plan) => plan.plan_id).join(",") !== "plan-thermal,plan-package-wide") {
  throw new Error("hub mission context should scope rollout plans to selected model/package and mission slot");
}
if (missionContextFixture.pendingOperationLedger.map((operation) => operation.payload_sha256).join(",") !== "pending-thermal,pending-legacy") {
  throw new Error("hub mission context should scope pending DDIL operations to selected mission slot");
}
if (missionContextFixture.deadLetteredOperationLedger.map((operation) => operation.payload_sha256).join(",") !== "dead-thermal,dead-legacy") {
  throw new Error("hub mission context should scope unresolved DDIL dead letters to selected mission slot");
}
if (missionContextFixture.latestEvents[0]?.summary !== "thermal runtime proof retained") {
  throw new Error("hub mission context should prioritize selected-slot runtime proof events");
}
if (missionContextFixture.deploymentDetail !== "activated model-thermal-detector-001") {
  throw new Error("hub mission context should show active selected-slot deployment detail");
}
if (missionContextFixture.derivedReadinessVerdict.gates.length === 0) {
  throw new Error("hub mission context should produce an operator readiness verdict");
}
const flowReadinessSelection = {
  device_id: "edge-thermal-01",
  model_id: "model-thermal-detector-001",
  package_id: "pkg-thermal-models-20260708",
  runtime_target_id: "temms-jetson-ort-trt",
  slot: "thermal"
};
const flowContextReadiness = {
  edge_execution_contract: {
    target_selection: {
      best_runtime_target_id: "temms-jetson-ort-trt",
      selected_runtime_target_id: "temms-jetson-ort-trt"
    }
  },
  gates: [{ gate_id: "runtime-fit", label: "Runtime fit", status: "go" }],
  headline: "Thermal mission can deploy",
  next_action: "Stage thermal package",
  runtime_fit: {
    detail: "validated thermal edge path",
    runtime_target_id: "temms-jetson-ort-trt",
    score: 98,
    tier: "optimal"
  },
  selection: flowReadinessSelection,
  status: "go"
};
const flowMissionPackagePlan = {
  ...missionPackageModule.buildMissionPackageManifest({
    device: missionContextFixture.selectedDevice,
    draft: thermalMissionDraftFixture,
    model: missionContextFixture.selectedModel,
    runtime: missionContextFixture.selectedRuntime
  }),
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
    rollout_id: "rollout-model-thermal-detector-001-temms-jetson-ort-trt-edge-thermal-01"
  },
  edge_handoff: { schema_version: "temms-edge-mission-package-handoff/v1" },
  proof_gate: { status: "passed" }
};
const flowStateFixture = hubFlowStateModule.buildHubFlowState({
  activeHubStage: "runtime",
  contextReadiness: flowContextReadiness,
  hasLoadedSnapshot: true,
  lastMissionPackageHandoff: undefined,
  missionContext: missionContextFixture,
  missionDraft: thermalMissionDraftFixture,
  missionPackagePlan: flowMissionPackagePlan,
  snapshot: {
    benchmarks: [],
    devices: [],
    evidenceBundles: [{ evidence_id: "evidence-thermal" }],
    missionReplay: { phases: [] },
    packages: [],
    readiness: undefined,
    rolloutPlans: [],
    rollouts: [],
    runtimeTargets: [
      {
        device_profiles: ["jetson"],
        runtime_target_id: "temms-jetson-ort-trt",
        runtimes: { onnx: { available: true }, onnxruntime: { available: true, providers: ["tensorrt"] } }
      }
    ],
    runtimeValidations: [
      {
        package_id: "pkg-thermal-models-20260708",
        result: { ok: true },
        runtime_target_id: "temms-jetson-ort-trt",
        validation_id: "validation-thermal"
      }
    ]
  }
});
if (
  flowStateFixture.readinessKey !==
  "pkg-thermal-models-20260708|model-thermal-detector-001|edge-thermal-01|temms-jetson-ort-trt|thermal"
) {
  throw new Error(`hub flow state should bind readiness context to selected mission path: ${flowStateFixture.readinessKey}`);
}
if (flowStateFixture.scopedReadiness !== flowContextReadiness || flowStateFixture.readinessVerdict.label !== "go") {
  throw new Error("hub flow state should prefer fresh context readiness for the selected mission path");
}
if (!flowStateFixture.edgeProofWorkflow.generateCommand.includes("--slot thermal")) {
  throw new Error("hub flow state should build edge proof workflow for the selected mission slot");
}
if (flowStateFixture.runtimeStageView.remediationContext.slot !== "thermal") {
  throw new Error("hub flow state should retain thermal slot in runtime remediation context");
}
if (
  flowStateFixture.missionPackageManifest.selection?.model_id !== "model-thermal-detector-001" ||
  flowStateFixture.missionPackageManifest.selection?.runtime_target_id !== "temms-jetson-ort-trt"
) {
  throw new Error("hub flow state should preserve selected model/runtime in mission package manifest");
}
if (!flowStateFixture.canStageMissionPackage || !flowStateFixture.missionPackageStageStatus.stageable) {
  throw new Error("hub flow state should unlock staging after proof gate and deployment intent pass");
}
if (flowStateFixture.hubStages.map((stage) => stage.id).join(">") !== "mission>model>runtime>handling>package>deploy>field") {
  throw new Error("hub flow state should preserve the mission-to-edge stage order");
}
if (flowStateFixture.showProductStage !== false) {
  throw new Error("hub flow state should keep runtime stage outside the product-grid advanced surface");
}
const edgeRecommendationSelectionFixture = deploymentIntentModule.edgeRecommendationSelection({
  device_id: "edge-thermal-01",
  model_id: "model-thermal-detector-001",
  runtime_target_id: "temms-jetson-ort-trt"
});
if (
  edgeRecommendationSelectionFixture.deviceId !== "edge-thermal-01" ||
  edgeRecommendationSelectionFixture.modelId !== "model-thermal-detector-001" ||
  edgeRecommendationSelectionFixture.runtimeTargetId !== "temms-jetson-ort-trt" ||
  edgeRecommendationSelectionFixture.workflowTarget !== "deployment"
) {
  throw new Error("edge recommendation selection should bind model, edge, runtime, and deploy focus");
}
const partialEdgeRecommendationSelectionFixture = deploymentIntentModule.edgeRecommendationSelection({
  device_id: "edge-rpi5",
  model_id: null,
  runtime_target_id: null
});
if (
  partialEdgeRecommendationSelectionFixture.deviceId !== "edge-rpi5" ||
  partialEdgeRecommendationSelectionFixture.modelId !== undefined ||
  partialEdgeRecommendationSelectionFixture.runtimeTargetId !== undefined ||
  partialEdgeRecommendationSelectionFixture.workflowTarget !== "deployment"
) {
  throw new Error("edge recommendation selection should preserve partial recommendation paths");
}
const promotionRequestFixture = deploymentIntentModule.buildPackagePromotionRequest("released");
if (
  promotionRequestFixture.state !== "released" ||
  promotionRequestFixture.actor !== "operator:react-ui" ||
  !String(promotionRequestFixture.reason).includes("promoted to released")
) {
  throw new Error("package promotion request should preserve promotion state, actor, and reason");
}
const rolloutApprovalRequestFixture = deploymentIntentModule.buildRolloutApprovalRequest();
if (
  rolloutApprovalRequestFixture.actor !== "operator:approver-ui" ||
  rolloutApprovalRequestFixture.reason !== "mission policy approved from Mission Package Workbench"
) {
  throw new Error("rollout approval request should preserve approver actor and mission policy reason");
}
const rolloutApplyRequestFixture = deploymentIntentModule.buildRolloutApplyRequest({
  rollout: { model_id: "model-from-rollout" },
  selectedModel: { id: "model-from-selection" }
});
if (
  rolloutApplyRequestFixture.actor !== "operator:react-ui" ||
  rolloutApplyRequestFixture.model_id !== "model-from-rollout"
) {
  throw new Error("rollout apply request should prefer the rollout model id");
}
const fallbackRolloutApplyRequestFixture = deploymentIntentModule.buildRolloutApplyRequest({
  rollout: {},
  selectedModel: { id: "model-from-selection" }
});
if (fallbackRolloutApplyRequestFixture.model_id !== "model-from-selection") {
  throw new Error("rollout apply request should fall back to the selected model id");
}
if (deploymentIntentModule.buildRolloutRollbackRequest().reason !== "operator requested rollback from Mission Package Workbench") {
  throw new Error("rollout rollback request should preserve mission workbench rollback reason");
}
if (deploymentIntentModule.buildRolloutPlanAdvanceRequest().actor !== "operator:mission-package-workbench") {
  throw new Error("rollout plan advance request should use the mission package workbench actor");
}
if (deploymentIntentModule.buildRolloutPlanPauseRequest().reason !== "operator paused rollout plan from Mission Package Workbench") {
  throw new Error("rollout plan pause request should preserve pause reason");
}
if (deploymentIntentModule.buildRolloutPlanResumeRequest().reason !== "operator resumed rollout plan from Mission Package Workbench") {
  throw new Error("rollout plan resume request should preserve resume reason");
}
const bundledMissionYamlImport = await build({
  bundle: true,
  entryPoints: [missionYamlImportPath],
  format: "esm",
  platform: "node",
  target: "es2020",
  write: false
});
const missionYamlImportModule = await import(
  `data:text/javascript;base64,${Buffer.from(bundledMissionYamlImport.outputFiles[0].text).toString("base64")}`
);
const missionYamlImportFixture = missionYamlImportModule.buildMissionYamlImportResult({
  currentDraft: missionDraftFixture,
  devices: [{ device_id: "edge-thermal-01" }],
  fileName: "thermal-mission.yaml",
  models: [modelFixture],
  runtimeTargets: [{ runtime_target_id: "temms-jetson-tensorrt" }],
  yaml: `
mission:
  goal: Track thermal vehicles while disconnected.
  sensor: camera.thermal
  slot: thermal
selection:
  package_id: pkg-vision-models-20240115
  device_id: edge-thermal-01
  runtime_target_id: temms-jetson-tensorrt
model_handling:
  switch_policy: condition_and_confidence
ddil:
  mode: queue_signed_intents
`
});
if (
  missionYamlImportFixture.selectedModelId !== modelFixture.id ||
  missionYamlImportFixture.selectedDeviceId !== "edge-thermal-01" ||
  missionYamlImportFixture.selectedRuntimeId !== "temms-jetson-tensorrt"
) {
  throw new Error("mission YAML import should apply model, edge, and runtime selections from YAML hints");
}
if (
  missionYamlImportFixture.draft.slot !== "thermal" ||
  missionYamlImportFixture.draft.sensor !== "camera.thermal" ||
  !missionYamlImportFixture.toastDetail.includes("Selected model model-yolov8-lowlight-001, edge edge-thermal-01, runtime temms-jetson-tensorrt from the spec.")
) {
  throw new Error("mission YAML import should populate mission fields and selected-context toast detail");
}
const missionYamlAdoptionFixture = missionYamlImportModule.missionYamlImportAdoption(
  missionYamlImportFixture
);
if (
  missionYamlAdoptionFixture.stage !== "mission" ||
  missionYamlAdoptionFixture.packagePlan !== undefined ||
  missionYamlAdoptionFixture.packageHandoff !== undefined
) {
  throw new Error("mission YAML adoption should return to Mission and clear package planning state");
}
if (
  missionYamlAdoptionFixture.selectedModelId !== modelFixture.id ||
  missionYamlAdoptionFixture.selectedDeviceId !== "edge-thermal-01" ||
  missionYamlAdoptionFixture.selectedRuntimeId !== "temms-jetson-tensorrt"
) {
  throw new Error("mission YAML adoption should preserve selected model, edge, and runtime hints");
}
if (
  missionYamlAdoptionFixture.draft.slot !== "thermal" ||
  missionYamlAdoptionFixture.draft.sensor !== "camera.thermal" ||
  missionYamlAdoptionFixture.draft.switchPolicy !== "condition_and_confidence"
) {
  throw new Error("mission YAML adoption should preserve mission sensor and model handling fields");
}
if (
  missionYamlAdoptionFixture.toast.title !== "Mission YAML imported" ||
  missionYamlAdoptionFixture.toast.detail !== missionYamlImportFixture.toastDetail
) {
  throw new Error("mission YAML adoption should expose an operator-facing import toast");
}
const missionYamlImportErrorNoticeFixture = missionYamlImportModule.missionYamlImportErrorNotice(
  "bad-mission.yaml"
);
if (
  missionYamlImportErrorNoticeFixture.tone !== "error" ||
  missionYamlImportErrorNoticeFixture.title !== "Mission YAML import failed" ||
  missionYamlImportErrorNoticeFixture.detail !== "bad-mission.yaml could not be read by the browser."
) {
  throw new Error("mission YAML import error notice should name the unreadable mission spec file");
}
const missingMissionYamlImportFixture = missionYamlImportModule.buildMissionYamlImportResult({
  currentDraft: missionDraftFixture,
  devices: [],
  fileName: "missing-selection.yaml",
  models: [modelFixture],
  runtimeTargets: [],
  yaml: `
selection:
  model_id: missing-model
  device_id: missing-edge
  runtime_target_id: missing-runtime
`
});
if (!missingMissionYamlImportFixture.toastDetail.includes("Unmatched hints: model missing-model, edge missing-edge, runtime missing-runtime.")) {
  throw new Error("mission YAML import should report unmatched model, edge, and runtime hints");
}
const scopedRollouts = deploymentIntentModule.missionRolloutsForSelection({
  missionSlot: "thermal",
  model: modelFixture,
  rollouts: [
    { model_id: modelFixture.id, package_id: modelFixture.packageId, rollout_id: "rollout-thermal", slot: "thermal" },
    { model_id: modelFixture.id, package_id: modelFixture.packageId, rollout_id: "rollout-vision", slot: "vision" },
    { model_id: modelFixture.id, package_id: modelFixture.packageId, rollout_id: "rollout-legacy" },
    { model_id: "other-model", package_id: modelFixture.packageId, rollout_id: "rollout-other", slot: "thermal" }
  ]
});
const scopedRolloutIds = scopedRollouts.map((rollout) => rollout.rollout_id).join(",");
if (scopedRolloutIds !== "rollout-thermal,rollout-legacy") {
  throw new Error(`mission rollouts should be scoped to selected model and mission slot: ${scopedRolloutIds}`);
}
const scopedPlans = deploymentIntentModule.missionRolloutPlansForSelection({
  missionSlot: "thermal",
  model: modelFixture,
  plans: [
    { model_id: modelFixture.id, package_id: modelFixture.packageId, plan_id: "plan-thermal", slot: "thermal" },
    { model_id: modelFixture.id, package_id: modelFixture.packageId, plan_id: "plan-vision", slot: "vision" },
    { package_id: modelFixture.packageId, plan_id: "plan-package-wide", slot: "thermal" },
    { model_id: "other-model", package_id: modelFixture.packageId, plan_id: "plan-other", slot: "thermal" }
  ]
});
const scopedPlanIds = scopedPlans.map((plan) => plan.plan_id).join(",");
if (scopedPlanIds !== "plan-thermal,plan-package-wide") {
  throw new Error(`mission rollout plans should be scoped to selected model/package and mission slot: ${scopedPlanIds}`);
}
const defaultSlotRollouts = deploymentIntentModule.missionRolloutsForSelection({
  missionSlot: "",
  model: undefined,
  rollouts: [
    { rollout_id: "rollout-vision", slot: "vision" },
    { rollout_id: "rollout-thermal", slot: "thermal" },
    { rollout_id: "rollout-legacy" }
  ]
});
if (defaultSlotRollouts.map((rollout) => rollout.rollout_id).join(",") !== "rollout-vision,rollout-legacy") {
  throw new Error("mission rollouts should default blank mission slots to vision while retaining legacy records");
}
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
const deploymentIntentQueueActionFixture = deploymentIntentModule.deploymentIntentQueueAction({
  device: { device_id: "edge-thermal-1" },
  draft: { ...missionDraftFixture, sensor: "camera.thermal", slot: "thermal" },
  model: modelFixture,
  runtime: { runtime_target_id: "temms-rpi5-tflite" }
});
if (
  deploymentIntentQueueActionFixture.title !== "Queue DDIL deployment intent" ||
  deploymentIntentQueueActionFixture.request.actor !== "operator:mission-package-workbench" ||
  deploymentIntentQueueActionFixture.request.model_id !== "model-yolov8-lowlight-001" ||
  deploymentIntentQueueActionFixture.request.device_id !== "edge-thermal-1" ||
  deploymentIntentQueueActionFixture.request.runtime_target_id !== "temms-rpi5-tflite" ||
  deploymentIntentQueueActionFixture.request.slot !== "thermal"
) {
  throw new Error("deployment intent queue action should bind the selected mission edge path");
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
const missionPackagePlanResponseFixture = {
  package_identity: { package_identity_sha256: "a".repeat(64) },
  schema_version: "temms-edge-mission-package/v1"
};
const missionPackageContextInvalidationFixture = missionPackageModule.missionPackageContextInvalidation();
if (
  missionPackageContextInvalidationFixture.plan !== undefined ||
  missionPackageContextInvalidationFixture.handoff !== undefined
) {
  throw new Error("mission package context invalidation should clear stale plan and download handoff");
}
const missionPackagePlanAdoptionFixture = missionPackageModule.missionPackagePlanAdoption(
  missionPackagePlanResponseFixture
);
if (
  missionPackagePlanAdoptionFixture.plan !== missionPackagePlanResponseFixture ||
  missionPackagePlanAdoptionFixture.preview !== missionPackagePlanResponseFixture ||
  missionPackagePlanAdoptionFixture.handoff !== undefined ||
  missionPackagePlanAdoptionFixture.fileName !== undefined
) {
  throw new Error("mission package plan adoption should retain plan payload and clear download handoff");
}
const missionPackageDownloadHandoffFixture = {
  deploymentIntentSha256: "b".repeat(64),
  edgeHandoffSha256: "c".repeat(64),
  fileName: "temms-mission-package.json",
  missionContractSha256: "d".repeat(64),
  missionSha256: "e".repeat(64),
  packageIdentitySha256: "f".repeat(64),
  payloadSha256: "1".repeat(64),
  runtimeCapabilityLockSha256: "2".repeat(64),
  runtimePlanSha256: "3".repeat(64)
};
const missionPackageDownloadAdoptionFixture = missionPackageModule.missionPackageDownloadAdoption({
  fileName: "temms-mission-package.json",
  handoff: missionPackageDownloadHandoffFixture,
  payload: missionPackagePlanResponseFixture
});
if (
  missionPackageDownloadAdoptionFixture.fileName !== "temms-mission-package.json" ||
  missionPackageDownloadAdoptionFixture.plan !== missionPackagePlanResponseFixture ||
  missionPackageDownloadAdoptionFixture.handoff !== missionPackageDownloadHandoffFixture ||
  missionPackageDownloadAdoptionFixture.preview.package !== missionPackagePlanResponseFixture ||
  missionPackageDownloadAdoptionFixture.preview.handoff !== missionPackageDownloadHandoffFixture
) {
  throw new Error("mission package download adoption should retain file, handoff, plan, and preview payload");
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
const stageablePackageStatusFixture = {
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
};
const stageablePackageStatus = missionPackageModule.buildMissionPackageStageStatus({
  handoff: undefined,
  manifest: stageablePackageStatusFixture,
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
const missingIntentStageBlocker = missionPackageModule.missionPackageStageBlocker({
  manifest: manifestFixture,
  stageStatus: {
    detail: "deploy intent missing",
    downloaded: false,
    gateStatus: "",
    planned: false,
    stageable: false,
    tone: "warn",
    value: "deploy intent missing"
  }
});
if (
  missingIntentStageBlocker?.title !== "Plan package first" ||
  missingIntentStageBlocker?.detail !== "Stage rollout uses the mission package deployment intent."
) {
  throw new Error("mission package stage blocker should require deployment intent before staging");
}
const failedProofStageBlocker = missionPackageModule.missionPackageStageBlocker({
  manifest: stageablePackageStatusFixture,
  stageStatus: {
    detail: "proof gate failed",
    downloaded: false,
    gateStatus: "failed",
    planned: true,
    stageable: false,
    tone: "bad",
    value: "proof gate failed"
  }
});
if (
  failedProofStageBlocker?.title !== "Proof gate blocks staging" ||
  !failedProofStageBlocker.detail.includes("resolving runtime readiness blockers")
) {
  throw new Error("mission package stage blocker should explain failed proof gates");
}
if (
  missionPackageModule.missionPackageStageBlocker({
    manifest: stageablePackageStatusFixture,
    stageStatus: stageablePackageStatus
  }) !== undefined
) {
  throw new Error("mission package stage blocker should be empty when package staging is allowed");
}
const missingIntentStagePlan = missionPackageModule.missionPackageStagePlan({
  manifest: manifestFixture,
  stageStatus: {
    detail: "deploy intent missing",
    downloaded: false,
    gateStatus: "",
    planned: false,
    stageable: false,
    tone: "warn",
    value: "deploy intent missing"
  }
});
if (
  missingIntentStagePlan.blocker?.title !== "Plan package first" ||
  missingIntentStagePlan.blockedStage !== "package" ||
  missingIntentStagePlan.successStage !== "deploy" ||
  missingIntentStagePlan.successWorkflowTarget !== "rollouts"
) {
  throw new Error("mission package stage plan should route blocked staging back to Package Handoff");
}
const stageablePackagePlan = missionPackageModule.missionPackageStagePlan({
  manifest: stageablePackageStatusFixture,
  stageStatus: stageablePackageStatus
});
if (
  stageablePackagePlan.blocker !== undefined ||
  stageablePackagePlan.runTitle !== "Stage package rollout" ||
  stageablePackagePlan.successStage !== "deploy" ||
  stageablePackagePlan.successWorkflowTarget !== "rollouts"
) {
  throw new Error("mission package stage plan should route successful staging to Edge Deploy rollouts");
}
const stageRequestFixture = missionPackageModule.buildMissionPackageStageRequest(stageablePackageStatusFixture);
if (
  stageRequestFixture.actor !== "operator:mission-package-workbench" ||
  stageRequestFixture.reason !== "mission package deployment handoff" ||
  stageRequestFixture.rollout_id !== "rollout-model-yolov8-lowlight-001-temms-rpi5-tflite-edge-rpi5" ||
  stageRequestFixture.mission_package !== stageablePackageStatusFixture
) {
  throw new Error("mission package stage request should preserve actor, reason, rollout id, and manifest payload");
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
const signalFixture = missionWorkflowModule.buildMissionWorkflowSignals({
  missionDraft: missionDraftFixture,
  missionPackageStageStatus: readyStageOptions.missionPackageStageStatus,
  runtimeFitDisplay: readyStageOptions.runtimeFitDisplay,
  selectedDevice: { device_id: "edge-rpi5" },
  selectedModel: modelFixture,
  selectedRuntime: readyStageOptions.selectedRuntime
});
const expectedSignalOrder = "Mission>Model>Runtime>Handling>Package";
const signalOrder = signalFixture.map((signal) => signal.label).join(">");
if (signalOrder !== expectedSignalOrder) {
  throw new Error(`mission workflow signal order mismatch: ${signalOrder}`);
}
if (signalFixture.find((signal) => signal.label === "Runtime")?.detail !== "edge-rpi5 / 98 optimal") {
  throw new Error("mission workflow runtime signal should bind selected device and runtime fit");
}
if (signalFixture.find((signal) => signal.label === "Handling")?.detail !== "fallback model-fallback / queue signed intents") {
  throw new Error("mission workflow handling signal should bind fallback model and DDIL policy");
}
const selectedRuntimeFitTileFixture = missionWorkflowModule.buildRuntimeFitTileSummary({
  compatibleTargets: 2,
  runtimeFitDisplay: readyStageOptions.runtimeFitDisplay,
  runtimeTargetCount: 5,
  selectedModel: modelFixture,
  selectedRuntime: { runtime_target_id: "temms-rpi5-tflite" }
});
if (selectedRuntimeFitTileFixture.value !== "98 optimal" || selectedRuntimeFitTileFixture.detail !== "temms-rpi5-tflite; 2/5 eligible") {
  throw new Error("mission workflow runtime fit tile should summarize selected model runtime fit");
}
const unselectedRuntimeFitTileFixture = missionWorkflowModule.buildRuntimeFitTileSummary({
  compatibleTargets: 3,
  runtimeFitDisplay: readyStageOptions.runtimeFitDisplay,
  runtimeTargetCount: 5,
  selectedModel: undefined,
  selectedRuntime: undefined
});
if (unselectedRuntimeFitTileFixture.value !== 3 || unselectedRuntimeFitTileFixture.detail !== "runtime targets available") {
  throw new Error("mission workflow runtime fit tile should summarize available targets before model selection");
}
const readinessContextSelectionFixture = missionWorkflowModule.readinessActionSelection({
  kind: "select_context",
  refs: {
    device_id: "edge-rpi5",
    model_id: "model-yolov8-lowlight-001",
    runtime_target_id: "temms-rpi5-tflite"
  }
});
if (
  readinessContextSelectionFixture.modelId !== "model-yolov8-lowlight-001" ||
  readinessContextSelectionFixture.deviceId !== "edge-rpi5" ||
  readinessContextSelectionFixture.runtimeTargetId !== "temms-rpi5-tflite"
) {
  throw new Error("mission workflow readiness action selection should extract model, device, and runtime refs");
}
const runtimeOnlySelectionFixture = missionWorkflowModule.readinessActionSelection({
  kind: "select_runtime_target",
  refs: { runtime_target_id: "temms-jetson-ort-trt" }
});
if (
  runtimeOnlySelectionFixture.modelId !== "" ||
  runtimeOnlySelectionFixture.deviceId !== "" ||
  runtimeOnlySelectionFixture.runtimeTargetId !== "temms-jetson-ort-trt"
) {
  throw new Error("mission workflow readiness action selection should support runtime-only refs");
}
const ignoredSelectionFixture = missionWorkflowModule.readinessActionSelection({
  kind: "record_benchmark",
  refs: {
    device_id: "edge-rpi5",
    model_id: "model-yolov8-lowlight-001",
    runtime_target_id: "temms-rpi5-tflite"
  }
});
if (
  ignoredSelectionFixture.modelId ||
  ignoredSelectionFixture.deviceId ||
  ignoredSelectionFixture.runtimeTargetId
) {
  throw new Error("mission workflow readiness action selection should ignore non-selection readiness actions");
}
const readinessActionPlanFixture = missionWorkflowModule.readinessActionPlan({
  command: { method: "POST", path: "/v1/control/sync" },
  kind: "select_context",
  label: "Select measured runtime path",
  refs: {
    device_id: "edge-rpi5",
    model_id: "model-yolov8-lowlight-001",
    runtime_target_id: "temms-rpi5-tflite"
  }
});
if (
  readinessActionPlanFixture.command?.method !== "POST" ||
  readinessActionPlanFixture.command?.path !== "/v1/control/sync" ||
  readinessActionPlanFixture.selection.modelId !== "model-yolov8-lowlight-001" ||
  readinessActionPlanFixture.selection.deviceId !== "edge-rpi5" ||
  readinessActionPlanFixture.selection.runtimeTargetId !== "temms-rpi5-tflite" ||
  readinessActionPlanFixture.focus.stage !== "deploy" ||
  readinessActionPlanFixture.focus.workflowTarget !== "deployment"
) {
  throw new Error("mission workflow readiness action plan should compose command, selection, and focus");
}
const deploymentFocusFixture = missionWorkflowModule.readinessActionFocus({
  kind: "select_context",
  label: "Select measured runtime path",
  refs: {
    device_id: "edge-rpi5",
    model_id: "model-yolov8-lowlight-001",
    runtime_target_id: "temms-rpi5-tflite"
  }
});
if (
  deploymentFocusFixture.stage !== "deploy" ||
  deploymentFocusFixture.workflowTarget !== "deployment" ||
  deploymentFocusFixture.title !== "Select measured runtime path" ||
  deploymentFocusFixture.detail !==
    "Deployment path is focused for model-yolov8-lowlight-001 on edge-rpi5 via temms-rpi5-tflite."
) {
  throw new Error("mission workflow readiness action focus should route selected context actions to deployment");
}
const deploymentFocusNoticeFixture = missionWorkflowModule.readinessActionFocusNotice(
  deploymentFocusFixture
);
if (
  deploymentFocusNoticeFixture.tone !== "success" ||
  deploymentFocusNoticeFixture.title !== "Select measured runtime path" ||
  deploymentFocusNoticeFixture.detail !== deploymentFocusFixture.detail
) {
  throw new Error("mission workflow readiness action focus notice should explain the focused mission path");
}
const evidenceFocusFixture = missionWorkflowModule.readinessActionFocus({
  kind: "export_replay",
  label: "Export replay evidence",
  refs: { slot: "thermal" }
});
if (
  evidenceFocusFixture.stage !== "field" ||
  evidenceFocusFixture.workflowTarget !== "evidence" ||
  evidenceFocusFixture.detail !== "Mission proof is focused for slot thermal."
) {
  throw new Error("mission workflow readiness action focus should route evidence actions to Field Ops evidence");
}
const edgeExecutionPlanFixture = missionWorkflowModule.readinessCommandExecutionPlan(
  { kind: "record_benchmark", label: "Record benchmark" },
  { method: "POST", path: "/v1/benchmarks", requires_edge_execution: true }
);
if (
  edgeExecutionPlanFixture.requiresEdgeExecution !== true ||
  edgeExecutionPlanFixture.edgeInstructionTitle !== "Run this on the edge node" ||
  !edgeExecutionPlanFixture.edgeInstructionDetail.includes("actual runtime") ||
  edgeExecutionPlanFixture.reconcileAfterRun !== false ||
  edgeExecutionPlanFixture.shouldRefreshAfterRun !== false
) {
  throw new Error("mission workflow readiness command execution should hold edge-required commands for edge execution");
}
const edgeExecutionNoticeFixture = missionWorkflowModule.readinessCommandEdgeExecutionNotice(
  edgeExecutionPlanFixture
);
if (
  edgeExecutionNoticeFixture?.tone !== "info" ||
  edgeExecutionNoticeFixture?.title !== "Run this on the edge node" ||
  !edgeExecutionNoticeFixture?.detail.includes("actual runtime")
) {
  throw new Error("mission workflow readiness edge execution notice should explain where to run the command");
}
const syncExecutionPlanFixture = missionWorkflowModule.readinessCommandExecutionPlan(
  { kind: "sync_pending", label: "Sync pending operations" },
  { method: "POST", path: "/v1/control/sync" }
);
if (
  syncExecutionPlanFixture.requiresEdgeExecution !== false ||
  syncExecutionPlanFixture.reconcileAfterRun !== true ||
  syncExecutionPlanFixture.shouldRefreshAfterRun !== false ||
  syncExecutionPlanFixture.runTitle !== "Run Sync pending operations"
) {
  throw new Error("mission workflow readiness command execution should reconcile Hub state after sync commands");
}
const normalExecutionPlanFixture = missionWorkflowModule.readinessCommandExecutionPlan(
  { kind: "approve_rollout", label: "Approve rollout" },
  { method: "POST", path: "/v1/hub/rollouts/rollout-1/approve" }
);
if (
  normalExecutionPlanFixture.requiresEdgeExecution !== false ||
  normalExecutionPlanFixture.reconcileAfterRun !== false ||
  normalExecutionPlanFixture.shouldRefreshAfterRun !== true ||
  normalExecutionPlanFixture.edgeInstructionDetail ||
  missionWorkflowModule.readinessCommandEdgeExecutionNotice(normalExecutionPlanFixture) !== undefined
) {
  throw new Error("mission workflow readiness command execution should refresh after normal daemon commands");
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
