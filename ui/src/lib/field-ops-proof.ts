import type { EvidenceSummary, MissionReplay } from "../types";
import { asRecord, booleanOf, numberOf, stringOf } from "./json";
import type { GateTone, RuntimeRepairProof } from "./workbench-types";

export function activeSlotForMission(
  activeSlots: unknown,
  missionSlot: string
): Record<string, unknown> | undefined {
  if (!Array.isArray(activeSlots)) return undefined;
  const slots = activeSlots.map(asRecord);
  const slotName = missionSlot || "vision";
  return slots.find((slot) => stringOf(slot.slot, "") === slotName) ?? slots[0];
}

export function prioritizedEvidenceEvents(
  timeline: unknown,
  activeModelId: string
): Record<string, unknown>[] {
  if (!Array.isArray(timeline)) return [];
  const events = timeline.map(asRecord);
  let activeRuntimeFitIndex = events.findIndex((event) => event.active_runtime_proof === true);
  if (activeRuntimeFitIndex < 0 && activeModelId) {
    activeRuntimeFitIndex = events.findIndex((event) => {
      const kind = stringOf(event.kind, "");
      const summary = stringOf(event.summary, "");
      return kind === "runtime_fit" && summary.includes(activeModelId);
    });
  }

  if (activeRuntimeFitIndex < 0) return events.slice(0, 4);

  const activeRuntimeFit = {
    ...events[activeRuntimeFitIndex],
    kind: "active_runtime_fit"
  };
  const remaining = events.filter((_, index) => index !== activeRuntimeFitIndex);
  return [activeRuntimeFit, ...remaining].slice(0, 4);
}

export function latestRuntimeRepairProofFor({
  evidenceSummary,
  missionReplay,
  pendingOperationLedger
}: {
  evidenceSummary: EvidenceSummary | undefined;
  missionReplay: MissionReplay | undefined;
  pendingOperationLedger: Record<string, unknown>[];
}): RuntimeRepairProof | undefined {
  const pendingProof = firstRuntimeRepairProof(
    pendingOperationLedger,
    "pending",
    (proof) => proof.status === "proved"
  );
  if (pendingProof) return pendingProof;

  const pendingCandidate = firstRuntimeRepairProof(
    pendingOperationLedger,
    "pending",
    (proof) => proof.status === "repair_available"
  );
  if (pendingCandidate) return pendingCandidate;

  const summary = asRecord(evidenceSummary);
  const decisions = Array.isArray(summary.decisions) ? summary.decisions.map(asRecord) : [];
  const replayedProof = firstRuntimeRepairProof(decisions, "replayed", (proof) => proof.status === "proved");
  if (replayedProof) return replayedProof;

  return runtimeRepairProofFromMissionReplay(missionReplay);
}

function firstRuntimeRepairProof(
  records: Record<string, unknown>[],
  source: RuntimeRepairProof["source"],
  predicate: (proof: RuntimeRepairProof) => boolean
): RuntimeRepairProof | undefined {
  for (const record of records) {
    const proof = runtimeRepairProofFromRecord(record, source);
    if (proof && predicate(proof)) return proof;
  }
  return undefined;
}

