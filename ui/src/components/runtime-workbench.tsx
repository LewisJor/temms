import { Activity, ArrowLeft, FileCheck2 } from "lucide-react";
import { deviceId, runtimeTargetId } from "../lib/hub-format";
import {
  artifactLaneDetail,
  artifactLaneTone,
  artifactLaneValue,
  capabilityLockDetail,
  capabilityLockTone,
  capabilityLockValue,
  formatArtifactSizeMb,
  runtimeInventoryDetail,
  runtimeInventoryLabel,
  runtimeInventoryTone,
  runtimeProviderDetail,
  runtimeProviderTone,
  runtimeProviderValue,
  runtimeTargetCapabilityDetail,
  runtimeTargetImageDetail,
  runtimeTargetImageValue
} from "../lib/runtime-fit";
import type {
  EdgeRuntimeFit,
  ModelRecord,
  RuntimeFitDisplay,
  RuntimeRemediationCommand,
  RuntimeRemediationContext,
  RuntimeWorkbenchRow
} from "../lib/workbench-types";
import type { Device, JsonObject, RuntimeTarget } from "../types";
import { EmptyState } from "./deploy-lists";
import { RuntimeDecisionTrace } from "./runtime-decision-trace";
import { Badge, Button, CapabilityMetric } from "./ui";

