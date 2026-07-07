import type { Device, JsonObject, RuntimeTarget, RuntimeValidation } from "../types";
import {
  compactDate,
  currentHubUrl,
  deviceId,
  localizeHubCommandPart,
  localizeHubCommandText,
  runtimeTargetId
} from "./hub-format";
import { asRecord, numberOf, stringOf, stringsOf } from "./json";
import {
  artifactLaneDetail,
  benchmarkFreshness,
  compactMetricDetail,
  formatBenchmark,
  formatMetricNumber,
  formatThroughput,
  runtimeLaneDetail,
  runtimeLaneValue,
  runtimeTargetInventoryFailures,
  runtimeValidationForModel,
  targetSupportsModel
} from "./runtime-fit";
import type {
  GateTone,
  ModelRecord,
  ReadinessVerdict,
  RuntimeRemediationCommand,
  RuntimeRemediationContext,
  RuntimeWorkbenchRow,
  RuntimeWorkbenchTraceMetric
} from "./workbench-types";

export function runtimeDecisionActionLabel(value: string): string {
  const normalized = value.replace(/-/g, "_");
  const labels: Record<string, string> = {
    apply_or_stage: "apply or stage",
    use_best_runtime: "use best runtime",
    resolve_blocking_gates: "resolve blockers",
    collect_missing_evidence: "collect evidence",
    review: "review"
  };
  return labels[normalized] ?? normalized.replace(/_/g, " ");
}

export function executionContractHeadline(
  action: string,
  decisionStatus: string,
  verdict: ReadinessVerdict
): string {
  const normalizedAction = action.replace(/-/g, "_");
  if (normalizedAction === "apply_or_stage" && verdict.tone === "good") {
    return "Selected edge runtime is ready for field apply";
  }
  if (normalizedAction === "apply_or_stage") {
    return "Selected runtime is the best measured path";
  }
  if (normalizedAction === "use_best_runtime") {
    return decisionStatus === "selected_not_eligible"
      ? "Pinned runtime cannot host this edge model"
      : "A better measured runtime is available";
  }
  if (normalizedAction === "collect_missing_evidence") {
    return "Selected edge runtime needs fresh on-device proof";
  }
  if (normalizedAction === "resolve_blocking_gates") return "Selected edge runtime is blocked";
  return verdict.headline;
}

export function executionContractTone({
  action,
  decisionStatus,
  productionAdmission,
  readinessVerdict
}: {
  action: string;
  decisionStatus: string;
  productionAdmission: JsonObject;
  readinessVerdict: ReadinessVerdict;
}): GateTone {
  const normalizedAction = action.replace(/-/g, "_");
  if (productionAdmission.apply_allowed === false || decisionStatus === "selected_not_eligible") return "bad";
  if (normalizedAction === "resolve_blocking_gates") return "bad";
  if (normalizedAction === "use_best_runtime" || normalizedAction === "collect_missing_evidence") return "warn";
  if (normalizedAction === "apply_or_stage" && productionAdmission.apply_allowed === true) return "good";
  return readinessVerdict.tone;
}

export function runtimeDecisionCandidates(
  runtimeDecision: JsonObject,
  runtimeFit: JsonObject,
  selectedRuntimeTargetId: string,
  bestRuntimeTargetId: string
): JsonObject[] {
  const direct = Array.isArray(runtimeDecision.top_candidates)
    ? runtimeDecision.top_candidates.map(asRecord)
    : [];
  if (direct.length) return direct.filter((candidate) => candidateRuntimeId(candidate) !== "runtime target");

  const targetSelection = asRecord(runtimeFit.target_selection);
  const alternatives = Array.isArray(targetSelection.alternatives)
    ? targetSelection.alternatives.map(asRecord)
    : [];
  if (alternatives.length) {
    return alternatives.filter((candidate) => candidateRuntimeId(candidate) !== "runtime target");
  }

  const score = numberOf(runtimeFit.score);
  if (!selectedRuntimeTargetId || selectedRuntimeTargetId.includes("missing")) return [];
  return [
    {
      rank: 1,
      runtime_target_id: selectedRuntimeTargetId,
      score,
      tier: stringOf(runtimeFit.tier, "selected"),
      runtime_lane: runtimeFit.runtime_lane,
      blocked: false,
      best: selectedRuntimeTargetId === bestRuntimeTargetId
    }
  ];
}

export function runtimeTargetAssessments(
  runtimeDecision: JsonObject,
  runtimeFit: JsonObject,
  fallbackCandidates: JsonObject[]
): JsonObject[] {
  const direct = Array.isArray(runtimeDecision.target_assessments)
    ? runtimeDecision.target_assessments.map(asRecord)
    : [];
  if (direct.length) return direct.filter((assessment) => candidateRuntimeId(assessment) !== "runtime target");

  const targetSelection = asRecord(runtimeFit.target_selection);
  const fromSelection = Array.isArray(targetSelection.target_assessments)
    ? targetSelection.target_assessments.map(asRecord)
    : [];
  if (fromSelection.length) {
    return fromSelection.filter((assessment) => candidateRuntimeId(assessment) !== "runtime target");
  }

  return fallbackCandidates;
}

