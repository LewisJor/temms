import { Clipboard, Download, FileCheck2, Terminal } from "lucide-react";
import type { EdgeProofDownloadHandoff } from "../api";
import { asRecord, numberOf, stringOf, stringsOf } from "../lib/json";
import { isSha256Digest, normalizeSha256Digest, shortProofDigest } from "../lib/proof-hash";
import { formatMetricNumber, formatThroughput } from "../lib/runtime-fit";
import type {
  EdgeProofComponentDigestStatus,
  EdgeProofTraceStatus,
  EdgeProofWorkflow,
  GateTone
} from "../lib/workbench-types";
import type { JsonObject } from "../types";
import { Badge, CapabilityMetric } from "./ui";

export function EdgeProofPanel({
  componentDigests,
  disabled,
  handoff,
  proof,
  trace,
  workflow,
  onGenerate,
  onDownload,
  onCopy
}: {
  componentDigests: EdgeProofComponentDigestStatus;
  disabled: boolean;
  handoff: EdgeProofDownloadHandoff | undefined;
  proof: JsonObject | undefined;
  trace: EdgeProofTraceStatus;
  workflow: EdgeProofWorkflow;
  onGenerate: () => void;
  onDownload: () => void;
  onCopy: (label: string, command: string) => void;
}): JSX.Element {
  const actionDisabled = disabled || workflow.missing.length > 0;
  return (
    <section className={`edge-proof edge-proof-${workflow.tone}`} aria-labelledby="edge-proof-heading">
      <div className="edge-proof-header">
        <div>
          <span className="section-kicker">Runtime proof artifact</span>
          <h2 id="edge-proof-heading">{workflow.status}</h2>
          <p>{workflow.detail}</p>
        </div>
        <div className="edge-proof-policy" aria-label="Proof gate policy">
          <span className="edge-proof-policy-line">Proof policy: {workflow.gatePolicy}</span>
          <Badge value={workflow.gatePolicy} />
          <span className={`badge badge-${workflow.capabilityLockTone}`}>{workflow.capabilityLock}</span>
          <small>{workflow.capabilityLockDetail}</small>
          <Badge value={workflow.attestation} />
          <strong>{workflow.runtimeFit}</strong>
          <small>{workflow.proofPath}</small>
          <div className="edge-proof-actions">
            <button
              className="button button-secondary"
              disabled={actionDisabled}
              type="button"
              onClick={onGenerate}
            >
              <FileCheck2 size={16} />
              <span>Generate artifact</span>
            </button>
            <button
              className="button button-ghost"
              disabled={actionDisabled}
              type="button"
              onClick={onDownload}
            >
              <Download size={16} />
              <span>Download JSON</span>
            </button>
          </div>
        </div>
      </div>

      {workflow.missing.length ? (
        <div className="edge-proof-missing" aria-label="Missing proof context">
          {workflow.missing.map((item) => (
            <span key={item}>{item}</span>
          ))}
        </div>
      ) : null}

      <EdgeProofTraceCard trace={trace} />
      <EdgeExecutionManifestCard proof={proof} />
      <EdgeProofComponentDigestCard status={componentDigests} />
      <EdgeProofDownloadHandoffCard componentDigests={componentDigests} handoff={handoff} />

      <div className="edge-proof-command-grid">
        <ProofCommand
          command={workflow.generateCommand}
          disabled={actionDisabled}
          icon={<Terminal size={16} />}
          label="Generate proof"
          onCopy={onCopy}
        />
        <ProofCommand
          command={workflow.verifyCommand}
          disabled={actionDisabled}
          icon={<FileCheck2 size={16} />}
          label="Verify gate"
          onCopy={onCopy}
        />
        <ProofCommand
          command={workflow.verifyJsonCommand}
          disabled={actionDisabled}
          icon={<Clipboard size={16} />}
          label="Verify JSON"
          onCopy={onCopy}
        />
      </div>
    </section>
  );
}

