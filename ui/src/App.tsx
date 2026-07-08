import {
  Activity,
  BadgeCheck,
  Box,
  Cpu,
  GitBranch,
  KeyRound,
  RefreshCw,
  ShieldCheck
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
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
  MissionPackageDownloadHandoff
} from "./api";
import {
  Badge,
  Button,
  PreviewPanel,
  ReadinessCard,
  ToastView
} from "./components/ui";
import { EdgeDeployStage } from "./components/edge-deploy-stage";
import { FieldOpsStage } from "./components/field-ops-stage";
import { PackageHandoffStage } from "./components/package-stage";
import {
  ReadinessCommandPanel,
} from "./components/readiness-panels";
import { EdgeOperatorCommandPanel } from "./components/runtime-operator-proof";
import { EdgeRuntimeWorkbench } from "./components/runtime-workbench";
import {
  HandlingPolicyPanel,
  MissionDesignPanel
} from "./components/mission-stages";
import { ModelPlanStage } from "./components/model-plan";
import { MissionWorkflowCockpit, StatusTile } from "./components/workbench-flow";
import {
  csv,
  deviceId,
  errorToast,
  fieldValue,
  isChecked,
  nextPromotion,
  packageId,
  rolloutId,
  runtimeTargetId,
  saveToken,
  storedToken,
  toneForPath
} from "./lib/hub-format";
import {
  asRecord,
  latestByTime,
  numberOf,
  stringOf,
  stringsOf
} from "./lib/json";
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
import { useHubStageNavigation } from "./lib/hub-stage-navigation";
import {
  buildHubStages,
  buildMissionWorkflowSignals,
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
  edgeRuntimeCapabilityFit,
  formatPerformanceSlo,
  isSigned,
  modelsForPackage,
  resourceEnvelopeCapabilityFit,
  runtimeForModel,
  runtimeFitDisplayFor,
  runtimeValidationForModel,
  targetSupportsModel,
  withBenchmarkEvidence
} from "./lib/runtime-fit";
import { buildRuntimeStageView } from "./lib/runtime-stage-view";
import { formatProofCommand } from "./lib/proof-command";
import { runtimeWorkbenchRowRemediationCommand } from "./lib/runtime-remediation";
import { actionTitle, loadSnapshotAfterReconciliation } from "./lib/hub-actions";
import {
  buildEdgeProofWorkflow,
  downloadJson,
  edgeProofComponentDigestStatus,
  edgeProofTraceStatus,
  verifyEdgeProofComponentDigestStatus
} from "./lib/edge-proof-workflow";
import {
  buildDeploymentIntentRequest,
  missionRolloutPlansForSelection,
  missionRolloutsForSelection
} from "./lib/deployment-intent";
import { buildEdgeRuntimeMission } from "./lib/edge-runtime-mission";
import {
  activeSlotForMission,
  latestRuntimeRepairProofFor,
  missionOperationLedgerForSlot,
  prioritizedEvidenceEvents
} from "./lib/field-ops-proof";
import {
  buildReadinessContext,
  hasReadinessContextSelection,
  readinessMatchesContext,
  readinessContextKey,
  readinessVerdictFromApi,
  selectionMatchesContext,
  scopedReadinessFor,
  syncingReadinessVerdict
} from "./lib/readiness";
import type {
  DeploymentReadiness,
  Device,
  EdgeRecommendation,
  EvidenceExportMode,
  HubSnapshot,
  JsonObject,
  Preview,
  Toast
} from "./types";
import type {
  EdgeProofComponentDigestStatus,
  ReadinessGateAction,
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
  const [pendingReadinessAction, setPendingReadinessAction] = useState<ReadinessGateAction | undefined>();
  const {
    activeHubStage,
    assetsWorkflowRef,
    ddilWorkflowRef,
    deploymentWorkflowRef,
    evidenceWorkflowRef,
    focusedWorkflow,
    modelWorkflowRef,
    navigateHubStage,
    plansWorkflowRef,
    rolloutsWorkflowRef,
    setFocusedWorkflow,
    stageFlowRef,
    workflowClass
  } = useHubStageNavigation();

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
  const activeSlot = activeSlotForMission(snapshot.evidenceSummary?.active_slots, missionDraft.slot);
  const missionRollouts = missionRolloutsForSelection({
    missionSlot: missionDraft.slot,
    model: selectedModel,
    rollouts: snapshot.rollouts
  });
  const missionRolloutPlans = missionRolloutPlansForSelection({
    missionSlot: missionDraft.slot,
    model: selectedModel,
    plans: snapshot.rolloutPlans
  });
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
  const pendingOperationTypes = stringsOf(evidenceRuntime.pending_operation_types);
  const hasPendingOperationRecords = Array.isArray(evidenceRuntime.pending_operations);
  const pendingOperationLedger = missionOperationLedgerForSlot(
    evidenceRuntime.pending_operations,
    missionDraft.slot
  );
  const pendingOperations = hasPendingOperationRecords
    ? pendingOperationLedger.length
    : numberOf(evidenceRuntime.pending_operations_count) ?? 0;
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
  const allDeadLetteredOperations = missionOperationLedgerForSlot(
    evidenceRuntime.pending_operation_dead_letters,
    missionDraft.slot
  );
  const deadLetteredOperationLedger = allDeadLetteredOperations.filter(
    (operation) => operation.acknowledged !== true && operation.requeued !== true
  );
  const hasDeadLetterRecords = Array.isArray(evidenceRuntime.pending_operation_dead_letters);
  const totalDeadLetteredOperations = hasDeadLetterRecords
    ? allDeadLetteredOperations.length
    : numberOf(evidenceRuntime.pending_operation_dead_letters_count) ?? 0;
  const deadLetteredOperations = hasDeadLetterRecords
    ? deadLetteredOperationLedger.length
    : numberOf(evidenceRuntime.pending_operation_dead_letters_unresolved_count) ?? totalDeadLetteredOperations;
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
    activeModelId || selectedModel?.id || "",
    missionDraft.slot
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
    () => buildReadinessContext({
      device: selectedDevice,
      model: selectedModel,
      runtime: selectedRuntime,
      slot: missionDraft.slot
    }),
    [missionDraft.slot, selectedDevice, selectedModel, selectedRuntime]
  );
  const readinessKey = readinessContextKey(readinessContext);
  const scopedReadiness = scopedReadinessFor({
    context: readinessContext,
    contextReadiness,
    snapshotReadiness: snapshot.readiness
  });
  const runtimeDecision = asRecord(scopedReadiness?.runtime_decision);
  const edgeExecutionContract = asRecord(scopedReadiness?.edge_execution_contract);
  const runtimeFitDisplay = runtimeFitDisplayFor(scopedReadiness, edgeRuntimeFit, selectedRuntime);
  const runtimeStageView = useMemo(() => buildRuntimeStageView({
    activeHubStage,
    edgeExecutionContract,
    readiness: scopedReadiness,
    runtimeDecision,
    runtimeTargets: snapshot.runtimeTargets,
    runtimeValidations: snapshot.runtimeValidations,
    selectedDevice,
    selectedModel,
    selectedRuntime,
    slot: missionDraft.slot
  }), [
    activeHubStage,
    edgeExecutionContract,
    missionDraft.slot,
    runtimeDecision,
    scopedReadiness,
    selectedDevice,
    selectedModel,
    selectedRuntime,
    snapshot.runtimeTargets,
    snapshot.runtimeValidations
  ]);
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
    runtimeFitDisplay,
    slot: readinessContext.slot || missionDraft.slot
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
    if (!hasReadinessContextSelection(readinessContext)) {
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
        buildDeploymentIntentRequest({
          device: selectedDevice,
          draft: missionDraft,
          model: selectedModel,
          runtime: selectedRuntime
        }),
        token
      )
    );
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
  const workflowSignals = buildMissionWorkflowSignals({
    missionDraft,
    missionPackageStageStatus,
    runtimeFitDisplay,
    selectedDevice,
    selectedModel,
    selectedRuntime
  });

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
            artifactLane={runtimeStageView.artifactLane}
            capabilityLock={runtimeStageView.capabilityLock}
            commandForRow={runtimeWorkbenchRowRemediationCommand}
            devices={snapshot.devices}
            modelCount={models.length}
            remediationContext={runtimeStageView.remediationContext}
            resourceEnvelopeFit={resourceEnvelopeFit}
            runtimeFitDisplay={runtimeFitDisplay}
            rows={runtimeStageView.rows}
            runtimeTargets={snapshot.runtimeTargets}
            selectedDevice={selectedDevice}
            selectedLane={runtimeStageView.selectedLane}
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
        <PackageHandoffStage
          canStageMissionPackage={canStageMissionPackage}
          componentDigests={edgeProofComponentDigests}
          disabled={loading}
          edgeExecutionContract={edgeExecutionContract}
          edgeProofHandoff={lastEdgeProofHandoff}
          edgeRuntimeFit={edgeRuntimeFit}
          manifest={missionPackageManifest}
          missionPackageHandoff={lastMissionPackageHandoff}
          proof={lastEdgeProof}
          readiness={scopedReadiness}
          readinessVerdict={readinessVerdict}
          resourceEnvelopeFit={resourceEnvelopeFit}
          runtimeDecision={runtimeDecision}
          runtimeFitDisplay={runtimeFitDisplay}
          runtimeMission={edgeRuntimeMission}
          selectedDevice={selectedDevice}
          selectedModel={selectedModel}
          selectedRuntime={selectedRuntime}
          selectedRuntimeValidation={selectedRuntimeValidation}
          trace={edgeProofTrace}
          workflow={edgeProofWorkflow}
          onCopyCommand={(label, command) => void copyCommand(label, command)}
          onCopyManifest={copyMissionPackageManifest}
          onDownloadEdgeProof={downloadEdgeProofArtifact}
          onDownloadPackage={downloadMissionPackageArtifact}
          onGenerateEdgeProof={generateEdgeProofArtifact}
          onGoDeploy={() => navigateHubStage("deploy")}
          onPlanPackage={planMissionPackageArtifact}
          onReadinessAction={handleReadinessAction}
          onSelectRuntimeTarget={(runtimeTargetIdValue) => setSelectedRuntimeId(runtimeTargetIdValue)}
          onStageDeploy={stageMissionPackageRollout}
        />
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
          <FieldOpsStage
            activeSlot={activeSlot}
            completedMissionPhases={completedMissionPhases}
            connectivityState={connectivityState}
            ddilSectionClassName={workflowClass("ddil", "section section-wide readiness-section repair-section")}
            ddilRef={ddilWorkflowRef}
            deadLetteredOperationLedger={deadLetteredOperationLedger}
            deadLetteredOperations={deadLetteredOperations}
            deploymentDetail={deploymentDetail}
            deploymentStateName={deploymentStateName}
            evidenceBundles={snapshot.evidenceBundles}
            evidenceSectionClassName={workflowClass("evidence", "section evidence-section")}
            evidenceRef={evidenceWorkflowRef}
            evidenceSummary={snapshot.evidenceSummary}
            incompleteMissionPhases={incompleteMissionPhases}
            latestEvents={latestEvents}
            missionPhaseTotal={missionPhaseTotal}
            missionPhases={missionPhases}
            missionProofComplete={missionProofComplete}
            missionReplayHeadline={snapshot.missionReplay?.headline}
            offlineMode={offlineMode}
            pendingOperationLedger={pendingOperationLedger}
            pendingOperations={pendingOperations}
            proofEvents={proofEvents}
            replayBlockedOperations={replayBlockedOperations}
            runtimeRepairProof={runtimeRepairProof}
            selectedDevice={selectedDevice}
            selectedModel={selectedModel}
            signedEvidenceImports={signedEvidenceImports}
            onAcknowledgeDeadLetteredOperations={acknowledgeDeadLetteredOperations}
            onCopyCommand={(label, command) => void copyCommand(label, command)}
            onEnterOfflineMode={enterOfflineMode}
            onExportAirgap={exportAirgap}
            onExportEvidence={exportEvidence}
            onQuarantineBlockedOperations={quarantineBlockedOperations}
            onQueueDeploymentIntent={queueDeploymentIntent}
            onRequeueDeadLetteredOperation={requeueDeadLetteredOperation}
            onRequeueDeadLetteredOperations={requeueDeadLetteredOperations}
            onRestoreOnlineMode={restoreOnlineMode}
            onRetargetRuntime={retargetPendingRuntime}
            onSyncPendingOperations={syncPendingOperations}
          />
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
          <EdgeDeployStage
            canStageMissionPackage={canStageMissionPackage}
            deploymentSectionClassName={workflowClass("deployment", "section section-wide deployment-section")}
            deploymentRef={deploymentWorkflowRef}
            devices={snapshot.devices}
            edgeRecommendations={edgeRecommendations}
            edgeRuntimeFit={edgeRuntimeFit}
            evidenceBundleCount={snapshot.evidenceBundles.length}
            evidenceValue={evidenceValue}
            hasMissionPackageDeploymentIntent={hasMissionPackageDeploymentIntent}
            latestRollout={latestRollout}
            missionSlot={missionDraft.slot}
            missionPackageDeploymentCommand={missionPackageDeploymentCommand}
            missionPackageDeploymentIntent={missionPackageDeploymentIntent}
            missionPackageStageStatus={missionPackageStageStatus}
            missionRolloutPlans={missionRolloutPlans}
            missionRollouts={missionRollouts}
            plansSectionClassName={workflowClass("plans", "section section-wide rollout-plan-section deploy-secondary-section")}
            plansRef={plansWorkflowRef}
            proofEvents={proofEvents}
            readiness={scopedReadiness}
            readinessVerdict={readinessVerdict}
            resourceEnvelopeFit={resourceEnvelopeFit}
            rolloutsSectionClassName={workflowClass("rollouts", "section rollout-section deploy-secondary-section")}
            rolloutsRef={rolloutsWorkflowRef}
            runtimeFitDisplay={runtimeFitDisplay}
            runtimeTargets={snapshot.runtimeTargets}
            selectedDevice={selectedDevice}
            selectedModel={selectedModel}
            selectedRuntime={selectedRuntime}
            selectedRuntimeValidation={selectedRuntimeValidation}
            onAdvanceRolloutPlan={advanceRolloutPlan}
            onApplyEdgeRecommendation={applyEdgeRecommendation}
            onApplyRollout={applyRollout}
            onApproveRollout={approveRollout}
            onPauseRolloutPlan={pauseRolloutPlan}
            onResumeRolloutPlan={resumeRolloutPlan}
            onRollbackRollout={rollbackRollout}
            onSelectDevice={setSelectedDeviceId}
            onSelectRuntime={setSelectedRuntimeId}
            onStageMissionPackageRollout={stageMissionPackageRollout}
            onSubmitForm={submitForm}
          />
        ) : null}

      </main>
      ) : null}

      {preview ? <PreviewPanel preview={preview} onClear={() => setPreview(undefined)} /> : null}
    </div>
  );
}
