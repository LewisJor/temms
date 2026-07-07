import type { DeploymentReadiness, Device, JsonObject, MissionReplay, RuntimeTarget, RuntimeValidation } from "../types";
import { deviceId, runtimeTargetId, toneForReadinessStatus } from "./hub-format";
import { asRecord, numberOf, stringOf, stringsOf } from "./json";
import {
  artifactLaneDetail,
  artifactLaneTone,
  artifactLaneValue,
  compactMetricDetail,
  performanceSloDetail,
  performanceSloLabel,
  performanceSloTone,
  productionAdmissionDetail,
  runtimeInventoryDetail,
  runtimeInventoryLabel,
  runtimeInventoryTone,
  runtimeLaneDetail,
  runtimeLaneFor,
  runtimeLaneTone,
  runtimeLaneValue,
  runtimeTargetSelectionDetail,
  runtimeTargetSelectionTone
} from "./runtime-fit";
import type {
  EdgeMissionMetric,
  EdgeRuntimeFit,
  EdgeRuntimeMission,
  GateTone,
  ModelRecord,
  ReadinessVerdict,
  RuntimeFitDisplay
} from "./workbench-types";

function edgeRuntimeMissionFromApi(value: unknown): EdgeRuntimeMission | undefined {
  const mission = asRecord(value);
  if (mission.schema_version !== "temms-edge-runtime-mission/v1") return undefined;
  const metrics = asRecord(mission.metrics);
  const path = asRecord(mission.path);
  const metricRows: EdgeMissionMetric[] = [
    edgeRuntimeMissionMetric(metrics.runtime_fit, "Runtime fit", apiScoreOrState),
    edgeRuntimeMissionMetric(metrics.runtime_lane, "Runtime lane", apiRuntimeLaneValue),
    edgeRuntimeMissionMetric(metrics.artifact_fit, "Artifact", apiStateOrFormat),
    edgeRuntimeMissionMetric(metrics.live_inventory, "Live inventory", apiStateValue),
    edgeRuntimeMissionMetric(metrics.performance, "Performance", apiStateValue),
    edgeRuntimeMissionMetric(metrics.resources, "Resources", apiStateValue),
    edgeRuntimeMissionMetric(metrics.runtime_validation, "Validation", apiStateValue),
    apiRuntimeDecisionMetric(metrics.runtime_decision),
    edgeRuntimeMissionMetric(
      metrics.ddil_repair || metrics.production_admission,
      metrics.ddil_repair ? "DDIL repair" : "Production apply",
      apiStateValue
    )
  ];
  return {
    headline: stringOf(mission.headline, "Selected edge path needs review"),
    detail: stringOf(mission.detail, "Review the selected model/device/runtime path."),
    tone: toneForReadinessStatus(stringOf(mission.status, "")),
    path: stringOf(path.label, "model -> runtime -> edge"),
    metrics: metricRows,
    focus: stringsOf(mission.operator_focus).length
      ? stringsOf(mission.operator_focus)
      : ["Selected on-device gates are aligned"]
  };
}

function apiRuntimeDecisionMetric(value: unknown): EdgeMissionMetric {
  const metric = asRecord(value);
  const action = stringOf(metric.recommended_action, "review").replace(/_/g, " ");
  const best = stringOf(metric.best_runtime_target_id, "");
  const applyAllowed = metric.apply_allowed === true;
  const status = stringOf(metric.status, "");
  const detail = stringOf(
    metric.detail,
    best ? `best runtime ${best}` : "runtime decision evidence pending"
  );
  return {
    label: "Runtime decision",
    value: action,
    detail: compactMetricDetail(detail),
    tone:
      metric.apply_allowed === false || status === "selected_not_eligible"
        ? "bad"
        : status === "upgrade_available"
          ? "warn"
          : applyAllowed || action === "apply or stage"
            ? "good"
            : "neutral"
  };
}

function edgeRuntimeMissionMetric(
  value: unknown,
  label: string,
  valueFormatter: (metric: JsonObject) => string
): EdgeMissionMetric {
  const metric = asRecord(value);
  const status = stringOf(metric.status, "");
  return {
    label,
    value: valueFormatter(metric),
    detail: apiMetricDetail(metric, label),
    tone: toneForReadinessStatus(status)
  };
}

function apiMetricDetail(metric: JsonObject, label: string): string {
  const detail = stringOf(metric.detail, "");
  if (detail) return compactMetricDetail(detail);
  if (label === "Runtime lane") return runtimeLaneDetail(metric);
  if (label === "Production apply") return productionAdmissionDetail(metric);
  if (label === "Runtime fit") return apiScoreOrState(metric);
  if (label === "Artifact") return artifactLaneDetail(metric);
  return stringOf(metric.state, stringOf(metric.status, "No detail reported")).replace(/_/g, " ");
}

function apiScoreOrState(metric: JsonObject): string {
  const score = numberOf(metric.score);
  const tier = stringOf(metric.tier, "").replace(/_/g, " ");
  if (score !== undefined) return tier ? `${score}/100 ${tier}` : `${score}/100`;
  return apiStateValue(metric);
}

