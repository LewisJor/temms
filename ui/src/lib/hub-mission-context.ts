import type { HubSnapshot } from "../types";
import { deviceId, nextPromotion, packageId, runtimeTargetId } from "./hub-format";
import { asRecord, latestByTime, numberOf, stringOf, stringsOf } from "./json";
import type { MissionDraft } from "./mission-spec";
import { missionRolloutPlansForSelection, missionRolloutsForSelection } from "./deployment-intent";
import {
  activeSlotForMission,
  latestRuntimeRepairProofFor,
  missionOperationLedgerForSlot,
  prioritizedEvidenceEvents
} from "./field-ops-proof";
import { buildReadinessVerdict, ddilStatusDetail } from "./mission-workflow";
import {
  edgeRuntimeCapabilityFit,
  isSigned,
  modelsForPackage,
  resourceEnvelopeCapabilityFit,
  runtimeForModel,
  runtimeValidationForModel,
  targetSupportsModel,
  withBenchmarkEvidence
} from "./runtime-fit";

export function buildHubMissionContext({
  missionDraft,
  selectedDeviceId,
  selectedModelId,
  selectedRuntimeId,
  snapshot
}: {
  missionDraft: MissionDraft;
  selectedDeviceId: string;
  selectedModelId: string;
  selectedRuntimeId: string;
  snapshot: HubSnapshot;
}) {
  const packageModels = snapshot.packages.flatMap(modelsForPackage);
  const models = withBenchmarkEvidence(packageModels, snapshot.benchmarks);
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
  const runtimeRepairProof = latestRuntimeRepairProofFor({
    evidenceSummary: snapshot.evidenceSummary,
    missionReplay: snapshot.missionReplay,
    pendingOperationLedger
  });
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

  return {
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
  };
}

export type HubMissionContext = ReturnType<typeof buildHubMissionContext>;