function EdgeExecutionManifestCard({ proof }: { proof: JsonObject | undefined }): JSX.Element {
  const manifest = asRecord(proof?.edge_execution_manifest);
  const execution = asRecord(manifest.execution);
  const edge = asRecord(manifest.edge);
  const evidence = asRecord(manifest.evidence);
  const admission = asRecord(manifest.admission);
  const capabilityLock = asRecord(edge.capability_lock);
  const schema = stringOf(manifest.schema_version, "");
  const runtimeImage = stringOf(execution.runtime_image, "");
  const runtimeTarget = stringOf(execution.runtime_target_id, "");
  const runtimeLane = asRecord(execution.runtime_lane);
  const capabilityStatus = stringOf(capabilityLock.status, "");
  const capabilityDigest = stringOf(capabilityLock.capability_sha256, "");
  const gateStatus = stringOf(admission.gate_status, "");
  const available = schema === "temms-edge-execution-manifest/v1";
  const tone: GateTone = !available ? "neutral" : gateStatus === "failed" ? "bad" : "good";
  const validationId = stringOf(evidence.runtime_validation_id, "");
  const benchmarkId = stringOf(evidence.benchmark_id, "");
  const latency = numberOf(evidence.latency_ms_p95);
  const throughput = numberOf(evidence.throughput_ips);
  const evidenceDetail = [
    validationId ? `validation ${validationId}` : "",
    benchmarkId ? `benchmark ${benchmarkId}` : "",
    latency !== undefined ? `${formatMetricNumber(latency)} ms p95` : "",
    throughput !== undefined ? `${formatThroughput(throughput)} ips` : ""
  ].filter(Boolean).join(" / ");

  return (
    <article className={`edge-proof-trace edge-execution-manifest edge-proof-trace-${tone}`} data-testid="edge-execution-manifest">
      <div className="edge-proof-trace-header">
        <div>
          <span>Execution manifest</span>
          <strong>{available ? stringOf(asRecord(manifest.path).label, "signed path") : "manifest pending"}</strong>
          <small>
            {available
              ? "Signed execution intent retained with runtime image, capability lock, and evidence ids."
              : "Generate or download a proof to inspect the signed execution manifest."}
          </small>
        </div>
        <Badge value={available ? gateStatus || "retained" : "not generated"} />
      </div>
      <div className="edge-proof-trace-grid">
        <CapabilityMetric
          detail={runtimeTarget || stringOf(execution.runtime_arch, "runtime target pending")}
          label="Runtime image"
          tone={runtimeImage ? "good" : available ? "warn" : "neutral"}
          value={runtimeImage || "pending"}
        />
        <CapabilityMetric
          detail={manifestRuntimeLaneDetail(runtimeLane)}
          label="Runtime lane"
          tone={runtimeLane.lane_id ? "good" : available ? "warn" : "neutral"}
          value={stringOf(runtimeLane.lane_id, "pending")}
        />
        <CapabilityMetric
          detail={capabilityDigest ? `sha256 ${capabilityDigest.slice(0, 12)}` : "capability digest pending"}
          label="Capability lock"
          tone={capabilityStatus === "locked" ? "good" : available ? "warn" : "neutral"}
          value={capabilityStatus || "pending"}
        />
        <CapabilityMetric
          detail={evidenceDetail || "validation and benchmark ids pending"}
          label="Evidence ids"
          tone={validationId && benchmarkId ? "good" : available ? "warn" : "neutral"}
          value={validationId && benchmarkId ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={execution.selected_is_best === true ? "selected runtime is best measured target" : "best-runtime proof pending"}
          label="Best target"
          tone={execution.selected_is_best === true ? "good" : available ? "warn" : "neutral"}
          value={execution.selected_is_best === true ? "yes" : "pending"}
        />
        <CapabilityMetric
          detail={manifestGatePolicyLabel(asRecord(admission.gate_policy))}
          label="Admission"
          tone={gateStatus === "passed" ? "good" : gateStatus === "failed" ? "bad" : "neutral"}
          value={gateStatus || "pending"}
        />
      </div>
    </article>
  );
}

function EdgeProofComponentDigestCard({
  status
}: {
  status: EdgeProofComponentDigestStatus;
}): JSX.Element {
  const errors = status.errors.slice(0, 2).join(" / ");
  const digestByKey = new Map(status.digests.map((digest) => [digest.key, digest]));
  const workbenchDigest = digestByKey.get("runtime_workbench_sha256");
  const traceDigest = digestByKey.get("runtime_decision_trace_sha256");
  const manifestDigest = digestByKey.get("edge_execution_manifest_sha256");

  return (
    <article className={`edge-proof-trace edge-proof-digests edge-proof-trace-${status.tone}`} data-testid="edge-proof-component-digests">
      <div className="edge-proof-trace-header">
        <div>
          <span>Component digests</span>
          <strong>{status.value}</strong>
          <small>{status.detail}</small>
        </div>
        <Badge value={status.status.replace(/_/g, " ")} />
      </div>
      <div className="edge-proof-trace-grid">
        <CapabilityMetric
          detail={status.schema || "component digest schema unavailable"}
          label="Digest schema"
          tone={status.schema === "temms-edge-runtime-proof-component-digests/v1" ? "good" : status.status === "not_generated" ? "neutral" : "warn"}
          value={status.schema || "pending"}
        />
        <CapabilityMetric
          detail={errors || "runtime workbench, trace, and execution manifest are individually hash-addressed"}
          label="Coverage"
          tone={status.tone}
          value={componentDigestCoverageLabel(status)}
        />
        <CapabilityMetric
          detail={workbenchDigest?.value ? `sha256 ${shortProofDigest(workbenchDigest.value)}` : "runtime workbench digest pending"}
          label="Workbench"
          tone={workbenchDigest?.value ? "good" : status.status === "not_generated" ? "neutral" : "warn"}
          value={workbenchDigest?.value ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={traceDigest?.value ? `sha256 ${shortProofDigest(traceDigest.value)}` : "runtime decision trace digest pending"}
          label="Trace"
          tone={traceDigest?.value ? "good" : status.status === "not_generated" ? "neutral" : "warn"}
          value={traceDigest?.value ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={manifestDigest?.value ? `sha256 ${shortProofDigest(manifestDigest.value)}` : "edge execution manifest digest pending"}
          label="Manifest"
          tone={manifestDigest?.value ? "good" : status.status === "not_generated" ? "neutral" : "warn"}
          value={manifestDigest?.value ? "retained" : "pending"}
        />
      </div>
    </article>
  );
}

function EdgeProofDownloadHandoffCard({
  componentDigests,
  handoff
}: {
  componentDigests: EdgeProofComponentDigestStatus;
  handoff: EdgeProofDownloadHandoff | undefined;
}): JSX.Element {
  const bodyDigests = new Map(componentDigests.digests.map((digest) => [digest.key, digest.value]));
  const headerDigests = [
    {
      key: "runtime_workbench_sha256",
      label: "Workbench",
      value: handoff?.runtimeWorkbenchSha256 || ""
    },
    {
      key: "runtime_decision_trace_sha256",
      label: "Trace",
      value: handoff?.runtimeDecisionTraceSha256 || ""
    },
    {
      key: "edge_execution_manifest_sha256",
      label: "Manifest",
      value: handoff?.edgeExecutionManifestSha256 || ""
    }
  ];
  const retainedHeaderDigests = headerDigests.filter((digest) => digest.value).length;
  const missingHeaderDigests = handoff
    ? headerDigests.filter((digest) => !digest.value).map((digest) => digest.label)
    : [];
  const mismatchedHeaderDigests = handoff
    ? headerDigests.filter((digest) => {
        const bodyDigest = bodyDigests.get(digest.key) || "";
        return digest.value && bodyDigest && normalizeSha256Digest(digest.value) !== normalizeSha256Digest(bodyDigest);
      })
    : [];
  const tone: GateTone = !handoff
    ? "neutral"
    : mismatchedHeaderDigests.length
      ? "bad"
      : missingHeaderDigests.length
        ? "warn"
        : "good";
  const value = !handoff
    ? "headers pending"
    : mismatchedHeaderDigests.length
      ? "header mismatch"
      : `${retainedHeaderDigests}/3 component headers`;
  const detail = !handoff
    ? "Downloaded artifact headers are not captured for the latest generated proof."
    : mismatchedHeaderDigests.length
      ? `${mismatchedHeaderDigests.map((digest) => digest.label).join(", ")} header disagrees with proof body`
      : missingHeaderDigests.length
        ? `${missingHeaderDigests.join(", ")} header missing from download response`
        : "Download response headers match the retained component digests.";
  const payloadDigest = handoff?.payloadSha256 || "";
  const gateTone: GateTone =
    handoff?.gateStatus === "passed" ? "good" : handoff?.gateStatus === "failed" ? "bad" : handoff ? "warn" : "neutral";
  const attestationTone: GateTone =
    handoff?.attestation === "signed" ? "good" : handoff?.attestation === "unsigned" ? "warn" : "neutral";

  return (
    <article className={`edge-proof-trace edge-proof-handoff edge-proof-trace-${tone}`} data-testid="edge-proof-download-handoff">
      <div className="edge-proof-trace-header">
        <div>
          <span>Download handoff headers</span>
          <strong>{value}</strong>
          <small>{detail}</small>
        </div>
        <Badge value={handoff ? "downloaded" : "not downloaded"} />
      </div>
      <div className="edge-proof-trace-grid">
        <CapabilityMetric
          detail={handoff?.fileName || "artifact filename header pending"}
          label="Filename"
          tone={handoff?.fileName ? "good" : "neutral"}
          value={handoff?.fileName ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={payloadDigest ? `sha256 ${shortProofDigest(payloadDigest)}` : "payload hash header pending"}
          label="Payload hash"
          tone={isSha256Digest(payloadDigest) ? "good" : handoff ? "warn" : "neutral"}
          value={payloadDigest ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={handoff?.keyFingerprint ? `key ${handoff.keyFingerprint}` : "signing-key fingerprint header pending"}
          label="Attestation"
          tone={attestationTone}
          value={handoff?.attestation || "pending"}
        />
        <CapabilityMetric
          detail="Strict proof policy result from the download envelope"
          label="Gate"
          tone={gateTone}
          value={handoff?.gateStatus || "pending"}
        />
        {headerDigests.map((digest) => {
          const bodyDigest = bodyDigests.get(digest.key) || "";
          const matches =
            digest.value && bodyDigest && normalizeSha256Digest(digest.value) === normalizeSha256Digest(bodyDigest);
          const digestTone: GateTone = matches ? "good" : digest.value ? "warn" : handoff ? "warn" : "neutral";
          return (
            <CapabilityMetric
              detail={digest.value ? `sha256 ${shortProofDigest(digest.value)}` : `${digest.label.toLowerCase()} header pending`}
              key={digest.key}
              label={`${digest.label} header`}
              tone={digestTone}
              value={matches ? "matches body" : digest.value ? "retained" : "pending"}
            />
          );
        })}
      </div>
    </article>
  );
}

function componentDigestCoverageLabel(status: EdgeProofComponentDigestStatus): string {
  if (!status.digestCount) return "pending";
  if (status.status === "consistent") return `${status.digestCount}/3 verified`;
  if (status.status === "mismatch") return `${status.digestCount}/3 checked`;
  if (status.status === "verifying") return `${status.digestCount}/3 checking`;
  return `${status.digestCount}/3 retained`;
}

function manifestRuntimeLaneDetail(lane: JsonObject): string {
  return [
    stringOf(lane.execution_engine, ""),
    stringsOf(lane.providers).join(", "),
    stringOf(lane.acceleration, ""),
    stringOf(lane.optimization_goal, "")
  ].filter(Boolean).join(" / ") || "runtime lane pending";
}

function manifestGatePolicyLabel(policy: JsonObject): string {
  const parts = [];
  if (policy.require_go === true) parts.push("go");
  if (policy.require_best_runtime === true) parts.push("best runtime");
  if (policy.require_capability_lock === true) parts.push("capability lock");
  const minRuntimeFit = numberOf(policy.min_runtime_fit);
  if (minRuntimeFit !== undefined) parts.push(`fit >= ${formatMetricNumber(minRuntimeFit)}`);
  return parts.length ? parts.join(" + ") : "proof policy pending";
}

function EdgeProofTraceCard({ trace }: { trace: EdgeProofTraceStatus }): JSX.Element {
  const sampleErrors = trace.errors.slice(0, 2);
  return (
    <article className={`edge-proof-trace edge-proof-trace-${trace.tone}`} data-testid="edge-proof-trace-consistency">
      <div className="edge-proof-trace-header">
        <div>
          <span>Signed runtime trace</span>
          <strong>{trace.value}</strong>
          <small>{trace.detail}</small>
        </div>
        <Badge value={trace.status.replace(/_/g, " ")} />
      </div>
      <div className="edge-proof-trace-grid">
        <CapabilityMetric
          detail={trace.schema || "trace schema unavailable"}
          label="Trace schema"
          tone={trace.schema === "temms-runtime-decision-trace/v1" ? "good" : trace.status === "not_generated" ? "neutral" : "warn"}
          value={trace.schema || "pending"}
        />
        <CapabilityMetric
          detail={`${trace.commandCount} remediation command${trace.commandCount === 1 ? "" : "s"}`}
          label="Targets"
          tone={trace.rowCount ? trace.tone : "neutral"}
          value={trace.rowCount ? `${trace.rowCount} ranked` : "pending"}
        />
        <CapabilityMetric
          detail={sampleErrors.length ? sampleErrors.join(" / ") : "trace agrees with runtime_workbench"}
          label="Workbench check"
          tone={trace.tone}
          value={trace.status === "mismatch" ? `${trace.errors.length} mismatch${trace.errors.length === 1 ? "" : "es"}` : trace.status.replace(/_/g, " ")}
        />
      </div>
    </article>
  );
}

function ProofCommand({
  command,
  disabled,
  icon,
  label,
  onCopy
}: {
  command: string;
  disabled: boolean;
  icon: JSX.Element;
  label: string;
  onCopy: (label: string, command: string) => void;
}): JSX.Element {
  return (
    <article className="edge-proof-command">
      <div className="edge-proof-command-topline">
        <span>{label}</span>
        <button
          className="button-mini"
          disabled={disabled}
          type="button"
          onClick={() => onCopy(label, command)}
        >
          {icon}
          Copy
        </button>
      </div>
      <pre>{command}</pre>
    </article>
  );
}