function apiRuntimeLaneValue(metric: JsonObject): string {
  return stringOf(metric.label, stringOf(metric.lane_id, apiStateValue(metric)));
}

function apiStateOrFormat(metric: JsonObject): string {
  return stringOf(metric.state, stringOf(metric.model_format, apiStateValue(metric))).replace(/_/g, " ");
}

function apiStateValue(metric: JsonObject): string {
  return stringOf(metric.state, stringOf(metric.status, "unknown")).replace(/_/g, " ");
}

export function buildEdgeRuntimeMission({
  device,
  edgeRuntimeFit,
  missionReplay,
  model,
  pendingOperationLedger,
  pendingOperations,
  readiness,
  readinessVerdict,
  replayBlockedOperations,
  resourceEnvelopeFit,
  runtime,
  runtimeFitDisplay,
  runtimeValidation
}: {
  device: Device | undefined;
  edgeRuntimeFit: EdgeRuntimeFit;
  missionReplay: MissionReplay | undefined;
  model: ModelRecord | undefined;
  pendingOperationLedger: Record<string, unknown>[];
  pendingOperations: number;
  readiness: DeploymentReadiness | undefined;
  readinessVerdict: ReadinessVerdict;
  replayBlockedOperations: number;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtime: RuntimeTarget | undefined;
  runtimeFitDisplay: RuntimeFitDisplay;
  runtimeValidation: RuntimeValidation | undefined;
}): EdgeRuntimeMission {
  const apiMission = edgeRuntimeMissionFromApi(readiness?.edge_runtime_mission);
  if (apiMission) return apiMission;
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const runtimeLane = runtimeLaneFor(runtimeFit, runtime);
  const artifactLane = asRecord(runtimeFit.artifact_lane);
  const ddilRepair = ddilRuntimeRepairMetric({
    missionReplay,
    pendingOperationLedger,
    pendingOperations,
    replayBlockedOperations
  });
  const modelLabel = model?.id ?? "model missing";
  const runtimeLabel = runtime ? runtimeTargetId(runtime) : "runtime missing";
  const deviceLabel = device ? deviceId(device) : "edge missing";
  const validationDetail = runtimeValidation
    ? `${runtimeLabel} passed package validation`
    : edgeRuntimeFit.detail;
  const inventoryTone = edgeRuntimeFit.tone === "bad" ? "bad" : runtimeInventoryTone(device);
  const focus = edgeMissionFocus({
    ddilRepair,
    edgeRuntimeFit,
    readinessVerdict,
    resourceEnvelopeFit,
    runtimeFit
  });
  const metrics: EdgeMissionMetric[] = [
    {
      label: "Runtime fit",
      value: runtimeFitDisplay.label,
      detail: runtimeFitDisplay.detail,
      tone: runtimeFitDisplay.tone
    },
    {
      label: "Runtime lane",
      value: runtimeLaneValue(runtimeLane),
      detail: runtimeLaneDetail(runtimeLane),
      tone: runtimeLaneTone(runtimeLane)
    },
    {
      label: "Artifact",
      value: artifactLaneMissionValue(artifactLane, model),
      detail: artifactLaneMissionDetail(artifactLane, model),
      tone: artifactLaneMissionTone(artifactLane)
    },
    {
      label: "Live inventory",
      value: runtimeInventoryLabel(device),
      detail: runtimeInventoryDetail(device),
      tone: inventoryTone
    },
    {
      label: "Performance",
      value: performanceSloLabel(model),
      detail: model ? performanceSloDetail(model) : "select a model",
      tone: performanceSloTone(model)
    },
    {
      label: "Resources",
      value: resourceEnvelopeFit.label,
      detail: resourceEnvelopeFit.detail,
      tone: resourceEnvelopeFit.tone
    },
    {
      label: "Validation",
      value: runtimeValidation ? "validated" : edgeRuntimeFit.label,
      detail: validationDetail,
      tone: runtimeValidation ? "good" : edgeRuntimeFit.tone
    },
    ddilRepair
  ];

  return {
    headline: edgeMissionHeadline(readinessVerdict),
    detail: edgeMissionDetail(readinessVerdict, model, runtime, device),
    tone: readinessVerdict.tone,
    path: `${modelLabel} -> ${runtimeLabel} -> ${deviceLabel}`,
    metrics,
    focus
  };
}

function edgeMissionHeadline(verdict: ReadinessVerdict): string {
  if (verdict.tone === "good") return "Selected model is proven for the edge path";
  if (verdict.tone === "bad") return "Selected edge path is blocked";
  if (verdict.tone === "warn") return "Selected edge path needs operator proof";
  return "Selected edge path is syncing";
}

function edgeMissionDetail(
  verdict: ReadinessVerdict,
  model: ModelRecord | undefined,
  runtime: RuntimeTarget | undefined,
  device: Device | undefined
): string {
  if (!model || !runtime || !device) return "Select a model, runtime target, and edge node to evaluate on-device deployment.";
  return `${verdict.nextAction} (${model.format || "artifact"} on ${runtimeTargetId(runtime)} at ${deviceId(device)}).`;
}

