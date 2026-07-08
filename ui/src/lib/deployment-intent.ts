import type { Device, JsonObject, Rollout, RolloutPlan, RuntimeTarget } from "../types";
import { deviceId, runtimeTargetId } from "./hub-format";
import type { MissionDraft } from "./mission-spec";
import type { ModelRecord } from "./workbench-types";

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
  actor = "operator:mission-package-workbench",
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

function matchesMissionSlot(recordSlot: string | undefined, missionSlot: string): boolean {
  return !recordSlot || recordSlot === (missionSlot || "vision");
}
