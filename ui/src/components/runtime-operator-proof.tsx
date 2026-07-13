import type { DeploymentReadiness, Device, JsonObject, RuntimeTarget } from "../types";
import { deviceId, runtimeTargetId } from "../lib/hub-format";
import { asRecord, numberOf, stringOf } from "../lib/json";
import {
  productionAdmissionDetail,
  productionAdmissionTone,
  productionAdmissionValue,
  runtimeFitScoreForProof,
  runtimeInventoryTone,
  runtimeTargetCapabilityDetail
} from "../lib/runtime-fit";
import {
  operatorRuntimeLaneItems,
  runtimeDecisionCandidates,
  runtimeTargetAssessments,
  targetRuntimeCoverageSummary
} from "../lib/runtime-decision";
import type {
  EdgeProofWorkflow,
  GateTone,
  ModelRecord,
  RuntimeFitDisplay
} from "../lib/workbench-types";
import { Badge } from "./ui";

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