export function targetRuntimeCoverageSummary(
  assessments: JsonObject[]
): { value: string; detail: string; tone: GateTone } {
  if (!assessments.length) {
    return {
      value: "pending",
      detail: "runtime target coverage pending",
      tone: "neutral"
    };
  }
  const blocked = assessments.filter(targetAssessmentBlocked).length;
  const eligible = assessments.filter((assessment) => !targetAssessmentBlocked(assessment)).length;
  return {
    value: `${eligible}/${assessments.length} eligible`,
    detail: `${eligible} eligible / ${blocked} blocked`,
    tone: blocked ? (eligible ? "warn" : "bad") : "good"
  };
}

export function operatorRuntimeLaneItems(
  assessments: JsonObject[],
  selectedRuntimeTargetId: string,
  bestRuntimeTargetId: string
): { detail: string; id: string; status: string; tone: GateTone }[] {
  return assessments
    .map((assessment, index) => ({ assessment, index }))
    .sort((left, right) => {
      const leftId = candidateRuntimeId(left.assessment);
      const rightId = candidateRuntimeId(right.assessment);
      const leftSelected = left.assessment.selected === true || leftId === selectedRuntimeTargetId;
      const rightSelected = right.assessment.selected === true || rightId === selectedRuntimeTargetId;
      if (leftSelected !== rightSelected) return leftSelected ? -1 : 1;
      const leftBest = left.assessment.best === true || leftId === bestRuntimeTargetId;
      const rightBest = right.assessment.best === true || rightId === bestRuntimeTargetId;
      if (leftBest !== rightBest) return leftBest ? -1 : 1;
      const leftBlocked = targetAssessmentBlocked(left.assessment);
      const rightBlocked = targetAssessmentBlocked(right.assessment);
      if (leftBlocked !== rightBlocked) return leftBlocked ? 1 : -1;
      const leftRank = numberOf(left.assessment.rank) ?? Number.MAX_SAFE_INTEGER;
      const rightRank = numberOf(right.assessment.rank) ?? Number.MAX_SAFE_INTEGER;
      if (leftRank !== rightRank) return leftRank - rightRank;
      return left.index - right.index;
    })
    .slice(0, 4)
    .map(({ assessment }) => {
      const id = candidateRuntimeId(assessment);
      const lane = runtimeLaneValue(asRecord(assessment.runtime_lane));
      const score = numberOf(assessment.score);
      const selected = assessment.selected === true || id === selectedRuntimeTargetId;
      const best = assessment.best === true || id === bestRuntimeTargetId;
      const blocked = targetAssessmentBlocked(assessment);
      const remediation = asRecord(assessment.remediation);
      const status = selected
        ? best
          ? "selected best"
          : "selected"
        : best
          ? "best alternate"
          : blocked
            ? "blocked"
            : "eligible";
      const detailSource = Object.keys(remediation).length
        ? targetAssessmentRemediationDetail(remediation)
        : targetAssessmentDetail(assessment);
      const detailParts = [lane, score !== undefined ? `${score}/100` : "", detailSource].filter(Boolean);
      return {
        detail: detailParts.join(" / "),
        id,
        status,
        tone: targetAssessmentTone(assessment)
      };
    });
}

export function targetAssessmentBlocked(assessment: JsonObject): boolean {
  const status = stringOf(assessment.status, "").toLowerCase();
  return assessment.blocked === true || status === "blocked";
}

export function targetAssessmentTone(assessment: JsonObject): GateTone {
  const status = stringOf(assessment.status, "");
  if (targetAssessmentBlocked(assessment)) return "bad";
  const penalties = stringsOf(assessment.penalties);
  if (penalties.length) return "warn";
  if (assessment.selected === true || assessment.best === true) return "good";
  return "neutral";
}

