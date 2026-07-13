import type { DeploymentReadiness, Device, RuntimeTarget } from "../types";
import { deviceId, runtimeTargetId, toneForPath, toneForReadinessStatus } from "./hub-format";
import { asRecord, stringOf } from "./json";
import { readinessCommandFromValue } from "./mission-workflow";
import { compactMetricDetail } from "./runtime-fit";
import type { ModelRecord, ReadinessGate, ReadinessVerdict } from "./workbench-types";

export interface ReadinessContext {
  package_id?: string;
  model_id?: string;
  device_id?: string;
  runtime_target_id?: string;
  slot?: string;
}

export function buildReadinessContext({
  device,
  model,
  runtime,
  slot
}: {
  device: Device | undefined;
  model: ModelRecord | undefined;
  runtime: RuntimeTarget | undefined;
  slot: string;
}): ReadinessContext {
  return {
    package_id: model?.packageId,
    model_id: model?.id,
    device_id: device ? deviceId(device) : undefined,
    runtime_target_id: runtime ? runtimeTargetId(runtime) : undefined,
    slot: slot || "vision"
  };
}

export function readinessContextKey(context: ReadinessContext): string {
  return [
    context.package_id,
    context.model_id,
    context.device_id,
    context.runtime_target_id,
    context.slot
  ].join("|");
}

export function hasReadinessContextSelection(context: ReadinessContext): boolean {
  return Boolean(context.package_id || context.device_id || context.runtime_target_id);
}

export function scopedReadinessFor({
  context,
  contextReadiness,
  snapshotReadiness
}: {
  context: ReadinessContext;
  contextReadiness: DeploymentReadiness | undefined;
  snapshotReadiness: DeploymentReadiness | undefined;
}): DeploymentReadiness | undefined {
  if (readinessMatchesContext(contextReadiness, context)) return contextReadiness;
  if (readinessMatchesContext(snapshotReadiness, context)) return snapshotReadiness;
  return undefined;
}

export function readinessMatchesContext(
  readiness: DeploymentReadiness | undefined,
  context: ReadinessContext
): boolean {
  if (!readiness?.gates?.length) return false;
  return selectionMatchesContext(asRecord(readiness.selection), context);
}

export function selectionMatchesContext(
  selection: Record<string, unknown>,
  context: ReadinessContext
): boolean {
  return (
    matchesSelection(selection, "package_id", context.package_id) &&
    matchesSelection(selection, "model_id", context.model_id) &&
    matchesSelection(selection, "device_id", context.device_id) &&
    matchesSelection(selection, "runtime_target_id", context.runtime_target_id) &&
    matchesSelection(selection, "slot", context.slot)
  );
}

function matchesSelection(
  selection: Record<string, unknown>,
  key: string,
  expected: string | undefined
): boolean {
  return !expected || stringOf(selection[key], "") === expected;
}

export function syncingReadinessVerdict(): ReadinessVerdict {
  const gates: ReadinessGate[] = [
    "Model inventory",
    "Runtime target",
    "Edge telemetry",
    "Rollout state",
    "DDIL queue",
    "Evidence chain"
  ].map((label) => ({
    label,
    state: "syncing",
    detail: "Waiting for the latest Hub snapshot",
    tone: "neutral"
  }));
  return {
    label: "syncing",
    headline: "Synchronizing edge state",
    detail: "Fetching model, runtime, rollout, DDIL, and evidence state from Hub.",
    nextAction: "Waiting for Hub snapshot",
    tone: "neutral",
    gates
  };
}

export function readinessVerdictFromApi(readiness: DeploymentReadiness): ReadinessVerdict {
  const status = stringOf(readiness.status, "attention");
  return {
    label: status,
    headline: stringOf(readiness.headline, "Deployment readiness needs review"),
    detail: compactMetricDetail(stringOf(readiness.detail, "Review the deployment readiness gates before rollout.")),
    nextAction: compactMetricDetail(stringOf(readiness.next_action, "Review the attention gate")),
    tone: toneForReadinessStatus(status),
    gates: (readiness.gates ?? []).map((gate) => {
      const gateStatus = stringOf(gate.status, "");
      const gateState = stringOf(gate.state, gateStatus || "unknown");
      return {
        label: stringOf(gate.label, stringOf(gate.gate_id, "Gate")),
        state: gateState,
        detail: compactMetricDetail(stringOf(gate.detail, "No additional detail")),
        tone: gateStatus ? toneForReadinessStatus(gateStatus) : toneForPath(gateState),
        actions: (gate.actions ?? [])
          .map((action) => ({
            id: stringOf(action.action_id, stringOf(action.kind, stringOf(action.label, ""))),
            label: stringOf(action.label, ""),
            kind: stringOf(action.kind, ""),
            gateId: stringOf(action.gate_id, stringOf(gate.gate_id, "")),
            refs: asRecord(action.refs),
            command: readinessCommandFromValue(action.command)
          }))
          .filter((action) => action.label)
      };
    })
  };
}
