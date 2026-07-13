import type { MissionPackageDownloadHandoff } from "../api";
import type { DeploymentReadiness, HubSnapshot, JsonObject } from "../types";
import { asRecord } from "./json";
import { buildEdgeProofWorkflow } from "./edge-proof-workflow";
import { buildEdgeRuntimeMission } from "./edge-runtime-mission";
import type { HubMissionContext } from "./hub-mission-context";
import {
  buildMissionPackageManifest,
  buildMissionPackageStageStatus
} from "./mission-package";
import type { MissionDraft } from "./mission-spec";
import { buildHubStages } from "./mission-workflow";
import {
  buildReadinessContext,
  readinessContextKey,
  readinessVerdictFromApi,
  scopedReadinessFor,
  syncingReadinessVerdict
} from "./readiness";
import { runtimeFitDisplayFor } from "./runtime-fit";
import { buildRuntimeStageView } from "./runtime-stage-view";
import type { HubStage } from "./workbench-types";

export function buildHubFlowState({
  activeHubStage,
  contextReadiness,
  hasLoadedSnapshot,
  lastMissionPackageHandoff,
  missionContext,
  missionDraft,
  missionPackagePlan,
  snapshot
}: {
  activeHubStage: HubStage;
  contextReadiness: DeploymentReadiness | undefined;
  hasLoadedSnapshot: boolean;
  lastMissionPackageHandoff: MissionPackageDownloadHandoff | undefined;
  missionContext: HubMissionContext;
  missionDraft: MissionDraft;
  missionPackagePlan: JsonObject | undefined;
  snapshot: HubSnapshot;
}) {
  const {
    deadLetteredOperations,
    derivedReadinessVerdict,
    ddilDetail,
    edgeRuntimeFit,
    evidenceDetail,
    evidenceValue,
    latestRollout,
    missionPhaseTotal,
    missionProofComplete,
    missionRollouts,
    offlineMode,
    pendingOperationLedger,
    pendingOperations,
    proofEvents,
    replayBlockedOperations,
    resourceEnvelopeFit,
    rolloutDetail,
    selectedDevice,
    selectedModel,
    selectedRuntime,
    selectedRuntimeValidation
  } = missionContext;

  const readinessContext = buildReadinessContext({
    device: selectedDevice,
    model: selectedModel,
    runtime: selectedRuntime,
    slot: missionDraft.slot
  });
  const scopedReadiness = scopedReadinessFor({
    context: readinessContext,
    contextReadiness,
    snapshotReadiness: snapshot.readiness
  });
  const runtimeDecision = asRecord(scopedReadiness?.runtime_decision);
  const edgeExecutionContract = asRecord(scopedReadiness?.edge_execution_contract);
  const runtimeFitDisplay = runtimeFitDisplayFor(scopedReadiness, edgeRuntimeFit, selectedRuntime);
  const runtimeStageView = buildRuntimeStageView({
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
  });
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

  return {
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
    readinessKey: readinessContextKey(readinessContext),
    readinessVerdict,
    runtimeDecision,
    runtimeFitDisplay,
    runtimeStageView,
    scopedReadiness,
    showProductStage
  };
}

export type HubFlowState = ReturnType<typeof buildHubFlowState>;
