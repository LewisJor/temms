import { GitBranch } from "lucide-react";
import { compactDate } from "../lib/hub-format";
import type { GateTone, RuntimeRepairProof } from "../lib/workbench-types";
import { Badge } from "./ui";

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
