import { Clipboard, GitBranch } from "lucide-react";
import type { DeploymentReadiness, Device, JsonObject, RuntimeTarget, RuntimeValidation } from "../types";
import { deviceId, displayGateState, runtimeTargetId, toneForReadinessStatus } from "../lib/hub-format";
import { asRecord, numberOf, stringOf } from "../lib/json";
import {
  artifactLaneDetail,
  artifactLaneTone,
  artifactLaneValue,
  capabilityLockDetail,
  capabilityLockTone,
  capabilityLockValue,
  compactMetricDetail,
  formatMetricNumber,
  formatThroughput,
  productionAdmissionDetail,
  productionAdmissionTone,
  productionAdmissionValue,
  runtimeFitScoreForProof,
  runtimeInventoryLabel,
  runtimeInventoryTone,
  runtimeLaneDetail,
  runtimeLaneFor,
  runtimeLaneTone,
  runtimeLaneValue,
  runtimeTargetCapabilityDetail
} from "../lib/runtime-fit";
import {
  candidateRuntimeId,
  executionContractHeadline,
  executionContractTone,
  operatorRuntimeLaneItems,
  runtimeCandidateTone,
  runtimeDecisionActionLabel,
  runtimeDecisionCandidates,
  runtimeDecisionGates,
  runtimeTargetAssessments,
  runtimeTargetComponentProofs,
  targetAssessmentDetail,
  targetAssessmentRemediationDetail,
  targetAssessmentTone,
  targetRuntimeCoverageSummary
} from "../lib/runtime-decision";
import { runtimeTargetAssessmentRemediationCommand } from "../lib/runtime-remediation";
import type {
  EdgeProofWorkflow,
  EdgeRuntimeFit,
  GateTone,
  ModelRecord,
  ReadinessVerdict,
  RuntimeFitDisplay,
  RuntimeRemediationContext
} from "../lib/workbench-types";
import { EmptyState } from "./deploy-lists";
import { Badge, Button, CapabilityMetric } from "./ui";

