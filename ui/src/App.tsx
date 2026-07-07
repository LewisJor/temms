import {
  Activity,
  ArrowLeft,
  BadgeCheck,
  Box,
  CheckCircle2,
  Clipboard,
  Cpu,
  Database,
  Download,
  FileCheck2,
  GitBranch,
  KeyRound,
  RefreshCw,
  Rocket,
  ShieldCheck,
  Terminal,
  UploadCloud
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  controlApi,
  downloadEdgeRuntimeProof,
  downloadMissionPackage,
  executeReadinessCommand,
  hubApi,
  loadEdgeRuntimeProof,
  loadReadiness,
  loadSnapshot,
  planMissionPackage,
  stageMissionPackage
} from "./api";
import type {
  EdgeProofDownloadHandoff,
  MissionPackageDownloadHandoff,
  ReadinessQuery
} from "./api";
import { Badge, Button, CapabilityMetric, PreviewPanel, Submit, ToastView } from "./components/ui";
import {
  EmptyState,
  EvidenceSummaryRow,
  MissionPhaseRow,
  RolloutPlanRow,
  RolloutRow,
  TargetRow
} from "./components/deploy-lists";
import {
  EdgePackagePlanPanel,
  MissionPackageDownloadHandoffCard
} from "./components/package-handoff";
import {
  ReadinessCommandPanel,
  ReadinessVerdictPanel
} from "./components/readiness-panels";
import {
  HandlingPolicyPanel,
  MissionDesignPanel
} from "./components/mission-stages";
import { ModelPlanStage } from "./components/model-plan";
import { MissionWorkflowCockpit, StatusTile } from "./components/workbench-flow";
import {
  compactDate,
  csv,
  deviceId,
  displayGateState,
  errorToast,
  fieldValue,
  isChecked,
  nextPromotion,
  packageId,
  planId,
  rolloutId,
  runtimeTargetId,
  saveToken,
  storedToken
} from "./lib/hub-format";
import {
  asRecord,
  booleanOf,
  latestByTime,
  numberOf,
  stringOf,
  stringsOf
} from "./lib/json";
import {
  EDGE_PROOF_COMPONENT_DIGEST_TARGETS,
  canonicalJsonStringify,
  isSha256Digest,
  normalizeSha256Digest,
  sha256Hex,
  shortProofDigest
} from "./lib/proof-hash";
import {
  defaultMissionDraft,
  missionDraftFromYaml,
  missionSelectionFromYaml,
  type MissionDraft
} from "./lib/mission-spec";
import {
  buildMissionPackagePlanRequest,
  buildMissionPackageManifest,
  buildMissionPackageStageStatus,
  missionPackageRolloutId
} from "./lib/mission-package";
import {
  buildHubStages,
  buildReadinessVerdict,
  ddilStatusDetail,
  edgeReadinessCommandReason,
  hubStageForWorkflowTarget,
  hubStageRunbookFor,
  readinessActionContext,
  readinessCommand,
  readinessCommandFromValue,
  workflowTargetForReadinessAction,
  workflowTargetLabel
} from "./lib/mission-workflow";
import {
  benchmarkFreshness,
  deviceResourceSnapshot,
  edgeRuntimeCapabilityFit,
  formatAge,
  formatArtifactSizeMb,
  formatBenchmark,
  formatBenchmarkFreshness,
  formatBenchmarkTarget,
  formatMb,
  formatMetricNumber,
  formatPerformanceSlo,
  formatPower,
  formatResourceEnvelope,
  formatTemperature,
  formatThroughput,
  isSigned,
  modelsForPackage,
  performanceSloDetail,
  performanceSloLabel,
  performanceSloTone,
  providerDisplayForModel,
  resourceEnvelopeCapabilityFit,
  runtimeForModel,
  runtimeInventoryDetail,
  runtimeInventoryLabel,
  runtimeInventoryTone,
  runtimeTargetCapabilityDetail,
  runtimeTargetInventoryConstraints,
  runtimeTargetInventoryFailures,
  runtimeValidationForModel,
  targetSupportsModel,
  withBenchmarkEvidence
} from "./lib/runtime-fit";
import type {
  DeploymentReadiness,
  Device,
  EdgeRecommendation,
  EvidenceSummary,
  EvidenceExportMode,
  HubSnapshot,
  JsonObject,
  MissionReplay,
  Preview,
  RuntimeValidation,
  RuntimeTarget,
  Toast
} from "./types";
import type {
  EdgeMissionMetric,
  EdgeProofComponentDigestStatus,
  EdgeProofTraceStatus,
  EdgeProofWorkflow,
  EdgeRuntimeFit,
  EdgeRuntimeMission,
  GateTone,
  HubStage,
  MissionWorkflowSignal,
  ModelRecord,
  ReadinessGate,
  ReadinessGateAction,
  ReadinessVerdict,
  RuntimeFitDisplay,
  RuntimeRemediationCommand,
  RuntimeRemediationContext,
  RuntimeRepairProof,
  RuntimeWorkbenchRow,
  RuntimeWorkbenchTraceMetric,
  WorkflowTarget
} from "./lib/workbench-types";

const emptySnapshot: HubSnapshot = {
  devices: [],
  packages: [],
  runtimeTargets: [],
  rollouts: [],
  rolloutPlans: [],
  runtimeValidations: [],
  benchmarks: [],
  evidenceBundles: []
};

const EDGE_PROOF_MAX_AGE_SECONDS = 900;

