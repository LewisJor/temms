import type { ReactNode } from "react";
import { toneFor } from "../lib/hub-format";
import type { Preview, Toast } from "../types";

export function Button({
  ariaLabel,
  children,
  testId,
  icon,
  variant = "primary",
  disabled = false,
  onClick
}: {
  ariaLabel?: string;
  children: ReactNode;
  testId?: string;
  icon?: ReactNode;
  variant?: "primary" | "secondary" | "ghost";
  disabled?: boolean;
  onClick?: () => void;
}): JSX.Element {
  return (
    <button
      aria-label={ariaLabel}
      className={`button button-${variant}`}
      data-testid={testId}
      type="button"
      onClick={onClick}
      disabled={disabled}
    >
      {icon}
      <span>{children}</span>
    </button>
  );
}

export function Badge({ value }: { value: string }): JSX.Element {
  return <span className={`badge badge-${toneFor(value)}`}>{value || "unknown"}</span>;
}

export function ToastView({ toast }: { toast: Toast }): JSX.Element {
  return (
    <section className={`toast toast-${toast.tone}`}>
      <strong>{toast.title}</strong>
      {toast.detail ? <span>{toast.detail}</span> : null}
    </section>
  );
}

export function Submit({
  children,
  icon,
  variant = "primary",
  disabled = false
}: {
  children: ReactNode;
  icon?: ReactNode;
  variant?: "primary" | "secondary";
  disabled?: boolean;
}): JSX.Element {
  return (
    <button className={`button button-${variant}`} type="submit" disabled={disabled}>
      {icon}
      <span>{children}</span>
    </button>
  );
}

export function PreviewPanel({ preview, onClear }: { preview: Preview; onClear: () => void }): JSX.Element {
  return (
    <section className="panel preview">
      <div className="panel-header">
        <h2>{preview.title}</h2>
        <button className="button-mini" type="button" onClick={onClear}>
          Clear
        </button>
      </div>
      <PreviewSummary payload={preview.payload} />
      <details className="payload-details">
        <summary>Payload</summary>
        <pre>{JSON.stringify(preview.payload, null, 2)}</pre>
      </details>
    </section>
  );
}

