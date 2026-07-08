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
  deviceId,
  errorToast,
  packageId,
  rolloutId,
  runtimeTargetId,
  saveToken,
  storedToken
} from "./lib/hub-format";
import {
  asRecord,
  stringOf
} from "./lib/json";
import {
  defaultMissionDraft,
  type MissionDraft
} from "./lib/mission-spec";
import { buildMissionYamlImportResult } from "./lib/mission-yaml-import";
import {
  buildMissionPackagePlanRequest,
  buildMissionPackageManifest,
  buildMissionPackageStageStatus,
  missionPackageRolloutId
} from "./lib/mission-package";
import { buildHubFormAction } from "./lib/hub-form-actions";
import { useHubStageNavigation } from "./lib/hub-stage-navigation";
import {
  buildHubStages,
  buildMissionWorkflowSignals,
  edgeReadinessCommandReason,
  hubStageForWorkflowTarget,
  hubStageRunbookFor,
  readinessActionContext,
  readinessCommand,
  workflowTargetForReadinessAction,
  workflowTargetLabel
} from "./lib/mission-workflow";
import {
  runtimeFitDisplayFor
} from "./lib/runtime-fit";
import { buildRuntimeStageView } from "./lib/runtime-stage-view";
import { runtimeWorkbenchRowRemediationCommand } from "./lib/runtime-remediation";
import { loadSnapshotAfterReconciliation } from "./lib/hub-actions";
import {
  buildEdgeProofQuery,
  buildEdgeProofWorkflow,
  downloadJson,
  edgeProofComponentDigestStatus,
  edgeProofTraceStatus,
  verifyEdgeProofComponentDigestStatus
} from "./lib/edge-proof-workflow";
import {
  buildPackagePromotionRequest,
  buildDeploymentIntentRequest,
  buildRolloutApplyRequest,
  buildRolloutApprovalRequest,
  buildRolloutPlanAdvanceRequest,
  buildRolloutPlanPauseRequest,
  buildRolloutPlanResumeRequest,
  buildRolloutRollbackRequest
} from "./lib/deployment-intent";
import { buildEdgeRuntimeMission } from "./lib/edge-runtime-mission";
import {
  buildDeadLetterRequeueRequest,
  buildPendingRuntimeRetargetRequest,
} from "./lib/field-ops-proof";
import { buildHubMissionContext } from "./lib/hub-mission-context";
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
    derivedReadinessVerdict,
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
    const result = buildMissionYamlImportResult({
      currentDraft: missionDraft,
      devices: snapshot.devices,
      fileName,
      models,
      runtimeTargets: snapshot.runtimeTargets,
      yaml
    });
    if (result.selectedModelId) setSelectedModelId(result.selectedModelId);
    if (result.selectedDeviceId) setSelectedDeviceId(result.selectedDeviceId);
    if (result.selectedRuntimeId) setSelectedRuntimeId(result.selectedRuntimeId);
    setMissionDraft(result.draft);
    setMissionPackagePlan(undefined);
    setLastMissionPackageHandoff(undefined);
    setToast({
      tone: "success",
      title: "Mission YAML imported",
      detail: result.toastDetail
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
        const proof = await loadEdgeRuntimeProof(token, buildEdgeProofQuery(readinessContext));
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
        const artifact = await downloadEdgeRuntimeProof(token, buildEdgeProofQuery(readinessContext));
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
    if (recommendation.model_id) setSelectedModelId(String(recommendation.model_id));
    if (recommendation.device_id) setSelectedDeviceId(String(recommendation.device_id));
    if (recommendation.runtime_target_id) setSelectedRuntimeId(String(recommendation.runtime_target_id));
    setFocusedWorkflow("deployment");
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
    const request = buildDeadLetterRequeueRequest(operation);
    if (!request) {
      setToast({
        tone: "info",
        title: "Requeue unavailable",
        detail: "This quarantined DDIL intent does not include a payload hash."
      });
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
      setToast({
        tone: "info",
        title: "Runtime retarget unavailable",
        detail: "This pending DDIL intent does not include a measured runtime target candidate."
      });
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