export function EdgeRuntimeWorkbench({
  artifactLane,
  capabilityLock,
  commandForRow,
  devices,
  modelCount,
  onCopyCommand,
  onGenerateProof,
  onGoHandling,
  onGoModels,
  onSelectDevice,
  onSelectRuntime,
  remediationContext,
  resourceEnvelopeFit,
  rows,
  runtimeFitDisplay,
  runtimeTargets,
  selectedDevice,
  selectedLane,
  selectedModel,
  selectedRuntime
}: {
  artifactLane: JsonObject;
  capabilityLock: JsonObject;
  commandForRow: (
    row: RuntimeWorkbenchRow,
    context: RuntimeRemediationContext
  ) => RuntimeRemediationCommand | undefined;
  devices: Device[];
  modelCount: number;
  onCopyCommand: (label: string, command: string) => void;
  onGenerateProof: () => void;
  onGoHandling: () => void;
  onGoModels: () => void;
  onSelectDevice: (id: string) => void;
  onSelectRuntime: (id: string) => void;
  remediationContext: RuntimeRemediationContext;
  resourceEnvelopeFit: EdgeRuntimeFit;
  rows: RuntimeWorkbenchRow[];
  runtimeFitDisplay: RuntimeFitDisplay;
  runtimeTargets: RuntimeTarget[];
  selectedDevice: Device | undefined;
  selectedLane: JsonObject;
  selectedModel: ModelRecord | undefined;
  selectedRuntime: RuntimeTarget | undefined;
}): JSX.Element {
  const bestRow = rows.find((row) => row.best) ?? rows[0];
  const selectedRow = rows.find((row) => row.selected);
  const proofDisabled = !selectedModel || !selectedDevice || !selectedRuntime;
  const modelRuntimeRequirements = [
    selectedModel?.runtimes.length ? `runtime ${selectedModel.runtimes.join(", ")}` : "",
    selectedModel?.providers.length ? `provider ${selectedModel.providers.join(", ")}` : "",
    selectedModel?.profiles.length ? `profile ${selectedModel.profiles.join(", ")}` : "",
    formatArtifactSizeMb(selectedModel?.artifactSizeMb)
  ].filter(Boolean);

  return (
    <section className="runtime-workbench" aria-labelledby="runtime-workbench-heading" data-testid="runtime-workbench">
      <div className="runtime-workbench-header">
        <div>
          <span className="section-kicker">Runtime workbench</span>
          <h2 id="runtime-workbench-heading">Target the model to the edge runtime</h2>
          <p>
            Compare the model selected in Model Plan against live edge inventory, runtime target validation,
            benchmark freshness, resource limits, and signed-proof gates.
          </p>
        </div>
        <div className="runtime-workbench-verdict">
          <Badge value={bestRow ? `best ${bestRow.targetId}` : "target pending"} />
          <strong>{selectedRow?.score !== undefined ? `${selectedRow.score}/100` : runtimeFitDisplay.label}</strong>
          <small>{selectedRow?.detail ?? runtimeFitDisplay.detail}</small>
        </div>
      </div>

      <div className="runtime-workbench-controls" aria-label="Runtime path controls">
        <div
          aria-label="Selected model from Model Plan"
          className="runtime-workbench-model-context"
          data-testid="runtime-workbench-model"
          id="runtime-workbench-model"
        >
          <div>
            <span>Selected model</span>
            <strong>{selectedModel?.name ?? "Model pending"}</strong>
            <small>
              {selectedModel
                ? `${selectedModel.id} / ${selectedModel.packageId}`
                : modelCount
                  ? "Open Model Plan to choose a signed model"
                  : "No signed models registered"}
            </small>
          </div>
          <Button icon={<ArrowLeft size={16} />} variant="secondary" onClick={onGoModels}>
            Model Plan
          </Button>
        </div>
        <label className="field" htmlFor="runtime-workbench-edge-node">
          <span>Edge node</span>
          <select
            aria-label="Edge node"
            data-testid="runtime-workbench-edge-node"
            id="runtime-workbench-edge-node"
            value={selectedDevice ? deviceId(selectedDevice) : ""}
            onChange={(event) => onSelectDevice(event.target.value)}
          >
            {devices.length ? null : <option value="">No edge nodes</option>}
            {devices.map((device) => (
              <option key={deviceId(device)} value={deviceId(device)}>
                {deviceId(device)} - {device.profile ?? "unknown profile"}
              </option>
            ))}
          </select>
        </label>
        <label className="field" htmlFor="runtime-workbench-target-runtime">
          <span>Target runtime</span>
          <select
            aria-label="Target runtime"
            data-testid="runtime-workbench-target-runtime"
            id="runtime-workbench-target-runtime"
            value={selectedRuntime ? runtimeTargetId(selectedRuntime) : ""}
            onChange={(event) => onSelectRuntime(event.target.value)}
          >
            {runtimeTargets.length ? null : <option value="">No runtime targets</option>}
            {runtimeTargets.map((target) => (
              <option key={runtimeTargetId(target)} value={runtimeTargetId(target)}>
                {runtimeTargetId(target)}
              </option>
            ))}
          </select>
        </label>
        <Button
          ariaLabel="Generate runtime proof for selected edge path"
          icon={<FileCheck2 size={16} />}
          testId="runtime-workbench-generate-proof"
          disabled={proofDisabled}
          onClick={onGenerateProof}
        >
          Generate proof
        </Button>
        <Button
          ariaLabel="Continue to Sensor Handling"
          icon={<Activity size={16} />}
          testId="runtime-workbench-go-handling"
          disabled={proofDisabled}
          onClick={onGoHandling}
        >
          Continue to Sensor Handling
        </Button>
      </div>

      <div className="runtime-capability-strip" aria-label="On-device runtime capability vector">
        <CapabilityMetric
          label="Runtime image"
          value={runtimeTargetImageValue(selectedRuntime)}
          detail={runtimeTargetImageDetail(selectedRuntime)}
          tone={selectedRuntime ? "good" : "bad"}
        />
        <CapabilityMetric
          label="Provider match"
          value={runtimeProviderValue(selectedLane)}
          detail={runtimeProviderDetail(selectedLane, selectedDevice)}
          tone={runtimeProviderTone(selectedLane, selectedDevice)}
        />
        <CapabilityMetric
          label="Artifact lane"
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
      </div>

      <div className="runtime-workbench-summary" aria-label="Selected edge runtime summary">
        <CapabilityMetric
          label="Selected fit"
          value={selectedRow?.score !== undefined ? `${selectedRow.score}/100` : runtimeFitDisplay.label}
          detail={selectedRow?.detail ?? runtimeFitDisplay.detail}
          tone={selectedRow?.tone ?? runtimeFitDisplay.tone}
        />
        <CapabilityMetric
          label="Best target"
          value={bestRow?.targetId ?? "pending"}
          detail={bestRow ? `${bestRow.status}; ${bestRow.detail}` : "runtime alternatives pending"}
          tone={bestRow?.tone ?? "neutral"}
        />
        <CapabilityMetric
          label="Model constraints"
          value={selectedModel?.format ?? "missing"}
          detail={modelRuntimeRequirements.join(" / ") || "model runtime constraints not declared"}
          tone={selectedModel ? "good" : "bad"}
        />
        <CapabilityMetric
          label="Edge inventory"
          value={runtimeInventoryLabel(selectedDevice)}
          detail={runtimeInventoryDetail(selectedDevice)}
          tone={runtimeInventoryTone(selectedDevice)}
        />
        <CapabilityMetric
          label="Resources"
          value={resourceEnvelopeFit.label}
          detail={resourceEnvelopeFit.detail}
          tone={resourceEnvelopeFit.tone}
        />
      </div>

      <div className="runtime-workbench-table" aria-label="Ranked target runtimes">
        <div className="runtime-workbench-table-head">
          <span>Target</span>
          <span>Fit</span>
          <span>Lane</span>
          <span>Proof</span>
          <span>Action</span>
        </div>
        {rows.length ? (
          rows.map((row) => (
            <div
              aria-label={`${row.targetId} runtime target ${row.status}`}
              aria-selected={row.selected}
              className={`runtime-workbench-row runtime-workbench-row-${row.tone}${
                row.selected ? " runtime-workbench-row-selected" : ""
              }`}
              data-runtime-target-id={row.targetId}
              data-testid={`runtime-workbench-row-${row.targetId}`}
              key={row.targetId}
            >
              <div>
                <strong>{row.targetId}</strong>
                <small>
                  {row.selected ? "selected" : row.best ? "best alternate" : row.status}
                  {row.best && row.selected ? " best" : ""}
                </small>
              </div>
              <div>
                <strong>{row.score !== undefined ? `${row.score}/100` : row.status}</strong>
                <small>{row.detail}</small>
              </div>
              <div>
                <strong>{row.lane}</strong>
                <small>{runtimeTargetCapabilityDetail(row.target)}</small>
              </div>
              <div>
                <strong>{row.validated ? "validated" : row.compatible ? "needs proof" : "blocked"}</strong>
                <small>{row.benchmark} / {row.inventory}</small>
              </div>
              <div className="runtime-workbench-row-action">
                <button
                  aria-label={
                    row.selected
                      ? `${row.targetId} is the selected runtime target`
                      : `Select runtime target ${row.targetId}`
                  }
                  className="button-mini"
                  data-testid={`runtime-workbench-select-${row.targetId}`}
                  disabled={row.selected}
                  type="button"
                  onClick={() => onSelectRuntime(row.targetId)}
                >
                  {row.selected ? "Selected" : "Select"}
                </button>
              </div>
            </div>
          ))
        ) : (
          <EmptyState title="No runtime targets" detail="Register target runtimes to compare deployment paths." />
        )}
      </div>

      {rows.length ? (
        <RuntimeDecisionTrace
          commandForRow={commandForRow}
          context={remediationContext}
          onCopyCommand={onCopyCommand}
          rows={rows}
        />
      ) : null}
    </section>
  );
}