function PreviewSummary({ payload }: { payload: unknown }): JSX.Element {
  const record = asRecord(payload);
  if (record.schema_version === "temms-edge-runtime-proof/v1") {
    return <EdgeProofPreviewSummary proof={record} />;
  }
  const rollout = asRecord(record.rollout);
  const packageRecord = asRecord(record.package);
  const outcome = asRecord(record.outcome);
  const counts = asRecord(record.counts);
  const trust = asRecord(record.trust);
  const headline = stringOf(record.headline) ?? stringOf(record.status) ?? stringOf(record.state);
  const rolloutId = stringOf(record.rollout_id) ?? stringOf(rollout.rollout_id);
  const packageId = stringOf(record.package_id) ?? stringOf(packageRecord.package_id);
  const model = stringOf(record.model) ?? stringOf(outcome.active_model);
  const compatible = typeof record.compatible === "boolean" ? (record.compatible ? "compatible" : "blocked") : undefined;
  const metrics = [
    ["Package", packageId],
    ["Rollout", rolloutId],
    ["Model", model],
    ["Compatibility", compatible],
    ["Events", stringOf(counts.timeline_entries)],
    ["Signed", stringOf(trust.signed_package_imports)],
    ["Released", stringOf(trust.released_packages)]
  ].filter(([, value]) => Boolean(value));

  if (!headline && !metrics.length) {
    return <div className="preview-summary">Action completed.</div>;
  }

  return (
    <div className="preview-summary">
      {headline ? <strong>{headline}</strong> : null}
      {metrics.length ? (
        <dl>
          {metrics.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}

function EdgeProofPreviewSummary({ proof }: { proof: Record<string, unknown> }): JSX.Element {
  const selection = asRecord(proof.selection);
  const readiness = asRecord(proof.readiness);
  const runtimeFit = asRecord(readiness.runtime_fit);
  const lock = runtimeCapabilityLock(readiness);
  const gatePolicy = asRecord(proof.gate_policy);
  const integrity = asRecord(proof.integrity);
  const attestation = asRecord(integrity.attestation);
  const componentDigests = asRecord(proof.component_digests);
  const componentDigestCount = [
    componentDigests.runtime_workbench_sha256,
    componentDigests.runtime_decision_trace_sha256,
    componentDigests.edge_execution_manifest_sha256
  ].filter((digest) => Boolean(stringOf(digest))).length;
  const gateStatus = stringOf(proof.gate_status) ?? stringOf(proof.status) ?? "unknown";
  const gateFailures = Array.isArray(proof.gate_failures) ? proof.gate_failures : [];
  const policy = [
    gatePolicy.require_go === true ? "go" : undefined,
    gatePolicy.require_best_runtime === true ? "best runtime" : undefined,
    gatePolicy.require_capability_lock === true ? "capability lock" : undefined,
    stringOf(gatePolicy.min_runtime_fit) ? `fit >= ${gatePolicy.min_runtime_fit}` : undefined
  ].filter(Boolean).join(" + ");
  const metrics = [
    ["Gate", gateStatus],
    ["Runtime fit", stringOf(proof.runtime_fit_score) ?? stringOf(runtimeFit.score)],
    ["Policy", policy],
    ["Model", stringOf(selection.model_id)],
    ["Runtime", stringOf(selection.runtime_target_id)],
    ["Edge", stringOf(selection.device_id)],
    ["Capability lock", capabilityLockLabel(lock)],
    ["Heartbeat", capabilityLockFreshnessLabel(lock)],
    ["Component hashes", componentDigestCount ? `${componentDigestCount}/3 retained` : undefined],
    ["Payload hash", shortHash(stringOf(integrity.payload_sha256))],
    ["Signed", Object.keys(attestation).length ? shortHash(stringOf(attestation.key_fingerprint)) || "attested" : "not attested"],
    ["Failures", gateFailures.length ? String(gateFailures.length) : undefined]
  ].filter(([, value]) => Boolean(value));

  return (
    <div className="preview-summary">
      <strong>{`Edge proof ${gateStatus}`}</strong>
      <dl>
        {metrics.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function runtimeCapabilityLock(readiness: Record<string, unknown>): Record<string, unknown> {
  const contract = asRecord(readiness.edge_execution_contract);
  const runtimeDecision = asRecord(readiness.runtime_decision);
  const runtimeFit = asRecord(readiness.runtime_fit);
  const contractLock = asRecord(contract.runtime_capability_lock);
  if (Object.keys(contractLock).length) return contractLock;
  const decisionLock = asRecord(runtimeDecision.runtime_capability_lock);
  if (Object.keys(decisionLock).length) return decisionLock;
  return asRecord(runtimeFit.runtime_capability_lock);
}

function capabilityLockLabel(lock: Record<string, unknown>): string | undefined {
  const status = stringOf(lock.status);
  const digest = shortHash(stringOf(lock.capability_sha256));
  if (status && digest) return `${status} ${digest}`;
  return status ?? digest;
}

function capabilityLockFreshnessLabel(lock: Record<string, unknown>): string | undefined {
  const edgeInventory = asRecord(lock.edge_inventory);
  const freshness = asRecord(edgeInventory.telemetry_freshness);
  const state = stringOf(freshness.state) ?? stringOf(freshness.status);
  const ageSeconds = numberOf(freshness.heartbeat_age_seconds);
  const budgetSeconds = numberOf(freshness.heartbeat_stale_after_seconds);
  if (ageSeconds !== undefined && budgetSeconds !== undefined) {
    const label = state ? state.replace(/_/g, " ") : "telemetry";
    return `${label}: ${formatAge(ageSeconds)} old / ${formatAge(budgetSeconds)} budget`;
  }
  return stringOf(freshness.detail);
}

function shortHash(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const normalized = value.replace(/^sha256:/, "");
  return normalized ? normalized.slice(0, 12) : undefined;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function stringOf(value: unknown): string | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  return String(value);
}

function numberOf(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}
