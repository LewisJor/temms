import { hubApi } from "../api";
import type { JsonObject } from "../types";
import { actionTitle } from "./hub-actions";
import { csv, fieldValue, isChecked } from "./hub-format";

export interface HubFormAction {
  refresh: boolean;
  run: () => Promise<unknown>;
  title: string;
}

export function buildHubFormAction(
  name: string,
  form: HTMLFormElement,
  token: string
): HubFormAction | undefined {
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
  if (!handler) return undefined;
  return {
    refresh: name !== "compatibility-preview",
    run: handler,
    title: actionTitle(name)
  };
}