function runtimeRepairProofFromRecord(
  record: Record<string, unknown>,
  source: RuntimeRepairProof["source"]
): RuntimeRepairProof | undefined {
  const remediationTarget = stringOf(record.runtime_remediation_runtime_target_id, "");
  const retargetedFrom = stringOf(record.runtime_retargeted_from, "");
  const retargetedTo = stringOf(record.runtime_retargeted_to, "");
  const workbenchPrevious = stringOf(
    record.runtime_retarget_workbench_previous_selected_runtime_target_id,
    ""
  );
  const workbenchSelected = stringOf(
    record.runtime_retarget_workbench_selected_runtime_target_id,
    ""
  );
  const workbenchBest =
    stringOf(record.runtime_retarget_workbench_best_runtime_target_id, "") ||
    stringOf(record.runtime_workbench_best_runtime_target_id, "");
  const previousRuntime =
    workbenchPrevious ||
    retargetedFrom ||
    stringOf(record.runtime_remediation_previous_runtime_target_id, "") ||
    (remediationTarget ? stringOf(record.runtime_workbench_selected_runtime_target_id, "") : "") ||
    stringOf(record.runtime_target_id, "");
  const selectedRuntime =
    workbenchSelected ||
    retargetedTo ||
    remediationTarget ||
    workbenchBest;
  const bestRuntime =
    workbenchBest ||
    stringOf(record.best_runtime_target_id, "") ||
    remediationTarget ||
    selectedRuntime;
  const proofStatus =
    stringOf(record.runtime_retarget_proof_status, "") ||
    stringOf(record.runtime_retarget_replay_proof_status, "");
  const workbenchSchema =
    stringOf(record.runtime_retarget_workbench_schema_version, "") ||
    stringOf(record.runtime_workbench_schema_version, "");
  const hasRetargetProof =
    record.runtime_retargeted === true ||
    Boolean(proofStatus || workbenchSchema || stringOf(record.runtime_retarget_capability_sha256, ""));
  const hasRepairCandidate = Boolean(remediationTarget && remediationTarget !== previousRuntime);
  if ((!hasRetargetProof && !hasRepairCandidate) || (!previousRuntime && !selectedRuntime && !bestRuntime)) {
    return undefined;
  }

  const status: RuntimeRepairProof["status"] = hasRetargetProof ? "proved" : "repair_available";
  const selectedIsBest =
    booleanOf(record.runtime_retarget_workbench_selected_is_best) ??
    booleanOf(record.runtime_workbench_selected_is_best);
  const runtimeFitScore =
    numberOf(record.runtime_retarget_runtime_fit_score) ??
    numberOf(record.runtime_fit_score);
  const targetSelectionStatus =
    stringOf(record.runtime_retarget_workbench_target_selection_status, "") ||
    stringOf(record.runtime_workbench_target_selection_status, "");
  const capabilityLockStatus = stringOf(record.runtime_retarget_capability_lock_status, "");
  const capabilitySha256 = stringOf(record.runtime_retarget_capability_sha256, "");
  const validationId = stringOf(record.runtime_retarget_validation_id, "");
  const benchmarkId = stringOf(record.runtime_retarget_benchmark_id, "");
  const tone = runtimeRepairTone(status, proofStatus, selectedIsBest);
  const detail = runtimeRepairDetail({
    benchmarkId,
    bestRuntime,
    capabilityLockStatus,
    previousRuntime,
    runtimeFitScore,
    selectedRuntime,
    status,
    validationId
  });

  return {
    actor: stringOf(record.runtime_retargeted_by, "") || stringOf(record.actor, ""),
    benchmarkId,
    bestRuntime,
    blockedTargetCount:
      numberOf(record.runtime_retarget_workbench_blocked_target_count) ??
      numberOf(record.runtime_workbench_blocked_target_count),
    capabilityLockStatus,
    capabilitySha256,
    detail,
    eligibleTargetCount:
      numberOf(record.runtime_retarget_workbench_eligible_target_count) ??
      numberOf(record.runtime_workbench_eligible_target_count),
    headline: status === "proved" ? "Retarget proof retained" : "Best runtime repair available",
    occurredAt: stringOf(record.runtime_retargeted_at, "") || stringOf(record.recorded_at, ""),
    operation: status === "repair_available" ? record : undefined,
    previousRuntime,
    proofStatus,
    reason: stringOf(record.runtime_retarget_reason, "") || stringOf(record.replay_reason, ""),
    runtimeFitScore,
    selectedIsBest,
    selectedRuntime,
    source,
    status,
    targetCount:
      numberOf(record.runtime_retarget_workbench_target_count) ??
      numberOf(record.runtime_workbench_target_count),
    targetSelectionStatus,
    tone,
    validationId,
    workbenchSchema
  };
}

function runtimeRepairProofFromMissionReplay(missionReplay: MissionReplay | undefined): RuntimeRepairProof | undefined {
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
  if (!event) return undefined;

  const detail = stringOf(event.detail, "");
  const match = detail.match(/^retargeted\s+(.+?)\s+->\s+(.+)$/);
  const previousRuntime = match?.[1] ?? "";
  const selectedRuntime = match?.[2] ?? "";
  return {
    actor: "",
    benchmarkId: "",
    bestRuntime: selectedRuntime,
    capabilityLockStatus: "",
    capabilitySha256: "",
    detail: detail || stringOf(event.summary, "retargeted DDIL replay"),
    headline: "Replay retained retarget proof",
    occurredAt: stringOf(event.timestamp, ""),
    previousRuntime,
    proofStatus: "proved",
    reason: stringOf(event.summary, ""),
    runtimeFitScore: undefined,
    selectedIsBest: undefined,
    selectedRuntime,
    source: "mission",
    status: "proved",
    targetSelectionStatus: "",
    tone: "good",
    validationId: "",
    workbenchSchema: ""
  };
}

function runtimeRepairTone(
  status: RuntimeRepairProof["status"],
  proofStatus: string,
  selectedIsBest?: boolean
): GateTone {
  const normalized = proofStatus.toLowerCase();
  if (normalized.includes("stale") || normalized.includes("blocked") || normalized.includes("failed")) return "bad";
  if (status === "repair_available") return "warn";
  if (selectedIsBest === false) return "warn";
  return "good";
}

function runtimeRepairDetail({
  benchmarkId,
  bestRuntime,
  capabilityLockStatus,
  previousRuntime,
  runtimeFitScore,
  selectedRuntime,
  status,
  validationId
}: {
  benchmarkId: string;
  bestRuntime: string;
  capabilityLockStatus: string;
  previousRuntime: string;
  runtimeFitScore?: number;
  selectedRuntime: string;
  status: RuntimeRepairProof["status"];
  validationId: string;
}): string {
  if (status === "repair_available") {
    return `${previousRuntime || "queued runtime"} can be retargeted to ${bestRuntime || selectedRuntime || "the measured best runtime"}.`;
  }
  const evidence = [];
  if (runtimeFitScore !== undefined) evidence.push(`fit ${runtimeFitScore}/100`);
  if (capabilityLockStatus) evidence.push(`capability ${capabilityLockStatus.replace(/_/g, " ")}`);
  if (validationId) evidence.push("validation");
  if (benchmarkId) evidence.push("benchmark");
  return `${previousRuntime || "queued runtime"} -> ${selectedRuntime || bestRuntime || "proved runtime"}${evidence.length ? ` with ${evidence.join(", ")}` : ""}.`;
}