export function runtimeWorkbenchRows({
  assessments,
  bestRuntimeTargetId,
  device,
  model,
  runtimeFit,
  runtimeWorkbench,
  runtimeTargets,
  runtimeValidations,
  selectedRuntimeTargetId
}: {
  assessments: JsonObject[];
  bestRuntimeTargetId: string;
  device: Device | undefined;
  model: ModelRecord | undefined;
  runtimeFit: JsonObject;
  runtimeWorkbench: JsonObject;
  runtimeTargets: RuntimeTarget[];
  runtimeValidations: RuntimeValidation[];
  selectedRuntimeTargetId: string;
}): RuntimeWorkbenchRow[] {
  const contractRows = runtimeWorkbenchContractRows(runtimeWorkbench, runtimeTargets);
  if (contractRows.length) return contractRows;

  const selectedScore = numberOf(runtimeFit.score);
  const assessmentByTarget = new Map(assessments.map((assessment) => [candidateRuntimeId(assessment), assessment]));
  const initialRows = runtimeTargets.map((target) => {
    const targetId = runtimeTargetId(target);
    const assessment = assessmentByTarget.get(targetId);
    const selected = targetId === selectedRuntimeTargetId;
    const validation = model ? runtimeValidationForModel(model, target, runtimeValidations) : undefined;
    const compatible = model ? targetSupportsModel(target, model) : false;
    const inventoryFailures = device ? runtimeTargetInventoryFailures(target, device) : ["edge inventory missing"];
    const benchmark = runtimeWorkbenchBenchmarkLabel(model, device, targetId);
    const remediation = asRecord(assessment?.remediation);
    const actionKind = stringOf(remediation.action, "");
    const actionLabel = stringOf(remediation.label, actionKind.replace(/_/g, " "));
    const assessedScore = numberOf(assessment?.score);
    const fallbackScore = runtimeWorkbenchFallbackScore({
      benchmark,
      compatible,
      inventoryFailures,
      selected,
      selectedScore,
      validation
    });
    const score = assessedScore ?? fallbackScore;
    const tone = assessment
      ? targetAssessmentTone(assessment)
      : runtimeWorkbenchFallbackTone({
          compatible,
          inventoryFailures,
          validation
        });
    const status = runtimeWorkbenchStatus({
      assessment,
      best: targetId === bestRuntimeTargetId,
      compatible,
      selected,
      validation
    });
    return {
      actionKind,
      actionLabel,
      actionRequiresEdge: remediation.requires_edge_execution === true,
      benchmark,
      best: targetId === bestRuntimeTargetId,
      capabilitySha256: stringOf(asRecord(assessment?.runtime_capability_lock).capability_sha256, ""),
      compatible,
      detail: runtimeWorkbenchDetail({
        assessment,
        compatible,
        inventoryFailures,
        model,
        target,
        validation
      }),
      inventory: inventoryFailures.length ? compactMetricDetail(inventoryFailures[0]) : "inventory match",
      lane: runtimeLaneValue(asRecord(assessment?.runtime_lane).lane_id ? asRecord(assessment?.runtime_lane) : asRecord(target.runtime_lane)),
      penalties: stringsOf(assessment?.penalties),
      rank: numberOf(assessment?.rank),
      reasons: stringsOf(assessment?.reasons),
      remediation,
      score,
      selected,
      status,
      target,
      targetId,
      tone,
      traceMetrics: runtimeWorkbenchFallbackTraceMetrics({
        benchmark,
        compatible,
        inventoryFailures,
        validation
      }),
      validated: Boolean(validation)
    };
  });
  const derivedBestTargetId =
    bestRuntimeTargetId ||
    [...initialRows]
      .filter((row) => row.compatible && row.tone !== "bad")
      .sort(runtimeWorkbenchScoreSort)[0]?.targetId ||
    "";
  return initialRows
    .map((row) => ({ ...row, best: row.targetId === derivedBestTargetId }))
    .sort(runtimeWorkbenchRowSort);
}

export function runtimeWorkbenchContractRows(
  runtimeWorkbench: JsonObject,
  runtimeTargets: RuntimeTarget[]
): RuntimeWorkbenchRow[] {
  if (runtimeWorkbench.schema_version !== "temms-runtime-workbench/v1") return [];
  const targets = Array.isArray(runtimeWorkbench.targets)
    ? runtimeWorkbench.targets.map(asRecord)
    : [];
  if (!targets.length) return [];
  const targetById = new Map(runtimeTargets.map((target) => [runtimeTargetId(target), target]));
  const rows: RuntimeWorkbenchRow[] = [];
  targets.forEach((target) => {
    const targetId = stringOf(target.runtime_target_id, "");
    if (!targetId) return;
    const runtimeTarget = targetById.get(targetId) ?? { runtime_target_id: targetId };
    const proof = asRecord(target.proof);
    const status = stringOf(target.status, "unknown").replace(/_/g, " ");
    const eligible = target.eligible !== false && status !== "blocked";
    const score = numberOf(target.score);
    const benchmark = runtimeWorkbenchContractBenchmark(proof);
    const inventory = runtimeWorkbenchContractInventory(proof, target);
    const remediation = asRecord(target.remediation);
    const actionKind = stringOf(remediation.action, stringOf(asRecord(target.action).kind, ""));
    const actionLabel = stringOf(
      remediation.label,
      stringOf(asRecord(target.action).label, actionKind.replace(/_/g, " "))
    );
    rows.push({
      actionKind,
      actionLabel,
      actionRequiresEdge: remediation.requires_edge_execution === true || asRecord(target.action).requires_edge_execution === true,
      benchmark,
      best: target.best === true,
      capabilitySha256: stringOf(proof.capability_sha256, ""),
      compatible: eligible,
      detail: compactMetricDetail(stringOf(target.detail, runtimeWorkbenchContractDetail(target))),
      inventory,
      lane: runtimeLaneValue(asRecord(target.runtime_lane)),
      penalties: stringsOf(target.penalties),
      rank: numberOf(target.rank),
      reasons: stringsOf(target.reasons),
      remediation,
      score,
      selected: target.selected === true,
      status,
      target: runtimeTarget,
      targetId,
      tone: runtimeWorkbenchTargetTone(target),
      traceMetrics: runtimeWorkbenchContractTraceMetrics(target, proof),
      validated: runtimeWorkbenchTargetValidated(proof)
    });
  });
  return rows.sort(runtimeWorkbenchRowSort);
}

