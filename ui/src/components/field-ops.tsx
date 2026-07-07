import { Clipboard, FileCheck2, GitBranch } from "lucide-react";
import { compactDate, localizeHubCommandText } from "../lib/hub-format";
import { numberOf, stringOf, stringsOf } from "../lib/json";
import { compactMetricDetail, formatAge } from "../lib/runtime-fit";
import type { GateTone, RuntimeRepairProof } from "../lib/workbench-types";
import { Badge } from "./ui";

export function EvidenceEventRow({ event }: { event: Record<string, unknown> }): JSX.Element {
  const kind = stringOf(event.kind, "event");
  const activeProof = event.active_runtime_proof === true || kind === "active_runtime_fit";
  return (
    <div className={activeProof ? "evidence-event-row evidence-event-row-active" : "evidence-event-row"}>
      <span>{activeProof ? "active runtime proof" : kind}</span>
      <strong>{compactMetricDetail(stringOf(event.summary, "mission event"))}</strong>
      <small>{compactDate(stringOf(event.timestamp, ""))}</small>
    </div>
  );
}

export function RuntimeRepairProofPanel({
  compact = false,
  proof,
  onRetargetRuntime
}: {
  compact?: boolean;
  proof: RuntimeRepairProof;
  onRetargetRuntime?: (operation: Record<string, unknown>) => void;
}): JSX.Element {
  const canRetarget = proof.status === "repair_available" && proof.operation && onRetargetRuntime;
  const capabilityDigest = proof.capabilitySha256 ? proof.capabilitySha256.slice(0, 12) : "";
  const sourceDetail = [
    proof.actor ? `actor ${proof.actor}` : "",
    proof.occurredAt ? compactDate(proof.occurredAt) : "",
    proof.reason
  ].filter(Boolean).join(" / ");
  const coverageDetail = runtimeRepairCoverageDetail(proof);
  const proofLabel = proof.proofStatus || (proof.status === "proved" ? "proved" : "repair available");

  return (
    <div
      className={`runtime-repair-proof runtime-repair-proof-${proof.tone}${compact ? " runtime-repair-proof-compact" : ""}`}
      data-testid="runtime-repair-proof"
    >
      <div className="runtime-repair-proof-header">
        <div>
          <span>{compact ? "DDIL repair evidence" : "DDIL runtime repair proof"}</span>
          <strong>{proof.headline}</strong>
          <small>{proof.detail}</small>
        </div>
        <Badge value={proofLabel.replace(/_/g, " ")} />
      </div>

      <div className="runtime-repair-proof-path" aria-label="Runtime repair path">
        <div>
          <span>Queued runtime</span>
          <strong>{proof.previousRuntime || "unknown"}</strong>
        </div>
        <span className="runtime-repair-proof-arrow">-&gt;</span>
        <div>
          <span>Proved runtime</span>
          <strong>{proof.selectedRuntime || proof.bestRuntime || "pending"}</strong>
        </div>
        <div>
          <span>Best measured</span>
          <strong>{proof.bestRuntime || proof.selectedRuntime || "pending"}</strong>
        </div>
      </div>

      <div className="runtime-repair-proof-grid">
        <RuntimeRepairMetric
          detail={proof.targetSelectionStatus || (proof.selectedIsBest ? "selected target is best" : "target selection retained")}
          label="Runtime fit"
          tone={proof.selectedIsBest === false ? "warn" : proof.tone}
          value={proof.runtimeFitScore !== undefined ? `${proof.runtimeFitScore}/100` : proof.selectedIsBest ? "best" : "pending"}
        />
        <RuntimeRepairMetric
          detail={capabilityDigest ? `sha256 ${capabilityDigest}` : "capability digest not retained"}
          label="Capability lock"
          tone={proof.capabilityLockStatus === "locked" ? "good" : proof.status === "proved" ? "warn" : "neutral"}
          value={proof.capabilityLockStatus || "not retained"}
        />
        <RuntimeRepairMetric
          detail={proof.validationId || "runtime validation id not retained"}
          label="Validation"
          tone={proof.validationId ? "good" : proof.status === "proved" ? "warn" : "neutral"}
          value={proof.validationId ? "present" : "missing"}
        />
        <RuntimeRepairMetric
          detail={proof.benchmarkId || "benchmark id not retained"}
          label="Benchmark"
          tone={proof.benchmarkId ? "good" : proof.status === "proved" ? "warn" : "neutral"}
          value={proof.benchmarkId ? "present" : "missing"}
        />
        <RuntimeRepairMetric
          detail={coverageDetail}
          label="Coverage"
          tone={proof.blockedTargetCount ? "warn" : "neutral"}
          value={runtimeRepairCoverageValue(proof)}
        />
        <RuntimeRepairMetric
          detail={proof.workbenchSchema || sourceDetail || proof.source}
          label="Audit source"
          tone={proof.workbenchSchema ? "good" : "neutral"}
          value={proof.source}
        />
      </div>

      {sourceDetail ? <small className="runtime-repair-proof-source">{sourceDetail}</small> : null}
      {canRetarget ? (
        <button className="button-mini" type="button" onClick={() => onRetargetRuntime(proof.operation!)}>
          <GitBranch size={14} aria-hidden="true" />
          Use proved runtime
        </button>
      ) : null}
    </div>
  );
}

