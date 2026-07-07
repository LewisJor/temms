import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { webcrypto } from "node:crypto";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { transform } from "esbuild";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const uiRoot = resolve(scriptDir, "..");
const repoRoot = resolve(uiRoot, "..");
const indexPath = join(uiRoot, "index.html");
const sourcePath = join(uiRoot, "src", "App.tsx");
const workbenchFlowPath = join(uiRoot, "src", "components", "workbench-flow.tsx");
const apiPath = join(uiRoot, "src", "api.ts");
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
const workbenchFlowSource = readFileSync(workbenchFlowPath, "utf8");
const missionPackageSource = readFileSync(missionPackagePath, "utf8");
const missionWorkflowSource = readFileSync(missionWorkflowPath, "utf8");
const workbenchSource = `${source}\n${workbenchFlowSource}\n${missionPackageSource}\n${missionWorkflowSource}`;
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
  "Stage rollout unlocks only after the mission package proof gate passes.",
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
  "Edge execution command",
  "Run on the edge node to refresh heartbeat",
  "Ranked on-device capability proof",
  "runtime_retarget_workbench_previous_selected_runtime_target_id",
  "Select runtime target",
  "Target the model to the edge runtime"
].forEach((needle) => assertContains("Hub workbench sources", workbenchSource, needle));

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
