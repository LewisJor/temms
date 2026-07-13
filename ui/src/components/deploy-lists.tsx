import { CheckCircle2, GitBranch, PlayCircle, RefreshCw } from "lucide-react";
import { planId, rolloutId } from "../lib/hub-format";
import { asRecord, numberOf, stringOf } from "../lib/json";
import { shortProofDigest } from "../lib/proof-hash";
import type { GateTone } from "../lib/workbench-types";
import type { MissionReplayPhase, Rollout, RolloutPlan } from "../types";
import { Badge } from "./ui";

export function RolloutRow({
  rollout,
  onApprove,
  onApply,
  onRollback
}: {
  rollout: Rollout;
  onApprove: (id: string) => void;
  onApply: (id: string) => void;
  onRollback: (id: string) => void;
}): JSX.Element {
  const id = rolloutId(rollout);
  const approvalPending = rollout.approval_required && !rollout.approval?.approved;
  const approval = rollout.approval_required
    ? rollout.approval?.approved
      ? "approved"
      : rollout.approval?.state ?? "pending"
    : "not required";
  const missionPackageStage = asRecord(rollout.mission_package_stage);
  const packageBindingDigest = stringOf(missionPackageStage.package_identity_sha256, "");
  return (
    <article className="rollout-row">
      <div>
        <strong>{id}</strong>
        <small>
          {rollout.model_id ?? rollout.package_id ?? "-"} to {rollout.device_id ?? "-"}
          {packageBindingDigest ? ` · pkg ${shortProofDigest(packageBindingDigest)}` : ""}
        </small>
      </div>
      <Badge value={rollout.state ?? "unknown"} />
      {packageBindingDigest ? <Badge value="package-bound" /> : null}
      <Badge value={approval} />
      <div className="row-actions">
        {approvalPending ? (
          <button className="button-mini" type="button" onClick={() => onApprove(id)}>
            <CheckCircle2 size={14} /> Approve
          </button>
        ) : null}
        <button className="button-mini" type="button" disabled={approvalPending || rollout.state === "activated"} onClick={() => onApply(id)}>
          <PlayCircle size={14} /> Apply
        </button>
        <button className="button-mini" type="button" disabled={rollout.state !== "activated"} onClick={() => onRollback(id)}>
          <RefreshCw size={14} /> Rollback
        </button>
      </div>
    </article>
  );
}

export function RolloutPlanRow({
  plan,
  onAdvance,
  onPause,
  onResume
}: {
  plan: RolloutPlan;
  onAdvance: (id: string) => void;
  onPause: (id: string) => void;
  onResume: (id: string) => void;
}): JSX.Element {
  const id = planId(plan);
  const targets = plan.targets ?? [];
  const counts = asRecord(plan.counts);
  const targetCount = planCount(plan, "targets", targets.length);
  const activated = planCount(plan, "activated", targets.filter((target) => target.state === "activated").length);
  const rolledBack = planCount(plan, "rolled_back", targets.filter((target) => target.state === "rolled_back").length);
  const failed = planCount(plan, "failed", targets.filter((target) => target.state === "failed").length);
  const inFlight =
    planCount(plan, "assigned", targets.filter((target) => target.state === "assigned").length) +
    planCount(plan, "downloading", targets.filter((target) => target.state === "downloading").length) +
    planCount(plan, "imported", targets.filter((target) => target.state === "imported").length);
  const pending = planCount(plan, "pending", targets.filter((target) => target.state === "pending").length);
  const reconciled = activated + rolledBack;
  const paused = plan.state === "paused";
  const terminal = plan.state === "complete" || plan.state === "completed" || plan.state === "failed";
  const blocked = plan.state === "blocked";
  const currentBatch = numberOf(plan.current_batch) ?? numberOf(counts.current_batch) ?? 0;
  return (
    <article className="rollout-row rollout-plan-row">
      <div>
        <strong>{id}</strong>
        <small>
          {plan.model_id ?? plan.package_id ?? "-"} across {targetCount} target{targetCount === 1 ? "" : "s"}
        </small>
        <small>{rolloutPlanProgress({ batchSize: plan.batch_size ?? 1, currentBatch, failed, inFlight, pending, reconciled })}</small>
      </div>
      <Badge value={plan.state ?? "unknown"} />
      <Badge value={plan.runtime_target_id ?? "runtime"} />
      <div className="row-actions">
        <button className="button-mini" type="button" disabled={paused || terminal || blocked} onClick={() => onAdvance(id)}>
          <GitBranch size={14} /> Advance
        </button>
        {paused ? (
          <button className="button-mini" type="button" onClick={() => onResume(id)}>
            <PlayCircle size={14} /> Resume
          </button>
        ) : (
          <button className="button-mini" type="button" disabled={terminal || blocked} onClick={() => onPause(id)}>
            <RefreshCw size={14} /> Pause
          </button>
        )}
      </div>
    </article>
  );
}

export function EvidenceSummaryRow({
  headline,
  events,
  signedImports
}: {
  headline: string;
  events: number;
  signedImports: number;
}): JSX.Element {
  return (
    <div className="proof-summary-row">
      <div>
        <strong>{headline}</strong>
        <small>
          {events} proof events - {signedImports} signed imports
        </small>
      </div>
      <Badge value="ready" />
    </div>
  );
}

export function MissionPhaseRow({ phase }: { phase: MissionReplayPhase }): JSX.Element {
  const status = stringOf(phase.status, "missing");
  const refs = Array.isArray(phase.evidence_refs) ? phase.evidence_refs : [];
  return (
    <div className={`mission-phase-row mission-phase-row-${phaseTone(status)}`}>
      <div>
        <strong>{phase.label ?? phase.phase ?? "Mission phase"}</strong>
        <small>{phase.summary ?? "no evidence recorded"}</small>
        {refs.length ? <code>{refs.slice(0, 3).join(" / ")}</code> : null}
      </div>
      <Badge value={phaseStatusLabel(status)} />
    </div>
  );
}

export function TargetRow({ label, detail, status }: { label: string; detail: string; status: string }): JSX.Element {
  return (
    <div className="target-row">
      <div>
        <strong>{label}</strong>
        <small>{detail}</small>
      </div>
      <Badge value={status} />
    </div>
  );
}

export function EmptyState({ title, detail }: { title: string; detail: string }): JSX.Element {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      <span>{detail}</span>
    </div>
  );
}

function planCount(plan: RolloutPlan, key: string, fallback: number): number {
  return numberOf(asRecord(plan.counts)[key]) ?? fallback;
}

function rolloutPlanProgress({
  batchSize,
  currentBatch,
  failed,
  inFlight,
  pending,
  reconciled
}: {
  batchSize: number;
  currentBatch: number;
  failed: number;
  inFlight: number;
  pending: number;
  reconciled: number;
}): string {
  const parts = [currentBatch ? `batch ${currentBatch}` : "not advanced", `size ${batchSize}`];
  if (pending) parts.push(`${pending} pending`);
  if (inFlight) parts.push(`${inFlight} in flight`);
  if (reconciled) parts.push(`${reconciled} reconciled`);
  if (failed) parts.push(`${failed} failed`);
  if (parts.length === 1) parts.push("no active targets");
  return parts.join(" / ");
}

function phaseTone(status: string): GateTone {
  const normalized = status.toLowerCase();
  if (normalized === "complete") return "good";
  if (normalized === "preview_only") return "warn";
  if (normalized === "missing") return "bad";
  return "neutral";
}

function phaseStatusLabel(status: string): string {
  return status === "preview_only" ? "preview" : status || "missing";
}