export function EdgeOperatorCommandPanel({
  device,
  edgeExecutionContract,
  model,
  proofWorkflow,
  readiness,
  runtime,
  runtimeDecision,
  runtimeFitDisplay
}: {
  device: Device | undefined;
  edgeExecutionContract: JsonObject;
  model: ModelRecord | undefined;
  proofWorkflow: EdgeProofWorkflow;
  readiness: DeploymentReadiness | undefined;
  runtime: RuntimeTarget | undefined;
  runtimeDecision: JsonObject;
  runtimeFitDisplay: RuntimeFitDisplay;
}): JSX.Element {
  const contract = Object.keys(edgeExecutionContract).length ? edgeExecutionContract : runtimeDecision;
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const decisionFit = asRecord(contract.runtime_fit);
  const targetSelection = Object.keys(asRecord(contract.target_selection)).length
    ? asRecord(contract.target_selection)
    : asRecord(runtimeFit.target_selection);
  const contractPath = asRecord(contract.path);
  const selectedRuntimeTargetId = stringOf(
    targetSelection.selected_runtime_target_id,
    stringOf(contractPath.runtime_target_id, runtime ? runtimeTargetId(runtime) : "")
  );
  const bestRuntimeTargetId = stringOf(
    targetSelection.best_runtime_target_id,
    selectedRuntimeTargetId
  );
  const selectedScore =
    numberOf(decisionFit.score) ??
    numberOf(runtimeFit.score) ??
    runtimeFitScoreForProof(readiness, runtimeFitDisplay);
  const candidates = runtimeDecisionCandidates(contract, runtimeFit, selectedRuntimeTargetId, bestRuntimeTargetId);
  const targetAssessments = runtimeTargetAssessments(contract, runtimeFit, candidates);
  const targetCoverage = targetRuntimeCoverageSummary(targetAssessments);
  const runtimeLaneItems = operatorRuntimeLaneItems(
    targetAssessments,
    selectedRuntimeTargetId,
    bestRuntimeTargetId
  );
  const productionAdmission = Object.keys(asRecord(contract.production_admission)).length
    ? asRecord(contract.production_admission)
    : asRecord(readiness?.production_admission);
  const modelId = model?.id ?? stringOf(contractPath.model_id, "model missing");
  const runtimeId = selectedRuntimeTargetId || (runtime ? runtimeTargetId(runtime) : "runtime missing");
  const edgeId = device ? deviceId(device) : stringOf(contractPath.device_id, "edge missing");
  const pathLabel = [modelId, runtimeId, edgeId].join(" -> ");
  const selectedIsBest = runtimeId === bestRuntimeTargetId;
  const statusDetail = [
    selectedScore !== undefined ? `${selectedScore}/100 runtime fit` : runtimeFitDisplay.label,
    selectedIsBest ? "selected runtime is best" : bestRuntimeTargetId ? `best runtime ${bestRuntimeTargetId}` : "",
    targetCoverage.detail,
    proofWorkflow.missing.length ? "proof context incomplete" : "signed proof ready"
  ].filter(Boolean).join(" / ");
  const proofValue = proofWorkflow.missing.length
    ? `${proofWorkflow.missing.length} missing`
    : proofWorkflow.attestation;
  const tone = proofWorkflow.missing.length
    ? "warn"
    : productionAdmissionTone(productionAdmission) === "bad"
      ? "bad"
      : runtimeFitDisplay.tone;

  return (
    <section className={`operator-command operator-command-${tone}`} aria-labelledby="operator-command-heading">
      <div className="operator-command-copy">
        <span className="section-kicker">On-device runtime proof</span>
        <h2 id="operator-command-heading">{pathLabel}</h2>
        <p>{statusDetail}</p>
      </div>
      <div className="operator-command-badges" aria-label="Active edge path status">
        <Badge value={runtimeFitDisplay.label} />
        <Badge value={selectedIsBest ? "best target" : "retarget available"} />
        <Badge value={proofWorkflow.gatePolicy} />
      </div>
      <div className="operator-command-grid" aria-label="Active model runtime edge proof">
        <OperatorCommandMetric
          detail={model ? `${model.packageId} / ${model.format}` : "select a model"}
          label="Model"
          tone={model ? "good" : "bad"}
          value={modelId}
        />
        <OperatorCommandMetric
          detail={runtime ? runtimeTargetCapabilityDetail(runtime) : "select a runtime target"}
          label="Runtime target"
          tone={runtime ? runtimeFitDisplay.tone : "bad"}
          value={runtimeId}
        />
        <OperatorCommandMetric
          detail={device ? `${device.profile ?? "unknown profile"} / ${device.status ?? "registered"}` : "select an edge"}
          label="Device inventory"
          tone={device ? runtimeInventoryTone(device) : "bad"}
          value={edgeId}
        />
        <OperatorCommandMetric
          detail={targetCoverage.detail}
          label="Runtime coverage"
          tone={targetCoverage.tone}
          value={targetCoverage.value}
        />
        <OperatorCommandMetric
          detail={productionAdmissionDetail(productionAdmission)}
          label="Field admission"
          tone={productionAdmissionTone(productionAdmission)}
          value={productionAdmissionValue(productionAdmission)}
        />
        <OperatorCommandMetric
          detail={proofWorkflow.proofPath}
          label="Signed proof"
          tone={proofWorkflow.tone}
          value={proofValue}
        />
      </div>
      {runtimeLaneItems.length ? (
        <div className="operator-command-lanes" aria-label="Runtime target alternatives">
          {runtimeLaneItems.map((item) => (
            <div key={item.id} className={`operator-command-lane operator-command-lane-${item.tone}`}>
              <div className="operator-command-lane-topline">
                <span>{item.status}</span>
                <strong>{item.id}</strong>
              </div>
              <small>{item.detail}</small>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function OperatorCommandMetric({
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
    <div className={`operator-command-metric operator-command-metric-${tone}`}>
      <span>{label}</span>
      <strong>{value || "-"}</strong>
      <small>{detail}</small>
    </div>
  );
}

export function EdgeExecutionContractPanel({
  device,
  edgeExecutionContract,
  edgeRuntimeFit,
  model,
  onCopyRemediation,
  onSelectRuntimeTarget,
  readiness,
  readinessVerdict,
  resourceEnvelopeFit,
  runtime,
  runtimeDecision,
  runtimeFitDisplay,
  runtimeValidation
}: {
  device: Device | undefined;
  edgeExecutionContract: JsonObject;
  edgeRuntimeFit: EdgeRuntimeFit;
  model: ModelRecord | undefined;
  onCopyRemediation: (label: string, command: string) => void;
  onSelectRuntimeTarget: (runtimeTargetIdValue: string) => void;
  readiness: DeploymentReadiness | undefined;
  readinessVerdict: ReadinessVerdict;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtime: RuntimeTarget | undefined;
  runtimeDecision: JsonObject;
  runtimeFitDisplay: RuntimeFitDisplay;
  runtimeValidation: RuntimeValidation | undefined;
}): JSX.Element {
  const contract = Object.keys(edgeExecutionContract).length ? edgeExecutionContract : runtimeDecision;
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const decisionFit = asRecord(contract.runtime_fit);
  const targetSelection = Object.keys(asRecord(contract.target_selection)).length
    ? asRecord(contract.target_selection)
    : asRecord(runtimeFit.target_selection);
  const contractPath = asRecord(contract.path);
  const remediationContext: RuntimeRemediationContext = {
    packageId: model?.packageId ?? stringOf(contractPath.package_id, ""),
    modelId: model?.id ?? stringOf(contractPath.model_id, ""),
    deviceId: device ? deviceId(device) : stringOf(contractPath.device_id, ""),
    slot: stringOf(contractPath.slot, "vision")
  };
  const selectedRuntimeTargetId = stringOf(
    targetSelection.selected_runtime_target_id,
    stringOf(contractPath.runtime_target_id, runtime ? runtimeTargetId(runtime) : "runtime missing")
  );
  const bestRuntimeTargetId = stringOf(
    targetSelection.best_runtime_target_id,
    selectedRuntimeTargetId
  );
  const selectedScore =
    numberOf(decisionFit.score) ??
    numberOf(runtimeFit.score) ??
    runtimeFitScoreForProof(readiness, runtimeFitDisplay);
  const bestScore = numberOf(targetSelection.best_score);
  const scoreDelta = numberOf(targetSelection.score_delta);
  const productionAdmission = Object.keys(asRecord(contract.production_admission)).length
    ? asRecord(contract.production_admission)
    : asRecord(readiness?.production_admission);
  const selectedLane = Object.keys(asRecord(contract.selected_runtime_lane)).length
    ? asRecord(contract.selected_runtime_lane)
    : runtimeLaneFor(runtimeFit, runtime);
  const bestLane = asRecord(contract.best_runtime_lane);
  const artifactLane = Object.keys(asRecord(contract.artifact_lane)).length
    ? asRecord(contract.artifact_lane)
    : asRecord(runtimeFit.artifact_lane);
  const capabilityLock = Object.keys(asRecord(contract.runtime_capability_lock)).length
    ? asRecord(contract.runtime_capability_lock)
    : asRecord(runtimeFit.runtime_capability_lock);
  const recommendedAction = stringOf(
    contract.recommended_action,
    readinessVerdict.label === "go" ? "apply_or_stage" : "review"
  );
  const decisionStatus = stringOf(targetSelection.status, stringOf(contract.status, readinessVerdict.label));
  const actionLabel = runtimeDecisionActionLabel(recommendedAction);
  const decisionDetail = compactMetricDetail(
    stringOf(contract.detail, readinessVerdict.nextAction)
  );
  const tone = executionContractTone({
    action: recommendedAction,
    decisionStatus,
    productionAdmission,
    readinessVerdict
  });
  const candidates = runtimeDecisionCandidates(contract, runtimeFit, selectedRuntimeTargetId, bestRuntimeTargetId);
  const targetAssessments = runtimeTargetAssessments(contract, runtimeFit, candidates);
  const blockingGates = runtimeDecisionGates(contract.blocking_gates);
  const attentionGates = runtimeDecisionGates(contract.attention_gates);
  const canSelectBest =
    bestRuntimeTargetId &&
    selectedRuntimeTargetId &&
    bestRuntimeTargetId !== selectedRuntimeTargetId &&
    !bestRuntimeTargetId.includes("missing");

  return (
    <section className={`execution-contract execution-contract-${tone}`} aria-labelledby="execution-contract-heading">
      <div className="execution-contract-header">
        <div>
          <span className="section-kicker">Edge execution contract</span>
          <h2 id="execution-contract-heading">
            {executionContractHeadline(recommendedAction, decisionStatus, readinessVerdict)}
          </h2>
          <p>{decisionDetail}</p>
        </div>
        <div className="execution-contract-decision" aria-label="Runtime decision">
          <Badge value={actionLabel} />
          <strong>
            {selectedRuntimeTargetId}
            {bestRuntimeTargetId && bestRuntimeTargetId !== selectedRuntimeTargetId
              ? ` -> ${bestRuntimeTargetId}`
              : ""}
          </strong>
          <small>
            {selectedScore !== undefined ? `${selectedScore}/100 selected` : runtimeFitDisplay.label}
            {bestScore !== undefined && bestRuntimeTargetId !== selectedRuntimeTargetId
              ? ` / ${bestScore}/100 best`
              : ""}
            {scoreDelta !== undefined && scoreDelta > 0 ? ` / +${formatMetricNumber(scoreDelta)} fit` : ""}
          </small>
          {canSelectBest ? (
            <Button
              icon={<GitBranch size={16} />}
              variant="secondary"
              onClick={() => onSelectRuntimeTarget(bestRuntimeTargetId)}
            >
              Use best runtime
            </Button>
          ) : null}
        </div>
      </div>

      <div className="execution-path" aria-label="Selected model runtime edge path">
        <ExecutionPathNode label="Model" value={model?.id ?? "missing"} detail={model?.format ?? "artifact"} tone={model ? "good" : "bad"} />
        <ExecutionPathNode
          label="Runtime"
          value={runtime ? runtimeTargetId(runtime) : "missing"}
          detail={runtimeLaneValue(selectedLane)}
          tone={runtime ? runtimeLaneTone(selectedLane) : "bad"}
        />
        <ExecutionPathNode
          label="Edge"
          value={device ? deviceId(device) : "missing"}
          detail={device?.profile ?? runtimeInventoryLabel(device)}
          tone={device ? runtimeInventoryTone(device) : "bad"}
        />
      </div>

      <div className="execution-contract-grid" aria-label="On-device runtime capabilities">
        <CapabilityMetric
          label="Fit score"
          value={selectedScore !== undefined ? `${selectedScore}/100` : runtimeFitDisplay.label}
          detail={runtimeFitDisplay.detail}
          tone={runtimeFitDisplay.tone}
        />
        <CapabilityMetric
          label="Runtime lane"
          value={runtimeLaneValue(selectedLane)}
          detail={bestRuntimeTargetId !== selectedRuntimeTargetId && Object.keys(bestLane).length
            ? `best lane: ${runtimeLaneValue(bestLane)}`
            : runtimeLaneDetail(selectedLane)}
          tone={runtimeLaneTone(selectedLane)}
        />
        <CapabilityMetric
          label="Artifact path"
          value={artifactLaneValue(artifactLane)}
          detail={artifactLaneDetail(artifactLane)}
          tone={artifactLaneTone(artifactLane)}
        />
        <CapabilityMetric
          label="Capability lock"
          value={capabilityLockValue(capabilityLock)}
          detail={capabilityLockDetail(capabilityLock)}
          tone={capabilityLockTone(capabilityLock)}
        />
        <CapabilityMetric
          label="Resources"
          value={resourceEnvelopeFit.label}
          detail={resourceEnvelopeFit.detail}
          tone={resourceEnvelopeFit.tone}
        />
        <CapabilityMetric
          label="Admission"
          value={productionAdmissionValue(productionAdmission)}
          detail={productionAdmissionDetail(productionAdmission)}
          tone={productionAdmissionTone(productionAdmission)}
        />
      </div>

      <div className="execution-evidence-grid">
        <div className="execution-runtime-board" aria-label="Target runtime coverage">
          <div className="execution-subheader">
            <strong>Target runtime coverage</strong>
            <span>{targetAssessments.length ? `${targetAssessments.length} assessed` : "pending"}</span>
          </div>
          <div className="execution-candidate-list">
            {targetAssessments.length ? (
              targetAssessments.slice(0, 6).map((assessment) => (
                <TargetRuntimeAssessmentRow
                  key={`${candidateRuntimeId(assessment)}-${stringOf(assessment.status, "status")}`}
                  assessment={assessment}
                  bestRuntimeTargetId={bestRuntimeTargetId}
                  context={remediationContext}
                  onCopyRemediation={onCopyRemediation}
                  selectedRuntimeTargetId={selectedRuntimeTargetId}
                />
              ))
            ) : (
              <EmptyState title="No target coverage" detail="Runtime target assessments will appear after readiness evaluates this model and edge." />
            )}
          </div>
        </div>

        <div className="execution-runtime-board" aria-label="Measured runtime candidates">
          <div className="execution-subheader">
            <strong>Measured runtime candidates</strong>
            <span>{candidates.length ? `${candidates.length} ranked` : "pending"}</span>
          </div>
          <div className="execution-candidate-list">
            {candidates.length ? (
              candidates.map((candidate) => (
                <RuntimeCandidateRow
                  key={`${candidateRuntimeId(candidate)}-${stringOf(candidate.rank, "rank")}`}
                  bestRuntimeTargetId={bestRuntimeTargetId}
                  candidate={candidate}
                  selectedRuntimeTargetId={selectedRuntimeTargetId}
                />
              ))
            ) : (
              <EmptyState title="No measured candidates" detail="Record on-device benchmark and validation evidence for this model/runtime path." />
            )}
          </div>
        </div>

        <div className="execution-gate-board" aria-label="Runtime blockers and evidence gaps">
          <div className="execution-subheader">
            <strong>Runtime blockers and evidence gaps</strong>
            <span>{blockingGates.length + attentionGates.length || "clear"}</span>
          </div>
          <div className="execution-gate-list">
            {[...blockingGates, ...attentionGates].length ? (
              [...blockingGates, ...attentionGates].slice(0, 5).map((gate) => (
                <div className={`execution-gate execution-gate-${toneForReadinessStatus(stringOf(gate.status, ""))}`} key={`${stringOf(gate.gate_id, "gate")}-${stringOf(gate.status, "status")}`}>
                  <span>{stringOf(gate.label, stringOf(gate.gate_id, "Gate"))}</span>
                  <strong>{displayGateState(stringOf(gate.state, stringOf(gate.status, "review")))}</strong>
                  <small>{compactMetricDetail(stringOf(gate.detail, "Review gate evidence"))}</small>
                </div>
              ))
            ) : (
              <div className="execution-gate execution-gate-good">
                <span>Runtime gates</span>
                <strong>Aligned</strong>
                <small>
                  {runtimeValidation
                    ? `${selectedRuntimeTargetId} validation and admission evidence are available`
                    : edgeRuntimeFit.detail}
                </small>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

function ExecutionPathNode({
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

function RuntimeCandidateRow({
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

function TargetRuntimeAssessmentRow({
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