export function runtimeWorkbenchContractBenchmark(proof: JsonObject): string {
  const latency = numberOf(proof.latency_ms_p95);
  const throughput = numberOf(proof.throughput_ips);
  const benchmarkId = stringOf(proof.benchmark_id, "");
  const parts = [];
  if (latency !== undefined) parts.push(`${formatMetricNumber(latency)} ms p95`);
  if (throughput !== undefined) parts.push(`${formatThroughput(throughput)} ips`);
  if (parts.length) return parts.join(" / ");
  return benchmarkId ? `benchmark ${benchmarkId}` : "no benchmark";
}

export function runtimeWorkbenchContractInventory(proof: JsonObject, target: JsonObject): string {
  const telemetry = stringOf(proof.telemetry_state, stringOf(proof.telemetry_status, "")).replace(/_/g, " ");
  const capability = stringOf(proof.capability_lock_status, "");
  if (capability || telemetry) {
    return [capability ? `capability ${capability}` : "", telemetry].filter(Boolean).join(" / ");
  }
  const penalties = stringsOf(target.penalties);
  return penalties.length ? compactMetricDetail(penalties[0]) : "inventory match";
}

export function runtimeWorkbenchContractDetail(target: JsonObject): string {
  const reasons = stringsOf(target.reasons);
  if (reasons.length) return reasons[0];
  const penalties = stringsOf(target.penalties);
  if (penalties.length) return penalties[0];
  const proof = asRecord(target.proof);
  return stringOf(proof.performance_state, stringOf(proof.runtime_validation_state, "runtime target assessed"));
}

export function runtimeWorkbenchContractTraceMetrics(target: JsonObject, proof: JsonObject): RuntimeWorkbenchTraceMetric[] {
  const validationId = stringOf(proof.validation_id, "");
  const benchmarkId = stringOf(proof.benchmark_id, "");
  const capabilityDigest = stringOf(proof.capability_sha256, "");
  const metrics: RuntimeWorkbenchTraceMetric[] = [
    runtimeWorkbenchTraceMetric(
      "validation",
      validationId ? "present" : runtimeWorkbenchProofValue(proof.runtime_validation_status, proof.runtime_validation_state, "pending"),
      validationId || runtimeWorkbenchProofValue(proof.runtime_validation_state, proof.runtime_validation_status, "runtime validation not retained"),
      runtimeWorkbenchProofTone(proof.runtime_validation_status, proof.runtime_validation_state, validationId)
    ),
    runtimeWorkbenchTraceMetric(
      "benchmark",
      benchmarkId ? "present" : runtimeWorkbenchProofValue(proof.performance_status, proof.performance_state, "pending"),
      benchmarkId || runtimeWorkbenchContractBenchmark(proof),
      runtimeWorkbenchProofTone(proof.performance_status, proof.performance_state, benchmarkId)
    ),
    runtimeWorkbenchTraceMetric(
      "resources",
      runtimeWorkbenchProofValue(proof.resource_status, proof.resource_state, "pending"),
      runtimeWorkbenchProofValue(proof.resource_state, proof.resource_status, "resource envelope not retained"),
      runtimeWorkbenchProofTone(proof.resource_status, proof.resource_state)
    ),
    runtimeWorkbenchTraceMetric(
      "telemetry",
      runtimeWorkbenchProofValue(proof.telemetry_status, proof.telemetry_state, "pending"),
      runtimeWorkbenchProofValue(proof.telemetry_state, proof.telemetry_status, "heartbeat state not retained"),
      runtimeWorkbenchProofTone(proof.telemetry_status, proof.telemetry_state)
    ),
    runtimeWorkbenchTraceMetric(
      "capability",
      stringOf(proof.capability_lock_status, capabilityDigest ? "hash locked" : "pending"),
      capabilityDigest ? `sha256 ${capabilityDigest.slice(0, 12)}` : runtimeWorkbenchContractInventory(proof, target),
      runtimeWorkbenchProofTone(proof.capability_lock_status, undefined, capabilityDigest)
    )
  ];
  return metrics;
}

