import type { Device, RuntimeTarget, Toast } from "../types";
import { deviceId, runtimeTargetId } from "./hub-format";
import {
  missionDraftFromYaml,
  missionSelectionFromYaml,
  type MissionDraft
} from "./mission-spec";
import type { HubStage, ModelRecord } from "./workbench-types";

export interface MissionYamlImportResult {
  draft: MissionDraft;
  selectedDeviceId?: string;
  selectedModelId?: string;
  selectedRuntimeId?: string;
  toastDetail: string;
}

export interface MissionYamlImportAdoption {
  draft: MissionDraft;
  packageHandoff: undefined;
  packagePlan: undefined;
  selectedDeviceId?: string;
  selectedModelId?: string;
  selectedRuntimeId?: string;
  stage: HubStage;
  toast: Toast;
}

export function missionYamlImportAdoption(
  result: MissionYamlImportResult
): MissionYamlImportAdoption {
  return {
    draft: result.draft,
    packageHandoff: undefined,
    packagePlan: undefined,
    selectedDeviceId: result.selectedDeviceId,
    selectedModelId: result.selectedModelId,
    selectedRuntimeId: result.selectedRuntimeId,
    stage: "mission",
    toast: {
      tone: "success",
      title: "Mission YAML imported",
      detail: result.toastDetail
    }
  };
}

export function missionYamlImportErrorNotice(fileName: string): Toast {
  return {
    tone: "error",
    title: "Mission YAML import failed",
    detail: `${fileName} could not be read by the browser.`
  };
}

export function buildMissionYamlImportResult({
  currentDraft,
  devices,
  fileName,
  models,
  runtimeTargets,
  yaml
}: {
  currentDraft: MissionDraft;
  devices: Device[];
  fileName: string;
  models: ModelRecord[];
  runtimeTargets: RuntimeTarget[];
  yaml: string;
}): MissionYamlImportResult {
  const selection = missionSelectionFromYaml(yaml);
  const selectedYamlModel =
    (selection.modelId ? models.find((model) => model.id === selection.modelId) : undefined) ??
    (selection.packageId ? models.find((model) => model.packageId === selection.packageId) : undefined);
  const selectedYamlDevice = selection.deviceId
    ? devices.find((device) => deviceId(device) === selection.deviceId)
    : undefined;
  const selectedYamlRuntime = selection.runtimeTargetId
    ? runtimeTargets.find((target) => runtimeTargetId(target) === selection.runtimeTargetId)
    : undefined;
  const appliedSelection: string[] = [];
  const missingSelection: string[] = [];

  if (selectedYamlModel) {
    appliedSelection.push(`model ${selectedYamlModel.id}`);
  } else {
    if (selection.modelId) missingSelection.push(`model ${selection.modelId}`);
    if (!selection.modelId && selection.packageId) missingSelection.push(`package ${selection.packageId}`);
  }
  if (selectedYamlDevice) {
    appliedSelection.push(`edge ${deviceId(selectedYamlDevice)}`);
  } else if (selection.deviceId) {
    missingSelection.push(`edge ${selection.deviceId}`);
  }
  if (selectedYamlRuntime) {
    appliedSelection.push(`runtime ${runtimeTargetId(selectedYamlRuntime)}`);
  } else if (selection.runtimeTargetId) {
    missingSelection.push(`runtime ${selection.runtimeTargetId}`);
  }

  const detailParts = [`${fileName} populated mission, SLO, handling, and DDIL fields.`];
  if (appliedSelection.length) {
    detailParts.push(`Selected ${appliedSelection.join(", ")} from the spec.`);
  }
  if (missingSelection.length) {
    detailParts.push(`Unmatched hints: ${missingSelection.join(", ")}.`);
  }

  return {
    draft: missionDraftFromYaml(currentDraft, yaml),
    selectedDeviceId: selectedYamlDevice ? deviceId(selectedYamlDevice) : undefined,
    selectedModelId: selectedYamlModel?.id,
    selectedRuntimeId: selectedYamlRuntime ? runtimeTargetId(selectedYamlRuntime) : undefined,
    toastDetail: detailParts.join(" ")
  };
}