function artifactLaneMissionValue(artifactLane: JsonObject, model: ModelRecord | undefined): string {
  const value = artifactLaneValue(artifactLane);
  if (value !== "not classified") return value;
  return model?.format ? `${model.format} artifact` : value;
}

function artifactLaneMissionDetail(artifactLane: JsonObject, model: ModelRecord | undefined): string {
  const detail = artifactLaneDetail(artifactLane);
  if (detail !== "artifact format has not been evaluated for this runtime lane") return detail;
  return model?.format
    ? `${model.format} artifact awaiting runtime-lane evaluation`
    : detail;
}

function artifactLaneMissionTone(artifactLane: JsonObject): GateTone {
  const tone = artifactLaneTone(artifactLane);
  return tone === "neutral" ? "warn" : tone;
}

function ddilRuntimeRepairMetric({
  missionReplay,
  pendingOperationLedger,
  pendingOperations,
  replayBlockedOperations
}: {
  missionReplay: MissionReplay | undefined;
  pendingOperationLedger: Record<string, unknown>[];
  pendingOperations: number;
  replayBlockedOperations: number;
}): EdgeMissionMetric {
  const repairCandidate = pendingOperationLedger.find((operation) =>
    stringOf(operation.runtime_remediation_runtime_target_id, "")
  );
  if (repairCandidate) {
    const previous = stringOf(repairCandidate.runtime_remediation_previous_runtime_target_id, "");
    const target = stringOf(repairCandidate.runtime_remediation_runtime_target_id, "");
    const delta = numberOf(repairCandidate.runtime_remediation_score_delta);
    return {
      label: "DDIL repair",
      value: "repair available",
      detail: `${previous ? `${previous} -> ` : ""}${target}${delta !== undefined ? ` (+${delta} fit)` : ""}`,
      tone: "warn"
    };
  }
  if (replayBlockedOperations) {
    return {
      label: "DDIL repair",
      value: "blocked replay",
      detail: `${replayBlockedOperations} queued runtime intent${replayBlockedOperations === 1 ? "" : "s"} blocked by preflight`,
      tone: "bad"
    };
  }

  const proof = latestRuntimeRetargetProof(missionReplay);
  if (proof) {
    return {
      label: "DDIL repair",
      value: "retarget proved",
      detail: proof,
      tone: "good"
    };
  }
  if (pendingOperations) {
    return {
      label: "DDIL repair",
      value: "queued",
      detail: `${pendingOperations} signed intent${pendingOperations === 1 ? "" : "s"} awaiting sync`,
      tone: "warn"
    };
  }
  return {
    label: "DDIL repair",
    value: "clear",
    detail: "no runtime repair pending",
    tone: "good"
  };
}

function latestRuntimeRetargetProof(missionReplay: MissionReplay | undefined): string {
  const events = Array.isArray(missionReplay?.events) ? missionReplay.events.map(asRecord) : [];
  const event = events.find((candidate) => {
    const summary = stringOf(candidate.summary, "");
    const detail = stringOf(candidate.detail, "");
    return (
      candidate.runtime_retargeted === true ||
      summary.includes("DDIL replay retargeted") ||
      detail.startsWith("retargeted ")
    );
  });
  if (event) {
    const detail = stringOf(event.detail, "");
    const summary = stringOf(event.summary, "retargeted DDIL replay");
    return detail ? `${summary}; ${detail}` : summary;
  }
  const phases = Array.isArray(missionReplay?.phases) ? missionReplay.phases : [];
  const phase = phases.find((candidate) => {
    const summary = candidate.summary ?? "";
    return candidate.phase === "offline_operation" && summary.includes("retargeted");
  });
  return phase?.summary ?? "";
}

function edgeMissionFocus({
  ddilRepair,
  edgeRuntimeFit,
  readinessVerdict,
  resourceEnvelopeFit,
  runtimeFit
}: {
  ddilRepair: EdgeMissionMetric;
  edgeRuntimeFit: EdgeRuntimeFit;
  readinessVerdict: ReadinessVerdict;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtimeFit: JsonObject;
}): string[] {
  const focus: string[] = [];
  if (readinessVerdict.tone !== "good") focus.push(readinessVerdict.nextAction);
  const targetSelection = asRecord(runtimeFit.target_selection);
  const targetSelectionDetail = runtimeTargetSelectionDetail(targetSelection);
  if (runtimeTargetSelectionTone(targetSelection) !== "neutral") focus.push(targetSelectionDetail);
  edgeRuntimeFit.failures.slice(0, 2).forEach((failure) => focus.push(failure));
  resourceEnvelopeFit.failures.slice(0, 2).forEach((failure) => focus.push(failure));
  if (ddilRepair.tone !== "good") focus.push(`${ddilRepair.value}: ${ddilRepair.detail}`);
  const unique = [...new Set(focus.filter(Boolean))].slice(0, 4);
  return unique.length ? unique : ["Selected on-device gates are aligned"];
}
