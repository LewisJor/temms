import type { DeploymentReadiness, Device, JsonObject, RuntimeTarget, RuntimeValidation } from "../types";
import { deviceId, runtimeTargetId } from "./hub-format";
import { asRecord, stringOf } from "./json";
import {
  runtimeCapabilityLockForProof,
  runtimeLaneFor
} from "./runtime-fit";
import {
  runtimeDecisionCandidates,
  runtimeTargetAssessments,
  runtimeWorkbenchRows
} from "./runtime-decision";
import type {
  HubStage,
  ModelRecord,
  RuntimeRemediationContext,
  RuntimeWorkbenchRow
} from "./workbench-types";

export interface RuntimeStageView {
  artifactLane: JsonObject;
  capabilityLock: JsonObject;
  remediationContext: RuntimeRemediationContext;
  rows: RuntimeWorkbenchRow[];
  selectedLane: JsonObject;
}

export function buildRuntimeStageView({
  activeHubStage,
  edgeExecutionContract,
  readiness,
  runtimeDecision,
  runtimeTargets,
  runtimeValidations,
  selectedDevice,
  selectedModel,
  selectedRuntime,
  slot
}: {
  activeHubStage: HubStage;
  edgeExecutionContract: JsonObject;
  readiness: DeploymentReadiness | undefined;
  runtimeDecision: JsonObject;
  runtimeTargets: RuntimeTarget[];
  runtimeValidations: RuntimeValidation[];
  selectedDevice: Device | undefined;
  selectedModel: ModelRecord | undefined;
  selectedRuntime: RuntimeTarget | undefined;
  slot: string;
}): RuntimeStageView {
  const remediationContext: RuntimeRemediationContext = {
    packageId: selectedModel?.packageId ?? "",
    modelId: selectedModel?.id ?? "",
    deviceId: selectedDevice ? deviceId(selectedDevice) : "",
    slot: slot || "vision"
  };
  if (activeHubStage !== "runtime") {
    return {
      artifactLane: {},
      capabilityLock: {},
      remediationContext,
      rows: [],
      selectedLane: {}
    };
  }

  const contract = Object.keys(edgeExecutionContract).length ? edgeExecutionContract : runtimeDecision;
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const targetSelection = Object.keys(asRecord(contract.target_selection)).length
    ? asRecord(contract.target_selection)
    : asRecord(runtimeFit.target_selection);
  const selectedRuntimeTargetId = stringOf(
    targetSelection.selected_runtime_target_id,
    selectedRuntime ? runtimeTargetId(selectedRuntime) : ""
  );
  const bestRuntimeTargetId = stringOf(targetSelection.best_runtime_target_id, "");
  const candidates = runtimeDecisionCandidates(
    contract,
    runtimeFit,
    selectedRuntimeTargetId,
    bestRuntimeTargetId || selectedRuntimeTargetId
  );
  const assessments = runtimeTargetAssessments(contract, runtimeFit, candidates);
  const rows = runtimeWorkbenchRows({
    assessments,
    device: selectedDevice,
    model: selectedModel,
    runtimeFit,
    runtimeWorkbench: asRecord(readiness?.runtime_workbench),
    runtimeTargets,
    runtimeValidations,
    selectedRuntimeTargetId,
    bestRuntimeTargetId
  });
  const selectedRow = rows.find((row) => row.selected);
  return {
    artifactLane: asRecord(runtimeFit.artifact_lane),
    capabilityLock: runtimeCapabilityLockForProof(readiness),
    remediationContext,
    rows,
    selectedLane: selectedRow
      ? asRecord(selectedRow.target.runtime_lane)
      : runtimeLaneFor(runtimeFit, selectedRuntime)
  };
}
