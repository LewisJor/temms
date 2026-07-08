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
  Button,
  PreviewPanel,
  ToastView
} from "./components/ui";
import { EdgeDeployStage } from "./components/edge-deploy-stage";
import { FieldOpsStage } from "./components/field-ops-stage";
import { PackageHandoffStage } from "./components/package-stage";
import { ReadinessCommandPanel } from "./components/readiness-panels";
import { EdgeOperatorCommandPanel } from "./components/runtime-operator-proof";
import { EdgeRuntimeWorkbench } from "./components/runtime-workbench";
import {
  HandlingPolicyPanel,
  MissionDesignPanel
} from "./components/mission-stages";
import { ModelPlanStage } from "./components/model-plan";
import { MissionWorkflowCockpit, StatusTile } from "./components/workbench-flow";
import {
  errorToast,
  packageId,
  rolloutId,
  saveToken,
  storedToken
} from "./lib/hub-format";
import {
  defaultMissionDraft,
  type MissionDraft
} from "./lib/mission-spec";
import {
  buildMissionYamlImportResult,
  missionYamlImportAdoption
} from "./lib/mission-yaml-import";
import {
  buildMissionPackageStageRequest,
  buildMissionPackagePlanRequest,
  missionPackageContextInvalidation,
  missionPackageDownloadAdoption,
  missionPackagePlanAdoption,
  missionPackageStagePlan
} from "./lib/mission-package";
import { buildHubFormAction } from "./lib/hub-form-actions";
import { useHubStageNavigation } from "./lib/hub-stage-navigation";
import {
  buildMissionWorkflowSignals,
  buildRuntimeFitTileSummary,
  hubStageRunbookFor,
  readinessActionFocusNotice,
  readinessActionPlan,
  readinessCommand,
  readinessCommandEdgeExecutionNotice,
  readinessCommandExecutionPlan
} from "./lib/mission-workflow";
import { runtimeWorkbenchRowRemediationCommand } from "./lib/runtime-remediation";
import { loadSnapshotAfterReconciliation } from "./lib/hub-actions";
import {
  buildEdgeProofQuery,
  downloadJson,
  edgeProofComponentDigestStatus,
  edgeProofComponentDigestVerificationFailureStatus,
  edgeProofComponentDigestVerificationPendingStatus,
  edgeProofDownloadAdoption,
  edgeProofGeneratedAdoption,
  edgeProofReadinessAdoptionForContext,
  edgeProofTraceStatus,
  verifyEdgeProofComponentDigestStatus
} from "./lib/edge-proof-workflow";
import {
  buildPackagePromotionRequest,
  buildDeploymentIntentRequest,
  edgeRecommendationSelection,
  buildRolloutApplyRequest,
  buildRolloutApprovalRequest,
  buildRolloutPlanAdvanceRequest,
  buildRolloutPlanPauseRequest,
  buildRolloutPlanResumeRequest,
  buildRolloutRollbackRequest
} from "./lib/deployment-intent";
import {
  buildAirgapExportRequest,
  buildBlockedOperationsQuarantineRequest,
  buildDeadLetterAcknowledgeRequest,
  buildDeadLetterBatchRequeueRequest,
  buildDeadLetterRequeueRequest,
  buildEvidenceExportRequest,
  buildPendingRuntimeRetargetRequest,
  deadLetterRequeueUnavailableNotice,
  pendingRuntimeRetargetUnavailableNotice
} from "./lib/field-ops-proof";
import { buildHubFlowState } from "./lib/hub-flow-state";
import {
  buildHubMissionContext,
  defaultDeviceSelectionId,
  defaultModelSelectionId,
  defaultRuntimeSelectionId
} from "./lib/hub-mission-context";
import { hasReadinessContextSelection } from "./lib/readiness";
import type {
  DeploymentReadiness,
  EdgeRecommendation,
  EvidenceExportMode,
  HubSnapshot,
  JsonObject,
  Preview,
  Toast
} from "./types";
import type {
  EdgeProofComponentDigestStatus,
  ReadinessGateAction
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

  const missionContext = useMemo(
    () =>
      buildHubMissionContext({
        missionDraft,
        selectedDeviceId,
        selectedModelId,
        selectedRuntimeId,
        snapshot
      }),
    [missionDraft, selectedDeviceId, selectedModelId, selectedRuntimeId, snapshot]
  );
  const {
    activeModelId,
    activeSlot,
    compatibleTargets,
    completedMissionPhases,
    connectivityState,
    ddilDetail,
    deadLetteredOperationLedger,
    deadLetteredOperations,
    deploymentDetail,
    deploymentStateName,
    edgeRecommendations,
    edgeRuntimeFit,
    evidenceDetail,
    evidenceValue,
    incompleteMissionPhases,
    latestEvents,
    latestRollout,
    missionPhaseTotal,
    missionPhases,
    missionProofComplete,
    missionRolloutPlans,
    missionRollouts,
    modelValidationCount,
    models,
    nextPackageState,
    offlineMode,
    pendingOperationLedger,
    pendingOperations,
    proofEvents,
    releasedPackages,
    replayBlockedOperations,
    resourceEnvelopeFit,
    rolloutDetail,
    runtimeRepairProof,
    selectedDevice,
    selectedModel,
    selectedPackage,
    selectedRuntime,
    selectedRuntimeValidation,
    signedEvidenceImports,
    signedPackages
  } = missionContext;
  const flowState = useMemo(
    () =>
      buildHubFlowState({
        activeHubStage,
        contextReadiness,
        hasLoadedSnapshot,
        lastMissionPackageHandoff,
        missionContext,
        missionDraft,
        missionPackagePlan,
        snapshot
      }),
    [
      activeHubStage,
      contextReadiness,
      hasLoadedSnapshot,
      lastMissionPackageHandoff,
      missionContext,
      missionDraft,
      missionPackagePlan,
      snapshot
    ]
  );
  const {
    canStageMissionPackage,
    edgeExecutionContract,
    edgeProofWorkflow,
    edgeRuntimeMission,
    hasMissionPackageDeploymentIntent,
    hubStages,
    missionPackageDeploymentCommand,
    missionPackageDeploymentIntent,
    missionPackageManifest,
    missionPackageStageStatus,
    missionReady,
    readinessContext,
    readinessKey,
    readinessVerdict,
    runtimeDecision,
    runtimeFitDisplay,
    runtimeStageView,
    scopedReadiness,
    showProductStage
  } = flowState;
  const runtimeFitTile = buildRuntimeFitTileSummary({
    compatibleTargets,
    runtimeFitDisplay,
    runtimeTargetCount: snapshot.runtimeTargets.length,
    selectedModel,
    selectedRuntime
  });
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
    setVerifiedEdgeProofComponentDigests(
      edgeProofComponentDigestVerificationPendingStatus(baseEdgeProofComponentDigests)
    );
    void verifyEdgeProofComponentDigestStatus(lastEdgeProof, baseEdgeProofComponentDigests)
      .then((status) => {
        if (!cancelled) setVerifiedEdgeProofComponentDigests(status);
      })
      .catch((error) => {
        if (!cancelled) {
          setVerifiedEdgeProofComponentDigests(
            edgeProofComponentDigestVerificationFailureStatus(baseEdgeProofComponentDigests, error)
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [baseEdgeProofComponentDigests, lastEdgeProof]);

  useEffect(() => {
    const nextModelId = defaultModelSelectionId({ activeModelId, models, selectedModelId });
    if (nextModelId) setSelectedModelId(nextModelId);
  }, [activeModelId, models, selectedModelId]);

  useEffect(() => {
    const nextDeviceId = defaultDeviceSelectionId({ devices: snapshot.devices, selectedDeviceId });
    if (nextDeviceId) setSelectedDeviceId(nextDeviceId);
  }, [snapshot.devices, selectedDeviceId]);

  useEffect(() => {
    const nextRuntimeId = defaultRuntimeSelectionId({ selectedRuntime, selectedRuntimeId });
    if (nextRuntimeId) setSelectedRuntimeId(nextRuntimeId);
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
    const invalidation = missionPackageContextInvalidation();
    setMissionPackagePlan(invalidation.plan);
    setLastMissionPackageHandoff(invalidation.handoff);
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
    const result = buildMissionYamlImportResult({
      currentDraft: missionDraft,
      devices: snapshot.devices,
      fileName,
      models,
      runtimeTargets: snapshot.runtimeTargets,
      yaml
    });
    const adoption = missionYamlImportAdoption(result);
    if (adoption.selectedModelId) setSelectedModelId(adoption.selectedModelId);
    if (adoption.selectedDeviceId) setSelectedDeviceId(adoption.selectedDeviceId);
    if (adoption.selectedRuntimeId) setSelectedRuntimeId(adoption.selectedRuntimeId);
    setMissionDraft(adoption.draft);
    setMissionPackagePlan(adoption.packagePlan);
    setLastMissionPackageHandoff(adoption.packageHandoff);
    setToast(adoption.toast);
    navigateHubStage(adoption.stage);
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
        const adoption = missionPackagePlanAdoption(plan);
        setMissionPackagePlan(adoption.plan);
        setLastMissionPackageHandoff(adoption.handoff);
        return adoption.preview;
      },
      false
    );
  }

  function downloadMissionPackageArtifact(): void {
    void run(
      "Download mission package",
      async () => {
        const artifact = await downloadMissionPackage(token, missionPackagePlanPayload());
        const adoption = missionPackageDownloadAdoption(artifact);
        setMissionPackagePlan(adoption.plan);
        setLastMissionPackageHandoff(adoption.handoff);
        if (adoption.fileName) downloadJson(adoption.fileName, adoption.plan);
        return adoption.preview;
      },
      false
    );
  }

  function stageMissionPackageRollout(): void {
    const stagePlan = missionPackageStagePlan({
      manifest: missionPackageManifest,
      stageStatus: missionPackageStageStatus
    });
    if (stagePlan.blocker) {
      setToast({ tone: "info", ...stagePlan.blocker });
      navigateHubStage(stagePlan.blockedStage);
      return;
    }
    void run(
      stagePlan.runTitle,
      async () => {
        const stage = await stageMissionPackage(
          token,
          buildMissionPackageStageRequest(missionPackageManifest)
        );
        navigateHubStage(stagePlan.successStage, { workflowTarget: stagePlan.successWorkflowTarget });
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
        const proof = await loadEdgeRuntimeProof(token, buildEdgeProofQuery(readinessContext));
        const adoption = edgeProofGeneratedAdoption(proof);
        adoptEdgeProofReadiness(adoption.proof);
        setLastEdgeProof(adoption.proof);
        setLastEdgeProofHandoff(adoption.handoff);
        return adoption.preview;
      },
      false
    );
  }

  function downloadEdgeProofArtifact(): void {
    void run(
      "Download edge runtime proof",
      async () => {
        const artifact = await downloadEdgeRuntimeProof(token, buildEdgeProofQuery(readinessContext));
        const adoption = edgeProofDownloadAdoption(artifact);
        adoptEdgeProofReadiness(adoption.proof);
        setLastEdgeProof(adoption.proof);
        setLastEdgeProofHandoff(adoption.handoff);
        if (adoption.fileName) downloadJson(adoption.fileName, adoption.proof);
        return adoption.preview;
      },
      false
    );
  }

  function adoptEdgeProofReadiness(proof: unknown): void {
    const adoption = edgeProofReadinessAdoptionForContext({ context: readinessContext, proof });
    if (!adoption) return;
    setContextReadiness(adoption.readiness);
    setSnapshot(adoption.applyToSnapshot);
  }

  function submitForm(name: string, event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const action = buildHubFormAction(name, event.currentTarget, token);
    if (action) void run(action.title, action.run, action.refresh);
  }

  function promoteSelectedPackage(): void {
    if (!selectedPackage || !nextPackageState) return;
    const id = packageId(selectedPackage);
    void run(`Promote ${id}`, () =>
      hubApi.promotePackage(
        id,
        buildPackagePromotionRequest(nextPackageState),
        token
      )
    );
  }

  function applyEdgeRecommendation(recommendation: EdgeRecommendation): void {
    const selection = edgeRecommendationSelection(recommendation);
    if (selection.modelId) setSelectedModelId(selection.modelId);
    if (selection.deviceId) setSelectedDeviceId(selection.deviceId);
    if (selection.runtimeTargetId) setSelectedRuntimeId(selection.runtimeTargetId);
    setFocusedWorkflow(selection.workflowTarget);
  }

  function approveRollout(id: string): void {
    void run(`Approve ${id}`, () =>
      hubApi.approveRollout(id, buildRolloutApprovalRequest(), token)
    );
  }

  function applyRollout(id: string): void {
    const rollout = snapshot.rollouts.find((candidate) => rolloutId(candidate) === id);
    void run(`Apply ${id}`, () =>
      hubApi.applyRollout(id, buildRolloutApplyRequest({ rollout, selectedModel }), token)
    );
  }

  function rollbackRollout(id: string): void {
    void run(`Rollback ${id}`, () =>
      hubApi.rollbackRollout(id, buildRolloutRollbackRequest(), token)
    );
  }

  function advanceRolloutPlan(id: string): void {
    void run(`Advance ${id}`, () =>
      hubApi.advanceRolloutPlan(id, buildRolloutPlanAdvanceRequest(), token)
    );
  }

  function pauseRolloutPlan(id: string): void {
    void run(`Pause ${id}`, () =>
      hubApi.pauseRolloutPlan(id, buildRolloutPlanPauseRequest(), token)
    );
  }

  function resumeRolloutPlan(id: string): void {
    void run(`Resume ${id}`, () =>
      hubApi.resumeRolloutPlan(id, buildRolloutPlanResumeRequest(), token)
    );
  }

  function exportEvidence(mode: EvidenceExportMode): void {
    void run(`Evidence ${mode}`, () => hubApi.exportEvidence(buildEvidenceExportRequest(mode), token), false);
  }

  function exportAirgap(includePackages: boolean): void {
    void run(
      includePackages ? "Export air-gap bundle with packages" : "Export air-gap bundle",
      () => hubApi.exportAirgap(buildAirgapExportRequest(includePackages), token),
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
      () => controlApi.quarantineBlocked(buildBlockedOperationsQuarantineRequest(), token),
      true
    );
  }

  function acknowledgeDeadLetteredOperations(): void {
    void run(
      "Acknowledge quarantined DDIL operations",
      () => controlApi.acknowledgeDeadLetters(buildDeadLetterAcknowledgeRequest(), token),
      true
    );
  }

  function requeueDeadLetteredOperations(): void {
    void run(
      "Requeue quarantined DDIL operations",
      () => controlApi.requeueDeadLetters(buildDeadLetterBatchRequeueRequest(), token),
      true
    );
  }

  function requeueDeadLetteredOperation(operation: Record<string, unknown>): void {
    const request = buildDeadLetterRequeueRequest(operation);
    if (!request) {
      setToast(deadLetterRequeueUnavailableNotice());
      return;
    }
    void run(
      "Requeue quarantined DDIL intent",
      () => controlApi.requeueDeadLetters(request, token),
      true
    );
  }

  function retargetPendingRuntime(operation: Record<string, unknown>): void {
    const request = buildPendingRuntimeRetargetRequest(operation);
    if (!request) {
      setToast(pendingRuntimeRetargetUnavailableNotice());
      return;
    }
    void run("Retarget pending runtime", () =>
      controlApi.retargetRuntime(request, token)
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

  function handleReadinessAction(action: ReadinessGateAction): void {
    const plan = readinessActionPlan(action);
    applyReadinessActionSelection(plan.selection);
    const { command, focus } = plan;
    navigateHubStage(focus.stage, { workflowTarget: focus.workflowTarget });
    setToast(readinessActionFocusNotice(focus));
    if (command) setPendingReadinessAction(action);
  }

  function applyReadinessActionSelection(selection: ReturnType<typeof readinessActionPlan>["selection"]): void {
    if (selection.modelId) setSelectedModelId(selection.modelId);
    if (selection.deviceId) setSelectedDeviceId(selection.deviceId);
    if (selection.runtimeTargetId) setSelectedRuntimeId(selection.runtimeTargetId);
  }

  function executePendingReadinessAction(): void {
    const action = pendingReadinessAction;
    const command = action ? readinessCommand(action) : undefined;
    if (!action || !command) return;
    const execution = readinessCommandExecutionPlan(action, command);
    const edgeExecutionNotice = readinessCommandEdgeExecutionNotice(execution);
    if (edgeExecutionNotice) {
      setToast(edgeExecutionNotice);
      return;
    }
    setPendingReadinessAction(undefined);
    void run(
      execution.runTitle,
      async () => {
        const payload = await executeReadinessCommand(command, token);
        if (execution.reconcileAfterRun) {
          const nextSnapshot = await loadSnapshotAfterReconciliation(token);
          setSnapshot(nextSnapshot);
          setReadinessRefreshVersion((version) => version + 1);
        }
        return payload;
      },
      execution.shouldRefreshAfterRun
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
              value={runtimeFitTile.value}
              detail={runtimeFitTile.detail}
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