export function runtimeWorkbenchFallbackTraceMetrics({
  benchmark,
  compatible,
  inventoryFailures,
  validation
}: {
  benchmark: string;
  compatible: boolean;
  inventoryFailures: string[];
  validation: RuntimeValidation | undefined;
}): RuntimeWorkbenchTraceMetric[] {
  return [
    runtimeWorkbenchTraceMetric(
      "compatibility",
      compatible ? "eligible" : "blocked",
      compatible ? "model constraints match runtime target" : "model/runtime constraints do not match",
      compatible ? "good" : "bad"
    ),
    runtimeWorkbenchTraceMetric(
      "validation",
      validation ? "present" : "missing",
      validation ? runtimeWorkbenchValidationDetail(validation) : "non-dry-run runtime validation required",
      validation ? "good" : "warn"
    ),
    runtimeWorkbenchTraceMetric(
      "benchmark",
      benchmark === "no benchmark" ? "missing" : "present",
      benchmark,
      benchmark === "no benchmark" ? "warn" : "good"
    ),
    runtimeWorkbenchTraceMetric(
      "inventory",
      inventoryFailures.length ? "blocked" : "match",
      inventoryFailures.length ? compactMetricDetail(inventoryFailures[0]) : "live edge inventory matches",
      inventoryFailures.length ? "bad" : "good"
    )
  ];
}

export function runtimeWorkbenchTraceMetric(
  label: string,
  value: string,
  detail: string,
  tone: GateTone
): RuntimeWorkbenchTraceMetric {
  return {
    detail: compactMetricDetail(detail || "not retained"),
    label,
    tone,
    value: value.replace(/_/g, " ") || "pending"
  };
}

export function runtimeWorkbenchProofValue(primary: unknown, secondary: unknown, fallback: string): string {
  return stringOf(primary, stringOf(secondary, fallback)).replace(/_/g, " ");
}

export function runtimeWorkbenchProofTone(primary: unknown, secondary?: unknown, retainedEvidence?: string): GateTone {
  const value = `${stringOf(primary, "")} ${stringOf(secondary, "")}`.toLowerCase();
  if (retainedEvidence) return "good";
  if (value.includes("blocked") || value.includes("fail") || value.includes("missing")) return "bad";
  if (value.includes("attention") || value.includes("warn") || value.includes("stale") || value.includes("pending")) return "warn";
  if (value.includes("go") || value.includes("pass") || value.includes("eligible") || value.includes("locked") || value.includes("fresh")) return "good";
  return "neutral";
}

export function runtimeWorkbenchValidationDetail(validation: RuntimeValidation): string {
  const validationId = stringOf(validation.validation_id, "");
  const createdAt = compactDate(validation.created_at);
  return [validationId || "runtime validation retained", createdAt].filter(Boolean).join(" / ");
}

export function runtimeWorkbenchRowRemediationCommand(
  row: RuntimeWorkbenchRow,
  context: RuntimeRemediationContext
): RuntimeRemediationCommand | undefined {
  if (!row.actionKind) return undefined;
  return runtimeTargetAssessmentRemediationCommand(
    {
      remediation: row.remediation,
      runtime_lane: row.target.runtime_lane,
      runtime_target_id: row.targetId
    },
    context
  );
}

export function runtimeWorkbenchTargetTone(target: JsonObject): GateTone {
  const status = stringOf(target.status, "");
  if (status === "blocked" || target.eligible === false) return "bad";
  if (target.selected === true || target.best === true) return "good";
  const penalties = stringsOf(target.penalties);
  return penalties.length ? "warn" : "neutral";
}

export function runtimeWorkbenchTargetValidated(proof: JsonObject): boolean {
  const validationStatus = stringOf(proof.runtime_validation_status, "");
  const validationState = stringOf(proof.runtime_validation_state, "").toLowerCase();
  return Boolean(proof.validation_id) || validationStatus === "go" || validationState.includes("validated");
}

export function runtimeWorkbenchFallbackScore({
  benchmark,
  compatible,
  inventoryFailures,
  selected,
  selectedScore,
  validation
}: {
  benchmark: string;
  compatible: boolean;
  inventoryFailures: string[];
  selected: boolean;
  selectedScore?: number;
  validation: RuntimeValidation | undefined;
}): number | undefined {
  if (selected && selectedScore !== undefined) return selectedScore;
  if (!compatible) return 0;
  let score = 48;
  if (validation) score += 18;
  if (benchmark.startsWith("fresh")) score += 19;
  else if (benchmark !== "no benchmark") score += 8;
  if (!inventoryFailures.length) score += 15;
  return Math.min(score, 95);
}

export function runtimeWorkbenchFallbackTone({
  compatible,
  inventoryFailures,
  validation
}: {
  compatible: boolean;
  inventoryFailures: string[];
  validation: RuntimeValidation | undefined;
}): GateTone {
  if (!compatible || inventoryFailures.length) return "bad";
  return validation ? "good" : "warn";
}

export function runtimeWorkbenchStatus({
  assessment,
  best,
  compatible,
  selected,
  validation
}: {
  assessment: JsonObject | undefined;
  best: boolean;
  compatible: boolean;
  selected: boolean;
  validation: RuntimeValidation | undefined;
}): string {
  const assessedStatus = stringOf(assessment?.status, "");
  if (assessedStatus) return assessedStatus.replace(/_/g, " ");
  if (!compatible) return "blocked";
  if (selected && best) return "selected best";
  if (selected) return "selected";
  if (best) return "best alternate";
  return validation ? "eligible" : "needs proof";
}

