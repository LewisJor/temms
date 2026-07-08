import type { Device, JsonObject, RuntimeTarget } from "../types";
import { deviceId, runtimeTargetId } from "./hub-format";
import type { MissionDraft } from "./mission-spec";
import type { ModelRecord } from "./workbench-types";

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
