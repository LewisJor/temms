import type { DeploymentReadiness, Device, RuntimeTarget, RuntimeValidation } from "../types";
import { compactDate, deviceId, displayGateState, runtimeTargetId } from "../lib/hub-format";
import { asRecord, numberOf, stringOf, stringsOf } from "../lib/json";
import {
  artifactLaneDetail,
  artifactLaneTone,
  artifactLaneValue,
  deviceResourceSnapshot,
  formatMb,
  formatPower,
  formatTemperature,
  performanceSloDetail,
  performanceSloLabel,
  performanceSloTone,
  productionAdmissionDetail,
  productionAdmissionTone,
  productionAdmissionValue,
  runtimeFitComponentRows,
  runtimeLaneDetail,
  runtimeLaneFor,
  runtimeLaneTone,
  runtimeLaneValue,
  runtimeTargetInventoryConstraints,
  runtimeTargetSelectionDetail,
  runtimeTargetSelectionTone,
  runtimeTargetSelectionValue
} from "../lib/runtime-fit";
import type { EdgeRuntimeFit, ModelRecord, ReadinessVerdict } from "../lib/workbench-types";
import { Badge, CapabilityMetric } from "./ui";

export function CapabilityDossier({
  device,
  edgeRuntimeFit,
  model,
  readiness,
  readinessVerdict,
  resourceEnvelopeFit,
  runtime,
  runtimeValidation
}: {
  device: Device | undefined;
  edgeRuntimeFit: EdgeRuntimeFit;
  model: ModelRecord | undefined;
  readiness: DeploymentReadiness | undefined;
  readinessVerdict: ReadinessVerdict;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtime: RuntimeTarget | undefined;
  runtimeValidation: RuntimeValidation | undefined;
}): JSX.Element {
  const observed = device ? deviceResourceSnapshot(device) : {};
  const constraints = runtime ? runtimeTargetInventoryConstraints(runtime) : undefined;
  const inventory = asRecord(device?.inventory);
  const runtimes = Object.entries(asRecord(inventory.runtimes))
    .filter(([, status]) => asRecord(status).available === true)
    .map(([name]) => name);
  const providers = stringsOf(asRecord(asRecord(inventory.runtimes).onnxruntime).providers);
  const accelerators = Object.entries(asRecord(inventory.accelerators))
    .filter(([, status]) => asRecord(status).available === true)
    .map(([name]) => name);
  const apiGates = readiness?.gates ?? [];
  const attentionGates = apiGates.filter((gate) => toneForReadinessStatus(stringOf(gate.status, "")) !== "good");
  const selectedGate = attentionGates[0];
  const validationResult = asRecord(runtimeValidation?.result);
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const runtimeLane = runtimeLaneFor(runtimeFit, runtime);
  const artifactLane = asRecord(runtimeFit.artifact_lane);
  const productionAdmission = asRecord(readiness?.production_admission);
  const runtimeFitScore = numberOf(runtimeFit.score);
  const runtimeFitTier = stringOf(runtimeFit.tier, edgeRuntimeFit.label).replace(/_/g, " ");
  const runtimeFitDetail = stringOf(runtimeFit.detail, edgeRuntimeFit.detail);
  const targetSelection = asRecord(runtimeFit.target_selection);
  const runtimeFitComponents = runtimeFitComponentRows(runtimeFit);
  const runtimeFitTone =
    runtimeFit.tier === "blocked"
      ? "bad"
      : runtimeFit.tier === "needs_evidence"
        ? "warn"
        : runtimeFitScore !== undefined
          ? "good"
          : edgeRuntimeFit.tone;

  return (
    <div className="capability-dossier" aria-label="Selected on-device capability dossier">
      <div className="capability-dossier-header">
        <div>
          <span className="section-kicker">On-device capability dossier</span>
          <strong>{model ? `${model.id} on ${device ? deviceId(device) : "edge"}` : "Select a model path"}</strong>
        </div>
        <Badge value={readinessVerdict.label} />
      </div>
      <div className="capability-dossier-grid">
        <CapabilityMetric
          label="Runtime fit"
          value={runtimeFitScore !== undefined ? `${runtimeFitScore}/100` : edgeRuntimeFit.label}
          detail={runtimeFitScore !== undefined ? `${runtimeFitTier}: ${runtimeFitDetail}` : edgeRuntimeFit.detail}
          tone={runtimeFitTone}
        />
        <CapabilityMetric
          label="Runtime lane"
          value={runtimeLaneValue(runtimeLane)}
          detail={runtimeLaneDetail(runtimeLane)}
          tone={runtimeLaneTone(runtimeLane)}
        />
        <CapabilityMetric
          label="Artifact fit"
          value={artifactLaneValue(artifactLane)}
          detail={artifactLaneDetail(artifactLane)}
          tone={artifactLaneTone(artifactLane)}
        />
        <CapabilityMetric
          label="Target rank"
          value={runtimeTargetSelectionValue(targetSelection)}
          detail={runtimeTargetSelectionDetail(targetSelection)}
          tone={runtimeTargetSelectionTone(targetSelection)}
        />
        <CapabilityMetric
          label="Resource envelope"
          value={resourceEnvelopeFit.label}
          detail={resourceEnvelopeFit.detail}
          tone={resourceEnvelopeFit.tone}
        />
        <CapabilityMetric
          label="Performance proof"
          value={performanceSloLabel(model)}
          detail={model ? performanceSloDetail(model) : "select a model"}
          tone={performanceSloTone(model)}
        />
        <CapabilityMetric
          label="Production apply"
          value={productionAdmissionValue(productionAdmission)}
          detail={productionAdmissionDetail(productionAdmission)}
          tone={productionAdmissionTone(productionAdmission)}
        />
        <CapabilityMetric
          label="Validation"
          value={runtimeValidation ? "validated" : "not validated"}
          detail={
            runtimeValidation
              ? `${runtime ? runtimeTargetId(runtime) : "runtime target"} passed ${compactDate(runtimeValidation.created_at)}`
              : "run package validation before field rollout"
          }
          tone={runtimeValidation ? "good" : "warn"}
        />
      </div>

      <div className="capability-dossier-detail">
        <CapabilityBlock title="Runtime fit components" items={runtimeFitComponents} />
        <CapabilityBlock
          title="Live edge inventory"
          items={[
            ["RAM", formatMb(numberOf(observed.memoryAvailableMb))],
            ["Storage", formatMb(numberOf(observed.storageAvailableMb))],
            ["Thermal", formatTemperature(numberOf(observed.temperatureC))],
            ["Power", formatPower(observed)],
            ["Runtimes", runtimes.join(", ") || "not reported"],
            ["Providers", providers.join(", ") || "not reported"],
            ["Accelerators", accelerators.join(", ") || "none reported"]
          ]}
        />
        <CapabilityBlock
          title="Target requirements"
          items={[
            ["Model", model?.id ?? "missing"],
            ["Package", model?.packageId ?? "missing"],
            ["Runtime target", runtime ? runtimeTargetId(runtime) : "missing"],
            ["Lane", runtimeLaneValue(runtimeLane)],
            ["Artifact", artifactLaneValue(artifactLane)],
            ["Requires", constraints?.runtimes.join(", ") || model?.runtimes.join(", ") || "not declared"],
            ["Providers", constraints?.providers.join(", ") || constraints?.preferredProviders.join(", ") || "not declared"],
            ["Accelerators", constraints?.accelerators.join(", ") || (constraints?.requiresGpu ? "GPU required" : "not declared")],
            ["Validation result", runtimeValidation ? stringOf(validationResult.validation_state, "passed") : "missing"]
          ]}
        />
        <CapabilityBlock
          title="Admission gates"
          items={[
            ["Apply admission", productionAdmissionValue(productionAdmission)],
            ["Verdict", readinessVerdict.headline],
            ["Next action", readinessVerdict.nextAction],
            [
              "Review gate",
              selectedGate
                ? `${stringOf(selectedGate.label, stringOf(selectedGate.gate_id, "gate"))}: ${displayGateState(stringOf(selectedGate.state, stringOf(selectedGate.status, "unknown")))}`
                : "none"
            ],
            ["Gate detail", selectedGate ? stringOf(selectedGate.detail, "no detail") : "all gates aligned"],
            ["Checked", compactDate(readiness?.checked_at)]
          ]}
        />
      </div>
    </div>
  );
}

function CapabilityBlock({ items, title }: { items: [string, string][]; title: string }): JSX.Element {
  return (
    <div className="capability-block">
      <strong>{title}</strong>
      <dl>
        {items.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value || "-"}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function toneForReadinessStatus(status: string): "good" | "warn" | "bad" | "neutral" {
  const normalized = status.toLowerCase();
  if (["go", "ready", "passed", "healthy", "synced", "complete", "completed"].includes(normalized)) return "good";
  if (["blocked", "failed", "error", "critical"].includes(normalized)) return "bad";
  if (["attention", "pending", "syncing", "stale", "warning", "warn"].includes(normalized)) return "warn";
  return "neutral";
}