export function runtimeWorkbenchDetail({
  assessment,
  compatible,
  inventoryFailures,
  model,
  target,
  validation
}: {
  assessment: JsonObject | undefined;
  compatible: boolean;
  inventoryFailures: string[];
  model: ModelRecord | undefined;
  target: RuntimeTarget;
  validation: RuntimeValidation | undefined;
}): string {
  if (assessment) return targetAssessmentDetail(assessment);
  if (!compatible) return "runtime target does not satisfy model constraints";
  if (inventoryFailures.length) return compactMetricDetail(inventoryFailures[0]);
  if (validation) return `${runtimeTargetId(target)} passed package validation`;
  if (model) return `${formatBenchmark(model)}; validation required for ${runtimeTargetId(target)}`;
  return "select a model to evaluate this target runtime";
}

export function runtimeWorkbenchBenchmarkLabel(
  model: ModelRecord | undefined,
  device: Device | undefined,
  targetId: string
): string {
  if (!model) return "no model";
  const targetMatches = model.benchmarkRuntimeId === targetId;
  const deviceMatches = device && model.benchmarkDeviceId === deviceId(device);
  if (!targetMatches && !deviceMatches) return "no benchmark";
  const freshness = benchmarkFreshness(model).state;
  const benchmark = formatBenchmark(model);
  if (targetMatches && deviceMatches) return `${freshness} ${benchmark}`;
  if (targetMatches) return `${freshness} ${benchmark} on another edge`;
  return `${freshness} ${benchmark} on another runtime`;
}

export function runtimeWorkbenchRowSort(left: RuntimeWorkbenchRow, right: RuntimeWorkbenchRow): number {
  if (left.selected !== right.selected) return left.selected ? -1 : 1;
  if (left.best !== right.best) return left.best ? -1 : 1;
  return runtimeWorkbenchScoreSort(left, right);
}

export function runtimeWorkbenchScoreSort(left: RuntimeWorkbenchRow, right: RuntimeWorkbenchRow): number {
  const leftScore = left.score ?? -1;
  const rightScore = right.score ?? -1;
  if (leftScore !== rightScore) return rightScore - leftScore;
  if (left.compatible !== right.compatible) return left.compatible ? -1 : 1;
  return left.targetId.localeCompare(right.targetId);
}

export function targetAssessmentDetail(assessment: JsonObject): string {
  const penalties = stringsOf(assessment.penalties);
  if (penalties.length) return compactMetricDetail(penalties[0]);
  const reasons = stringsOf(assessment.reasons);
  if (reasons.length) return compactMetricDetail(reasons[0]);
  const detail = stringOf(assessment.detail, "");
  if (detail) return compactMetricDetail(detail);
  const artifact = asRecord(assessment.artifact_lane);
  if (Object.keys(artifact).length) return artifactLaneDetail(artifact);
  return runtimeLaneDetail(asRecord(assessment.runtime_lane));
}

export function targetAssessmentRemediationDetail(remediation: JsonObject): string {
  const label = stringOf(remediation.label, "");
  const detail = compactMetricDetail(stringOf(remediation.detail, ""));
  if (label && detail) return `${label} - ${detail}`;
  return label || detail || "Review this runtime target";
}