function RuntimeRepairMetric({
  detail,
  label,
  tone,
  value
}: {
  detail: string;
  label: string;
  tone: GateTone;
  value: string;
}): JSX.Element {
  return (
    <div className={`runtime-repair-metric runtime-repair-metric-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function runtimeRepairCoverageValue(proof: RuntimeRepairProof): string {
  if (proof.targetCount !== undefined) return `${proof.targetCount} target${proof.targetCount === 1 ? "" : "s"}`;
  if (proof.eligibleTargetCount !== undefined || proof.blockedTargetCount !== undefined) return "ranked";
  return "not retained";
}

function runtimeRepairCoverageDetail(proof: RuntimeRepairProof): string {
  const parts = [];
  if (proof.eligibleTargetCount !== undefined) parts.push(`${proof.eligibleTargetCount} eligible`);
  if (proof.blockedTargetCount !== undefined) parts.push(`${proof.blockedTargetCount} blocked`);
  return parts.join(" / ") || "target coverage not retained";
}

function OperationRuntimeProof({
  operation,
  onCopyCommand
}: {
  operation: Record<string, unknown>;
  onCopyCommand?: (label: string, command: string) => void;
}): JSX.Element {
  const optimizerDetail = stringOf(operation.runtime_optimizer_detail, "");
  const bestRuntimeTarget = stringOf(operation.best_runtime_target_id, "");
  const scoreDelta = numberOf(operation.runtime_score_delta);
  const runtimeFitScore = numberOf(operation.runtime_fit_score);
  const runtimeFitTier = stringOf(operation.runtime_fit_tier, "");
  const runtimeLane = stringOf(operation.runtime_lane_label, "");
  const runtimeLaneAcceleration = stringOf(operation.runtime_lane_acceleration, "").replace(/_/g, " ");
  const artifactLaneState = stringOf(operation.artifact_lane_state, "");
  const artifactLaneDetail = stringOf(operation.artifact_lane_detail, "");
  const productionApplyAllowed = operation.production_apply_allowed;
  const capabilityLockStatus = stringOf(operation.runtime_capability_lock_status, "");
  const capabilityLockDigest = stringOf(operation.runtime_capability_sha256, "");
  const capabilityLockProfile = stringOf(operation.runtime_capability_edge_profile, "");
  const telemetryState = stringOf(operation.runtime_capability_telemetry_state, "").replace(/_/g, " ");
  const telemetryDetail = stringOf(operation.runtime_capability_telemetry_detail, "");
  const heartbeatAge = numberOf(operation.runtime_capability_heartbeat_age_seconds);
  const heartbeatBudget = numberOf(operation.runtime_capability_heartbeat_stale_after_seconds);
  const capabilityFailures = stringsOf(operation.runtime_capability_failures);
  const contractAction = stringOf(operation.edge_execution_contract_action, "");
  const runtimeRemediationLabel = stringOf(operation.runtime_remediation_label, "");
  const runtimeRemediationTarget = stringOf(operation.runtime_remediation_runtime_target_id, "");
  const runtimeRemediationPrevious = stringOf(operation.runtime_remediation_previous_runtime_target_id, "");
  const runtimeRemediationDelta = numberOf(operation.runtime_remediation_score_delta);
  const runtimeContractTarget = stringOf(operation.runtime_remediation_contract_runtime_target_id, "");
  const runtimeContractLabel = stringOf(operation.runtime_remediation_contract_label, "");
  const runtimeContractKind = stringOf(operation.runtime_remediation_contract_kind, "");
  const runtimeContractCommand = localizeHubCommandText(stringOf(operation.runtime_remediation_contract_command_text, ""));
  const runtimeContractNote = stringOf(operation.runtime_remediation_contract_command_note, "");
  const runtimeContractRequiresEdge = operation.runtime_remediation_contract_requires_edge_execution === true;
  const runtimeContractCommandLabel =
    `${runtimeContractTarget || runtimeRemediationTarget || "runtime"} ${runtimeContractRequiresEdge || runtimeContractKind === "edge" ? "edge command" : "operator command"}`;
  const runtimeRetargetedFrom = stringOf(operation.runtime_retargeted_from, "");
  const runtimeRetargetedTo = stringOf(operation.runtime_retargeted_to, "");
  const runtimeRetargetedBy = stringOf(operation.runtime_retargeted_by, "");
  const runtimeRetargetProofStatus = stringOf(operation.runtime_retarget_proof_status, "");
  const runtimeRetargetFitScore = numberOf(operation.runtime_retarget_runtime_fit_score);
  const runtimeRetargetLockStatus = stringOf(operation.runtime_retarget_capability_lock_status, "");
  const runtimeRetargetCapability = stringOf(operation.runtime_retarget_capability_sha256, "");
  const runtimeRetargetValidation = stringOf(operation.runtime_retarget_validation_id, "");
  const runtimeRetargetBenchmark = stringOf(operation.runtime_retarget_benchmark_id, "");
  const runtimeRetargetAssessment = stringOf(operation.runtime_retarget_target_assessment_sha256, "");
  const runtimeRetargetReplayProofStatus = stringOf(operation.runtime_retarget_replay_proof_status, "");
  const runtimeRetargetReplaySignedCapability = stringOf(operation.runtime_retarget_replay_signed_capability_sha256, "");
  const runtimeRetargetReplayCurrentCapability = stringOf(operation.runtime_retarget_replay_current_capability_sha256, "");
  const runtimeRetargetReplaySignedAssessment = stringOf(
    operation.runtime_retarget_replay_signed_target_assessment_sha256,
    ""
  );
  const runtimeRetargetReplayCurrentAssessment = stringOf(
    operation.runtime_retarget_replay_current_target_assessment_sha256,
    ""
  );
  return (
    <>
      {runtimeFitScore !== undefined ? (
        <small>
          runtime fit {runtimeFitScore}/100{runtimeFitTier ? ` ${runtimeFitTier}` : ""}
        </small>
      ) : null}
      {runtimeLane ? (
        <small>
          lane {runtimeLane}{runtimeLaneAcceleration ? ` / ${runtimeLaneAcceleration}` : ""}
        </small>
      ) : null}
      {artifactLaneState ? (
        <small>
          artifact {artifactLaneState.replace(/_/g, " ")}
          {artifactLaneDetail ? `: ${artifactLaneDetail}` : ""}
        </small>
      ) : null}
      {typeof productionApplyAllowed === "boolean" ? (
        <small>production apply {productionApplyAllowed ? "permitted" : "blocked"}</small>
      ) : null}
      {capabilityLockStatus || capabilityLockDigest ? (
        <small>
          capability lock {capabilityLockStatus ? capabilityLockStatus.replace(/_/g, " ") : "hash locked"}
          {capabilityLockDigest ? ` ${capabilityLockDigest.slice(0, 12)}` : ""}
          {capabilityLockProfile ? ` / ${capabilityLockProfile}` : ""}
        </small>
      ) : null}
      {heartbeatAge !== undefined && heartbeatBudget !== undefined ? (
        <small>
          telemetry {telemetryState || "heartbeat"}: {formatAge(heartbeatAge)} old / {formatAge(heartbeatBudget)} budget
        </small>
      ) : telemetryDetail ? (
        <small>telemetry {compactMetricDetail(telemetryDetail)}</small>
      ) : null}
      {capabilityFailures.length ? <small>{compactMetricDetail(capabilityFailures[0])}</small> : null}
      {contractAction ? <small>edge contract {contractAction.replace(/_/g, " ")}</small> : null}
      {optimizerDetail ? <small>{optimizerDetail}</small> : null}
      {bestRuntimeTarget ? (
        <small>
          best runtime {bestRuntimeTarget}
          {scoreDelta !== undefined ? ` (+${scoreDelta} fit)` : ""}
        </small>
      ) : null}
      {runtimeRemediationLabel && runtimeRemediationTarget ? (
        <small>
          runtime fix {runtimeRemediationLabel}:{" "}
          {runtimeRemediationPrevious ? `${runtimeRemediationPrevious} -> ` : ""}
          {runtimeRemediationTarget}
          {runtimeRemediationDelta !== undefined ? ` (+${runtimeRemediationDelta} fit)` : ""}
        </small>
      ) : null}
      {runtimeContractCommand ? (
        <div className="pending-runtime-command">
          <span>
            {runtimeContractRequiresEdge || runtimeContractKind === "edge" ? "edge-run" : "operator"}{" "}
            {runtimeContractLabel || runtimeRemediationLabel || "runtime command"}
          </span>
          <code>{runtimeContractCommand}</code>
          {onCopyCommand ? (
            <button
              className="button-mini"
              type="button"
              onClick={() => onCopyCommand(runtimeContractCommandLabel, runtimeContractCommand)}
            >
              <Clipboard size={14} aria-hidden="true" />
              Copy
            </button>
          ) : null}
          {runtimeContractNote ? <small>{compactMetricDetail(runtimeContractNote)}</small> : null}
        </div>
      ) : null}
      {runtimeRetargetedFrom && runtimeRetargetedTo ? (
        <small>
          retargeted {runtimeRetargetedFrom}
          {" -> "}
          {runtimeRetargetedTo}
          {runtimeRetargetedBy ? ` by ${runtimeRetargetedBy}` : ""}
        </small>
      ) : null}
      {runtimeRetargetProofStatus || runtimeRetargetCapability || runtimeRetargetFitScore !== undefined ? (
        <small>
          retarget proof {runtimeRetargetProofStatus || "proved"}
          {runtimeRetargetFitScore !== undefined ? ` / fit ${runtimeRetargetFitScore}/100` : ""}
          {runtimeRetargetLockStatus ? ` / lock ${runtimeRetargetLockStatus.replace(/_/g, " ")}` : ""}
          {runtimeRetargetCapability ? ` ${runtimeRetargetCapability.slice(0, 12)}` : ""}
          {runtimeRetargetValidation ? ` / validation ${runtimeRetargetValidation}` : ""}
          {runtimeRetargetBenchmark ? ` / benchmark ${runtimeRetargetBenchmark}` : ""}
          {runtimeRetargetAssessment ? ` / assessment ${runtimeRetargetAssessment.slice(0, 12)}` : ""}
        </small>
      ) : null}
      {runtimeRetargetReplayProofStatus ? (
        <small>
          retarget replay proof {runtimeRetargetReplayProofStatus.replace(/_/g, " ")}
          {runtimeRetargetReplaySignedCapability ? ` / signed ${runtimeRetargetReplaySignedCapability.slice(0, 12)}` : ""}
          {runtimeRetargetReplayCurrentCapability ? ` / current ${runtimeRetargetReplayCurrentCapability.slice(0, 12)}` : ""}
          {runtimeRetargetReplaySignedAssessment ? ` / signed assessment ${runtimeRetargetReplaySignedAssessment.slice(0, 12)}` : ""}
          {runtimeRetargetReplayCurrentAssessment ? ` / current assessment ${runtimeRetargetReplayCurrentAssessment.slice(0, 12)}` : ""}
        </small>
      ) : null}
    </>
  );
}

export function PendingOperationRow({
  operation,
  onCopyCommand,
  onRetargetRuntime
}: {
  operation: Record<string, unknown>;
  onCopyCommand?: (label: string, command: string) => void;
  onRetargetRuntime?: (operation: Record<string, unknown>) => void;
}): JSX.Element {
  const digest = stringOf(operation.payload_sha256, "");
  const operationType = stringOf(operation.operation, "operation");
  const signatureLabel = pendingSignatureLabel(operation);
  const signatureReason = stringOf(operation.signature_verification_reason, "");
  const replayLabel = pendingReplayLabel(operation);
  const replayReason = stringOf(operation.replay_reason, "");
  const supersededByModel = stringOf(operation.superseded_by_model_id, "");
  const retargetCandidate = stringOf(operation.runtime_remediation_runtime_target_id, "");
  const target = [
    stringOf(operation.device_id, ""),
    stringOf(operation.slot, ""),
    stringOf(operation.runtime_target_id, "")
  ].filter(Boolean);
  return (
    <div className="pending-operation-row">
      <span>{operationType} intent</span>
      <strong>{stringOf(operation.summary, "queued operation")}</strong>
      <small>
        {stringOf(operation.actor, "operator")} - {compactDate(stringOf(operation.recorded_at, ""))}
      </small>
      <code>{digest ? `sha256:${digest.slice(0, 12)}` : "sha256:pending"}</code>
      <small>{signatureLabel}</small>
      {signatureReason && signatureLabel !== "verified intent" ? <small>{signatureReason}</small> : null}
      <small>{replayLabel}</small>
      {replayReason && replayLabel !== "ready to replay" ? <small>{replayReason}</small> : null}
      <OperationRuntimeProof operation={operation} onCopyCommand={onCopyCommand} />
      {retargetCandidate && onRetargetRuntime ? (
        <button className="button-mini" type="button" onClick={() => onRetargetRuntime(operation)}>
          <GitBranch size={14} aria-hidden="true" />
          Use best runtime
        </button>
      ) : null}
      {supersededByModel ? <small>final intent selects {supersededByModel}</small> : null}
      {target.length ? <small>{target.join(" / ")}</small> : null}
    </div>
  );
}

export function DeadLetteredOperationRow({
  operation,
  onCopyCommand,
  onRequeue
}: {
  operation: Record<string, unknown>;
  onCopyCommand?: (label: string, command: string) => void;
  onRequeue?: (operation: Record<string, unknown>) => void;
}): JSX.Element {
  const digest = stringOf(operation.payload_sha256, "");
  const operationType = stringOf(operation.operation, "operation");
  const signatureLabel = pendingSignatureLabel(operation);
  const replayLabel = pendingReplayLabel(operation);
  const replayReason = stringOf(operation.replay_reason, "");
  const quarantineReason = stringOf(operation.reason, "");
  const target = [
    stringOf(operation.device_id, ""),
    stringOf(operation.slot, ""),
    stringOf(operation.runtime_target_id, "")
  ].filter(Boolean);
  return (
    <div className="pending-operation-row pending-operation-row-dead-letter">
      <span>{operationType} quarantined</span>
      <strong>{stringOf(operation.summary, "quarantined operation")}</strong>
      <small>
        {stringOf(operation.actor, "operator")} - {compactDate(stringOf(operation.quarantined_at, ""))}
      </small>
      <code>{digest ? `sha256:${digest.slice(0, 12)}` : "sha256:quarantined"}</code>
      <small>{signatureLabel}</small>
      <small>{replayLabel}</small>
      {replayReason ? <small>{replayReason}</small> : null}
      {quarantineReason ? <small>{quarantineReason}</small> : null}
      <OperationRuntimeProof operation={operation} onCopyCommand={onCopyCommand} />
      {onRequeue ? (
        <button className="button-mini" type="button" onClick={() => onRequeue(operation)}>
          <FileCheck2 size={14} aria-hidden="true" />
          Requeue intent
        </button>
      ) : null}
      {target.length ? <small>{target.join(" / ")}</small> : null}
    </div>
  );
}

function pendingSignatureLabel(operation: Record<string, unknown>): string {
  const status = stringOf(operation.signature_status, "");
  if (status === "verified") return "verified intent";
  if (status === "invalid") return "tampered intent";
  if (status === "missing_signature") return "missing signature";
  if (status === "key_unavailable") return "key unavailable";
  if (status === "unsigned_allowed") return "unsigned allowed";
  return operation.signature_present === true ? "signed intent" : "unsigned intent";
}

function pendingReplayLabel(operation: Record<string, unknown>): string {
  const status = stringOf(operation.replay_status, "");
  if (status === "superseded" || operation.superseded === true) return "superseded intent";
  if (status === "ready_with_runtime_advisory") return "runtime advisory";
  if (operation.replay_ready === true || status === "ready") return "ready to replay";
  if (status === "blocked") return "replay blocked";
  return "replay pending";
}
