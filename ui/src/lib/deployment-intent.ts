import type { Device, EdgeRecommendation, JsonObject, Rollout, RolloutPlan, RuntimeTarget } from "../types";
import { deviceId, runtimeTargetId } from "./hub-format";
import type { MissionDraft } from "./mission-spec";
import type { ModelRecord, WorkflowTarget } from "./workbench-types";

const WORKBENCH_ACTOR = "operator:mission-package-workbench";

export interface EdgeRecommendationSelection {
  deviceId?: string;
  modelId?: string;
  runtimeTargetId?: string;
  workflowTarget: WorkflowTarget;
}

export interface DeploymentIntentQueueAction {
  request: JsonObject;
  title: string;
}

export interface RolloutApplyAction {
  request: JsonObject;
  title: string;
}

export interface RolloutOperatorAction {
  request: JsonObject;
  title: string;
}

export function edgeRecommendationSelection(
  recommendation: EdgeRecommendation
): EdgeRecommendationSelection {
  return {
    deviceId: recommendation.device_id ? String(recommendation.device_id) : undefined,
    modelId: recommendation.model_id ? String(recommendation.model_id) : undefined,
    runtimeTargetId: recommendation.runtime_target_id ? String(recommendation.runtime_target_id) : undefined,
    workflowTarget: "deployment"
  };
}

export function missionRolloutsForSelection({
  missionSlot,
  model,
  rollouts
}: {
  missionSlot: string;
  model: ModelRecord | undefined;
  rollouts: Rollout[];
}): Rollout[] {
  return rollouts.filter((rollout) => {
    if (!matchesMissionSlot(rollout.slot, missionSlot)) return false;
    if (!model) return true;
    return rollout.package_id === model.packageId && rollout.model_id === model.id;
  });
}

export function missionRolloutPlansForSelection({
  missionSlot,
  model,
  plans
}: {
  missionSlot: string;
  model: ModelRecord | undefined;
  plans: RolloutPlan[];
}): RolloutPlan[] {
  return plans.filter((plan) => {
    if (!matchesMissionSlot(plan.slot, missionSlot)) return false;
    if (!model) return true;
    return plan.package_id === model.packageId && (!plan.model_id || plan.model_id === model.id);
  });
}

export function buildDeploymentIntentRequest({
  actor = WORKBENCH_ACTOR,
  device,
  draft,
  model,
  requestedAt = new Date().toISOString(),
  runtime,
  source = "hub-ddil-drill"
}: {
  actor?: string;
  device: Device | undefined;
  draft: MissionDraft;
  model: ModelRecord | undefined;
  requestedAt?: string;
  runtime: RuntimeTarget | undefined;
  source?: string;
}): JsonObject {
  return {
    actor,
    source,
    package_id: model?.packageId,
    model_id: model?.id,
    device_id: device ? deviceId(device) : undefined,
    runtime_target_id: runtime ? runtimeTargetId(runtime) : undefined,
    slot: draft.slot || "vision",
    requested_at: requestedAt
  };
}

export function deploymentIntentQueueAction({
  device,
  draft,
  model,
  runtime
}: {
  device: Device | undefined;
  draft: MissionDraft;
  model: ModelRecord | undefined;
  runtime: RuntimeTarget | undefined;
}): DeploymentIntentQueueAction {
  return {
    request: buildDeploymentIntentRequest({ device, draft, model, runtime }),
    title: "Queue DDIL deployment intent"
  };
}

export function buildPackagePromotionRequest(nextPackageState: string): JsonObject {
  return {
    state: nextPackageState,
    actor: "operator:react-ui",
    reason: `promoted to ${nextPackageState} from Mission Package Workbench`
  };
}

export function buildRolloutApprovalRequest(): JsonObject {
  return {
    actor: "operator:approver-ui",
    reason: "mission policy approved from Mission Package Workbench"
  };
}

export function rolloutApprovalAction(rolloutId: string): RolloutOperatorAction {
  return {
    request: buildRolloutApprovalRequest(),
    title: `Approve ${rolloutId}`
  };
}

export function buildRolloutApplyRequest({
  rollout,
  selectedModel
}: {
  rollout: Rollout | undefined;
  selectedModel: ModelRecord | undefined;
}): JsonObject {
  return {
    actor: "operator:react-ui",
    model_id: rollout?.model_id || selectedModel?.id
  };
}

export function rolloutApplyAction({
  rollout,
  rolloutId,
  selectedModel
}: {
  rollout: Rollout | undefined;
  rolloutId: string;
  selectedModel: ModelRecord | undefined;
}): RolloutApplyAction {
  return {
    request: buildRolloutApplyRequest({ rollout, selectedModel }),
    title: `Apply ${rolloutId}`
  };
}

export function buildRolloutRollbackRequest(): JsonObject {
  return {
    actor: WORKBENCH_ACTOR,
    reason: "operator requested rollback from Mission Package Workbench"
  };
}

export function rolloutRollbackAction(rolloutId: string): RolloutOperatorAction {
  return {
    request: buildRolloutRollbackRequest(),
    title: `Rollback ${rolloutId}`
  };
}

export function buildRolloutPlanAdvanceRequest(): JsonObject {
  return {
    actor: WORKBENCH_ACTOR
  };
}

export function rolloutPlanAdvanceAction(planId: string): RolloutOperatorAction {
  return {
    request: buildRolloutPlanAdvanceRequest(),
    title: `Advance ${planId}`
  };
}

export function buildRolloutPlanPauseRequest(): JsonObject {
  return {
    actor: WORKBENCH_ACTOR,
    reason: "operator paused rollout plan from Mission Package Workbench"
  };
}

export function rolloutPlanPauseAction(planId: string): RolloutOperatorAction {
  return {
    request: buildRolloutPlanPauseRequest(),
    title: `Pause ${planId}`
  };
}

export function buildRolloutPlanResumeRequest(): JsonObject {
  return {
    actor: WORKBENCH_ACTOR,
    reason: "operator resumed rollout plan from Mission Package Workbench"
  };
}

export function rolloutPlanResumeAction(planId: string): RolloutOperatorAction {
  return {
    request: buildRolloutPlanResumeRequest(),
    title: `Resume ${planId}`
  };
}

function matchesMissionSlot(recordSlot: string | undefined, missionSlot: string): boolean {
  return !recordSlot || recordSlot === (missionSlot || "vision");
}