export function runtimeTargetAssessmentRemediationCommand(
  assessment: JsonObject,
  context: RuntimeRemediationContext
): RuntimeRemediationCommand | undefined {
  const remediation = asRecord(assessment.remediation);
  const action = stringOf(remediation.action, "");
  if (!action) return undefined;

  const refs = asRecord(remediation.refs);
  const runtimeTargetIdValue = stringOf(refs.runtime_target_id, candidateRuntimeId(assessment));
  if (!runtimeTargetIdValue || runtimeTargetIdValue === "runtime target") return undefined;

  const actionLabel = stringOf(remediation.label, action.replace(/_/g, " "));
  const contractCommand = runtimeTargetContractRemediationCommand(
    remediation,
    runtimeTargetIdValue,
    action,
    actionLabel
  );
  if (contractCommand) return contractCommand;

  const packageIdValue = context.packageId || stringOf(refs.package_id, "<package-id>");
  const modelIdValue = context.modelId || stringOf(refs.model_id, "<model-id>");
  const deviceIdValue = context.deviceId || stringOf(refs.device_id, "<device-id>");
  const slotValue = context.slot || stringOf(refs.slot, "vision");
  const hubUrl = currentHubUrl();

  if (action === "record_benchmark") {
    return {
      action,
      label: `${runtimeTargetIdValue} benchmark command`,
      edgeRun: true,
      note: "Run on the selected edge after the model package is cached.",
      command: formatProofCommand([
        "temms",
        "benchmark",
        modelIdValue || "<model-id>",
        "--slot",
        slotValue,
        "--samples",
        "10",
        "--warmup",
        "2",
        "--hub-url",
        hubUrl,
        "--device-id",
        deviceIdValue || "<device-id>",
        "--package-id",
        packageIdValue || "<package-id>",
        "--runtime-target-id",
        runtimeTargetIdValue,
        "--actor",
        "edge-agent"
      ])
    };
  }

  if (action === "validate_runtime") {
    return {
      action,
      label: `${runtimeTargetIdValue} validation command`,
      edgeRun: false,
      note: "Replace the package path with the signed TEMMS package artifact.",
      command: formatProofCommand([
        "uv",
        "run",
        "temms",
        "hub",
        "validate-runtime",
        "<package-path>",
        "--hub-url",
        hubUrl,
        "--package-id",
        packageIdValue || "<package-id>",
        "--runtime-target-id",
        runtimeTargetIdValue,
        "--actor",
        "operator:runtime-remediation",
        "--require-signature"
      ])
    };
  }

  if (action === "refresh_edge_inventory") {
    return {
      action,
      label: `${deviceIdValue || "edge"} heartbeat command`,
      edgeRun: true,
      note: "Run on the edge node to refresh runtime/provider inventory and heartbeat freshness.",
      command: formatProofCommand([
        `TEMMS_HUB_URL=${hubUrl}`,
        `TEMMS_DEVICE_ID=${deviceIdValue || "<device-id>"}`,
        "TEMMS_EDGE_HEARTBEAT_INTERVAL_S=10",
        "temms",
        "daemon",
        "start",
        "--foreground"
      ])
    };
  }

  if (action === "package_runtime_artifact") {
    const lane = asRecord(assessment.runtime_lane);
    const providers = stringsOf(lane.providers);
    const accelerators = stringsOf(lane.accelerators);
    const engine = stringOf(lane.execution_engine, "");
    const commandParts = [
      "uv",
      "run",
      "temms",
      "hub",
      "package-from-mlflow",
      "<model-uri>",
      "--hub-url",
      hubUrl,
      "--slot",
      slotValue,
      "--model-artifact",
      "<runtime-native-artifact-path>",
      "--actor",
      "operator:runtime-remediation"
    ];
    if (engine) commandParts.push("--runtime", engine);
    providers.forEach((provider) => commandParts.push("--provider", provider));
    accelerators.forEach((accelerator) => commandParts.push("--accelerator", accelerator));
    return {
      action,
      label: `${runtimeTargetIdValue} packaging command`,
      edgeRun: false,
      note: "Package a runtime-native artifact, then re-run validation and proof.",
      command: formatProofCommand(commandParts)
    };
  }

  if (["select_matching_edge_class", "resolve_runtime_capability", "free_edge_resources", "resolve_target_blocker"].includes(action)) {
    return {
      action,
      label: `${runtimeTargetIdValue} compatibility inspection`,
      edgeRun: false,
      note: `${actionLabel} with live inventory and model/runtime constraints.`,
      command: formatProofCommand([
        "uv",
        "run",
        "temms",
        "hub",
        "compatibility-matrix",
        "--hub-url",
        hubUrl,
        "--device-id",
        deviceIdValue || "<device-id>",
        "--package-id",
        packageIdValue || "<package-id>",
        "--model-id",
        modelIdValue || "<model-id>",
        "--runtime-target-id",
        runtimeTargetIdValue,
        "--include-device-inventory",
        "--json"
      ])
    };
  }

  return {
    action,
    label: `${runtimeTargetIdValue} proof check`,
    edgeRun: false,
    note: `${actionLabel} against the signed edge-runtime gate.`,
    command: formatProofCommand([
      "uv",
      "run",
      "temms",
      "hub",
      "edge-runtime-mission",
      "--hub-url",
      hubUrl,
      "--package-id",
      packageIdValue || "<package-id>",
      "--model-id",
      modelIdValue || "<model-id>",
      "--device-id",
      deviceIdValue || "<device-id>",
      "--runtime-target-id",
      runtimeTargetIdValue,
      "--slot",
      slotValue,
      "--require-go",
      "--require-best-runtime",
      "--require-capability-lock",
      "--min-runtime-fit",
      "95",
      "--json"
    ])
  };
}