export function App(): JSX.Element {
  const [snapshot, setSnapshot] = useState<HubSnapshot>(emptySnapshot);
  const [token, setToken] = useState(storedToken);
  const [loading, setLoading] = useState(false);
  const [hasLoadedSnapshot, setHasLoadedSnapshot] = useState(false);
  const [toast, setToast] = useState<Toast | undefined>();
  const [preview, setPreview] = useState<Preview | undefined>();
  const [missionDraft, setMissionDraft] = useState<MissionDraft>(defaultMissionDraft);
  const [selectedModelId, setSelectedModelId] = useState("");
  const [selectedDeviceId, setSelectedDeviceId] = useState("");
  const [selectedRuntimeId, setSelectedRuntimeId] = useState("");
  const [contextReadiness, setContextReadiness] = useState<DeploymentReadiness | undefined>();
  const [lastEdgeProof, setLastEdgeProof] = useState<JsonObject | undefined>();
  const [lastEdgeProofHandoff, setLastEdgeProofHandoff] = useState<EdgeProofDownloadHandoff | undefined>();
  const [missionPackagePlan, setMissionPackagePlan] = useState<JsonObject | undefined>();
  const [lastMissionPackageHandoff, setLastMissionPackageHandoff] = useState<MissionPackageDownloadHandoff | undefined>();
  const [readinessRefreshVersion, setReadinessRefreshVersion] = useState(0);
  const [focusedWorkflow, setFocusedWorkflow] = useState<WorkflowTarget | undefined>();
  const [activeHubStage, setActiveHubStage] = useState<HubStage>("mission");
  const [pendingReadinessAction, setPendingReadinessAction] = useState<ReadinessGateAction | undefined>();
  const stageFlowRef = useRef<HTMLDivElement>(null);
  const ddilWorkflowRef = useRef<HTMLElement>(null);
  const modelWorkflowRef = useRef<HTMLElement>(null);
  const deploymentWorkflowRef = useRef<HTMLElement>(null);
  const plansWorkflowRef = useRef<HTMLElement>(null);
  const rolloutsWorkflowRef = useRef<HTMLElement>(null);
  const evidenceWorkflowRef = useRef<HTMLElement>(null);
  const assetsWorkflowRef = useRef<HTMLDetailsElement>(null);

  const refresh = useCallback(async (options?: { quiet?: boolean }) => {
    setLoading(true);
    try {
      const nextSnapshot = await loadSnapshot(token);
      setSnapshot(nextSnapshot);
      setHasLoadedSnapshot(true);
      setReadinessRefreshVersion((version) => version + 1);
      if (!options?.quiet) setToast({ tone: "success", title: "Hub refreshed" });
    } catch (error) {
      setToast(errorToast("Refresh failed", error));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh({ quiet: true });
  }, [refresh]);

  const packageModels = useMemo(() => snapshot.packages.flatMap(modelsForPackage), [snapshot.packages]);
  const models = useMemo(
    () => withBenchmarkEvidence(packageModels, snapshot.benchmarks),
    [packageModels, snapshot.benchmarks]
  );
  const selectedModel = models.find((model) => model.id === selectedModelId) ?? models[0];
  const selectedPackage = selectedModel
    ? snapshot.packages.find((pkg) => packageId(pkg) === selectedModel.packageId)
    : undefined;
  const selectedDevice =
    snapshot.devices.find((device) => deviceId(device) === selectedDeviceId) ?? snapshot.devices[0];
  const selectedRuntime =
    snapshot.runtimeTargets.find((target) => runtimeTargetId(target) === selectedRuntimeId) ??
    runtimeForModel(snapshot.runtimeTargets, selectedModel);
  const activeSlots = Array.isArray(snapshot.evidenceSummary?.active_slots)
    ? snapshot.evidenceSummary.active_slots.map(asRecord)
    : [];
  const activeSlot = activeSlots[0];
  const missionRollouts = selectedModel
    ? snapshot.rollouts.filter(
        (rollout) =>
          rollout.package_id === selectedModel.packageId && rollout.model_id === selectedModel.id
      )
    : snapshot.rollouts;
  const missionRolloutPlans = selectedModel
    ? snapshot.rolloutPlans.filter(
        (plan) =>
          plan.package_id === selectedModel.packageId &&
          (!plan.model_id || plan.model_id === selectedModel.id)
      )
    : snapshot.rolloutPlans;
  const latestRollout = latestByTime(missionRollouts);
  const pendingApprovals = missionRollouts.filter(
    (rollout) => rollout.approval_required && !rollout.approval?.approved
  ).length;
  const rolloutDetail = pendingApprovals
    ? `${pendingApprovals} waiting approval`
    : latestRollout?.state ?? "for selected model";
  const releasedPackages = snapshot.packages.filter((pkg) => pkg.promotion?.state === "released").length;
  const signedPackages = snapshot.packages.filter((pkg) => isSigned(pkg)).length;
  const compatibleTargets = selectedModel
    ? snapshot.runtimeTargets.filter((target) => targetSupportsModel(target, selectedModel)).length
    : snapshot.runtimeTargets.length;
  const modelValidationCount = selectedModel
    ? snapshot.runtimeValidations.filter((validation) => validation.package_id === selectedModel.packageId).length
    : snapshot.runtimeValidations.length;
  const selectedRuntimeValidation = selectedModel
    ? runtimeValidationForModel(selectedModel, selectedRuntime, snapshot.runtimeValidations)
    : undefined;
  const edgeRuntimeFit = edgeRuntimeCapabilityFit(
    selectedModel,
    selectedDevice,
    selectedRuntime,
    selectedRuntimeValidation
  );
  const edgeRecommendations = snapshot.compatibilityMatrix?.recommendations ?? [];
  const resourceEnvelopeFit = resourceEnvelopeCapabilityFit(selectedModel, selectedDevice);
  const proofEvents = numberOf(asRecord(snapshot.evidenceSummary?.counts).timeline_entries) ?? 0;
  const missionPhases = Array.isArray(snapshot.missionReplay?.phases) ? snapshot.missionReplay.phases : [];
  const missionOutcome = asRecord(snapshot.missionReplay?.outcome);
  const completedMissionPhases =
    numberOf(missionOutcome.completed_phases) ??
    missionPhases.filter((phase) => phase.status === "complete").length;
  const incompleteMissionPhases = stringsOf(missionOutcome.incomplete_phases);
  const missionPhaseTotal = missionPhases.length;
  const missionProofComplete = missionPhaseTotal > 0 && incompleteMissionPhases.length === 0;
  const signedEvidenceImports = numberOf(asRecord(snapshot.evidenceSummary?.trust).signed_package_imports) ?? 0;
  const evidenceRuntime = asRecord(snapshot.evidenceSummary?.runtime);
  const deploymentState = asRecord(evidenceRuntime.deployment_state);
  const offlineMode = evidenceRuntime.offline_mode === true;
  const pendingOperations = numberOf(evidenceRuntime.pending_operations_count) ?? 0;
  const pendingOperationTypes = stringsOf(evidenceRuntime.pending_operation_types);
  const pendingOperationLedger = Array.isArray(evidenceRuntime.pending_operations)
    ? evidenceRuntime.pending_operations.map(asRecord)
    : [];
  const runtimeRepairProof = useMemo(
    () =>
      latestRuntimeRepairProofFor({
        evidenceSummary: snapshot.evidenceSummary,
        missionReplay: snapshot.missionReplay,
        pendingOperationLedger
      }),
    [pendingOperationLedger, snapshot.evidenceSummary, snapshot.missionReplay]
  );
  const pendingOperationVerification = asRecord(evidenceRuntime.pending_operation_verification);
  const verifiedPendingOperations = numberOf(pendingOperationVerification.verified) ?? 0;
  const invalidPendingOperations = numberOf(pendingOperationVerification.invalid) ?? 0;
  const pendingOperationPreflight = asRecord(evidenceRuntime.pending_operation_preflight);
  const replayReadyOperations = numberOf(pendingOperationPreflight.ready) ?? 0;
  const replayBlockedOperations = numberOf(pendingOperationPreflight.blocked) ?? 0;
  const supersededOperations = numberOf(pendingOperationPreflight.superseded) ?? 0;
  const runtimeOptimizationAdvisories =
    numberOf(pendingOperationPreflight.optimization_advisories) ?? 0;
  const totalDeadLetteredOperations = numberOf(evidenceRuntime.pending_operation_dead_letters_count) ?? 0;
  const deadLetteredOperations =
    numberOf(evidenceRuntime.pending_operation_dead_letters_unresolved_count) ?? totalDeadLetteredOperations;
  const allDeadLetteredOperations = Array.isArray(evidenceRuntime.pending_operation_dead_letters)
    ? evidenceRuntime.pending_operation_dead_letters.map(asRecord)
    : [];
  const deadLetteredOperationLedger = allDeadLetteredOperations.filter(
    (operation) => operation.acknowledged !== true && operation.requeued !== true
  );
  const deploymentStateName = stringOf(deploymentState.state, "UNKNOWN");
  const deploymentReason = stringOf(
    deploymentState.reason,
    pendingOperationTypes.length ? pendingOperationTypes.join(", ") : "reconciled"
  );
  const activeModelId = stringOf(activeSlot?.active_model, "");
  const deploymentDetail =
    deploymentStateName === "READY" && activeModelId ? `activated ${activeModelId}` : deploymentReason;
  const connectivityState = offlineMode ? "offline" : "online";
  const latestEvents = prioritizedEvidenceEvents(
    snapshot.evidenceSummary?.timeline,
    activeModelId || selectedModel?.id || ""
  );
  const evidenceValue = snapshot.evidenceBundles.length || proofEvents;
  const evidenceDetail = missionPhaseTotal
    ? `${completedMissionPhases}/${missionPhaseTotal} phases complete`
    : proofEvents
      ? `${proofEvents} proof events`
      : `${snapshot.benchmarks.length} benchmarks`;
  const ddilDetail = ddilStatusDetail({
    deploymentStateName,
    invalidPendingOperations,
    pendingOperations,
    replayBlockedOperations,
    replayReadyOperations,
    runtimeOptimizationAdvisories,
    supersededOperations,
    verifiedPendingOperations
  });
  const nextPackageState = selectedPackage ? nextPromotion(selectedPackage.promotion?.state ?? "candidate") : "";
  const derivedReadinessVerdict = buildReadinessVerdict({
    deadLetteredOperations,
    evidenceValue,
    invalidPendingOperations,
    latestRollout,
    missionPhaseTotal,
    missionProofComplete,
    offlineMode,
    pendingOperations,
    proofEvents,
    replayBlockedOperations,
    runtimeOptimizationAdvisories,
    selectedDevice,
    edgeRuntimeFit,
    resourceEnvelopeFit,
    selectedModel,
    selectedRuntime,
    selectedRuntimeValidation,
    signedEvidenceImports
  });
  const readinessContext = useMemo(
    () => ({
      package_id: selectedModel?.packageId,
      model_id: selectedModel?.id,
      device_id: selectedDevice ? deviceId(selectedDevice) : undefined,
      runtime_target_id: selectedRuntime ? runtimeTargetId(selectedRuntime) : undefined,
      slot: missionDraft.slot || "vision"
    }),
    [missionDraft.slot, selectedDevice, selectedModel, selectedRuntime]
  );
  const readinessKey = [
    readinessContext.package_id,
    readinessContext.model_id,
    readinessContext.device_id,
    readinessContext.runtime_target_id,
    readinessContext.slot
  ].join("|");
  const scopedReadiness = readinessMatchesContext(contextReadiness, readinessContext)
    ? contextReadiness
    : readinessMatchesContext(snapshot.readiness, readinessContext)
      ? snapshot.readiness
      : undefined;
  const runtimeDecision = asRecord(scopedReadiness?.runtime_decision);
  const edgeExecutionContract = asRecord(scopedReadiness?.edge_execution_contract);
  const runtimeFitDisplay = runtimeFitDisplayFor(scopedReadiness, edgeRuntimeFit, selectedRuntime);
  const targetFitValue = selectedModel ? runtimeFitDisplay.label : compatibleTargets;
  const targetFitDetail = selectedModel
    ? `${selectedRuntime ? runtimeTargetId(selectedRuntime) : "runtime target"}; ${compatibleTargets}/${snapshot.runtimeTargets.length} eligible`
    : "runtime targets available";
  const readinessVerdict =
    !hasLoadedSnapshot
      ? syncingReadinessVerdict()
      : scopedReadiness?.gates?.length
      ? readinessVerdictFromApi(scopedReadiness)
      : derivedReadinessVerdict;
  const edgeRuntimeMission = buildEdgeRuntimeMission({
    device: selectedDevice,
    edgeRuntimeFit,
    missionReplay: snapshot.missionReplay,
    model: selectedModel,
    pendingOperationLedger,
    pendingOperations,
    readiness: scopedReadiness,
    readinessVerdict,
    replayBlockedOperations,
    resourceEnvelopeFit,
    runtime: selectedRuntime,
    runtimeFitDisplay,
    runtimeValidation: selectedRuntimeValidation
  });
  const edgeProofWorkflow = buildEdgeProofWorkflow({
    device: selectedDevice,
    model: selectedModel,
    readiness: scopedReadiness,
    readinessVerdict,
    runtime: selectedRuntime,
    runtimeFitDisplay
  });
  const draftMissionPackageManifest = buildMissionPackageManifest({
    device: selectedDevice,
    draft: missionDraft,
    model: selectedModel,
    runtime: selectedRuntime
  });
  const missionPackageManifest = missionPackagePlan ?? draftMissionPackageManifest;
  const missionReady = Boolean((missionDraft.goal || missionDraft.yaml).trim());
  const missionPackageStageStatus = buildMissionPackageStageStatus({
    handoff: lastMissionPackageHandoff,
    manifest: missionPackageManifest,
    missionReady,
    plan: missionPackagePlan
  });
  const hubStages = buildHubStages({
    ddilDetail,
    deadLetteredOperations,
    evidenceBundleCount: snapshot.evidenceBundles.length,
    evidenceDetail,
    evidenceValue,
    latestRollout,
    missionDraft,
    missionPackageStageStatus,
    missionProofComplete,
    missionReady,
    missionRolloutCount: missionRollouts.length,
    offlineMode,
    proofEvents,
    replayBlockedOperations,
    rolloutDetail,
    runtimeFitDisplay,
    selectedModel,
    selectedRuntime
  });
  const showProductStage =
    activeHubStage === "model" || activeHubStage === "deploy" || activeHubStage === "field";
  const missionPackageDeploymentIntent = asRecord(missionPackageManifest.deployment_intent);
  const missionPackageDeploymentCommand = asRecord(missionPackageDeploymentIntent.command);
  const hasMissionPackageDeploymentIntent = Boolean(
    missionPackageDeploymentIntent.rollout_id && missionPackageDeploymentCommand.path
  );
  const canStageMissionPackage =
    hasMissionPackageDeploymentIntent && missionPackageStageStatus.stageable;
  const edgeProofTrace = useMemo(
    () => edgeProofTraceStatus(lastEdgeProof, readinessContext),
    [lastEdgeProof, readinessKey]
  );
  const baseEdgeProofComponentDigests = useMemo(
    () => edgeProofComponentDigestStatus(lastEdgeProof, readinessContext),
    [lastEdgeProof, readinessKey]
  );
  const [verifiedEdgeProofComponentDigests, setVerifiedEdgeProofComponentDigests] = useState<EdgeProofComponentDigestStatus | undefined>();
  const edgeProofComponentDigests = verifiedEdgeProofComponentDigests ?? baseEdgeProofComponentDigests;

  useEffect(() => {
    let cancelled = false;
    setVerifiedEdgeProofComponentDigests(undefined);
    if (!lastEdgeProof || baseEdgeProofComponentDigests.status !== "retained") return () => {
      cancelled = true;
    };
    setVerifiedEdgeProofComponentDigests({
      ...baseEdgeProofComponentDigests,
      detail: "Browser is recomputing runtime workbench, trace, and manifest hashes.",
      status: "verifying",
      tone: "neutral",
      value: "verifying digests"
    });
    void verifyEdgeProofComponentDigestStatus(lastEdgeProof, baseEdgeProofComponentDigests)
      .then((status) => {
        if (!cancelled) setVerifiedEdgeProofComponentDigests(status);
      })
      .catch((error) => {
        if (!cancelled) {
          const detail = error instanceof Error ? error.message : String(error);
          setVerifiedEdgeProofComponentDigests({
            ...baseEdgeProofComponentDigests,
            detail,
            errors: [detail],
            status: "mismatch",
            tone: "bad",
            value: "digest verification failed"
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [baseEdgeProofComponentDigests, lastEdgeProof]);

  useEffect(() => {
    if (selectedModelId) return;
    const activeModel = activeModelId ? models.find((model) => model.id === activeModelId) : undefined;
    if (activeModel) {
      setSelectedModelId(activeModel.id);
      return;
    }
    if (models[0]) setSelectedModelId(models[0].id);
  }, [activeModelId, models, selectedModelId]);

  useEffect(() => {
    if (!selectedDeviceId && snapshot.devices[0]) setSelectedDeviceId(deviceId(snapshot.devices[0]));
  }, [snapshot.devices, selectedDeviceId]);

  useEffect(() => {
    if (!selectedRuntimeId && selectedRuntime) setSelectedRuntimeId(runtimeTargetId(selectedRuntime));
  }, [selectedRuntime, selectedRuntimeId]);

  useEffect(() => {
    if (!readinessContext.package_id && !readinessContext.device_id && !readinessContext.runtime_target_id) {
      setContextReadiness(undefined);
      return;
    }
    let cancelled = false;
    setContextReadiness(undefined);
    void loadReadiness(token, readinessContext)
      .then((readiness) => {
        if (!cancelled) setContextReadiness(readiness);
      })
      .catch(() => {
        if (!cancelled) setContextReadiness(undefined);
      });
    return () => {
      cancelled = true;
    };
  }, [readinessKey, readinessRefreshVersion, token]);

  useEffect(() => {
    setMissionPackagePlan(undefined);
    setLastMissionPackageHandoff(undefined);
  }, [missionDraft, readinessKey]);

  async function run(title: string, action: () => Promise<unknown>, shouldRefresh = true): Promise<void> {
    setLoading(true);
    try {
      const payload = await action();
      setPreview(payload === undefined ? undefined : { title, payload });
      if (shouldRefresh) {
        const nextSnapshot = await loadSnapshot(token);
        setSnapshot(nextSnapshot);
        setReadinessRefreshVersion((version) => version + 1);
      }
      setToast({ tone: "success", title });
    } catch (error) {
      setToast(errorToast(title, error));
    } finally {
      setLoading(false);
    }
  }

  function persistToken(): void {
    const trimmed = token.trim();
    saveToken(trimmed);
    setToken(trimmed);
    setToast({ tone: "success", title: trimmed ? "API token saved" : "API token cleared" });
  }

  function importMissionYaml(yaml: string, fileName: string): void {
    const selection = missionSelectionFromYaml(yaml);
    const selectedYamlModel =
      (selection.modelId ? models.find((model) => model.id === selection.modelId) : undefined) ??
      (selection.packageId ? models.find((model) => model.packageId === selection.packageId) : undefined);
    const selectedYamlDevice = selection.deviceId
      ? snapshot.devices.find((device) => deviceId(device) === selection.deviceId)
      : undefined;
    const selectedYamlRuntime = selection.runtimeTargetId
      ? snapshot.runtimeTargets.find((target) => runtimeTargetId(target) === selection.runtimeTargetId)
      : undefined;
    const appliedSelection: string[] = [];
    const missingSelection: string[] = [];

    if (selectedYamlModel) {
      setSelectedModelId(selectedYamlModel.id);
      appliedSelection.push(`model ${selectedYamlModel.id}`);
    } else {
      if (selection.modelId) missingSelection.push(`model ${selection.modelId}`);
      if (!selection.modelId && selection.packageId) missingSelection.push(`package ${selection.packageId}`);
    }
    if (selectedYamlDevice) {
      setSelectedDeviceId(deviceId(selectedYamlDevice));
      appliedSelection.push(`edge ${deviceId(selectedYamlDevice)}`);
    } else if (selection.deviceId) {
      missingSelection.push(`edge ${selection.deviceId}`);
    }
    if (selectedYamlRuntime) {
      setSelectedRuntimeId(runtimeTargetId(selectedYamlRuntime));
      appliedSelection.push(`runtime ${runtimeTargetId(selectedYamlRuntime)}`);
    } else if (selection.runtimeTargetId) {
      missingSelection.push(`runtime ${selection.runtimeTargetId}`);
    }

    setMissionDraft((current) => missionDraftFromYaml(current, yaml));
    setMissionPackagePlan(undefined);
    setLastMissionPackageHandoff(undefined);
    const detailParts = [`${fileName} populated mission, SLO, handling, and DDIL fields.`];
    if (appliedSelection.length) {
      detailParts.push(`Selected ${appliedSelection.join(", ")} from the spec.`);
    }
    if (missingSelection.length) {
      detailParts.push(`Unmatched hints: ${missingSelection.join(", ")}.`);
    }
    setToast({
      tone: "success",
      title: "Mission YAML imported",
      detail: detailParts.join(" ")
    });
    navigateHubStage("mission");
  }

  function reportMissionYamlImportError(fileName: string): void {
    setToast({
      tone: "error",
      title: "Mission YAML import failed",
      detail: `${fileName} could not be read by the browser.`
    });
  }

  async function copyCommand(label: string, command: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(command);
      setToast({ tone: "success", title: `${label} copied` });
    } catch {
      setPreview({ title: label, payload: { command } });
      setToast({
        tone: "info",
        title: `${label} ready`,
        detail: "Command opened in the payload panel."
      });
    }
  }

  function missionPackagePlanPayload() {
    return buildMissionPackagePlanRequest({
      draft: missionDraft,
      readinessContext
    });
  }

  function planMissionPackageArtifact(): void {
    void run(
      "Plan mission package",
      async () => {
        const plan = await planMissionPackage(token, missionPackagePlanPayload());
        setMissionPackagePlan(plan);
        setLastMissionPackageHandoff(undefined);
        return plan;
      },
      false
    );
  }

  function downloadMissionPackageArtifact(): void {
    void run(
      "Download mission package",
      async () => {
        const artifact = await downloadMissionPackage(token, missionPackagePlanPayload());
        setMissionPackagePlan(artifact.payload);
        setLastMissionPackageHandoff(artifact.handoff);
        downloadJson(artifact.fileName, artifact.payload);
        return {
          fileName: artifact.fileName,
          handoff: artifact.handoff,
          package: artifact.payload
        };
      },
      false
    );
  }

  function stageMissionPackageRollout(): void {
    const deploymentIntent = asRecord(missionPackageManifest.deployment_intent);
    if (!deploymentIntent.rollout_id) {
      setToast({
        tone: "info",
        title: "Plan package first",
        detail: "Stage rollout uses the mission package deployment intent."
      });
      navigateHubStage("package");
      return;
    }
    if (!missionPackageStageStatus.stageable) {
      setToast({
        tone: "info",
        title: "Proof gate blocks staging",
        detail:
          missionPackageStageStatus.gateStatus === "failed"
            ? "Refresh package planning after resolving runtime readiness blockers."
            : "Run readiness/proof planning until the package proof gate passes."
      });
      navigateHubStage("package");
      return;
    }
    void run(
      "Stage package rollout",
      async () => {
        const stage = await stageMissionPackage(
          token,
          {
            actor: "operator:mission-package-workbench",
            mission_package: missionPackageManifest,
            reason: "mission package deployment handoff",
            rollout_id: missionPackageRolloutId(missionPackageManifest)
          }
        );
        navigateHubStage("deploy", { workflowTarget: "rollouts" });
        return stage;
      },
      true
    );
  }

  function copyMissionPackageManifest(): void {
    void copyCommand(
      "Mission package manifest",
      JSON.stringify(missionPackageManifest, null, 2)
    );
  }

  function generateEdgeProofArtifact(): void {
    void run(
      "Generate edge runtime proof",
      async () => {
        const proof = await loadEdgeRuntimeProof(token, {
          ...readinessContext,
          source_action: "edge-runtime-mission",
          require_go: true,
          min_runtime_fit: 95,
          require_best_runtime: true,
          require_capability_lock: true
        });
        adoptEdgeProofReadiness(proof);
        setLastEdgeProof(proof);
        setLastEdgeProofHandoff(undefined);
        return proof;
      },
      false
    );
  }

  function downloadEdgeProofArtifact(): void {
    void run(
      "Download edge runtime proof",
      async () => {
        const artifact = await downloadEdgeRuntimeProof(token, {
          ...readinessContext,
          source_action: "edge-runtime-mission",
          require_go: true,
          min_runtime_fit: 95,
          require_best_runtime: true,
          require_capability_lock: true
        });
        adoptEdgeProofReadiness(artifact.payload);
        setLastEdgeProof(artifact.payload);
        setLastEdgeProofHandoff(artifact.handoff);
        downloadJson(artifact.fileName, artifact.payload);
        return artifact.payload;
      },
      false
    );
  }

  function adoptEdgeProofReadiness(proof: unknown): void {
    const record = asRecord(proof);
    if (record.schema_version !== "temms-edge-runtime-proof/v1") return;
    const readiness = asRecord(record.readiness) as DeploymentReadiness;
    if (!selectionMatchesContext(asRecord(record.selection), readinessContext)) return;
    if (!readinessMatchesContext(readiness, readinessContext)) return;
    setContextReadiness(readiness);
    setSnapshot((current) => ({ ...current, readiness }));
  }

  function submitForm(name: string, event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const form = event.currentTarget;
    const actor = fieldValue(form, "actor") || "operator:react-ui";
    const handlers: Record<string, () => Promise<unknown>> = {
      "enroll-device": () =>
        hubApi.enrollDevice(
          {
            device_id: fieldValue(form, "device_id"),
            profile: fieldValue(form, "profile"),
            labels: { site: fieldValue(form, "site"), source: "react-ui" },
            actor
          },
          token
        ),
      "register-package": () =>
        hubApi.registerPackage(
          {
            package_path: fieldValue(form, "package_path"),
            strict_metadata: isChecked(form, "strict_metadata"),
            actor
          },
          token
        ),
      "compatibility-preview": () =>
        hubApi.previewCompatibility(
          {
            device_id: fieldValue(form, "device_id"),
            package_id: fieldValue(form, "package_id"),
            model_id: fieldValue(form, "model_id") || undefined,
            runtime_target_id: fieldValue(form, "runtime_target_id") || undefined
          },
          token
        ),
      "assign-rollout": () =>
        hubApi.assignRollout(
          {
            rollout_id: fieldValue(form, "rollout_id") || undefined,
            device_id: fieldValue(form, "device_id"),
            package_id: fieldValue(form, "package_id"),
            model_id: fieldValue(form, "model_id") || undefined,
            slot: fieldValue(form, "slot") || undefined,
            runtime_target_id: fieldValue(form, "runtime_target_id") || undefined,
            require_approval: isChecked(form, "require_approval"),
            reason: "operator assigned rollout from Mission Package Workbench",
            actor
          },
          token
        ),
      "create-rollout-plan": () =>
        hubApi.createRolloutPlan(
          {
            plan_id: fieldValue(form, "plan_id") || undefined,
            package_id: fieldValue(form, "package_id"),
            model_id: fieldValue(form, "model_id") || undefined,
            device_ids: csv(fieldValue(form, "device_ids")),
            slot: fieldValue(form, "slot") || undefined,
            runtime_target_id: fieldValue(form, "runtime_target_id") || undefined,
            batch_size: Number(fieldValue(form, "batch_size") || "1"),
            require_approval: isChecked(form, "require_approval"),
            require_runtime_validation: isChecked(form, "require_runtime_validation"),
            reason: "operator created rollout plan from Mission Package Workbench",
            actor
          },
          token
        ),
      "airgap-import": () => hubApi.importAirgap(JSON.parse(fieldValue(form, "bundle")) as JsonObject, token)
    };
    const handler = handlers[name];
    if (handler) void run(actionTitle(name), handler, name !== "compatibility-preview");
  }

  function promoteSelectedPackage(): void {
    if (!selectedPackage || !nextPackageState) return;
    const id = packageId(selectedPackage);
    void run(`Promote ${id}`, () =>
      hubApi.promotePackage(
        id,
        {
          state: nextPackageState,
          actor: "operator:react-ui",
          reason: `promoted to ${nextPackageState} from Mission Package Workbench`
        },
        token
      )
    );
  }

  function applyEdgeRecommendation(recommendation: EdgeRecommendation): void {
    if (recommendation.model_id) setSelectedModelId(String(recommendation.model_id));
    if (recommendation.device_id) setSelectedDeviceId(String(recommendation.device_id));
    if (recommendation.runtime_target_id) setSelectedRuntimeId(String(recommendation.runtime_target_id));
    setFocusedWorkflow("deployment");
  }

  function approveRollout(id: string): void {
    void run(`Approve ${id}`, () =>
      hubApi.approveRollout(
        id,
        {
          actor: "operator:approver-ui",
          reason: "mission policy approved from Mission Package Workbench"
        },
        token
      )
    );
  }

  function applyRollout(id: string): void {
    const rollout = snapshot.rollouts.find((candidate) => rolloutId(candidate) === id);
    void run(`Apply ${id}`, () =>
      hubApi.applyRollout(
        id,
        {
          actor: "operator:react-ui",
          model_id: rollout?.model_id || selectedModel?.id
        },
        token
      )
    );
  }

  function rollbackRollout(id: string): void {
    void run(`Rollback ${id}`, () =>
      hubApi.rollbackRollout(
        id,
        {
          actor: "operator:mission-package-workbench",
          reason: "operator requested rollback from Mission Package Workbench"
        },
        token
      )
    );
  }

  function advanceRolloutPlan(id: string): void {
    void run(`Advance ${id}`, () =>
      hubApi.advanceRolloutPlan(
        id,
        {
          actor: "operator:mission-package-workbench"
        },
        token
      )
    );
  }

  function pauseRolloutPlan(id: string): void {
    void run(`Pause ${id}`, () =>
      hubApi.pauseRolloutPlan(
        id,
        {
          actor: "operator:mission-package-workbench",
          reason: "operator paused rollout plan from Mission Package Workbench"
        },
        token
      )
    );
  }

  function resumeRolloutPlan(id: string): void {
    void run(`Resume ${id}`, () =>
      hubApi.resumeRolloutPlan(
        id,
        {
          actor: "operator:mission-package-workbench",
          reason: "operator resumed rollout plan from Mission Package Workbench"
        },
        token
      )
    );
  }

  function exportEvidence(mode: EvidenceExportMode): void {
    const body =
      mode === "summary"
        ? { summary: true, summary_limit: 20 }
        : mode === "replay"
          ? { replay: true, replay_limit: 50 }
          : { decision_limit: 100, include_benchmarks: true };
    void run(`Evidence ${mode}`, () => hubApi.exportEvidence(body, token), false);
  }

  function exportAirgap(includePackages: boolean): void {
    void run(
      includePackages ? "Export air-gap bundle with packages" : "Export air-gap bundle",
      () => hubApi.exportAirgap({ include_packages: includePackages }, token),
      false
    );
  }

  function enterOfflineMode(): void {
    void run("Enter DDIL offline mode", () => controlApi.setOffline(token));
  }

  function restoreOnlineMode(): void {
    void run("Restore online mode", () => controlApi.setOnline(token));
  }

  function syncPendingOperations(): void {
    void run(
      "Sync pending DDIL operations",
      async () => {
        const payload = await controlApi.syncPending(token);
        const nextSnapshot = await loadSnapshotAfterReconciliation(token);
        setSnapshot(nextSnapshot);
        setReadinessRefreshVersion((version) => version + 1);
        return payload;
      },
      false
    );
  }

  function quarantineBlockedOperations(): void {
    void run(
      "Quarantine blocked DDIL operations",
      () =>
        controlApi.quarantineBlocked(
          {
            actor: "operator:mission-package-workbench",
            reason: "operator quarantined blocked DDIL preflight"
          },
          token
        ),
      true
    );
  }

  function acknowledgeDeadLetteredOperations(): void {
    void run(
      "Acknowledge quarantined DDIL operations",
      () =>
        controlApi.acknowledgeDeadLetters(
          {
            actor: "operator:mission-package-workbench",
            reason: "operator reviewed quarantined DDIL intents"
          },
          token
        ),
      true
    );
  }

  function requeueDeadLetteredOperations(): void {
    void run(
      "Requeue quarantined DDIL operations",
      () =>
        controlApi.requeueDeadLetters(
          {
            actor: "operator:mission-package-workbench",
            reason: "operator requeued remediated DDIL intents",
            require_ready: true
          },
          token
        ),
      true
    );
  }

  function requeueDeadLetteredOperation(operation: Record<string, unknown>): void {
    const digest = stringOf(operation.payload_sha256, "");
    if (!digest) {
      setToast({
        tone: "info",
        title: "Requeue unavailable",
        detail: "This quarantined DDIL intent does not include a payload hash."
      });
      return;
    }
    void run(
      "Requeue quarantined DDIL intent",
      () =>
        controlApi.requeueDeadLetters(
          {
            actor: "operator:mission-package-workbench",
            reason: "operator requeued remediated DDIL intent",
            payload_sha256s: [digest],
            require_ready: true
          },
          token
        ),
      true
    );
  }

  function retargetPendingRuntime(operation: Record<string, unknown>): void {
    const payloadSha256 = stringOf(operation.payload_sha256, "");
    const runtimeTargetId =
      stringOf(operation.runtime_remediation_runtime_target_id, "") ||
      stringOf(operation.best_runtime_target_id, "");
    if (!payloadSha256 || !runtimeTargetId) {
      setToast({
        tone: "info",
        title: "Runtime retarget unavailable",
        detail: "This pending DDIL intent does not include a measured runtime target candidate."
      });
      return;
    }
    void run("Retarget pending runtime", () =>
      controlApi.retargetRuntime(
        {
          payload_sha256: payloadSha256,
          runtime_target_id: runtimeTargetId,
          actor: "operator:mission-package-workbench",
          reason: "operator selected measured best runtime target"
        },
        token
      )
    );
  }

  function queueDeploymentIntent(): void {
    void run("Queue DDIL deployment intent", () =>
      controlApi.requestDeploy(
        {
          actor: "operator:mission-package-workbench",
          source: "hub-ddil-drill",
          package_id: selectedModel?.packageId,
          model_id: selectedModel?.id,
          device_id: selectedDevice ? deviceId(selectedDevice) : undefined,
          runtime_target_id: selectedRuntime ? runtimeTargetId(selectedRuntime) : undefined,
          slot: "vision",
          requested_at: new Date().toISOString()
        },
        token
      )
    );
  }

  function workflowClass(target: WorkflowTarget, className: string): string {
    return focusedWorkflow === target ? `${className} workflow-target-active` : className;
  }

  function workflowRefForTarget(target: WorkflowTarget) {
    return {
      model: modelWorkflowRef,
      deployment: deploymentWorkflowRef,
      plans: plansWorkflowRef,
      rollouts: rolloutsWorkflowRef,
      ddil: ddilWorkflowRef,
      evidence: evidenceWorkflowRef,
      assets: assetsWorkflowRef
    }[target];
  }

  function navigateHubStage(stage: HubStage, options: { workflowTarget?: WorkflowTarget } = {}): void {
    setActiveHubStage(stage);
    setFocusedWorkflow(options.workflowTarget);
    window.setTimeout(() => {
      const section = options.workflowTarget ? workflowRefForTarget(options.workflowTarget).current : stageFlowRef.current;
      if (!section) return;
      section.scrollIntoView({ behavior: "smooth", block: "start" });
      if (options.workflowTarget) {
        section.focus({ preventScroll: true });
        return;
      }
      const activeStep = stageFlowRef.current?.querySelector<HTMLElement>(`[data-stage-id="${stage}"]`);
      activeStep?.focus({ preventScroll: true });
    }, 0);
  }

  function focusWorkflow(target: WorkflowTarget, actionLabel: string, contextLabel = ""): void {
    navigateHubStage(hubStageForWorkflowTarget(target), { workflowTarget: target });
    setToast({
      tone: "success",
      title: actionLabel,
      detail: `${workflowTargetLabel(target)} is focused${
        contextLabel ? ` for ${contextLabel}` : ""
      }.`
    });
  }

  function handleReadinessAction(action: ReadinessGateAction): void {
    const command = readinessCommand(action);
    applyReadinessActionSelection(action);
    focusWorkflow(
      workflowTargetForReadinessAction(action),
      action.label,
      readinessActionContext(action)
    );
    if (command) setPendingReadinessAction(action);
  }

  function applyReadinessActionSelection(action: ReadinessGateAction): void {
    if (!["select_context", "select_runtime_target"].includes(action.kind)) return;
    const refs = asRecord(action.refs);
    const model = stringOf(refs.model_id, "");
    const device = stringOf(refs.device_id, "");
    const runtime = stringOf(refs.runtime_target_id, "");
    if (model) setSelectedModelId(model);
    if (device) setSelectedDeviceId(device);
    if (runtime) setSelectedRuntimeId(runtime);
  }

function executePendingReadinessAction(): void {
  const action = pendingReadinessAction;
  const command = action ? readinessCommand(action) : undefined;
  if (!action || !command) return;
  if (command.requires_edge_execution) {
    setToast({
      tone: "info",
      title: "Run this on the edge node",
      detail: edgeReadinessCommandReason(action, command)
    });
    return;
  }
    setPendingReadinessAction(undefined);
    void run(
      `Run ${action.label}`,
      async () => {
        const payload = await executeReadinessCommand(command, token);
        if (command.path === "/v1/control/sync") {
          const nextSnapshot = await loadSnapshotAfterReconciliation(token);
          setSnapshot(nextSnapshot);
          setReadinessRefreshVersion((version) => version + 1);
        }
        return payload;
      },
      command.path !== "/v1/control/sync"
    );
  }

  const activeStageRunbook = hubStageRunbookFor({
    activeStage: activeHubStage,
    currentStage: hubStages.find((stage) => stage.id === activeHubStage) ?? hubStages[0],
    deadLetteredOperations,
    latestRollout,
    missionPackageStageStatus,
    missionProofComplete,
    missionReady,
    offlineMode,
    proofEvents,
    replayBlockedOperations,
    runtimeFitDisplay,
    selectedDevice,
    selectedModel,
    selectedRuntime,
    onDownloadPackage: downloadMissionPackageArtifact,
    onGenerateProof: generateEdgeProofArtifact,
    onGoDeploy: () => navigateHubStage("deploy"),
    onGoFieldOps: () => navigateHubStage("field"),
    onGoHandling: () => navigateHubStage("handling"),
    onGoModels: () => navigateHubStage("model"),
    onGoPackage: () => navigateHubStage("package"),
    onGoRuntime: () => navigateHubStage("runtime"),
    onPlanPackage: planMissionPackageArtifact,
    onStageDeploy: stageMissionPackageRollout,
    onSync: () => void refresh()
  });
  const workflowSignals: MissionWorkflowSignal[] = [
    {
      label: "Mission",
      value: missionDraft.yaml ? "YAML loaded" : missionDraft.goal ? "goal defined" : "mission pending",
      detail: missionDraft.sensor ? `${missionDraft.sensor} / ${missionDraft.slot || "vision"}` : "sensor pending",
      tone: missionReady ? "good" : "warn"
    },
    {
      label: "Model",
      value: selectedModel?.name ?? "select model",
      detail: selectedModel?.packageId ?? "signed package pending",
      tone: selectedModel ? "good" : "warn"
    },
    {
      label: "Runtime",
      value: selectedRuntime ? runtimeTargetId(selectedRuntime) : "select runtime",
      detail: selectedDevice ? `${deviceId(selectedDevice)} / ${runtimeFitDisplay.label}` : runtimeFitDisplay.detail,
      tone: selectedRuntime && selectedDevice ? runtimeFitDisplay.tone : "warn"
    },
    {
      label: "Handling",
      value: missionDraft.switchPolicy.replace(/_/g, " "),
      detail: `fallback ${missionDraft.fallbackModelId || "auto"} / ${missionDraft.ddilMode.replace(/_/g, " ")}`,
      tone: missionDraft.sensor && missionDraft.slot ? "good" : "warn"
    },
    {
      label: "Package",
      value: missionPackageStageStatus.value,
      detail: missionPackageStageStatus.detail,
      tone: missionPackageStageStatus.tone
    }
  ];

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <span className="eyebrow">TEMMS</span>
          <h1>Mission Package Workbench</h1>
          <p>
            Turn a mission spec into a model/runtime plan, sensor handling policy, signed package, and edge deploy
            intent with on-device SLO, DDIL, and evidence gates attached.
          </p>
        </div>
        <div className="access-strip">
          <label className="token-field">
            <span>API token</span>
            <input
              type="password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="optional bearer token"
            />
          </label>
          <Button icon={<KeyRound size={16} />} variant="secondary" onClick={persistToken}>
            Save
          </Button>
          <Button icon={<RefreshCw size={16} />} onClick={() => void refresh()} disabled={loading}>
            {loading ? "Syncing" : "Sync"}
          </Button>
        </div>
      </header>

      {toast ? <ToastView toast={toast} /> : null}

      <div className="mission-workflow-shell" ref={stageFlowRef}>
        <MissionWorkflowCockpit
          activeStage={activeHubStage}
          contextState={offlineMode ? "offline" : "online"}
          runbook={activeStageRunbook}
          signals={workflowSignals}
          stages={hubStages}
          onSelect={navigateHubStage}
        >
          <section className="mission-strip" aria-label="Mission status">
            <StatusTile label="Models" value={models.length} detail={`${signedPackages} signed`} icon={<Box size={18} />} />
            <StatusTile
              label="Released"
              value={releasedPackages}
              detail={`${modelValidationCount} validations`}
              icon={<BadgeCheck size={18} />}
            />
            <StatusTile
              label="Runtime Fit"
              value={targetFitValue}
              detail={targetFitDetail}
              icon={<Cpu size={18} />}
            />
            <StatusTile
              label="Rollouts"
              value={missionRollouts.length}
              detail={rolloutDetail}
              icon={<GitBranch size={18} />}
            />
            <StatusTile
              label="Evidence"
              value={evidenceValue}
              detail={evidenceDetail}
              icon={<ShieldCheck size={18} />}
            />
            <StatusTile
              label="DDIL"
              value={offlineMode ? "Offline" : "Online"}
              detail={ddilDetail}
              icon={<Activity size={18} />}
            />
          </section>
        </MissionWorkflowCockpit>
      </div>

      {activeHubStage === "mission" ? (
        <div className="stage-stack" data-testid="hub-stage-mission">
          <MissionDesignPanel
            draft={missionDraft}
            manifest={missionPackageManifest}
            selectedDevice={selectedDevice}
            selectedModel={selectedModel}
            selectedRuntime={selectedRuntime}
            onChange={setMissionDraft}
            onCopyManifest={copyMissionPackageManifest}
            onImportYaml={importMissionYaml}
            onImportYamlError={reportMissionYamlImportError}
            onPlanPackage={planMissionPackageArtifact}
          />
        </div>
      ) : null}

      {activeHubStage === "runtime" ? (
        <div className="stage-stack" data-testid="hub-stage-runtime">
          <EdgeRuntimeWorkbench
            devices={snapshot.devices}
            edgeExecutionContract={edgeExecutionContract}
            models={models}
            readiness={scopedReadiness}
            resourceEnvelopeFit={resourceEnvelopeFit}
            runtimeDecision={runtimeDecision}
            runtimeFitDisplay={runtimeFitDisplay}
            runtimeTargets={snapshot.runtimeTargets}
            runtimeValidations={snapshot.runtimeValidations}
            selectedDevice={selectedDevice}
            selectedModel={selectedModel}
            selectedRuntime={selectedRuntime}
            onCopyCommand={(label, command) => void copyCommand(label, command)}
            onGenerateProof={generateEdgeProofArtifact}
            onGoHandling={() => navigateHubStage("handling")}
            onGoModels={() => navigateHubStage("model")}
            onSelectDevice={setSelectedDeviceId}
            onSelectRuntime={setSelectedRuntimeId}
          />

          <EdgeOperatorCommandPanel
            device={selectedDevice}
            edgeExecutionContract={edgeExecutionContract}
            model={selectedModel}
            proofWorkflow={edgeProofWorkflow}
            readiness={scopedReadiness}
            runtime={selectedRuntime}
            runtimeDecision={runtimeDecision}
            runtimeFitDisplay={runtimeFitDisplay}
          />
        </div>
      ) : null}

      {activeHubStage === "handling" ? (
        <div className="stage-stack" data-testid="hub-stage-handling">
          <HandlingPolicyPanel
            draft={missionDraft}
            manifest={missionPackageManifest}
            models={models}
            selectedDevice={selectedDevice}
            selectedModel={selectedModel}
            selectedRuntime={selectedRuntime}
            onChange={setMissionDraft}
            onGoPackage={() => navigateHubStage("package")}
            onPlanPackage={planMissionPackageArtifact}
          />
        </div>
      ) : null}

      {activeHubStage === "package" ? (
        <div className="stage-stack" data-testid="hub-stage-package">
          <EdgePackagePlanPanel
            canStageDeploy={canStageMissionPackage}
            manifest={missionPackageManifest}
            readinessVerdict={readinessVerdict}
            workflow={edgeProofWorkflow}
            onCopyManifest={copyMissionPackageManifest}
            onDownloadPackage={downloadMissionPackageArtifact}
            onGoDeploy={() => navigateHubStage("deploy")}
            onPlanPackage={planMissionPackageArtifact}
            onStageDeploy={stageMissionPackageRollout}
          />
          <MissionPackageDownloadHandoffCard
            handoff={lastMissionPackageHandoff}
            manifest={missionPackageManifest}
          />

          <details className="package-verification-drawer" data-testid="package-advanced-verification">
            <summary>
              <span className="package-verification-summary-copy">
                <span className="section-kicker">Advanced verification</span>
                <strong>Proof, readiness, and execution contract</strong>
                <small>Open when an operator needs to inspect why this package can or cannot deploy.</small>
              </span>
              <Badge value={readinessVerdict.label} />
            </summary>

            <div className="package-verification-stack">
              <ReadinessVerdictPanel verdict={readinessVerdict} onAction={handleReadinessAction} />

              <EdgeRuntimeMissionPanel mission={edgeRuntimeMission} />

              <EdgeProofPanel
                componentDigests={edgeProofComponentDigests}
                disabled={loading}
                handoff={lastEdgeProofHandoff}
                proof={lastEdgeProof}
                trace={edgeProofTrace}
                workflow={edgeProofWorkflow}
                onGenerate={generateEdgeProofArtifact}
                onDownload={downloadEdgeProofArtifact}
                onCopy={(label, command) => void copyCommand(label, command)}
              />

              <EdgeExecutionContractPanel
                device={selectedDevice}
                edgeRuntimeFit={edgeRuntimeFit}
                edgeExecutionContract={edgeExecutionContract}
                model={selectedModel}
                readiness={scopedReadiness}
                readinessVerdict={readinessVerdict}
                resourceEnvelopeFit={resourceEnvelopeFit}
                runtime={selectedRuntime}
                runtimeDecision={runtimeDecision}
                runtimeFitDisplay={runtimeFitDisplay}
                runtimeValidation={selectedRuntimeValidation}
                onCopyRemediation={(label, command) => void copyCommand(label, command)}
                onSelectRuntimeTarget={(runtimeTargetIdValue) => setSelectedRuntimeId(runtimeTargetIdValue)}
              />
            </div>
          </details>
        </div>
      ) : null}

      {pendingReadinessAction ? (
        <ReadinessCommandPanel
          action={pendingReadinessAction}
          disabled={loading}
          onCopy={(label, command) => void copyCommand(label, command)}
          onClose={() => setPendingReadinessAction(undefined)}
          onRun={executePendingReadinessAction}
        />
      ) : null}

      {showProductStage ? (
      <main className={`product-grid product-grid-stage-${activeHubStage}`} data-testid={`hub-stage-${activeHubStage}`}>
        {activeHubStage === "field" ? (
        <section
          className={workflowClass("ddil", "section section-wide readiness-section repair-section")}
          aria-labelledby="readiness-heading"
          ref={ddilWorkflowRef}
          tabIndex={-1}
        >
          <div className="section-header">
            <div>
              <span className="section-kicker">DDIL readiness</span>
              <h2 id="readiness-heading">Field operating picture</h2>
            </div>
            <Badge value={offlineMode ? "offline" : pendingOperations ? "pending" : "ready"} />
          </div>

          <div className="readiness-grid">
            <ReadinessCard
              title="Connectivity"
              value={connectivityState}
              detail={
                offlineMode
                  ? "link unavailable"
                  : pendingOperations
                    ? `${pendingOperations} pending operations`
                    : "network available"
              }
              state={offlineMode ? "warn" : "good"}
            />
            <ReadinessCard
              title="Deployment"
              value={deploymentStateName}
              detail={deploymentDetail}
              state={deploymentStateName === "READY" ? "good" : "warn"}
            />
            <ReadinessCard
              title="Active slot"
              value={stringOf(activeSlot?.slot, "vision")}
              detail={stringOf(activeSlot?.active_model, selectedModel?.id ?? "no active model")}
              state={activeSlot?.active_model ? "good" : "warn"}
            />
            <ReadinessCard
              title="Evidence chain"
              value={`${proofEvents} events`}
              detail={
                deadLetteredOperations
                  ? `${deadLetteredOperations} quarantined intent${deadLetteredOperations === 1 ? "" : "s"}`
                  : missionPhaseTotal
                    ? `${completedMissionPhases}/${missionPhaseTotal} replay phases`
                    : `${signedEvidenceImports} signed package${signedEvidenceImports === 1 ? "" : "s"}`
              }
              state={
                missionProofComplete || (proofEvents && signedEvidenceImports && !missionPhaseTotal)
                  ? "good"
                  : "warn"
              }
            />
	          </div>

	          {runtimeRepairProof ? (
	            <RuntimeRepairProofPanel
	              proof={runtimeRepairProof}
	              onRetargetRuntime={
	                runtimeRepairProof.operation ? retargetPendingRuntime : undefined
	              }
	            />
	          ) : null}

	          {latestEvents.length ? (
	            <div className="readiness-timeline" aria-label="Latest evidence events">
	              {latestEvents.map((event, index) => (
                <EvidenceEventRow key={`${stringOf(event.timestamp, "event")}-${index}`} event={event} />
              ))}
            </div>
          ) : null}

          {pendingOperationLedger.length ? (
            <div className="pending-operation-ledger" aria-label="Pending DDIL operations">
              {pendingOperationLedger.map((operation, index) => (
                <PendingOperationRow
                  key={`${stringOf(operation.payload_sha256, "pending")}-${index}`}
                  operation={operation}
                  onCopyCommand={(label, command) => void copyCommand(label, command)}
                  onRetargetRuntime={retargetPendingRuntime}
                />
              ))}
            </div>
          ) : null}

          {deadLetteredOperationLedger.length ? (
            <div className="dead-letter-operation-ledger" aria-label="Quarantined DDIL operations">
              <div className="ddil-ledger-heading">
                <span>Quarantined DDIL intents</span>
                <strong>{deadLetteredOperations}</strong>
              </div>
              {deadLetteredOperationLedger.map((operation, index) => (
                <DeadLetteredOperationRow
                  key={`${stringOf(operation.payload_sha256, "dead-letter")}-${index}`}
                  operation={operation}
                  onCopyCommand={(label, command) => void copyCommand(label, command)}
                  onRequeue={requeueDeadLetteredOperation}
                />
              ))}
            </div>
          ) : null}

          <div className="ddil-controls" aria-label="DDIL drill controls">
            <Button icon={<Activity size={16} />} variant="secondary" disabled={offlineMode} onClick={enterOfflineMode}>
              Link loss
            </Button>
            <Button icon={<RefreshCw size={16} />} variant="secondary" disabled={!offlineMode} onClick={restoreOnlineMode}>
              Restore link
            </Button>
            <Button
              icon={<Rocket size={16} />}
              variant="secondary"
              disabled={!selectedModel || !selectedDevice}
              onClick={queueDeploymentIntent}
            >
              Queue intent
            </Button>
            <Button
              icon={<UploadCloud size={16} />}
              variant="secondary"
              disabled={!pendingOperations || Boolean(replayBlockedOperations)}
              onClick={syncPendingOperations}
            >
              Sync pending
            </Button>
            <Button
              icon={<Database size={16} />}
              variant="secondary"
              disabled={!replayBlockedOperations}
              onClick={quarantineBlockedOperations}
            >
              Quarantine blocked
            </Button>
            <Button
              icon={<FileCheck2 size={16} />}
              variant="secondary"
              disabled={!deadLetteredOperations}
              onClick={requeueDeadLetteredOperations}
            >
              Requeue quarantined
            </Button>
            <Button
              icon={<CheckCircle2 size={16} />}
              variant="secondary"
              disabled={!deadLetteredOperations}
              onClick={acknowledgeDeadLetteredOperations}
            >
              Acknowledge quarantine
            </Button>
          </div>
        </section>
        ) : null}

        {activeHubStage === "model" ? (
          <ModelPlanStage
            assetsOpen={focusedWorkflow === "assets"}
            assetsRef={assetsWorkflowRef}
            assetsSectionClassName={workflowClass(
              "assets",
              "section section-wide utility-section assets-section stage-advanced-drawer"
            )}
            modelRef={modelWorkflowRef}
            models={models}
            nextPackageState={nextPackageState}
            resourceEnvelopeFit={resourceEnvelopeFit}
            runtimeFitDisplay={runtimeFitDisplay}
            selectedModel={selectedModel}
            selectedModelSectionClassName={workflowClass("model", "section selected-model-section")}
            selectedRuntime={selectedRuntime}
            selectedRuntimeValidation={selectedRuntimeValidation}
            onGoRuntime={() => navigateHubStage("runtime")}
            onPromoteSelectedPackage={promoteSelectedPackage}
            onSelectModel={setSelectedModelId}
            onSubmitForm={submitForm}
          />
        ) : null}

        {activeHubStage === "deploy" ? (
        <section
          className={workflowClass("deployment", "section section-wide deployment-section")}
          aria-labelledby="deploy-heading"
          ref={deploymentWorkflowRef}
          tabIndex={-1}
        >
          <div className="section-header">
            <div>
              <span className="section-kicker">Edge deploy</span>
              <h2 id="deploy-heading">Stage the planned mission package</h2>
            </div>
            <Badge value={latestRollout?.state ?? "not assigned"} />
          </div>

          <div className="deploy-primary-lane" aria-label="Mission package deploy path">
            <CapabilityMetric
              label="Package handoff"
              value={missionPackageStageStatus.value}
              detail={missionPackageStageStatus.detail}
              tone={missionPackageStageStatus.tone}
            />
            <CapabilityMetric
              label="Deploy intent"
              value={
                hasMissionPackageDeploymentIntent
                  ? String(missionPackageDeploymentIntent.rollout_id)
                  : "plan package first"
              }
              detail={
                hasMissionPackageDeploymentIntent
                  ? String(missionPackageDeploymentCommand.path || "/v1/hub/rollouts")
                  : "Deploy is bound only after the mission package is hashed."
              }
              tone={canStageMissionPackage ? "good" : "warn"}
            />
            <CapabilityMetric
              label="Edge target"
              value={selectedDevice ? deviceId(selectedDevice) : "select edge"}
              detail={`${selectedRuntime ? runtimeTargetId(selectedRuntime) : "runtime pending"}; ${selectedModel?.name ?? "model pending"}`}
              tone={selectedDevice && selectedRuntime && selectedModel ? "good" : "warn"}
            />
            <Button
              icon={<Rocket size={16} />}
              disabled={!canStageMissionPackage}
              onClick={stageMissionPackageRollout}
            >
              Stage package rollout
            </Button>
          </div>

          <EdgeRecommendationPanel
            recommendations={edgeRecommendations}
            selectedDeviceId={selectedDevice ? deviceId(selectedDevice) : ""}
            selectedModelId={selectedModel?.id ?? ""}
            selectedRuntimeId={selectedRuntime ? runtimeTargetId(selectedRuntime) : ""}
            onSelect={applyEdgeRecommendation}
          />

          <CapabilityDossier
            edgeRuntimeFit={edgeRuntimeFit}
            model={selectedModel}
            readiness={scopedReadiness}
            readinessVerdict={readinessVerdict}
            resourceEnvelopeFit={resourceEnvelopeFit}
            runtime={selectedRuntime}
            runtimeValidation={selectedRuntimeValidation}
            device={selectedDevice}
          />

          <div className="path-line" aria-label="Deployment readiness">
            <PathStep title="Model" value={selectedModel?.name ?? "Missing"} state={selectedModel ? "ready" : "blocked"} />
            <PathStep title="Runtime" value={selectedRuntime ? runtimeTargetId(selectedRuntime) : "Missing"} state={selectedRuntime ? "ready" : "blocked"} />
            <PathStep title="Edge" value={selectedDevice ? deviceId(selectedDevice) : "Missing"} state={selectedDevice ? "ready" : "blocked"} />
            <PathStep title="Runtime fit" value={runtimeFitDisplay.label} state={runtimeFitDisplay.tone} />
            <PathStep title="Resources" value={resourceEnvelopeFit.label} state={resourceEnvelopeFit.tone} />
            <PathStep title="Rollout" value={latestRollout?.state ?? "Not assigned"} state={latestRollout ? latestRollout.state ?? "pending" : "pending"} />
            <PathStep
              title="Evidence"
              value={proofEvents ? `${proofEvents} proof events` : `${snapshot.evidenceBundles.length} bundles`}
              state={evidenceValue ? "ready" : "pending"}
            />
          </div>

          <div className="readiness-grid edge-fit-grid" aria-label="On-device runtime capability fit">
            <ReadinessCard
              title="On-device runtime fit"
              value={runtimeFitDisplay.label}
              detail={runtimeFitDisplay.detail}
              state={runtimeFitDisplay.tone}
            />
            <ReadinessCard
              title="Runtime inventory"
              value={runtimeInventoryLabel(selectedDevice)}
              detail={runtimeInventoryDetail(selectedDevice)}
              state={edgeRuntimeFit.failures.length ? "bad" : runtimeInventoryTone(selectedDevice)}
            />
            <ReadinessCard
              title="Runtime target"
              value={selectedRuntime ? runtimeTargetId(selectedRuntime) : "missing"}
              detail={runtimeTargetCapabilityDetail(selectedRuntime)}
              state={selectedRuntime ? edgeRuntimeFit.tone : "bad"}
            />
            <ReadinessCard
              title="Performance SLO"
              value={performanceSloLabel(selectedModel)}
              detail={selectedModel ? performanceSloDetail(selectedModel) : "select a model"}
              state={performanceSloTone(selectedModel)}
            />
            <ReadinessCard
              title="Resource envelope"
              value={resourceEnvelopeFit.label}
              detail={resourceEnvelopeFit.detail}
              state={resourceEnvelopeFit.tone}
            />
            <ReadinessCard
              title="Field proof"
              value={
                selectedRuntimeValidation
                  ? "validated"
                  : selectedModel?.benchmarkDeviceId && benchmarkFreshness(selectedModel).state === "fresh"
                    ? "benchmarked"
                    : selectedModel?.benchmarkDeviceId
                      ? "stale proof"
                      : "pending"
              }
              detail={
                selectedRuntimeValidation
                  ? "package passed selected runtime target validation"
                  : selectedModel
                    ? `${formatBenchmark(selectedModel)}; ${formatBenchmarkFreshness(selectedModel)}`
                    : "no benchmark"
              }
              state={
                selectedRuntimeValidation ||
                (selectedModel?.benchmarkDeviceId && benchmarkFreshness(selectedModel).state === "fresh")
                  ? "good"
                  : "warn"
              }
            />
          </div>

          <details className="stage-inline-drawer">
            <summary>
              <span>
                <span className="section-kicker">Manual controls</span>
                <strong>Direct rollout and compatibility tools</strong>
              </span>
              <Badge value="advanced" />
            </summary>
            <div className="stage-inline-drawer-body">
              <form className="deploy-form" onSubmit={(event) => submitForm("assign-rollout", event)}>
                <input name="package_id" type="hidden" value={selectedModel?.packageId ?? ""} />
                <input name="model_id" type="hidden" value={selectedModel?.id ?? ""} />
                <label className="field">
                  <span>Edge node</span>
                  <select name="device_id" value={selectedDevice ? deviceId(selectedDevice) : ""} onChange={(event) => setSelectedDeviceId(event.target.value)} required>
                    {snapshot.devices.length ? null : <option value="">No edge nodes</option>}
                    {snapshot.devices.map((device) => (
                      <option key={deviceId(device)} value={deviceId(device)}>
                        {deviceId(device)} - {device.profile ?? "unknown profile"}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Runtime target</span>
                  <select name="runtime_target_id" value={selectedRuntime ? runtimeTargetId(selectedRuntime) : ""} onChange={(event) => setSelectedRuntimeId(event.target.value)}>
                    {snapshot.runtimeTargets.length ? null : <option value="">No runtime targets</option>}
                    {snapshot.runtimeTargets.map((target) => (
                      <option key={runtimeTargetId(target)} value={runtimeTargetId(target)}>
                        {runtimeTargetId(target)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Slot</span>
                  <input name="slot" defaultValue="vision" />
                </label>
                <label className="field">
                  <span>Rollout ID</span>
                  <input name="rollout_id" placeholder="auto-generated" />
                </label>
                <input name="actor" type="hidden" value="operator:mission-package-workbench" />
                <label className="check deploy-check">
                  <input name="require_approval" type="checkbox" defaultChecked />
                  <span>Require approval</span>
                </label>
                <Submit icon={<Rocket size={16} />} disabled={!selectedModel || !selectedDevice}>
                  Create rollout
                </Submit>
              </form>

              <form className="preview-form" onSubmit={(event) => submitForm("compatibility-preview", event)}>
                <input name="device_id" type="hidden" value={selectedDevice ? deviceId(selectedDevice) : ""} />
                <input name="package_id" type="hidden" value={selectedModel?.packageId ?? ""} />
                <input name="model_id" type="hidden" value={selectedModel?.id ?? ""} />
                <input name="runtime_target_id" type="hidden" value={selectedRuntime ? runtimeTargetId(selectedRuntime) : ""} />
                <Submit icon={<ShieldCheck size={16} />} variant="secondary" disabled={!selectedModel || !selectedDevice}>
                  Preview compatibility
                </Submit>
              </form>
            </div>
          </details>
        </section>
        ) : null}

        {activeHubStage === "deploy" ? (
        <section
          className={workflowClass("plans", "section section-wide rollout-plan-section deploy-secondary-section")}
          aria-labelledby="plans-heading"
          ref={plansWorkflowRef}
          tabIndex={-1}
        >
          <div className="section-header">
            <div>
              <span className="section-kicker">Rollout coordination</span>
              <h2 id="plans-heading">Stage selected model across the fleet</h2>
            </div>
            <span className="section-count">{missionRolloutPlans.length}</span>
          </div>

          <form className="rollout-plan-form" onSubmit={(event) => submitForm("create-rollout-plan", event)}>
            <input name="package_id" type="hidden" value={selectedModel?.packageId ?? ""} />
            <input name="model_id" type="hidden" value={selectedModel?.id ?? ""} />
            <input name="runtime_target_id" type="hidden" value={selectedRuntime ? runtimeTargetId(selectedRuntime) : ""} />
            <input name="actor" type="hidden" value="operator:mission-package-workbench" />
            <label className="field">
              <span>Plan ID</span>
              <input name="plan_id" placeholder="auto-generated" />
            </label>
            <label className="field">
              <span>Device IDs</span>
              <input name="device_ids" defaultValue={snapshot.devices.map(deviceId).join(",")} required />
            </label>
            <label className="field">
              <span>Slot</span>
              <input name="slot" defaultValue="vision" />
            </label>
            <label className="field">
              <span>Batch size</span>
              <input name="batch_size" type="number" min="1" defaultValue="1" />
            </label>
            <label className="check deploy-check">
              <input name="require_approval" type="checkbox" defaultChecked />
              <span>Require approval</span>
            </label>
            <label className="check deploy-check">
              <input name="require_runtime_validation" type="checkbox" />
              <span>Require validation</span>
            </label>
            <Submit icon={<GitBranch size={16} />} disabled={!selectedModel || !selectedDevice || !snapshot.devices.length}>
              Create plan
            </Submit>
          </form>

          <div className="rollout-list rollout-plan-list">
            {missionRolloutPlans.length ? (
              missionRolloutPlans.slice(0, 4).map((plan) => (
                <RolloutPlanRow
                  key={planId(plan)}
                  plan={plan}
                  onAdvance={advanceRolloutPlan}
                  onPause={pauseRolloutPlan}
                  onResume={resumeRolloutPlan}
                />
              ))
            ) : (
              <EmptyState title="No coordinated rollout plans" detail="Create a plan to stage selected models through approval and batch assignment." />
            )}
          </div>
        </section>
        ) : null}

        {activeHubStage === "deploy" ? (
        <section
          className={workflowClass("rollouts", "section rollout-section deploy-secondary-section")}
          aria-labelledby="rollouts-heading"
          ref={rolloutsWorkflowRef}
          tabIndex={-1}
        >
          <div className="section-header">
            <div>
              <span className="section-kicker">Rollouts</span>
              <h2 id="rollouts-heading">Approval and activation</h2>
            </div>
            <span className="section-count">{missionRollouts.length}</span>
          </div>
          <div className="rollout-list">
            {missionRollouts.length ? (
              missionRollouts.slice(0, 6).map((rollout) => (
                <RolloutRow
                  key={rolloutId(rollout)}
                  rollout={rollout}
                  onApprove={approveRollout}
                  onApply={applyRollout}
                  onRollback={rollbackRollout}
                />
              ))
            ) : (
              <EmptyState title="No rollouts assigned" detail="Create a rollout from the selected model to start activation." />
            )}
          </div>
        </section>
        ) : null}

        {activeHubStage === "deploy" ? (
        <section className="section fleet-section deploy-secondary-section" aria-labelledby="fleet-heading">
          <div className="section-header">
            <div>
              <span className="section-kicker">Fleet and runtimes</span>
              <h2 id="fleet-heading">Deployment targets</h2>
            </div>
            <span className="section-count">{snapshot.devices.length}</span>
          </div>
          <div className="compact-list">
            {snapshot.devices.map((device) => (
              <TargetRow key={deviceId(device)} label={deviceId(device)} detail={device.profile ?? "unknown profile"} status={device.status ?? "registered"} />
            ))}
            {snapshot.runtimeTargets.slice(0, 4).map((target) => (
              <TargetRow
                key={runtimeTargetId(target)}
                label={runtimeTargetId(target)}
                detail={`${target.arch ?? "arch unknown"} - ${target.device_profiles?.join(", ") || "any profile"}`}
                status="runtime"
              />
            ))}
          </div>
        </section>
        ) : null}

        {activeHubStage === "field" ? (
        <section
          className={workflowClass("evidence", "section evidence-section")}
          aria-labelledby="evidence-heading"
          ref={evidenceWorkflowRef}
          tabIndex={-1}
        >
          <div className="section-header">
            <div>
              <span className="section-kicker">Evidence</span>
              <h2 id="evidence-heading">Mission proof</h2>
            </div>
            <span className="section-count">
              {missionPhaseTotal ? `${completedMissionPhases}/${missionPhaseTotal}` : snapshot.evidenceBundles.length}
            </span>
          </div>
          <div className="button-row">
            <Button icon={<ShieldCheck size={16} />} onClick={() => exportEvidence("summary")}>
              Summary
            </Button>
            <Button icon={<GitBranch size={16} />} variant="secondary" onClick={() => exportEvidence("replay")}>
              Replay
            </Button>
            <Button icon={<Download size={16} />} variant="secondary" onClick={() => exportEvidence("full")}>
              Full bundle
            </Button>
            <Button icon={<Database size={16} />} variant="secondary" onClick={() => exportAirgap(true)}>
              Air-gap bundle
            </Button>
          </div>
          {missionPhases.length ? (
            <div className="mission-phase-list" aria-label="Mission replay phases">
              <div className="mission-phase-heading">
                <span>{snapshot.missionReplay?.headline ?? "mission replay"}</span>
                <strong>
                  {incompleteMissionPhases.length
                    ? `${incompleteMissionPhases.length} remaining`
                    : "complete"}
                </strong>
              </div>
              {missionPhases.map((phase) => (
                <MissionPhaseRow key={phase.phase ?? phase.label ?? phase.summary} phase={phase} />
	              ))}
	            </div>
	          ) : null}
	          {runtimeRepairProof ? <RuntimeRepairProofPanel compact proof={runtimeRepairProof} /> : null}
	          <div className="compact-list evidence-list">
            {snapshot.evidenceBundles.length ? (
              snapshot.evidenceBundles.slice(0, 4).map((record) => (
                <TargetRow
                  key={record.evidence_id ?? `${record.device_id}-${record.created_at}`}
                  label={record.evidence_id ?? "evidence"}
                  detail={`${record.device_id ?? "unknown device"} - ${compactDate(record.created_at)}`}
                  status={record.schema_version ?? "evidence"}
                />
              ))
            ) : proofEvents ? (
              <EvidenceSummaryRow
                headline={snapshot.evidenceSummary?.headline ?? "mission proof ready"}
                events={proofEvents}
                signedImports={signedEvidenceImports}
              />
            ) : (
              <EmptyState title="No evidence yet" detail="Export or ingest evidence after rollout activity." />
            )}
          </div>
        </section>
        ) : null}

      </main>
      ) : null}

      {preview ? <PreviewPanel preview={preview} onClear={() => setPreview(undefined)} /> : null}
    </div>
  );
}

function EdgeRuntimeWorkbench({
  devices,
  edgeExecutionContract,
  models,
  onCopyCommand,
  onGenerateProof,
  onGoHandling,
  onGoModels,
  onSelectDevice,
  onSelectRuntime,
  readiness,
  resourceEnvelopeFit,
  runtimeDecision,
  runtimeFitDisplay,
  runtimeTargets,
  runtimeValidations,
  selectedDevice,
  selectedModel,
  selectedRuntime
}: {
  devices: Device[];
  edgeExecutionContract: JsonObject;
  models: ModelRecord[];
  onCopyCommand: (label: string, command: string) => void;
  onGenerateProof: () => void;
  onGoHandling: () => void;
  onGoModels: () => void;
  onSelectDevice: (id: string) => void;
  onSelectRuntime: (id: string) => void;
  readiness: DeploymentReadiness | undefined;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtimeDecision: JsonObject;
  runtimeFitDisplay: RuntimeFitDisplay;
  runtimeTargets: RuntimeTarget[];
  runtimeValidations: RuntimeValidation[];
  selectedDevice: Device | undefined;
  selectedModel: ModelRecord | undefined;
  selectedRuntime: RuntimeTarget | undefined;
}): JSX.Element {
  const contract = Object.keys(edgeExecutionContract).length ? edgeExecutionContract : runtimeDecision;
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const targetSelection = Object.keys(asRecord(contract.target_selection)).length
    ? asRecord(contract.target_selection)
    : asRecord(runtimeFit.target_selection);
  const selectedRuntimeTargetId = stringOf(
    targetSelection.selected_runtime_target_id,
    selectedRuntime ? runtimeTargetId(selectedRuntime) : ""
  );
  const explicitBestRuntimeTargetId = stringOf(targetSelection.best_runtime_target_id, "");
  const candidates = runtimeDecisionCandidates(
    contract,
    runtimeFit,
    selectedRuntimeTargetId,
    explicitBestRuntimeTargetId || selectedRuntimeTargetId
  );
  const assessments = runtimeTargetAssessments(contract, runtimeFit, candidates);
  const runtimeWorkbench = asRecord(readiness?.runtime_workbench);
  const rows = runtimeWorkbenchRows({
    assessments,
    device: selectedDevice,
    model: selectedModel,
    runtimeFit,
    runtimeWorkbench,
    runtimeTargets,
    runtimeValidations,
    selectedRuntimeTargetId,
    bestRuntimeTargetId: explicitBestRuntimeTargetId
  });
  const bestRow = rows.find((row) => row.best) ?? rows[0];
  const selectedRow = rows.find((row) => row.selected);
  const remediationContext: RuntimeRemediationContext = {
    packageId: selectedModel?.packageId ?? "",
    modelId: selectedModel?.id ?? "",
    deviceId: selectedDevice ? deviceId(selectedDevice) : "",
    slot: "vision"
  };
  const proofDisabled = !selectedModel || !selectedDevice || !selectedRuntime;
  const modelRuntimeRequirements = [
    selectedModel?.runtimes.length ? `runtime ${selectedModel.runtimes.join(", ")}` : "",
    selectedModel?.providers.length ? `provider ${selectedModel.providers.join(", ")}` : "",
    selectedModel?.profiles.length ? `profile ${selectedModel.profiles.join(", ")}` : "",
    formatArtifactSizeMb(selectedModel?.artifactSizeMb)
  ].filter(Boolean);
  const selectedLane = selectedRow ? asRecord(selectedRow.target.runtime_lane) : runtimeLaneFor(runtimeFit, selectedRuntime);
  const artifactLane = asRecord(runtimeFit.artifact_lane);
  const capabilityLock = runtimeCapabilityLockForProof(readiness);

  return (
    <section className="runtime-workbench" aria-labelledby="runtime-workbench-heading" data-testid="runtime-workbench">
      <div className="runtime-workbench-header">
        <div>
          <span className="section-kicker">Runtime workbench</span>
          <h2 id="runtime-workbench-heading">Target the model to the edge runtime</h2>
          <p>
            Compare the model selected in Model Plan against live edge inventory, runtime target validation,
            benchmark freshness, resource limits, and signed-proof gates.
          </p>
        </div>
        <div className="runtime-workbench-verdict">
          <Badge value={bestRow ? `best ${bestRow.targetId}` : "target pending"} />
          <strong>{selectedRow?.score !== undefined ? `${selectedRow.score}/100` : runtimeFitDisplay.label}</strong>
          <small>{selectedRow?.detail ?? runtimeFitDisplay.detail}</small>
        </div>
      </div>

      <div className="runtime-workbench-controls" aria-label="Runtime path controls">
        <div
          aria-label="Selected model from Model Plan"
          className="runtime-workbench-model-context"
          data-testid="runtime-workbench-model"
          id="runtime-workbench-model"
        >
          <div>
            <span>Selected model</span>
            <strong>{selectedModel?.name ?? "Model pending"}</strong>
            <small>
              {selectedModel
                ? `${selectedModel.id} / ${selectedModel.packageId}`
                : models.length
                  ? "Open Model Plan to choose a signed model"
                  : "No signed models registered"}
            </small>
          </div>
          <Button icon={<ArrowLeft size={16} />} variant="secondary" onClick={onGoModels}>
            Model Plan
          </Button>
        </div>
        <label className="field" htmlFor="runtime-workbench-edge-node">
          <span>Edge node</span>
          <select
            aria-label="Edge node"
            data-testid="runtime-workbench-edge-node"
            id="runtime-workbench-edge-node"
            value={selectedDevice ? deviceId(selectedDevice) : ""}
            onChange={(event) => onSelectDevice(event.target.value)}
          >
            {devices.length ? null : <option value="">No edge nodes</option>}
            {devices.map((device) => (
              <option key={deviceId(device)} value={deviceId(device)}>
                {deviceId(device)} - {device.profile ?? "unknown profile"}
              </option>
            ))}
          </select>
        </label>
        <label className="field" htmlFor="runtime-workbench-target-runtime">
          <span>Target runtime</span>
          <select
            aria-label="Target runtime"
            data-testid="runtime-workbench-target-runtime"
            id="runtime-workbench-target-runtime"
            value={selectedRuntime ? runtimeTargetId(selectedRuntime) : ""}
            onChange={(event) => onSelectRuntime(event.target.value)}
          >
            {runtimeTargets.length ? null : <option value="">No runtime targets</option>}
            {runtimeTargets.map((target) => (
              <option key={runtimeTargetId(target)} value={runtimeTargetId(target)}>
                {runtimeTargetId(target)}
              </option>
            ))}
          </select>
        </label>
        <Button
          ariaLabel="Generate runtime proof for selected edge path"
          icon={<FileCheck2 size={16} />}
          testId="runtime-workbench-generate-proof"
          disabled={proofDisabled}
          onClick={onGenerateProof}
        >
          Generate proof
        </Button>
        <Button
          ariaLabel="Continue to Sensor Handling"
          icon={<Activity size={16} />}
          testId="runtime-workbench-go-handling"
          disabled={proofDisabled}
          onClick={onGoHandling}
        >
          Continue to Sensor Handling
        </Button>
      </div>

      <div className="runtime-capability-strip" aria-label="On-device runtime capability vector">
        <CapabilityMetric
          label="Runtime image"
          value={runtimeTargetImageValue(selectedRuntime)}
          detail={runtimeTargetImageDetail(selectedRuntime)}
          tone={selectedRuntime ? "good" : "bad"}
        />
        <CapabilityMetric
          label="Provider match"
          value={runtimeProviderValue(selectedLane)}
          detail={runtimeProviderDetail(selectedLane, selectedDevice)}
          tone={runtimeProviderTone(selectedLane, selectedDevice)}
        />
        <CapabilityMetric
          label="Artifact lane"
          value={artifactLaneValue(artifactLane)}
          detail={artifactLaneDetail(artifactLane)}
          tone={artifactLaneTone(artifactLane)}
        />
        <CapabilityMetric
          label="Capability lock"
          value={capabilityLockValue(capabilityLock)}
          detail={capabilityLockDetail(capabilityLock)}
          tone={capabilityLockTone(capabilityLock)}
        />
      </div>

      <div className="runtime-workbench-summary" aria-label="Selected edge runtime summary">
        <CapabilityMetric
          label="Selected fit"
          value={selectedRow?.score !== undefined ? `${selectedRow.score}/100` : runtimeFitDisplay.label}
          detail={selectedRow?.detail ?? runtimeFitDisplay.detail}
          tone={selectedRow?.tone ?? runtimeFitDisplay.tone}
        />
        <CapabilityMetric
          label="Best target"
          value={bestRow?.targetId ?? "pending"}
          detail={bestRow ? `${bestRow.status}; ${bestRow.detail}` : "runtime alternatives pending"}
          tone={bestRow?.tone ?? "neutral"}
        />
        <CapabilityMetric
          label="Model constraints"
          value={selectedModel?.format ?? "missing"}
          detail={modelRuntimeRequirements.join(" / ") || "model runtime constraints not declared"}
          tone={selectedModel ? "good" : "bad"}
        />
        <CapabilityMetric
          label="Edge inventory"
          value={runtimeInventoryLabel(selectedDevice)}
          detail={runtimeInventoryDetail(selectedDevice)}
          tone={runtimeInventoryTone(selectedDevice)}
        />
        <CapabilityMetric
          label="Resources"
          value={resourceEnvelopeFit.label}
          detail={resourceEnvelopeFit.detail}
          tone={resourceEnvelopeFit.tone}
        />
      </div>

      <div className="runtime-workbench-table" aria-label="Ranked target runtimes">
        <div className="runtime-workbench-table-head">
          <span>Target</span>
          <span>Fit</span>
          <span>Lane</span>
          <span>Proof</span>
          <span>Action</span>
        </div>
        {rows.length ? (
          rows.map((row) => (
            <div
              aria-label={`${row.targetId} runtime target ${row.status}`}
              aria-selected={row.selected}
              className={`runtime-workbench-row runtime-workbench-row-${row.tone}${
                row.selected ? " runtime-workbench-row-selected" : ""
              }`}
              data-runtime-target-id={row.targetId}
              data-testid={`runtime-workbench-row-${row.targetId}`}
              key={row.targetId}
            >
              <div>
                <strong>{row.targetId}</strong>
                <small>
                  {row.selected ? "selected" : row.best ? "best alternate" : row.status}
                  {row.best && row.selected ? " best" : ""}
                </small>
              </div>
              <div>
                <strong>{row.score !== undefined ? `${row.score}/100` : row.status}</strong>
                <small>{row.detail}</small>
              </div>
              <div>
                <strong>{row.lane}</strong>
                <small>{runtimeTargetCapabilityDetail(row.target)}</small>
              </div>
              <div>
                <strong>{row.validated ? "validated" : row.compatible ? "needs proof" : "blocked"}</strong>
                <small>{row.benchmark} / {row.inventory}</small>
              </div>
              <div className="runtime-workbench-row-action">
                <button
                  aria-label={
                    row.selected
                      ? `${row.targetId} is the selected runtime target`
                      : `Select runtime target ${row.targetId}`
                  }
                  className="button-mini"
                  data-testid={`runtime-workbench-select-${row.targetId}`}
                  disabled={row.selected}
                  type="button"
                  onClick={() => onSelectRuntime(row.targetId)}
                >
                  {row.selected ? "Selected" : "Select"}
                </button>
              </div>
            </div>
          ))
        ) : (
          <EmptyState title="No runtime targets" detail="Register target runtimes to compare deployment paths." />
        )}
      </div>

      {rows.length ? (
        <RuntimeDecisionTrace
          context={remediationContext}
          onCopyCommand={onCopyCommand}
          rows={rows}
        />
      ) : null}
    </section>
  );
}

function RuntimeDecisionTrace({
  context,
  onCopyCommand,
  rows
}: {
  context: RuntimeRemediationContext;
  onCopyCommand: (label: string, command: string) => void;
  rows: RuntimeWorkbenchRow[];
}): JSX.Element {
  return (
    <details className="runtime-decision-trace" data-testid="runtime-decision-trace">
      <summary className="runtime-decision-trace-header">
        <div>
          <span className="section-kicker">Runtime decision trace</span>
          <strong>Ranked on-device capability proof</strong>
        </div>
        <Badge value={`${rows.filter((row) => row.compatible).length}/${rows.length} eligible`} />
      </summary>
      <div className="runtime-decision-trace-grid">
        {rows.map((row) => (
          <RuntimeDecisionTraceItem
            context={context}
            key={row.targetId}
            onCopyCommand={onCopyCommand}
            row={row}
          />
        ))}
      </div>
    </details>
  );
}

function RuntimeDecisionTraceItem({
  context,
  onCopyCommand,
  row
}: {
  context: RuntimeRemediationContext;
  onCopyCommand: (label: string, command: string) => void;
  row: RuntimeWorkbenchRow;
}): JSX.Element {
  const command = runtimeWorkbenchRowRemediationCommand(row, context);
  const reason = runtimeWorkbenchTraceReason(row);
  return (
    <article className={`runtime-decision-trace-item runtime-decision-trace-item-${row.tone}`}>
      <div className="runtime-decision-trace-topline">
        <div>
          <span>{runtimeWorkbenchTraceRank(row)}</span>
          <strong>{row.targetId}</strong>
        </div>
        <Badge value={runtimeWorkbenchTraceBadge(row)} />
      </div>
      <p>{reason}</p>
      <div className="runtime-decision-trace-metrics">
        {row.traceMetrics.map((metric) => (
          <div className={`runtime-decision-trace-metric runtime-decision-trace-metric-${metric.tone}`} key={`${row.targetId}-${metric.label}`}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
            <small>{metric.detail}</small>
          </div>
        ))}
      </div>
      <div className="runtime-decision-trace-command">
        <div>
          <span>{row.actionRequiresEdge ? "edge-run action" : "operator action"}</span>
          <strong>{row.actionLabel || row.actionKind || "Review runtime path"}</strong>
          <small>{command?.note || runtimeWorkbenchTraceActionDetail(row)}</small>
        </div>
        {command ? (
          <button className="button-mini" type="button" onClick={() => onCopyCommand(command.label, command.command)}>
            <Clipboard size={13} />
            Copy
          </button>
        ) : null}
      </div>
    </article>
  );
}

function EdgeOperatorCommandPanel({
  device,
  edgeExecutionContract,
  model,
  proofWorkflow,
  readiness,
  runtime,
  runtimeDecision,
  runtimeFitDisplay
}: {
  device: Device | undefined;
  edgeExecutionContract: JsonObject;
  model: ModelRecord | undefined;
  proofWorkflow: EdgeProofWorkflow;
  readiness: DeploymentReadiness | undefined;
  runtime: RuntimeTarget | undefined;
  runtimeDecision: JsonObject;
  runtimeFitDisplay: RuntimeFitDisplay;
}): JSX.Element {
  const contract = Object.keys(edgeExecutionContract).length ? edgeExecutionContract : runtimeDecision;
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const decisionFit = asRecord(contract.runtime_fit);
  const targetSelection = Object.keys(asRecord(contract.target_selection)).length
    ? asRecord(contract.target_selection)
    : asRecord(runtimeFit.target_selection);
  const contractPath = asRecord(contract.path);
  const selectedRuntimeTargetId = stringOf(
    targetSelection.selected_runtime_target_id,
    stringOf(contractPath.runtime_target_id, runtime ? runtimeTargetId(runtime) : "")
  );
  const bestRuntimeTargetId = stringOf(
    targetSelection.best_runtime_target_id,
    selectedRuntimeTargetId
  );
  const selectedScore =
    numberOf(decisionFit.score) ??
    numberOf(runtimeFit.score) ??
    runtimeFitScoreForProof(readiness, runtimeFitDisplay);
  const candidates = runtimeDecisionCandidates(contract, runtimeFit, selectedRuntimeTargetId, bestRuntimeTargetId);
  const targetAssessments = runtimeTargetAssessments(contract, runtimeFit, candidates);
  const targetCoverage = targetRuntimeCoverageSummary(targetAssessments);
  const runtimeLaneItems = operatorRuntimeLaneItems(
    targetAssessments,
    selectedRuntimeTargetId,
    bestRuntimeTargetId
  );
  const productionAdmission = Object.keys(asRecord(contract.production_admission)).length
    ? asRecord(contract.production_admission)
    : asRecord(readiness?.production_admission);
  const modelId = model?.id ?? stringOf(contractPath.model_id, "model missing");
  const runtimeId = selectedRuntimeTargetId || (runtime ? runtimeTargetId(runtime) : "runtime missing");
  const edgeId = device ? deviceId(device) : stringOf(contractPath.device_id, "edge missing");
  const pathLabel = [modelId, runtimeId, edgeId].join(" -> ");
  const selectedIsBest = runtimeId === bestRuntimeTargetId;
  const statusDetail = [
    selectedScore !== undefined ? `${selectedScore}/100 runtime fit` : runtimeFitDisplay.label,
    selectedIsBest ? "selected runtime is best" : bestRuntimeTargetId ? `best runtime ${bestRuntimeTargetId}` : "",
    targetCoverage.detail,
    proofWorkflow.missing.length ? "proof context incomplete" : "signed proof ready"
  ].filter(Boolean).join(" / ");
  const proofValue = proofWorkflow.missing.length
    ? `${proofWorkflow.missing.length} missing`
    : proofWorkflow.attestation;
  const tone = proofWorkflow.missing.length
    ? "warn"
    : productionAdmissionTone(productionAdmission) === "bad"
      ? "bad"
      : runtimeFitDisplay.tone;

  return (
    <section className={`operator-command operator-command-${tone}`} aria-labelledby="operator-command-heading">
      <div className="operator-command-copy">
        <span className="section-kicker">On-device runtime proof</span>
        <h2 id="operator-command-heading">{pathLabel}</h2>
        <p>{statusDetail}</p>
      </div>
      <div className="operator-command-badges" aria-label="Active edge path status">
        <Badge value={runtimeFitDisplay.label} />
        <Badge value={selectedIsBest ? "best target" : "retarget available"} />
        <Badge value={proofWorkflow.gatePolicy} />
      </div>
      <div className="operator-command-grid" aria-label="Active model runtime edge proof">
        <OperatorCommandMetric
          detail={model ? `${model.packageId} / ${model.format}` : "select a model"}
          label="Model"
          tone={model ? "good" : "bad"}
          value={modelId}
        />
        <OperatorCommandMetric
          detail={runtime ? runtimeTargetCapabilityDetail(runtime) : "select a runtime target"}
          label="Runtime target"
          tone={runtime ? runtimeFitDisplay.tone : "bad"}
          value={runtimeId}
        />
        <OperatorCommandMetric
          detail={device ? `${device.profile ?? "unknown profile"} / ${device.status ?? "registered"}` : "select an edge"}
          label="Device inventory"
          tone={device ? runtimeInventoryTone(device) : "bad"}
          value={edgeId}
        />
        <OperatorCommandMetric
          detail={targetCoverage.detail}
          label="Runtime coverage"
          tone={targetCoverage.tone}
          value={targetCoverage.value}
        />
        <OperatorCommandMetric
          detail={productionAdmissionDetail(productionAdmission)}
          label="Field admission"
          tone={productionAdmissionTone(productionAdmission)}
          value={productionAdmissionValue(productionAdmission)}
        />
        <OperatorCommandMetric
          detail={proofWorkflow.proofPath}
          label="Signed proof"
          tone={proofWorkflow.tone}
          value={proofValue}
        />
      </div>
      {runtimeLaneItems.length ? (
        <div className="operator-command-lanes" aria-label="Runtime target alternatives">
          {runtimeLaneItems.map((item) => (
            <div key={item.id} className={`operator-command-lane operator-command-lane-${item.tone}`}>
              <div className="operator-command-lane-topline">
                <span>{item.status}</span>
                <strong>{item.id}</strong>
              </div>
              <small>{item.detail}</small>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function OperatorCommandMetric({
  detail,
  label,
  tone,
  value
}: {
  detail: string;
  label: string;
  tone: GateTone;
  value: string;
}): JSX.Element {
  return (
    <div className={`operator-command-metric operator-command-metric-${tone}`}>
      <span>{label}</span>
      <strong>{value || "-"}</strong>
      <small>{detail}</small>
    </div>
  );
}

function PathStep({ title, value, state }: { title: string; value: string; state: string }): JSX.Element {
  return (
    <div className={`path-step path-step-${toneForPath(state)}`}>
      <span>{title}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ReadinessCard({
  title,
  value,
  detail,
  state
}: {
  title: string;
  value: string;
  detail: string;
  state: string;
}): JSX.Element {
  return (
    <div className={`readiness-card readiness-card-${toneForPath(state)}`}>
      <span>{title}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function EdgeRuntimeMissionPanel({ mission }: { mission: EdgeRuntimeMission }): JSX.Element {
  return (
    <section className={`edge-mission edge-mission-${mission.tone}`} aria-labelledby="edge-mission-heading">
      <div className="edge-mission-header">
        <div>
          <span className="section-kicker">Edge runtime mission</span>
          <h2 id="edge-mission-heading">{mission.headline}</h2>
          <p>{mission.detail}</p>
        </div>
        <code>{mission.path}</code>
      </div>
      <div className="edge-mission-grid" aria-label="Selected on-device runtime proof">
        {mission.metrics.map((metric) => (
          <div className={`edge-mission-metric edge-mission-metric-${metric.tone}`} key={metric.label}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
            <small>{metric.detail}</small>
          </div>
        ))}
      </div>
      <div className="edge-mission-focus" aria-label="Operator focus">
        {mission.focus.map((item) => (
          <span key={item}>{item}</span>
        ))}
      </div>
    </section>
  );
}

function EdgeExecutionContractPanel({
  device,
  edgeExecutionContract,
  edgeRuntimeFit,
  model,
  onCopyRemediation,
  onSelectRuntimeTarget,
  readiness,
  readinessVerdict,
  resourceEnvelopeFit,
  runtime,
  runtimeDecision,
  runtimeFitDisplay,
  runtimeValidation
}: {
  device: Device | undefined;
  edgeExecutionContract: JsonObject;
  edgeRuntimeFit: EdgeRuntimeFit;
  model: ModelRecord | undefined;
  onCopyRemediation: (label: string, command: string) => void;
  onSelectRuntimeTarget: (runtimeTargetIdValue: string) => void;
  readiness: DeploymentReadiness | undefined;
  readinessVerdict: ReadinessVerdict;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtime: RuntimeTarget | undefined;
  runtimeDecision: JsonObject;
  runtimeFitDisplay: RuntimeFitDisplay;
  runtimeValidation: RuntimeValidation | undefined;
}): JSX.Element {
  const contract = Object.keys(edgeExecutionContract).length ? edgeExecutionContract : runtimeDecision;
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const decisionFit = asRecord(contract.runtime_fit);
  const targetSelection = Object.keys(asRecord(contract.target_selection)).length
    ? asRecord(contract.target_selection)
    : asRecord(runtimeFit.target_selection);
  const contractPath = asRecord(contract.path);
  const remediationContext: RuntimeRemediationContext = {
    packageId: model?.packageId ?? stringOf(contractPath.package_id, ""),
    modelId: model?.id ?? stringOf(contractPath.model_id, ""),
    deviceId: device ? deviceId(device) : stringOf(contractPath.device_id, ""),
    slot: stringOf(contractPath.slot, "vision")
  };
  const selectedRuntimeTargetId = stringOf(
    targetSelection.selected_runtime_target_id,
    stringOf(contractPath.runtime_target_id, runtime ? runtimeTargetId(runtime) : "runtime missing")
  );
  const bestRuntimeTargetId = stringOf(
    targetSelection.best_runtime_target_id,
    selectedRuntimeTargetId
  );
  const selectedScore =
    numberOf(decisionFit.score) ??
    numberOf(runtimeFit.score) ??
    runtimeFitScoreForProof(readiness, runtimeFitDisplay);
  const bestScore = numberOf(targetSelection.best_score);
  const scoreDelta = numberOf(targetSelection.score_delta);
  const productionAdmission = Object.keys(asRecord(contract.production_admission)).length
    ? asRecord(contract.production_admission)
    : asRecord(readiness?.production_admission);
  const selectedLane = Object.keys(asRecord(contract.selected_runtime_lane)).length
    ? asRecord(contract.selected_runtime_lane)
    : runtimeLaneFor(runtimeFit, runtime);
  const bestLane = asRecord(contract.best_runtime_lane);
  const artifactLane = Object.keys(asRecord(contract.artifact_lane)).length
    ? asRecord(contract.artifact_lane)
    : asRecord(runtimeFit.artifact_lane);
  const capabilityLock = Object.keys(asRecord(contract.runtime_capability_lock)).length
    ? asRecord(contract.runtime_capability_lock)
    : asRecord(runtimeFit.runtime_capability_lock);
  const recommendedAction = stringOf(
    contract.recommended_action,
    readinessVerdict.label === "go" ? "apply_or_stage" : "review"
  );
  const decisionStatus = stringOf(targetSelection.status, stringOf(contract.status, readinessVerdict.label));
  const actionLabel = runtimeDecisionActionLabel(recommendedAction);
  const decisionDetail = compactMetricDetail(
    stringOf(contract.detail, readinessVerdict.nextAction)
  );
  const tone = executionContractTone({
    action: recommendedAction,
    decisionStatus,
    productionAdmission,
    readinessVerdict
  });
  const candidates = runtimeDecisionCandidates(contract, runtimeFit, selectedRuntimeTargetId, bestRuntimeTargetId);
  const targetAssessments = runtimeTargetAssessments(contract, runtimeFit, candidates);
  const blockingGates = runtimeDecisionGates(contract.blocking_gates);
  const attentionGates = runtimeDecisionGates(contract.attention_gates);
  const canSelectBest =
    bestRuntimeTargetId &&
    selectedRuntimeTargetId &&
    bestRuntimeTargetId !== selectedRuntimeTargetId &&
    !bestRuntimeTargetId.includes("missing");

  return (
    <section className={`execution-contract execution-contract-${tone}`} aria-labelledby="execution-contract-heading">
      <div className="execution-contract-header">
        <div>
          <span className="section-kicker">Edge execution contract</span>
          <h2 id="execution-contract-heading">
            {executionContractHeadline(recommendedAction, decisionStatus, readinessVerdict)}
          </h2>
          <p>{decisionDetail}</p>
        </div>
        <div className="execution-contract-decision" aria-label="Runtime decision">
          <Badge value={actionLabel} />
          <strong>
            {selectedRuntimeTargetId}
            {bestRuntimeTargetId && bestRuntimeTargetId !== selectedRuntimeTargetId
              ? ` -> ${bestRuntimeTargetId}`
              : ""}
          </strong>
          <small>
            {selectedScore !== undefined ? `${selectedScore}/100 selected` : runtimeFitDisplay.label}
            {bestScore !== undefined && bestRuntimeTargetId !== selectedRuntimeTargetId
              ? ` / ${bestScore}/100 best`
              : ""}
            {scoreDelta !== undefined && scoreDelta > 0 ? ` / +${formatMetricNumber(scoreDelta)} fit` : ""}
          </small>
          {canSelectBest ? (
            <Button
              icon={<GitBranch size={16} />}
              variant="secondary"
              onClick={() => onSelectRuntimeTarget(bestRuntimeTargetId)}
            >
              Use best runtime
            </Button>
          ) : null}
        </div>
      </div>

      <div className="execution-path" aria-label="Selected model runtime edge path">
        <ExecutionPathNode label="Model" value={model?.id ?? "missing"} detail={model?.format ?? "artifact"} tone={model ? "good" : "bad"} />
        <ExecutionPathNode
          label="Runtime"
          value={runtime ? runtimeTargetId(runtime) : "missing"}
          detail={runtimeLaneValue(selectedLane)}
          tone={runtime ? runtimeLaneTone(selectedLane) : "bad"}
        />
        <ExecutionPathNode
          label="Edge"
          value={device ? deviceId(device) : "missing"}
          detail={device?.profile ?? runtimeInventoryLabel(device)}
          tone={device ? runtimeInventoryTone(device) : "bad"}
        />
      </div>

      <div className="execution-contract-grid" aria-label="On-device runtime capabilities">
        <CapabilityMetric
          label="Fit score"
          value={selectedScore !== undefined ? `${selectedScore}/100` : runtimeFitDisplay.label}
          detail={runtimeFitDisplay.detail}
          tone={runtimeFitDisplay.tone}
        />
        <CapabilityMetric
          label="Runtime lane"
          value={runtimeLaneValue(selectedLane)}
          detail={bestRuntimeTargetId !== selectedRuntimeTargetId && Object.keys(bestLane).length
            ? `best lane: ${runtimeLaneValue(bestLane)}`
            : runtimeLaneDetail(selectedLane)}
          tone={runtimeLaneTone(selectedLane)}
        />
        <CapabilityMetric
          label="Artifact path"
          value={artifactLaneValue(artifactLane)}
          detail={artifactLaneDetail(artifactLane)}
          tone={artifactLaneTone(artifactLane)}
        />
        <CapabilityMetric
          label="Capability lock"
          value={capabilityLockValue(capabilityLock)}
          detail={capabilityLockDetail(capabilityLock)}
          tone={capabilityLockTone(capabilityLock)}
        />
        <CapabilityMetric
          label="Resources"
          value={resourceEnvelopeFit.label}
          detail={resourceEnvelopeFit.detail}
          tone={resourceEnvelopeFit.tone}
        />
        <CapabilityMetric
          label="Admission"
          value={productionAdmissionValue(productionAdmission)}
          detail={productionAdmissionDetail(productionAdmission)}
          tone={productionAdmissionTone(productionAdmission)}
        />
      </div>

      <div className="execution-evidence-grid">
        <div className="execution-runtime-board" aria-label="Target runtime coverage">
          <div className="execution-subheader">
            <strong>Target runtime coverage</strong>
            <span>{targetAssessments.length ? `${targetAssessments.length} assessed` : "pending"}</span>
          </div>
          <div className="execution-candidate-list">
            {targetAssessments.length ? (
              targetAssessments.slice(0, 6).map((assessment) => (
                <TargetRuntimeAssessmentRow
                  key={`${candidateRuntimeId(assessment)}-${stringOf(assessment.status, "status")}`}
                  assessment={assessment}
                  bestRuntimeTargetId={bestRuntimeTargetId}
                  context={remediationContext}
                  onCopyRemediation={onCopyRemediation}
                  selectedRuntimeTargetId={selectedRuntimeTargetId}
                />
              ))
            ) : (
              <EmptyState title="No target coverage" detail="Runtime target assessments will appear after readiness evaluates this model and edge." />
            )}
          </div>
        </div>

        <div className="execution-runtime-board" aria-label="Measured runtime candidates">
          <div className="execution-subheader">
            <strong>Measured runtime candidates</strong>
            <span>{candidates.length ? `${candidates.length} ranked` : "pending"}</span>
          </div>
          <div className="execution-candidate-list">
            {candidates.length ? (
              candidates.map((candidate) => (
                <RuntimeCandidateRow
                  key={`${candidateRuntimeId(candidate)}-${stringOf(candidate.rank, "rank")}`}
                  bestRuntimeTargetId={bestRuntimeTargetId}
                  candidate={candidate}
                  selectedRuntimeTargetId={selectedRuntimeTargetId}
                />
              ))
            ) : (
              <EmptyState title="No measured candidates" detail="Record on-device benchmark and validation evidence for this model/runtime path." />
            )}
          </div>
        </div>

        <div className="execution-gate-board" aria-label="Runtime blockers and evidence gaps">
          <div className="execution-subheader">
            <strong>Runtime blockers and evidence gaps</strong>
            <span>{blockingGates.length + attentionGates.length || "clear"}</span>
          </div>
          <div className="execution-gate-list">
            {[...blockingGates, ...attentionGates].length ? (
              [...blockingGates, ...attentionGates].slice(0, 5).map((gate) => (
                <div className={`execution-gate execution-gate-${toneForReadinessStatus(stringOf(gate.status, ""))}`} key={`${stringOf(gate.gate_id, "gate")}-${stringOf(gate.status, "status")}`}>
                  <span>{stringOf(gate.label, stringOf(gate.gate_id, "Gate"))}</span>
                  <strong>{displayGateState(stringOf(gate.state, stringOf(gate.status, "review")))}</strong>
                  <small>{compactMetricDetail(stringOf(gate.detail, "Review gate evidence"))}</small>
                </div>
              ))
            ) : (
              <div className="execution-gate execution-gate-good">
                <span>Runtime gates</span>
                <strong>Aligned</strong>
                <small>
                  {runtimeValidation
                    ? `${selectedRuntimeTargetId} validation and admission evidence are available`
                    : edgeRuntimeFit.detail}
                </small>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

function ExecutionPathNode({
  detail,
  label,
  tone,
  value
}: {
  detail: string;
  label: string;
  tone: GateTone;
  value: string;
}): JSX.Element {
  return (
    <div className={`execution-path-node execution-path-node-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function RuntimeCandidateRow({
  bestRuntimeTargetId,
  candidate,
  selectedRuntimeTargetId
}: {
  bestRuntimeTargetId: string;
  candidate: JsonObject;
  selectedRuntimeTargetId: string;
}): JSX.Element {
  const id = candidateRuntimeId(candidate);
  const lane = asRecord(candidate.runtime_lane);
  const score = numberOf(candidate.score);
  const latency = numberOf(candidate.latency_ms_p95);
  const throughput = numberOf(candidate.throughput_ips);
  const tier = stringOf(candidate.tier, "fit").replace(/_/g, " ");
  const labels = [
    id === selectedRuntimeTargetId ? "selected" : "",
    id === bestRuntimeTargetId ? "best" : "",
    stringOf(candidate.blocked, "") === "true" ? "blocked" : ""
  ].filter(Boolean);
  return (
    <div className={`execution-candidate execution-candidate-${runtimeCandidateTone(candidate, id, selectedRuntimeTargetId, bestRuntimeTargetId)}`}>
      <div>
        <span>{numberOf(candidate.rank) !== undefined ? `#${candidate.rank}` : "candidate"}</span>
        <strong>{id}</strong>
        <small>
          {score !== undefined ? `${score}/100 ${tier}` : tier}
          {latency !== undefined ? ` / ${formatMetricNumber(latency)} ms p95` : ""}
          {throughput !== undefined ? ` / ${formatThroughput(throughput)} ips` : ""}
        </small>
      </div>
      <div className="execution-candidate-meta">
        {labels.map((label) => (
          <Badge key={label} value={label} />
        ))}
        <small>{runtimeLaneValue(lane)}</small>
      </div>
    </div>
  );
}

function TargetRuntimeAssessmentRow({
  assessment,
  bestRuntimeTargetId,
  context,
  onCopyRemediation,
  selectedRuntimeTargetId
}: {
  assessment: JsonObject;
  bestRuntimeTargetId: string;
  context: RuntimeRemediationContext;
  onCopyRemediation: (label: string, command: string) => void;
  selectedRuntimeTargetId: string;
}): JSX.Element {
  const id = candidateRuntimeId(assessment);
  const lane = asRecord(assessment.runtime_lane);
  const score = numberOf(assessment.score);
  const status = stringOf(
    assessment.status,
    assessment.blocked === true ? "blocked" : "eligible"
  ).replace(/_/g, " ");
  const remediation = asRecord(assessment.remediation);
  const remediationCommand = runtimeTargetAssessmentRemediationCommand(assessment, context);
  const componentProofs = runtimeTargetComponentProofs(assessment);
  const labels = [
    id === selectedRuntimeTargetId || assessment.selected === true ? "selected" : "",
    id === bestRuntimeTargetId || assessment.best === true ? "best" : "",
    status,
    remediation.requires_edge_execution === true ? "edge-run" : ""
  ].filter(Boolean);
  return (
    <div className={`execution-candidate execution-candidate-${targetAssessmentTone(assessment)}`}>
      <div>
        <span>{runtimeLaneValue(lane)}</span>
        <strong>{id}</strong>
        <small>
          {score !== undefined ? `${score}/100` : status}
          {` / ${targetAssessmentDetail(assessment)}`}
        </small>
        {Object.keys(remediation).length ? (
          <div className="execution-remediation-block">
            <small className="execution-remediation">
              Next: {targetAssessmentRemediationDetail(remediation)}
            </small>
            {remediationCommand ? (
              <div className="execution-remediation-actions">
                <span>{remediationCommand.edgeRun ? "edge-run" : "operator"}</span>
                <small>{remediationCommand.note}</small>
                <button
                  className="button-mini"
                  type="button"
                  onClick={() => onCopyRemediation(remediationCommand.label, remediationCommand.command)}
                >
                  <Clipboard size={14} />
                  Copy command
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        {componentProofs.length ? (
          <div className="execution-proof-chips" aria-label={`${id} component proof`}>
            {componentProofs.map((component) => (
              <span
                className={`execution-proof-chip execution-proof-chip-${component.tone}`}
                key={component.key}
              >
                {component.label}: {component.state}
                {component.score ? ` ${component.score}` : ""}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <div className="execution-candidate-meta">
        {labels.map((label) => (
          <Badge key={label} value={label} />
        ))}
      </div>
    </div>
  );
}

function EdgeProofPanel({
  componentDigests,
  disabled,
  handoff,
  proof,
  trace,
  workflow,
  onGenerate,
  onDownload,
  onCopy
}: {
  componentDigests: EdgeProofComponentDigestStatus;
  disabled: boolean;
  handoff: EdgeProofDownloadHandoff | undefined;
  proof: JsonObject | undefined;
  trace: EdgeProofTraceStatus;
  workflow: EdgeProofWorkflow;
  onGenerate: () => void;
  onDownload: () => void;
  onCopy: (label: string, command: string) => void;
}): JSX.Element {
  const actionDisabled = disabled || workflow.missing.length > 0;
  return (
    <section className={`edge-proof edge-proof-${workflow.tone}`} aria-labelledby="edge-proof-heading">
      <div className="edge-proof-header">
        <div>
          <span className="section-kicker">Runtime proof artifact</span>
          <h2 id="edge-proof-heading">{workflow.status}</h2>
          <p>{workflow.detail}</p>
        </div>
        <div className="edge-proof-policy" aria-label="Proof gate policy">
          <span className="edge-proof-policy-line">Proof policy: {workflow.gatePolicy}</span>
          <Badge value={workflow.gatePolicy} />
          <span className={`badge badge-${workflow.capabilityLockTone}`}>{workflow.capabilityLock}</span>
          <small>{workflow.capabilityLockDetail}</small>
          <Badge value={workflow.attestation} />
          <strong>{workflow.runtimeFit}</strong>
          <small>{workflow.proofPath}</small>
          <div className="edge-proof-actions">
            <button
              className="button button-secondary"
              disabled={actionDisabled}
              type="button"
              onClick={onGenerate}
            >
              <FileCheck2 size={16} />
              <span>Generate artifact</span>
            </button>
            <button
              className="button button-ghost"
              disabled={actionDisabled}
              type="button"
              onClick={onDownload}
            >
              <Download size={16} />
              <span>Download JSON</span>
            </button>
          </div>
        </div>
      </div>

      {workflow.missing.length ? (
        <div className="edge-proof-missing" aria-label="Missing proof context">
          {workflow.missing.map((item) => (
            <span key={item}>{item}</span>
          ))}
        </div>
      ) : null}

      <EdgeProofTraceCard trace={trace} />
      <EdgeExecutionManifestCard proof={proof} />
      <EdgeProofComponentDigestCard status={componentDigests} />
      <EdgeProofDownloadHandoffCard componentDigests={componentDigests} handoff={handoff} />

      <div className="edge-proof-command-grid">
        <ProofCommand
          command={workflow.generateCommand}
          disabled={actionDisabled}
          icon={<Terminal size={16} />}
          label="Generate proof"
          onCopy={onCopy}
        />
        <ProofCommand
          command={workflow.verifyCommand}
          disabled={actionDisabled}
          icon={<FileCheck2 size={16} />}
          label="Verify gate"
          onCopy={onCopy}
        />
        <ProofCommand
          command={workflow.verifyJsonCommand}
          disabled={actionDisabled}
          icon={<Clipboard size={16} />}
          label="Verify JSON"
          onCopy={onCopy}
        />
      </div>
    </section>
  );
}

function EdgeExecutionManifestCard({ proof }: { proof: JsonObject | undefined }): JSX.Element {
  const manifest = asRecord(proof?.edge_execution_manifest);
  const execution = asRecord(manifest.execution);
  const edge = asRecord(manifest.edge);
  const evidence = asRecord(manifest.evidence);
  const admission = asRecord(manifest.admission);
  const capabilityLock = asRecord(edge.capability_lock);
  const schema = stringOf(manifest.schema_version, "");
  const runtimeImage = stringOf(execution.runtime_image, "");
  const runtimeTarget = stringOf(execution.runtime_target_id, "");
  const runtimeLane = asRecord(execution.runtime_lane);
  const capabilityStatus = stringOf(capabilityLock.status, "");
  const capabilityDigest = stringOf(capabilityLock.capability_sha256, "");
  const gateStatus = stringOf(admission.gate_status, "");
  const available = schema === "temms-edge-execution-manifest/v1";
  const tone: GateTone = !available ? "neutral" : gateStatus === "failed" ? "bad" : "good";
  const validationId = stringOf(evidence.runtime_validation_id, "");
  const benchmarkId = stringOf(evidence.benchmark_id, "");
  const latency = numberOf(evidence.latency_ms_p95);
  const throughput = numberOf(evidence.throughput_ips);
  const evidenceDetail = [
    validationId ? `validation ${validationId}` : "",
    benchmarkId ? `benchmark ${benchmarkId}` : "",
    latency !== undefined ? `${formatMetricNumber(latency)} ms p95` : "",
    throughput !== undefined ? `${formatThroughput(throughput)} ips` : ""
  ].filter(Boolean).join(" / ");

  return (
    <article className={`edge-proof-trace edge-execution-manifest edge-proof-trace-${tone}`} data-testid="edge-execution-manifest">
      <div className="edge-proof-trace-header">
        <div>
          <span>Execution manifest</span>
          <strong>{available ? stringOf(asRecord(manifest.path).label, "signed path") : "manifest pending"}</strong>
          <small>
            {available
              ? "Signed execution intent retained with runtime image, capability lock, and evidence ids."
              : "Generate or download a proof to inspect the signed execution manifest."}
          </small>
        </div>
        <Badge value={available ? gateStatus || "retained" : "not generated"} />
      </div>
      <div className="edge-proof-trace-grid">
        <CapabilityMetric
          detail={runtimeTarget || stringOf(execution.runtime_arch, "runtime target pending")}
          label="Runtime image"
          tone={runtimeImage ? "good" : available ? "warn" : "neutral"}
          value={runtimeImage || "pending"}
        />
        <CapabilityMetric
          detail={manifestRuntimeLaneDetail(runtimeLane)}
          label="Runtime lane"
          tone={runtimeLane.lane_id ? "good" : available ? "warn" : "neutral"}
          value={stringOf(runtimeLane.lane_id, "pending")}
        />
        <CapabilityMetric
          detail={capabilityDigest ? `sha256 ${capabilityDigest.slice(0, 12)}` : "capability digest pending"}
          label="Capability lock"
          tone={capabilityStatus === "locked" ? "good" : available ? "warn" : "neutral"}
          value={capabilityStatus || "pending"}
        />
        <CapabilityMetric
          detail={evidenceDetail || "validation and benchmark ids pending"}
          label="Evidence ids"
          tone={validationId && benchmarkId ? "good" : available ? "warn" : "neutral"}
          value={validationId && benchmarkId ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={execution.selected_is_best === true ? "selected runtime is best measured target" : "best-runtime proof pending"}
          label="Best target"
          tone={execution.selected_is_best === true ? "good" : available ? "warn" : "neutral"}
          value={execution.selected_is_best === true ? "yes" : "pending"}
        />
        <CapabilityMetric
          detail={manifestGatePolicyLabel(asRecord(admission.gate_policy))}
          label="Admission"
          tone={gateStatus === "passed" ? "good" : gateStatus === "failed" ? "bad" : "neutral"}
          value={gateStatus || "pending"}
        />
      </div>
    </article>
  );
}

function EdgeProofComponentDigestCard({
  status
}: {
  status: EdgeProofComponentDigestStatus;
}): JSX.Element {
  const errors = status.errors.slice(0, 2).join(" / ");
  const digestByKey = new Map(status.digests.map((digest) => [digest.key, digest]));
  const workbenchDigest = digestByKey.get("runtime_workbench_sha256");
  const traceDigest = digestByKey.get("runtime_decision_trace_sha256");
  const manifestDigest = digestByKey.get("edge_execution_manifest_sha256");

  return (
    <article className={`edge-proof-trace edge-proof-digests edge-proof-trace-${status.tone}`} data-testid="edge-proof-component-digests">
      <div className="edge-proof-trace-header">
        <div>
          <span>Component digests</span>
          <strong>{status.value}</strong>
          <small>{status.detail}</small>
        </div>
        <Badge value={status.status.replace(/_/g, " ")} />
      </div>
      <div className="edge-proof-trace-grid">
        <CapabilityMetric
          detail={status.schema || "component digest schema unavailable"}
          label="Digest schema"
          tone={status.schema === "temms-edge-runtime-proof-component-digests/v1" ? "good" : status.status === "not_generated" ? "neutral" : "warn"}
          value={status.schema || "pending"}
        />
        <CapabilityMetric
          detail={errors || "runtime workbench, trace, and execution manifest are individually hash-addressed"}
          label="Coverage"
          tone={status.tone}
          value={componentDigestCoverageLabel(status)}
        />
        <CapabilityMetric
          detail={workbenchDigest?.value ? `sha256 ${shortProofDigest(workbenchDigest.value)}` : "runtime workbench digest pending"}
          label="Workbench"
          tone={workbenchDigest?.value ? "good" : status.status === "not_generated" ? "neutral" : "warn"}
          value={workbenchDigest?.value ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={traceDigest?.value ? `sha256 ${shortProofDigest(traceDigest.value)}` : "runtime decision trace digest pending"}
          label="Trace"
          tone={traceDigest?.value ? "good" : status.status === "not_generated" ? "neutral" : "warn"}
          value={traceDigest?.value ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={manifestDigest?.value ? `sha256 ${shortProofDigest(manifestDigest.value)}` : "edge execution manifest digest pending"}
          label="Manifest"
          tone={manifestDigest?.value ? "good" : status.status === "not_generated" ? "neutral" : "warn"}
          value={manifestDigest?.value ? "retained" : "pending"}
        />
      </div>
    </article>
  );
}

function EdgeProofDownloadHandoffCard({
  componentDigests,
  handoff
}: {
  componentDigests: EdgeProofComponentDigestStatus;
  handoff: EdgeProofDownloadHandoff | undefined;
}): JSX.Element {
  const bodyDigests = new Map(componentDigests.digests.map((digest) => [digest.key, digest.value]));
  const headerDigests = [
    {
      key: "runtime_workbench_sha256",
      label: "Workbench",
      value: handoff?.runtimeWorkbenchSha256 || ""
    },
    {
      key: "runtime_decision_trace_sha256",
      label: "Trace",
      value: handoff?.runtimeDecisionTraceSha256 || ""
    },
    {
      key: "edge_execution_manifest_sha256",
      label: "Manifest",
      value: handoff?.edgeExecutionManifestSha256 || ""
    }
  ];
  const retainedHeaderDigests = headerDigests.filter((digest) => digest.value).length;
  const missingHeaderDigests = handoff
    ? headerDigests.filter((digest) => !digest.value).map((digest) => digest.label)
    : [];
  const mismatchedHeaderDigests = handoff
    ? headerDigests.filter((digest) => {
        const bodyDigest = bodyDigests.get(digest.key) || "";
        return digest.value && bodyDigest && normalizeSha256Digest(digest.value) !== normalizeSha256Digest(bodyDigest);
      })
    : [];
  const tone: GateTone = !handoff
    ? "neutral"
    : mismatchedHeaderDigests.length
      ? "bad"
      : missingHeaderDigests.length
        ? "warn"
        : "good";
  const value = !handoff
    ? "headers pending"
    : mismatchedHeaderDigests.length
      ? "header mismatch"
      : `${retainedHeaderDigests}/3 component headers`;
  const detail = !handoff
    ? "Downloaded artifact headers are not captured for the latest generated proof."
    : mismatchedHeaderDigests.length
      ? `${mismatchedHeaderDigests.map((digest) => digest.label).join(", ")} header disagrees with proof body`
      : missingHeaderDigests.length
        ? `${missingHeaderDigests.join(", ")} header missing from download response`
        : "Download response headers match the retained component digests.";
  const payloadDigest = handoff?.payloadSha256 || "";
  const gateTone: GateTone =
    handoff?.gateStatus === "passed" ? "good" : handoff?.gateStatus === "failed" ? "bad" : handoff ? "warn" : "neutral";
  const attestationTone: GateTone =
    handoff?.attestation === "signed" ? "good" : handoff?.attestation === "unsigned" ? "warn" : "neutral";

  return (
    <article className={`edge-proof-trace edge-proof-handoff edge-proof-trace-${tone}`} data-testid="edge-proof-download-handoff">
      <div className="edge-proof-trace-header">
        <div>
          <span>Download handoff headers</span>
          <strong>{value}</strong>
          <small>{detail}</small>
        </div>
        <Badge value={handoff ? "downloaded" : "not downloaded"} />
      </div>
      <div className="edge-proof-trace-grid">
        <CapabilityMetric
          detail={handoff?.fileName || "artifact filename header pending"}
          label="Filename"
          tone={handoff?.fileName ? "good" : "neutral"}
          value={handoff?.fileName ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={payloadDigest ? `sha256 ${shortProofDigest(payloadDigest)}` : "payload hash header pending"}
          label="Payload hash"
          tone={isSha256Digest(payloadDigest) ? "good" : handoff ? "warn" : "neutral"}
          value={payloadDigest ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={handoff?.keyFingerprint ? `key ${handoff.keyFingerprint}` : "signing-key fingerprint header pending"}
          label="Attestation"
          tone={attestationTone}
          value={handoff?.attestation || "pending"}
        />
        <CapabilityMetric
          detail="Strict proof policy result from the download envelope"
          label="Gate"
          tone={gateTone}
          value={handoff?.gateStatus || "pending"}
        />
        {headerDigests.map((digest) => {
          const bodyDigest = bodyDigests.get(digest.key) || "";
          const matches =
            digest.value && bodyDigest && normalizeSha256Digest(digest.value) === normalizeSha256Digest(bodyDigest);
          const digestTone: GateTone = matches ? "good" : digest.value ? "warn" : handoff ? "warn" : "neutral";
          return (
            <CapabilityMetric
              detail={digest.value ? `sha256 ${shortProofDigest(digest.value)}` : `${digest.label.toLowerCase()} header pending`}
              key={digest.key}
              label={`${digest.label} header`}
              tone={digestTone}
              value={matches ? "matches body" : digest.value ? "retained" : "pending"}
            />
          );
        })}
      </div>
    </article>
  );
}

function componentDigestCoverageLabel(status: EdgeProofComponentDigestStatus): string {
  if (!status.digestCount) return "pending";
  if (status.status === "consistent") return `${status.digestCount}/3 verified`;
  if (status.status === "mismatch") return `${status.digestCount}/3 checked`;
  if (status.status === "verifying") return `${status.digestCount}/3 checking`;
  return `${status.digestCount}/3 retained`;
}

function manifestRuntimeLaneDetail(lane: JsonObject): string {
  return [
    stringOf(lane.execution_engine, ""),
    stringsOf(lane.providers).join(", "),
    stringOf(lane.acceleration, ""),
    stringOf(lane.optimization_goal, "")
  ].filter(Boolean).join(" / ") || "runtime lane pending";
}

function manifestGatePolicyLabel(policy: JsonObject): string {
  const parts = [];
  if (policy.require_go === true) parts.push("go");
  if (policy.require_best_runtime === true) parts.push("best runtime");
  if (policy.require_capability_lock === true) parts.push("capability lock");
  const minRuntimeFit = numberOf(policy.min_runtime_fit);
  if (minRuntimeFit !== undefined) parts.push(`fit >= ${formatMetricNumber(minRuntimeFit)}`);
  return parts.length ? parts.join(" + ") : "proof policy pending";
}

function EdgeProofTraceCard({ trace }: { trace: EdgeProofTraceStatus }): JSX.Element {
  const sampleErrors = trace.errors.slice(0, 2);
  return (
    <article className={`edge-proof-trace edge-proof-trace-${trace.tone}`} data-testid="edge-proof-trace-consistency">
      <div className="edge-proof-trace-header">
        <div>
          <span>Signed runtime trace</span>
          <strong>{trace.value}</strong>
          <small>{trace.detail}</small>
        </div>
        <Badge value={trace.status.replace(/_/g, " ")} />
      </div>
      <div className="edge-proof-trace-grid">
        <CapabilityMetric
          detail={trace.schema || "trace schema unavailable"}
          label="Trace schema"
          tone={trace.schema === "temms-runtime-decision-trace/v1" ? "good" : trace.status === "not_generated" ? "neutral" : "warn"}
          value={trace.schema || "pending"}
        />
        <CapabilityMetric
          detail={`${trace.commandCount} remediation command${trace.commandCount === 1 ? "" : "s"}`}
          label="Targets"
          tone={trace.rowCount ? trace.tone : "neutral"}
          value={trace.rowCount ? `${trace.rowCount} ranked` : "pending"}
        />
        <CapabilityMetric
          detail={sampleErrors.length ? sampleErrors.join(" / ") : "trace agrees with runtime_workbench"}
          label="Workbench check"
          tone={trace.tone}
          value={trace.status === "mismatch" ? `${trace.errors.length} mismatch${trace.errors.length === 1 ? "" : "es"}` : trace.status.replace(/_/g, " ")}
        />
      </div>
    </article>
  );
}

function ProofCommand({
  command,
  disabled,
  icon,
  label,
  onCopy
}: {
  command: string;
  disabled: boolean;
  icon: JSX.Element;
  label: string;
  onCopy: (label: string, command: string) => void;
}): JSX.Element {
  return (
    <article className="edge-proof-command">
      <div className="edge-proof-command-topline">
        <span>{label}</span>
        <button
          className="button-mini"
          disabled={disabled}
          type="button"
          onClick={() => onCopy(label, command)}
        >
          {icon}
          Copy
        </button>
      </div>
      <pre>{command}</pre>
    </article>
  );
}

function EdgeRecommendationPanel({
  recommendations,
  selectedModelId,
  selectedDeviceId,
  selectedRuntimeId,
  onSelect
}: {
  recommendations: EdgeRecommendation[];
  selectedModelId: string;
  selectedDeviceId: string;
  selectedRuntimeId: string;
  onSelect: (recommendation: EdgeRecommendation) => void;
}): JSX.Element {
  const visible = recommendations.slice(0, 3);
  if (!visible.length) {
    return (
      <div className="edge-recommendations edge-recommendations-empty">
        <div>
          <span className="section-kicker">Runtime optimizer</span>
          <strong>Recommendation score pending</strong>
        </div>
        <p>Register packages, edge inventory, and runtime targets to rank deployment paths.</p>
      </div>
    );
  }
  return (
    <div className="edge-recommendations" aria-label="Ranked edge runtime recommendations">
      <div className="edge-recommendations-header">
        <div>
          <span className="section-kicker">Runtime optimizer</span>
          <strong>Best edge paths</strong>
        </div>
        <span>{visible.length} ranked</span>
      </div>
      <div className="edge-recommendation-grid">
        {visible.map((recommendation) => {
          const runtimeId = recommendation.runtime_target_id
            ? String(recommendation.runtime_target_id)
            : "device inventory";
          const modelId = recommendation.model_id ? String(recommendation.model_id) : "package";
          const device = recommendation.device_id ? String(recommendation.device_id) : "edge";
          const selected =
            modelId === selectedModelId &&
            device === selectedDeviceId &&
            (recommendation.runtime_target_id ? runtimeId === selectedRuntimeId : !selectedRuntimeId);
          const optimization = asRecord(recommendation.optimization);
          const runtimeFit = asRecord(recommendation.runtime_fit);
          const artifactLane = asRecord(recommendation.artifact_lane ?? runtimeFit.artifact_lane);
          const runtimeFitScore = numberOf(runtimeFit.score);
          const runtimeFitTier = stringOf(runtimeFit.tier, "fit").replace(/_/g, " ");
          const latency = metricText(optimization.latency_ms_p95);
          const throughput = throughputText(optimization.throughput_ips);
          const action = (recommendation.required_actions ?? [])[0];
          return (
            <article
              className={`edge-recommendation edge-recommendation-${recommendationTone(recommendation)}${
                selected ? " edge-recommendation-selected" : ""
              }`}
              key={`${recommendation.rank}-${modelId}-${device}-${runtimeId}`}
            >
              <div className="edge-recommendation-topline">
                <span>#{recommendation.rank ?? "-"}</span>
                <strong>{recommendation.score ?? 0}</strong>
              </div>
              <div>
                <Badge value={formatRecommendationDecision(recommendation.decision)} />
                <h3>{modelId}</h3>
                <p>{device} / {runtimeId}</p>
              </div>
              <p className="edge-recommendation-reason">
                {recommendation.primary_reason || action || "Review this target"}
              </p>
              <div className="edge-recommendation-metrics">
                <span>
                  {runtimeFitScore !== undefined
                    ? `${runtimeFitScore}/100 ${runtimeFitTier}`
                    : `${recommendation.confidence || "low"} confidence`}
                </span>
                {latency ? <span>{latency} ms p95</span> : null}
                {throughput ? <span>{throughput} ips</span> : null}
                {Object.keys(artifactLane).length ? <span>{artifactLaneValue(artifactLane)}</span> : null}
              </div>
              <button
                className="button button-ghost"
                type="button"
                onClick={() => onSelect(recommendation)}
                disabled={selected}
              >
                <span>{selected ? "Selected" : "Use path"}</span>
              </button>
            </article>
          );
        })}
      </div>
    </div>
  );
}

function CapabilityDossier({
  device,
  edgeRuntimeFit,
  model,
  readiness,
  readinessVerdict,
  resourceEnvelopeFit,
  runtime,
  runtimeValidation
}: {
  device: Device | undefined;
  edgeRuntimeFit: EdgeRuntimeFit;
  model: ModelRecord | undefined;
  readiness: DeploymentReadiness | undefined;
  readinessVerdict: ReadinessVerdict;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtime: RuntimeTarget | undefined;
  runtimeValidation: RuntimeValidation | undefined;
}): JSX.Element {
  const observed = device ? deviceResourceSnapshot(device) : {};
  const constraints = runtime ? runtimeTargetInventoryConstraints(runtime) : undefined;
  const inventory = asRecord(device?.inventory);
  const runtimes = Object.entries(asRecord(inventory.runtimes))
    .filter(([, status]) => asRecord(status).available === true)
    .map(([name]) => name);
  const providers = stringsOf(asRecord(asRecord(inventory.runtimes).onnxruntime).providers);
  const accelerators = Object.entries(asRecord(inventory.accelerators))
    .filter(([, status]) => asRecord(status).available === true)
    .map(([name]) => name);
  const apiGates = readiness?.gates ?? [];
  const attentionGates = apiGates.filter((gate) => toneForReadinessStatus(stringOf(gate.status, "")) !== "good");
  const selectedGate = attentionGates[0];
  const validationResult = asRecord(runtimeValidation?.result);
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const runtimeLane = runtimeLaneFor(runtimeFit, runtime);
  const artifactLane = asRecord(runtimeFit.artifact_lane);
  const productionAdmission = asRecord(readiness?.production_admission);
  const runtimeFitScore = numberOf(runtimeFit.score);
  const runtimeFitTier = stringOf(runtimeFit.tier, edgeRuntimeFit.label).replace(/_/g, " ");
  const runtimeFitDetail = stringOf(runtimeFit.detail, edgeRuntimeFit.detail);
  const targetSelection = asRecord(runtimeFit.target_selection);
  const runtimeFitComponents = runtimeFitComponentRows(runtimeFit);
  const runtimeFitTone =
    runtimeFit.tier === "blocked"
      ? "bad"
      : runtimeFit.tier === "needs_evidence"
        ? "warn"
        : runtimeFitScore !== undefined
          ? "good"
          : edgeRuntimeFit.tone;

  return (
    <div className="capability-dossier" aria-label="Selected on-device capability dossier">
      <div className="capability-dossier-header">
        <div>
          <span className="section-kicker">On-device capability dossier</span>
          <strong>{model ? `${model.id} on ${device ? deviceId(device) : "edge"}` : "Select a model path"}</strong>
        </div>
        <Badge value={readinessVerdict.label} />
      </div>
      <div className="capability-dossier-grid">
        <CapabilityMetric
          label="Runtime fit"
          value={runtimeFitScore !== undefined ? `${runtimeFitScore}/100` : edgeRuntimeFit.label}
          detail={runtimeFitScore !== undefined ? `${runtimeFitTier}: ${runtimeFitDetail}` : edgeRuntimeFit.detail}
          tone={runtimeFitTone}
        />
        <CapabilityMetric
          label="Runtime lane"
          value={runtimeLaneValue(runtimeLane)}
          detail={runtimeLaneDetail(runtimeLane)}
          tone={runtimeLaneTone(runtimeLane)}
        />
        <CapabilityMetric
          label="Artifact fit"
          value={artifactLaneValue(artifactLane)}
          detail={artifactLaneDetail(artifactLane)}
          tone={artifactLaneTone(artifactLane)}
        />
        <CapabilityMetric
          label="Target rank"
          value={runtimeTargetSelectionValue(targetSelection)}
          detail={runtimeTargetSelectionDetail(targetSelection)}
          tone={runtimeTargetSelectionTone(targetSelection)}
        />
        <CapabilityMetric
          label="Resource envelope"
          value={resourceEnvelopeFit.label}
          detail={resourceEnvelopeFit.detail}
          tone={resourceEnvelopeFit.tone}
        />
        <CapabilityMetric
          label="Performance proof"
          value={performanceSloLabel(model)}
          detail={model ? performanceSloDetail(model) : "select a model"}
          tone={performanceSloTone(model)}
        />
        <CapabilityMetric
          label="Production apply"
          value={productionAdmissionValue(productionAdmission)}
          detail={productionAdmissionDetail(productionAdmission)}
          tone={productionAdmissionTone(productionAdmission)}
        />
        <CapabilityMetric
          label="Validation"
          value={runtimeValidation ? "validated" : "not validated"}
          detail={
            runtimeValidation
              ? `${runtime ? runtimeTargetId(runtime) : "runtime target"} passed ${compactDate(runtimeValidation.created_at)}`
              : "run package validation before field rollout"
          }
          tone={runtimeValidation ? "good" : "warn"}
        />
      </div>

      <div className="capability-dossier-detail">
        <CapabilityBlock
          title="Runtime fit components"
          items={runtimeFitComponents}
        />
        <CapabilityBlock
          title="Live edge inventory"
          items={[
            ["RAM", formatMb(numberOf(observed.memoryAvailableMb))],
            ["Storage", formatMb(numberOf(observed.storageAvailableMb))],
            ["Thermal", formatTemperature(numberOf(observed.temperatureC))],
            ["Power", formatPower(observed)],
            ["Runtimes", runtimes.join(", ") || "not reported"],
            ["Providers", providers.join(", ") || "not reported"],
            ["Accelerators", accelerators.join(", ") || "none reported"]
          ]}
        />
        <CapabilityBlock
          title="Target requirements"
          items={[
            ["Model", model?.id ?? "missing"],
            ["Package", model?.packageId ?? "missing"],
            ["Runtime target", runtime ? runtimeTargetId(runtime) : "missing"],
            ["Lane", runtimeLaneValue(runtimeLane)],
            ["Artifact", artifactLaneValue(artifactLane)],
            ["Requires", constraints?.runtimes.join(", ") || model?.runtimes.join(", ") || "not declared"],
            ["Providers", constraints?.providers.join(", ") || constraints?.preferredProviders.join(", ") || "not declared"],
            ["Accelerators", constraints?.accelerators.join(", ") || (constraints?.requiresGpu ? "GPU required" : "not declared")],
            ["Validation result", runtimeValidation ? stringOf(validationResult.validation_state, "passed") : "missing"]
          ]}
        />
        <CapabilityBlock
          title="Admission gates"
          items={[
            ["Apply admission", productionAdmissionValue(productionAdmission)],
            ["Verdict", readinessVerdict.headline],
            ["Next action", readinessVerdict.nextAction],
            [
              "Review gate",
              selectedGate
                ? `${stringOf(selectedGate.label, stringOf(selectedGate.gate_id, "gate"))}: ${displayGateState(stringOf(selectedGate.state, stringOf(selectedGate.status, "unknown")))}`
                : "none"
            ],
            ["Gate detail", selectedGate ? stringOf(selectedGate.detail, "no detail") : "all gates aligned"],
            ["Checked", compactDate(readiness?.checked_at)]
          ]}
        />
      </div>
    </div>
  );
}

function CapabilityBlock({ items, title }: { items: [string, string][]; title: string }): JSX.Element {
  return (
    <div className="capability-block">
      <strong>{title}</strong>
      <dl>
        {items.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value || "-"}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function formatRecommendationDecision(value?: string): string {
  if (!value) return "review";
  return value.replace(/_/g, " ");
}

function metricText(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  const numeric = numberOf(value);
  if (numeric !== undefined) return formatMetricNumber(numeric);
  return String(value);
}

function throughputText(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  const numeric = numberOf(value);
  if (numeric !== undefined) return formatThroughput(numeric);
  return String(value);
}

function runtimeLaneFor(
  runtimeFit: Record<string, unknown>,
  runtime: RuntimeTarget | undefined
): JsonObject {
  const readinessLane = asRecord(runtimeFit.runtime_lane);
  if (Object.keys(readinessLane).length) return readinessLane;
  return asRecord(runtime?.runtime_lane);
}

function runtimeLaneValue(lane: JsonObject): string {
  return stringOf(lane.label, stringOf(lane.lane_id, "not classified"));
}

function runtimeLaneDetail(lane: JsonObject): string {
  const parts = [
    stringOf(lane.execution_engine, ""),
    stringOf(lane.acceleration, "").replace(/_/g, " "),
    stringOf(lane.optimization_goal, "")
  ].filter(Boolean);
  const providers = stringsOf(lane.providers);
  if (providers.length) {
    parts.splice(Math.min(2, parts.length), 0, `providers ${providers.join(", ")}`);
  }
  return parts.join(" / ") || "runtime target has no lane metadata";
}

function runtimeLaneTone(lane: JsonObject): GateTone {
  const laneId = stringOf(lane.lane_id, "");
  if (!laneId) return "neutral";
  return laneId === "device-inventory" ? "warn" : "good";
}

function runtimeTargetImageValue(runtime: RuntimeTarget | undefined): string {
  if (!runtime) return "runtime missing";
  return runtime.image || runtime.name || runtime.runtime_target_id || runtime.id || "image pending";
}

function runtimeTargetImageDetail(runtime: RuntimeTarget | undefined): string {
  if (!runtime) return "select a runtime target";
  const platform = [runtime.os, runtime.arch].filter(Boolean).join("/");
  const profiles = stringsOf(runtime.device_profiles);
  return [
    platform,
    profiles.length ? `profiles ${profiles.join(", ")}` : "",
    runtime.updated_at ? `updated ${compactDate(runtime.updated_at)}` : ""
  ]
    .filter(Boolean)
    .join(" / ") || "runtime image metadata pending";
}

function runtimeProviderValue(lane: JsonObject): string {
  const providers = stringsOf(lane.providers);
  if (providers.length) return providers.join(", ");
  return stringOf(lane.execution_engine, "provider pending");
}

function runtimeProviderDetail(lane: JsonObject, device: Device | undefined): string {
  const engine = stringOf(lane.execution_engine, "");
  const acceleration = stringOf(lane.acceleration, "").replace(/_/g, " ");
  const accelerators = stringsOf(lane.accelerators);
  const edgeProfile = device?.profile ?? "";
  return [
    engine,
    acceleration,
    accelerators.length ? `accelerators ${accelerators.join(", ")}` : "",
    edgeProfile ? `edge profile ${edgeProfile}` : ""
  ]
    .filter(Boolean)
    .join(" / ") || "provider and accelerator metadata pending";
}

function runtimeProviderTone(lane: JsonObject, device: Device | undefined): GateTone {
  if (!Object.keys(lane).length) return "neutral";
  const inventoryTone = runtimeInventoryTone(device);
  if (inventoryTone === "bad") return "bad";
  return runtimeLaneTone(lane);
}

function artifactLaneValue(artifactLane: JsonObject): string {
  const state = stringOf(artifactLane.state, "");
  if (state) return state.replace(/_/g, " ");
  const format = stringOf(artifactLane.model_format, "");
  return format ? `${format} artifact` : "not classified";
}

function artifactLaneDetail(artifactLane: JsonObject): string {
  const detail = stringOf(artifactLane.detail, "");
  if (detail) return detail;
  const nativeFormats = stringsOf(artifactLane.native_formats);
  if (nativeFormats.length) return `native formats: ${nativeFormats.join(", ")}`;
  return "artifact format has not been evaluated for this runtime lane";
}

function artifactLaneTone(artifactLane: JsonObject): GateTone {
  const status = stringOf(artifactLane.status, "");
  if (status === "go") return "good";
  if (status === "blocked") return "bad";
  if (status === "attention") return "warn";
  return "neutral";
}

function capabilityLockValue(lock: JsonObject): string {
  const status = stringOf(lock.status, "");
  if (status) return status.replace(/_/g, " ");
  return stringOf(lock.capability_sha256, "") ? "hash locked" : "not locked";
}

function capabilityLockDetail(lock: JsonObject): string {
  const failures = stringsOf(lock.failures);
  if (failures.length) return compactMetricDetail(failures[0]);
  const runtimeTarget = asRecord(lock.runtime_target);
  const edgeInventory = asRecord(lock.edge_inventory);
  const runtimeTargetId = stringOf(lock.runtime_target_id, stringOf(runtimeTarget.runtime_target_id, "runtime target"));
  const edgeProfile = stringOf(edgeInventory.device_profile, "edge profile");
  const freshness = capabilityLockFreshnessDetail(lock);
  const digest = stringOf(lock.capability_sha256, "");
  const digestLabel = digest ? `capability ${digest.slice(0, 12)}` : "";
  return [runtimeTargetId, edgeProfile, freshness, digestLabel].filter(Boolean).join(" / ") || "capability basis pending";
}

function capabilityLockTone(lock: JsonObject): GateTone {
  const status = stringOf(lock.status, "");
  if (status === "locked") return "good";
  if (status === "blocked") return "bad";
  if (status === "attention") return "warn";
  return stringOf(lock.capability_sha256, "") ? "good" : "neutral";
}

function capabilityLockFreshnessDetail(lock: JsonObject): string {
  const freshness = asRecord(asRecord(lock.edge_inventory).telemetry_freshness);
  const state = stringOf(freshness.state, stringOf(freshness.status, "")).replace(/_/g, " ");
  const ageSeconds = numberOf(freshness.heartbeat_age_seconds);
  const budgetSeconds = numberOf(freshness.heartbeat_stale_after_seconds);
  if (ageSeconds !== undefined && budgetSeconds !== undefined) {
    const label = state || "telemetry";
    return `${label}: heartbeat ${formatAge(ageSeconds)} old / ${formatAge(budgetSeconds)} budget`;
  }
  const detail = stringOf(freshness.detail, "");
  if (detail) return compactMetricDetail(detail);
  return "";
}

function runtimeFitDisplayFor(
  readiness: DeploymentReadiness | undefined,
  fallback: EdgeRuntimeFit,
  runtime: RuntimeTarget | undefined
): RuntimeFitDisplay {
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const score = numberOf(runtimeFit.score);
  const tier = stringOf(runtimeFit.tier, "").replace(/_/g, " ");
  const detail = stringOf(runtimeFit.detail, fallback.detail);
  const runtimeId = stringOf(runtimeFit.runtime_target_id, runtime ? runtimeTargetId(runtime) : "");
  if (score === undefined) {
    return {
      ...fallback,
      tileDetail: runtimeId ? `${fallback.label} on ${runtimeId}` : fallback.detail
    };
  }
  const label = tier ? `${score}/100 ${tier}` : `${score}/100`;
  return {
    label,
    detail,
    tone: runtimeFitTone(runtimeFit, fallback.tone),
    failures: fallback.failures,
    tileDetail: runtimeId ? `${label} on ${runtimeId}` : label
  };
}

function runtimeFitTone(runtimeFit: Record<string, unknown>, fallback: GateTone): GateTone {
  const tier = stringOf(runtimeFit.tier, "");
  if (tier === "blocked") return "bad";
  if (tier === "needs_evidence") return "warn";
  return numberOf(runtimeFit.score) !== undefined ? "good" : fallback;
}

function runtimeFitComponentRows(runtimeFit: Record<string, unknown>): [string, string][] {
  const components = asRecord(runtimeFit.components);
  const rows = [
    runtimeFitComponentRow("Compatibility", asRecord(components.compatibility)),
    runtimeFitComponentRow("Validation", asRecord(components.runtime_validation)),
    runtimeFitComponentRow("Performance", asRecord(components.performance)),
    runtimeFitComponentRow("Resource", asRecord(components.resource)),
    runtimeFitComponentRow("Telemetry", asRecord(components.telemetry))
  ].filter((row): row is [string, string] => row !== undefined);
  return rows.length ? rows : [["Runtime score", "waiting for readiness evidence"]];
}

function runtimeFitComponentRow(
  label: string,
  component: Record<string, unknown>
): [string, string] | undefined {
  const score = numberOf(component.score);
  const maxScore = numberOf(component.max_score);
  const state = stringOf(component.state, stringOf(component.status, "unknown")).replace(/_/g, " ");
  if (score === undefined && maxScore === undefined && state === "unknown") return undefined;
  const parts = [];
  if (score !== undefined && maxScore !== undefined) parts.push(`${score}/${maxScore}`);
  else if (score !== undefined) parts.push(`${score}`);
  parts.push(state);

  const failures = stringsOf(component.failures);
  if (failures.length) parts.push(failures.slice(0, 2).join("; "));

  if (label === "Performance") {
    const latencyHeadroom = numberOf(component.latency_headroom_pct);
    const throughputHeadroom = numberOf(component.throughput_headroom_pct);
    if (latencyHeadroom !== undefined) parts.push(`latency ${formatSignedPercent(latencyHeadroom)}`);
    if (throughputHeadroom !== undefined) parts.push(`throughput ${formatSignedPercent(throughputHeadroom)}`);
  }
  if (label === "Resource") {
    const memoryHeadroom = numberOf(component.memory_headroom_mb);
    const storageHeadroom = numberOf(component.storage_headroom_mb);
    if (memoryHeadroom !== undefined) parts.push(`RAM ${formatSignedMb(memoryHeadroom)}`);
    if (storageHeadroom !== undefined) parts.push(`storage ${formatSignedMb(storageHeadroom)}`);
  }
  return [label, parts.join(", ")];
}

function runtimeDecisionActionLabel(value: string): string {
  const normalized = value.replace(/-/g, "_");
  const labels: Record<string, string> = {
    apply_or_stage: "apply or stage",
    use_best_runtime: "use best runtime",
    resolve_blocking_gates: "resolve blockers",
    collect_missing_evidence: "collect evidence",
    review: "review"
  };
  return labels[normalized] ?? normalized.replace(/_/g, " ");
}

function executionContractHeadline(
  action: string,
  decisionStatus: string,
  verdict: ReadinessVerdict
): string {
  const normalizedAction = action.replace(/-/g, "_");
  if (normalizedAction === "apply_or_stage" && verdict.tone === "good") {
    return "Selected edge runtime is ready for field apply";
  }
  if (normalizedAction === "apply_or_stage") {
    return "Selected runtime is the best measured path";
  }
  if (normalizedAction === "use_best_runtime") {
    return decisionStatus === "selected_not_eligible"
      ? "Pinned runtime cannot host this edge model"
      : "A better measured runtime is available";
  }
  if (normalizedAction === "collect_missing_evidence") {
    return "Selected edge runtime needs fresh on-device proof";
  }
  if (normalizedAction === "resolve_blocking_gates") return "Selected edge runtime is blocked";
  return verdict.headline;
}

function executionContractTone({
  action,
  decisionStatus,
  productionAdmission,
  readinessVerdict
}: {
  action: string;
  decisionStatus: string;
  productionAdmission: JsonObject;
  readinessVerdict: ReadinessVerdict;
}): GateTone {
  const normalizedAction = action.replace(/-/g, "_");
  if (productionAdmission.apply_allowed === false || decisionStatus === "selected_not_eligible") return "bad";
  if (normalizedAction === "resolve_blocking_gates") return "bad";
  if (normalizedAction === "use_best_runtime" || normalizedAction === "collect_missing_evidence") return "warn";
  if (normalizedAction === "apply_or_stage" && productionAdmission.apply_allowed === true) return "good";
  return readinessVerdict.tone;
}

function runtimeDecisionCandidates(
  runtimeDecision: JsonObject,
  runtimeFit: JsonObject,
  selectedRuntimeTargetId: string,
  bestRuntimeTargetId: string
): JsonObject[] {
  const direct = Array.isArray(runtimeDecision.top_candidates)
    ? runtimeDecision.top_candidates.map(asRecord)
    : [];
  if (direct.length) return direct.filter((candidate) => candidateRuntimeId(candidate) !== "runtime target");

  const targetSelection = asRecord(runtimeFit.target_selection);
  const alternatives = Array.isArray(targetSelection.alternatives)
    ? targetSelection.alternatives.map(asRecord)
    : [];
  if (alternatives.length) {
    return alternatives.filter((candidate) => candidateRuntimeId(candidate) !== "runtime target");
  }

  const score = numberOf(runtimeFit.score);
  if (!selectedRuntimeTargetId || selectedRuntimeTargetId.includes("missing")) return [];
  return [
    {
      rank: 1,
      runtime_target_id: selectedRuntimeTargetId,
      score,
      tier: stringOf(runtimeFit.tier, "selected"),
      runtime_lane: runtimeFit.runtime_lane,
      blocked: false,
      best: selectedRuntimeTargetId === bestRuntimeTargetId
    }
  ];
}

function runtimeTargetAssessments(
  runtimeDecision: JsonObject,
  runtimeFit: JsonObject,
  fallbackCandidates: JsonObject[]
): JsonObject[] {
  const direct = Array.isArray(runtimeDecision.target_assessments)
    ? runtimeDecision.target_assessments.map(asRecord)
    : [];
  if (direct.length) return direct.filter((assessment) => candidateRuntimeId(assessment) !== "runtime target");

  const targetSelection = asRecord(runtimeFit.target_selection);
  const fromSelection = Array.isArray(targetSelection.target_assessments)
    ? targetSelection.target_assessments.map(asRecord)
    : [];
  if (fromSelection.length) {
    return fromSelection.filter((assessment) => candidateRuntimeId(assessment) !== "runtime target");
  }

  return fallbackCandidates;
}

function targetRuntimeCoverageSummary(
  assessments: JsonObject[]
): { value: string; detail: string; tone: GateTone } {
  if (!assessments.length) {
    return {
      value: "pending",
      detail: "runtime target coverage pending",
      tone: "neutral"
    };
  }
  const blocked = assessments.filter(targetAssessmentBlocked).length;
  const eligible = assessments.filter((assessment) => !targetAssessmentBlocked(assessment)).length;
  return {
    value: `${eligible}/${assessments.length} eligible`,
    detail: `${eligible} eligible / ${blocked} blocked`,
    tone: blocked ? (eligible ? "warn" : "bad") : "good"
  };
}

function operatorRuntimeLaneItems(
  assessments: JsonObject[],
  selectedRuntimeTargetId: string,
  bestRuntimeTargetId: string
): { detail: string; id: string; status: string; tone: GateTone }[] {
  return assessments
    .map((assessment, index) => ({ assessment, index }))
    .sort((left, right) => {
      const leftId = candidateRuntimeId(left.assessment);
      const rightId = candidateRuntimeId(right.assessment);
      const leftSelected = left.assessment.selected === true || leftId === selectedRuntimeTargetId;
      const rightSelected = right.assessment.selected === true || rightId === selectedRuntimeTargetId;
      if (leftSelected !== rightSelected) return leftSelected ? -1 : 1;
      const leftBest = left.assessment.best === true || leftId === bestRuntimeTargetId;
      const rightBest = right.assessment.best === true || rightId === bestRuntimeTargetId;
      if (leftBest !== rightBest) return leftBest ? -1 : 1;
      const leftBlocked = targetAssessmentBlocked(left.assessment);
      const rightBlocked = targetAssessmentBlocked(right.assessment);
      if (leftBlocked !== rightBlocked) return leftBlocked ? 1 : -1;
      const leftRank = numberOf(left.assessment.rank) ?? Number.MAX_SAFE_INTEGER;
      const rightRank = numberOf(right.assessment.rank) ?? Number.MAX_SAFE_INTEGER;
      if (leftRank !== rightRank) return leftRank - rightRank;
      return left.index - right.index;
    })
    .slice(0, 4)
    .map(({ assessment }) => {
      const id = candidateRuntimeId(assessment);
      const lane = runtimeLaneValue(asRecord(assessment.runtime_lane));
      const score = numberOf(assessment.score);
      const selected = assessment.selected === true || id === selectedRuntimeTargetId;
      const best = assessment.best === true || id === bestRuntimeTargetId;
      const blocked = targetAssessmentBlocked(assessment);
      const remediation = asRecord(assessment.remediation);
      const status = selected
        ? best
          ? "selected best"
          : "selected"
        : best
          ? "best alternate"
          : blocked
            ? "blocked"
            : "eligible";
      const detailSource = Object.keys(remediation).length
        ? targetAssessmentRemediationDetail(remediation)
        : targetAssessmentDetail(assessment);
      const detailParts = [lane, score !== undefined ? `${score}/100` : "", detailSource].filter(Boolean);
      return {
        detail: detailParts.join(" / "),
        id,
        status,
        tone: targetAssessmentTone(assessment)
      };
    });
}

function targetAssessmentBlocked(assessment: JsonObject): boolean {
  const status = stringOf(assessment.status, "").toLowerCase();
  return assessment.blocked === true || status === "blocked";
}

function targetAssessmentTone(assessment: JsonObject): GateTone {
  const status = stringOf(assessment.status, "");
  if (targetAssessmentBlocked(assessment)) return "bad";
  const penalties = stringsOf(assessment.penalties);
  if (penalties.length) return "warn";
  if (assessment.selected === true || assessment.best === true) return "good";
  return "neutral";
}

function runtimeWorkbenchRows({
  assessments,
  bestRuntimeTargetId,
  device,
  model,
  runtimeFit,
  runtimeWorkbench,
  runtimeTargets,
  runtimeValidations,
  selectedRuntimeTargetId
}: {
  assessments: JsonObject[];
  bestRuntimeTargetId: string;
  device: Device | undefined;
  model: ModelRecord | undefined;
  runtimeFit: JsonObject;
  runtimeWorkbench: JsonObject;
  runtimeTargets: RuntimeTarget[];
  runtimeValidations: RuntimeValidation[];
  selectedRuntimeTargetId: string;
}): RuntimeWorkbenchRow[] {
  const contractRows = runtimeWorkbenchContractRows(runtimeWorkbench, runtimeTargets);
  if (contractRows.length) return contractRows;

  const selectedScore = numberOf(runtimeFit.score);
  const assessmentByTarget = new Map(assessments.map((assessment) => [candidateRuntimeId(assessment), assessment]));
  const initialRows = runtimeTargets.map((target) => {
    const targetId = runtimeTargetId(target);
    const assessment = assessmentByTarget.get(targetId);
    const selected = targetId === selectedRuntimeTargetId;
    const validation = model ? runtimeValidationForModel(model, target, runtimeValidations) : undefined;
    const compatible = model ? targetSupportsModel(target, model) : false;
    const inventoryFailures = device ? runtimeTargetInventoryFailures(target, device) : ["edge inventory missing"];
    const benchmark = runtimeWorkbenchBenchmarkLabel(model, device, targetId);
    const remediation = asRecord(assessment?.remediation);
    const actionKind = stringOf(remediation.action, "");
    const actionLabel = stringOf(remediation.label, actionKind.replace(/_/g, " "));
    const assessedScore = numberOf(assessment?.score);
    const fallbackScore = runtimeWorkbenchFallbackScore({
      benchmark,
      compatible,
      inventoryFailures,
      selected,
      selectedScore,
      validation
    });
    const score = assessedScore ?? fallbackScore;
    const tone = assessment
      ? targetAssessmentTone(assessment)
      : runtimeWorkbenchFallbackTone({
          compatible,
          inventoryFailures,
          validation
        });
    const status = runtimeWorkbenchStatus({
      assessment,
      best: targetId === bestRuntimeTargetId,
      compatible,
      selected,
      validation
    });
    return {
      actionKind,
      actionLabel,
      actionRequiresEdge: remediation.requires_edge_execution === true,
      benchmark,
      best: targetId === bestRuntimeTargetId,
      capabilitySha256: stringOf(asRecord(assessment?.runtime_capability_lock).capability_sha256, ""),
      compatible,
      detail: runtimeWorkbenchDetail({
        assessment,
        compatible,
        inventoryFailures,
        model,
        target,
        validation
      }),
      inventory: inventoryFailures.length ? compactMetricDetail(inventoryFailures[0]) : "inventory match",
      lane: runtimeLaneValue(asRecord(assessment?.runtime_lane).lane_id ? asRecord(assessment?.runtime_lane) : asRecord(target.runtime_lane)),
      penalties: stringsOf(assessment?.penalties),
      rank: numberOf(assessment?.rank),
      reasons: stringsOf(assessment?.reasons),
      remediation,
      score,
      selected,
      status,
      target,
      targetId,
      tone,
      traceMetrics: runtimeWorkbenchFallbackTraceMetrics({
        benchmark,
        compatible,
        inventoryFailures,
        validation
      }),
      validated: Boolean(validation)
    };
  });
  const derivedBestTargetId =
    bestRuntimeTargetId ||
    [...initialRows]
      .filter((row) => row.compatible && row.tone !== "bad")
      .sort(runtimeWorkbenchScoreSort)[0]?.targetId ||
    "";
  return initialRows
    .map((row) => ({ ...row, best: row.targetId === derivedBestTargetId }))
    .sort(runtimeWorkbenchRowSort);
}

function runtimeWorkbenchContractRows(
  runtimeWorkbench: JsonObject,
  runtimeTargets: RuntimeTarget[]
): RuntimeWorkbenchRow[] {
  if (runtimeWorkbench.schema_version !== "temms-runtime-workbench/v1") return [];
  const targets = Array.isArray(runtimeWorkbench.targets)
    ? runtimeWorkbench.targets.map(asRecord)
    : [];
  if (!targets.length) return [];
  const targetById = new Map(runtimeTargets.map((target) => [runtimeTargetId(target), target]));
  const rows: RuntimeWorkbenchRow[] = [];
  targets.forEach((target) => {
    const targetId = stringOf(target.runtime_target_id, "");
    if (!targetId) return;
    const runtimeTarget = targetById.get(targetId) ?? { runtime_target_id: targetId };
    const proof = asRecord(target.proof);
    const status = stringOf(target.status, "unknown").replace(/_/g, " ");
    const eligible = target.eligible !== false && status !== "blocked";
    const score = numberOf(target.score);
    const benchmark = runtimeWorkbenchContractBenchmark(proof);
    const inventory = runtimeWorkbenchContractInventory(proof, target);
    const remediation = asRecord(target.remediation);
    const actionKind = stringOf(remediation.action, stringOf(asRecord(target.action).kind, ""));
    const actionLabel = stringOf(
      remediation.label,
      stringOf(asRecord(target.action).label, actionKind.replace(/_/g, " "))
    );
    rows.push({
      actionKind,
      actionLabel,
      actionRequiresEdge: remediation.requires_edge_execution === true || asRecord(target.action).requires_edge_execution === true,
      benchmark,
      best: target.best === true,
      capabilitySha256: stringOf(proof.capability_sha256, ""),
      compatible: eligible,
      detail: compactMetricDetail(stringOf(target.detail, runtimeWorkbenchContractDetail(target))),
      inventory,
      lane: runtimeLaneValue(asRecord(target.runtime_lane)),
      penalties: stringsOf(target.penalties),
      rank: numberOf(target.rank),
      reasons: stringsOf(target.reasons),
      remediation,
      score,
      selected: target.selected === true,
      status,
      target: runtimeTarget,
      targetId,
      tone: runtimeWorkbenchTargetTone(target),
      traceMetrics: runtimeWorkbenchContractTraceMetrics(target, proof),
      validated: runtimeWorkbenchTargetValidated(proof)
    });
  });
  return rows.sort(runtimeWorkbenchRowSort);
}

function runtimeWorkbenchContractBenchmark(proof: JsonObject): string {
  const latency = numberOf(proof.latency_ms_p95);
  const throughput = numberOf(proof.throughput_ips);
  const benchmarkId = stringOf(proof.benchmark_id, "");
  const parts = [];
  if (latency !== undefined) parts.push(`${formatMetricNumber(latency)} ms p95`);
  if (throughput !== undefined) parts.push(`${formatThroughput(throughput)} ips`);
  if (parts.length) return parts.join(" / ");
  return benchmarkId ? `benchmark ${benchmarkId}` : "no benchmark";
}

function runtimeWorkbenchContractInventory(proof: JsonObject, target: JsonObject): string {
  const telemetry = stringOf(proof.telemetry_state, stringOf(proof.telemetry_status, "")).replace(/_/g, " ");
  const capability = stringOf(proof.capability_lock_status, "");
  if (capability || telemetry) {
    return [capability ? `capability ${capability}` : "", telemetry].filter(Boolean).join(" / ");
  }
  const penalties = stringsOf(target.penalties);
  return penalties.length ? compactMetricDetail(penalties[0]) : "inventory match";
}

function runtimeWorkbenchContractDetail(target: JsonObject): string {
  const reasons = stringsOf(target.reasons);
  if (reasons.length) return reasons[0];
  const penalties = stringsOf(target.penalties);
  if (penalties.length) return penalties[0];
  const proof = asRecord(target.proof);
  return stringOf(proof.performance_state, stringOf(proof.runtime_validation_state, "runtime target assessed"));
}

function runtimeWorkbenchContractTraceMetrics(target: JsonObject, proof: JsonObject): RuntimeWorkbenchTraceMetric[] {
  const validationId = stringOf(proof.validation_id, "");
  const benchmarkId = stringOf(proof.benchmark_id, "");
  const capabilityDigest = stringOf(proof.capability_sha256, "");
  const metrics: RuntimeWorkbenchTraceMetric[] = [
    runtimeWorkbenchTraceMetric(
      "validation",
      validationId ? "present" : runtimeWorkbenchProofValue(proof.runtime_validation_status, proof.runtime_validation_state, "pending"),
      validationId || runtimeWorkbenchProofValue(proof.runtime_validation_state, proof.runtime_validation_status, "runtime validation not retained"),
      runtimeWorkbenchProofTone(proof.runtime_validation_status, proof.runtime_validation_state, validationId)
    ),
    runtimeWorkbenchTraceMetric(
      "benchmark",
      benchmarkId ? "present" : runtimeWorkbenchProofValue(proof.performance_status, proof.performance_state, "pending"),
      benchmarkId || runtimeWorkbenchContractBenchmark(proof),
      runtimeWorkbenchProofTone(proof.performance_status, proof.performance_state, benchmarkId)
    ),
    runtimeWorkbenchTraceMetric(
      "resources",
      runtimeWorkbenchProofValue(proof.resource_status, proof.resource_state, "pending"),
      runtimeWorkbenchProofValue(proof.resource_state, proof.resource_status, "resource envelope not retained"),
      runtimeWorkbenchProofTone(proof.resource_status, proof.resource_state)
    ),
    runtimeWorkbenchTraceMetric(
      "telemetry",
      runtimeWorkbenchProofValue(proof.telemetry_status, proof.telemetry_state, "pending"),
      runtimeWorkbenchProofValue(proof.telemetry_state, proof.telemetry_status, "heartbeat state not retained"),
      runtimeWorkbenchProofTone(proof.telemetry_status, proof.telemetry_state)
    ),
    runtimeWorkbenchTraceMetric(
      "capability",
      stringOf(proof.capability_lock_status, capabilityDigest ? "hash locked" : "pending"),
      capabilityDigest ? `sha256 ${capabilityDigest.slice(0, 12)}` : runtimeWorkbenchContractInventory(proof, target),
      runtimeWorkbenchProofTone(proof.capability_lock_status, undefined, capabilityDigest)
    )
  ];
  return metrics;
}

function runtimeWorkbenchFallbackTraceMetrics({
  benchmark,
  compatible,
  inventoryFailures,
  validation
}: {
  benchmark: string;
  compatible: boolean;
  inventoryFailures: string[];
  validation: RuntimeValidation | undefined;
}): RuntimeWorkbenchTraceMetric[] {
  return [
    runtimeWorkbenchTraceMetric(
      "compatibility",
      compatible ? "eligible" : "blocked",
      compatible ? "model constraints match runtime target" : "model/runtime constraints do not match",
      compatible ? "good" : "bad"
    ),
    runtimeWorkbenchTraceMetric(
      "validation",
      validation ? "present" : "missing",
      validation ? runtimeWorkbenchValidationDetail(validation) : "non-dry-run runtime validation required",
      validation ? "good" : "warn"
    ),
    runtimeWorkbenchTraceMetric(
      "benchmark",
      benchmark === "no benchmark" ? "missing" : "present",
      benchmark,
      benchmark === "no benchmark" ? "warn" : "good"
    ),
    runtimeWorkbenchTraceMetric(
      "inventory",
      inventoryFailures.length ? "blocked" : "match",
      inventoryFailures.length ? compactMetricDetail(inventoryFailures[0]) : "live edge inventory matches",
      inventoryFailures.length ? "bad" : "good"
    )
  ];
}

function runtimeWorkbenchTraceMetric(
  label: string,
  value: string,
  detail: string,
  tone: GateTone
): RuntimeWorkbenchTraceMetric {
  return {
    detail: compactMetricDetail(detail || "not retained"),
    label,
    tone,
    value: value.replace(/_/g, " ") || "pending"
  };
}

function runtimeWorkbenchProofValue(primary: unknown, secondary: unknown, fallback: string): string {
  return stringOf(primary, stringOf(secondary, fallback)).replace(/_/g, " ");
}

function runtimeWorkbenchProofTone(primary: unknown, secondary?: unknown, retainedEvidence?: string): GateTone {
  const value = `${stringOf(primary, "")} ${stringOf(secondary, "")}`.toLowerCase();
  if (retainedEvidence) return "good";
  if (value.includes("blocked") || value.includes("fail") || value.includes("missing")) return "bad";
  if (value.includes("attention") || value.includes("warn") || value.includes("stale") || value.includes("pending")) return "warn";
  if (value.includes("go") || value.includes("pass") || value.includes("eligible") || value.includes("locked") || value.includes("fresh")) return "good";
  return "neutral";
}

function runtimeWorkbenchValidationDetail(validation: RuntimeValidation): string {
  const validationId = stringOf(validation.validation_id, "");
  const createdAt = compactDate(validation.created_at);
  return [validationId || "runtime validation retained", createdAt].filter(Boolean).join(" / ");
}

function runtimeWorkbenchTraceRank(row: RuntimeWorkbenchRow): string {
  if (row.rank !== undefined) return `rank ${row.rank}`;
  if (row.selected && row.best) return "selected best";
  if (row.selected) return "selected";
  if (row.best) return "best";
  return row.status;
}

function runtimeWorkbenchTraceBadge(row: RuntimeWorkbenchRow): string {
  const labels = [];
  if (row.selected) labels.push("selected");
  if (row.best) labels.push("best");
  if (!labels.length) labels.push(row.compatible ? "eligible" : "blocked");
  if (row.score !== undefined) labels.push(`${row.score}/100`);
  return labels.join(" / ");
}

function runtimeWorkbenchTraceReason(row: RuntimeWorkbenchRow): string {
  return (
    row.reasons[0] ||
    row.penalties[0] ||
    stringOf(row.remediation.detail, "") ||
    row.detail ||
    "runtime target assessed"
  );
}

function runtimeWorkbenchTraceActionDetail(row: RuntimeWorkbenchRow): string {
  return (
    stringOf(row.remediation.operator_command_note, "") ||
    stringOf(row.remediation.edge_command_note, "") ||
    stringOf(row.remediation.detail, "") ||
    row.detail ||
    "review this runtime path"
  );
}

function runtimeWorkbenchRowRemediationCommand(
  row: RuntimeWorkbenchRow,
  context: RuntimeRemediationContext
): RuntimeRemediationCommand | undefined {
  if (!row.actionKind) return undefined;
  return runtimeTargetAssessmentRemediationCommand(
    {
      remediation: row.remediation,
      runtime_lane: row.target.runtime_lane,
      runtime_target_id: row.targetId
    },
    context
  );
}

function runtimeWorkbenchTargetTone(target: JsonObject): GateTone {
  const status = stringOf(target.status, "");
  if (status === "blocked" || target.eligible === false) return "bad";
  if (target.selected === true || target.best === true) return "good";
  const penalties = stringsOf(target.penalties);
  return penalties.length ? "warn" : "neutral";
}

function runtimeWorkbenchTargetValidated(proof: JsonObject): boolean {
  const validationStatus = stringOf(proof.runtime_validation_status, "");
  const validationState = stringOf(proof.runtime_validation_state, "").toLowerCase();
  return Boolean(proof.validation_id) || validationStatus === "go" || validationState.includes("validated");
}

function runtimeWorkbenchFallbackScore({
  benchmark,
  compatible,
  inventoryFailures,
  selected,
  selectedScore,
  validation
}: {
  benchmark: string;
  compatible: boolean;
  inventoryFailures: string[];
  selected: boolean;
  selectedScore?: number;
  validation: RuntimeValidation | undefined;
}): number | undefined {
  if (selected && selectedScore !== undefined) return selectedScore;
  if (!compatible) return 0;
  let score = 48;
  if (validation) score += 18;
  if (benchmark.startsWith("fresh")) score += 19;
  else if (benchmark !== "no benchmark") score += 8;
  if (!inventoryFailures.length) score += 15;
  return Math.min(score, 95);
}

function runtimeWorkbenchFallbackTone({
  compatible,
  inventoryFailures,
  validation
}: {
  compatible: boolean;
  inventoryFailures: string[];
  validation: RuntimeValidation | undefined;
}): GateTone {
  if (!compatible || inventoryFailures.length) return "bad";
  return validation ? "good" : "warn";
}

function runtimeWorkbenchStatus({
  assessment,
  best,
  compatible,
  selected,
  validation
}: {
  assessment: JsonObject | undefined;
  best: boolean;
  compatible: boolean;
  selected: boolean;
  validation: RuntimeValidation | undefined;
}): string {
  const assessedStatus = stringOf(assessment?.status, "");
  if (assessedStatus) return assessedStatus.replace(/_/g, " ");
  if (!compatible) return "blocked";
  if (selected && best) return "selected best";
  if (selected) return "selected";
  if (best) return "best alternate";
  return validation ? "eligible" : "needs proof";
}

function runtimeWorkbenchDetail({
  assessment,
  compatible,
  inventoryFailures,
  model,
  target,
  validation
}: {
  assessment: JsonObject | undefined;
  compatible: boolean;
  inventoryFailures: string[];
  model: ModelRecord | undefined;
  target: RuntimeTarget;
  validation: RuntimeValidation | undefined;
}): string {
  if (assessment) return targetAssessmentDetail(assessment);
  if (!compatible) return "runtime target does not satisfy model constraints";
  if (inventoryFailures.length) return compactMetricDetail(inventoryFailures[0]);
  if (validation) return `${runtimeTargetId(target)} passed package validation`;
  if (model) return `${formatBenchmark(model)}; validation required for ${runtimeTargetId(target)}`;
  return "select a model to evaluate this target runtime";
}

function runtimeWorkbenchBenchmarkLabel(
  model: ModelRecord | undefined,
  device: Device | undefined,
  targetId: string
): string {
  if (!model) return "no model";
  const targetMatches = model.benchmarkRuntimeId === targetId;
  const deviceMatches = device && model.benchmarkDeviceId === deviceId(device);
  if (!targetMatches && !deviceMatches) return "no benchmark";
  const freshness = benchmarkFreshness(model).state;
  const benchmark = formatBenchmark(model);
  if (targetMatches && deviceMatches) return `${freshness} ${benchmark}`;
  if (targetMatches) return `${freshness} ${benchmark} on another edge`;
  return `${freshness} ${benchmark} on another runtime`;
}

function runtimeWorkbenchRowSort(left: RuntimeWorkbenchRow, right: RuntimeWorkbenchRow): number {
  if (left.selected !== right.selected) return left.selected ? -1 : 1;
  if (left.best !== right.best) return left.best ? -1 : 1;
  return runtimeWorkbenchScoreSort(left, right);
}

function runtimeWorkbenchScoreSort(left: RuntimeWorkbenchRow, right: RuntimeWorkbenchRow): number {
  const leftScore = left.score ?? -1;
  const rightScore = right.score ?? -1;
  if (leftScore !== rightScore) return rightScore - leftScore;
  if (left.compatible !== right.compatible) return left.compatible ? -1 : 1;
  return left.targetId.localeCompare(right.targetId);
}

function targetAssessmentDetail(assessment: JsonObject): string {
  const penalties = stringsOf(assessment.penalties);
  if (penalties.length) return compactMetricDetail(penalties[0]);
  const reasons = stringsOf(assessment.reasons);
  if (reasons.length) return compactMetricDetail(reasons[0]);
  const detail = stringOf(assessment.detail, "");
  if (detail) return compactMetricDetail(detail);
  const artifact = asRecord(assessment.artifact_lane);
  if (Object.keys(artifact).length) return artifactLaneDetail(artifact);
  return runtimeLaneDetail(asRecord(assessment.runtime_lane));
}

function targetAssessmentRemediationDetail(remediation: JsonObject): string {
  const label = stringOf(remediation.label, "");
  const detail = compactMetricDetail(stringOf(remediation.detail, ""));
  if (label && detail) return `${label} - ${detail}`;
  return label || detail || "Review this runtime target";
}

function runtimeTargetAssessmentRemediationCommand(
  assessment: JsonObject,
  context: RuntimeRemediationContext
): RuntimeRemediationCommand | undefined {
  const remediation = asRecord(assessment.remediation);
  const action = stringOf(remediation.action, "");
  if (!action) return undefined;

  const refs = asRecord(remediation.refs);
  const runtimeTargetIdValue = stringOf(refs.runtime_target_id, candidateRuntimeId(assessment));
  if (!runtimeTargetIdValue || runtimeTargetIdValue === "runtime target") return undefined;

  const actionLabel = stringOf(remediation.label, action.replace(/_/g, " "));
  const contractCommand = runtimeTargetContractRemediationCommand(
    remediation,
    runtimeTargetIdValue,
    action,
    actionLabel
  );
  if (contractCommand) return contractCommand;

  const packageIdValue = context.packageId || stringOf(refs.package_id, "<package-id>");
  const modelIdValue = context.modelId || stringOf(refs.model_id, "<model-id>");
  const deviceIdValue = context.deviceId || stringOf(refs.device_id, "<device-id>");
  const slotValue = context.slot || stringOf(refs.slot, "vision");
  const hubUrl = currentHubUrl();

  if (action === "record_benchmark") {
    return {
      action,
      label: `${runtimeTargetIdValue} benchmark command`,
      edgeRun: true,
      note: "Run on the selected edge after the model package is cached.",
      command: formatProofCommand([
        "temms",
        "benchmark",
        modelIdValue || "<model-id>",
        "--slot",
        slotValue,
        "--samples",
        "10",
        "--warmup",
        "2",
        "--hub-url",
        hubUrl,
        "--device-id",
        deviceIdValue || "<device-id>",
        "--package-id",
        packageIdValue || "<package-id>",
        "--runtime-target-id",
        runtimeTargetIdValue,
        "--actor",
        "edge-agent"
      ])
    };
  }

  if (action === "validate_runtime") {
    return {
      action,
      label: `${runtimeTargetIdValue} validation command`,
      edgeRun: false,
      note: "Replace the package path with the signed TEMMS package artifact.",
      command: formatProofCommand([
        "uv",
        "run",
        "temms",
        "hub",
        "validate-runtime",
        "<package-path>",
        "--hub-url",
        hubUrl,
        "--package-id",
        packageIdValue || "<package-id>",
        "--runtime-target-id",
        runtimeTargetIdValue,
        "--actor",
        "operator:runtime-remediation",
        "--require-signature"
      ])
    };
  }

  if (action === "refresh_edge_inventory") {
    return {
      action,
      label: `${deviceIdValue || "edge"} heartbeat command`,
      edgeRun: true,
      note: "Run on the edge node to refresh runtime/provider inventory and heartbeat freshness.",
      command: formatProofCommand([
        `TEMMS_HUB_URL=${hubUrl}`,
        `TEMMS_DEVICE_ID=${deviceIdValue || "<device-id>"}`,
        "TEMMS_EDGE_HEARTBEAT_INTERVAL_S=10",
        "temms",
        "daemon",
        "start",
        "--foreground"
      ])
    };
  }

  if (action === "package_runtime_artifact") {
    const lane = asRecord(assessment.runtime_lane);
    const providers = stringsOf(lane.providers);
    const accelerators = stringsOf(lane.accelerators);
    const engine = stringOf(lane.execution_engine, "");
    const commandParts = [
      "uv",
      "run",
      "temms",
      "hub",
      "package-from-mlflow",
      "<model-uri>",
      "--hub-url",
      hubUrl,
      "--slot",
      slotValue,
      "--model-artifact",
      "<runtime-native-artifact-path>",
      "--actor",
      "operator:runtime-remediation"
    ];
    if (engine) commandParts.push("--runtime", engine);
    providers.forEach((provider) => commandParts.push("--provider", provider));
    accelerators.forEach((accelerator) => commandParts.push("--accelerator", accelerator));
    return {
      action,
      label: `${runtimeTargetIdValue} packaging command`,
      edgeRun: false,
      note: "Package a runtime-native artifact, then re-run validation and proof.",
      command: formatProofCommand(commandParts)
    };
  }

  if (["select_matching_edge_class", "resolve_runtime_capability", "free_edge_resources", "resolve_target_blocker"].includes(action)) {
    return {
      action,
      label: `${runtimeTargetIdValue} compatibility inspection`,
      edgeRun: false,
      note: `${actionLabel} with live inventory and model/runtime constraints.`,
      command: formatProofCommand([
        "uv",
        "run",
        "temms",
        "hub",
        "compatibility-matrix",
        "--hub-url",
        hubUrl,
        "--device-id",
        deviceIdValue || "<device-id>",
        "--package-id",
        packageIdValue || "<package-id>",
        "--model-id",
        modelIdValue || "<model-id>",
        "--runtime-target-id",
        runtimeTargetIdValue,
        "--include-device-inventory",
        "--json"
      ])
    };
  }

  return {
    action,
    label: `${runtimeTargetIdValue} proof check`,
    edgeRun: false,
    note: `${actionLabel} against the signed edge-runtime gate.`,
    command: formatProofCommand([
      "uv",
      "run",
      "temms",
      "hub",
      "edge-runtime-mission",
      "--hub-url",
      hubUrl,
      "--package-id",
      packageIdValue || "<package-id>",
      "--model-id",
      modelIdValue || "<model-id>",
      "--device-id",
      deviceIdValue || "<device-id>",
      "--runtime-target-id",
      runtimeTargetIdValue,
      "--slot",
      slotValue,
      "--require-go",
      "--require-best-runtime",
      "--require-capability-lock",
      "--min-runtime-fit",
      "95",
      "--json"
    ])
  };
}

function runtimeTargetContractRemediationCommand(
  remediation: JsonObject,
  runtimeTargetIdValue: string,
  action: string,
  actionLabel: string
): RuntimeRemediationCommand | undefined {
  const commandRecord = asRecord(remediation.command);
  const edgeCommandText = stringOf(
    remediation.edge_command_text,
    stringOf(commandRecord.edge_command_text, "")
  );
  if (edgeCommandText) {
    return {
      action,
      label: `${runtimeTargetIdValue} edge command`,
      edgeRun: true,
      note: stringOf(
        remediation.edge_command_note,
        stringOf(commandRecord.edge_command_note, "Run this command on the selected edge node.")
      ),
      command: localizeHubCommandText(edgeCommandText)
    };
  }

  const operatorCommandText = stringOf(
    remediation.operator_command_text,
    stringOf(commandRecord.operator_command_text, "")
  );
  if (operatorCommandText) {
    return {
      action,
      label: `${runtimeTargetIdValue} operator command`,
      edgeRun: remediation.requires_edge_execution === true,
      note: stringOf(
        remediation.operator_command_note,
        stringOf(commandRecord.operator_command_note, `${actionLabel} against the current edge-runtime contract.`)
      ),
      command: localizeHubCommandText(operatorCommandText)
    };
  }

  const edgeCommand = stringsOf(remediation.edge_command).length
    ? stringsOf(remediation.edge_command)
    : stringsOf(commandRecord.edge_command);
  if (edgeCommand.length) {
    return {
      action,
      label: `${runtimeTargetIdValue} edge command`,
      edgeRun: true,
      note: stringOf(
        remediation.edge_command_note,
        stringOf(commandRecord.edge_command_note, "Run this command on the selected edge node.")
      ),
      command: formatProofCommand(edgeCommand.map(localizeHubCommandPart))
    };
  }

  const operatorCommand = stringsOf(remediation.operator_command).length
    ? stringsOf(remediation.operator_command)
    : stringsOf(commandRecord.operator_command);
  if (operatorCommand.length) {
    return {
      action,
      label: `${runtimeTargetIdValue} operator command`,
      edgeRun: remediation.requires_edge_execution === true,
      note: stringOf(
        remediation.operator_command_note,
        stringOf(commandRecord.operator_command_note, `${actionLabel} against the current edge-runtime contract.`)
      ),
      command: formatProofCommand(operatorCommand.map(localizeHubCommandPart))
    };
  }

  return undefined;
}

function localizeHubCommandText(command: string): string {
  return command.split("${TEMMS_HUB_URL}").join(currentHubUrl());
}

function localizeHubCommandPart(part: string): string {
  return part.split("${TEMMS_HUB_URL}").join(currentHubUrl());
}

function runtimeTargetComponentProofs(
  assessment: JsonObject
): { key: string; label: string; state: string; score: string; tone: GateTone }[] {
  const components = asRecord(assessment.component_states);
  const specs: { key: string; label: string }[] = [
    { key: "compatibility", label: "compat" },
    { key: "runtime_validation", label: "valid" },
    { key: "performance", label: "perf" },
    { key: "resource", label: "res" },
    { key: "telemetry", label: "telemetry" }
  ];
  return specs
    .map(({ key, label }) => {
      const component = asRecord(components[key]);
      const state = componentProofState(component);
      if (!state) return undefined;
      const score = componentProofScore(component);
      return {
        key,
        label,
        state,
        score,
        tone: componentProofTone(component, state)
      };
    })
    .filter((value): value is { key: string; label: string; state: string; score: string; tone: GateTone } => Boolean(value));
}

function componentProofState(component: JsonObject): string {
  return stringOf(component.state, stringOf(component.status, "")).replace(/_/g, " ");
}

function componentProofScore(component: JsonObject): string {
  const score = numberOf(component.score);
  const maxScore = numberOf(component.max_score);
  if (score === undefined) return "";
  return maxScore !== undefined ? `${score}/${maxScore}` : `${score}`;
}

function componentProofTone(component: JsonObject, state: string): GateTone {
  const status = stringOf(component.status, "").toLowerCase();
  const normalized = state.toLowerCase();
  if (status === "blocked" || normalized.includes("blocked") || normalized.includes("miss")) return "bad";
  if (
    status === "attention" ||
    normalized.includes("missing") ||
    normalized.includes("stale") ||
    normalized.includes("unknown")
  ) {
    return "warn";
  }
  if (
    status === "go" ||
    normalized.includes("compatible") ||
    normalized.includes("validated") ||
    normalized.includes("met") ||
    normalized.includes("fresh")
  ) {
    return "good";
  }
  return "neutral";
}

function runtimeDecisionGates(value: unknown): JsonObject[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(asRecord)
    .filter((gate) => stringOf(gate.gate_id, "") || stringOf(gate.label, ""));
}

function candidateRuntimeId(candidate: JsonObject): string {
  return stringOf(candidate.runtime_target_id, "runtime target");
}

function runtimeCandidateTone(
  candidate: JsonObject,
  candidateId: string,
  selectedRuntimeTargetId: string,
  bestRuntimeTargetId: string
): GateTone {
  if (candidate.blocked === true) return "bad";
  if (candidateId === bestRuntimeTargetId) return "good";
  if (candidateId === selectedRuntimeTargetId && bestRuntimeTargetId !== selectedRuntimeTargetId) return "warn";
  return "neutral";
}

function formatSignedPercent(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return `${rounded >= 0 ? "+" : ""}${rounded}%`;
}

function formatSignedMb(value: number): string {
  const rounded = Math.round(value);
  return `${rounded >= 0 ? "+" : ""}${rounded} MB`;
}

function recommendationTone(recommendation: EdgeRecommendation): GateTone {
  if (recommendation.decision === "deploy") return "good";
  if (recommendation.decision === "blocked") return "bad";
  return "warn";
}

function runtimeTargetSelectionValue(selection: JsonObject): string {
  const status = stringOf(selection.status, "");
  const selectedRank = numberOf(selection.selected_rank);
  const eligibleCount = numberOf(selection.eligible_target_count);
  if (status === "best") {
    return selectedRank !== undefined && eligibleCount !== undefined
      ? `#${selectedRank} of ${eligibleCount}`
      : "best target";
  }
  if (status === "upgrade_available") return "upgrade available";
  if (status === "selected_not_eligible") return "not eligible";
  if (status === "no_eligible_targets") return "no target";
  return "comparison pending";
}

function runtimeTargetSelectionDetail(selection: JsonObject): string {
  const detail = stringOf(selection.detail, "");
  if (detail) return detail;
  const bestTarget = stringOf(selection.best_runtime_target_id, "");
  const bestScore = numberOf(selection.best_score);
  if (bestTarget) {
    return bestScore !== undefined
      ? `Best measured target is ${bestTarget} at ${bestScore}/100`
      : `Best measured target is ${bestTarget}`;
  }
  return "No alternate runtime target comparison is available yet.";
}

function runtimeTargetSelectionTone(selection: JsonObject): GateTone {
  const status = stringOf(selection.status, "");
  if (status === "best") return "good";
  if (status === "upgrade_available") return "warn";
  if (status === "selected_not_eligible" || status === "no_eligible_targets") return "bad";
  return "neutral";
}

function productionAdmissionValue(admission: JsonObject): string {
  if (admission.apply_allowed === true) return "permitted";
  if (admission.apply_allowed === false) return "blocked";
  return "pending";
}

function productionAdmissionDetail(admission: JsonObject): string {
  const detail = stringOf(admission.detail, "");
  const blockers = numberOf(admission.blocking_gate_count);
  if (detail && blockers && blockers > 0) return `${detail}; ${blockers} blocking gate${blockers === 1 ? "" : "s"}`;
  if (detail) return detail;
  return "waiting for Hub admission gates";
}

function productionAdmissionTone(admission: JsonObject): GateTone {
  if (admission.apply_allowed === true) return "good";
  if (admission.apply_allowed === false) return "bad";
  return "neutral";
}

function edgeRuntimeMissionFromApi(value: unknown): EdgeRuntimeMission | undefined {
  const mission = asRecord(value);
  if (mission.schema_version !== "temms-edge-runtime-mission/v1") return undefined;
  const metrics = asRecord(mission.metrics);
  const path = asRecord(mission.path);
  const metricRows: EdgeMissionMetric[] = [
    edgeRuntimeMissionMetric(metrics.runtime_fit, "Runtime fit", apiScoreOrState),
    edgeRuntimeMissionMetric(metrics.runtime_lane, "Runtime lane", apiRuntimeLaneValue),
    edgeRuntimeMissionMetric(metrics.artifact_fit, "Artifact", apiStateOrFormat),
    edgeRuntimeMissionMetric(metrics.live_inventory, "Live inventory", apiStateValue),
    edgeRuntimeMissionMetric(metrics.performance, "Performance", apiStateValue),
    edgeRuntimeMissionMetric(metrics.resources, "Resources", apiStateValue),
    edgeRuntimeMissionMetric(metrics.runtime_validation, "Validation", apiStateValue),
    apiRuntimeDecisionMetric(metrics.runtime_decision),
    edgeRuntimeMissionMetric(
      metrics.ddil_repair || metrics.production_admission,
      metrics.ddil_repair ? "DDIL repair" : "Production apply",
      apiStateValue
    )
  ];
  return {
    headline: stringOf(mission.headline, "Selected edge path needs review"),
    detail: stringOf(mission.detail, "Review the selected model/device/runtime path."),
    tone: toneForReadinessStatus(stringOf(mission.status, "")),
    path: stringOf(path.label, "model -> runtime -> edge"),
    metrics: metricRows,
    focus: stringsOf(mission.operator_focus).length
      ? stringsOf(mission.operator_focus)
      : ["Selected on-device gates are aligned"]
  };
}

function apiRuntimeDecisionMetric(value: unknown): EdgeMissionMetric {
  const metric = asRecord(value);
  const action = stringOf(metric.recommended_action, "review").replace(/_/g, " ");
  const best = stringOf(metric.best_runtime_target_id, "");
  const applyAllowed = metric.apply_allowed === true;
  const status = stringOf(metric.status, "");
  const detail = stringOf(
    metric.detail,
    best ? `best runtime ${best}` : "runtime decision evidence pending"
  );
  return {
    label: "Runtime decision",
    value: action,
    detail: compactMetricDetail(detail),
    tone:
      metric.apply_allowed === false || status === "selected_not_eligible"
        ? "bad"
        : status === "upgrade_available"
          ? "warn"
          : applyAllowed || action === "apply or stage"
            ? "good"
            : "neutral"
  };
}

function edgeRuntimeMissionMetric(
  value: unknown,
  label: string,
  valueFormatter: (metric: JsonObject) => string
): EdgeMissionMetric {
  const metric = asRecord(value);
  const status = stringOf(metric.status, "");
  return {
    label,
    value: valueFormatter(metric),
    detail: apiMetricDetail(metric, label),
    tone: toneForReadinessStatus(status)
  };
}

function apiMetricDetail(metric: JsonObject, label: string): string {
  const detail = stringOf(metric.detail, "");
  if (detail) return compactMetricDetail(detail);
  if (label === "Runtime lane") return runtimeLaneDetail(metric);
  if (label === "Production apply") return productionAdmissionDetail(metric);
  if (label === "Runtime fit") return apiScoreOrState(metric);
  if (label === "Artifact") return artifactLaneDetail(metric);
  return stringOf(metric.state, stringOf(metric.status, "No detail reported")).replace(/_/g, " ");
}

function compactMetricDetail(detail: string): string {
  return detail
    .replace(/(-?\d+(?:\.\d+)?)\s*ms/g, (_match, value) => `${formatMetricNumber(Number(value))} ms`)
    .replace(/(-?\d+(?:\.\d+)?)\s*ips/g, (_match, value) => `${formatThroughput(Number(value))} ips`)
    .replace(/(-?\d+(?:\.\d+)?)\s*MB/g, (_match, value) => `${Math.round(Number(value))} MB`);
}

function apiScoreOrState(metric: JsonObject): string {
  const score = numberOf(metric.score);
  const tier = stringOf(metric.tier, "").replace(/_/g, " ");
  if (score !== undefined) return tier ? `${score}/100 ${tier}` : `${score}/100`;
  return apiStateValue(metric);
}

function apiRuntimeLaneValue(metric: JsonObject): string {
  return stringOf(metric.label, stringOf(metric.lane_id, apiStateValue(metric)));
}

function apiStateOrFormat(metric: JsonObject): string {
  return stringOf(metric.state, stringOf(metric.model_format, apiStateValue(metric))).replace(/_/g, " ");
}

function apiStateValue(metric: JsonObject): string {
  return stringOf(metric.state, stringOf(metric.status, "unknown")).replace(/_/g, " ");
}

function buildEdgeRuntimeMission({
  device,
  edgeRuntimeFit,
  missionReplay,
  model,
  pendingOperationLedger,
  pendingOperations,
  readiness,
  readinessVerdict,
  replayBlockedOperations,
  resourceEnvelopeFit,
  runtime,
  runtimeFitDisplay,
  runtimeValidation
}: {
  device: Device | undefined;
  edgeRuntimeFit: EdgeRuntimeFit;
  missionReplay: MissionReplay | undefined;
  model: ModelRecord | undefined;
  pendingOperationLedger: Record<string, unknown>[];
  pendingOperations: number;
  readiness: DeploymentReadiness | undefined;
  readinessVerdict: ReadinessVerdict;
  replayBlockedOperations: number;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtime: RuntimeTarget | undefined;
  runtimeFitDisplay: RuntimeFitDisplay;
  runtimeValidation: RuntimeValidation | undefined;
}): EdgeRuntimeMission {
  const apiMission = edgeRuntimeMissionFromApi(readiness?.edge_runtime_mission);
  if (apiMission) return apiMission;
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const runtimeLane = runtimeLaneFor(runtimeFit, runtime);
  const artifactLane = asRecord(runtimeFit.artifact_lane);
  const ddilRepair = ddilRuntimeRepairMetric({
    missionReplay,
    pendingOperationLedger,
    pendingOperations,
    replayBlockedOperations
  });
  const modelLabel = model?.id ?? "model missing";
  const runtimeLabel = runtime ? runtimeTargetId(runtime) : "runtime missing";
  const deviceLabel = device ? deviceId(device) : "edge missing";
  const validationDetail = runtimeValidation
    ? `${runtimeLabel} passed package validation`
    : edgeRuntimeFit.detail;
  const inventoryTone = edgeRuntimeFit.tone === "bad" ? "bad" : runtimeInventoryTone(device);
  const focus = edgeMissionFocus({
    ddilRepair,
    edgeRuntimeFit,
    readinessVerdict,
    resourceEnvelopeFit,
    runtimeFit
  });
  const metrics: EdgeMissionMetric[] = [
    {
      label: "Runtime fit",
      value: runtimeFitDisplay.label,
      detail: runtimeFitDisplay.detail,
      tone: runtimeFitDisplay.tone
    },
    {
      label: "Runtime lane",
      value: runtimeLaneValue(runtimeLane),
      detail: runtimeLaneDetail(runtimeLane),
      tone: runtimeLaneTone(runtimeLane)
    },
    {
      label: "Artifact",
      value: artifactLaneMissionValue(artifactLane, model),
      detail: artifactLaneMissionDetail(artifactLane, model),
      tone: artifactLaneMissionTone(artifactLane)
    },
    {
      label: "Live inventory",
      value: runtimeInventoryLabel(device),
      detail: runtimeInventoryDetail(device),
      tone: inventoryTone
    },
    {
      label: "Performance",
      value: performanceSloLabel(model),
      detail: model ? performanceSloDetail(model) : "select a model",
      tone: performanceSloTone(model)
    },
    {
      label: "Resources",
      value: resourceEnvelopeFit.label,
      detail: resourceEnvelopeFit.detail,
      tone: resourceEnvelopeFit.tone
    },
    {
      label: "Validation",
      value: runtimeValidation ? "validated" : edgeRuntimeFit.label,
      detail: validationDetail,
      tone: runtimeValidation ? "good" : edgeRuntimeFit.tone
    },
    ddilRepair
  ];

  return {
    headline: edgeMissionHeadline(readinessVerdict),
    detail: edgeMissionDetail(readinessVerdict, model, runtime, device),
    tone: readinessVerdict.tone,
    path: `${modelLabel} -> ${runtimeLabel} -> ${deviceLabel}`,
    metrics,
    focus
  };
}

function edgeMissionHeadline(verdict: ReadinessVerdict): string {
  if (verdict.tone === "good") return "Selected model is proven for the edge path";
  if (verdict.tone === "bad") return "Selected edge path is blocked";
  if (verdict.tone === "warn") return "Selected edge path needs operator proof";
  return "Selected edge path is syncing";
}

function edgeMissionDetail(
  verdict: ReadinessVerdict,
  model: ModelRecord | undefined,
  runtime: RuntimeTarget | undefined,
  device: Device | undefined
): string {
  if (!model || !runtime || !device) return "Select a model, runtime target, and edge node to evaluate on-device deployment.";
  return `${verdict.nextAction} (${model.format || "artifact"} on ${runtimeTargetId(runtime)} at ${deviceId(device)}).`;
}

function artifactLaneMissionValue(artifactLane: JsonObject, model: ModelRecord | undefined): string {
  const value = artifactLaneValue(artifactLane);
  if (value !== "not classified") return value;
  return model?.format ? `${model.format} artifact` : value;
}

function artifactLaneMissionDetail(artifactLane: JsonObject, model: ModelRecord | undefined): string {
  const detail = artifactLaneDetail(artifactLane);
  if (detail !== "artifact format has not been evaluated for this runtime lane") return detail;
  return model?.format
    ? `${model.format} artifact awaiting runtime-lane evaluation`
    : detail;
}

function artifactLaneMissionTone(artifactLane: JsonObject): GateTone {
  const tone = artifactLaneTone(artifactLane);
  return tone === "neutral" ? "warn" : tone;
}

function ddilRuntimeRepairMetric({
  missionReplay,
  pendingOperationLedger,
  pendingOperations,
  replayBlockedOperations
}: {
  missionReplay: MissionReplay | undefined;
  pendingOperationLedger: Record<string, unknown>[];
  pendingOperations: number;
  replayBlockedOperations: number;
}): EdgeMissionMetric {
  const repairCandidate = pendingOperationLedger.find((operation) =>
    stringOf(operation.runtime_remediation_runtime_target_id, "")
  );
  if (repairCandidate) {
    const previous = stringOf(repairCandidate.runtime_remediation_previous_runtime_target_id, "");
    const target = stringOf(repairCandidate.runtime_remediation_runtime_target_id, "");
    const delta = numberOf(repairCandidate.runtime_remediation_score_delta);
    return {
      label: "DDIL repair",
      value: "repair available",
      detail: `${previous ? `${previous} -> ` : ""}${target}${delta !== undefined ? ` (+${delta} fit)` : ""}`,
      tone: "warn"
    };
  }
  if (replayBlockedOperations) {
    return {
      label: "DDIL repair",
      value: "blocked replay",
      detail: `${replayBlockedOperations} queued runtime intent${replayBlockedOperations === 1 ? "" : "s"} blocked by preflight`,
      tone: "bad"
    };
  }

  const proof = latestRuntimeRetargetProof(missionReplay);
  if (proof) {
    return {
      label: "DDIL repair",
      value: "retarget proved",
      detail: proof,
      tone: "good"
    };
  }
  if (pendingOperations) {
    return {
      label: "DDIL repair",
      value: "queued",
      detail: `${pendingOperations} signed intent${pendingOperations === 1 ? "" : "s"} awaiting sync`,
      tone: "warn"
    };
  }
  return {
    label: "DDIL repair",
    value: "clear",
    detail: "no runtime repair pending",
    tone: "good"
  };
}

function latestRuntimeRetargetProof(missionReplay: MissionReplay | undefined): string {
  const events = Array.isArray(missionReplay?.events) ? missionReplay.events.map(asRecord) : [];
  const event = events.find((candidate) => {
    const summary = stringOf(candidate.summary, "");
    const detail = stringOf(candidate.detail, "");
    return (
      candidate.runtime_retargeted === true ||
      summary.includes("DDIL replay retargeted") ||
      detail.startsWith("retargeted ")
    );
  });
  if (event) {
    const detail = stringOf(event.detail, "");
    const summary = stringOf(event.summary, "retargeted DDIL replay");
    return detail ? `${summary}; ${detail}` : summary;
  }
  const phases = Array.isArray(missionReplay?.phases) ? missionReplay.phases : [];
  const phase = phases.find((candidate) => {
    const summary = candidate.summary ?? "";
    return candidate.phase === "offline_operation" && summary.includes("retargeted");
  });
  return phase?.summary ?? "";
}

function edgeMissionFocus({
  ddilRepair,
  edgeRuntimeFit,
  readinessVerdict,
  resourceEnvelopeFit,
  runtimeFit
}: {
  ddilRepair: EdgeMissionMetric;
  edgeRuntimeFit: EdgeRuntimeFit;
  readinessVerdict: ReadinessVerdict;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtimeFit: JsonObject;
}): string[] {
  const focus: string[] = [];
  if (readinessVerdict.tone !== "good") focus.push(readinessVerdict.nextAction);
  const targetSelection = asRecord(runtimeFit.target_selection);
  const targetSelectionDetail = runtimeTargetSelectionDetail(targetSelection);
  if (runtimeTargetSelectionTone(targetSelection) !== "neutral") focus.push(targetSelectionDetail);
  edgeRuntimeFit.failures.slice(0, 2).forEach((failure) => focus.push(failure));
  resourceEnvelopeFit.failures.slice(0, 2).forEach((failure) => focus.push(failure));
  if (ddilRepair.tone !== "good") focus.push(`${ddilRepair.value}: ${ddilRepair.detail}`);
  const unique = [...new Set(focus.filter(Boolean))].slice(0, 4);
  return unique.length ? unique : ["Selected on-device gates are aligned"];
}

function buildEdgeProofWorkflow({
  device,
  model,
  readiness,
  readinessVerdict,
  runtime,
  runtimeFitDisplay
}: {
  device: Device | undefined;
  model: ModelRecord | undefined;
  readiness: DeploymentReadiness | undefined;
  readinessVerdict: ReadinessVerdict;
  runtime: RuntimeTarget | undefined;
  runtimeFitDisplay: RuntimeFitDisplay;
}): EdgeProofWorkflow {
  const runtimeId = runtime ? runtimeTargetId(runtime) : "";
  const edgeId = device ? deviceId(device) : "";
  const missing = [
    model ? "" : "model",
    runtimeId ? "" : "runtime target",
    edgeId ? "" : "edge device"
  ].filter(Boolean);
  const runtimeFitScore = runtimeFitScoreForProof(readiness, runtimeFitDisplay);
  const runtimeFitLabel =
    runtimeFitScore !== undefined ? `runtime fit ${runtimeFitScore}/100` : runtimeFitDisplay.label;
  const gatePolicy = "go + best runtime + capability lock + fit >= 95 + proof <= 15m + path bound";
  const proofPath = `/tmp/${proofFileName(model?.id, runtimeId, edgeId)}`;
  const hubUrl = currentHubUrl();
  const capabilityLock = runtimeCapabilityLockForProof(readiness);

  let tone: GateTone = "warn";
  let status = "Proof context incomplete";
  if (!missing.length) {
    if (readinessVerdict.label === "go" && runtimeFitScore !== undefined && runtimeFitScore >= 95) {
      tone = "good";
      status = "Edge proof ready";
    } else if (readinessVerdict.tone === "bad" || (runtimeFitScore !== undefined && runtimeFitScore < 95)) {
      tone = "bad";
      status = "Edge proof will fail";
    } else {
      tone = "warn";
      status = "Edge proof needs evidence";
    }
  }

  const detail = missing.length
    ? `Missing ${missing.join(", ")} for proof export.`
    : `${model?.id ?? "model"} -> ${runtimeId} -> ${edgeId}; ${readinessVerdict.nextAction}; offline verifier fails stale proofs or proofs for a different path.`;

  const generateCommand = formatProofCommand([
    "uv",
    "run",
    "temms",
    "hub",
    "edge-runtime-mission",
    "--hub-url",
    hubUrl,
    "--package-id",
    model?.packageId ?? "<package-id>",
    "--model-id",
    model?.id ?? "<model-id>",
    "--device-id",
    edgeId || "<device-id>",
    "--runtime-target-id",
    runtimeId || "<runtime-target-id>",
    "--slot",
    "vision",
    "--require-go",
    "--require-best-runtime",
    "--require-capability-lock",
    "--min-runtime-fit",
    "95",
    "--output",
    proofPath
  ]);
  const verifyCommand = formatProofCommand([
    "uv",
    "run",
    "temms",
    "hub",
    "verify-edge-proof",
    proofPath,
    "--require-go",
    "--require-best-runtime",
    "--require-capability-lock",
    "--min-runtime-fit",
    "95",
    "--max-proof-age-seconds",
    String(EDGE_PROOF_MAX_AGE_SECONDS),
    "--package-id",
    model?.packageId ?? "<package-id>",
    "--model-id",
    model?.id ?? "<model-id>",
    "--device-id",
    edgeId || "<device-id>",
    "--runtime-target-id",
    runtimeId || "<runtime-target-id>",
    "--slot",
    "vision",
    "--require-proof-signature"
  ]);
  const verifyJsonCommand = formatProofCommand([
    "uv",
    "run",
    "temms",
    "hub",
    "verify-edge-proof",
    proofPath,
    "--require-go",
    "--require-best-runtime",
    "--require-capability-lock",
    "--min-runtime-fit",
    "95",
    "--max-proof-age-seconds",
    String(EDGE_PROOF_MAX_AGE_SECONDS),
    "--package-id",
    model?.packageId ?? "<package-id>",
    "--model-id",
    model?.id ?? "<model-id>",
    "--device-id",
    edgeId || "<device-id>",
    "--runtime-target-id",
    runtimeId || "<runtime-target-id>",
    "--slot",
    "vision",
    "--require-proof-signature",
    "--json"
  ]);

  return {
    status,
    detail,
    tone,
    proofPath,
    gatePolicy,
    attestation: "signed attestation required",
    capabilityLock: `Capability lock: ${capabilityLockValue(capabilityLock)}`,
    capabilityLockDetail: capabilityLockDetail(capabilityLock),
    capabilityLockTone: capabilityLockTone(capabilityLock),
    runtimeFit: runtimeFitLabel,
    generateCommand,
    verifyCommand,
    verifyJsonCommand,
    missing
  };
}

function edgeProofTraceStatus(
  proof: JsonObject | undefined,
  context: ReadinessQuery
): EdgeProofTraceStatus {
  if (!proof) {
    return {
      commandCount: 0,
      detail: "Generate or download a proof to inspect its signed runtime decision trace.",
      errors: [],
      rowCount: 0,
      schema: "",
      status: "not_generated",
      tone: "neutral",
      value: "not generated"
    };
  }
  if (proof.schema_version !== "temms-edge-runtime-proof/v1") {
    return {
      commandCount: 0,
      detail: "The latest payload is not a TEMMS edge runtime proof.",
      errors: ["payload schema is not temms-edge-runtime-proof/v1"],
      rowCount: 0,
      schema: stringOf(proof.schema_version, ""),
      status: "missing",
      tone: "warn",
      value: "not a proof"
    };
  }
  const selection = asRecord(proof.selection);
  const trace = asRecord(proof.runtime_decision_trace);
  if (!selectionMatchesContext(selection, context)) {
    return {
      commandCount: edgeProofTraceCommands(trace).length,
      detail: edgeProofTracePathDetail(selection),
      errors: ["latest proof does not match the selected model/runtime/edge path"],
      rowCount: edgeProofTraceRows(trace).length,
      schema: stringOf(trace.schema_version, ""),
      status: "stale",
      tone: "warn",
      value: "different path"
    };
  }

  const workbench = asRecord(proof.runtime_workbench);
  const rows = edgeProofTraceRows(trace);
  const commands = edgeProofTraceCommands(trace);
  const schema = stringOf(trace.schema_version, "");
  if (schema !== "temms-runtime-decision-trace/v1") {
    return {
      commandCount: commands.length,
      detail: "Proof does not retain a runtime decision trace.",
      errors: [`trace schema is ${schema || "missing"}`],
      rowCount: rows.length,
      schema,
      status: "missing",
      tone: "warn",
      value: "trace missing"
    };
  }
  if (workbench.schema_version !== "temms-runtime-workbench/v1") {
    return {
      commandCount: commands.length,
      detail: "Proof does not retain the canonical runtime workbench needed for browser consistency checks.",
      errors: ["runtime_workbench schema is missing"],
      rowCount: rows.length,
      schema,
      status: "missing",
      tone: "warn",
      value: "workbench missing"
    };
  }

  const errors = edgeProofTraceConsistencyErrors(trace, workbench);
  return {
    commandCount: commands.length,
    detail: errors.length
      ? "Signed trace disagrees with the canonical runtime workbench."
      : "Signed trace agrees with the canonical runtime workbench.",
    errors,
    rowCount: rows.length,
    schema,
    status: errors.length ? "mismatch" : "consistent",
    tone: errors.length ? "bad" : "good",
    value: errors.length ? "trace mismatch" : "trace consistent"
  };
}

function edgeProofComponentDigestStatus(
  proof: JsonObject | undefined,
  context: ReadinessQuery
): EdgeProofComponentDigestStatus {
  if (!proof) {
    return {
      detail: "Generate or download a proof to inspect component-level hashes.",
      digestCount: 0,
      digests: [],
      errors: [],
      schema: "",
      status: "not_generated",
      tone: "neutral",
      value: "not generated"
    };
  }
  if (proof.schema_version !== "temms-edge-runtime-proof/v1") {
    return {
      detail: "The latest payload is not a TEMMS edge runtime proof.",
      digestCount: 0,
      digests: [],
      errors: ["payload schema is not temms-edge-runtime-proof/v1"],
      schema: "",
      status: "missing",
      tone: "warn",
      value: "not a proof"
    };
  }
  const selection = asRecord(proof.selection);
  const componentDigests = asRecord(proof.component_digests);
  const digests = EDGE_PROOF_COMPONENT_DIGEST_TARGETS
    .map(({ key, label }) => ({ key, label, value: stringOf(componentDigests[key], "") }))
    .filter((digest) => digest.value);
  if (!selectionMatchesContext(selection, context)) {
    return {
      detail: edgeProofTracePathDetail(selection),
      digestCount: digests.length,
      digests,
      errors: ["latest proof does not match the selected model/runtime/edge path"],
      schema: stringOf(componentDigests.schema_version, ""),
      status: "stale",
      tone: "warn",
      value: "different path"
    };
  }

  const schema = stringOf(componentDigests.schema_version, "");
  const errors: string[] = [];
  if (schema !== "temms-edge-runtime-proof-component-digests/v1") {
    errors.push(`component digest schema is ${schema || "missing"}`);
  }
  EDGE_PROOF_COMPONENT_DIGEST_TARGETS.forEach(({ key, label, component }) => {
    const digest = stringOf(componentDigests[key], "");
    const componentPresent = Object.keys(asRecord(proof[component])).length > 0;
    if (componentPresent && !digest) errors.push(`${label} digest is missing`);
    if (digest && !isSha256Digest(digest)) errors.push(`${label} digest is not a sha256 hex value`);
    if (digest && !componentPresent) errors.push(`${label} digest is recorded but component is missing`);
  });

  const digestCount = digests.length;
  return {
    detail: errors.length
      ? errors.slice(0, 2).join(" / ")
      : "Runtime workbench, trace, and execution manifest hashes are retained; browser verification starts automatically.",
    digestCount,
    digests,
    errors,
    schema,
    status: errors.length ? "missing" : "retained",
    tone: errors.length ? "warn" : "good",
    value: errors.length ? "digest evidence incomplete" : "digests retained"
  };
}

async function verifyEdgeProofComponentDigestStatus(
  proof: JsonObject,
  baseStatus: EdgeProofComponentDigestStatus
): Promise<EdgeProofComponentDigestStatus> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    return {
      ...baseStatus,
      detail: "Browser crypto is unavailable; use verify-edge-proof for component digest recomputation.",
      status: "retained",
      tone: "warn",
      value: "digests retained"
    };
  }
  const recorded = new Map(baseStatus.digests.map((digest) => [digest.key, digest.value]));
  const errors: string[] = [];
  await Promise.all(
    EDGE_PROOF_COMPONENT_DIGEST_TARGETS.map(async ({ key, label, component }) => {
      const value = asRecord(proof[component]);
      if (!Object.keys(value).length) return;
      const expected = recorded.get(key);
      if (!expected) {
        errors.push(`${label} digest is missing`);
        return;
      }
      const computed = await sha256Hex(canonicalJsonStringify(value));
      if (expected.replace(/^sha256:/, "").toLowerCase() !== computed) {
        errors.push(`${label} digest mismatch`);
      }
    })
  );
  return {
    ...baseStatus,
    detail: errors.length
      ? errors.slice(0, 2).join(" / ")
      : "Browser recomputed workbench, trace, and manifest hashes against the proof payload.",
    errors,
    status: errors.length ? "mismatch" : "consistent",
    tone: errors.length ? "bad" : "good",
    value: errors.length ? "digest mismatch" : "digests verified"
  };
}

function edgeProofTraceConsistencyErrors(trace: JsonObject, workbench: JsonObject): string[] {
  const errors: string[] = [];
  const summary = asRecord(workbench.summary);
  const targetSelection = asRecord(workbench.target_selection);
  const topLevel: Array<[string, unknown]> = [
    ["selected_runtime_target_id", workbench.selected_runtime_target_id],
    ["best_runtime_target_id", workbench.best_runtime_target_id],
    ["selected_is_best", summary.selected_is_best],
    ["target_count", summary.target_count],
    ["eligible_target_count", summary.eligible_target_count],
    ["blocked_target_count", summary.blocked_target_count],
    ["target_selection_status", targetSelection.status],
    ["selected_rank", targetSelection.selected_rank],
    ["selected_score", targetSelection.selected_score],
    ["best_score", targetSelection.best_score],
    ["score_delta", targetSelection.score_delta]
  ];
  topLevel.forEach(([field, expected]) => {
    const actual = trace[field];
    if (!edgeProofTraceValuesEqual(actual, expected)) {
      errors.push(`${field} ${edgeProofValueLabel(actual)} != ${edgeProofValueLabel(expected)}`);
    }
  });

  const traceRows = new Map(edgeProofTraceRows(trace).map((row) => [stringOf(row.runtime_target_id, ""), row]));
  const workbenchRows = Array.isArray(workbench.targets)
    ? workbench.targets.map(asRecord).filter((row) => stringOf(row.runtime_target_id, ""))
    : [];
  workbenchRows.forEach((expectedRow) => {
    const targetId = stringOf(expectedRow.runtime_target_id, "");
    const traceRow = traceRows.get(targetId);
    if (!traceRow) {
      errors.push(`missing trace row ${targetId}`);
      return;
    }
    edgeProofTraceRowFields(expectedRow).forEach(([field, expected]) => {
      if (!edgeProofTraceValuesEqual(traceRow[field], expected)) {
        errors.push(`${targetId}.${field} ${edgeProofValueLabel(traceRow[field])} != ${edgeProofValueLabel(expected)}`);
      }
    });
    const expectedProof = asRecord(expectedRow.proof);
    const traceLock = asRecord(traceRow.capability_lock);
    const lockChecks: Array<[string, unknown]> = [
      ["status", expectedProof.capability_lock_status],
      ["capability_sha256", expectedProof.capability_sha256],
      ["telemetry_state", expectedProof.telemetry_state],
      ["telemetry_status", expectedProof.telemetry_status]
    ];
    lockChecks.forEach(([field, expected]) => {
      if (!edgeProofTraceValuesEqual(traceLock[field], expected)) errors.push(`${targetId}.capability_lock.${field} mismatch`);
    });
    const traceComponents = asRecord(traceRow.proof_components);
    edgeProofTraceComponentChecks(expectedProof).forEach(([component, field, expected]) => {
      const actual = asRecord(traceComponents[component])[field];
      if (!edgeProofTraceValuesEqual(actual, expected)) errors.push(`${targetId}.${component}.${field} mismatch`);
    });
  });
  traceRows.forEach((_row, targetId) => {
    if (!workbenchRows.some((row) => stringOf(row.runtime_target_id, "") === targetId)) {
      errors.push(`unexpected trace row ${targetId}`);
    }
  });

  const traceCommands = new Map(edgeProofTraceCommands(trace).map((command) => [stringOf(command.runtime_target_id, ""), command]));
  workbenchRows.forEach((row) => {
    const targetId = stringOf(row.runtime_target_id, "");
    const expected = edgeProofWorkbenchCommand(row);
    const actual = traceCommands.get(targetId);
    if (expected && !actual) errors.push(`missing trace command ${targetId}`);
    if (!expected && actual) errors.push(`unexpected trace command ${targetId}`);
    if (expected && actual) {
      ["action", "label", "kind", "requires_edge_execution", "command_text"].forEach((field) => {
        if (!edgeProofTraceValuesEqual(actual[field], expected[field])) errors.push(`${targetId}.command.${field} mismatch`);
      });
    }
  });

  return errors;
}

function edgeProofTraceRows(trace: JsonObject): JsonObject[] {
  return Array.isArray(trace.rows) ? trace.rows.map(asRecord) : [];
}

function edgeProofTraceCommands(trace: JsonObject): JsonObject[] {
  return Array.isArray(trace.commands) ? trace.commands.map(asRecord) : [];
}

function edgeProofTraceRowFields(row: JsonObject): Array<[string, unknown]> {
  const proof = asRecord(row.proof);
  return [
    ["rank", row.rank],
    ["status", row.status],
    ["eligible", row.eligible],
    ["selected", row.selected === true],
    ["best", row.best === true],
    ["score", row.score],
    ["tier", row.tier],
    ["detail", row.detail],
    ["validation_id", proof.validation_id],
    ["benchmark_id", proof.benchmark_id],
    ["latency_ms_p95", proof.latency_ms_p95],
    ["throughput_ips", proof.throughput_ips]
  ];
}

function edgeProofTraceComponentChecks(proof: JsonObject): Array<[string, string, unknown]> {
  return [
    ["runtime_validation", "status", proof.runtime_validation_status],
    ["runtime_validation", "state", proof.runtime_validation_state],
    ["runtime_validation", "evidence_id", proof.validation_id],
    ["benchmark", "status", proof.performance_status],
    ["benchmark", "state", proof.performance_state],
    ["benchmark", "evidence_id", proof.benchmark_id],
    ["benchmark", "latency_ms_p95", proof.latency_ms_p95],
    ["benchmark", "throughput_ips", proof.throughput_ips],
    ["resource", "status", proof.resource_status],
    ["resource", "state", proof.resource_state],
    ["telemetry", "status", proof.telemetry_status],
    ["telemetry", "state", proof.telemetry_state],
    ["capability_lock", "status", proof.capability_lock_status],
    ["capability_lock", "capability_sha256", proof.capability_sha256]
  ];
}

function edgeProofWorkbenchCommand(row: JsonObject): JsonObject | undefined {
  const remediation = asRecord(row.remediation);
  if (!Object.keys(remediation).length) return undefined;
  const commandRecord = asRecord(remediation.command);
  const edgeCommandText = stringOf(remediation.edge_command_text, stringOf(commandRecord.edge_command_text, ""));
  const operatorCommandText = stringOf(remediation.operator_command_text, stringOf(commandRecord.operator_command_text, ""));
  const edgeCommand = edgeProofCommandText(remediation.edge_command || commandRecord.edge_command);
  const operatorCommand = edgeProofCommandText(remediation.operator_command || commandRecord.operator_command);
  const commandText = edgeCommandText || operatorCommandText || edgeCommand || operatorCommand;
  if (!commandText) return undefined;
  const kind = edgeCommandText || edgeCommand ? "edge" : "operator";
  return {
    runtime_target_id: stringOf(row.runtime_target_id, ""),
    action: stringOf(remediation.action, ""),
    label: stringOf(remediation.label, stringOf(remediation.action, "Review")),
    kind,
    requires_edge_execution: remediation.requires_edge_execution === true,
    command_text: commandText
  };
}

function edgeProofCommandText(value: unknown): string {
  return Array.isArray(value) ? value.map((part) => String(part)).filter(Boolean).join(" ") : "";
}

function edgeProofTraceValuesEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if ((left === undefined || left === null || left === "") && (right === undefined || right === null || right === "")) return true;
  const leftNumber = numberOf(left);
  const rightNumber = numberOf(right);
  if (leftNumber !== undefined && rightNumber !== undefined) return leftNumber === rightNumber;
  return false;
}

function edgeProofValueLabel(value: unknown): string {
  if (value === undefined || value === null || value === "") return "missing";
  return JSON.stringify(value);
}

function edgeProofTracePathDetail(selection: JsonObject): string {
  const model = stringOf(selection.model_id, "model");
  const runtime = stringOf(selection.runtime_target_id, "runtime");
  const device = stringOf(selection.device_id, "edge");
  return `Latest proof is for ${model} -> ${runtime} -> ${device}.`;
}

function runtimeCapabilityLockForProof(readiness: DeploymentReadiness | undefined): JsonObject {
  const contract = asRecord(readiness?.edge_execution_contract);
  const runtimeDecision = asRecord(readiness?.runtime_decision);
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const contractLock = asRecord(contract.runtime_capability_lock);
  if (Object.keys(contractLock).length) return contractLock;
  const decisionLock = asRecord(runtimeDecision.runtime_capability_lock);
  if (Object.keys(decisionLock).length) return decisionLock;
  return asRecord(runtimeFit.runtime_capability_lock);
}

function runtimeFitScoreForProof(
  readiness: DeploymentReadiness | undefined,
  runtimeFitDisplay: RuntimeFitDisplay
): number | undefined {
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const score = numberOf(runtimeFit.score);
  if (score !== undefined) return score;
  const match = runtimeFitDisplay.label.match(/^(\d+(?:\.\d+)?)\/100/);
  return match ? Number(match[1]) : undefined;
}

function proofFileName(modelId: string | undefined, runtimeId: string, deviceIdValue: string): string {
  const slug = [modelId, runtimeId, deviceIdValue]
    .filter((part): part is string => Boolean(part))
    .map((part) => part.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""))
    .filter(Boolean)
    .join("-")
    .slice(0, 140);
  return `temms-edge-runtime-proof${slug ? `-${slug}` : ""}.json`;
}

function downloadJson(fileName: string, payload: unknown): void {
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function currentHubUrl(): string {
  if (typeof window === "undefined" || !window.location?.origin) return "http://127.0.0.1:8080";
  return window.location.origin;
}

function formatProofCommand(parts: string[]): string {
  if (parts.length <= 5) return parts.map(shellArg).join(" ");
  const firstLine = parts.slice(0, 5).map(shellArg).join(" ");
  const lines = [firstLine];
  for (let index = 5; index < parts.length;) {
    const token = parts[index];
    const flag = shellArg(token);
    const value = parts[index + 1];
    if (!token.startsWith("--")) {
      lines.push(`  ${flag}`);
      index += 1;
    } else if (value === undefined || value.startsWith("--")) {
      lines.push(`  ${flag}`);
      index += 1;
    } else {
      lines.push(`  ${flag} ${shellArg(value)}`);
      index += 2;
    }
  }
  return lines.join(" \\\n");
}

function shellArg(value: string): string {
  if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(value)) return value;
  return `"${value.replace(/(["\\$`])/g, "\\$1")}"`;
}

function prioritizedEvidenceEvents(
  timeline: unknown,
  activeModelId: string
): Record<string, unknown>[] {
  if (!Array.isArray(timeline)) return [];
  const events = timeline.map(asRecord);
  let activeRuntimeFitIndex = events.findIndex((event) => event.active_runtime_proof === true);
  if (activeRuntimeFitIndex < 0 && activeModelId) {
    activeRuntimeFitIndex = events.findIndex((event) => {
      const kind = stringOf(event.kind, "");
      const summary = stringOf(event.summary, "");
      return kind === "runtime_fit" && summary.includes(activeModelId);
    });
  }

  if (activeRuntimeFitIndex < 0) return events.slice(0, 4);

  const activeRuntimeFit = {
    ...events[activeRuntimeFitIndex],
    kind: "active_runtime_fit"
  };
  const remaining = events.filter((_, index) => index !== activeRuntimeFitIndex);
  return [activeRuntimeFit, ...remaining].slice(0, 4);
}

function EvidenceEventRow({ event }: { event: Record<string, unknown> }): JSX.Element {
  const kind = stringOf(event.kind, "event");
  const activeProof = event.active_runtime_proof === true || kind === "active_runtime_fit";
  return (
    <div className={activeProof ? "evidence-event-row evidence-event-row-active" : "evidence-event-row"}>
      <span>{activeProof ? "active runtime proof" : kind}</span>
      <strong>{compactMetricDetail(stringOf(event.summary, "mission event"))}</strong>
      <small>{compactDate(stringOf(event.timestamp, ""))}</small>
    </div>
  );
}

function RuntimeRepairProofPanel({
  compact = false,
  proof,
  onRetargetRuntime
}: {
  compact?: boolean;
  proof: RuntimeRepairProof;
  onRetargetRuntime?: (operation: Record<string, unknown>) => void;
}): JSX.Element {
  const canRetarget = proof.status === "repair_available" && proof.operation && onRetargetRuntime;
  const capabilityDigest = proof.capabilitySha256 ? proof.capabilitySha256.slice(0, 12) : "";
  const sourceDetail = [
    proof.actor ? `actor ${proof.actor}` : "",
    proof.occurredAt ? compactDate(proof.occurredAt) : "",
    proof.reason
  ].filter(Boolean).join(" / ");
  const coverageDetail = runtimeRepairCoverageDetail(proof);
  const proofLabel = proof.proofStatus || (proof.status === "proved" ? "proved" : "repair available");

  return (
    <div
      className={`runtime-repair-proof runtime-repair-proof-${proof.tone}${compact ? " runtime-repair-proof-compact" : ""}`}
      data-testid="runtime-repair-proof"
    >
      <div className="runtime-repair-proof-header">
        <div>
          <span>{compact ? "DDIL repair evidence" : "DDIL runtime repair proof"}</span>
          <strong>{proof.headline}</strong>
          <small>{proof.detail}</small>
        </div>
        <Badge value={proofLabel.replace(/_/g, " ")} />
      </div>

      <div className="runtime-repair-proof-path" aria-label="Runtime repair path">
        <div>
          <span>Queued runtime</span>
          <strong>{proof.previousRuntime || "unknown"}</strong>
        </div>
        <span className="runtime-repair-proof-arrow">-&gt;</span>
        <div>
          <span>Proved runtime</span>
          <strong>{proof.selectedRuntime || proof.bestRuntime || "pending"}</strong>
        </div>
        <div>
          <span>Best measured</span>
          <strong>{proof.bestRuntime || proof.selectedRuntime || "pending"}</strong>
        </div>
      </div>

      <div className="runtime-repair-proof-grid">
        <RuntimeRepairMetric
          detail={proof.targetSelectionStatus || (proof.selectedIsBest ? "selected target is best" : "target selection retained")}
          label="Runtime fit"
          tone={proof.selectedIsBest === false ? "warn" : proof.tone}
          value={proof.runtimeFitScore !== undefined ? `${proof.runtimeFitScore}/100` : proof.selectedIsBest ? "best" : "pending"}
        />
        <RuntimeRepairMetric
          detail={capabilityDigest ? `sha256 ${capabilityDigest}` : "capability digest not retained"}
          label="Capability lock"
          tone={proof.capabilityLockStatus === "locked" ? "good" : proof.status === "proved" ? "warn" : "neutral"}
          value={proof.capabilityLockStatus || "not retained"}
        />
        <RuntimeRepairMetric
          detail={proof.validationId || "runtime validation id not retained"}
          label="Validation"
          tone={proof.validationId ? "good" : proof.status === "proved" ? "warn" : "neutral"}
          value={proof.validationId ? "present" : "missing"}
        />
        <RuntimeRepairMetric
          detail={proof.benchmarkId || "benchmark id not retained"}
          label="Benchmark"
          tone={proof.benchmarkId ? "good" : proof.status === "proved" ? "warn" : "neutral"}
          value={proof.benchmarkId ? "present" : "missing"}
        />
        <RuntimeRepairMetric
          detail={coverageDetail}
          label="Coverage"
          tone={proof.blockedTargetCount ? "warn" : "neutral"}
          value={runtimeRepairCoverageValue(proof)}
        />
        <RuntimeRepairMetric
          detail={proof.workbenchSchema || sourceDetail || proof.source}
          label="Audit source"
          tone={proof.workbenchSchema ? "good" : "neutral"}
          value={proof.source}
        />
      </div>

      {sourceDetail ? <small className="runtime-repair-proof-source">{sourceDetail}</small> : null}
      {canRetarget ? (
        <button className="button-mini" type="button" onClick={() => onRetargetRuntime(proof.operation!)}>
          <GitBranch size={14} aria-hidden="true" />
          Use proved runtime
        </button>
      ) : null}
    </div>
  );
}

function RuntimeRepairMetric({
  detail,
  label,
  tone,
  value
}: {
  detail: string;
  label: string;
  tone: GateTone;
  value: string;
}): JSX.Element {
  return (
    <div className={`runtime-repair-metric runtime-repair-metric-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function latestRuntimeRepairProofFor({
  evidenceSummary,
  missionReplay,
  pendingOperationLedger
}: {
  evidenceSummary: EvidenceSummary | undefined;
  missionReplay: MissionReplay | undefined;
  pendingOperationLedger: Record<string, unknown>[];
}): RuntimeRepairProof | undefined {
  const pendingProof = firstRuntimeRepairProof(
    pendingOperationLedger,
    "pending",
    (proof) => proof.status === "proved"
  );
  if (pendingProof) return pendingProof;

  const pendingCandidate = firstRuntimeRepairProof(
    pendingOperationLedger,
    "pending",
    (proof) => proof.status === "repair_available"
  );
  if (pendingCandidate) return pendingCandidate;

  const summary = asRecord(evidenceSummary);
  const decisions = Array.isArray(summary.decisions) ? summary.decisions.map(asRecord) : [];
  const replayedProof = firstRuntimeRepairProof(decisions, "replayed", (proof) => proof.status === "proved");
  if (replayedProof) return replayedProof;

  return runtimeRepairProofFromMissionReplay(missionReplay);
}

function firstRuntimeRepairProof(
  records: Record<string, unknown>[],
  source: RuntimeRepairProof["source"],
  predicate: (proof: RuntimeRepairProof) => boolean
): RuntimeRepairProof | undefined {
  for (const record of records) {
    const proof = runtimeRepairProofFromRecord(record, source);
    if (proof && predicate(proof)) return proof;
  }
  return undefined;
}

function runtimeRepairProofFromRecord(
  record: Record<string, unknown>,
  source: RuntimeRepairProof["source"]
): RuntimeRepairProof | undefined {
  const remediationTarget = stringOf(record.runtime_remediation_runtime_target_id, "");
  const retargetedFrom = stringOf(record.runtime_retargeted_from, "");
  const retargetedTo = stringOf(record.runtime_retargeted_to, "");
  const workbenchPrevious = stringOf(
    record.runtime_retarget_workbench_previous_selected_runtime_target_id,
    ""
  );
  const workbenchSelected = stringOf(
    record.runtime_retarget_workbench_selected_runtime_target_id,
    ""
  );
  const workbenchBest =
    stringOf(record.runtime_retarget_workbench_best_runtime_target_id, "") ||
    stringOf(record.runtime_workbench_best_runtime_target_id, "");
  const previousRuntime =
    workbenchPrevious ||
    retargetedFrom ||
    stringOf(record.runtime_remediation_previous_runtime_target_id, "") ||
    (remediationTarget ? stringOf(record.runtime_workbench_selected_runtime_target_id, "") : "") ||
    stringOf(record.runtime_target_id, "");
  const selectedRuntime =
    workbenchSelected ||
    retargetedTo ||
    remediationTarget ||
    workbenchBest;
  const bestRuntime =
    workbenchBest ||
    stringOf(record.best_runtime_target_id, "") ||
    remediationTarget ||
    selectedRuntime;
  const proofStatus =
    stringOf(record.runtime_retarget_proof_status, "") ||
    stringOf(record.runtime_retarget_replay_proof_status, "");
  const workbenchSchema =
    stringOf(record.runtime_retarget_workbench_schema_version, "") ||
    stringOf(record.runtime_workbench_schema_version, "");
  const hasRetargetProof =
    record.runtime_retargeted === true ||
    Boolean(proofStatus || workbenchSchema || stringOf(record.runtime_retarget_capability_sha256, ""));
  const hasRepairCandidate = Boolean(remediationTarget && remediationTarget !== previousRuntime);
  if ((!hasRetargetProof && !hasRepairCandidate) || (!previousRuntime && !selectedRuntime && !bestRuntime)) {
    return undefined;
  }

  const status: RuntimeRepairProof["status"] = hasRetargetProof ? "proved" : "repair_available";
  const selectedIsBest =
    booleanOf(record.runtime_retarget_workbench_selected_is_best) ??
    booleanOf(record.runtime_workbench_selected_is_best);
  const runtimeFitScore =
    numberOf(record.runtime_retarget_runtime_fit_score) ??
    numberOf(record.runtime_fit_score);
  const targetSelectionStatus =
    stringOf(record.runtime_retarget_workbench_target_selection_status, "") ||
    stringOf(record.runtime_workbench_target_selection_status, "");
  const capabilityLockStatus = stringOf(record.runtime_retarget_capability_lock_status, "");
  const capabilitySha256 = stringOf(record.runtime_retarget_capability_sha256, "");
  const validationId = stringOf(record.runtime_retarget_validation_id, "");
  const benchmarkId = stringOf(record.runtime_retarget_benchmark_id, "");
  const tone = runtimeRepairTone(status, proofStatus, selectedIsBest);
  const detail = runtimeRepairDetail({
    benchmarkId,
    bestRuntime,
    capabilityLockStatus,
    previousRuntime,
    runtimeFitScore,
    selectedRuntime,
    status,
    validationId
  });

  return {
    actor: stringOf(record.runtime_retargeted_by, "") || stringOf(record.actor, ""),
    benchmarkId,
    bestRuntime,
    blockedTargetCount:
      numberOf(record.runtime_retarget_workbench_blocked_target_count) ??
      numberOf(record.runtime_workbench_blocked_target_count),
    capabilityLockStatus,
    capabilitySha256,
    detail,
    eligibleTargetCount:
      numberOf(record.runtime_retarget_workbench_eligible_target_count) ??
      numberOf(record.runtime_workbench_eligible_target_count),
    headline: status === "proved" ? "Retarget proof retained" : "Best runtime repair available",
    occurredAt: stringOf(record.runtime_retargeted_at, "") || stringOf(record.recorded_at, ""),
    operation: status === "repair_available" ? record : undefined,
    previousRuntime,
    proofStatus,
    reason: stringOf(record.runtime_retarget_reason, "") || stringOf(record.replay_reason, ""),
    runtimeFitScore,
    selectedIsBest,
    selectedRuntime,
    source,
    status,
    targetCount:
      numberOf(record.runtime_retarget_workbench_target_count) ??
      numberOf(record.runtime_workbench_target_count),
    targetSelectionStatus,
    tone,
    validationId,
    workbenchSchema
  };
}

function runtimeRepairProofFromMissionReplay(missionReplay: MissionReplay | undefined): RuntimeRepairProof | undefined {
  const events = Array.isArray(missionReplay?.events) ? missionReplay.events.map(asRecord) : [];
  const event = events.find((candidate) => {
    const summary = stringOf(candidate.summary, "");
    const detail = stringOf(candidate.detail, "");
    return (
      candidate.runtime_retargeted === true ||
      summary.includes("DDIL replay retargeted") ||
      detail.startsWith("retargeted ")
    );
  });
  if (!event) return undefined;

  const detail = stringOf(event.detail, "");
  const match = detail.match(/^retargeted\s+(.+?)\s+->\s+(.+)$/);
  const previousRuntime = match?.[1] ?? "";
  const selectedRuntime = match?.[2] ?? "";
  return {
    actor: "",
    benchmarkId: "",
    bestRuntime: selectedRuntime,
    capabilityLockStatus: "",
    capabilitySha256: "",
    detail: detail || stringOf(event.summary, "retargeted DDIL replay"),
    headline: "Replay retained retarget proof",
    occurredAt: stringOf(event.timestamp, ""),
    previousRuntime,
    proofStatus: "proved",
    reason: stringOf(event.summary, ""),
    runtimeFitScore: undefined,
    selectedIsBest: undefined,
    selectedRuntime,
    source: "mission",
    status: "proved",
    targetSelectionStatus: "",
    tone: "good",
    validationId: "",
    workbenchSchema: ""
  };
}

function runtimeRepairTone(
  status: RuntimeRepairProof["status"],
  proofStatus: string,
  selectedIsBest?: boolean
): GateTone {
  const normalized = proofStatus.toLowerCase();
  if (normalized.includes("stale") || normalized.includes("blocked") || normalized.includes("failed")) return "bad";
  if (status === "repair_available") return "warn";
  if (selectedIsBest === false) return "warn";
  return "good";
}

function runtimeRepairDetail({
  benchmarkId,
  bestRuntime,
  capabilityLockStatus,
  previousRuntime,
  runtimeFitScore,
  selectedRuntime,
  status,
  validationId
}: {
  benchmarkId: string;
  bestRuntime: string;
  capabilityLockStatus: string;
  previousRuntime: string;
  runtimeFitScore?: number;
  selectedRuntime: string;
  status: RuntimeRepairProof["status"];
  validationId: string;
}): string {
  if (status === "repair_available") {
    return `${previousRuntime || "queued runtime"} can be retargeted to ${bestRuntime || selectedRuntime || "the measured best runtime"}.`;
  }
  const evidence = [];
  if (runtimeFitScore !== undefined) evidence.push(`fit ${runtimeFitScore}/100`);
  if (capabilityLockStatus) evidence.push(`capability ${capabilityLockStatus.replace(/_/g, " ")}`);
  if (validationId) evidence.push("validation");
  if (benchmarkId) evidence.push("benchmark");
  return `${previousRuntime || "queued runtime"} -> ${selectedRuntime || bestRuntime || "proved runtime"}${evidence.length ? ` with ${evidence.join(", ")}` : ""}.`;
}

function runtimeRepairCoverageValue(proof: RuntimeRepairProof): string {
  if (proof.targetCount !== undefined) return `${proof.targetCount} target${proof.targetCount === 1 ? "" : "s"}`;
  if (proof.eligibleTargetCount !== undefined || proof.blockedTargetCount !== undefined) return "ranked";
  return "not retained";
}

function runtimeRepairCoverageDetail(proof: RuntimeRepairProof): string {
  const parts = [];
  if (proof.eligibleTargetCount !== undefined) parts.push(`${proof.eligibleTargetCount} eligible`);
  if (proof.blockedTargetCount !== undefined) parts.push(`${proof.blockedTargetCount} blocked`);
  return parts.join(" / ") || "target coverage not retained";
}

function OperationRuntimeProof({
  operation,
  onCopyCommand
}: {
  operation: Record<string, unknown>;
  onCopyCommand?: (label: string, command: string) => void;
}): JSX.Element {
  const optimizerDetail = stringOf(operation.runtime_optimizer_detail, "");
  const bestRuntimeTarget = stringOf(operation.best_runtime_target_id, "");
  const scoreDelta = numberOf(operation.runtime_score_delta);
  const runtimeFitScore = numberOf(operation.runtime_fit_score);
  const runtimeFitTier = stringOf(operation.runtime_fit_tier, "");
  const runtimeLane = stringOf(operation.runtime_lane_label, "");
  const runtimeLaneAcceleration = stringOf(operation.runtime_lane_acceleration, "").replace(/_/g, " ");
  const artifactLaneState = stringOf(operation.artifact_lane_state, "");
  const artifactLaneDetail = stringOf(operation.artifact_lane_detail, "");
  const productionApplyAllowed = operation.production_apply_allowed;
  const capabilityLockStatus = stringOf(operation.runtime_capability_lock_status, "");
  const capabilityLockDigest = stringOf(operation.runtime_capability_sha256, "");
  const capabilityLockProfile = stringOf(operation.runtime_capability_edge_profile, "");
  const telemetryState = stringOf(operation.runtime_capability_telemetry_state, "").replace(/_/g, " ");
  const telemetryDetail = stringOf(operation.runtime_capability_telemetry_detail, "");
  const heartbeatAge = numberOf(operation.runtime_capability_heartbeat_age_seconds);
  const heartbeatBudget = numberOf(operation.runtime_capability_heartbeat_stale_after_seconds);
  const capabilityFailures = stringsOf(operation.runtime_capability_failures);
  const contractAction = stringOf(operation.edge_execution_contract_action, "");
  const runtimeRemediationLabel = stringOf(operation.runtime_remediation_label, "");
  const runtimeRemediationTarget = stringOf(operation.runtime_remediation_runtime_target_id, "");
  const runtimeRemediationPrevious = stringOf(operation.runtime_remediation_previous_runtime_target_id, "");
  const runtimeRemediationDelta = numberOf(operation.runtime_remediation_score_delta);
  const runtimeContractTarget = stringOf(operation.runtime_remediation_contract_runtime_target_id, "");
  const runtimeContractLabel = stringOf(operation.runtime_remediation_contract_label, "");
  const runtimeContractKind = stringOf(operation.runtime_remediation_contract_kind, "");
  const runtimeContractCommand = localizeHubCommandText(stringOf(operation.runtime_remediation_contract_command_text, ""));
  const runtimeContractNote = stringOf(operation.runtime_remediation_contract_command_note, "");
  const runtimeContractRequiresEdge = operation.runtime_remediation_contract_requires_edge_execution === true;
  const runtimeContractCommandLabel =
    `${runtimeContractTarget || runtimeRemediationTarget || "runtime"} ${runtimeContractRequiresEdge || runtimeContractKind === "edge" ? "edge command" : "operator command"}`;
  const runtimeRetargetedFrom = stringOf(operation.runtime_retargeted_from, "");
  const runtimeRetargetedTo = stringOf(operation.runtime_retargeted_to, "");
  const runtimeRetargetedBy = stringOf(operation.runtime_retargeted_by, "");
  const runtimeRetargetProofStatus = stringOf(operation.runtime_retarget_proof_status, "");
  const runtimeRetargetFitScore = numberOf(operation.runtime_retarget_runtime_fit_score);
  const runtimeRetargetLockStatus = stringOf(operation.runtime_retarget_capability_lock_status, "");
  const runtimeRetargetCapability = stringOf(operation.runtime_retarget_capability_sha256, "");
  const runtimeRetargetValidation = stringOf(operation.runtime_retarget_validation_id, "");
  const runtimeRetargetBenchmark = stringOf(operation.runtime_retarget_benchmark_id, "");
  const runtimeRetargetAssessment = stringOf(operation.runtime_retarget_target_assessment_sha256, "");
  const runtimeRetargetReplayProofStatus = stringOf(operation.runtime_retarget_replay_proof_status, "");
  const runtimeRetargetReplaySignedCapability = stringOf(operation.runtime_retarget_replay_signed_capability_sha256, "");
  const runtimeRetargetReplayCurrentCapability = stringOf(operation.runtime_retarget_replay_current_capability_sha256, "");
  const runtimeRetargetReplaySignedAssessment = stringOf(
    operation.runtime_retarget_replay_signed_target_assessment_sha256,
    ""
  );
  const runtimeRetargetReplayCurrentAssessment = stringOf(
    operation.runtime_retarget_replay_current_target_assessment_sha256,
    ""
  );
  return (
    <>
      {runtimeFitScore !== undefined ? (
        <small>
          runtime fit {runtimeFitScore}/100{runtimeFitTier ? ` ${runtimeFitTier}` : ""}
        </small>
      ) : null}
      {runtimeLane ? (
        <small>
          lane {runtimeLane}{runtimeLaneAcceleration ? ` / ${runtimeLaneAcceleration}` : ""}
        </small>
      ) : null}
      {artifactLaneState ? (
        <small>
          artifact {artifactLaneState.replace(/_/g, " ")}
          {artifactLaneDetail ? `: ${artifactLaneDetail}` : ""}
        </small>
      ) : null}
      {typeof productionApplyAllowed === "boolean" ? (
        <small>production apply {productionApplyAllowed ? "permitted" : "blocked"}</small>
      ) : null}
      {capabilityLockStatus || capabilityLockDigest ? (
        <small>
          capability lock {capabilityLockStatus ? capabilityLockStatus.replace(/_/g, " ") : "hash locked"}
          {capabilityLockDigest ? ` ${capabilityLockDigest.slice(0, 12)}` : ""}
          {capabilityLockProfile ? ` / ${capabilityLockProfile}` : ""}
        </small>
      ) : null}
      {heartbeatAge !== undefined && heartbeatBudget !== undefined ? (
        <small>
          telemetry {telemetryState || "heartbeat"}: {formatAge(heartbeatAge)} old / {formatAge(heartbeatBudget)} budget
        </small>
      ) : telemetryDetail ? (
        <small>telemetry {compactMetricDetail(telemetryDetail)}</small>
      ) : null}
      {capabilityFailures.length ? <small>{compactMetricDetail(capabilityFailures[0])}</small> : null}
      {contractAction ? <small>edge contract {contractAction.replace(/_/g, " ")}</small> : null}
      {optimizerDetail ? <small>{optimizerDetail}</small> : null}
      {bestRuntimeTarget ? (
        <small>
          best runtime {bestRuntimeTarget}
          {scoreDelta !== undefined ? ` (+${scoreDelta} fit)` : ""}
        </small>
      ) : null}
      {runtimeRemediationLabel && runtimeRemediationTarget ? (
        <small>
          runtime fix {runtimeRemediationLabel}:{" "}
          {runtimeRemediationPrevious ? `${runtimeRemediationPrevious} -> ` : ""}
          {runtimeRemediationTarget}
          {runtimeRemediationDelta !== undefined ? ` (+${runtimeRemediationDelta} fit)` : ""}
        </small>
      ) : null}
      {runtimeContractCommand ? (
        <div className="pending-runtime-command">
          <span>
            {runtimeContractRequiresEdge || runtimeContractKind === "edge" ? "edge-run" : "operator"}{" "}
            {runtimeContractLabel || runtimeRemediationLabel || "runtime command"}
          </span>
          <code>{runtimeContractCommand}</code>
          {onCopyCommand ? (
            <button
              className="button-mini"
              type="button"
              onClick={() => onCopyCommand(runtimeContractCommandLabel, runtimeContractCommand)}
            >
              <Clipboard size={14} aria-hidden="true" />
              Copy
            </button>
          ) : null}
          {runtimeContractNote ? <small>{compactMetricDetail(runtimeContractNote)}</small> : null}
        </div>
      ) : null}
      {runtimeRetargetedFrom && runtimeRetargetedTo ? (
        <small>
          retargeted {runtimeRetargetedFrom}
          {" -> "}
          {runtimeRetargetedTo}
          {runtimeRetargetedBy ? ` by ${runtimeRetargetedBy}` : ""}
        </small>
      ) : null}
      {runtimeRetargetProofStatus || runtimeRetargetCapability || runtimeRetargetFitScore !== undefined ? (
        <small>
          retarget proof {runtimeRetargetProofStatus || "proved"}
          {runtimeRetargetFitScore !== undefined ? ` / fit ${runtimeRetargetFitScore}/100` : ""}
          {runtimeRetargetLockStatus ? ` / lock ${runtimeRetargetLockStatus.replace(/_/g, " ")}` : ""}
          {runtimeRetargetCapability ? ` ${runtimeRetargetCapability.slice(0, 12)}` : ""}
          {runtimeRetargetValidation ? ` / validation ${runtimeRetargetValidation}` : ""}
          {runtimeRetargetBenchmark ? ` / benchmark ${runtimeRetargetBenchmark}` : ""}
          {runtimeRetargetAssessment ? ` / assessment ${runtimeRetargetAssessment.slice(0, 12)}` : ""}
        </small>
      ) : null}
      {runtimeRetargetReplayProofStatus ? (
        <small>
          retarget replay proof {runtimeRetargetReplayProofStatus.replace(/_/g, " ")}
          {runtimeRetargetReplaySignedCapability ? ` / signed ${runtimeRetargetReplaySignedCapability.slice(0, 12)}` : ""}
          {runtimeRetargetReplayCurrentCapability ? ` / current ${runtimeRetargetReplayCurrentCapability.slice(0, 12)}` : ""}
          {runtimeRetargetReplaySignedAssessment ? ` / signed assessment ${runtimeRetargetReplaySignedAssessment.slice(0, 12)}` : ""}
          {runtimeRetargetReplayCurrentAssessment ? ` / current assessment ${runtimeRetargetReplayCurrentAssessment.slice(0, 12)}` : ""}
        </small>
      ) : null}
    </>
  );
}

function PendingOperationRow({
  operation,
  onCopyCommand,
  onRetargetRuntime
}: {
  operation: Record<string, unknown>;
  onCopyCommand?: (label: string, command: string) => void;
  onRetargetRuntime?: (operation: Record<string, unknown>) => void;
}): JSX.Element {
  const digest = stringOf(operation.payload_sha256, "");
  const operationType = stringOf(operation.operation, "operation");
  const signatureLabel = pendingSignatureLabel(operation);
  const signatureReason = stringOf(operation.signature_verification_reason, "");
  const replayLabel = pendingReplayLabel(operation);
  const replayReason = stringOf(operation.replay_reason, "");
  const supersededByModel = stringOf(operation.superseded_by_model_id, "");
  const retargetCandidate = stringOf(operation.runtime_remediation_runtime_target_id, "");
  const target = [
    stringOf(operation.device_id, ""),
    stringOf(operation.slot, ""),
    stringOf(operation.runtime_target_id, "")
  ].filter(Boolean);
  return (
    <div className="pending-operation-row">
      <span>{operationType} intent</span>
      <strong>{stringOf(operation.summary, "queued operation")}</strong>
      <small>
        {stringOf(operation.actor, "operator")} - {compactDate(stringOf(operation.recorded_at, ""))}
      </small>
      <code>{digest ? `sha256:${digest.slice(0, 12)}` : "sha256:pending"}</code>
      <small>{signatureLabel}</small>
      {signatureReason && signatureLabel !== "verified intent" ? <small>{signatureReason}</small> : null}
      <small>{replayLabel}</small>
      {replayReason && replayLabel !== "ready to replay" ? <small>{replayReason}</small> : null}
      <OperationRuntimeProof operation={operation} onCopyCommand={onCopyCommand} />
      {retargetCandidate && onRetargetRuntime ? (
        <button className="button-mini" type="button" onClick={() => onRetargetRuntime(operation)}>
          <GitBranch size={14} aria-hidden="true" />
          Use best runtime
        </button>
      ) : null}
      {supersededByModel ? <small>final intent selects {supersededByModel}</small> : null}
      {target.length ? <small>{target.join(" / ")}</small> : null}
    </div>
  );
}

function DeadLetteredOperationRow({
  operation,
  onCopyCommand,
  onRequeue
}: {
  operation: Record<string, unknown>;
  onCopyCommand?: (label: string, command: string) => void;
  onRequeue?: (operation: Record<string, unknown>) => void;
}): JSX.Element {
  const digest = stringOf(operation.payload_sha256, "");
  const operationType = stringOf(operation.operation, "operation");
  const signatureLabel = pendingSignatureLabel(operation);
  const replayLabel = pendingReplayLabel(operation);
  const replayReason = stringOf(operation.replay_reason, "");
  const quarantineReason = stringOf(operation.reason, "");
  const target = [
    stringOf(operation.device_id, ""),
    stringOf(operation.slot, ""),
    stringOf(operation.runtime_target_id, "")
  ].filter(Boolean);
  return (
    <div className="pending-operation-row pending-operation-row-dead-letter">
      <span>{operationType} quarantined</span>
      <strong>{stringOf(operation.summary, "quarantined operation")}</strong>
      <small>
        {stringOf(operation.actor, "operator")} - {compactDate(stringOf(operation.quarantined_at, ""))}
      </small>
      <code>{digest ? `sha256:${digest.slice(0, 12)}` : "sha256:quarantined"}</code>
      <small>{signatureLabel}</small>
      <small>{replayLabel}</small>
      {replayReason ? <small>{replayReason}</small> : null}
      {quarantineReason ? <small>{quarantineReason}</small> : null}
      <OperationRuntimeProof operation={operation} onCopyCommand={onCopyCommand} />
      {onRequeue ? (
        <button className="button-mini" type="button" onClick={() => onRequeue(operation)}>
          <FileCheck2 size={14} aria-hidden="true" />
          Requeue intent
        </button>
      ) : null}
      {target.length ? <small>{target.join(" / ")}</small> : null}
    </div>
  );
}

function readinessMatchesContext(
  readiness: DeploymentReadiness | undefined,
  context: {
    package_id?: string;
    model_id?: string;
    device_id?: string;
    runtime_target_id?: string;
    slot?: string;
  }
): boolean {
  if (!readiness?.gates?.length) return false;
  return selectionMatchesContext(asRecord(readiness.selection), context);
}

function selectionMatchesContext(
  selection: Record<string, unknown>,
  context: {
    package_id?: string;
    model_id?: string;
    device_id?: string;
    runtime_target_id?: string;
    slot?: string;
  }
): boolean {
  return (
    matchesSelection(selection, "package_id", context.package_id) &&
    matchesSelection(selection, "model_id", context.model_id) &&
    matchesSelection(selection, "device_id", context.device_id) &&
    matchesSelection(selection, "runtime_target_id", context.runtime_target_id) &&
    matchesSelection(selection, "slot", context.slot)
  );
}

function matchesSelection(
  selection: Record<string, unknown>,
  key: string,
  expected: string | undefined
): boolean {
  return !expected || stringOf(selection[key], "") === expected;
}

function syncingReadinessVerdict(): ReadinessVerdict {
  const gates: ReadinessGate[] = [
    "Model inventory",
    "Runtime target",
    "Edge telemetry",
    "Rollout state",
    "DDIL queue",
    "Evidence chain"
  ].map((label) => ({
    label,
    state: "syncing",
    detail: "Waiting for the latest Hub snapshot",
    tone: "neutral"
  }));
  return {
    label: "syncing",
    headline: "Synchronizing edge state",
    detail: "Fetching model, runtime, rollout, DDIL, and evidence state from Hub.",
    nextAction: "Waiting for Hub snapshot",
    tone: "neutral",
    gates
  };
}

function readinessVerdictFromApi(readiness: DeploymentReadiness): ReadinessVerdict {
  const status = stringOf(readiness.status, "attention");
  return {
    label: status,
    headline: stringOf(readiness.headline, "Deployment readiness needs review"),
    detail: compactMetricDetail(stringOf(readiness.detail, "Review the deployment readiness gates before rollout.")),
    nextAction: compactMetricDetail(stringOf(readiness.next_action, "Review the attention gate")),
    tone: toneForReadinessStatus(status),
    gates: (readiness.gates ?? []).map((gate) => {
      const gateStatus = stringOf(gate.status, "");
      const gateState = stringOf(gate.state, gateStatus || "unknown");
      return {
        label: stringOf(gate.label, stringOf(gate.gate_id, "Gate")),
        state: gateState,
        detail: compactMetricDetail(stringOf(gate.detail, "No additional detail")),
        tone: gateStatus ? toneForReadinessStatus(gateStatus) : toneForPath(gateState),
        actions: (gate.actions ?? [])
          .map((action) => ({
            id: stringOf(action.action_id, stringOf(action.kind, stringOf(action.label, ""))),
            label: stringOf(action.label, ""),
            kind: stringOf(action.kind, ""),
            gateId: stringOf(action.gate_id, stringOf(gate.gate_id, "")),
            refs: asRecord(action.refs),
            command: readinessCommandFromValue(action.command)
          }))
          .filter((action) => action.label)
      };
    })
  };
}

function pendingSignatureLabel(operation: Record<string, unknown>): string {
  const status = stringOf(operation.signature_status, "");
  if (status === "verified") return "verified intent";
  if (status === "invalid") return "tampered intent";
  if (status === "missing_signature") return "missing signature";
  if (status === "key_unavailable") return "key unavailable";
  if (status === "unsigned_allowed") return "unsigned allowed";
  return operation.signature_present === true ? "signed intent" : "unsigned intent";
}

function pendingReplayLabel(operation: Record<string, unknown>): string {
  const status = stringOf(operation.replay_status, "");
  if (status === "superseded" || operation.superseded === true) return "superseded intent";
  if (status === "ready_with_runtime_advisory") return "runtime advisory";
  if (operation.replay_ready === true || status === "ready") return "ready to replay";
  if (status === "blocked") return "replay blocked";
  return "replay pending";
}

async function loadSnapshotAfterReconciliation(token: string): Promise<HubSnapshot> {
  let next = await loadSnapshot(token);
  for (let attempt = 0; attempt < 5 && isAwaitingReconciliation(next); attempt += 1) {
    await delay(350);
    next = await loadSnapshot(token);
  }
  return next;
}

function isAwaitingReconciliation(snapshot: HubSnapshot): boolean {
  const runtime = asRecord(snapshot.evidenceSummary?.runtime);
  const deployment = asRecord(runtime.deployment_state);
  const pendingOperations = numberOf(runtime.pending_operations_count) ?? 0;
  return runtime.offline_mode === false && pendingOperations === 0 && stringOf(deployment.state, "") === "PENDING";
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function actionTitle(action: string): string {
  return action
    .split("-")
    .map((part) => `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

function toneForPath(state: string): "good" | "warn" | "bad" | "neutral" {
  const normalized = state.toLowerCase();
  if (["ready", "released", "approved", "activated", "rolled_back", "complete"].includes(normalized)) return "good";
  if (["blocked", "failed", "error", "missing"].includes(normalized)) return "bad";
  if (["pending", "assigned", "advancing", "downloading", "imported", "preview", "preview_only"].includes(normalized)) return "warn";
  return "neutral";
}

function toneForReadinessStatus(status: string): "good" | "warn" | "bad" | "neutral" {
  const normalized = status.toLowerCase();
  if (normalized === "go") return "good";
  if (normalized === "attention") return "warn";
  if (normalized === "blocked") return "bad";
  return toneForPath(normalized);
}
