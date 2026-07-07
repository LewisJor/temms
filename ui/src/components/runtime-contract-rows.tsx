import { Clipboard } from "lucide-react";
import type { JsonObject } from "../types";
import { asRecord, numberOf, stringOf } from "../lib/json";
import {
  formatMetricNumber,
  formatThroughput,
  runtimeLaneValue
} from "../lib/runtime-fit";
import {
  candidateRuntimeId,
  runtimeCandidateTone,
  runtimeTargetComponentProofs,
  targetAssessmentDetail,
  targetAssessmentRemediationDetail,
  targetAssessmentTone
} from "../lib/runtime-decision";
import { runtimeTargetAssessmentRemediationCommand } from "../lib/runtime-remediation";
import type {
  GateTone,
  RuntimeRemediationContext
} from "../lib/workbench-types";
import { Badge } from "./ui";

export function ExecutionPathNode({
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
    <div className={`execution-path-node execution-path-node-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

export function RuntimeCandidateRow({
  bestRuntimeTargetId,
  candidate,
  selectedRuntimeTargetId
}: {
  bestRuntimeTargetId: string;
  candidate: JsonObject;
  selectedRuntimeTargetId: string;
}): JSX.Element {
  const id = candidateRuntimeId(candidate);
  const lane = asRecord(candidate.runtime_lane);
  const score = numberOf(candidate.score);
  const latency = numberOf(candidate.latency_ms_p95);
  const throughput = numberOf(candidate.throughput_ips);
  const tier = stringOf(candidate.tier, "fit").replace(/_/g, " ");
  const labels = [
    id === selectedRuntimeTargetId ? "selected" : "",
    id === bestRuntimeTargetId ? "best" : "",
    stringOf(candidate.blocked, "") === "true" ? "blocked" : ""
  ].filter(Boolean);
  return (
    <div className={`execution-candidate execution-candidate-${runtimeCandidateTone(candidate, id, selectedRuntimeTargetId, bestRuntimeTargetId)}`}>
      <div>
        <span>{numberOf(candidate.rank) !== undefined ? `#${candidate.rank}` : "candidate"}</span>
        <strong>{id}</strong>
        <small>
          {score !== undefined ? `${score}/100 ${tier}` : tier}
          {latency !== undefined ? ` / ${formatMetricNumber(latency)} ms p95` : ""}
          {throughput !== undefined ? ` / ${formatThroughput(throughput)} ips` : ""}
        </small>
      </div>
      <div className="execution-candidate-meta">
        {labels.map((label) => (
          <Badge key={label} value={label} />
        ))}
        <small>{runtimeLaneValue(lane)}</small>
      </div>
    </div>
  );
}

export function TargetRuntimeAssessmentRow({
  assessment,
  bestRuntimeTargetId,
  context,
  onCopyRemediation,
  selectedRuntimeTargetId
}: {
  assessment: JsonObject;
  bestRuntimeTargetId: string;
  context: RuntimeRemediationContext;
  onCopyRemediation: (label: string, command: string) => void;
  selectedRuntimeTargetId: string;
}): JSX.Element {
  const id = candidateRuntimeId(assessment);
  const lane = asRecord(assessment.runtime_lane);
  const score = numberOf(assessment.score);
  const status = stringOf(
    assessment.status,
    assessment.blocked === true ? "blocked" : "eligible"
  ).replace(/_/g, " ");
  const remediation = asRecord(assessment.remediation);
  const remediationCommand = runtimeTargetAssessmentRemediationCommand(assessment, context);
  const componentProofs = runtimeTargetComponentProofs(assessment);
  const labels = [
    id === selectedRuntimeTargetId || assessment.selected === true ? "selected" : "",
    id === bestRuntimeTargetId || assessment.best === true ? "best" : "",
    status,
    remediation.requires_edge_execution === true ? "edge-run" : ""
  ].filter(Boolean);
  return (
    <div className={`execution-candidate execution-candidate-${targetAssessmentTone(assessment)}`}>
      <div>
        <span>{runtimeLaneValue(lane)}</span>
        <strong>{id}</strong>
        <small>
          {score !== undefined ? `${score}/100` : status}
          {` / ${targetAssessmentDetail(assessment)}`}
        </small>
        {Object.keys(remediation).length ? (
          <div className="execution-remediation-block">
            <small className="execution-remediation">
              Next: {targetAssessmentRemediationDetail(remediation)}
            </small>
            {remediationCommand ? (
              <div className="execution-remediation-actions">
                <span>{remediationCommand.edgeRun ? "edge-run" : "operator"}</span>
                <small>{remediationCommand.note}</small>
                <button
                  className="button-mini"
                  type="button"
                  onClick={() => onCopyRemediation(remediationCommand.label, remediationCommand.command)}
                >
                  <Clipboard size={14} />
                  Copy command
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        {componentProofs.length ? (
          <div className="execution-proof-chips" aria-label={`${id} component proof`}>
            {componentProofs.map((component) => (
              <span
                className={`execution-proof-chip execution-proof-chip-${component.tone}`}
                key={component.key}
              >
                {component.label}: {component.state}
                {component.score ? ` ${component.score}` : ""}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <div className="execution-candidate-meta">
        {labels.map((label) => (
          <Badge key={label} value={label} />
        ))}
      </div>
    </div>
  );
}