export function runtimeTargetContractRemediationCommand(
  remediation: JsonObject,
  runtimeTargetIdValue: string,
  action: string,
  actionLabel: string
): RuntimeRemediationCommand | undefined {
  const commandRecord = asRecord(remediation.command);
  const edgeCommandText = stringOf(
    remediation.edge_command_text,
    stringOf(commandRecord.edge_command_text, "")
  );
  if (edgeCommandText) {
    return {
      action,
      label: `${runtimeTargetIdValue} edge command`,
      edgeRun: true,
      note: stringOf(
        remediation.edge_command_note,
        stringOf(commandRecord.edge_command_note, "Run this command on the selected edge node.")
      ),
      command: localizeHubCommandText(edgeCommandText)
    };
  }

  const operatorCommandText = stringOf(
    remediation.operator_command_text,
    stringOf(commandRecord.operator_command_text, "")
  );
  if (operatorCommandText) {
    return {
      action,
      label: `${runtimeTargetIdValue} operator command`,
      edgeRun: remediation.requires_edge_execution === true,
      note: stringOf(
        remediation.operator_command_note,
        stringOf(commandRecord.operator_command_note, `${actionLabel} against the current edge-runtime contract.`)
      ),
      command: localizeHubCommandText(operatorCommandText)
    };
  }

  const edgeCommand = stringsOf(remediation.edge_command).length
    ? stringsOf(remediation.edge_command)
    : stringsOf(commandRecord.edge_command);
  if (edgeCommand.length) {
    return {
      action,
      label: `${runtimeTargetIdValue} edge command`,
      edgeRun: true,
      note: stringOf(
        remediation.edge_command_note,
        stringOf(commandRecord.edge_command_note, "Run this command on the selected edge node.")
      ),
      command: formatProofCommand(edgeCommand.map(localizeHubCommandPart))
    };
  }

  const operatorCommand = stringsOf(remediation.operator_command).length
    ? stringsOf(remediation.operator_command)
    : stringsOf(commandRecord.operator_command);
  if (operatorCommand.length) {
    return {
      action,
      label: `${runtimeTargetIdValue} operator command`,
      edgeRun: remediation.requires_edge_execution === true,
      note: stringOf(
        remediation.operator_command_note,
        stringOf(commandRecord.operator_command_note, `${actionLabel} against the current edge-runtime contract.`)
      ),
      command: formatProofCommand(operatorCommand.map(localizeHubCommandPart))
    };
  }

  return undefined;
}

export function runtimeTargetComponentProofs(
  assessment: JsonObject
): { key: string; label: string; state: string; score: string; tone: GateTone }[] {
  const components = asRecord(assessment.component_states);
  const specs: { key: string; label: string }[] = [
    { key: "compatibility", label: "compat" },
    { key: "runtime_validation", label: "valid" },
    { key: "performance", label: "perf" },
    { key: "resource", label: "res" },
    { key: "telemetry", label: "telemetry" }
  ];
  return specs
    .map(({ key, label }) => {
      const component = asRecord(components[key]);
      const state = componentProofState(component);
      if (!state) return undefined;
      const score = componentProofScore(component);
      return {
        key,
        label,
        state,
        score,
        tone: componentProofTone(component, state)
      };
    })
    .filter((value): value is { key: string; label: string; state: string; score: string; tone: GateTone } => Boolean(value));
}

export function componentProofState(component: JsonObject): string {
  return stringOf(component.state, stringOf(component.status, "")).replace(/_/g, " ");
}

export function componentProofScore(component: JsonObject): string {
  const score = numberOf(component.score);
  const maxScore = numberOf(component.max_score);
  if (score === undefined) return "";
  return maxScore !== undefined ? `${score}/${maxScore}` : `${score}`;
}

export function componentProofTone(component: JsonObject, state: string): GateTone {
  const status = stringOf(component.status, "").toLowerCase();
  const normalized = state.toLowerCase();
  if (status === "blocked" || normalized.includes("blocked") || normalized.includes("miss")) return "bad";
  if (
    status === "attention" ||
    normalized.includes("missing") ||
    normalized.includes("stale") ||
    normalized.includes("unknown")
  ) {
    return "warn";
  }
  if (
    status === "go" ||
    normalized.includes("compatible") ||
    normalized.includes("validated") ||
    normalized.includes("met") ||
    normalized.includes("fresh")
  ) {
    return "good";
  }
  return "neutral";
}

export function runtimeDecisionGates(value: unknown): JsonObject[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(asRecord)
    .filter((gate) => stringOf(gate.gate_id, "") || stringOf(gate.label, ""));
}

export function candidateRuntimeId(candidate: JsonObject): string {
  return stringOf(candidate.runtime_target_id, "runtime target");
}

export function runtimeCandidateTone(
  candidate: JsonObject,
  candidateId: string,
  selectedRuntimeTargetId: string,
  bestRuntimeTargetId: string
): GateTone {
  if (candidate.blocked === true) return "bad";
  if (candidateId === bestRuntimeTargetId) return "good";
  if (candidateId === selectedRuntimeTargetId && bestRuntimeTargetId !== selectedRuntimeTargetId) return "warn";
  return "neutral";
}

export function formatProofCommand(parts: string[]): string {
  if (parts.length <= 5) return parts.map(shellArg).join(" ");
  const firstLine = parts.slice(0, 5).map(shellArg).join(" ");
  const lines = [firstLine];
  for (let index = 5; index < parts.length;) {
    const token = parts[index];
    const flag = shellArg(token);
    const value = parts[index + 1];
    if (!token.startsWith("--")) {
      lines.push(`  ${flag}`);
      index += 1;
    } else if (value === undefined || value.startsWith("--")) {
      lines.push(`  ${flag}`);
      index += 1;
    } else {
      lines.push(`  ${flag} ${shellArg(value)}`);
      index += 2;
    }
  }
  return lines.join(" \\\n");
}

export function shellArg(value: string): string {
  if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(value)) return value;
  return `"${value.replace(/(["\\$`])/g, "\\$1")}"`;
}
